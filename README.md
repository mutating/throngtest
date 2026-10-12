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
- [**How it works?**](#how-it-works)
- [**Configuration**](#configuration)


## Quick start

Simply install [`throngtest`](https://pypi.org/project/throngtest) using the following command:

```bash
pip install throngtest
```

Now `throngtest` will be used automatically whenever you run `pytest`. You don't need to enable anything else; just type the command `pytest` into the console, and you'll see that the test was run using throngtest:

```console
pytest
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


## How it works?

`throngtest` is a pytest plugin based on the [`throng`](https://github.com/mutating/throng) system. To understand why it's cool, you need to understand why `throng` is cool.

`throng` allows you to execute arbitrary terminal commands in various execution environments (referred to as “isolates” in `throng` terminology) that are connected as plugins. The core code that issues commands to the execution environments “knows” nothing about how a specific environment is actually structured, which makes it easy to swap them out without modifying that code. Your code can easily run on your computer, making the most of its processing power, and when you need more—you can simply connect a plugin that will allow you to run the same logic within a computing cluster or a network of multiple computers. 

`throngtest`, being based on `throng`, allows you to use any `throng`-compatible plugins to execute your code. Simply install the appropriate plugin or use one of the built-in ones, and your tests will run on the appropriate infrastructure.

The basic workflow of `throngtest` looks like this:

- First, you need to integrate with `pytest` as an intermediate execution layer, receiving a list of tests from `pytest` and returning a list of execution results to `pytest`. `pytest` provides the appropriate entry points for this.
- Next, you need to create a “snapshot” of your environment’s state, which typically includes a set of your project’s source code files, so that you can use it to create isolated execution environments (isolates) in which commands can then be executed independently.
- Next, based on the snapshot, you need to create n independent “isolates,” that is, the final executors of your tests. The `throngtest` logic does not know what a specific isolate is; instead, it relies on the currently connected and selected plugin, which provides `throngtest` with a basic API for interacting with it.
- Each isolate receives an independent test execution command that specifies which subset of tests the particular isolate should run.
- Tests in each isolate are executed independently and then aggregated at the `throngtest` level, after which they are provided to `pytest` in such a way that `pytest` has absolutely no idea how they were obtained.

You can set the number of isolates to which the work is distributed in the settings; the default value is `4`. You can also use the settings to control which plugin you authorize to actually execute your commands.

The built-in plugin, which is enabled by default and where your commands are typically executed, is called `temporary_directory`. It’s quite simple:

- Commands are actually executed on the same computer from which you run the `pytest` command.
- Each isolate is essentially a temporary directory where all your code is copied from the main directory; the test commands are run there, and then the temporary directory is deleted.

The overhead associated with distributing work is usually not zero. As a rule, each plugin: reads the state of your system and working directory, packaging all of this into a special byte artifact; then prepares the resources on which the test command will actually be executed—for example, by creating virtual machine instances on remote computers; and then additional time is spent passing both the byte artifact and subsequent commands to the system responsible for the actual execution of the command. `throng` logically isolates all these stages from the control code, but it does not eliminate the overhead associated with distributed code execution if that is required by a specific plugin. What does this mean for us?

`throngtest` [is not a magic solution](https://en.wikipedia.org/wiki/No_Silver_Bullet) for speeding up your tests. Your tests will only run faster if the benefits of parallelism outweigh the overhead. Typically, this “break-even point” occurs when your tests without parallelization take at least a few minutes to run. It’s impossible to say for certain, since both the tests and the hardware can vary, so it’s best to rely on actual measurements.

For small test suites, you’ll likely be able to find more efficient parallelization tools, such as pytest-xdist. These may utilize more efficient local execution and load-balancing mechanisms. 

However, in some cases, it makes sense to give preference to throngtest even if your tests slow down slightly “here and now”. The fact is that the so-called “[Jevons paradox](https://en.wikipedia.org/wiki/Jevons_paradox)” is quite common in software development. If a resource becomes sufficiently “cheap”, demand for it rises more sharply than its price falls. How does this work in the context of testing? You’ll likely write fewer tests than you could if you knew they would take a very long time to run. Fast tests don’t just save resources—they [encourage you to write more tests](https://medium.com/%40oluwasayo/youre-not-meta-build-fast-teams-with-fast-tests-20b47694db52). The strength of throngtest lies in the fact that, although some plugins have operational overhead, they are capable of running your tests in parallel on a very large scale, across a large number of independent computers. The system overhead in this case can be practically fixed, and the available computing power can be so great that you can write and run more tests than ever before, while still maintaining acceptable execution times. Your projects will no longer have an “upper limit” on the number of tests—beyond which each new test makes your software harder to maintain. `throngtest` was created precisely to eliminate this upper limit.


## Configuration

There are several important settings that you can configure when running throngtest, if necessary:

| Setting | CLI option | Default | Meaning |
| --- | --- | --- | --- |
| `isolates` | `--isolates` | `4` | Maximum number of nonempty test subsets; a nonnegative integer. `0` disables distribution. |
| `check_fingerprints` | `--check-fingerprints` | `false` | Require identical ordered collections in the controller and isolates. |
| `backend` | `--backend` | `temporary_directory` | Name of an installed throng plugin. |
| `distribution` | `--distribution` | `tests` | Split individual tests or keep each file together (`files`). |
| `python` | `--python` | Controller's `sys.executable` | Python executable available inside each isolate. |
| `exclude` | `--exclude` | `.git/`, `.venv/`, `venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `build/`, and `dist/` | Throng snapshot exclusion patterns; a JSON array for CLI/environment sources and an array in TOML. |
| `preparation` | `--preparation` | `[]` | Ordered list of nonempty commands run once in each isolate before pytest; JSON for CLI/environment sources and an array in TOML. |
| `packages` | `--packages` | `[]` | List of nonempty package specifications installed by the backend in each isolate before preparation; JSON for CLI/environment sources and an array in TOML. |

As you can see, `throngtest` has default settings. You can run the `pytest` command without any additional arguments, like this:

```bash
pytest
```

... and in that case, the default settings will be applied automatically.

There are several ways to override the default settings:

- Using CLI options.
- Using environment variables, which are written in uppercase with the prefix `THRONGTEST_`.
- By adding parameters to the `[tool.throngtest]` section of the `pyproject.toml` file, located in the project's root directory.

If you set a single parameter in multiple ways, the priority order is as follows: CLI options have the highest priority, followed by environment variables, and then configuration files.

Here's an example of what overriding parameters via the CLI might look like:

```bash
pytest --isolates=2 --exclude='[".git/", ".venv/", "large-data/"]'
```

> ↑ Please note that all parameters that require specifying collections must be specified in [`JSON`](https://en.wikipedia.org/wiki/JSON) format. This applies only to CLI parameters and environment variables. In configuration files, you can use collections in the native format of the specific file.

And here's an example of a section in `pyproject.toml` where some parameters are overridden:

```toml
[tool.throngtest]
isolates = 4
check_fingerprints = false
backend = "temporary_directory"
distribution = "files"
packages = []
preparation = ["python scripts/prepare.py", "python scripts/seed_test_data.py"]
```


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
