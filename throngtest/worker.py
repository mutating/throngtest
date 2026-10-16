"""Runs inside an isolate; never creates further isolates."""

import json
import os
import sys
import warnings
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, cast

import pytest
from _pytest._io import TerminalWriter
from _pytest.terminal import TerminalReporter

from throngtest.coverage import coverage_agents
from throngtest.coverage_transport import marker_path
from throngtest.distribution import fingerprint, partition
from throngtest.protocol import Request, decode, encode
from throngtest.settings import WORKER
from throngtest.xdist import Node


class Worker:
    def __init__(self, request: Request, basetemp: str) -> None:
        self.request = request
        self.basetemp = basetemp
        self.config: Optional[pytest.Config] = None
        self.reports: List[Dict[str, object]] = []
        self.finished: List[str] = []
        self.error = ''
        self.collection: Optional[List[str]] = None
        self.assigned: Optional[List[str]] = None
        self.warnings: List[Dict[str, object]] = []
        self.parallel = False
        self.crashed = False
        self.names: Dict[str, str] = {}

    def pytest_load_initial_conftests(self, early_config: pytest.Config) -> None:
        early_config.stash[WORKER] = True

    @pytest.hookimpl(optionalhook=True)  # type: ignore[misc]
    def pytest_configure_node(self, node: Node) -> None:
        self.parallel = True
        node.workerinput['throngtest'] = self.request.pack()
        node.workerinput['throngtest_manifest'] = str(Path(self.basetemp) / f'throngtest-{node.gateway.id}.data')

    @pytest.hookimpl(optionalhook=True)  # type: ignore[misc]
    def pytest_xdist_node_collection_finished(self, node: Node) -> None:
        manifest = decode(Path(cast(str, node.workerinput['throngtest_manifest'])).read_text())
        self.assigned = cast(List[str], manifest['assigned'])
        self.names.update(zip(cast(List[str], manifest['ids']), self.assigned))

    @pytest.hookimpl(optionalhook=True)  # type: ignore[misc]
    def pytest_testnodedown(self, node: Node, error: object) -> None:
        self.crashed |= error is not None
        output = cast(Optional[Dict[str, object]], getattr(node, 'workeroutput', None)) or {}
        if 'throngtest_collection' in output:
            self.collection = cast(List[str], output['throngtest_collection'])

    @pytest.hookimpl(tryfirst=True)  # type: ignore[misc]  # pluggy's decorator bound includes Any.
    def pytest_configure(self, config: pytest.Config) -> None:
        self.config = config
        # Only the controller writes aggregate output files. Each worker gets
        # its own pytest temporary directory even when --basetemp was supplied.
        config.option.xmlpath = None
        config.option.basetemp = self.basetemp

    @pytest.hookimpl(tryfirst=True)  # type: ignore[misc]
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        reporter = cast(Optional[TerminalReporter], session.config.pluginmanager.get_plugin('terminalreporter'))
        if reporter is not None:
            # Assertion formatting still needs the terminal writer. Keep the
            # reporter registered, but discard its duplicate session output.
            reporter._tw = TerminalWriter(StringIO())

    @pytest.hookimpl(trylast=True)  # type: ignore[misc]
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        nodeids = [item.nodeid for item in session.items]
        if self.request.fingerprint is not None and fingerprint(nodeids) != self.request.fingerprint:
            self.error = 'test collection differs between the controller and the isolate'
            self.collection = nodeids
            raise pytest.UsageError(self.error)
        shards = partition(nodeids, self.request.isolates, self.request.distribution)
        indices = shards[self.request.shard] if self.request.shard < len(shards) else []
        self.assigned = [nodeids[index] for index in indices]
        session.items[:] = [session.items[index] for index in indices]
        session.testscollected = len(session.items)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        assert self.config is not None
        data = cast(Dict[str, object], self.config.hook.pytest_report_to_serializable(config=self.config, report=report))
        node = data.pop('node', None)
        if node is not None:
            data['worker_id'] = cast(Node, node).gateway.id
        if report.when == '???':
            self.finished.append(report.nodeid)
        self.reports.append(data)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.error += str(report.longrepr) + '\n'

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        self.finished.append(nodeid)

    def pytest_warning_recorded(self, warning_message: warnings.WarningMessage, when: str, nodeid: str) -> None:
        if when == 'runtest':
            self.warnings.append({
                'message': str(warning_message.message),
                'category': warning_message.category.__name__,
                'filename': warning_message.filename,
                'lineno': warning_message.lineno,
                'nodeid': nodeid,
            })


def main(value: str) -> None:
    request = Request.unpack(value)
    root = Path.cwd().resolve()
    os.chdir(request.directory)
    agents = coverage_agents()
    selected_names = request.coverage_agents or []
    if selected_names:
        marker_path(request.marker).touch()
    for name in selected_names:
        agents[name].start_worker(request.marker, (request.coverage_configurations or {}).get(name, {}), root)
    if selected_names:
        data_files: Dict[str, str] = {name: agents[name].data_file() for name in selected_names}
        marker_path(request.marker).write_text(json.dumps(data_files))
    # Effective arguments already include ini addopts and PYTEST_ADDOPTS.
    os.environ.pop('PYTEST_ADDOPTS', None)
    with TemporaryDirectory(prefix='throngtest-pytest-') as basetemp:
        worker = Worker(request, basetemp)
        try:
            agent_arguments = [argument for name in selected_names for argument in agents[name].pytest_arguments()]
            status = pytest.main([*request.arguments, *agent_arguments, '-o', 'addopts='], plugins=[worker])
        finally:
            for name in selected_names:
                agents[name].stop_worker()
        if status == pytest.ExitCode.NO_TESTS_COLLECTED and worker.assigned == []:
            status = pytest.ExitCode.OK
        if worker.parallel and worker.error and not worker.reports:
            status = pytest.ExitCode.INTERRUPTED
        if worker.collection is not None:
            status = pytest.ExitCode.USAGE_ERROR
        if worker.parallel:
            for report in worker.reports:
                nodeid = cast(str, report['nodeid'])
                report['nodeid'] = worker.names.get(nodeid, nodeid)
            worker.finished = [worker.names.get(nodeid, nodeid) for nodeid in worker.finished]
            outcomes = [report['outcome'] for report in worker.reports]
            if status == pytest.ExitCode.INTERRUPTED and worker.config is not None and cast(int, worker.config.getoption('maxfail')) and 'failed' in outcomes:
                status = pytest.ExitCode.TESTS_FAILED
        response: Dict[str, object] = {
            'version': 1,
            'exitcode': int(status),
            'reports': worker.reports,
            'finished': worker.finished,
            'assigned': worker.assigned,
            'error': worker.error,
            'warnings': worker.warnings,
            'worker_crashed': worker.crashed,
            'parallel': worker.parallel,
        }
        if worker.collection is not None:
            response['collection'] = worker.collection
        # A leading newline also handles tests that print without a newline.
        sys.stdout.write('\n' + request.marker + encode(response) + '\n')
        sys.stdout.flush()
    raise SystemExit(status)
