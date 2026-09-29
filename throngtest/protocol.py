"""A versioned, JSON-only protocol carried by throng's command output."""

import base64
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, cast


class WorkerError(Exception):
    """An isolate failed to produce a complete, trustworthy pytest result."""

    def __init__(self, message: str, exitcode: int = 3) -> None:
        super().__init__(message)
        self.exitcode = exitcode


def encode(data: Dict[str, object]) -> str:
    return base64.b64encode(json.dumps(data, ensure_ascii=True).encode('ascii')).decode('ascii')


def decode(value: str) -> Dict[str, object]:
    result = cast(object, json.loads(base64.b64decode(value, validate=True)))
    if not isinstance(result, dict):
        raise ValueError('expected a JSON object')
    return cast(Dict[str, object], result)


@dataclass
class Request:
    arguments: List[str]
    fingerprint: Optional[str]
    workers: int
    distribution: str
    shard: int
    marker: str
    directory: str

    def pack(self) -> str:
        return encode(cast(Dict[str, object], vars(self)))

    @classmethod
    def unpack(cls, value: str) -> 'Request':
        data = decode(value)
        return cls(**data)  # type: ignore[arg-type]


def read_response(stdout: str, marker: str) -> Dict[str, object]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(marker):
            try:
                response = decode(line[len(marker):])
            except ValueError as error:
                raise WorkerError(f'invalid worker response: {error}') from error
            if response.get('version') != 1:
                raise WorkerError('unsupported worker protocol version')
            return response
    raise WorkerError('worker terminated without a result')
