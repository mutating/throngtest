"""Coordinate isolates through the public throng manager API."""

import os
import shlex
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, cast
from uuid import uuid4

import pytest
from cantok import SimpleToken
from throng import AbstractManager, throng

from throngtest.coverage import coverage_agents
from throngtest.coverage_transport import receive
from throngtest.distribution import collection_difference, fingerprint, partition
from throngtest.protocol import Request, WorkerError, read_response
from throngtest.settings import ARGUMENTS, Settings
from throngtest.xdist import NESTED

VALUE_OPTIONS = frozenset(('--isolates', '--backend', '--distribution', '--python', '--exclude', '--preparation'))


def relocate(argument: str, root: Path, invocation: Path, directory: Path) -> str:
    """Relocate project paths without interpreting pytest's other arguments."""
    if not argument or argument.partition('=')[0] in VALUE_OPTIONS or (argument.startswith('-') and '=' not in argument):
        return argument
    prefix, separator, value = argument.partition('=') if argument.startswith('-') else ('', '', argument)
    path, *selectors = value.split('::')
    candidate = Path(path)
    if not candidate.is_absolute():
        if invocation == directory:
            return argument
        candidate = invocation / candidate
    if candidate.exists():
        try:
            candidate.relative_to(root)
        except ValueError:
            return argument
        value = '::'.join([os.path.relpath(candidate, directory), *selectors])
    return prefix + separator + value


def coverage_plan(config: pytest.Config, backend: str) -> Tuple[List[str], Optional[Dict[str, str]], Optional[Dict[str, Dict[str, object]]]]:
    """Select every active coverage agent and its controller-side data file."""
    agents = coverage_agents()
    enabled = [name for name, agent in agents.items() if agent.active(config)]
    if shared_local_coverage(backend):
        # coverage.py already starts in each local child and writes to the
        # explicit controller path. Other active agents still participate.
        enabled = [name for name in enabled if name != 'python_coverage']
    if not enabled:
        return [], None, None
    return enabled, {name: str(Path(agents[name].data_file()).absolute()) for name in enabled}, {name: agents[name].configuration(config.rootpath) for name in enabled}


def shared_local_coverage(backend: str) -> bool:
    """Detect coverage.py startup with an explicit same-machine data path."""
    data_file = os.environ.get('COVERAGE_FILE')
    return backend in ('local', 'temporary_directory') and bool(os.environ.get('COVERAGE_PROCESS_START')) and bool(data_file and Path(data_file).is_absolute())


@contextmanager
def isolate_coverage_environment(enabled: bool) -> Iterator[None]:
    """Keep controller-only coverage paths out of potentially remote Python."""
    names = ('COVERAGE_PROCESS_START', 'COV_CORE_SOURCE', 'COV_CORE_CONFIG', 'COV_CORE_DATAFILE', 'COV_CORE_BRANCH', 'COV_CORE_CONTEXT')
    saved = {name: os.environ.pop(name) for name in names if enabled and name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def worker_arguments(config: pytest.Config, root: Path, invocation: Path, directory: Path) -> List[str]:
    """Preserve effective pytest options while relocating project paths."""
    arguments: List[str] = []
    preserve_value = False
    for argument in config.stash[ARGUMENTS]:
        if preserve_value:
            arguments.append(argument)
            preserve_value = False
        else:
            arguments.append(relocate(argument, root, invocation, root / directory))
            preserve_value = argument in VALUE_OPTIONS
    arguments.extend(['--rootdir', os.path.relpath(root, root / directory)])
    return arguments


def backend_error(error: Exception, stage: str, request: Request, settings: Settings, preparation_output: str) -> WorkerError:
    """Keep backend phase and exception chain visible in pytest's final exit."""
    detail = str(error)
    if not isinstance(error, WorkerError):
        detail = f'{type(error).__name__}: {error}'
        cause = error.__cause__
        while cause is not None:
            detail += f'\ncaused by {type(cause).__name__}: {cause}'
            cause = cause.__cause__
    message = f'isolate {request.shard + 1} (backend={settings.backend}): {stage} failed: {detail}'
    if preparation_output:
        message += f'\npreparation output:\n{preparation_output}'
    return WorkerError(message, exitcode=error.exitcode if isinstance(error, WorkerError) else 3)


def execute(manager: AbstractManager, request: Request, settings: Settings, token: SimpleToken, nodeids: Sequence[str] = ()) -> Dict[str, object]:
    command = shlex.join([settings.python, '-c', 'from throngtest.worker import main; import sys; main(sys.argv[1])', request.pack()])
    preparation_output = ''
    stage = 'acquiring isolate'
    try:
        with manager.scope as isolate:
            for index, instruction in enumerate(settings.preparation, 1):
                stage = f'running preparation command {index}'
                try:
                    prepared = isolate.run(instruction, token=token)
                except Exception as error:
                    raise WorkerError(f'isolate {request.shard + 1}: preparation command {index} could not execute: {instruction}\n{error}') from error
                preparation_output += (prepared.stdout or '') + (prepared.stderr or '')
                if prepared.returncode != 0:
                    raise WorkerError(
                        f'isolate {request.shard + 1}: preparation command {index} failed '
                        f'with exit code {prepared.returncode}: {instruction}',
                    )
            stage = 'running pytest worker'
            result = isolate.run(command, token=token)
            if request.coverage_agents and result.returncode in (0, 1) and request.marker in [line[:len(request.marker)] for line in (result.stdout or '').splitlines()]:
                stage = 'exporting coverage'
                exported = isolate.run(shlex.join([settings.python, '-c', 'from throngtest.coverage_transport import export; import sys; export(sys.argv[1])', request.pack()]), token=token)
                if exported.returncode != 0:
                    raise WorkerError(f'isolate could not export coverage: {exported.stderr or exported.stdout}')
                coverage_payload = read_response(exported.stdout or '', request.marker)
            stage = 'releasing isolate'
    except Exception as error:  # Third-party plugins may raise their own exception types.
        raise backend_error(error, stage, request, settings, preparation_output) from error
    stdout = result.stdout or ''
    try:
        if request.coverage_agents and result.returncode in (0, 1) and request.marker in [line[:len(request.marker)] for line in stdout.splitlines()]:
            receive(coverage_payload, request.coverage_targets or {}, Path.cwd())
        response = read_response(stdout, request.marker)
        # The decoded protocol is represented by the diagnostic below; avoid
        # flooding errors with its base64 payload. Preserve unrelated output.
        lines = stdout.splitlines(keepends=True)
        response_index = max(index for index, line in enumerate(lines) if line.startswith(request.marker))
        stdout = ''.join(lines[:response_index])
        if stdout.endswith('\n'):
            stdout = stdout[:-1]
        stdout += ''.join(lines[response_index + 1:])
        if response.get('exitcode') != result.returncode:
            raise WorkerError('worker exit code does not match its result')
        if response['exitcode'] not in (0, 1):
            detail = str(response.get('error', ''))
            if 'collection' in response:
                collection = response['collection']
                if not isinstance(collection, list) or any(not isinstance(item, str) for item in collection):
                    raise WorkerError('worker returned a malformed test collection')
                detail = collection_difference(nodeids, cast(List[str], collection))
            raise WorkerError(f"worker exited with code {response['exitcode']}: {detail}", exitcode=2 if response['exitcode'] == 2 else 3)
        response['output'] = preparation_output + stdout + (result.stderr or '')
        return response
    except WorkerError as error:
        raise WorkerError(f'{error}\npreparation output:\n{preparation_output}\nstdout:\n{stdout}\nstderr:\n{result.stderr or ""}', exitcode=error.exitcode) from error


def finished_tests(session: pytest.Session, response: Dict[str, object], expected: List[str], parallel: bool) -> List[str]:
    finished = response.get('finished')
    if not isinstance(finished, list):
        raise WorkerError('worker returned malformed reports')
    if any(not isinstance(nodeid, str) for nodeid in finished):
        raise WorkerError('worker returned malformed finished tests')
    completed = cast(List[str], finished)
    matches = Counter(completed) == Counter(expected) if parallel else completed == expected
    if not matches:
        # With fail-fast pytest is allowed to finish just a prefix of a shard.
        maxfail = cast(int, session.config.getoption('maxfail'))
        subset = not (Counter(completed) - Counter(expected)) if parallel else completed == expected[:len(completed)]
        interrupted = maxfail or (parallel and response.get('worker_crashed') is True)
        if not (interrupted and response['exitcode'] == 1 and completed and subset):
            raise WorkerError('worker did not execute its assigned tests exactly once')
    return completed


def replay(session: pytest.Session, response: Dict[str, object], expected: Optional[List[str]], parallel: bool = False) -> None:
    if expected is None:
        assigned = response.get('assigned')
        if not isinstance(assigned, list) or any(not isinstance(item, str) for item in assigned):
            raise WorkerError('worker returned malformed assigned tests')
        expected = cast(List[str], assigned)
    reports = response.get('reports')
    if not isinstance(reports, list):
        raise WorkerError('worker returned malformed reports')
    completed = finished_tests(session, response, expected, parallel)
    decoded: List[pytest.TestReport] = []
    for data in cast(List[object], reports):
        if not isinstance(data, dict):
            raise WorkerError('worker returned a malformed test report')
        # JSON has no tuples; pytest's skip representation requires one.
        record = cast(Dict[str, object], data)
        longrepr = record.get('longrepr')
        if isinstance(longrepr, list):
            record['longrepr'] = tuple(cast(List[object], longrepr))
        report = cast(object, session.config.hook.pytest_report_from_serializable(config=session.config, data=data))
        if not isinstance(report, pytest.TestReport) or report.nodeid not in expected:  # type: ignore[misc]  # pytest report constructors accept arbitrary plugin attributes.
            raise WorkerError('worker returned an unexpected test report')
        decoded.append(report)
    endings = [report.nodeid for report in decoded if report.when == 'teardown' or (parallel and report.when == '???')]
    if not (Counter(endings) == Counter(completed) if parallel else endings == completed):
        raise WorkerError('worker returned an incomplete set of test reports')
    if parallel:
        decoded = parallel_reports(decoded)
    block: List[pytest.TestReport] = []
    for report in decoded:
        block.append(report)
        if parallel and report.when == '???' and report.failed:
            block.clear()
        elif report.when == 'teardown':
            stages = ['setup', 'call', 'teardown'] if block[0].passed else ['setup', 'teardown']
            if [part.when for part in block] != stages or any(part.nodeid != report.nodeid for part in block):
                raise WorkerError('worker returned incomplete test phases')
            block.clear()
    if block:
        raise WorkerError('worker returned unterminated test phases')
    if response['exitcode'] == 1 and not any(report.failed for report in decoded) and not session.testsfailed:
        raise WorkerError('worker failed without reporting a test failure')
    for warning in cast(List[Dict[str, object]], response.get('warnings', [])):
        warning_message = warnings.WarningMessage(
            f"{warning['category']}: {warning['message']}", pytest.PytestWarning,
            cast(str, warning['filename']), cast(int, warning['lineno']),
        )
        session.config.hook.pytest_warning_recorded.call_historic(kwargs={
            'warning_message': warning_message, 'when': 'runtest',
            'nodeid': warning['nodeid'], 'location': None,
        })
    for report in decoded:
        if report.when == 'setup':
            session.config.hook.pytest_runtest_logstart(nodeid=report.nodeid, location=report.location)
        session.config.hook.pytest_runtest_logreport(report=report)
        if report.when == 'teardown' or (parallel and report.when == '???'):
            session.config.hook.pytest_runtest_logfinish(nodeid=report.nodeid, location=report.location)
    output = cast(str, response.get('output', ''))
    if output:
        reporter = session.config.pluginmanager.hasplugin('terminalreporter')
        if reporter:
            session.config.get_terminal_writer().write(output)


def parallel_reports(reports: List[pytest.TestReport]) -> List[pytest.TestReport]:
    """Reassemble interleaved xdist phases into complete individual executions."""
    pending: Dict[str, List[pytest.TestReport]] = {}
    result: List[pytest.TestReport] = []
    for report in reports:
        worker = cast(object, getattr(report, 'worker_id', None))
        if not isinstance(worker, str):
            raise WorkerError('xdist report is missing its worker ID')
        block = pending.setdefault(worker, [])
        block.append(report)
        if any(part.nodeid != report.nodeid for part in block):
            raise WorkerError('worker returned incomplete test phases')
        if report.when in ('teardown', '???'):
            if report.when == '???' and (not report.failed or [part.when for part in block[:-1]] not in ([], ['setup'], ['setup', 'call'])):
                raise WorkerError('worker returned malformed crash phases')
            result.extend(block)
            del pending[worker]
    if pending:
        raise WorkerError('worker returned unterminated test phases')
    return result


class Runner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def pytest_report_header(self) -> str:
        return f'throngtest: {self.settings.workers} isolates, backend={self.settings.backend}, distribution={self.settings.distribution}'

    @pytest.hookimpl(tryfirst=True)  # type: ignore[misc]  # pluggy's decorator exposes Any in its generic bound.
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        if session.testsfailed and not cast(bool, session.config.getoption('continue_on_collection_errors')):
            raise session.Interrupted(f'{session.testsfailed} errors during collection')
        if cast(bool, session.config.getoption('collectonly')) or not session.items:
            return True
        root = session.config.rootpath
        invocation = session.config.invocation_params.dir
        try:
            directory = invocation.relative_to(root)
        except ValueError:
            directory = Path()
        arguments = worker_arguments(session.config, root, invocation, directory)
        nodeids = [item.nodeid for item in session.items]
        shards = partition(nodeids, self.settings.workers, self.settings.distribution)
        collection_fingerprint = fingerprint(nodeids) if self.settings.check_fingerprints else None
        enabled, targets, configurations = coverage_plan(session.config, self.settings.backend)
        completed = 0
        token = SimpleToken()
        previous = Path.cwd()
        try:
            # throng's built-in archive backend expects a relative source path;
            # local also uses the controller's cwd. Never chdir in worker threads.
            os.chdir(root)
            managers = throng('.', exclude=self.settings.exclude)
            if self.settings.backend not in managers:
                raise pytest.UsageError(f'throngtest: unknown backend {self.settings.backend!r}; available: {", ".join(sorted(managers))}')
            manager = managers[self.settings.backend]
            with isolate_coverage_environment(bool(enabled)), ThreadPoolExecutor(max_workers=len(shards), thread_name_prefix='throngtest') as pool:
                try:
                    futures = {
                        pool.submit(execute, manager, Request(
                            arguments, collection_fingerprint, len(shards),
                            self.settings.distribution, index, f'THRONGTEST_{uuid4().hex}:', str(directory),
                            enabled, targets, configurations,
                        ), self.settings, token, nodeids): shard
                        for index, shard in enumerate(shards)
                    }
                    for future in as_completed(futures):
                        response = future.result()
                        replay(session, response, [nodeids[index] for index in futures[future]] if self.settings.check_fingerprints else None,
                               session.config.stash.get(NESTED, False) and response.get('parallel') is True)
                        completed += len(cast(List[str], response['finished']))
                        if session.shouldfail:
                            raise session.Failed(session.shouldfail)
                        if session.shouldstop:
                            raise session.Interrupted(session.shouldstop)
                finally:
                    token.cancel()
        except WorkerError as error:
            pytest.exit(f'throngtest: {error}', returncode=error.exitcode)
        finally:
            os.chdir(previous)
        if not completed:
            session.testscollected = 0
        return True
