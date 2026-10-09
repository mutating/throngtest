<details>
  <summary>ⓘ</summary>

[![Downloads](https://static.pepy.tech/badge/throngtest/month)](https://pepy.tech/project/throngtest)
[![Downloads](https://static.pepy.tech/badge/throngtest)](https://pepy.tech/project/throngtest)
[![Coverage Status](https://coveralls.io/repos/github/mutating/throngtest/badge.svg?branch=main)](https://coveralls.io/github/mutating/throngtest?branch=main)
[![Lines of code](https://sloc.xyz/github/mutating/throngtest/?category=code)](https://github.com/boyter/scc/)
[![Hits-of-Code](https://hitsofcode.com/github/mutating/throngtest?branch=main)](https://hitsofcode.com/github/mutating/throngtest/view?branch=main)
[![Test-Package](https://github.com/mutating/throngtest/actions/workflows/tests_and_coverage.yml/badge.svg)](https://github.com/mutating/throngtest/actions/workflows/tests_and_coverage.yml)
[![Python versions](https://img.shields.io/pypi/pyversions/throngtest.svg)](https://pypi.python.org/pypi/throngtest)
[![PyPI version](https://badge.fury.io/py/throngtest.svg)](https://badge.fury.io/py/throngtest)
[![Checked with mypy](http://www.mypy-lang.org/static/mypy_badge.svg)](http://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/mutating/throngtest)

</details>

![logo](https://raw.githubusercontent.com/mutating/throngtest/develop/docs/assets/logo_2.svg)


Are your tests taking too long to run? Are your agent workflows slowing down because the agent is simply waiting for your tests to finish? If so, you need `throngtest` — a tool for running tests in a distributed manner.

The key feature is that tests can be run in a distributed and isolated manner on a wide variety of infrastructure — ranging from your own PC to a distributed network of computers or a computer cluster. The [`throng`](https://github.com/mutating/throng) technology is used for distribution, which allows different test runs to be isolated from one another and executed in parallel, hiding the specifics of a particular platform’s implementation from the control code.

`throngtest` is a `pytest` plugin that creates `throng` isolates and runs sharded test-running commands within them.


## Table of contents

- [**Quick start**](#quick-start)
- [**Configuration**](#configuration)


## Quick start

Simply install [`throngtest`](https://pypi.org/project/throngtest) using the following command:

```bash
$ pip install throngtest
```

Now `throngtest` will be used automatically whenever you run `pytest`. You don't need to enable anything else; just type the command `pytest` into the console, and you'll see that the test was run using throngtest:

```console
$ pytest
============================= test session starts ==============================
platform darwin -- Python 3.14.3, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/pomponchik/Desktop/Projects/pytest-throng/test-project
configfile: pytest.ini
testpaths: tests
plugins: throngtest-0.0.3
throngtest: 4 isolates, backend=temporary_directory, distribution=tests
collected 6 items

tests/test_calculator.py ......                                          [100%]

============================== 6 passed in 0.37s ===============================
```

This test run took place entirely on your computer, but the tests themselves were executed in parallel across several temporary copies of the directory in which the original `pytest` command was run. However, the extent of the parallelism and the actual number of worker processes depend on the settings, which you can read about below.


## Configuration





Run pytest test subsets in [throng](https://github.com/mutating/throng) isolates.
Throngtest is an independent pytest plugin: it has its own options and does not
depend on pytest-xdist or implement xdist's flags or fixtures.

```bash
pip install throngtest
pytest
pytest --distribution=files
pytest --isolates=2 --backend=local
```

Installing the plugin enables distribution into up to four isolates by default.
Use `--isolates=0` to disable it and run ordinary pytest.
Python 3.8+ and pytest 8.3.5–9.x are supported. Throng 0.0.9 or newer is required.

-------------------------

All throngtest settings are loaded, converted and validated through
[skelet](https://github.com/mutating/skelet). Sources have this precedence:

1. Explicit CLI options listed below, including arguments supplied by pytest's
   `addopts` or `PYTEST_ADDOPTS`.
2. Environment variables with the `THRONGTEST_` prefix.
3. `[tool.throngtest]` in `pyproject.toml` at pytest's `rootdir`.
4. Defaults.

```toml
[tool.throngtest]
isolates = 4
check_fingerprints = false
backend = "temporary_directory"
distribution = "files"
packages = []
preparation = ["python scripts/prepare.py", "python scripts/seed_test_data.py"]
```

```bash
THRONGTEST_ISOLATES=4 pytest
pytest --isolates=2 --exclude='[".git/", ".venv/", "large-data/"]'
```

| Setting | CLI option | Default | Meaning |
| --- | --- | --- | --- |
| `isolates` | `--isolates` | `4` | Maximum number of nonempty test subsets; a nonnegative integer. `0` disables distribution. |
| `check_fingerprints` | `--check-fingerprints` | `false` | Require identical ordered collections in the controller and isolates. |
| `backend` | `--backend` | `temporary_directory` | Name of an installed throng plugin. |
| `distribution` | `--distribution` | `tests` | Split individual tests or keep each file together (`files`). |
| `python` | `--python` | Controller's `sys.executable` | Python executable available inside each isolate. |
| `exclude` | `--exclude` | See below | Throng snapshot exclusion patterns; a JSON array for CLI/environment sources and an array in TOML. |
| `preparation` | `--preparation` | `[]` | Ordered list of nonempty commands run once in each isolate before pytest; JSON for CLI/environment sources and an array in TOML. |
| `packages` | `--packages` | `[]` | List of nonempty package specifications installed by the backend in each isolate before preparation; JSON for CLI/environment sources and an array in TOML. |

The default exclusions are `.git/`, `.venv/`, `venv/`, `__pycache__/`,
`.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `build/`, and `dist/`.
An explicit exclusion list replaces the defaults. Patterns are interpreted by
throng. Configurations are read afresh for each pytest session.

Fingerprint checks are disabled by default. Enable them with
`--check-fingerprints`, `THRONGTEST_CHECK_FINGERPRINTS=true`, or
`check_fingerprints = true` in `[tool.throngtest]`. Use
`--no-check-fingerprints` to override an enabled setting from the
environment or TOML. Both CLI flags take no value; if both are supplied, the
last flag wins. The environment accepts `true`/`false`; TOML uses booleans.

## Isolate preparation

Use `packages` to ask the backend to install dependencies before running
preparation commands and collecting tests inside each isolate:

```bash
pytest --packages='["requests"]'
THRONGTEST_PACKAGES='["requests"]' pytest
```

The equivalent TOML setting is `packages = ["requests"]` in
`[tool.throngtest]`. Each isolate receives the complete list once, in order.
An explicit list replaces the lower-priority list; `[]` disables installation.
No installation runs with `isolates = 0`, `--collect-only`, or an empty test
selection. The controller must already have the dependencies needed for its
initial collection; packages are installed only in the isolates.

Package syntax and the installation environment belong to the selected backend.
The built-in backends run `pip install <specification>` using `pip` from `PATH`;
`--python` selects the test interpreter and does **not** select that installer.
These backends share the existing Python environment, so installation may
modify it, and temporary-directory cleanup does not uninstall packages.
To install into a specific interpreter, use an explicit preparation command
such as `path/to/python -m pip install requests` and select that interpreter
with `--python`. Successful installer output is managed by the backend;
installation failures include available return codes, stdout, and stderr in
the diagnostic, stop that isolate's preparation, and trigger cleanup and
cancellation of outstanding work.

Use `preparation` to generate files, install dependencies, or otherwise prepare
each isolate before its pytest process starts:

```bash
pytest --preparation='["python scripts/prepare.py"]'
THRONGTEST_PREPARATION='["python scripts/prepare.py"]' pytest
```

Commands run after package installation, in the listed order through
`isolate.run`, in the same isolate as the tests. With the built-in backends
they start at the project root, even when
pytest is invoked from a subdirectory. Each call starts a separate process:
file changes persist, but `cd`, `export`, and shell activation do not carry over
to later commands or pytest. To select a prepared interpreter, set `python` to
its executable path. Command syntax follows the chosen throng backend.

A failed command or a nonzero/missing exit code stops preparation of that
isolate and prevents its tests from starting. The controller cancels outstanding work, cleans up the
isolates, and exits with code 3, reporting the failed command, its exit code,
and preparation output. Other isolates may already have started their tests.
Successful preparation output is forwarded with the isolate's test results.
Throngtest retains individual command results to preserve this output; it does
not forward its `preparation` setting to the manager's `prepare` parameter,
which discards successful command results.

An explicit list replaces the lower-priority list; `[]` disables preparation.
No preparation runs with `isolates = 0`, `--collect-only`, or an empty test
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
result into at most `isolates` nonempty subsets. In `tests` mode, tests are
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
  command processes, but throng serializes commands belonging to one local
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
For example, use `--distribution=files --dist=loadfile` to keep a
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
required pytest plugins available by the end of preparation. Throngtest installs
packages only when explicitly configured through `packages` or preparation commands.
For a remote backend, set `--python` to an interpreter available in
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
the controller observes the limit, it cancels outstanding work through
throng's cancellation token and cleans up isolates. The same token is passed
to isolate creation, package installation, preparation, test execution, and
coverage export. Backend errors and controller interrupts also cancel pending
work. Other subsets may already have executed additional tests. Responsiveness
depends on the backend: snapshot reading has no cancellation token, and a
backend may not interrupt allocation or lock waits immediately.

The plugin rejects interactive `--pdb`, cache-based selection (`--lf`, `--ff`, `--nf`), and
`--stepwise`. Other plugins must be importable in each worker, for example via
installation, `-p`, or conftest; programmatically injected plugin objects cannot
be transported. Plugins that implement custom test protocols, reruns, or their
own output artifacts need separate compatibility work. The built-in throng
backends reuse the interpreter environment; temporary file copies are not
container or virtual-environment isolation.

## Backend API

Third-party backends must support throng 0.0.9. Factories should accept `path`,
`exclude`, `prepare`, and `packages`, forwarding them to `AbstractManager`.
Throngtest supplies the absolute project root as `path`, plus `exclude` and
`packages`; the controller keeps its original working directory throughout
execution. A backend must use the supplied path rather than the controller's cwd.

Implement `AbstractManager._get(state, token=...)`,
`AbstractIsolate._run(command, token=...)`, and
`AbstractIsolate.install(*packages, token=...)`. Inherit the public `get()` and
`run()` wrappers so throng handles package installation, native preparation,
failure policies, and cleanup when initialization fails. Backends remain
responsible for releasing partially allocated resources if `_get()` raises.
Observe the supplied cancellation token during blocking operations.

Preparation uses `run(..., exception=True)`. Test execution keeps the default
`exception=False`, because pytest exit code 1 contains ordinary test failures
and valid reports. The runner preserves a single scope for installation,
preparation, pytest, and subsequent coverage export. Backend exceptions retain
their cause chain and available command results in the final diagnostic.

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
concurrency barriers, configuration precedence, package installation, preparation,
native reports, cancellation during allocation and setup, interrupts,
cleanup, and fault injection at the protocol/backend boundaries. The existing
CI also checks statement and branch coverage across Python and OS versions.
The CI workflow uses a startup hook to measure its own local subprocesses.
Integration tests also verify that the coverage agents transfer data from
temporary isolates, including when pytest-xdist runs inside them.
