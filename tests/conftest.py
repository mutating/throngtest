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
    if os.environ.get('COVERAGE_PROCESS_START'):
        monkeypatch.setenv('COVERAGE_FILE', str(Path(__file__).resolve().parents[1] / '.coverage'))


@pytest.fixture(params=['local', 'temporary_directory'])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)
