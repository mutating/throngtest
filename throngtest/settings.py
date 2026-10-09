# mypy: disallow_any_expr=False
"""All user configuration is resolved and validated by skelet."""

# skelet.Storage currently triggers an unlocated Any-expression error in mypy
# when subclassed. Keep all other strict checks enabled.

import sys
from typing import List, cast

import pytest
from skelet import EnvSource, Field, FixedCLISource, MemorySource, Storage, TOMLSource

WORKER = pytest.StashKey[bool]()
ARGUMENTS = pytest.StashKey[List[str]]()


class PytestSource(FixedCLISource[object]):
    """Use pytest's argument discovery with skelet's typed CLI conversion."""

    def __init__(self, config: pytest.Config) -> None:
        super().__init__(named_arguments=['workers', 'backend', 'distribution', 'python', 'exclude', 'preparation', 'packages'])
        self.config = config

    def __getitem__(self, key: str) -> str:
        value = cast(str, self.config.getoption(f'throngtest_{key}'))
        if value is None:
            raise KeyError(key)
        return value


def nonnegative(value: int) -> bool:
    return value >= 0


def nonempty(value: str) -> bool:
    return bool(value.strip())


def distribution(value: str) -> bool:
    return value in ('tests', 'files')


class Settings(Storage):
    workers: int = Field(4, validation={'workers must be nonnegative': nonnegative})
    check_fingerprints: bool = Field(False)
    backend: str = Field('temporary_directory', validation={'backend must not be empty': nonempty})
    distribution: str = Field('tests', validation={'distribution must be tests or files': distribution})
    python: str = Field(default_factory=lambda: sys.executable, validation={'python must not be empty': nonempty})
    exclude: List[str] = Field(default_factory=lambda: ['.git/', '.venv/', 'venv/', '__pycache__/', '.pytest_cache/', '.mypy_cache/', '.ruff_cache/', 'build/', 'dist/', 'mutants/'])
    preparation: List[str] = Field(default_factory=list, validation={'preparation commands must not be empty': lambda value: all(nonempty(item) for item in value)})
    packages: List[str] = Field(default_factory=list, validation={'package specifications must not be empty': lambda value: all(nonempty(item) for item in value)})


def read_settings(config: pytest.Config) -> Settings:
    try:
        check_fingerprints = cast(object, config.getoption('throngtest_check_fingerprints'))
        return Settings(_sources=[
            # Pytest supplies native booleans for flags; skelet validates their
            # type and resolves priority without treating an absent flag as false.
            MemorySource[object]({} if check_fingerprints is None else {'check_fingerprints': check_fingerprints}),
            PytestSource(config),
            EnvSource[object](prefix='THRONGTEST_'),
            TOMLSource[object](config.rootpath / 'pyproject.toml', table='tool.throngtest'),
        ])
    except (TypeError, ValueError, OSError) as error:
        raise pytest.UsageError(f'throngtest: {error}') from error
