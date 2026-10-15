import os
from pathlib import Path

import pytest

pytest_plugins = ['pytester']


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
