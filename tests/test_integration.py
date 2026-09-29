import json
import os
from pathlib import Path
from xml.etree import ElementTree

import pytest


def run(pytester: pytest.Pytester, backend: str, *arguments: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess('--isolates=2', f'--throngtest-backend={backend}', *arguments, timeout=30)


@pytest.mark.parametrize(('arguments', 'workers'), [((), 4), (('--isolates=2',), 2), (('--isolates', '2'), 2), (('--throngtest-check-fingerprints',), 4)])
def test_real_isolates_execute_every_test_once(pytester: pytest.Pytester, backend: str, tmp_path: Path, arguments: tuple, workers: int) -> None:
    """Execute every test once through the selected throng backend and isolate count.

    Wrappers observe the real backend run methods, while external files record
    test execution, PIDs, and working directories. Together they distinguish
    actual isolate dispatch from ordinary pytest execution and expose whether
    temporary copies are removed or the local backend shares the project.
    """
    pytester.makeconftest(f'''
        from pathlib import Path
        from uuid import uuid4
        from throng.extensions.local.isolate import LocalIsolate
        from throng.extensions.temporary_directory.isolate import TemporaryDirectoryIsolate
        def observe(cls):
            original = cls.run
            def traced(self, *args, **kwargs):
                (Path({str(tmp_path)!r}) / ('dispatch-' + uuid4().hex)).write_text(cls.__name__)
                return original(self, *args, **kwargs)
            cls.run = traced
        observe(LocalIsolate)
        observe(TemporaryDirectoryIsolate)
    ''')
    pytester.makepyfile(f'''
        import json, os
        from pathlib import Path
        import pytest
        @pytest.mark.parametrize('index', range(8))
        def test_item(index):
            assert os.getpid() != {os.getpid()}
            target = Path({str(tmp_path)!r}) / str(index)
            assert not target.exists()
            target.write_text(json.dumps({{'pid': os.getpid(), 'cwd': str(Path.cwd())}}))
            Path('isolate-marker').write_text('written')
    ''')
    result = pytester.runpytest_subprocess(f'--throngtest-backend={backend}', *arguments, timeout=30)
    result.assert_outcomes(passed=8)
    records = [json.loads((tmp_path / str(index)).read_text()) for index in range(8)]
    dispatched = [path.read_text() for path in tmp_path.glob('dispatch-*')]
    expected_class = 'LocalIsolate' if backend == 'local' else 'TemporaryDirectoryIsolate'
    assert dispatched == [expected_class] * workers
    assert len({record['pid'] for record in records}) == workers
    directories = {record['cwd'] for record in records}
    if backend == 'local':
        assert directories == {str(pytester.path.resolve())}
        assert (pytester.path / 'isolate-marker').exists()
    else:
        assert len(directories) == workers
        assert all(not Path(directory).exists() for directory in directories)
        assert not (pytester.path / 'isolate-marker').exists()


def test_temporary_isolates_overlap(pytester: pytest.Pytester, tmp_path: Path) -> None:
    """Run temporary-directory isolates concurrently.

    Each process writes to a shared external barrier and waits for both PIDs.
    Sequential execution cannot pass this rendezvous; the deadline bounds a
    stalled run without using total execution time as the concurrency assertion.
    """
    pytester.makepyfile(f'''
        import os, time
        from pathlib import Path
        import pytest
        @pytest.mark.parametrize('index', range(2))
        def test_overlap(index):
            barrier = Path({str(tmp_path)!r})
            (barrier / str(os.getpid())).touch()
            deadline = time.monotonic() + 10
            while len(list(barrier.iterdir())) != 2:
                assert time.monotonic() < deadline, 'isolates did not run concurrently'
                time.sleep(0.01)
    ''')
    run(pytester, 'temporary_directory').assert_outcomes(passed=2)


def test_files_stay_together_and_fixtures_work(pytester: pytest.Pytester, backend: str, tmp_path: Path) -> None:
    """Keep each file in one isolate and run its module fixture setup and teardown once.

    Module fixtures write uniquely named external markers and their PIDs.
    Repeated setup fails immediately, while teardown markers and distinct PIDs
    confirm fixture completion and distribution across processes.
    """
    source = f'''
        import os
        from pathlib import Path
        import pytest
        @pytest.fixture(scope='module')
        def resource():
            path = Path({str(tmp_path)!r}) / (Path(__file__).stem + '.fixture')
            assert not path.exists()
            path.write_text(str(os.getpid()))
            yield 42
            path.with_suffix('.done').touch()
        @pytest.mark.parametrize('index', range(4))
        def test_item(resource, index):
            assert resource == 42
    '''
    pytester.makepyfile(test_a=source, test_b=source)
    run(pytester, backend, '--throngtest-distribution=files').assert_outcomes(passed=8)
    assert (tmp_path / 'test_a.fixture').read_text() != (tmp_path / 'test_b.fixture').read_text()
    assert len(list(tmp_path.glob('*.done'))) == 2


def test_reports_and_junit(pytester: pytest.Pytester, backend: str) -> None:
    """Preserve pytest outcomes, failure details, captured output, and JUnit properties.

    A generated suite mixes passes, failures, skips, xfail/xpass, and fixture
    errors in setup and teardown. JUnit uses xunit1 to retain record_property
    metadata, and its case count checks that replay does not duplicate tests.
    """
    pytester.makepyfile('''
        import pytest
        def test_pass(record_property):
            record_property('custom', 'value')
        def test_fail():
            print('captured output')
            assert 1 == 2
        @pytest.mark.skip(reason='skip reason')
        def test_skip(): pass
        @pytest.mark.xfail(reason='expected failure')
        def test_xfail(): assert False
        @pytest.mark.xfail(reason='unexpected success')
        def test_xpass(): pass
        @pytest.fixture
        def broken(): raise RuntimeError('fixture failed')
        def test_error(broken): pass
        @pytest.fixture
        def bad_teardown():
            yield
            raise RuntimeError('teardown failed')
        def test_teardown(bad_teardown): pass
    ''')
    result = run(pytester, backend, '--junitxml=results.xml', '-o', 'junit_family=xunit1', '-ra')
    result.assert_outcomes(passed=2, failed=1, skipped=1, xfailed=1, xpassed=1, errors=2)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines_random(['*captured output*', '*fixture failed*', '*teardown failed*', '*assert 1 == 2*'])
    assert 'get_terminal_writer' not in result.stdout.str()
    xml = ElementTree.parse(pytester.path / 'results.xml')
    assert len(xml.findall('.//testcase')) == 7
    assert xml.find('.//property[@name="custom"]') is not None


def test_selection_and_conftest(pytester: pytest.Pytester, backend: str) -> None:
    """Apply keyword and marker selection while making conftest fixtures available in isolates."""
    pytester.makeconftest('''
        import pytest
        @pytest.fixture
        def resource(): return 9
    ''')
    pytester.makepyfile('''
        import pytest
        @pytest.mark.chosen
        @pytest.mark.parametrize('value', [1, 2, 3])
        def test_selected(resource, value): assert resource == 9
        def test_ignored(): assert False
    ''')
    run(pytester, backend, '-k', 'not 2', '-m', 'chosen').assert_outcomes(passed=2, deselected=2)


def test_disabled_is_normal_pytest(pytester: pytest.Pytester) -> None:
    """Leave ordinary pytest execution active when --isolates=0 disables the runner."""
    pytester.makeconftest('''
        def pytest_sessionstart(session):
            assert not session.config.pluginmanager.hasplugin('throngtest-runner')
    ''')
    pytester.makepyfile('def test_ok(): pass')
    pytester.runpytest_subprocess('--isolates=0').assert_outcomes(passed=1)


def test_collect_only_does_not_execute(pytester: pytest.Pytester, backend: str) -> None:
    """Collect tests without executing their bodies under --collect-only."""
    pytester.makepyfile('def test_never(): raise AssertionError("executed")')
    result = run(pytester, backend, '--collect-only')
    assert result.ret == 0
    result.stdout.fnmatch_lines(['*1 test collected*'])


def test_empty_collection(pytester: pytest.Pytester, backend: str) -> None:
    """Return pytest's no-tests exit code for an empty project."""
    assert run(pytester, backend).ret == pytest.ExitCode.NO_TESTS_COLLECTED


def test_collection_error(pytester: pytest.Pytester, backend: str) -> None:
    """Interrupt the run and display the original error when collection fails."""
    pytester.makepyfile('raise RuntimeError("cannot collect")')
    result = run(pytester, backend)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(['*cannot collect*'])


def test_failfast(pytester: pytest.Pytester, backend: str) -> None:
    """Stop after early failures when pytest's -x option is enabled.

    Two isolates may already be executing when the first failure arrives, so
    the allowed failure count includes one in-flight test from each isolate.
    """
    pytester.makepyfile('''
        import pytest
        @pytest.mark.parametrize('index', range(10))
        def test_fail(index): assert False
    ''')
    result = run(pytester, backend, '-x')
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert 1 <= result.parseoutcomes()['failed'] <= 2


def test_crashed_worker_is_not_success(pytester: pytest.Pytester, backend: str) -> None:
    """Treat an isolate process exiting without a protocol response as an internal error.

    os._exit bypasses pytest hooks and normal interpreter shutdown, reproducing
    a missing worker result rather than a normally reported test failure.
    """
    pytester.makepyfile('import os\ndef test_crash(): os._exit(17)')
    result = run(pytester, backend)
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    assert 'worker terminated without a result' in result.stdout.str() + result.stderr.str()


def test_collection_mismatch(pytester: pytest.Pytester, backend: str, tmp_path: Path) -> None:
    """Reject changed collections before execution and show actionable mismatch diagnostics.

    The controller's import creates an external marker that makes later worker
    imports generate an extra parameter. This produces a deterministic mismatch
    on both backends, with no dependence on random values or process timing.
    """
    marker = tmp_path / 'collected'
    pytester.makepyfile(f'''
        from pathlib import Path
        import pytest
        marker = Path({str(marker)!r})
        values = [1, 2] if marker.exists() else [1]
        marker.touch()
        @pytest.mark.parametrize('value', values)
        def test_item(value): raise AssertionError('test must not execute')
    ''')
    result = run(pytester, backend, '--throngtest-check-fingerprints')
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    output = result.stdout.str() + result.stderr.str()
    assert 'collection differs' in output
    assert 'controller: 1 selected tests' in output
    assert 'isolate: 2 selected tests' in output
    assert output.index('Possible causes to check:') < output.index('controller: 1 selected tests')
    assert 'preparation commands may generate or change files' in output
    assert '+test_collection_mismatch.py::test_item[2]' in output
    assert 'THRONGTEST_' not in output


@pytest.mark.parametrize('backslash_separators', [False, True])
def test_collection_diagnostic_for_absolute_parameter_path(pytester: pytest.Pytester, backslash_separators: bool) -> None:
    """Explain fingerprint mismatches caused solely by relocated absolute parameter paths.

    A parameter derives its ID from the current directory, so a temporary copy
    changes the identifier without changing the collection size. The diff must
    expose both paths while omitting the encoded worker response. Pytest doubles
    Windows backslashes in parameter IDs, so expectations use that same spelling.
    Rewriting separators also exercises this behavior on non-Windows systems.
    """
    path_expression = "str(Path.cwd() / 'missing-command')"
    if backslash_separators:
        path_expression += ".replace('/', chr(92))"
    pytester.makepyfile(f'''
        from pathlib import Path
        import pytest
        @pytest.mark.parametrize('value', [{path_expression}])
        def test_item(value): raise AssertionError('test must not execute')
    ''')
    result = run(pytester, 'temporary_directory', '--throngtest-check-fingerprints')
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    output = result.stdout.str() + result.stderr.str()
    assert 'controller: 1 selected tests\nisolate: 1 selected tests' in output
    assert output.index('Absolute paths in parameter IDs change') < output.index('controller: 1 selected tests')
    assert 'even if the tests are otherwise equivalent' in output
    expected = str(pytester.path.resolve() / 'missing-command')
    if backslash_separators:
        expected = expected.replace('/', '\\')
    expected = expected.replace('\\', '\\\\')
    assert f'-test_collection_diagnostic_for_absolute_parameter_path.py::test_item[{expected}]' in output
    assert '+test_collection_diagnostic_for_absolute_parameter_path.py::test_item[' in output
    assert f'+test_collection_diagnostic_for_absolute_parameter_path.py::test_item[{expected}]' not in output
    assert 'THRONGTEST_' not in output


def test_warning_and_uncaptured_output(pytester: pytest.Pytester, backend: str) -> None:
    """Forward worker stdout, stderr, and a single warning when pytest capture is disabled."""
    pytester.makepyfile('''
        import sys, warnings
        def test_output():
            print('raw stdout marker')
            print('raw stderr marker', file=sys.stderr)
            warnings.warn('runtime warning marker', UserWarning)
    ''')
    result = run(pytester, backend, '-s')
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines_random(['*raw stdout marker*', '*raw stderr marker*', '*runtime warning marker*'])


def test_ini_and_environment_addopts(pytester: pytest.Pytester, backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Combine selection options from pytest.ini and PYTEST_ADDOPTS in isolate runs."""
    pytester.makeini('[pytest]\naddopts = -m chosen\nmarkers = chosen: selected test')
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k good')
    pytester.makepyfile('''
        import pytest
        @pytest.mark.chosen
        def test_good(): pass
        @pytest.mark.chosen
        def test_bad(): assert False
        def test_good_unmarked(): assert False
    ''')
    run(pytester, backend).assert_outcomes(passed=1, deselected=2)


def test_enable_using_skelet_sources(pytester: pytest.Pytester, backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure isolate execution through TOML and environment values without CLI options."""
    pytester.makepyprojecttoml(f'[tool.throngtest]\nworkers = 1\nbackend = "{backend}"\ndistribution = "files"')
    monkeypatch.setenv('THRONGTEST_WORKERS', '2')
    pytester.makepyfile(test_a='def test_a(): pass', test_b='def test_b(): pass')
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines([f'*throngtest: 2 isolates, backend={backend}, distribution=files*'])


@pytest.mark.parametrize('inside', [True, False])
def test_invocation_directory_and_absolute_nodeid(pytester: pytest.Pytester, backend: str, monkeypatch: pytest.MonkeyPatch, inside: bool) -> None:
    """Select an absolute node ID when invoked inside or outside the project root.

    The test path includes a space, and an unselected failing test catches
    accidental loss of the node selector during path relocation.
    """
    pytester.makeini('[pytest]')
    child = pytester.path / 'sub directory'
    child.mkdir()
    test = child / 'test_example.py'
    test.write_text('def test_ok(): pass\ndef test_bad(): assert False\n')
    monkeypatch.chdir(child if inside else pytester.path.parent)
    # pytester.runpytest_subprocess executes from the current working directory.
    result = run(pytester, backend, str(test) + '::test_ok')
    result.assert_outcomes(passed=1)


def test_missing_interpreter(pytester: pytest.Pytester, backend: str) -> None:
    """Report a missing configured Python executable as a worker startup failure."""
    pytester.makepyfile('def test_ok(): pass')
    result = run(pytester, backend, '--throngtest-python=missing-throngtest-python')
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    assert 'worker terminated without a result' in result.stdout.str() + result.stderr.str()


def test_unknown_backend(pytester: pytest.Pytester) -> None:
    """Reject unknown backends with a usage error listing the built-in alternatives."""
    pytester.makepyfile('def test_ok(): pass')
    result = run(pytester, 'unknown')
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(['*unknown backend*local*temporary_directory*'])


@pytest.mark.parametrize('option', ['--pdb', '--lf', '--ff', '--nf', '--sw'])
def test_incompatible_execution_options(pytester: pytest.Pytester, option: str) -> None:
    """Reject debugging and cache-dependent execution options incompatible with isolates."""
    pytester.makepyfile('def test_ok(): pass')
    result = run(pytester, 'local', option)
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_duplicate_nodeids(pytester: pytest.Pytester, backend: str) -> None:
    """Execute both requested occurrences when the same test file is collected twice."""
    test = pytester.makepyfile('def test_ok(): pass')
    run(pytester, backend, '--keep-duplicates', str(test), str(test)).assert_outcomes(passed=2)


def test_excluded_file_is_not_copied(pytester: pytest.Pytester) -> None:
    """Omit explicitly excluded project files from temporary isolate copies."""
    (pytester.path / 'private.txt').write_text('not for the snapshot')
    pytester.makepyfile('from pathlib import Path\ndef test_excluded(): assert not Path("private.txt").exists()')
    run(pytester, 'temporary_directory', '--throngtest-exclude=["private.txt"]').assert_outcomes(passed=1)


def test_strict_xpass_is_failure(pytester: pytest.Pytester, backend: str) -> None:
    """Count a strict unexpected pass as a failure and preserve pytest's failure exit code."""
    pytester.makepyfile('import pytest\n@pytest.mark.xfail(strict=True)\ndef test_ok(): pass')
    result = run(pytester, backend)
    result.assert_outcomes(failed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_without_terminal_reporter(pytester: pytest.Pytester, backend: str) -> None:
    """Complete isolate execution successfully with pytest's terminal reporter disabled."""
    pytester.makepyfile('def test_ok(): print("output without terminal")')
    assert run(pytester, backend, '-p', 'no:terminal', '-s', '--assert=plain').ret == pytest.ExitCode.OK


def test_failfast_cancels_running_isolate_and_removes_copies(pytester: pytest.Pytester, tmp_path: Path) -> None:
    """Cancel an active sibling isolate on failfast and remove both temporary copies.

    The failing test waits for the slow test's external start marker before
    failing. A marker after the slow sleep detects missed cancellation, while
    externally recorded paths let the controller check cleanup after shutdown.
    """
    pytester.makepyfile(f'''
        import time
        from pathlib import Path
        import pytest
        observer = Path({str(tmp_path)!r})
        def test_fail():
            (observer / 'fast-directory').write_text(str(Path.cwd()))
            deadline = time.monotonic() + 10
            while not (observer / 'started').exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            pytest.fail('cancel the other isolate')
        def test_slow():
            (observer / 'slow-directory').write_text(str(Path.cwd()))
            (observer / 'started').touch()
            time.sleep(10)
            (observer / 'not-cancelled').touch()
    ''')
    result = run(pytester, 'temporary_directory', '-x')
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert not (tmp_path / 'not-cancelled').exists()
    assert not Path((tmp_path / 'fast-directory').read_text()).exists()
    assert not Path((tmp_path / 'slow-directory').read_text()).exists()


def test_keyboard_interrupt_in_worker(pytester: pytest.Pytester, backend: str) -> None:
    """Propagate a worker KeyboardInterrupt as pytest's interrupted exit code."""
    pytester.makepyfile('def test_interrupt(): raise KeyboardInterrupt')
    assert run(pytester, backend).ret == pytest.ExitCode.INTERRUPTED


def test_continue_after_collection_errors(pytester: pytest.Pytester, backend: str) -> None:
    """Run collectable tests while retaining collection errors when continuation is requested."""
    pytester.makepyfile(test_broken='raise RuntimeError("broken collection")', test_good='def test_ok(): pass')
    result = run(pytester, backend, '--continue-on-collection-errors')
    result.assert_outcomes(passed=1, errors=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
