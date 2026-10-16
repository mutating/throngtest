from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Literal, cast
from unittest.mock import MagicMock, Mock

import pytest
from cantok import SimpleToken
from throng import AbstractManager

from throngtest.protocol import Request, WorkerError, encode
from throngtest.runner import Runner, execute, relocate, replay, worker_arguments
from throngtest.settings import ARGUMENTS, Settings


def test_relocate_paths(tmp_path: Path) -> None:
    """Relocate project paths while preserving node selectors and unrelated arguments.

    A file named '2' exposes accidental relocation of --isolates=2. The long
    preparation value checks that command text is not treated as a filesystem path.
    """
    root = tmp_path / 'project'
    root.mkdir()
    test = root / 'test.py'
    test.touch()
    assert relocate('--rootdir=' + str(root), root, tmp_path, root) == '--rootdir=.'
    assert relocate(str(test) + '::test_a[x y]', root, tmp_path, root) == 'test.py::test_a[x y]'
    assert relocate('project/test.py', root, tmp_path, root) == 'test.py'
    assert relocate(str(tmp_path), root, root, root) == str(tmp_path)
    assert relocate('-x', root, root, root) == '-x'
    assert relocate('', root, root, root) == ''
    assert relocate('missing', root, tmp_path, root) == 'missing'
    child = root / 'subdir'
    child.mkdir()
    (child / '2').touch()
    assert relocate('--isolates=2', root, child, root) == '--isolates=2'
    preparation = '--preparation=["echo ' + 'long command ' * 100 + '"]'
    assert relocate(preparation, root, tmp_path, root) == preparation


@pytest.mark.parametrize('option', ['--isolates', '--backend', '--distribution', '--python', '--exclude', '--preparation', '--packages'])
def test_worker_arguments_preserve_unprefixed_option_values(pytester: pytest.Pytester, tmp_path: Path, option: str) -> None:
    """Keep isolate option values intact with both CLI argument spellings.

    A same-named file in the invocation directory would trigger path rewriting
    if a value were mistaken for a test path during worker dispatch.
    """
    root = tmp_path / 'project'
    invocation = root / 'subdirectory'
    invocation.mkdir(parents=True)
    (invocation / 'value').touch()
    config = pytester.parseconfig('--isolates=0')
    for arguments in ([option, 'value'], [f'{option}=value']):
        config.stash[ARGUMENTS] = arguments
        assert worker_arguments(config, root, invocation, Path())[:len(arguments)] == arguments


@pytest.mark.parametrize('stage', ['enter', 'run', 'exit'])
def test_isolate_errors_are_reported_and_cleanup_attempted(stage: str) -> None:
    """Name the failing backend phase and attempt cleanup after scope entry.

    The mocked context manager fails separately on entry, execution, and exit
    to check the cleanup obligations at each point in the isolate lifecycle.
    """
    manager = Mock()
    scope = manager.scope.return_value
    scope.__enter__ = Mock()
    scope.__exit__ = Mock(return_value=False)
    isolate = scope.__enter__.return_value
    target = {'enter': scope.__enter__, 'run': isolate.run, 'exit': scope.__exit__}[stage]
    target.side_effect = RuntimeError('backend problem')
    with pytest.raises(WorkerError) as caught:
        execute(manager, Request([], '', 1, 'tests', 0, 'marker:', '.'), Settings(_sources=[]), SimpleToken(), Path.cwd())
    phase = {'enter': 'acquiring isolate', 'run': 'running pytest worker', 'exit': 'releasing isolate'}[stage]
    assert f'{phase} failed: RuntimeError: backend problem' in str(caught.value)
    if stage != 'enter':
        scope.__exit__.assert_called_once()


def test_backend_error_preserves_underlying_connection_failure() -> None:
    """Expose the socket error beneath a third-party isolate allocation failure.

    Backend exceptions can wrap a lower-level transport exception. The runner
    must show both causes and the allocation phase, even though pytest reports
    only the final WorkerError message.
    """
    manager = Mock()
    manager.scope.return_value.__enter__ = Mock()
    manager.scope.return_value.__exit__ = Mock(return_value=False)
    failure = RuntimeError('The connection was lost; commands are never replayed.')
    failure.__cause__ = OSError('socket is already closed')
    manager.scope.return_value.__enter__.side_effect = failure
    settings = Settings(_sources=[])
    settings.backend = 'fission'
    with pytest.raises(WorkerError) as caught:
        execute(manager, Request([], None, 1, 'tests', 0, 'marker:', '.'), settings, SimpleToken(), Path.cwd())
    message = str(caught.value)
    assert 'isolate 1 (backend=fission): acquiring isolate failed' in message
    assert 'RuntimeError: The connection was lost; commands are never replayed.' in message
    assert 'caused by OSError: socket is already closed' in message


def test_exit_code_mismatch() -> None:
    """Reject a worker response whose exit code contradicts the process exit status."""
    manager = MagicMock()
    manager.scope.return_value.__enter__.return_value.run.return_value = SimpleNamespace(stdout='marker:' + encode({'version': 1, 'exitcode': 0}), stderr='diagnostic', returncode=1)
    with pytest.raises(WorkerError, match='exit code does not match'):
        execute(manager, Request([], '', 1, 'tests', 0, 'marker:', '.'), Settings(_sources=[]), SimpleToken(), Path.cwd())
    manager.scope.return_value.__exit__.assert_called_once()


def test_collection_diagnostic_retains_output_without_encoded_response() -> None:
    """Show collection changes and process output without exposing the protocol payload.

    The mocked stdout surrounds an encoded collection response with ordinary
    output, checking that filtering removes only protocol data and blank lines.
    """
    manager = MagicMock()
    data: Dict[str, object] = {'version': 1, 'exitcode': 4, 'collection': ['test.py::new']}
    manager.scope.return_value.__enter__.return_value.run.return_value = SimpleNamespace(
        stdout='before\n\nmarker:' + encode(data) + '\nafter\n', stderr='stderr diagnostic', returncode=4,
    )
    with pytest.raises(WorkerError) as caught:
        execute(manager, Request([], '', 1, 'tests', 0, 'marker:', '.'), Settings(_sources=[]), SimpleToken(), Path.cwd(), nodeids=['test.py::old'])
    diagnostic = str(caught.value)
    assert caught.value.exitcode == 3
    assert 'controller: 1 selected tests\nisolate: 1 selected tests' in diagnostic
    assert '-test.py::old\n+test.py::new' in diagnostic
    assert 'stdout:\nbefore\nafter\n' in diagnostic
    assert 'stderr diagnostic' in diagnostic
    assert 'marker:' not in diagnostic
    assert encode(data) not in diagnostic


@pytest.mark.parametrize('collection', ['not a list', [42]])
def test_malformed_collection_is_reported(collection: object) -> None:
    """Reject worker collection data that is not a list of string identifiers."""
    manager = MagicMock()
    manager.scope.return_value.__enter__.return_value.run.return_value = SimpleNamespace(
        stdout='marker:' + encode({'version': 1, 'exitcode': 4, 'collection': collection}), stderr='', returncode=4,
    )
    with pytest.raises(WorkerError, match='malformed test collection'):
        execute(manager, Request([], '', 1, 'tests', 0, 'marker:', '.'), Settings(_sources=[]), SimpleToken(), Path.cwd())


def report_data(when: Literal['setup', 'call', 'teardown'], outcome: Literal['passed', 'failed', 'skipped'] = 'passed', nodeid: str = 'test.py::test_a') -> Dict[str, object]:
    report = pytest.TestReport(nodeid, ('test.py', 0, 'test_a'), {}, outcome, None, when)
    return report._to_json()


@pytest.fixture
def session(pytester: pytest.Pytester) -> pytest.Session:
    config = pytester.parseconfigure()
    session = pytest.Session.from_config(config)
    # Exercise real pytest report serialization, without terminal session state.
    config.pluginmanager.unregister(name='terminalreporter')
    return session


@pytest.mark.parametrize('checked', [False, True])
@pytest.mark.parametrize(('change', 'message'), [
    ({'finished': None}, 'malformed reports'),
    ({'reports': None}, 'malformed reports'),
    ({'finished': []}, 'exactly once'),
    ({'finished': ['other']}, 'exactly once'),
    ({'reports': [None]}, 'malformed test report'),
    ({'reports': [dict(report_data('call'), nodeid='other')]}, 'unexpected test report'),
    ({'reports': [report_data('call')]}, 'incomplete set'),
    ({'reports': [report_data('teardown')]}, 'incomplete test phases'),
    ({'reports': [report_data('setup'), report_data('call'), report_data('teardown'), report_data('setup')]}, 'unterminated'),
    ({'exitcode': 1}, 'without reporting a test failure'),
])
def test_corrupted_or_incomplete_reports_never_pass(session: pytest.Session, change: Dict[str, object], message: str, checked: bool) -> None:
    """Reject corrupted results regardless of whether fingerprints are checked.

    Each case alters one field in an otherwise complete result. Reports use
    pytest's own JSON representation, with its report-type discriminator added
    explicitly so deserialization reaches the intended consistency check.
    """
    data: Dict[str, object] = {
        'exitcode': 0, 'finished': ['test.py::test_a'],
        'assigned': ['test.py::test_a'],
        'reports': [report_data('setup'), report_data('call'), report_data('teardown')],
    }
    data.update(change)
    # _to_json is the payload before pytest adds its discriminator.
    for report in cast(List[object], data['reports'] or []):
        if isinstance(report, dict):
            report['$report_type'] = 'TestReport'
    with pytest.raises(WorkerError, match=message):
        replay(session, data, ['test.py::test_a'] if checked else None)


@pytest.mark.parametrize('assigned', [None, 'not a list', [42]])
def test_unchecked_reports_still_require_valid_assignment(session: pytest.Session, assigned: object) -> None:
    """Validate worker-assigned identifiers even when controller fingerprints are disabled."""
    with pytest.raises(WorkerError, match='malformed assigned tests'):
        replay(session, {'assigned': assigned}, None)


def test_interrupt_cancels_token_without_changing_cwd(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cancel dispatched work without changing the controller's working directory.

    A stub executor records the real cancellation token, and report replay is
    bypassed to isolate runner cleanup. The invocation directory deliberately
    differs from the project root; any chdir is forbidden, even temporarily.
    """
    config = pytester.parseconfigure()
    session = pytest.Session.from_config(config)
    session.items = [cast(pytest.Item, SimpleNamespace(nodeid='test.py::test_a'))]
    session.shouldstop = 'requested stop'
    config.stash[ARGUMENTS] = []
    token_seen: List[SimpleToken] = []

    def fake_execute(manager: AbstractManager, _request: Request, _settings: Settings, token: SimpleToken, root: Path, _nodeids: List[str]) -> Dict[str, object]:
        assert manager.path == root == config.rootpath
        assert Path.cwd() == tmp_path
        token_seen.append(token)
        return {'finished': ['test.py::test_a']}

    monkeypatch.setattr('throngtest.runner.execute', fake_execute)
    monkeypatch.setattr('throngtest.runner.replay', lambda *_args: None)
    monkeypatch.chdir(tmp_path)
    chdir = Mock(side_effect=AssertionError('controller must not change cwd'))
    # Restore chdir before older pytester versions use it during teardown.
    with monkeypatch.context() as patch:
        patch.setattr('throngtest.runner.os.chdir', chdir)
        settings = Settings(_sources=[])
        settings.isolates = 1
        with pytest.raises(session.Interrupted, match='requested stop'):
            Runner(settings).pytest_runtestloop(session)
        assert Path.cwd() == tmp_path
        assert len(token_seen) == 1
        assert not token_seen[0]
        chdir.assert_not_called()


def xdist_report(nodeid: str, when: str, worker: str = 'gw0', outcome: str = 'passed') -> Dict[str, object]:
    return {'nodeid': nodeid, 'when': when, 'worker_id': worker, 'outcome': outcome,
            'location': ('test.py', 0, 'test'), 'keywords': {}, 'longrepr': None, '$report_type': 'TestReport'}


def test_parallel_reports_accept_interleaving_and_completion_order(session: pytest.Session) -> None:
    """Accept interleaved reports and completion order independent of assignment order.

    Synthetic reports make gw1 finish its test between gw0's setup and call,
    exercising per-worker phase tracking without depending on process timing.
    """
    reports = [xdist_report('a', 'setup'), xdist_report('b', 'setup', 'gw1'),
               xdist_report('b', 'call', 'gw1'), xdist_report('b', 'teardown', 'gw1'),
               xdist_report('a', 'call'), xdist_report('a', 'teardown')]
    replay(session, {'exitcode': 0, 'finished': ['b', 'a'], 'reports': reports}, ['a', 'b'], parallel=True)
    assert session.testsfailed == 0


@pytest.mark.parametrize('finished', [[], ['a', 'a'], ['a', 'unexpected']])
def test_parallel_finished_tests_cannot_omit_or_duplicate_assignments(session: pytest.Session, finished: List[str]) -> None:
    """Reject successful parallel results with missing, repeated, or unassigned completions."""
    with pytest.raises(WorkerError, match='exactly once'):
        replay(session, {'exitcode': 0, 'finished': finished, 'reports': []}, ['a', 'b'], parallel=True)


@pytest.mark.parametrize('parallel', [False, True])
def test_finished_tests_must_be_strings(session: pytest.Session, parallel: bool) -> None:
    """Require string completion identifiers in both serial and parallel results."""
    with pytest.raises(WorkerError, match='malformed finished'):
        replay(session, {'exitcode': 0, 'finished': [{}], 'reports': []}, ['a'], parallel=parallel)


def test_parallel_failfast_can_finish_a_nonprefix_subset(session: pytest.Session) -> None:
    """Allow parallel failfast to finish a later assignment before earlier tests run.

    Only the second assigned test reports a failure, demonstrating why a valid
    parallel failfast result need not be a prefix of the assignment list.
    """
    session.config.option.maxfail = 1
    reports = [xdist_report('b', 'setup'), xdist_report('b', 'call', outcome='failed'), xdist_report('b', 'teardown')]
    replay(session, {'exitcode': 1, 'finished': ['b'], 'reports': reports}, ['a', 'b'], parallel=True)
    assert session.testsfailed == 1


@pytest.mark.parametrize(('reports', 'finished', 'message'), [
    ([xdist_report('a', 'setup')], ['a'], 'incomplete set'),
    ([dict(xdist_report('a', 'teardown'), worker_id=None)], ['a'], 'worker ID'),
    ([xdist_report('b', 'setup'), xdist_report('a', 'teardown')], ['a'], 'incomplete test phases'),
    ([xdist_report('a', 'setup'), xdist_report('a', 'teardown')], ['a'], 'incomplete test phases'),
    ([xdist_report('a', 'setup', 'gw1'), xdist_report('a', '???', outcome='failed')], ['a'], 'unterminated'),
    ([xdist_report('a', '???')], ['a'], 'malformed crash phases'),
    ([xdist_report('a', 'call'), xdist_report('a', '???', outcome='failed')], ['a'], 'malformed crash phases'),
    ([xdist_report('a', 'teardown'), xdist_report('a', 'teardown')], ['a'], 'incomplete set'),
])
def test_invalid_parallel_phases_fail(session: pytest.Session, reports: List[Dict[str, object]], finished: List[str], message: str) -> None:
    """Reject inconsistent parallel report phases even when failfast permits partial results.

    Handcrafted report streams exercise invalid worker IDs, phase sequences,
    and xdist's '???' crash phase without relying on nondeterministic crashes.
    """
    session.config.option.maxfail = 1
    with pytest.raises(WorkerError, match=message):
        replay(session, {'exitcode': 1, 'finished': finished, 'reports': reports}, ['a', 'b'], parallel=True)
