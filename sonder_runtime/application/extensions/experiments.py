"""Bounded, ephemeral extension experiments.

Experiments are intentionally not a deployment mechanism.  Definitions live
only in this manager, each experiment receives a private temporary directory,
and the only process transition that can occur is an explicitly authorized
``start``.  There is no persistence, promotion, or configuration-writing API.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class ExperimentLimits:
    """Typed resource limits accepted by the application experiment boundary."""

    memory_limit_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.memory_limit_bytes is not None and (
            isinstance(self.memory_limit_bytes, bool)
            or not isinstance(self.memory_limit_bytes, int)
            or self.memory_limit_bytes <= 0
        ):
            raise ExperimentInvalidDefinition(
                "memory_limit_bytes must be a positive integer when set"
            )


class ExperimentHost(Protocol):
    """Injected child-host boundary; process ownership stays in adapters."""

    @property
    def stats(self) -> Any: ...
    def start(self) -> None: ...
    def close(self) -> None: ...


class ExperimentError(RuntimeError):
    """Base error for the ephemeral experiment lifecycle."""


class ExperimentNotFound(ExperimentError):
    """The requested experiment does not exist."""


class ExperimentInvalidDefinition(ExperimentError, ValueError):
    """An experiment definition exceeds a bounded lifecycle contract."""


class ExperimentInvalidTransition(ExperimentError):
    """The requested operation is not valid for the current state."""


class ExperimentStartupDenied(ExperimentError, PermissionError):
    """The explicit startup authority did not authorize the experiment."""


class ExperimentState:
    DEFINED = "defined"
    RUNNING = "running"
    STOPPED = "stopped"
    DELETED = "deleted"


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_MAX_ARGV = 32
_MAX_ARG_LENGTH = 512
_MAX_ENV_ITEMS = 32
_MAX_TEXT_LENGTH = 512


def _remove_experiment_tree(path: Path) -> None:
    """Remove one stopped experiment after bounded handle-release retries."""
    for attempt in range(8):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(0.02 * (2 ** attempt), 0.2))


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    """The complete bounded input needed to launch one child process."""

    experiment_id: str
    argv: tuple[str, ...]
    description: str = ""
    environment: tuple[tuple[str, str], ...] = ()
    limits: object | None = None

    def __post_init__(self) -> None:
        if not _ID_PATTERN.fullmatch(self.experiment_id):
            raise ExperimentInvalidDefinition("experiment_id must match [a-z][a-z0-9-]{0,31}")
        if not isinstance(self.argv, tuple):
            raise ExperimentInvalidDefinition("argv must be an immutable sequence")
        if not self.argv or len(self.argv) > _MAX_ARGV:
            raise ExperimentInvalidDefinition("argv must contain 1 to 32 entries")
        if any(not isinstance(item, str) or not item or len(item) > _MAX_ARG_LENGTH for item in self.argv):
            raise ExperimentInvalidDefinition("argv entries must be non-empty and at most 512 characters")
        if not isinstance(self.description, str):
            raise ExperimentInvalidDefinition("description must be text")
        if len(self.description) > _MAX_TEXT_LENGTH:
            raise ExperimentInvalidDefinition("description is too long")
        if len(self.environment) > _MAX_ENV_ITEMS:
            raise ExperimentInvalidDefinition("environment is too large")
        keys: set[str] = set()
        for key, value in self.environment:
            if (
                not isinstance(key, str)
                or not key
                or len(key) > _MAX_TEXT_LENGTH
                or not isinstance(value, str)
                or len(value) > _MAX_TEXT_LENGTH
                or key in keys
            ):
                raise ExperimentInvalidDefinition("environment must contain unique bounded string pairs")
            keys.add(key)


@dataclass(frozen=True, slots=True)
class ExperimentSnapshot:
    experiment_id: str
    state: str
    description: str
    starts: int
    stops: int
    stats: Any


@dataclass
class _Experiment:
    definition: ExperimentDefinition
    directory: Path
    state: str = ExperimentState.DEFINED
    host: ExperimentHost | None = None
    starts: int = 0
    stops: int = 0


StartupAuthority = Callable[[ExperimentDefinition], bool]
HostFactory = Callable[[ExperimentDefinition, Path], ExperimentHost]


class EphemeralExperimentManager:
    """Own a finite collection of temporary, explicitly authorized experiments."""

    def __init__(self, startup_authority: StartupAuthority, *, host_factory: HostFactory, temp_root: str | Path | None = None) -> None:
        if not callable(startup_authority):
            raise TypeError("startup_authority must be callable")
        if not callable(host_factory):
            raise TypeError("host_factory must be callable")
        self._authorize = startup_authority
        self._host_factory = host_factory
        self._root = Path(tempfile.mkdtemp(prefix="sonder-experiments-", dir=temp_root))
        self._experiments: dict[str, _Experiment] = {}

    @property
    def root(self) -> Path:
        """Return the private temporary root for inspection by a caller."""
        return self._root

    def define(
        self,
        experiment_id: str,
        argv: Sequence[str],
        *,
        description: str = "",
        environment: Mapping[str, str] | None = None,
        limits: object | None = None,
    ) -> ExperimentSnapshot:
        """Register a definition without starting a process or persisting config."""
        if experiment_id in self._experiments:
            raise ExperimentInvalidDefinition("experiment_id is already defined")
        if isinstance(argv, str):
            raise ExperimentInvalidDefinition("argv must be a sequence of arguments")
        if environment is not None and not isinstance(environment, Mapping):
            raise ExperimentInvalidDefinition("environment must be a mapping")
        definition = ExperimentDefinition(
            experiment_id=experiment_id,
            argv=tuple(argv),
            description=description,
            environment=tuple(sorted((environment or {}).items())),
            limits=limits,
        )
        directory = self._root / experiment_id
        directory.mkdir()
        experiment = _Experiment(definition=definition, directory=directory)
        self._experiments[experiment_id] = experiment
        return self.inspect(experiment_id)

    def inspect(self, experiment_id: str) -> ExperimentSnapshot:
        experiment = self._get(experiment_id)
        return ExperimentSnapshot(
            experiment_id=experiment.definition.experiment_id,
            state=experiment.state,
            description=experiment.definition.description,
            starts=experiment.starts,
            stops=experiment.stops,
            stats=experiment.host.stats if experiment.host is not None else None,
        )

    def snapshot(self) -> tuple[ExperimentSnapshot, ...]:
        """Return bounded operator-visible experiment state in stable order."""
        return tuple(
            self.inspect(experiment_id)
            for experiment_id in sorted(self._experiments)
        )

    def start(self, experiment_id: str) -> ExperimentSnapshot:
        experiment = self._get(experiment_id)
        if experiment.state != ExperimentState.DEFINED:
            raise ExperimentInvalidTransition("only a defined experiment can start")
        if not self._authorize(experiment.definition):
            raise ExperimentStartupDenied("startup authority denied experiment")
        host = self._host_factory(experiment.definition, experiment.directory)
        try:
            host.start()
        except Exception:
            host.close()
            raise
        experiment.host = host
        experiment.state = ExperimentState.RUNNING
        experiment.starts += 1
        return self.inspect(experiment_id)

    def stop(self, experiment_id: str) -> ExperimentSnapshot:
        experiment = self._get(experiment_id)
        if experiment.state != ExperimentState.RUNNING or experiment.host is None:
            raise ExperimentInvalidTransition("only a running experiment can stop")
        experiment.host.close()
        experiment.host = None
        experiment.state = ExperimentState.STOPPED
        experiment.stops += 1
        return self.inspect(experiment_id)

    def delete(self, experiment_id: str) -> ExperimentSnapshot:
        experiment = self._get(experiment_id)
        if experiment.state == ExperimentState.RUNNING:
            raise ExperimentInvalidTransition("stop the experiment before deleting it")
        if experiment.state == ExperimentState.DELETED:
            raise ExperimentInvalidTransition("experiment is already deleted")
        _remove_experiment_tree(experiment.directory)
        experiment.state = ExperimentState.DELETED
        return self.inspect(experiment_id)

    def close(self) -> None:
        """Stop live children and remove all temporary experiment material."""
        for experiment in tuple(self._experiments.values()):
            if experiment.state == ExperimentState.RUNNING and experiment.host is not None:
                experiment.host.close()
                experiment.host = None
                experiment.state = ExperimentState.STOPPED
                experiment.stops += 1
        shutil.rmtree(self._root, ignore_errors=True)

    def __enter__(self) -> "EphemeralExperimentManager":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def _get(self, experiment_id: str) -> _Experiment:
        experiment = self._experiments.get(experiment_id)
        if experiment is None:
            raise ExperimentNotFound(experiment_id)
        return experiment


__all__ = [
    "EphemeralExperimentManager",
    "ExperimentDefinition",
    "ExperimentLimits",
    "ExperimentError",
    "ExperimentInvalidDefinition",
    "ExperimentInvalidTransition",
    "ExperimentNotFound",
    "ExperimentSnapshot",
    "ExperimentStartupDenied",
    "ExperimentState",
    "ExperimentHost", "HostFactory", "StartupAuthority",
]
