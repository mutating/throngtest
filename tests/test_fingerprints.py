from pathlib import Path
from typing import Tuple
from xml.etree import ElementTree

import pytest


@pytest.mark.parametrize('configuration', [
    ('default', False), ('cli', True), ('cli', False),
    ('environment', True), ('environment', False), ('toml', True), ('toml', False),
])
def test_changed_identifiers_with_each_setting_source(pytester: pytest.Pytester, backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: Tuple[str, bool]) -> None:
    """Reject changed IDs only when fingerprint checking is enabled by a settings source.

    A collection hook appends a suffix only inside isolates. External marker
    files verify execution counts, while JUnit checks that modified test IDs
    survive reporting when fingerprint checking is disabled.
    """
    source, checked = configuration
    pytester.makeconftest('''
        from throngtest.settings import WORKER
        def pytest_collection_modifyitems(config, items):
            if config.stash.get(WORKER, False):
                for item in items:
                    item._nodeid += '[isolate]'
    ''')
    pytester.makepyfile(f'''
        from pathlib import Path
        import pytest
        @pytest.mark.parametrize('index', range(4))
        def test_item(index):
            marker = Path({str(tmp_path)!r}) / str(index)
            assert not marker.exists()
            marker.touch()
    ''')
    arguments = ['--isolates=2', f'--throngtest-backend={backend}', '--junitxml=results.xml']
    if source == 'cli':
        arguments.append('--throngtest-check-fingerprints' if checked else '--throngtest-no-check-fingerprints')
    elif source == 'environment':
        monkeypatch.setenv('THRONGTEST_CHECK_FINGERPRINTS', str(checked).lower())
    elif source == 'toml':
        pytester.makepyprojecttoml('[tool.throngtest]\ncheck_fingerprints = ' + str(checked).lower())
    result = pytester.runpytest_subprocess(*arguments, timeout=30)
    output = result.stdout.str() + result.stderr.str()
    if checked:
        assert result.ret == pytest.ExitCode.INTERNAL_ERROR
        assert 'Possible causes to check:' in output
        assert 'controller: 4 selected tests\nisolate: 4 selected tests' in output
        assert '[isolate]' in output
        assert list(tmp_path.iterdir()) == []
    else:
        result.assert_outcomes(passed=4)
        assert 'collection differs' not in output
        assert len(list(tmp_path.iterdir())) == 4
        cases = ElementTree.parse(pytester.path / 'results.xml').findall('.//testcase')
        assert len(cases) == 4
        assert all(case.attrib['name'].endswith('[isolate]') for case in cases)


@pytest.mark.parametrize('checked', [False, True])
def test_fingerprint_flag_before_an_absolute_test_path(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, checked: bool) -> None:
    """Parse a fingerprint flag separately from the following absolute test path.

    Pytest is invoked from a child directory to exercise path relocation. A
    marker file keeps that directory present in the temporary project copy.
    """
    pytester.makeini('[pytest]')
    test = pytester.makepyfile('def test_ok(): pass')
    child = pytester.path / 'child'
    child.mkdir()
    (child / 'keep.txt').write_text('Keep the invocation directory in the isolate snapshot.')
    monkeypatch.chdir(child)
    flag = '--throngtest-check-fingerprints' if checked else '--throngtest-no-check-fingerprints'
    pytester.runpytest_subprocess(flag, str(test), timeout=30).assert_outcomes(passed=1)


def test_default_skips_hashing_and_accepts_absolute_parameter_paths(pytester: pytest.Pytester) -> None:
    """Skip fingerprint computation by default even when isolation changes parameter IDs.

    Both hashing entry points are replaced with functions that raise, so a
    passing run proves hashing was skipped. A cwd-derived parameter changes
    its ID naturally when the project is copied into an isolate.
    """
    pytester.makeconftest('''
        import throngtest.runner
        import throngtest.worker
        def unexpected_hash(*args):
            raise AssertionError('fingerprints must not be computed when disabled')
        throngtest.runner.fingerprint = unexpected_hash
        throngtest.worker.fingerprint = unexpected_hash
    ''')
    pytester.makepyfile('''
        from pathlib import Path
        import pytest
        @pytest.mark.parametrize('path', [str(Path.cwd() / 'missing-parent' / 'missing-command')])
        def test_path(path):
            assert Path(path).parent.parent == Path.cwd()
    ''')
    pytester.runpytest_subprocess(timeout=30).assert_outcomes(passed=1)


@pytest.mark.parametrize('distribution', ['tests', 'files'])
@pytest.mark.parametrize(('controller_count', 'worker_count'), [(1, 4), (4, 1), (4, 0)])
def test_changed_collection_sizes_without_fingerprints(pytester: pytest.Pytester, backend: str, distribution: str, controller_count: int, worker_count: int) -> None:
    """Run the isolate's collection when unchecked collection sizes differ.

    A hook independently truncates controller and worker collections to exercise
    growth, shrinkage, and an empty worker collection under both distributions.
    """
    pytester.makeconftest(f'''
        from throngtest.settings import WORKER
        def pytest_collection_modifyitems(config, items):
            count = {worker_count} if config.stash.get(WORKER, False) else {controller_count}
            items[:] = items[:count]
    ''')
    pytester.makepyfile(**{f'test_{index}': 'def test_ok(): pass' for index in range(4)})
    result = pytester.runpytest_subprocess(f'--throngtest-backend={backend}', f'--throngtest-distribution={distribution}', timeout=30)
    result.assert_outcomes(passed=worker_count)
    assert result.ret == (pytest.ExitCode.OK if worker_count else pytest.ExitCode.NO_TESTS_COLLECTED)


@pytest.mark.parametrize('checked', [False, True])
def test_duplicate_identifiers_remain_valid_in_both_modes(pytester: pytest.Pytester, backend: str, checked: bool) -> None:
    """Preserve duplicate test occurrences with fingerprint checking enabled or disabled."""
    test = pytester.makepyfile('def test_ok(): pass')
    flag = '--throngtest-check-fingerprints' if checked else '--throngtest-no-check-fingerprints'
    pytester.runpytest_subprocess(f'--throngtest-backend={backend}', flag, '--keep-duplicates', str(test), str(test), timeout=30).assert_outcomes(passed=2)
