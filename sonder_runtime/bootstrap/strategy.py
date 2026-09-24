"""Host-owned composition for observe-only strategy checkpoints.

The seal key lives at a stable state path, separate from the SQLite database.
POSIX ownership and mode are checked. Windows relies on the host state ACL;
POSIX mode bits alone cannot establish equivalent Windows key privacy. A
missing key is created once only for a new database; a lost, malformed or
insecure key is never regenerated because that would orphan sealed checkpoints.
This repository has no effect journal binding, so observations cannot authorize
effect replay or claim that an external effect has been reconciled.
"""
from __future__ import annotations

import hashlib
import logging
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import (
    SQLiteRuntimeCheckpointRepository,
)
from sonder_runtime.application.ports.runtime_checkpoints import CheckpointError
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.platform.paths import state_path

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IsolatedCodegenBuild:
    """Host-owned runner whose process cannot read strategy key or checkpoints.

    Only a concrete lower-privilege build adapter may construct this for
    production. A caller-provided command, option, or environment value is not
    evidence of that filesystem separation.
    """

    run: Callable[..., tuple[str, bool]]


def compose_isolated_codegen_build() -> IsolatedCodegenBuild | None:
    """No supported isolated Codegen build adapter is installed yet."""
    return None


@dataclass(frozen=True, slots=True)
class StrategyRollout:
    """Host startup policy for observation and deterministic canary selection."""

    mode: str = "off"
    canary_percent: int = 0

    def __post_init__(self):
        if self.mode not in {"off", "observe", "shadow", "canary"}:
            raise ValueError("unknown strategy rollout mode")
        if type(self.canary_percent) is not int or not 0 <= self.canary_percent <= 100:
            raise ValueError("strategy canary percent must be between 0 and 100")

    @property
    def observes(self) -> bool:
        return self.mode != "off"

    def selected(self, durable_run_id: str) -> bool:
        if self.mode != "canary" or not durable_run_id:
            return False
        bucket = int(hashlib.sha256(durable_run_id.encode("utf-8")).hexdigest()[:8], 16) % 10_000
        return bucket < self.canary_percent * 100


def configured_strategy_rollout() -> StrategyRollout:
    """Parse rollout settings at bootstrap, with the prior observe switch as alias."""
    mode = os.environ.get("SONDER_STRATEGY_MODE", "").strip().lower()
    if not mode:
        observed = os.environ.get("SONDER_STRATEGY_OBSERVE", "").strip().lower()
        if observed in {"", "0", "false", "off"}:
            mode = "off"
        elif observed in {"1", "true", "on"}:
            mode = "observe"
        else:
            raise ValueError("SONDER_STRATEGY_OBSERVE must be true or false")
    raw_percent = os.environ.get("SONDER_STRATEGY_CANARY_PERCENT", "0").strip() or "0"
    try:
        percent = int(raw_percent)
    except ValueError as error:
        raise ValueError("SONDER_STRATEGY_CANARY_PERCENT must be an integer") from error
    return StrategyRollout(mode, percent)


def try_configured_strategy_rollout() -> StrategyRollout:
    try:
        return configured_strategy_rollout()
    except Exception as error:  # noqa: BLE001 - bad optional rollout cannot widen authority
        _LOG.warning("strategy rollout unavailable: %s", type(error).__name__)
        return StrategyRollout()


def _private_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        directory = path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o077:
            raise CheckpointError("strategy key directory is not private")
        if directory.st_uid != os.getuid():
            raise CheckpointError("strategy key directory has another owner")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            key = os.urandom(32)
            if os.write(descriptor, key) != len(key):
                raise CheckpointError("strategy key write was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, read_flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CheckpointError("strategy key is not a private regular file")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise CheckpointError("strategy key has insecure ownership or permissions")
        key = os.read(descriptor, 33)
        if len(key) != 32:
            raise CheckpointError("strategy key is missing or malformed")
        return key
    finally:
        os.close(descriptor)


def compose_strategy_trace(*, db_path: str | Path | None = None,
                           key_path: str | Path | None = None) -> StrategyTraceService:
    """Compose the sealed observer using host paths and no replay authorization."""
    database = Path(db_path or state_path("strategy/checkpoints.db", "SONDER_STRATEGY_CHECKPOINT_DB"))
    key_file = Path(key_path or state_path("strategy-private/checkpoint.key"))
    if database.resolve() == key_file.resolve():
        raise CheckpointError("strategy key and database paths must be separate")
    if database.exists() and not key_file.exists():
        raise CheckpointError("strategy checkpoint database exists without its seal key")
    key = _private_key(key_file)
    repository = SQLiteRuntimeCheckpointRepository(database, seal_key=key)
    return StrategyTraceService(repository)


def existing_strategy_trace() -> StrategyTraceService | None:
    """Open prior sealed state for effect guards, even when rollout is now off.

    Absence does not create a new key or database. An existing database with a
    missing key is deliberately left to the strict composer to reject.
    """
    database = Path(state_path("strategy/checkpoints.db", "SONDER_STRATEGY_CHECKPOINT_DB"))
    key_file = Path(state_path("strategy-private/checkpoint.key"))
    if not os.path.lexists(database):
        if os.path.lexists(key_file):
            raise CheckpointError("strategy seal key exists without checkpoint database")
        return None
    return compose_strategy_trace(db_path=database)


def configured_strategy_trace(rollout: StrategyRollout | None = None) -> StrategyTraceService | None:
    """Read the rollout switch only at the host composition boundary."""
    selected = rollout or configured_strategy_rollout()
    if not selected.observes:
        return None
    return compose_strategy_trace()


def try_configured_strategy_trace(rollout: StrategyRollout | None = None) -> StrategyTraceService | None:
    """Keep optional observation faults from changing work execution."""
    try:
        return configured_strategy_trace(rollout)
    except Exception as error:  # noqa: BLE001 - optional observation never gates work
        _LOG.warning("strategy observation unavailable: %s", type(error).__name__)
        return None


def try_compose_strategy_memory(trace: StrategyTraceService | None, unit_of_work_provider):
    """Bind optional strategy memory to the canonical application unit of work."""
    if trace is None or unit_of_work_provider is None:
        return None
    try:
        from sonder_runtime.application.memory.strategy_memory import (
            StrategyMemoryService,
        )

        unit_of_work = unit_of_work_provider()
        if not callable(unit_of_work):
            raise TypeError("application unit_of_work must be a factory")
        return StrategyMemoryService(trace, unit_of_work)
    except Exception as error:  # noqa: BLE001 - optional indexing never gates work
        _LOG.warning("strategy memory observation unavailable: %s", type(error).__name__)
        return None


def compose_workbench_strategy_observer(trace, memory, rollout: StrategyRollout, store):
    """Bind lane observations to the durable host store after lane transitions."""
    if trace is None:
        return None
    from sonder_runtime.bootstrap.strategy_observers import observe_workbench_lane
    from sonder_runtime.domain.strategy.models import StrategyAction

    def observe(lane):
        if lane.get("status") not in {"completed", "failed", "awaiting_input"}:
            return
        fresh_attempt = not any(
            item.attempt_id == lane["attempt_id"] for item in trace.history(lane["id"])
        )
        decision = observe_workbench_lane(trace, lane=lane, memory_service=memory)
        if decision is None or not fresh_attempt or rollout.mode not in {"shadow", "canary"}:
            return
        legacy = (
            StrategyAction.RECONCILE if lane.get("pending_effect") or
            lane["status"] == "awaiting_input" else StrategyAction.PAUSE
        )
        with store.transaction() as transaction:
            current = transaction.lane(lane["id"])
            transaction.emit(current, "strategy.shadow", {
                "attempt_id": lane["attempt_id"], "policy_version": decision.policy_version,
                "controller_action": decision.action.value, "legacy_action": legacy.value,
                "match": decision.action is legacy,
                "cohort_selected": rollout.selected(lane["id"]),
                "applied": False, "reason": "manual_resume_requires_host_authority",
            })

    return observe


def compose_fleet_strategy_observer(trace, memory, rollout: StrategyRollout, *,
                                    pure_model: bool, event_sink):
    """Observe fleet calls; a selected pure-model canary may suppress a retry.

    The host proves ``pure_model`` from its worker implementation. This
    observer cannot create a retry, increase the legacy retry limit, or
    authorize a repository worker's effects.
    """
    from sonder_runtime.bootstrap.strategy_observers import observe_fleet_worker
    from sonder_runtime.domain.strategy.models import StrategyAction

    if not rollout.observes:
        return None

    def observe(*, agent_id: str, master_id: str, prompt: str,
                master_digest: str, project_scope: str, attempt_number: int,
                attempt_limit: int, route: str, accepted: bool,
                failure_code: str = "", legacy_retry: bool = False) -> bool:
        selected = rollout.selected(master_id) and pure_model
        scope = project_scope or "fleet:" + master_id
        decision = None
        if trace is not None:
            try:
                decision = observe_fleet_worker(
                    trace, agent_id=agent_id, master_id=master_id,
                    prompt=prompt, master_digest=master_digest,
                    project_scope=scope, attempt_number=attempt_number,
                    attempt_limit=attempt_limit, route=route, accepted=accepted,
                    failure_code=failure_code, effects_resolved=pure_model,
                    transport_replay_safe=pure_model, memory_service=memory,
                )
            except Exception as error:  # noqa: BLE001 - observer fault boundary
                _LOG.warning("fleet strategy observation unavailable: %s", type(error).__name__)
        allow_retry = legacy_retry
        if selected and legacy_retry:
            # Fail closed if the seal or controller is unavailable. Only the
            # existing pure-model retry is eligible, and only the host loop
            # can perform it after its own cancellation and budget checks.
            allow_retry = decision is not None and decision.action is StrategyAction.RETRY_TRANSIENT
        if rollout.mode in {"shadow", "canary"} and decision is not None:
            with suppress(Exception):
                event_sink(
                    agent_id,
                    f"strategy shadow: attempt={attempt_number} "
                    f"legacy={'retry' if legacy_retry else 'stop'} "
                    f"controller={decision.action.value} "
                    f"match={(decision.action is StrategyAction.RETRY_TRANSIENT) == legacy_retry} "
                    f"selected={selected} applied={selected and allow_retry != legacy_retry}",
                )
        return allow_retry

    return observe


__all__ = [
    "StrategyRollout",
    "compose_fleet_strategy_observer",
    "compose_strategy_trace",
    "compose_workbench_strategy_observer",
    "configured_strategy_rollout",
    "configured_strategy_trace",
    "try_compose_strategy_memory",
    "try_configured_strategy_rollout",
    "try_configured_strategy_trace",
]
