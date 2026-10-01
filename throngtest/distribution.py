"""Deterministic partitions, shared by the controller and the workers."""

from difflib import unified_diff
from hashlib import sha256
from itertools import islice
from typing import Dict, List, Sequence


def fingerprint(nodeids: Sequence[str]) -> str:
    # Length prefixes make the encoding unambiguous even for unusual node IDs.
    digest = sha256()
    for nodeid in nodeids:
        value = nodeid.encode('utf-8')
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
    return digest.hexdigest()


def collection_difference(expected: Sequence[str], actual: Sequence[str]) -> str:
    difference = list(islice(unified_diff(expected, actual, fromfile='controller', tofile='isolate', n=2, lineterm=''), 101))
    if len(difference) > 100:
        difference[-1] = '... collection diff truncated after 100 lines'
    return '\n'.join([
        'test collection differs between the controller and the isolate',
        'Possible causes to check:',
        '  * Test files were added, removed, or excluded from the isolate snapshot;',
        '    preparation commands may generate or change files.',
        '  * Parametrization or parameter IDs depend on environment variables, files,',
        '    external data, random values, or the current time.',
        '  * Collection order is unstable, for example parameters come from a set.',
        '  * Test selection differs: check -k, -m, addopts, configuration,',
        '    and installed plugins or dependencies.',
        '  * Absolute paths in parameter IDs change when the project is copied',
        '    to a different directory, even if the tests are otherwise equivalent.',
        '',
        f'controller: {len(expected)} selected tests',
        f'isolate: {len(actual)} selected tests',
        *difference,
    ])


def partition(nodeids: Sequence[str], workers: int, mode: str) -> List[List[int]]:
    if workers < 1:
        raise ValueError('workers must be positive')
    if mode not in ('tests', 'files'):
        raise ValueError('distribution must be tests or files')
    groups: Dict[str, List[int]] = {}
    for index, nodeid in enumerate(nodeids):
        key = nodeid.split('::', 1)[0] if mode == 'files' else str(index)
        groups.setdefault(key, []).append(index)
    shards: List[List[int]] = [[] for _ in range(min(workers, len(groups)))]
    for group in sorted(groups.values(), key=len, reverse=True):
        min(shards, key=len).extend(group)
    return [sorted(shard) for shard in shards]
