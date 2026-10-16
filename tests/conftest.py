import os
from pathlib import Path

import pytest

pytest_plugins = ['pytester']


@pytest.fixture(scope='session')
def bootstrap_plugin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp('bootstrap-plugin')
    # Keep the helper importable without exposing the checkout on PYTHONPATH:
    # temporary isolates should still import the installed throngtest package.
    (directory / '_throngtest_bootstrap.py').write_text(Path(__file__).with_name('bootstrap.py').read_text())
    return directory


@pytest.fixture(autouse=True)
def stub_pip(monkeypatch: pytest.MonkeyPatch, bootstrap_plugin: Path) -> str:
    """Stub repeated bootstrap commands; return plugins for real-pip checks."""
    plugins = os.environ.get('PYTEST_PLUGINS', '')
    monkeypatch.setenv('PYTEST_PLUGINS', ','.join(filter(None, (plugins, '_throngtest_bootstrap'))))
    monkeypatch.setenv('PYTHONPATH', str(bootstrap_plugin) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    monkeypatch.syspath_prepend(bootstrap_plugin)
    return plugins


@pytest.fixture
def real_pip(monkeypatch: pytest.MonkeyPatch, stub_pip: str) -> None:
    monkeypatch.setenv('PYTEST_PLUGINS', stub_pip)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith('THRONGTEST_'):
            monkeypatch.delenv(name)
    monkeypatch.delenv('PYTEST_ADDOPTS', raising=False)
    # Bootstrap packages are already installed in the shared test interpreter.
    # Keep repeated isolate installations independent of the package index.
    monkeypatch.setenv('PIP_NO_INDEX', '1')
    monkeypatch.setenv('PIP_DISABLE_PIP_VERSION_CHECK', '1')
    if os.environ.get('COVERAGE_PROCESS_START'):
        monkeypatch.setenv('COVERAGE_FILE', str(Path(__file__).resolve().parents[1] / '.coverage'))


@pytest.fixture(params=['local', 'temporary_directory'])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)
