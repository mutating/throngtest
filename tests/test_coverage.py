import subprocess
import sys
from pathlib import Path

import pytest
from coverage import CoverageData


@pytest.mark.parametrize('branch', [False, True])
@pytest.mark.parametrize('installed_path', [
    '/Library/Frameworks/Python.framework/Versions/3.10/lib/python3.10/site-packages/throngtest/distribution.py',
    '/opt/hostedtoolcache/Python/3.10.11/x64/lib/python3.10/site-packages/throngtest/distribution.py',
    r'C:\hostedtoolcache\windows\Python\3.10.11\x64\Lib\site-packages\throngtest\distribution.py',
])
def test_coverage_combines_checkout_and_installed_package(tmp_path: Path, branch: bool, installed_path: str) -> None:
    """Merge checkout and installed-package coverage into one file without losing hits.

    Synthetic data files give each package location different line or branch
    hits, reproducing the split between unit tests and isolate subprocesses.
    Real coverage combination uses the repository configuration and installed
    paths from all three CI platforms, without requiring those paths to exist.
    Combination runs in a child process because creating a Coverage instance
    here would disable automatic coverage saving for the current pytest worker.
    """
    root = Path(__file__).resolve().parents[1]
    source = str(root / 'throngtest' / 'distribution.py')
    basename = str(tmp_path / '.coverage')
    for index, path in enumerate((source, installed_path)):
        data = CoverageData(basename=basename, suffix=str(index))
        if branch:
            data.add_arcs({path: [(10 + index, 11 + index)]})
        else:
            data.add_lines({path: [10 + index, 11 + index]})
        data.write()
    result = subprocess.run(
        [sys.executable, '-m', 'coverage', 'combine', '--rcfile=' + str(root / 'pyproject.toml'), '--data-file=' + basename],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = CoverageData(basename=basename)
    combined.read()
    assert combined.measured_files() == {source}
    assert set(combined.lines(source) or []) == {10, 11, 12}
    if branch:
        assert set(combined.arcs(source) or []) == {(10, 11), (11, 12)}
