from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep
from types import SimpleNamespace
from typing import List, Set, Tuple, cast

import pytest
from cantok import AbstractToken, DefaultToken
from throng import AbstractIsolate, AbstractManager
from throng.abstracts.results import SimpleRunResult

from throngtest.runner import Runner
from throngtest.settings import ARGUMENTS, Settings


@pytest.mark.parametrize('phase', ['acquisition', 'installation', 'native preparation', 'preparation', 'worker'])
@pytest.mark.parametrize('interrupt', [False, True])
def test_failure_cancels_every_lifecycle_phase(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, phase: str, interrupt: bool) -> None:
    """Cancel a sibling during allocation, installation, preparation, or execution.

    A backend using the current abstract API blocks one isolate until its
    sibling raises. Real manager scopes own installation, native preparation,
    and cleanup. External observations detect missed cancellation even when
    the failing sibling's exception prevents the runner reading both futures.
    """
    started = Event()
    allocation_lock = Lock()
    calls: List[Tuple[str, int, AbstractToken]] = []
    cancelled: List[bool] = []
    created: Set[int] = set()
    killed: Set[int] = set()
    allocated: List[int] = []

    def wait_for_cancellation(token: AbstractToken) -> None:
        started.set()
        deadline = monotonic() + 5
        while token and monotonic() < deadline:
            sleep(0.01)
        cancelled.append(not bool(token))
        raise RuntimeError('sibling stopped')

    class Isolate(AbstractIsolate):
        def __init__(self, number: int) -> None:
            self.number = number
            created.add(number)

        def _run(self, command: str, token: AbstractToken = DefaultToken()) -> SimpleRunResult:  # noqa: B008
            operation = {'native setup': 'native preparation', 'project setup': 'preparation'}.get(command, 'worker')
            calls.append((operation, self.number, token))
            if self.number == 1 and operation == phase:
                wait_for_cancellation(token)
            if operation == 'worker':
                assert started.wait(5), 'sibling never reached the requested phase'
                if interrupt:
                    raise KeyboardInterrupt
                raise RuntimeError('primary failure')
            return SimpleRunResult(True, 0)

        def install(self, *packages: str, token: AbstractToken = DefaultToken()) -> None:  # noqa: B008
            assert packages == ('dependency',)
            calls.append(('installation', self.number, token))
            if self.number == 1 and phase == 'installation':
                wait_for_cancellation(token)

        def read(self) -> bytes:
            return b''

        def kill(self) -> None:
            killed.add(self.number)

    class Manager(AbstractManager):
        def _get(self, state: bytes, token: AbstractToken = DefaultToken()) -> Isolate:  # noqa: B008
            assert state == b'snapshot'
            with allocation_lock:
                number = len(allocated)
                allocated.append(number)
            calls.append(('acquisition', number, token))
            if number == 1 and phase == 'acquisition':
                wait_for_cancellation(token)
            return Isolate(number)

        def read(self) -> bytes:
            return b'snapshot'

    config = pytester.parseconfigure()
    config.stash[ARGUMENTS] = []
    session = pytest.Session.from_config(config)
    session.items = [cast(pytest.Item, SimpleNamespace(nodeid=f'test.py::test_{index}')) for index in range(2)]
    manager = Manager(config.rootpath, prepare=['native setup'], packages=['dependency'])

    def factory(path: Path, exclude: List[str], packages: List[str]) -> dict:
        assert path == config.rootpath
        assert exclude == ['excluded/']
        assert packages == manager.packages
        return {'probe': manager}

    monkeypatch.setattr('throngtest.runner.throng', factory)
    settings = Settings(_sources=[])
    settings.backend = 'probe'
    settings.workers = 2
    settings.packages = ['dependency']
    settings.exclude = ['excluded/']
    settings.preparation = ['project setup']
    with pytest.raises(KeyboardInterrupt if interrupt else pytest.exit.Exception) as caught:
        Runner(settings).pytest_runtestloop(session)
    if not interrupt:
        assert 'primary failure' in str(caught.value)
        assert isinstance(caught.value, pytest.exit.Exception)
        assert caught.value.returncode == 3
    assert cancelled == [True], f'cancellation did not reach {phase}'
    assert allocated == [0, 1]
    assert created == ({0} if phase == 'acquisition' else {0, 1})
    assert killed == created
    shared_token = calls[0][2]
    assert all(token is shared_token for _, _, token in calls)
    assert not shared_token
    assert [(operation, number) for operation, number, _ in calls if number == 1][-1] == (phase, 1)
