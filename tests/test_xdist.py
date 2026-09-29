import json
import shlex
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tests import test_integration


@pytest.mark.parametrize('mode', ['load', 'loadfile', 'loadscope', 'loadgroup', 'worksteal'])
@pytest.mark.parametrize('checked', [False, True])
def test_nested_workers_execute_only_their_shard(pytester: pytest.Pytester, backend: str, tmp_path: Path, mode: str, checked: bool) -> None:
    """Distribute each isolate's shard across its own xdist workers without repeated tests.

    Exclusive external records identify every test's PID, shard, and worker,
    proving that two isolates each use two workers. Tracing real backend calls
    checks that preparation and pytest each start once per isolate; temporary
    directory records also verify cleanup across schedulers and fingerprint modes.
    """
    pytester.makeconftest(f'''
        from pathlib import Path
        from uuid import uuid4
        from throng.extensions.local.isolate import LocalIsolate
        from throng.extensions.temporary_directory.isolate import TemporaryDirectoryIsolate
        def observe(cls):
            original = cls.run
            def traced(self, command, *args, **kwargs):
                (Path({str(tmp_path)!r}) / ('dispatch-' + uuid4().hex)).write_text(command)
                return original(self, command, *args, **kwargs)
            cls.run = traced
        observe(LocalIsolate)
        observe(TemporaryDirectoryIsolate)
    ''')
    source = f'''
        import json, os
        from pathlib import Path
        import pytest
        from throngtest.protocol import Request
        @pytest.mark.parametrize('index', range(4))
        def test_item(index, worker_id, request):
            assert worker_id != 'master'
            assert Path('prepared').read_text() == 'ready'
            shard = Request.unpack(request.config.workerinput['throngtest']).shard
            record = Path({str(tmp_path)!r}) / (Path(__file__).stem + '-' + str(index) + '.json')
            with record.open('x') as stream:
                json.dump({{'pid': os.getpid(), 'shard': shard, 'worker': worker_id, 'cwd': str(Path.cwd())}}, stream)
    '''
    pytester.makepyfile(**{f'test_{index}': source for index in range(4)})
    preparation = shlex.join([sys.executable, '-c', "from pathlib import Path; Path('prepared').write_text('ready')"])
    result = pytester.runpytest_subprocess(
        '--isolates=2', '-n', '2', f'--dist={mode}', '--throngtest-distribution=files', f'--throngtest-backend={backend}',
        '--throngtest-preparation=' + json.dumps([preparation]), *(['--throngtest-check-fingerprints'] if checked else []), timeout=60,
    )
    result.assert_outcomes(passed=16)
    records = [json.loads(record.read_text()) for record in tmp_path.glob('*.json')]
    assert len(records) == 16
    assert len({record['pid'] for record in records}) == 4
    assert {record['shard'] for record in records} == {0, 1}
    assert len(list(tmp_path.glob('dispatch-*'))) == 4  # One preparation and one pytest command per isolate.
    for shard in (0, 1):
        assert {record['worker'] for record in records if record['shard'] == shard} == {'gw0', 'gw1'}
    if backend == 'temporary_directory':
        directories = {record['cwd'] for record in records}
        assert len(directories) == 2
        assert all(not Path(directory).exists() for directory in directories)
        assert not (pytester.path / 'prepared').exists()


def test_plugin_works_without_xdist(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run throngtest without loading or importing xdist.

    Disabling plugin autoload and explicitly enabling throngtest exercises this
    path even though xdist is installed in the test environment.
    """
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    pytester.makepyfile('''
        import sys
        def test_no_xdist(request):
            assert not request.config.pluginmanager.hasplugin('xdist')
            assert 'xdist' not in sys.modules
    ''')
    pytester.runpytest_subprocess('-p', 'throngtest.plugin', '--isolates=2').assert_outcomes(passed=1)


@pytest.mark.parametrize('arguments', [(), ('-n', '0')])
def test_installed_xdist_stays_inactive(pytester: pytest.Pytester, arguments: tuple) -> None:
    """Keep xdist inactive inside isolates unless its worker count is explicitly enabled."""
    pytester.makepyfile('''
        def test_no_workers(worker_id):
            assert worker_id == 'master'
    ''')
    pytester.runpytest_subprocess('--isolates=1', *arguments).assert_outcomes(passed=1)


def test_zero_isolates_preserves_standalone_xdist(pytester: pytest.Pytester) -> None:
    """Let xdist operate independently when --isolates=0 disables throngtest execution."""
    pytester.makepyfile('''
        def test_xdist(worker_id, request):
            assert worker_id != 'master'
            assert 'throngtest' not in request.config.workerinput
            assert not request.config.pluginmanager.hasplugin('throngtest-runner')
    ''')
    pytester.runpytest_subprocess('--isolates=0', '-n', '2').assert_outcomes(passed=1)


@pytest.mark.parametrize('scenario', ['test_reports_and_junit', 'test_selection_and_conftest', 'test_collect_only_does_not_execute', 'test_empty_collection', 'test_collection_error'])
def test_nested_regressions(pytester: pytest.Pytester, backend: str, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    """Preserve existing pytest integration behavior with xdist enabled inside isolates.

    The test calls shared integration scenarios directly after injecting -n2
    through PYTEST_ADDOPTS, reusing their assertions for reports, selection,
    collect-only, empty projects, and collection failures.
    """
    monkeypatch.setenv('PYTEST_ADDOPTS', '-n2')
    function = getattr(test_integration, scenario)
    function(pytester, backend)


@pytest.mark.parametrize('checked', [False, True])
def test_nested_absolute_parameter_paths(pytester: pytest.Pytester, checked: bool) -> None:
    """Respect fingerprint opt-in when nested workers collect cwd-dependent parameter IDs.

    The parameter contains an absolute path that changes in the temporary
    isolate, while xdist workers within that isolate see the same path.
    """
    pytester.makepyfile('''
        from pathlib import Path
        import pytest
        @pytest.mark.parametrize('path', [str(Path.cwd() / 'missing')])
        def test_path(path): assert Path(path).parent == Path.cwd()
    ''')
    args = ['--throngtest-check-fingerprints'] if checked else []
    result = pytester.runpytest_subprocess('-n2', *args, timeout=45)
    if checked:
        assert result.ret == pytest.ExitCode.INTERNAL_ERROR
        assert 'Possible causes to check:' in result.stdout.str()
    else:
        result.assert_outcomes(passed=1)


@pytest.mark.parametrize('checked', [False, True])
def test_nested_duplicate_nodeids(pytester: pytest.Pytester, backend: str, checked: bool) -> None:
    """Preserve duplicate collected occurrences when xdist schedules an isolate's tests."""
    test = pytester.makepyfile('def test_ok(): pass')
    args = ['--throngtest-check-fingerprints'] if checked else []
    pytester.runpytest_subprocess('--isolates=1', '-n2', f'--throngtest-backend={backend}', *args, str(test), str(test), timeout=45).assert_outcomes(passed=2)


@pytest.mark.parametrize('source', ['cli', 'environment', 'ini'])
def test_user_configures_xdist_and_auto_is_resolved_inside_each_isolate(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """Honor native xdist option sources and resolve auto worker counts inside isolates.

    A custom auto-count hook asserts the isolate worker marker before returning
    two. This detects premature resolution in the outer controller, while
    workerinput verifies the count received by the resulting xdist processes.
    """
    pytester.makeconftest('''
        from throngtest.settings import WORKER
        def pytest_xdist_auto_num_workers(config):
            assert config.stash.get(WORKER, False), 'xdist must not start in the outer controller'
            return 2
    ''')
    pytester.makepyfile('''
        import pytest
        @pytest.mark.parametrize('index', range(8))
        def test_item(index, request, worker_id):
            assert worker_id != 'master'
            assert request.config.workerinput['workercount'] == 2
    ''')
    args = ['--numprocesses=auto']
    if source == 'environment':
        monkeypatch.setenv('PYTEST_ADDOPTS', '--numprocesses=auto')
        args = []
    elif source == 'ini':
        pytester.makeini('[pytest]\naddopts = --numprocesses=auto')
        args = []
    pytester.runpytest_subprocess('--isolates=2', *args, timeout=45).assert_outcomes(passed=8)


def test_xdist_loadgroup_marks_and_file_distribution(pytester: pytest.Pytester, backend: str, tmp_path: Path) -> None:
    """Keep marked tests on one xdist worker with file distribution and fingerprint checks.

    Multiple group marks and a name containing '@' exercise xdist's group ID
    handling. External PID records verify that every case runs on one process.
    """
    pytester.makepyfile(f'''
        import os
        from pathlib import Path
        import pytest
        @pytest.mark.xdist_group('first@group')
        @pytest.mark.xdist_group('second')
        @pytest.mark.parametrize('index', range(8))
        def test_item(index):
            (Path({str(tmp_path)!r}) / str(index)).write_text(str(os.getpid()))
    ''')
    result = pytester.runpytest_subprocess('--isolates=2', '-n2', '--dist=loadgroup', '--throngtest-check-fingerprints', f'--throngtest-backend={backend}', '--throngtest-distribution=files', timeout=45)
    result.assert_outcomes(passed=8)
    assert len({path.read_text() for path in tmp_path.iterdir()}) == 1


def test_nested_failfast(pytester: pytest.Pytester, backend: str) -> None:
    """Accept early xdist termination under -x without treating unfinished work as corruption.

    Either of two workers may report an in-flight failure before stopping, so
    the expected failure count allows both outcomes of that scheduling race.
    """
    pytester.makepyfile('''
        import pytest
        @pytest.mark.parametrize('index', range(20))
        def test_fail(index): assert False
    ''')
    result = pytester.runpytest_subprocess('--isolates=1', '-n2', '-x', f'--throngtest-backend={backend}', timeout=45)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert 1 <= result.parseoutcomes()['failed'] <= 2
    assert 'exactly once' not in result.stdout.str()


@pytest.mark.parametrize('restart', ['0', '1'])
def test_nested_worker_crash(pytester: pytest.Pytester, backend: str, restart: str) -> None:
    """Report an xdist worker crash and honor the configured restart policy.

    os._exit kills the process without normal pytest teardown. With a restart
    available, the remaining tests must run; both policies must produce one
    crash failure and one JUnit error in the outer controller.
    """
    pytester.makepyfile('''
        import os
        def test_crash(): os._exit(17)
        def test_ok(): pass
        def test_also_ok(): pass
    ''')
    result = pytester.runpytest_subprocess('--isolates=1', '-n1', '--max-worker-restart=' + restart, f'--throngtest-backend={backend}', '--junitxml=results.xml', timeout=45)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert result.parseoutcomes()['failed'] == 1
    assert 'crashed while running' in result.stdout.str()
    if restart == '1':
        result.assert_outcomes(passed=2, failed=1)
    assert len(ElementTree.parse(pytester.path / 'results.xml').findall('.//error')) == 1


@pytest.mark.parametrize('arguments', [('-n2', '--dist=each'), ('--dist=load', '--tx=popen'), ('-n2', '--px=id=proxy//popen'), ('--looponfail',)])
def test_unsupported_nested_modes_have_clear_errors(pytester: pytest.Pytester, arguments: tuple) -> None:
    """Reject repetition, remote-worker, and loop-on-failure modes in nested execution.

    Older xdist versions do not expose --px, so that case accepts pytest's own
    unknown-option diagnostic instead of requiring a throngtest validation error.
    """
    pytester.makepyfile('def test_never(): assert False')
    result = pytester.runpytest_subprocess('--isolates=2', *arguments)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    if any(argument.startswith('--px=') for argument in arguments) and not hasattr(pytester.parseconfig().option, 'px'):
        assert 'unrecognized arguments: --px' in result.stderr.str()
    else:
        assert 'throngtest:' in result.stderr.str()


def test_auto_can_disable_xdist_inside_isolate(pytester: pytest.Pytester) -> None:
    """Run tests without xdist workers when the isolate's auto-count hook returns zero."""
    pytester.makeconftest('def pytest_xdist_auto_num_workers(config): return 0')
    pytester.makepyfile('def test_ok(worker_id): assert worker_id == "master"')
    pytester.runpytest_subprocess('--isolates=1', '-n', 'auto', timeout=45).assert_outcomes(passed=1)


def test_nested_xdist_still_checks_its_own_collections(pytester: pytest.Pytester) -> None:
    """Retain xdist's collection consistency checks when throngtest fingerprints are disabled.

    Appending each native worker ID to collected node IDs creates a disagreement
    within one isolate, which xdist must detect before any test body runs.
    """
    pytester.makeconftest('''
        def pytest_collection_modifyitems(config, items):
            if hasattr(config, 'workerinput'):
                for item in items:
                    item._nodeid += config.workerinput['workerid']
    ''')
    pytester.makepyfile('def test_never(): assert False, "must not run"')
    result = pytester.runpytest_subprocess('--isolates=1', '-n2', timeout=45)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    output = result.stdout.str() + result.stderr.str()
    assert 'Different tests were collected between' in output
    assert 'gw0' in output
    assert 'gw1' in output


@pytest.mark.parametrize('count', [0, 1])
def test_nested_empty_shards(pytester: pytest.Pytester, backend: str, count: int) -> None:
    """Handle empty nested shards and preserve the exit code when every shard is empty.

    The controller collects four cases, but a worker-only hook retains zero or
    one. This creates empty assignments after isolates have already been selected.
    """
    pytester.makeconftest(f'''
        from throngtest.settings import WORKER
        def pytest_collection_modifyitems(config, items):
            if config.stash.get(WORKER, False):
                items[:] = items[:{count}]
    ''')
    pytester.makepyfile('''
        import pytest
        @pytest.mark.parametrize('index', range(4))
        def test_item(index): pass
    ''')
    result = pytester.runpytest_subprocess('--isolates=2', '-n2', f'--throngtest-backend={backend}', timeout=45)
    result.assert_outcomes(passed=count)
    assert result.ret == (pytest.ExitCode.OK if count else pytest.ExitCode.NO_TESTS_COLLECTED)


def test_nested_warnings(pytester: pytest.Pytester, backend: str) -> None:
    """Forward an xdist worker warning exactly once through the isolate controller."""
    pytester.makepyfile('''
        import warnings
        def test_warning(): warnings.warn('nested warning', UserWarning)
    ''')
    result = pytester.runpytest_subprocess('--isolates=1', '-n2', f'--throngtest-backend={backend}', timeout=45)
    result.assert_outcomes(passed=1, warnings=1)
    assert 'nested warning' in result.stdout.str()


@pytest.mark.parametrize('stage', ['setup', 'call', 'teardown'])
def test_grouped_worker_crash_without_restart(pytester: pytest.Pytester, stage: str) -> None:
    """Report a grouped test crashing during setup, call, or teardown without restarting xdist.

    The generated fixture or test exits abruptly at the selected phase. With
    restarts disabled, the controller must already know the grouped test's
    identity, without relying on worker shutdown output or a replacement process.
    """
    pytester.makepyfile(f'''
        import os
        import pytest
        @pytest.fixture
        def resource():
            if {stage!r} == 'setup': os._exit(17)
            yield
            if {stage!r} == 'teardown': os._exit(17)
        @pytest.mark.xdist_group('group')
        def test_crash(resource):
            if {stage!r} == 'call': os._exit(17)
    ''')
    result = pytester.runpytest_subprocess('--isolates=1', '-n1', '--dist=loadgroup', '--max-worker-restart=0', '--throngtest-check-fingerprints', timeout=45)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert result.parseoutcomes()['failed'] == 1
    assert 'crashed while running' in result.stdout.str()


def test_preparation_failure_prevents_xdist_start(pytester: pytest.Pytester, tmp_path: Path) -> None:
    """Fail preparation before starting any nested xdist workers.

    A pytest_configure_node hook would create an external marker on worker
    startup, so its absence checks the startup boundary as well as the exit code.
    """
    pytester.makeconftest(f'''
        from pathlib import Path
        def pytest_configure_node(node):
            Path({str(tmp_path / 'started')!r}).touch()
    ''')
    pytester.makepyfile('def test_never(): assert False')
    commands = [shlex.join([sys.executable, '-c', 'raise SystemExit(7)'])]
    result = pytester.runpytest_subprocess('--isolates=2', '-n2', '--throngtest-preparation=' + json.dumps(commands), timeout=45)
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    assert 'preparation command 1 failed with exit code 7' in result.stdout.str()
    assert not (tmp_path / 'started').exists()


def test_nested_run_from_subdirectory_with_absolute_nodeid(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve an absolute node selector when nested execution starts from a subdirectory."""
    pytester.makeini('[pytest]')
    child = pytester.path / 'child'
    child.mkdir()
    test = child / 'test_example.py'
    test.write_text('def test_ok(): pass\ndef test_bad(): assert False\n')
    monkeypatch.chdir(child)
    pytester.runpytest_subprocess('--isolates=2', '-n2', str(test) + '::test_ok', timeout=45).assert_outcomes(passed=1)


@pytest.mark.parametrize('order', ['tryfirst=True', 'trylast=True', ''])
@pytest.mark.parametrize('checked', [False, True])
def test_loadgroup_preserves_other_plugins_nodeid_changes(pytester: pytest.Pytester, checked: bool, order: str) -> None:
    """Preserve third-party node ID changes around xdist's loadgroup suffix handling.

    The custom collection hook runs with first, last, or default priority.
    JUnit must retain its '[custom]' suffix while omitting xdist's group suffix,
    including when fingerprints compare the controller and worker collections.
    """
    pytester.makeconftest(f'''
        import pytest
        @pytest.hookimpl({order})
        def pytest_collection_modifyitems(items):
            for item in items:
                item._nodeid += '[custom]'
    ''')
    pytester.makepyfile('''
        import pytest
        @pytest.mark.xdist_group('group')
        def test_ok(): pass
    ''')
    args = ['--throngtest-check-fingerprints'] if checked else []
    result = pytester.runpytest_subprocess('--isolates=1', '-n2', '--dist=loadgroup', '--junitxml=results.xml', *args, timeout=45)
    result.assert_outcomes(passed=1)
    case = ElementTree.parse(pytester.path / 'results.xml').find('.//testcase')
    assert case is not None
    assert case.attrib['name'] == 'test_ok[custom]'
