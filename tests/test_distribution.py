import pytest

from throngtest.distribution import collection_difference, fingerprint, partition


@pytest.mark.parametrize('mode', ['tests', 'files'])
@pytest.mark.parametrize('workers', [1, 2, 3, 20])
def test_partition_is_complete_and_disjoint(mode: str, workers: int) -> None:
    """Assign every collected occurrence exactly once with stable ordering.

    Repeated node IDs must remain distinct by collection index. Requesting more
    workers than items also checks that partitioning never creates empty shards.
    """
    nodes = ['a.py::one', 'a.py::two', 'b.py::three', 'c.py::four', 'a.py::two']
    shards = partition(nodes, workers, mode)
    assert sorted(index for shard in shards for index in shard) == list(range(len(nodes)))
    assert all(shard == sorted(shard) for shard in shards)
    assert all(shards)
    assert len(shards) <= workers
    assert shards == partition(nodes, workers, mode)
    if mode == 'files':
        assert any(all(index in shard for index in (0, 1, 4)) for shard in shards)


def test_balanced_partitions() -> None:
    """Balance shards by tests per file or round-robin assignment, including empty input."""
    assert partition(['a::1', 'a::2', 'a::3', 'b::1', 'c::1', 'c::2'], 2, 'files') == [[0, 1, 2], [3, 4, 5]]
    assert partition(['a', 'b', 'c', 'd', 'e'], 2, 'tests') == [[0, 2, 4], [1, 3]]
    assert partition([], 3, 'tests') == []


@pytest.mark.parametrize(('workers', 'mode', 'message'), [(0, 'tests', 'positive'), (-1, 'files', 'positive'), (1, 'bad', 'distribution')])
def test_invalid_partition(workers: int, mode: str, message: str) -> None:
    """Reject nonpositive worker counts and unknown distribution modes."""
    with pytest.raises(ValueError, match=message):
        partition(['a'], workers, mode)


def test_fingerprint_includes_order_duplicates_and_boundaries() -> None:
    """Distinguish collection order, repeated IDs, and identifier boundaries in hashes."""
    values = [[], ['a'], ['a', 'a'], ['a', 'b'], ['b', 'a'], ['ab'], ['a\nb'], ['a', '\nb'], ['тест']]
    assert len({fingerprint(value) for value in values}) == len(values)
    assert fingerprint(['a', 'b']) == fingerprint(['a', 'b'])


@pytest.mark.parametrize(('expected', 'actual', 'changes'), [
    (['a'], ['a', 'b'], ['+b']),
    (['a', 'b'], ['a'], ['-b']),
    (['a', 'b'], ['b', 'a'], ['+b', '-b']),
    (['a', 'a'], ['a'], ['-a']),
    ([], ['a'], ['+a']),
    (['a'], [], ['-a']),
    (['test.py::test_item[/project/файл]'], ['test.py::test_item[/temporary/файл]'], ['-test.py::test_item[/project/файл]', '+test.py::test_item[/temporary/файл]']),
])
def test_collection_difference_preserves_identifiers_order_and_duplicates(expected: list, actual: list, changes: list) -> None:
    """Show exact collection changes, including ordering, duplicate IDs, and Unicode paths."""
    diagnostic = collection_difference(expected, actual)
    assert f'controller: {len(expected)} selected tests' in diagnostic
    assert f'isolate: {len(actual)} selected tests' in diagnostic
    assert '--- controller\n+++ isolate\n@@ ' in diagnostic
    lines = diagnostic.splitlines()
    assert [line for line in lines if line.startswith(('+', '-')) and line not in ('--- controller', '+++ isolate')] == changes


def test_collection_difference_explains_possible_causes_before_the_diff() -> None:
    """Place troubleshooting hints before collection counts and the detailed diff."""
    diagnostic = collection_difference(['test.py::old'], ['test.py::new'])
    hints, details = diagnostic.split('\ncontroller:', 1)
    assert hints.startswith('test collection differs between the controller and the isolate\nPossible causes to check:\n')
    for cause in (
        'Test files were added, removed, or excluded',
        'preparation commands may generate or change files',
        'environment variables, files',
        'external data, random values, or the current time',
        'Collection order is unstable',
        'parameters come from a set',
        'check -k, -m, addopts, configuration',
        'installed plugins or dependencies',
        'Absolute paths in parameter IDs change',
        'even if the tests are otherwise equivalent',
    ):
        assert cause in hints
    assert hints.count('  * ') == 5
    assert details.startswith(' 1 selected tests\nisolate: 1 selected tests\n--- controller\n+++ isolate\n')


def test_large_collection_difference_is_bounded() -> None:
    """Truncate a large collection diff while retaining counts and troubleshooting hints."""
    expected = [f'test.py::old[{index}]' for index in range(1000)]
    actual = [f'test.py::new[{index}]' for index in range(1000)]
    lines = collection_difference(expected, actual).splitlines()
    diff_start = lines.index('--- controller')
    assert lines[diff_start - 2:diff_start] == ['controller: 1000 selected tests', 'isolate: 1000 selected tests']
    assert 'Possible causes to check:' in lines[:diff_start]
    assert any('Absolute paths in parameter IDs change' in line for line in lines[:diff_start])
    assert len(lines[diff_start:]) == 101
    assert lines[-1] == '... collection diff truncated after 100 lines'
