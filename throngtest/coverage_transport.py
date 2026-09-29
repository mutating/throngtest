"""Move coverage data through isolate command output, without shared storage."""

import base64
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Dict, List, Set, cast
from uuid import uuid4

from throngtest.protocol import Request, WorkerError, encode


def marker_path(marker: str) -> Path:
    return Path('.throngtest-coverage-' + hashlib.sha256(marker.encode()).hexdigest())


def worker_data_file(marker: str) -> str:
    return '.coverage.throngtest-' + hashlib.sha256(marker.encode()).hexdigest()


def export(value: str) -> None:
    """Print all new coverage databases after the pytest process has exited."""
    request = Request.unpack(value)
    root = Path.cwd().resolve()
    directory = root / request.directory
    marker = directory / marker_path(request.marker)
    since = marker.stat().st_mtime_ns
    data_files = cast(Dict[str, str], json.loads(marker.read_text()))
    files: Dict[str, List[str]] = {}
    for name in request.coverage_agents or []:
        candidates: Set[Path] = set()
        data_file = Path(data_files[name])
        if not data_file.is_absolute():
            data_file = directory / data_file
        candidates.update(root.rglob(data_file.name + '*'))
        candidates.update(data_file.parent.glob(data_file.name + '*'))
        files[name] = []
        for path in sorted(candidates):
            if path.is_file() and path.stat().st_mtime_ns >= since:
                content = path.read_bytes()
                if content.startswith(b'SQLite format 3\x00'):
                    files[name].append(base64.b64encode(content).decode('ascii'))
    marker.unlink()
    sys.stdout.write(request.marker + encode({'version': 1, 'root': str(root), 'files': files}) + '\n')


def receive(payload: Dict[str, object], targets: Dict[str, str], root: Path) -> None:
    """Restore and remap coverage databases before the controller reports."""
    remote = payload.get('root')
    files = payload.get('files')
    if not isinstance(remote, str) or not isinstance(files, dict):
        raise WorkerError('isolate did not return coverage data')
    from coverage import CoverageData  # noqa: PLC0415
    from coverage.exceptions import DataError  # noqa: PLC0415
    remote_path = PureWindowsPath(remote) if '\\' in remote or ':' in remote else PurePosixPath(remote)

    def map_path(filename: str) -> str:
        path = type(remote_path)(filename)
        try:
            relative = path.relative_to(remote_path)
        except ValueError:
            return filename
        return str(root.joinpath(*relative.parts))

    for name, target in targets.items():
        records = files.get(name)
        if not isinstance(records, list) or not records:
            raise WorkerError(f'isolate did not return coverage data for {name}')
        for item in cast(List[object], records):
            if not isinstance(item, str):
                raise WorkerError('isolate returned malformed coverage data')
            try:
                content = base64.b64decode(item, validate=True)
            except ValueError as error:
                raise WorkerError('isolate returned malformed coverage data') from error
            if not content.startswith(b'SQLite format 3\x00'):
                raise WorkerError('isolate returned malformed coverage data')
            try:
                with TemporaryDirectory(prefix='throngtest-coverage-') as temporary:
                    source = Path(temporary) / 'data'
                    source.write_bytes(content)
                    source_data = CoverageData(basename=str(source))
                    source_data.read()
                    restored = CoverageData(basename=target, suffix=uuid4().hex)
                    restored.update(source_data, map_path=map_path)
                    restored.write()
            except DataError as error:
                raise WorkerError('isolate returned malformed coverage data') from error
