import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cantok import SimpleToken

from throngtest.protocol import Request, WorkerError, encode
from throngtest.runner import execute
from throngtest.settings import Settings


def python_command(source: str) -> str:
    return shlex.join([sys.executable, '-c', source])


@pytest.mark.parametrize('source', ['cli', 'cli_separate', 'environment', 'toml'])
def test_preparation_runs_in_order_in_every_isolate(pytester: pytest.Pytester, backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """Run preparation once per isolate, in order, before starting its pytest process.

    Wrappers trace real backend calls per isolate, and the second command
    depends on the first command's marker. Invoking pytest from a subdirectory
    also checks preparation at the project root and test execution from the
    invocation directory, with preparation output forwarded to the controller.
    """
    commands = [
        python_command("from pathlib import Path; Path('first marker').write_text('ready'); print('preparation stdout')"),
        python_command("import sys; from pathlib import Path; assert Path('first marker').read_text() == 'ready'; Path('second marker').write_text('prepared'); print('preparation stderr', file=sys.stderr)"),
    ]
    pytester.makeini('[pytest]')
    pytester.makeconftest(f'''
        import json
        from pathlib import Path
        from uuid import uuid4
        from throng.extensions.local.isolate import LocalIsolate
        from throng.extensions.temporary_directory.isolate import TemporaryDirectoryIsolate
        from throngtest.settings import WORKER

        def observe(cls):
            original = cls.run
            def traced(self, command, *args, **kwargs):
                if not hasattr(self, '_trace_name'):
                    self._trace_name = uuid4().hex
                path = Path({str(tmp_path)!r}) / self._trace_name
                with path.open('a') as stream:
                    stream.write(json.dumps([cls.__name__, command]) + '\\n')
                return original(self, command, *args, **kwargs)
            cls.run = traced
        observe(LocalIsolate)
        observe(TemporaryDirectoryIsolate)

        def pytest_sessionstart(session):
            if session.config.stash.get(WORKER, False):
                assert (session.config.rootpath / 'second marker').read_text() == 'prepared'
    ''')
    child = pytester.path / 'nested'
    child.mkdir()
    (child / 'test_example.py').write_text('''
from pathlib import Path
import pytest
@pytest.mark.parametrize('index', range(4))
def test_prepared(index):
    assert (Path.cwd().parent / 'second marker').read_text() == 'prepared'
''')
    arguments = ['--isolates=2', f'--throngtest-backend={backend}']
    if source == 'cli':
        arguments.append('--throngtest-preparation=' + json.dumps(commands))
    elif source == 'cli_separate':
        arguments.extend(['--throngtest-preparation', json.dumps(commands)])
    elif source == 'environment':
        monkeypatch.setenv('THRONGTEST_PREPARATION', json.dumps(commands))
    else:
        pytester.makepyprojecttoml('[tool.throngtest]\npreparation = ' + json.dumps(commands))
    monkeypatch.chdir(child)
    result = pytester.runpytest_subprocess(*arguments, timeout=30)
    result.assert_outcomes(passed=4)
    assert result.stdout.str().count('preparation stdout') == 2
    assert result.stdout.str().count('preparation stderr') == 2
    traces = [path.read_text().splitlines() for path in tmp_path.iterdir()]
    assert len(traces) == 2
    expected_class = 'LocalIsolate' if backend == 'local' else 'TemporaryDirectoryIsolate'
    for trace in traces:
        calls = [json.loads(line) for line in trace if 'throngtest.coverage_transport import export' not in line]
        assert len(calls) == 3
        assert [call[0] for call in calls] == [expected_class] * 3
        assert [call[1] for call in calls[:2]] == commands
        assert 'from throngtest.worker import main' in calls[2][1]
    assert (pytester.path / 'second marker').exists() == (backend == 'local')


def test_preparation_failure_stops_commands_and_tests(pytester: pytest.Pytester, backend: str, tmp_path: Path) -> None:
    """Stop on the first preparation failure, retain diagnostics, and clean up the isolate.

    A shared marker would be created by either the remaining command or the
    test, so its absence checks both execution barriers. Another external file
    records the isolate directory for the cleanup assertion after failure.
    """
    directory = tmp_path / 'directory'
    forbidden = tmp_path / 'must-not-run'
    commands = [
        python_command(f"from pathlib import Path; Path({str(directory)!r}).write_text(str(Path.cwd())); print('earlier preparation output')"),
        python_command("import sys; print('failed preparation stdout'); print('failed preparation stderr', file=sys.stderr); sys.exit(7)"),
        python_command(f'from pathlib import Path; Path({str(forbidden)!r}).touch()'),
    ]
    pytester.makepyfile(f'from pathlib import Path\ndef test_never(): Path({str(forbidden)!r}).touch()')
    result = pytester.runpytest_subprocess('--isolates=1', f'--throngtest-backend={backend}', '--throngtest-preparation=' + json.dumps(commands), timeout=30)
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    output = result.stdout.str() + result.stderr.str()
    assert 'preparation command 2 failed with exit code 7' in output
    assert commands[1] in output
    assert 'earlier preparation output' in output
    assert 'failed preparation stdout' in output
    assert 'failed preparation stderr' in output
    assert not forbidden.exists()
    assert Path(directory.read_text()).exists() == (backend == 'local')


@pytest.mark.parametrize(('arguments', 'exitcode'), [(('--isolates=0',), pytest.ExitCode.OK), (('--collect-only',), pytest.ExitCode.OK), (('-k', 'not test_ok'), pytest.ExitCode.NO_TESTS_COLLECTED)])
def test_preparation_is_not_run_without_isolates(pytester: pytest.Pytester, tmp_path: Path, arguments: tuple, exitcode: pytest.ExitCode) -> None:
    """Skip preparation when isolates are disabled, only collecting, or no tests are selected."""
    marker = tmp_path / 'must-not-run'
    commands = [python_command(f'from pathlib import Path; Path({str(marker)!r}).touch()')]
    pytester.makepyfile('def test_ok(): pass')
    result = pytester.runpytest_subprocess(*arguments, '--throngtest-preparation=' + json.dumps(commands), timeout=30)
    assert result.ret == exitcode
    assert not marker.exists()


def test_preparation_failure_cancels_other_preparation(pytester: pytest.Pytester, tmp_path: Path) -> None:
    """Cancel another isolate's active preparation and remove both copies after a failure.

    An atomic mkdir elects one command to fail only after the other signals it
    has started sleeping. External markers reveal uncancelled completion and
    preserve both directory paths for checking cleanup after the run.
    """
    pytester.makepyfile('def test_a(): pass\ndef test_b(): pass')
    script = pytester.makepyfile(prepare=f'''
        import sys, time
        from pathlib import Path
        observer = Path({str(tmp_path)!r})
        try:
            (observer / 'leader').mkdir()
        except FileExistsError:
            (observer / 'slow-directory').write_text(str(Path.cwd()))
            (observer / 'started').touch()
            time.sleep(10)
            (observer / 'not-cancelled').touch()
        else:
            (observer / 'fast-directory').write_text(str(Path.cwd()))
            deadline = time.monotonic() + 10
            while not (observer / 'started').exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            sys.exit(9)
    ''')
    commands = [shlex.join([sys.executable, script.name])]
    result = pytester.runpytest_subprocess('--isolates=2', '--throngtest-preparation=' + json.dumps(commands), timeout=30)
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    assert 'preparation command 1 failed with exit code 9' in result.stdout.str() + result.stderr.str()
    assert not (tmp_path / 'not-cancelled').exists()
    assert not Path((tmp_path / 'fast-directory').read_text()).exists()
    assert not Path((tmp_path / 'slow-directory').read_text()).exists()


@pytest.mark.parametrize('failure', [RuntimeError('preparation backend error'), SimpleNamespace(stdout='worker crashed', stderr='', returncode=7)])
def test_preparation_cleanup_on_backend_error_and_worker_crash(failure: object) -> None:
    """Preserve preparation output and clean up after backend errors or a missing worker result.

    A mock first returns successful preparation output, then either raises in
    the next preparation command or returns a worker exit without a response.
    Every dispatched command must receive the same cancellation token.
    """
    manager = MagicMock()
    isolate = manager.scope.__enter__.return_value
    isolate.run.side_effect = [SimpleNamespace(stdout='preparation output', stderr=None, returncode=0), failure]
    token = SimpleToken()
    commands = ['prepare', 'prepare again'] if isinstance(failure, Exception) else ['prepare']
    settings = Settings(_sources=[])
    settings.preparation = commands
    with pytest.raises(WorkerError) as caught:
        execute(manager, Request([], '', 1, 'tests', 0, 'marker:', '.'), settings, token)
    assert isolate.run.call_count == 2
    assert all(call.kwargs['token'] is token for call in isolate.run.call_args_list)
    manager.scope.__exit__.assert_called_once()
    assert 'preparation output' in str(caught.value)
    if isinstance(failure, Exception):
        assert 'preparation command 2 could not execute: prepare again' in str(caught.value)
        assert 'preparation backend error' in str(caught.value)
    else:
        assert 'worker terminated without a result' in str(caught.value)


def test_preparation_accepts_commands_without_output() -> None:
    """Accept successful preparation whose stdout and stderr are both None."""
    manager = MagicMock()
    isolate = manager.scope.__enter__.return_value
    isolate.run.side_effect = [
        SimpleNamespace(stdout=None, stderr=None, returncode=0),
        SimpleNamespace(stdout='marker:' + encode({'version': 1, 'exitcode': 0}), stderr=None, returncode=0),
    ]
    settings = Settings(_sources=[])
    settings.preparation = ['prepare']
    result = execute(manager, Request([], '', 1, 'tests', 0, 'marker:', '.'), settings, SimpleToken())
    assert result['output'] == ''
    manager.scope.__exit__.assert_called_once()
