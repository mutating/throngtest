"""Pytest entry point. Distribution is enabled by default with up to four isolates."""

from typing import Dict, Generator, List, Optional, cast

import pytest

from throngtest.protocol import Request
from throngtest.runner import Runner
from throngtest.settings import ARGUMENTS, WORKER, read_settings
from throngtest.xdist import Shard, suspend


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup('throngtest', 'Run test subsets in throng isolates')
    group.addoption('--isolates', dest='throngtest_isolates', metavar='COUNT', default=None,
                    help='Maximum isolate count; 0 disables distribution (default: 4)')
    for name, description in (
        ('backend', 'Throng plugin name (default: temporary_directory)'),
        ('distribution', 'Partition by tests or files (default: tests)'),
        ('python', 'Python executable available inside the isolate'),
        ('exclude', 'JSON list of patterns excluded from the isolate snapshot'),
        ('preparation', 'JSON list of commands run in each isolate before pytest (default: [])'),
        ('packages', 'JSON list of packages installed by the backend before preparation (default: [])'),
    ):
        group.addoption(f'--{name}', dest=f'throngtest_{name}', metavar=name.upper(), default=None, help=description)
    group.addoption('--check-fingerprints', action='store_const', const=True, default=None,
                    dest='throngtest_check_fingerprints', help='Require identical ordered test collections in all isolates (default: disabled)')
    group.addoption('--no-check-fingerprints', action='store_const', const=False, default=None,
                    dest='throngtest_check_fingerprints', help='Disable collection fingerprint checks, overriding environment and TOML settings')


def pytest_load_initial_conftests(early_config: pytest.Config, args: List[str]) -> None:
    early_config.stash[ARGUMENTS] = list(args)


@pytest.hookimpl(wrapper=True, tryfirst=True)  # type: ignore[misc]
def pytest_cmdline_main(config: pytest.Config) -> Generator[None, Optional[int], Optional[int]]:
    if not config.stash.get(WORKER, False) and not hasattr(config, 'workerinput') and read_settings(config).isolates:
        suspend(config)
    return (yield)


@pytest.hookimpl(trylast=True)  # type: ignore[misc]  # pluggy's decorator bound includes Any.
def pytest_configure(config: pytest.Config) -> None:
    workerinput = cast(Optional[Dict[str, object]], getattr(config, 'workerinput', None)) or {}
    if 'throngtest' in workerinput:
        config.stash[WORKER] = True
        config.pluginmanager.register(Shard(Request.unpack(cast(str, workerinput['throngtest']))), 'throngtest-shard')
    if config.stash.get(WORKER, False):
        return
    settings = read_settings(config)
    if settings.isolates:
        if cast(bool, config.getoption('usepdb')):
            raise pytest.UsageError('throngtest cannot be combined with --pdb')
        if any(cast(object, config.getoption(name, default=False)) for name in ('lf', 'failedfirst', 'newfirst', 'stepwise')):
            raise pytest.UsageError('throngtest does not support cache-based selection (--lf/--ff/--nf) or --stepwise')
        config.pluginmanager.register(Runner(settings), 'throngtest-runner')
