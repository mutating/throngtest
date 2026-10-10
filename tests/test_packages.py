import json
import shlex
import sys
from pathlib import Path

import pytest
from cantok import AbstractToken, SimpleToken
from throng import AbstractIsolate, throng

from throngtest.protocol import Request, WorkerError
from throngtest.runner import execute
from throngtest.settings import Settings


@pytest.fixture
def package_trace(pytester: pytest.Pytester, tmp_path: Path) -> Path:
    """Trace real manager lifecycles, replacing only the external pip command.

    Installation produces importable modules in each isolate without network
    access or changes to the developer's Python environment. Native install,
    scope, command failure policies, and cleanup still run unchanged.
    """
    pytester.makeconftest(f'''
        import json
        from pathlib import Path
        from typing import List, Optional, Union
        from uuid import uuid4
        from cantok import DefaultToken
        from throng import throng
        from throng.abstracts.results import SimpleRunResult
        from throng.extensions.local.manager import LocalManager
        from throng.extensions.local.isolate import LocalIsolate
        from throng.extensions.temporary_directory.manager import TemporaryDirectoryManager
        from throng.extensions.temporary_directory.isolate import TemporaryDirectoryIsolate

        observer = Path({str(tmp_path)!r})

        def record(isolate, operation, *values):
            with (observer / (isolate.trace_name + '.jsonl')).open('a') as stream:
                stream.write(json.dumps([operation, str(isolate.path), *values]) + '\\n')

        def observe(manager_class, isolate_class):
            original_get = manager_class._get
            original_install = isolate_class.install
            original_run = isolate_class._run
            original_kill = isolate_class.kill

            def get(self, state, token=DefaultToken()):
                assert self.path.is_absolute()
                isolate = original_get(self, state, token=token)
                isolate.trace_name = uuid4().hex
                isolate.expected_token = token
                record(isolate, 'get')
                return isolate

            def install(self, *packages, token=DefaultToken()):
                assert token is self.expected_token
                record(self, 'install', list(packages))
                return original_install(self, *packages, token=token)

            def run(self, command, token=DefaultToken()):
                assert token is self.expected_token
                record(self, 'run', command)
                if command.startswith('pip install '):
                    package = command[len('pip install '):]
                    failure = observer / 'fail'
                    if failure.exists() and failure.read_text() == package:
                        return SimpleRunResult(False, 17, 'installer stdout', 'installer stderr')
                    module = package.split('==')[0]
                    (self.path / (module + '.py')).write_text('VALUE = 42\\n')
                    return SimpleRunResult(True, 0)
                return original_run(self, command, token=token)

            def kill(self):
                if hasattr(self, 'trace_name') and not getattr(self, 'trace_killed', False):
                    record(self, 'kill')
                    self.trace_killed = True
                return original_kill(self)

            manager_class._get = get
            isolate_class.install = install
            isolate_class._run = run
            isolate_class.kill = kill

        observe(LocalManager, LocalIsolate)
        observe(TemporaryDirectoryManager, TemporaryDirectoryIsolate)

        @throng.plugin(unique=True)
        def custom(path: Union[str, Path] = '.', exclude: Optional[List[str]] = None,
                   prepare: Optional[List[str]] = None, packages: Optional[List[str]] = None) -> TemporaryDirectoryManager:
            assert prepare is None, 'project preparation must run only once, preserving output'
            return TemporaryDirectoryManager(path, exclude, prepare, packages)
    ''')
    return tmp_path


@pytest.mark.parametrize('source', ['cli', 'cli_separate', 'environment', 'toml'])
@pytest.mark.parametrize('backend', ['local', 'temporary_directory', 'custom'])
def test_packages_precede_preparation_and_tests(pytester: pytest.Pytester, backend: str, package_trace: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """Install the configured list once in every isolate, before preparation and collection.

    Invoking from a subdirectory checks that installation still uses the root.
    The controller collects without the new modules; preparation imports both,
    then worker collection imports them before the selected tests execute.
    """
    packages = ['dependency_a==1.0', 'dependency_b==2.0']
    preparation = shlex.join([sys.executable, '-c', 'import dependency_a, dependency_b; from pathlib import Path; Path("ready").write_text(str(dependency_a.VALUE + dependency_b.VALUE)); print("prepared packages")'])
    pytester.makeini('[pytest]')
    child = pytester.path / 'sub directory'
    child.mkdir()
    (child / 'test_dependencies.py').write_text('''
import pytest
from pathlib import Path
from throngtest.settings import WORKER

def pytest_generate_tests(metafunc):
    if metafunc.config.stash.get(WORKER, False):
        import dependency_a, dependency_b
        assert dependency_a.VALUE + dependency_b.VALUE == 84

@pytest.mark.parametrize('number', range(4))
def test_installed(number):
    assert Path.cwd().name == 'sub directory'
    assert (Path.cwd().parent / 'ready').read_text() == '84'
''')
    arguments = ['--isolates=2', f'--backend={backend}', '--preparation=' + json.dumps([preparation])]
    if source == 'cli':
        arguments.append('--packages=' + json.dumps(packages))
    elif source == 'cli_separate':
        arguments.extend(['--packages', json.dumps(packages)])
    elif source == 'environment':
        monkeypatch.setenv('THRONGTEST_PACKAGES', json.dumps(packages))
    else:
        pytester.makepyprojecttoml('[tool.throngtest]\npackages = ' + json.dumps(packages))
    monkeypatch.chdir(child)
    result = pytester.runpytest_subprocess(*arguments, timeout=30)
    result.assert_outcomes(passed=4)
    assert result.stdout.str().count('prepared packages') == 2
    traces = [list(map(json.loads, path.read_text().splitlines())) for path in package_trace.glob('*.jsonl')]
    assert len(traces) == 2
    for trace in traces:
        assert trace[0][0] == 'get'
        assert trace[1][0] == 'install'
        assert trace[1][2] == packages
        commands = [row[2] for row in trace if row[0] == 'run' and 'throngtest.coverage_transport import export' not in row[2]]
        assert commands[:3] == ['pip install ' + package for package in packages] + [preparation]
        assert len(commands) == 4
        assert 'from throngtest.worker import main' in commands[3]
        assert trace[-1][0] == 'kill'
        assert Path(trace[0][1]).exists() == (backend == 'local')


def test_installation_failure_stops_preparation_and_reports_output(pytester: pytest.Pytester, backend: str, package_trace: Path) -> None:
    """Expose the failing installer result and clean up without starting later work."""
    (package_trace / 'fail').write_text('broken==1.0')
    pytester.makepyfile('def test_never(): assert False')
    packages = ['dependency_a==1.0', 'broken==1.0', 'must_not_install']
    result = pytester.runpytest_subprocess(
        '--isolates=1', f'--backend={backend}', '--packages=' + json.dumps(packages),
        '--preparation=["must not execute"]', timeout=30,
    )
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    output = result.stdout.str() + result.stderr.str()
    assert f'isolate 1 (backend={backend}): installing packages failed' in output
    for detail in ('broken==1.0', 'InterruptedInstallationError', 'CannotInstallDependencyError', 'command return code: 17', 'installer stdout', 'installer stderr'):
        assert detail in output
    trace = list(map(json.loads, next(package_trace.glob('*.jsonl')).read_text().splitlines()))
    assert [row[2] for row in trace if row[0] == 'run'] == ['pip install dependency_a==1.0', 'pip install broken==1.0']
    assert trace[-1][0] == 'kill'
    assert Path(trace[0][1]).exists() == (backend == 'local')


@pytest.mark.parametrize(('arguments', 'exitcode'), [(('--isolates=0',), pytest.ExitCode.OK), (('--collect-only',), pytest.ExitCode.OK), (('-k', 'not test_ok'), pytest.ExitCode.NO_TESTS_COLLECTED)])
def test_packages_are_not_installed_without_isolates(pytester: pytest.Pytester, package_trace: Path, arguments: tuple, exitcode: pytest.ExitCode) -> None:
    """Do not allocate environments or install packages when no isolate is needed."""
    pytester.makepyfile('def test_ok(): pass')
    result = pytester.runpytest_subprocess(*arguments, '--packages=["must_not_install"]', timeout=30)
    assert result.ret == exitcode
    assert not list(package_trace.glob('*.jsonl'))


@pytest.mark.parametrize('empty_override', [False, True])
def test_empty_packages_skip_installation(pytester: pytest.Pytester, backend: str, package_trace: Path, empty_override: bool) -> None:
    """Both the default and an explicit empty override bypass installation."""
    pytester.makepyfile('def test_ok(): pass')
    arguments = []
    if empty_override:
        pytester.makepyprojecttoml('[tool.throngtest]\npackages = ["must_not_install"]')
        arguments.append('--packages=[]')
    result = pytester.runpytest_subprocess('--isolates=1', f'--backend={backend}', *arguments, timeout=30)
    result.assert_outcomes(passed=1)
    trace = list(map(json.loads, next(package_trace.glob('*.jsonl')).read_text().splitlines()))
    assert 'install' not in [row[0] for row in trace]


def test_installed_dependencies_do_not_turn_test_failures_into_backend_errors(pytester: pytest.Pytester, backend: str, package_trace: Path) -> None:
    """Preserve pytest's exit code, reports, and cleanup after installing dependencies."""
    pytester.makepyfile('def test_failure():\n    import dependency_a\n    assert dependency_a.VALUE == 0')
    result = pytester.runpytest_subprocess('--isolates=1', f'--backend={backend}', '--packages=["dependency_a==1.0"]', '--junitxml=result.xml', timeout=30)
    result.assert_outcomes(failed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert 'assert 42 == 0' in result.stdout.str()
    assert 'NotSuccessfulRunError' not in result.stdout.str() + result.stderr.str()
    assert '<failure' in (pytester.path / 'result.xml').read_text()
    trace = list(map(json.loads, next(package_trace.glob('*.jsonl')).read_text().splitlines()))
    assert trace[-1][0] == 'kill'


def test_cancelled_token_prevents_installation(tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A previously cancelled job may allocate a copy but must never start pip."""
    manager = throng(tmp_path, packages=['must_not_install'])[backend]
    original = manager._get
    directories = []
    token = SimpleToken()
    token.cancel()

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail('a cancelled installation started a command')

    def get(state: bytes, token: AbstractToken) -> AbstractIsolate:
        isolate = original(state, token=token)
        directories.append(isolate.path)  # type: ignore[attr-defined]  # Both built-in isolates expose their directory.
        monkeypatch.setattr(isolate, '_run', unexpected_run)
        return isolate

    monkeypatch.setattr(manager, '_get', get)
    settings = Settings(_sources=[])
    settings.backend = backend
    with pytest.raises(WorkerError) as caught:
        execute(manager, Request([], None, 1, 'tests', 0, 'marker:', '.'), settings, token, tmp_path)
    assert 'installing packages failed' in str(caught.value)
    assert 'must_not_install' in str(caught.value)
    assert 'cancelled' in str(caught.value)
    assert len(directories) == 1
    assert directories[0].exists() == (backend == 'local')
