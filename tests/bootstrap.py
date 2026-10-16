"""Avoid redundant pip processes in subprocess-based integration scenarios."""

from typing import Callable

import pytest
from cantok import AbstractToken, DefaultToken
from throng import AbstractIsolate
from throng.abstracts.results import RunResultProtocol, SimpleRunResult
from throng.extensions.local.isolate import LocalIsolate
from throng.extensions.temporary_directory.isolate import TemporaryDirectoryIsolate


def installed_runtime(original: Callable[..., RunResultProtocol]) -> Callable[..., RunResultProtocol]:
    def run(isolate: AbstractIsolate, command: str, token: AbstractToken = DefaultToken()) -> RunResultProtocol:  # noqa: B008
        if command in ('pip install pytest', 'pip install throngtest'):
            return SimpleRunResult(True, 0)
        return original(isolate, command, token=token)

    return run


def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    # Patch before generated conftests add their own tracing and failure hooks.
    patch = pytest.MonkeyPatch()
    early_config.add_cleanup(patch.undo)
    for isolate in (LocalIsolate, TemporaryDirectoryIsolate):
        patch.setattr(isolate, '_run', installed_runtime(isolate._run))
