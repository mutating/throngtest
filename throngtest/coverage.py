"""Coverage agents discovered through a pristan slot."""

import os
from abc import ABC, abstractmethod
from importlib import import_module
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, cast

import pytest
from pristan import slot

from throngtest.coverage_transport import worker_data_file

if TYPE_CHECKING:  # pragma: no cover - this import only exists for static analysis.
    from coverage import Coverage


class CoverageAgent(ABC):
    """Adapt one coverage provider to the isolate lifecycle."""

    @abstractmethod
    def active(self, config: pytest.Config) -> bool:
        ...  # pragma: no cover - abstract signature.

    @abstractmethod
    def start_worker(self, marker: str, configuration: Dict[str, object], root: Path) -> None:
        ...  # pragma: no cover - abstract signature.

    def configuration(self, _root: Path) -> Dict[str, object]:
        """Return JSON-safe settings needed by this agent in an isolate."""
        return {}

    def pytest_arguments(self) -> List[str]:
        """Override provider options that must be evaluated by the controller."""
        return []

    @abstractmethod
    def stop_worker(self) -> None:
        ...  # pragma: no cover - abstract signature.

    def data_file(self) -> str:
        """Return the provider's configured coverage data file in this process."""
        from coverage import Coverage  # noqa: PLC0415
        current = Coverage.current()
        return str(current.config.data_file if current is not None else Coverage().config.data_file)


@slot(entrypoint_group='throngtest.coverage')
def coverage_agents() -> Dict[str, CoverageAgent]:  # type: ignore[empty-body]
    ...  # pragma: no cover - pristan replaces the slot body.


class PythonCoverageAgent(CoverageAgent):
    """Start coverage.py in a worker created by ``coverage run``."""

    def __init__(self) -> None:
        self.started: Optional['Coverage'] = None
        self.config_path: Optional[Path] = None

    def active(self, config: pytest.Config) -> bool:
        try:
            from coverage import Coverage  # noqa: PLC0415
        except ImportError:
            return False
        return Coverage.current() is not None and not bool(cast(Optional[List[str]], config.getoption('cov_source', default=None)))

    def configuration(self, root: Path) -> Dict[str, object]:
        from coverage import Coverage  # noqa: PLC0415
        current = Coverage.current()
        assert current is not None
        filename = current.config.config_file
        path = Path(filename) if filename else None
        return {
            'suffix': path.suffix if path else '',
            'text': path.read_text() if path and path.is_file() else '',
            'source': current.config.source,
            'branch': current.config.branch,
            'root': str(root),
        }

    def start_worker(self, marker: str, configuration: Dict[str, object], root: Path) -> None:
        from coverage import Coverage  # noqa: PLC0415
        if Coverage.current() is None:
            os.environ['COVERAGE_FILE'] = worker_data_file(marker)
            contents = cast(str, configuration.get('text', ''))
            if contents:
                suffix = cast(str, configuration.get('suffix', ''))
                self.config_path = Path('.throngtest-coverage-config-' + marker.rstrip(':') + suffix)
                self.config_path.write_text(contents)
            original_source = cast(Optional[List[str]], configuration.get('source'))
            source = original_source
            controller_root = cast(str, configuration.get('root', ''))
            if controller_root and original_source:
                controller_path = PureWindowsPath(controller_root) if '\\' in controller_root or ':' in controller_root else PurePosixPath(controller_root)

                def map_source(value: str) -> str:
                    try:
                        relative = type(controller_path)(value).relative_to(controller_path)
                    except ValueError:
                        return value
                    return str(root.joinpath(*relative.parts))

                source = [map_source(value) for value in original_source]
            self.started = Coverage(
                config_file=str(self.config_path) if self.config_path else True,
                source=source,
                branch=cast(bool, configuration.get('branch')),
            )
            self.started.start()

    def stop_worker(self) -> None:
        if self.started is not None:
            self.started.stop()
            self.started.save()
        if self.config_path is not None:
            self.config_path.unlink()


class PytestCovAgent(CoverageAgent):
    """Use pytest-cov's own worker lifecycle and collect its saved data."""

    def __init__(self) -> None:
        self.worker_file: Optional[str] = None

    def active(self, config: pytest.Config) -> bool:
        return bool(cast(Optional[List[str]], config.getoption('cov_source', default=None))) and not cast(bool, config.getoption('no_cov', default=False))

    def start_worker(self, marker: str, _configuration: Dict[str, object], _root: Path) -> None:
        # pytest-cov 6 and earlier starts a subprocess tracer before this
        # module runs. Stop it before selecting a per-isolate data file.
        try:
            cleanup = cast(Callable[[], None], import_module('pytest_cov.embed').cleanup)
        except (ImportError, AttributeError):
            pass
        else:
            cleanup()
        self.worker_file = worker_data_file(marker)
        os.environ['COVERAGE_FILE'] = self.worker_file

    def stop_worker(self) -> None:
        pass

    def data_file(self) -> str:
        return self.worker_file or super().data_file()

    def pytest_arguments(self) -> List[str]:
        return ['--cov-fail-under=0']


@coverage_agents.plugin(unique=True)
def python_coverage() -> CoverageAgent:
    return PythonCoverageAgent()


@coverage_agents.plugin(unique=True)
def pytest_cov() -> CoverageAgent:
    return PytestCovAgent()
