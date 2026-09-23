"""Durable worker launch and terminal evidence contract."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol


class WorkerRegistryError(ValueError):
    pass


class DuplicateWorkerError(WorkerRegistryError):
    """An active worker already owns the stable resume/idempotency key."""


class WorkerStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


ACTIVE_WORKER_STATUSES = frozenset((WorkerStatus.QUEUED, WorkerStatus.RUNNING))


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkerRegistryError(f"{name} must be a mapping")
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise WorkerRegistryError(f"{name} keys must be non-empty text")
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerRegistryError(f"{name} must be non-empty text")
    return value.strip()


class WorkerContextPolicy(str, Enum):
    """How much parent context a worker is allowed to start from.

    ``UNSPECIFIED`` preserves legacy launches that never declared a policy and
    therefore may not carry context inputs or an inherited-context digest.
    ``INHERIT`` binds the child to one exact parent-context digest, ``SCOPED``
    requires an explicit, digest-pinned input list, and ``CLEAN`` forbids both.
    """

    UNSPECIFIED = "unspecified"
    INHERIT = "inherit"
    SCOPED = "scoped"
    CLEAN = "clean"


_MAX_CONTRACT_ITEMS = 64


def _sha256_hex(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise WorkerRegistryError(f"{name} must be a lowercase sha256 hex digest")
    return value


def _owned_path(value: str) -> str:
    text = _text(value, "owned file").replace("\\", "/")
    if len(text) > 512:
        raise WorkerRegistryError("owned file exceeds its bound")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise WorkerRegistryError("owned file must not traverse with '..'")
    if not parts:
        raise WorkerRegistryError("owned file must name a path")
    return ("/" if text.startswith("/") else "") + "/".join(parts)


def owned_paths_overlap(left: str, right: str) -> bool:
    """Return true when one owned path equals or contains the other.

    Comparison is case-insensitive so a case-only spelling difference on a
    case-insensitive filesystem fails closed as an overlap.
    """
    a, b = left.casefold(), right.casefold()
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


@dataclass(frozen=True, slots=True)
class WorkerContextInput:
    """One explicit, content-pinned context input handed to a worker."""

    reference: str
    sha256: str

    def __post_init__(self) -> None:
        reference = _text(self.reference, "context input reference")
        if len(reference) > 512:
            raise WorkerRegistryError("context input reference exceeds its bound")
        object.__setattr__(self, "reference", reference)
        object.__setattr__(self, "sha256", _sha256_hex(self.sha256, "context input sha256"))


@dataclass(frozen=True, slots=True)
class WorkerExecutionContract:
    """Typed, durable proof requirements for one worker terminal result.

    ``owned_files`` are the paths this worker exclusively mutates; they are
    distinct from ``task_scope`` (the logical task identity it owns) and from
    ``WorkerLaunch.scope`` (the workspace roots it may read).
    """

    success_criteria: tuple[str, ...] = ()
    verification_commands: tuple[tuple[str, ...], ...] = ()
    context_policy: WorkerContextPolicy = WorkerContextPolicy.UNSPECIFIED
    context_inputs: tuple[WorkerContextInput, ...] = ()
    inherited_context_sha256: str = ""
    owned_files: tuple[str, ...] = ()
    task_scope: str = ""
    speculative_lane: bool = False

    def __post_init__(self) -> None:
        criteria = tuple(sorted({_text(value, "success criterion") for value in self.success_criteria}))
        if any(len(value) > 512 for value in criteria):
            raise WorkerRegistryError("success criterion exceeds its bound")
        commands: list[tuple[str, ...]] = []
        for command in self.verification_commands:
            argv = tuple(_text(value, "verification command argument") for value in command)
            if not argv:
                raise WorkerRegistryError("verification commands must contain argv")
            if any(len(value) > 512 for value in argv):
                raise WorkerRegistryError("verification command argument exceeds its bound")
            commands.append(argv)
        object.__setattr__(self, "success_criteria", criteria)
        object.__setattr__(self, "verification_commands", tuple(commands))
        try:
            policy = WorkerContextPolicy(self.context_policy)
        except ValueError as exc:
            raise WorkerRegistryError("context_policy must be inherit, scoped, or clean") from exc
        object.__setattr__(self, "context_policy", policy)
        inputs: dict[str, WorkerContextInput] = {}
        for item in self.context_inputs:
            if not isinstance(item, WorkerContextInput):
                raise WorkerRegistryError("context inputs must be WorkerContextInput values")
            prior = inputs.get(item.reference)
            if prior is not None and prior.sha256 != item.sha256:
                raise WorkerRegistryError("context input reference is pinned to conflicting digests")
            inputs[item.reference] = item
        ordered_inputs = tuple(inputs[key] for key in sorted(inputs))
        inherited = self.inherited_context_sha256
        if inherited:
            inherited = _sha256_hex(inherited, "inherited_context_sha256")
        if policy is WorkerContextPolicy.INHERIT and not inherited:
            raise WorkerRegistryError("inherit context policy requires inherited_context_sha256")
        if policy is not WorkerContextPolicy.INHERIT and inherited:
            raise WorkerRegistryError("only inherit context policy may carry an inherited-context digest")
        if policy is WorkerContextPolicy.SCOPED and not ordered_inputs:
            raise WorkerRegistryError("scoped context policy requires explicit context inputs")
        if policy in (WorkerContextPolicy.CLEAN, WorkerContextPolicy.UNSPECIFIED) and ordered_inputs:
            raise WorkerRegistryError(f"{policy.value} context policy may not carry context inputs")
        owned = tuple(sorted({_owned_path(value) for value in self.owned_files}))
        task_scope = self.task_scope
        if not isinstance(task_scope, str):
            raise WorkerRegistryError("task_scope must be text")
        task_scope = task_scope.strip()
        if len(task_scope) > 512:
            raise WorkerRegistryError("task_scope exceeds its bound")
        if type(self.speculative_lane) is not bool:
            raise WorkerRegistryError("speculative_lane must be a boolean")
        if self.speculative_lane and owned:
            raise WorkerRegistryError("speculative lanes may not own files")
        for name, values in (
            ("success criteria", criteria), ("verification commands", commands),
            ("context inputs", ordered_inputs), ("owned files", owned),
        ):
            if len(values) > _MAX_CONTRACT_ITEMS:
                raise WorkerRegistryError(f"{name} exceed the contract bound")
        object.__setattr__(self, "context_inputs", ordered_inputs)
        object.__setattr__(self, "inherited_context_sha256", inherited)
        object.__setattr__(self, "owned_files", owned)
        object.__setattr__(self, "task_scope", task_scope)

    @property
    def requested(self) -> bool:
        """Whether any field differs from the empty legacy contract."""
        return self != WorkerExecutionContract()

    def conflicts_with(self, other: "WorkerExecutionContract") -> str:
        """Return why two active contracts cannot run together, or ``""``."""
        for mine in self.owned_files:
            for theirs in other.owned_files:
                if owned_paths_overlap(mine, theirs):
                    return f"owned file {mine!r} overlaps active worker ownership {theirs!r}"
        if (
            self.task_scope
            and self.task_scope == other.task_scope
            and not (self.speculative_lane and other.speculative_lane)
        ):
            return f"task scope {self.task_scope!r} is already owned by an active worker"
        return ""


@dataclass(frozen=True, slots=True)
class WorkerLaunch:
    worker_id: str
    parent_id: str
    role: str
    model: str
    backend: str
    effort: str
    scope: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    budgets: Mapping[str, Any]
    retry_policy: Mapping[str, Any]
    resume_key: str
    idempotency_key: str
    # The prompt is retained only by the single authoritative child-session
    # store when this contract is composed over that store.  The standalone
    # registry keeps it optional for backwards compatibility with its original
    # metadata-only callers.
    prompt: str = ""
    owner_id: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    execution_contract: WorkerExecutionContract = WorkerExecutionContract()

    def __post_init__(self) -> None:
        for name in ("worker_id", "parent_id", "role", "model", "backend", "effort", "resume_key", "idempotency_key"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
            if len(getattr(self, name)) > 256:
                raise WorkerRegistryError(f"{name} exceeds its bound")
        for name in ("prompt", "owner_id"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise WorkerRegistryError(f"{name} must be text")
            if len(value) > 16 * 1024:
                raise WorkerRegistryError(f"{name} exceeds its bound")
        metadata = tuple(self.metadata)
        if any(
            not isinstance(item, tuple) or len(item) != 2
            or not isinstance(item[0], str) or not item[0].strip()
            or not isinstance(item[1], str)
            for item in metadata
        ) or len(dict(metadata)) != len(metadata):
            raise WorkerRegistryError("metadata must contain unique string pairs")
        object.__setattr__(self, "metadata", metadata)
        for name in ("scope", "allowed_tools"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise WorkerRegistryError(f"{name} values must be non-empty text")
            if any(len(value) > 512 for value in values):
                raise WorkerRegistryError(f"{name} value exceeds its bound")
            object.__setattr__(self, name, tuple(sorted(set(values))))
        object.__setattr__(self, "budgets", _mapping(self.budgets, "budgets"))
        object.__setattr__(self, "retry_policy", _mapping(self.retry_policy, "retry_policy"))
        if not isinstance(self.execution_contract, WorkerExecutionContract):
            raise WorkerRegistryError("execution_contract must be WorkerExecutionContract")
        max_attempts = self.retry_policy.get("max_attempts", 1)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 16:
            raise WorkerRegistryError("retry_policy.max_attempts must be between 1 and 16")


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    launch: WorkerLaunch
    status: WorkerStatus
    progress: Mapping[str, Any] = MappingProxyType({})
    terminal_verification: Mapping[str, Any] = MappingProxyType({})
    error: str = ""
    revision: int = 0
    attempt_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "progress", _mapping(self.progress, "progress"))
        object.__setattr__(self, "terminal_verification", _mapping(self.terminal_verification, "terminal_verification"))
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int) or self.attempt_count < 0:
            raise WorkerRegistryError("attempt_count must be non-negative")


class WorkerRegistry(Protocol):
    def admit(self, launch: WorkerLaunch) -> WorkerRecord: ...
    def start(self, worker_id: str, *, expected_revision: int) -> WorkerRecord | None: ...
    def progress(self, worker_id: str, progress: Mapping[str, Any], *, expected_revision: int) -> WorkerRecord | None: ...
    def finish(self, worker_id: str, *, status: WorkerStatus, verification: Mapping[str, Any], error: str = "", expected_revision: int) -> WorkerRecord | None: ...
    def get(self, worker_id: str) -> WorkerRecord | None: ...


__all__ = [
    "ACTIVE_WORKER_STATUSES", "DuplicateWorkerError", "WorkerContextInput", "WorkerContextPolicy",
    "WorkerExecutionContract", "WorkerLaunch", "WorkerRecord", "WorkerRegistry", "WorkerRegistryError",
    "WorkerStatus", "owned_paths_overlap",
]
