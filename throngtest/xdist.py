"""Optional cooperation with user-installed xdist, without importing it."""

from pathlib import Path
from typing import Dict, Generator, List, Optional, Protocol, Set, cast

import pytest

from throngtest.distribution import fingerprint, partition
from throngtest.protocol import Request, encode

NESTED = pytest.StashKey[bool]()


class Gateway(Protocol):
    id: str


class Node(Protocol):
    workerinput: Dict[str, object]
    workeroutput: Dict[str, object]
    gateway: Gateway


def suspend(config: pytest.Config) -> None:
    """Leave xdist's original arguments for the isolate, suppress its outer runner."""
    if cast(bool, config.getoption('looponfail', default=False)):
        raise pytest.UsageError('throngtest: --looponfail cannot run inside isolates')
    processes = cast(object, config.getoption('numprocesses', default=None))
    mode = cast(str, config.getoption('dist', default='no'))
    targets = cast(Optional[List[str]], config.getoption('tx', default=None)) or []
    active = bool(processes) or (processes is None and bool(targets) and (mode != 'no' or cast(bool, config.getoption('distload', default=False))))
    config.stash[NESTED] = active
    if not active:
        return
    if mode == 'each':
        raise pytest.UsageError('throngtest: --dist=each repeats tests on every xdist worker; use a distributing scheduler such as --dist=load')
    if targets or cast(object, config.getoption('px', default=None)):
        raise pytest.UsageError('throngtest: nested xdist supports local workers configured with -n; --tx and --px are not supported inside isolates')
    config.option.numprocesses = 0
    config.option.dist = 'no'
    config.option.distload = False


class Shard:
    """Select the isolate's subset before xdist advertises its collection."""

    def __init__(self, request: Request) -> None:
        self.request = request
        self.original: Dict[int, str] = {}

    @pytest.hookimpl(wrapper=True, tryfirst=True)  # type: ignore[misc]
    def pytest_collection_modifyitems(self, session: pytest.Session, config: pytest.Config, items: List[pytest.Item]) -> Generator[None, object, object]:
        grouped = cast(bool, config.getoption('loadgroup', default=False))
        # Let collection modifiers finish before xdist adds its group suffix.
        # Reuse xdist's own hook so both old single-group and new multi-group
        # formats work without duplicating its naming rules.
        config.option.loadgroup = False
        try:
            result = yield
        finally:
            config.option.loadgroup = grouped
        if grouped:
            self.original = {id(item): item.nodeid for item in items}
            others = [plugin for plugin in cast(Set[object], config.pluginmanager.get_plugins()) if type(plugin).__name__ != 'WorkerInteractor']
            config.pluginmanager.subset_hook_caller('pytest_collection_modifyitems', remove_plugins=others)(session=session, config=config, items=items)
        return result

    @pytest.hookimpl(tryfirst=True)  # type: ignore[misc]
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        grouped = cast(bool, session.config.getoption('loadgroup', default=False))
        nodeids = [self.original[id(item)] if grouped else item.nodeid for item in session.items]
        if self.request.fingerprint is not None and fingerprint(nodeids) != self.request.fingerprint:
            output = cast(Dict[str, object], getattr(session.config, 'workeroutput', None))
            output['throngtest_collection'] = nodeids
            raise pytest.UsageError('test collection differs between the controller and the isolate')
        shards = partition(nodeids, self.request.workers, self.request.distribution)
        indices = shards[self.request.shard] if self.request.shard < len(shards) else []
        assigned = [nodeids[index] for index in indices]
        session.items[:] = [session.items[index] for index in indices]
        session.testscollected = len(session.items)
        workerinput = cast(Dict[str, object], getattr(session.config, 'workerinput', None))
        # Local xdist workers share the isolate filesystem. Publish the mapping
        # before xdist announces collection, so it survives even a setup crash.
        Path(cast(str, workerinput['throngtest_manifest'])).write_text(encode({
            'assigned': assigned, 'ids': [item.nodeid for item in session.items],
        }))
