"""Coverage agents and transport tests."""

import base64
import builtins
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Dict, Iterator, List, cast
from unittest.mock import MagicMock

import coverage
import pytest
from cantok import SimpleToken
from coverage import CoverageData

from throngtest.coverage import (
    CoverageAgent,
    PytestCovAgent,
    PythonCoverageAgent,
    coverage_agents,
)
from throngtest.coverage_transport import export, marker_path, receive, worker_data_file
from throngtest.protocol import Request, WorkerError, encode, read_response
from throngtest.runner import (
    coverage_plan,
    execute,
    isolate_coverage_environment,
    shared_local_coverage,
)
from throngtest.settings import Settings
from throngtest.worker import main as worker_main


def test_builtin_agents_are_registered() -> None:
    """Expose both coverage.py and pytest-cov through the pristan slot."""
    agents = coverage_agents()
    assert isinstance(agents['python_coverage'], PythonCoverageAgent)
    assert isinstance(agents['pytest_cov'], PytestCovAgent)


def test_coverage_plan_without_active_agents(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the protocol unchanged when no coverage provider is running."""
    monkeypatch.setattr('throngtest.runner.coverage_agents', dict)
    assert coverage_plan(pytester.parseconfigure(), 'local') == ([], None, None)


def test_python_coverage_detection_and_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Copy the running coverage configuration and avoid duplicate pytest-cov agents."""
    config_file = tmp_path / 'coverage.toml'
    config_file.write_text('[run]\nbranch = true\n')
    current = SimpleNamespace(config=SimpleNamespace(config_file=str(config_file), source=['package'], branch=True, data_file='.coverage'))
    monkeypatch.setattr(coverage.Coverage, 'current', lambda: current)
    agent = PythonCoverageAgent()
    config = MagicMock()
    config.getoption.return_value = []
    assert agent.active(config)
    assert agent.configuration(tmp_path) == {'suffix': '.toml', 'text': config_file.read_text(), 'source': ['package'], 'branch': True, 'root': str(tmp_path)}
    config.getoption.return_value = ['package']
    assert not agent.active(config)
    monkeypatch.setattr(coverage.Coverage, 'current', lambda: None)
    assert not agent.active(config)

    original_import = builtins.__import__

    def without_coverage(name: str, *_args: object, **_kwargs: object) -> object:
        if name == 'coverage':
            raise ImportError('coverage is unavailable')
        return original_import(name)

    monkeypatch.setattr(builtins, '__import__', without_coverage)
    assert not agent.active(config)


def test_python_coverage_worker_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start a separate tracer with controller settings, then save and remove its config.

    A fake Coverage class keeps this unit test independent of the tracer that
    instruments the repository's own CI run.
    """
    instances: List[object] = []

    class FakeCoverage:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.saved = False
            self.config = SimpleNamespace(data_file='.coverage.default')
            instances.append(self)

        @staticmethod
        def current() -> None:
            return None

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def save(self) -> None:
            self.saved = True

    monkeypatch.setattr(coverage, 'Coverage', FakeCoverage)
    monkeypatch.setenv('COVERAGE_FILE', 'original')
    monkeypatch.chdir(tmp_path)
    agent = PythonCoverageAgent()
    agent.start_worker('marker:', {'suffix': '.toml', 'text': '[tool.coverage.run]\nbranch = true\n', 'source': ['app'], 'branch': True}, tmp_path)
    assert os.environ['COVERAGE_FILE'] == worker_data_file('marker:')
    assert agent.config_path is not None
    assert agent.config_path.read_text().startswith('[tool.coverage.run]')
    assert cast(FakeCoverage, instances[0]).kwargs == {'config_file': str(agent.config_path), 'source': ['app'], 'branch': True}
    assert cast(FakeCoverage, instances[0]).started
    config_path = agent.config_path
    agent.stop_worker()
    assert cast(FakeCoverage, instances[0]).stopped
    assert cast(FakeCoverage, instances[0]).saved
    assert not config_path.exists()

    plain = PythonCoverageAgent()
    plain.start_worker('other:', {}, tmp_path)
    assert cast(FakeCoverage, instances[1]).kwargs == {'config_file': True, 'source': None, 'branch': None}
    assert plain.config_path is None
    plain.stop_worker()
    monkeypatch.setattr(FakeCoverage, 'current', lambda: object())
    inactive = PythonCoverageAgent()
    inactive.start_worker('unused:', {}, tmp_path)
    inactive.stop_worker()
    assert len(instances) == 2

    monkeypatch.setattr(FakeCoverage, 'current', lambda: None)
    posix = PythonCoverageAgent()
    posix.start_worker('posix:', {'root': '/controller/project', 'source': ['/controller/project/src', 'installed_package']}, tmp_path)
    assert cast(FakeCoverage, instances[2]).kwargs['source'] == [str(tmp_path / 'src'), 'installed_package']
    posix.stop_worker()
    windows = PythonCoverageAgent()
    windows.start_worker('windows:', {'root': r'C:\controller\project', 'source': [r'C:\controller\project\src']}, tmp_path)
    assert cast(FakeCoverage, instances[3]).kwargs['source'] == [str(tmp_path / 'src')]
    windows.stop_worker()


def test_pytest_cov_worker_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace an embedded subprocess tracer before pytest-cov starts its own."""
    cleanups: List[bool] = []
    monkeypatch.setenv('COVERAGE_FILE', 'original')
    monkeypatch.setattr('throngtest.coverage.import_module', lambda _name: SimpleNamespace(cleanup=lambda: cleanups.append(True)))
    agent = PytestCovAgent()
    config = MagicMock()
    config.getoption.side_effect = lambda key, **_kwargs: ['app'] if key == 'cov_source' else False
    assert agent.active(config)
    assert agent.pytest_arguments() == ['--cov-fail-under=0']
    agent.start_worker('marker:', {}, Path.cwd())
    assert cleanups == [True]
    assert agent.data_file() == worker_data_file('marker:')
    assert os.environ['COVERAGE_FILE'] == agent.data_file()
    agent.stop_worker()
    config.getoption.side_effect = lambda key, **_kwargs: True if key == 'no_cov' else ['app']
    assert not agent.active(config)

    def missing_embed(_name: str) -> object:
        raise ImportError('pytest-cov 7 has no embed module')

    monkeypatch.setattr('throngtest.coverage.import_module', missing_embed)
    agent.start_worker('second:', {}, Path.cwd())
    assert agent.data_file() == worker_data_file('second:')


def test_coverage_agent_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide default arguments and data-file discovery for external agents."""
    class Extra(CoverageAgent):
        def active(self, _config: pytest.Config) -> bool:
            return False

        def start_worker(self, _marker: str, _configuration: Dict[str, object], _root: Path) -> None:
            pass

        def stop_worker(self) -> None:
            pass

    extra = Extra()
    assert extra.configuration(Path.cwd()) == {}
    assert extra.pytest_arguments() == []
    current = SimpleNamespace(config=SimpleNamespace(data_file='current.db'))
    monkeypatch.setattr(coverage.Coverage, 'current', lambda: current)
    assert extra.data_file() == 'current.db'
    class NoCurrentCoverage:
        def __init__(self) -> None:
            self.config = SimpleNamespace(data_file='default.db')

        @staticmethod
        def current() -> None:
            return None

    monkeypatch.setattr(coverage, 'Coverage', NoCurrentCoverage)
    assert extra.data_file() == 'default.db'


def test_external_agent_is_loaded_from_pristan_entrypoint(tmp_path: Path) -> None:
    """Discover a third-party agent through the documented entry-point group.

    Discovery runs in a child process so registration cannot leak into other
    tests through pristan's process-global slot.
    """
    (tmp_path / 'extra_agent.py').write_text('''
from throngtest.coverage import CoverageAgent, coverage_agents
class Extra(CoverageAgent):
    def active(self, config): return False
    def start_worker(self, marker, configuration, root): pass
    def stop_worker(self): pass
@coverage_agents.plugin(unique=True)
def extra(): return Extra()
''')
    metadata = tmp_path / 'throngtest_extra-0.0.0.dist-info'
    metadata.mkdir()
    (metadata / 'METADATA').write_text('Name: throngtest-extra\nVersion: 0.0.0\n')
    (metadata / 'entry_points.txt').write_text('[throngtest.coverage]\nextra = extra_agent:extra\n')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(tmp_path) + os.pathsep + environment.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, '-c', 'from throngtest.coverage import coverage_agents; print(sorted(coverage_agents()))'], cwd=tmp_path, env=environment, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "['extra', 'pytest_cov', 'python_coverage']" in result.stdout


def test_controller_coverage_paths_are_not_passed_to_isolates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide controller paths only during isolate commands and restore them afterward."""
    monkeypatch.setenv('COVERAGE_PROCESS_START', '/controller/pyproject.toml')
    monkeypatch.setenv('COV_CORE_DATAFILE', '/controller/.coverage')
    with isolate_coverage_environment(False):
        assert os.environ['COVERAGE_PROCESS_START'] == '/controller/pyproject.toml'
    with isolate_coverage_environment(True):
        assert 'COVERAGE_PROCESS_START' not in os.environ
        assert 'COV_CORE_DATAFILE' not in os.environ
    assert os.environ['COVERAGE_PROCESS_START'] == '/controller/pyproject.toml'
    assert os.environ['COV_CORE_DATAFILE'] == '/controller/.coverage'


def test_existing_shared_local_coverage_needs_no_transfer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Recognize the one coverage.py case already handled by local startup hooks."""
    monkeypatch.delenv('COVERAGE_PROCESS_START', raising=False)
    monkeypatch.delenv('COVERAGE_FILE', raising=False)
    assert not shared_local_coverage('local')
    monkeypatch.setenv('COVERAGE_PROCESS_START', str(tmp_path / 'pyproject.toml'))
    monkeypatch.setenv('COVERAGE_FILE', '.coverage')
    assert not shared_local_coverage('temporary_directory')
    monkeypatch.setenv('COVERAGE_FILE', str(tmp_path / '.coverage'))
    assert shared_local_coverage('local')
    assert shared_local_coverage('temporary_directory')
    assert not shared_local_coverage('remote_backend')


def test_all_active_agents_are_selected(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Select every attached agent, including third-party implementations.

    A fake pristan result avoids changing the process-global slot during the
    remaining test suite and verifies that both providers stay in the plan.
    """
    class ExtraAgent(CoverageAgent):
        def active(self, _config: pytest.Config) -> bool:
            return True

        def start_worker(self, _marker: str, _configuration: Dict[str, object], _root: Path) -> None:
            pass

        def stop_worker(self) -> None:
            pass

        def data_file(self) -> str:
            return str(tmp_path / 'extra.db')

    agents = {'first': ExtraAgent(), 'second': ExtraAgent()}
    monkeypatch.setattr('throngtest.runner.coverage_agents', lambda: agents)
    enabled, targets, configurations = coverage_plan(pytester.parseconfigure(), 'local')
    assert enabled == ['first', 'second']
    assert targets == {name: str(tmp_path / 'extra.db') for name in enabled}
    assert configurations == {'first': {}, 'second': {}}


def test_shared_coverage_keeps_other_agents(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Skip only coverage.py when its local startup tracer already writes shared data."""
    class ActiveAgent(CoverageAgent):
        def active(self, _config: pytest.Config) -> bool:
            return True

        def start_worker(self, _marker: str, _configuration: Dict[str, object], _root: Path) -> None:
            pass

        def stop_worker(self) -> None:
            pass

        def data_file(self) -> str:
            return str(tmp_path / '.coverage')

    monkeypatch.setattr('throngtest.runner.coverage_agents', lambda: {'python_coverage': ActiveAgent(), 'external': ActiveAgent()})
    monkeypatch.setenv('COVERAGE_PROCESS_START', str(tmp_path / 'pyproject.toml'))
    monkeypatch.setenv('COVERAGE_FILE', str(tmp_path / '.coverage'))
    assert coverage_plan(pytester.parseconfigure(), 'local')[0] == ['external']
    assert coverage_plan(pytester.parseconfigure(), 'temporary_directory')[0] == ['external']
    assert coverage_plan(pytester.parseconfigure(), 'remote')[0] == ['python_coverage', 'external']


def test_worker_starts_all_selected_agents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Start each selected agent before pytest and stop each after it exits.

    Replacing pytest.main keeps the protocol and lifecycle visible without
    launching a second pytest session inside the current test process.
    """
    events: List[str] = []

    class Extra(CoverageAgent):
        def __init__(self, name: str) -> None:
            self.name = name

        def active(self, _config: pytest.Config) -> bool:
            return True

        def start_worker(self, marker: str, configuration: Dict[str, object], root: Path) -> None:
            events.append(f'start {self.name} {marker} {configuration["value"]}')
            assert root == tmp_path

        def stop_worker(self) -> None:
            events.append(f'stop {self.name}')

        def data_file(self) -> str:
            return f'.coverage.{self.name}'

        def pytest_arguments(self) -> List[str]:
            return [f'--agent={self.name}']

    request = Request([], None, 1, 'tests', 0, 'marker:', '.', ['first', 'second'], coverage_configurations={'first': {'value': 1}, 'second': {'value': 2}})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PYTEST_ADDOPTS', '--wrong-option')
    monkeypatch.setattr('throngtest.worker.coverage_agents', lambda: {'first': Extra('first'), 'second': Extra('second')})

    def fake_pytest_main(arguments: List[str], plugins: List[object]) -> pytest.ExitCode:
        assert arguments == ['--agent=first', '--agent=second', '-o', 'addopts=']
        assert 'PYTEST_ADDOPTS' not in os.environ
        assert len(plugins) == 1
        assert json.loads(marker_path(request.marker).read_text()) == {'first': '.coverage.first', 'second': '.coverage.second'}
        events.append('pytest')
        return pytest.ExitCode.OK

    monkeypatch.setattr(pytest, 'main', fake_pytest_main)
    with pytest.raises(SystemExit) as caught:
        worker_main(request.pack())
    assert caught.value.code == 0
    assert events == ['start first marker: 1', 'start second marker: 2', 'pytest', 'stop first', 'stop second']
    assert read_response(capsys.readouterr().out, request.marker)['exitcode'] == 0


@pytest.mark.parametrize('exitcode', [0, 1])
def test_coverage_export_is_received_before_replay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exitcode: int) -> None:
    """Fetch coverage after passing or failing tests, before replaying reports."""
    manager = MagicMock()
    request = Request([], None, 1, 'tests', 0, 'marker:', '.', ['provider'], {'provider': str(tmp_path / '.coverage')})
    manager.scope.__enter__.return_value.run.side_effect = [
        SimpleNamespace(stdout='before\nmarker:' + encode({'version': 1, 'exitcode': exitcode}) + '\n', stderr='', returncode=exitcode),
        SimpleNamespace(stdout='marker:' + encode({'version': 1, 'root': '/remote', 'files': {'provider': ['encoded']}}), stderr='', returncode=0),
    ]
    received: List[object] = []
    monkeypatch.setattr('throngtest.runner.receive', lambda payload, targets, root: received.append((payload, targets, root)))
    response = execute(manager, request, Settings(_sources=[]), SimpleToken())
    assert response['exitcode'] == exitcode
    assert response['output'] == 'before'
    assert received == [({'version': 1, 'root': '/remote', 'files': {'provider': ['encoded']}}, request.coverage_targets, Path.cwd())]
    assert manager.scope.__enter__.return_value.run.call_count == 2


@pytest.mark.parametrize('remote_name', ['/remote/project/app.py', r'C:\remote\project\app.py'])
def test_receive_restores_data_without_remote_files(tmp_path: Path, remote_name: str) -> None:
    """Rebuild coverage locally after the remote directory has disappeared.

    The remote database exists only long enough to become stdout payload;
    path remapping is checked for POSIX and Windows worker paths.
    """
    remote = '/remote/project' if remote_name.startswith('/') else r'C:\remote\project'
    source = CoverageData(basename=str(tmp_path / 'remote-data'))
    source.add_lines({remote_name: {2}})
    source.write()
    payload: Dict[str, object] = {
        'root': remote,
        'files': {'provider': [base64.b64encode((tmp_path / 'remote-data').read_bytes()).decode('ascii')]},
    }
    (tmp_path / 'remote-data').unlink()
    target = tmp_path / '.coverage'
    receive(payload, {'provider': str(target)}, tmp_path)
    restored = list(tmp_path.glob('.coverage.*'))
    assert len(restored) == 1
    data = CoverageData(basename=str(restored[0]))
    data.read()
    assert data.lines(str(tmp_path / 'app.py')) == [2]


def test_receive_preserves_sources_outside_remote_project(tmp_path: Path) -> None:
    """Keep external source paths when remapping only the isolate project root."""
    source = CoverageData(basename=str(tmp_path / 'remote-data'))
    source.add_lines({'/external/library.py': {3}})
    source.write()
    payload: Dict[str, object] = {
        'root': '/remote/project',
        'files': {'provider': [base64.b64encode((tmp_path / 'remote-data').read_bytes()).decode('ascii')]},
    }
    receive(payload, {'provider': str(tmp_path / '.coverage')}, tmp_path)
    restored = CoverageData(basename=str(next(tmp_path.glob('.coverage.*'))))
    restored.read()
    assert restored.lines('/external/library.py') == [3]


def test_worker_data_file_is_unique_per_isolate() -> None:
    """Give each isolate a stable, distinct data-file prefix."""
    assert worker_data_file('first:') == worker_data_file('first:')
    assert worker_data_file('first:') != worker_data_file('second:')


def test_export_ignores_stale_data_and_removes_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Send only databases produced after the worker starts.

    Existing coverage files may be present in the copied project, so the
    marker separates this run's data from stale input.
    """
    request = Request([], None, 1, 'tests', 0, 'unique:', '.', ['provider'])
    monkeypatch.chdir(tmp_path)
    old = CoverageData(basename=str(tmp_path / '.coverage.old'))
    old.add_lines({str(tmp_path / 'old.py'): {1}})
    old.write()
    os.utime(tmp_path / '.coverage.old', (1, 1))
    marker_path(request.marker).write_text(json.dumps({'provider': '.coverage'}))
    new = CoverageData(basename=str(tmp_path / '.coverage.new'))
    new.add_lines({str(tmp_path / 'new.py'): {2}})
    new.write()
    export(request.pack())
    response = read_response(capsys.readouterr().out, request.marker)
    assert len(cast(Dict[str, list], response['files'])['provider']) == 1
    assert not marker_path(request.marker).exists()


def test_export_accepts_absolute_data_file_and_skips_non_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Find an absolute provider data path and ignore adjacent non-databases."""
    request = Request([], None, 1, 'tests', 0, 'absolute:', '.', ['provider'])
    monkeypatch.chdir(tmp_path)
    data_file = tmp_path / '.coverage.absolute'
    marker_path(request.marker).write_text(json.dumps({'provider': str(data_file)}))
    source = CoverageData(basename=str(data_file))
    source.add_lines({str(tmp_path / 'app.py'): {4}})
    source.write()
    (tmp_path / '.coverage.absolute-junk').write_text('not a database')
    export(request.pack())
    response = read_response(capsys.readouterr().out, request.marker)
    assert len(cast(Dict[str, list], response['files'])['provider']) == 1


@pytest.mark.parametrize('payload', [
    {},
    {'root': '/remote', 'files': []},
    {'root': '/remote', 'files': {}},
    {'root': '/remote', 'files': {'provider': []}},
    {'root': '/remote', 'files': {'provider': [42]}},
    {'root': '/remote', 'files': {'provider': ['bad-base64!']}},
    {'root': '/remote', 'files': {'provider': [base64.b64encode(b'not sqlite').decode('ascii')]}},
    {'root': '/remote', 'files': {'provider': [base64.b64encode(b'SQLite format 3\x00garbage').decode('ascii')]}},
])
def test_bad_coverage_payload_is_rejected(tmp_path: Path, payload: Dict[str, object]) -> None:
    """Never mistake missing or malformed remote data for successful coverage."""
    with pytest.raises(WorkerError, match='coverage data'):
        receive(payload, {'provider': str(tmp_path / '.coverage')}, tmp_path)


def test_bad_coverage_database_is_released_before_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Close a damaged SQLite database before removing its temporary directory.

    The wrapper checks the file before directory cleanup, mirroring the point
    where Windows rejects deletion of a database with an open connection.
    """
    @contextmanager
    def checked_temporary_directory(prefix: str) -> Iterator[str]:
        with TemporaryDirectory(prefix=prefix) as directory:
            try:
                yield directory
            finally:
                assert not (Path(directory) / 'data').exists()

    monkeypatch.setattr('throngtest.coverage_transport.TemporaryDirectory', checked_temporary_directory)
    damaged = base64.b64encode(b'SQLite format 3\x00garbage').decode('ascii')
    payload: Dict[str, object] = {'root': '/remote', 'files': {'provider': [damaged]}}
    with pytest.raises(WorkerError, match='malformed coverage data'):
        receive(payload, {'provider': str(tmp_path / '.coverage')}, tmp_path)


def test_export_failure_is_reported() -> None:
    """Fail the run when the isolate cannot return coverage after its tests."""
    manager = MagicMock()
    manager.scope.__enter__.return_value.run.side_effect = [
        SimpleNamespace(stdout='marker:', stderr='', returncode=0),
        SimpleNamespace(stdout='', stderr='transfer failed', returncode=7),
    ]
    request = Request([], None, 1, 'tests', 0, 'marker:', '.', ['provider'], {'provider': '.coverage'})
    with pytest.raises(WorkerError, match='transfer failed'):
        execute(manager, request, Settings(_sources=[]), SimpleToken())


@pytest.mark.parametrize('backend', ['local', 'temporary_directory'])
@pytest.mark.parametrize('provider', ['coverage', 'pytest-cov'])
def test_real_coverage_reaches_controller(pytester: pytest.Pytester, backend: str, provider: str) -> None:
    """Combine both isolate subsets after temporary copies are destroyed.

    Child processes do not inherit the repository's coverage startup or data
    path. Each branch is exercised only in an isolate, so 100% proves that its
    database crossed the command-output transport and was remapped correctly.
    """
    pytester.makepyfile(app='''
        def classify(value):
            if value > 0:
                return 'positive'
            return 'other'
    ''', test_positive='''
        from app import classify
        def test_positive(): assert classify(1) == 'positive'
    ''', test_other='''
        from app import classify
        def test_other(): assert classify(0) == 'other'
    ''')
    pytester.makefile('.toml', pyproject='''
        [tool.coverage.run]
        source = ["app"]
        branch = true
        parallel = true
    ''')
    environment = dict(os.environ)
    environment.pop('COVERAGE_PROCESS_START', None)
    environment.pop('COVERAGE_FILE', None)
    arguments = [sys.executable, '-m', 'coverage', 'run', '-m', 'pytest'] if provider == 'coverage' else [sys.executable, '-m', 'pytest', '--cov=app', '--cov-branch', '--cov-fail-under=100']
    result = subprocess.run([*arguments, '-q', '--isolates=2', f'--throngtest-backend={backend}'], cwd=pytester.path, env=environment, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    if provider == 'coverage':
        combined = subprocess.run([sys.executable, '-m', 'coverage', 'combine', '-q'], cwd=pytester.path, env=environment, text=True, capture_output=True, check=False)
        assert combined.returncode == 0, combined.stdout + combined.stderr
    report = subprocess.run([sys.executable, '-m', 'coverage', 'json', '-o', 'report.json'], cwd=pytester.path, env=environment, text=True, capture_output=True, check=False)
    assert report.returncode == 0, report.stdout + report.stderr
    data = json.loads((pytester.path / 'report.json').read_text())
    assert data['totals']['percent_covered'] == 100
    assert data['files']['app.py']['missing_branches'] == []


def test_pytest_cov_combines_nested_xdist_isolates(pytester: pytest.Pytester) -> None:
    """Aggregate coverage when xdist also distributes tests inside each isolate.

    Each of the two throng isolates starts two xdist workers. The controller
    must see all four contributions before enforcing the 100% threshold.
    """
    pytest.importorskip('xdist')
    pytester.makepyfile(app='''
        def classify(value):
            if value > 0:
                return 'positive'
            return 'other'
    ''', test_positive='''
        from app import classify
        def test_positive(): assert classify(1) == 'positive'
    ''', test_other='''
        from app import classify
        def test_other(): assert classify(0) == 'other'
    ''')
    environment = dict(os.environ)
    environment.pop('COVERAGE_PROCESS_START', None)
    environment.pop('COVERAGE_FILE', None)
    result = subprocess.run([
        sys.executable, '-m', 'pytest', '-q', '--isolates=2', '--throngtest-backend=temporary_directory',
        '-n', '2', '--cov=app', '--cov-branch', '--cov-fail-under=100',
    ], cwd=pytester.path, env=environment, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    report = subprocess.run([sys.executable, '-m', 'coverage', 'json', '-o', 'report.json'], cwd=pytester.path, env=environment, text=True, capture_output=True, check=False)
    assert report.returncode == 0, report.stdout + report.stderr
    data = json.loads((pytester.path / 'report.json').read_text())
    assert data['totals']['percent_covered'] == 100
    assert data['files']['app.py']['missing_branches'] == []
