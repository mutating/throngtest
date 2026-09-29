# throngtest

Run pytest test subsets in [throng](https://github.com/mutating/throng) isolates.
Throngtest is an independent pytest plugin: it has its own options and does not
depend on pytest-xdist or implement xdist's flags or fixtures.

```bash
pip install throngtest
pytest
pytest --throngtest-distribution=files
pytest --isolates=2 --throngtest-backend=local
```

Installing the plugin enables distribution into up to four isolates by default.
Use `--isolates=0` to disable it and run ordinary pytest.
Python 3.8+ and pytest 8.3.5–9.x are supported.

## Configuration

All throngtest settings are loaded, converted and validated through
[skelet](https://github.com/mutating/skelet). Sources have this precedence:

1. Explicit `--throngtest-*` arguments, including arguments supplied by pytest's
   `addopts` or `PYTEST_ADDOPTS`.
2. Environment variables with the `THRONGTEST_` prefix.
3. `[tool.throngtest]` in `pyproject.toml` at pytest's `rootdir`.
4. Defaults.

```toml
[tool.throngtest]
workers = 4
check_fingerprints = false
backend = "temporary_directory"
distribution = "files"
preparation = ["python scripts/prepare.py", "python scripts/seed_test_data.py"]
```

```bash
THRONGTEST_WORKERS=4 pytest
pytest --isolates=2 --throngtest-exclude='[".git/", ".venv/", "large-data/"]'
```

| Setting | CLI option | Default | Meaning |
| --- | --- | --- | --- |
| `workers` | `--isolates` | `4` | Maximum number of nonempty test subsets; a nonnegative integer. `0` disables distribution. |
| `check_fingerprints` | `--throngtest-check-fingerprints` | `false` | Require identical ordered collections in the controller and isolates. |
| `backend` | `--throngtest-backend` | `temporary_directory` | Name of an installed throng plugin. |
| `distribution` | `--throngtest-distribution` | `tests` | Split individual tests or keep each file together (`files`). |
| `python` | `--throngtest-python` | Controller's `sys.executable` | Python executable available inside each isolate. |
| `exclude` | `--throngtest-exclude` | See below | Throng snapshot exclusion patterns; a JSON array for CLI/environment sources and an array in TOML. |
| `preparation` | `--throngtest-preparation` | `[]` | Ordered list of nonempty commands run once in each isolate before pytest; JSON for CLI/environment sources and an array in TOML. |

The default exclusions are `.git/`, `.venv/`, `venv/`, `__pycache__/`,
`.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `build/`, `dist/`, and `mutants/`.
An explicit exclusion list replaces the defaults. Patterns are interpreted by
throng. Configurations are read afresh for each pytest session.

Fingerprint checks are disabled by default. Enable them with
`--throngtest-check-fingerprints`, `THRONGTEST_CHECK_FINGERPRINTS=true`, or
`check_fingerprints = true` in `[tool.throngtest]`. Use
`--throngtest-no-check-fingerprints` to override an enabled setting from the
environment or TOML. Both CLI flags take no value; if both are supplied, the
last flag wins. The environment accepts `true`/`false`; TOML uses booleans.

## Isolate preparation

Use `preparation` to generate files, install dependencies, or otherwise prepare
each isolate before its pytest process starts:

```bash
pytest --throngtest-preparation='["python scripts/prepare.py"]'
THRONGTEST_PREPARATION='["python scripts/prepare.py"]' pytest
```

Commands run in the listed order through `isolate.run`, in the same isolate as
the tests. With the built-in backends they start at the project root, even when
pytest is invoked from a subdirectory. Each call starts a separate process:
file changes persist, but `cd`, `export`, and shell activation do not carry over
to later commands or pytest. To select a prepared interpreter, set `python` to
its executable path. Command syntax follows the chosen throng backend.

A nonzero command exit code stops preparation of that isolate and prevents its
tests from starting. The controller cancels outstanding work, cleans up the
isolates, and exits with code 3, reporting the failed command, its exit code,
and preparation output. Other isolates may already have started their tests.
Successful preparation output is forwarded with the isolate's test results.

An explicit list replaces the lower-priority list; `[]` disables preparation.
No preparation runs with `workers = 0`, `--collect-only`, or an empty test
selection. The controller collects tests before creating isolates, so its
environment must already support that initial collection. Preparation runs
before collection inside each worker, not before controller collection.

## Coverage

Throngtest includes two coverage agents, for `coverage run -m pytest` and
`pytest --cov`. They are pristan plugins in the `throngtest.coverage` slot.
Only agents whose coverage tool is active participate in a run. Coverage tools
are optional dependencies; install the one you intend to use in the controller
and in every isolate.

```bash
coverage run -m pytest --isolates=4
coverage combine
coverage report -m

pytest --isolates=4 --cov=your_package
```

Each isolate saves its coverage database under a unique name. After its pytest
process exits, throngtest runs another command in the same isolate, serializes
the database into command output, and reconstructs it on the controller. Source
paths inside copied projects are mapped back to the controller's project path.
The transport requires no shared filesystem, including with throng backends
that run on remote machines. With `pytest-cov`, the final report includes the
transferred data automatically. With `coverage run`, combine the data before
reporting, as shown above. A `COVERAGE_FILE` path shared with isolates is not
required.

Third-party agents can register a function returning a `CoverageAgent` subclass
with `@coverage_agents.plugin(unique=True)` and expose that registration module
through a `throngtest.coverage` entry point. Every active registered agent is
started in each isolate and has its coverage data returned to the controller.
An agent implements `active(config)`, `start_worker(marker, configuration,
root)`, and `stop_worker()`. It can override `configuration(root)` to send
JSON-safe settings, `pytest_arguments()` to adjust worker options, and
`data_file()` to name its coverage database. `root` is the project root on
the corresponding machine.

Subprocesses created *inside* a test still need subprocess coverage support
from the chosen coverage tool. On remote machines, its Python environment also
needs the agent package installed.

## Distribution and execution

The controller collects and selects tests using pytest. It partitions the
result into at most `workers` nonempty subsets. In `tests` mode, tests are
assigned in round-robin order. In `files` mode, larger files are assigned first
to the least populated subset, using test counts as the size estimate. Original
collection order is preserved within each subset. Durations are not predicted
and work is not reassigned between subsets.

Each subset gets one isolate from a single throng manager. A service thread
waits for its synchronous command; test functions themselves execute in pytest
inside the isolate. Every worker independently collects and partitions tests.
The number of partitions is limited to the number of isolates actually started.
With `check_fingerprints = false` (the default), the collections are not compared
with the controller, and reports retain the identifiers collected inside each
isolate. Report completeness is still validated against that isolate's assigned
subset, including fail-fast handling. Empty subsets are allowed; if no isolate
executes any tests, the overall run returns pytest's exit code 5.

Without fingerprint checks, differences in collection order or contents between
isolates can cause tests to be omitted or executed more than once. Enable
`check_fingerprints` when you need to reject these differences before execution.
With checks enabled, each worker compares its ordered collection fingerprint
before running tests. A collection mismatch first lists possible causes to check:
file changes or exclusions, preparation, environment-dependent parametrization,
unstable ordering, different selection settings or plugins, and absolute paths
in parameter IDs that change when the project is copied. These are diagnostic
suggestions, not an automatic determination of the cause. It then reports both
selected-test counts and a unified diff of the ordered test identifiers (`-` for the controller, `+` for
the isolate). The diff includes parameter IDs, preserves duplicates, and is
limited to 100 lines with an explicit truncation notice for larger differences.

The built-in backends have different guarantees:

* `temporary_directory` copies the project into a separate temporary directory
  for each isolate and removes it after execution. Commands in different
  isolates can run concurrently.
* `local` executes in the original project directory. Its isolates use separate
  command processes, but throng 0.0.3 serializes commands belonging to one local
  manager. Project file changes are visible to other subsets and remain after
  the run.

Throng determines isolation and available concurrency. Throngtest does not
bypass a manager's synchronization. Without nested xdist, a session fixture runs once **per isolate**;
module and class fixtures may also be instantiated in multiple isolates in
`tests` mode. Use `files` to keep a file's fixtures and tests together.

## Optional cooperation with pytest-xdist

Throngtest does not depend on, install, or enable pytest-xdist. Users who want
both levels of distribution install and configure xdist separately:

```bash
python -m pip install pytest-xdist
pytest --isolates=2 -n 4 --dist=load
```

Throngtest partitions the tests between up to two isolates. Each isolate runs
its preparation once, then starts its own xdist controller with four local
workers. Xdist distributes only that isolate's subset. The example can execute
up to eight tests concurrently with `temporary_directory`; the `local` backend
still serializes isolate commands. Processes belonging to the same isolate
share its prepared filesystem. A session fixture runs once per xdist worker.

`-n`, `--numprocesses`, `--dist`, and other xdist options belong to xdist and
retain its parsing and configuration through CLI, pytest `addopts`, and
`PYTEST_ADDOPTS`. Throngtest adds no xdist settings or aliases. Without an
enabled xdist run, execution stays sequential within each isolate. `-n 0`
disables inner distribution; `--isolates=0` leaves standalone xdist in control.
`-n auto` is resolved independently in each isolate and is not a global process
budget. Xdist's `worker_id` values, such as `gw0`, are local to each isolate.

The supported schedulers are `load`, `loadfile`, `loadscope`, `loadgroup`, and
`worksteal`. Their grouping guarantees apply within each assigned subset.
For example, use `--throngtest-distribution=files --dist=loadfile` to keep a
file together at both levels. Xdist groups do not combine tests from different
isolates. `--dist=each` is rejected because it intentionally repeats tests.
Explicit `--tx`/`--px` execution environments and `--looponfail` are unsupported;
inner workers must be local to the isolate and configured with `-n`.

Xdist still checks collection consistency between its own workers even when
throngtest's `check_fingerprints` is disabled. Throngtest accepts interleaved
results and validates completed executions, including duplicate node IDs,
fail-fast stops, and xdist worker crashes. Xdist's worker restart settings are
respected, and crash reports reach the combined terminal and JUnit output.
Xdist's usual limitations also apply, including its lack of live stdout
forwarding with `-s`. Captured output on failures is retained.

## Results and failures

Workers return JSON reports through the command's stdout. No shared report
directory, network listener, or pickle transport is required. Third-party
throng backends must provide stdout and a process return code. The selected
interpreter must have throngtest, pytest, the project's dependencies, and
required pytest plugins available by the end of preparation. Throngtest only
installs packages when explicitly instructed through preparation commands.
For a remote backend, set `--throngtest-python` to an interpreter available in
the isolate, such as `python`; its default is the controller's absolute
`sys.executable` path, which is usually absent on a remote machine.

The controller replays pytest reports, preserving assertion explanations,
captured output, skip/xfail/xpass results, setup/teardown failures, durations,
and test properties. `--junitxml` produces one aggregate report. Runtime warnings
are forwarded with their original category name in the message. `-s` output is
forwarded after its subset completes. Reports are buffered per subset; there is
no live per-test progress from a running isolate.

`-k`, `-m`, explicit node IDs, parametrization, conftest fixtures, `--collect-only`,
and `--continue-on-collection-errors` are supported. Empty collections retain
pytest's exit code 5. Isolate process crashes, missing interpreters, collection mismatches
with fingerprint checks enabled, and incomplete report payloads fail with exit code 3. A reported
worker interrupt propagates exit code 2.

`-x` and `--maxfail` apply within each worker and to the aggregate reports. Once
the controller observes the limit, it cancels outstanding commands through
throng's cancellation token and cleans up isolates. Other subsets may already
have executed additional tests. Cancellation responsiveness depends on the
backend; throng's isolate creation itself does not accept a cancellation token.

The plugin rejects interactive `--pdb`, cache-based selection (`--lf`, `--ff`, `--nf`), and
`--stepwise`. Other plugins must be importable in each worker, for example via
installation, `-p`, or conftest; programmatically injected plugin objects cannot
be transported. Plugins that implement custom test protocols, reruns, or their
own output artifacts need separate compatibility work. The built-in throng
backends reuse the interpreter environment; temporary file copies are not
container or virtual-environment isolation.

## Development

```bash
python -m pip install -r requirements_dev.txt -e .
python -m pytest --isolates=0
ruff check throngtest tests
mypy --strict --disallow-any-decorated --disallow-any-explicit \
  --disallow-any-expr --disallow-any-generics --disallow-any-unimported \
  --disallow-subclassing-any --warn-return-any throngtest
mypy tests
```

The outer test session disables distribution; integration tests launch their
own pytest sessions to exercise throngtest, including its default settings.
The test suite includes actual subprocess runs through both built-in throng
plugins, trace checks of their isolate APIs, complete/disjoint distribution,
concurrency barriers, configuration precedence, preparation, native reports, cancellation,
cleanup, and fault injection at the protocol/backend boundaries. The existing
CI also checks statement and branch coverage across Python and OS versions.
The CI workflow uses a startup hook to measure its own local subprocesses.
Integration tests also verify that the coverage agents transfer data from
temporary isolates, including when pytest-xdist runs inside them.
