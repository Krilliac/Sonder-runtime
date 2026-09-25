"""Requests, plans, errors and ports of the bounded build-fix loop.

``BuildFixService`` (``fix_service``) drives the loop over these ports:

* ``SourceEditor``: reads and writes project sources only through the typed
  tool gateway, carrying the fix's grant token (``EditContext``). It never
  opens a file for writing itself. The service journals each edit as a
  ``build-fix`` effect around the editor call (``fix_effects``).
* ``CandidateGenerator``: turns ``RepairEvidence`` into a ``CandidatePatch``;
  it checks residency before anything leaves the machine.
* ``FixStrategyPort``: maps each attempt to the strategy controller.
* ``BuildNavigator`` (optional): extra include/definition context.
* ``PreimageStore``: private originals so an operator can restore after a
  crash; nothing restores automatically.
* ``FixGrantIssuer``: mints the ``BuildFixGrant`` behind the one approval.

Domain build types are referenced for annotations only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol

from ...domain.common.errors import (
    CapacityExceeded,
    Conflict,
    Forbidden,
    IntegrityFailure,
    InvalidInput,
    NotFound,
    SonderError,
)
from ..context import OperationContext
from .grants import BuildFixGrant, BuildFixGrantSpec, sha256_hex

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from ...domain.build.repair import BuildProgress, CandidatePatch, EditScope, RepairEvidence
    from .ports import BuildJobPlan

FIX_SCOPE_REJECTED = "FIX_SCOPE_REJECTED"
RESIDENCY_REFUSED = "RESIDENCY_REFUSED"
RESTORE_CONFLICT = "RESTORE_CONFLICT"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
BUILD_BUSY = "BUILD_BUSY"

MIN_ATTEMPTS, MAX_ATTEMPTS, DEFAULT_ATTEMPTS = 1, 8, 4
MIN_FIX_TIMEOUT_SECONDS = 60
MAX_FIX_TIMEOUT_SECONDS = 14_400
DEFAULT_FIX_TIMEOUT_SECONDS = 3_600
MAX_EDITABLE_GLOBS = 16
MAX_GLOB_CHARS = 128
MAX_RESTORE_FILES = 6
FIX_ACTIONS = ("repair", "inspect", "critic", "switch_model", "rollback", "fail")
BUILD_FIX_ID_RE = re.compile(r"^build-fix-[0-9a-f]{16,32}$")

_ERROR_CLASSES: dict[str, type[SonderError]] = {
    FIX_SCOPE_REJECTED: InvalidInput,
    RESIDENCY_REFUSED: Forbidden,
    RESTORE_CONFLICT: Conflict,
    JOB_NOT_FOUND: NotFound,
    BUILD_BUSY: CapacityExceeded,
}


def fix_error(code: str, message: str) -> SonderError:
    """A taxonomy error carrying one of the stable fix error ``code`` values."""
    error = _ERROR_CLASSES.get(code, InvalidInput)(message)
    error.code = code
    return error


class EditConflict(Conflict):
    """The file was not what the loop expected.

    ``uncertain`` is True when a write may have reached the file (the gateway
    admitted it and the result cannot prove what is on disk): the loop stops
    with UNCERTAIN_SIDE_EFFECT and reverts nothing.
    """

    code = "EDIT_CONFLICT"

    def __init__(self, message: str, *, uncertain: bool = False, rel: str = "") -> None:
        super().__init__(message)
        self.uncertain = bool(uncertain)
        self.rel = rel


class EditRefused(Forbidden):
    """The gateway refused the read or write (policy, grant, guard); nothing changed."""

    code = "EDIT_REFUSED"

    def __init__(self, message: str, *, rel: str = "", policy: bool = True) -> None:
        super().__init__(message)
        self.rel = rel
        self.policy = bool(policy)


class ResidencyRefused(Forbidden):
    """The candidate route may leave the machine and the caller has no cloud consent."""

    code = RESIDENCY_REFUSED


class PreimageIntegrityError(IntegrityFailure):
    code = "PREIMAGE_INTEGRITY"


# ---------------------------------------------------------------------------
# Request and plan


def _text(value: object, name: str, *, limit: int = 1024, required: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise fix_error(FIX_SCOPE_REJECTED, "%s must be bounded text" % name)
    if required and not value.strip():
        raise fix_error(FIX_SCOPE_REJECTED, "%s is required" % name)
    return value


@dataclass(frozen=True, slots=True)
class BuildFixRequest:
    project: str = "."
    build_dir: str = ""
    target: str = ""
    config: str = ""
    platform: str = ""
    focus_file: str = ""
    attempts: int = DEFAULT_ATTEMPTS
    apply: bool = True
    revert_after: bool = False
    editable_globs: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    verify_dependents: bool = False
    allow_network: bool = False

    def __post_init__(self) -> None:
        _text(self.project, "project")
        _text(self.build_dir, "build_dir")
        _text(self.target, "target", limit=128, required=True)
        _text(self.config, "config", limit=64)
        _text(self.platform, "platform", limit=64)
        _text(self.focus_file, "focus_file")
        attempts = self.attempts
        if isinstance(attempts, bool) or not isinstance(attempts, int) \
                or not MIN_ATTEMPTS <= attempts <= MAX_ATTEMPTS:
            raise fix_error(FIX_SCOPE_REJECTED, "attempts must be within %d..%d"
                            % (MIN_ATTEMPTS, MAX_ATTEMPTS))
        for name in ("apply", "revert_after", "verify_dependents", "allow_network"):
            if not isinstance(getattr(self, name), bool):
                raise fix_error(FIX_SCOPE_REJECTED, "%s must be a boolean" % name)
        globs = self.editable_globs
        if isinstance(globs, list):
            globs = tuple(globs)
            object.__setattr__(self, "editable_globs", globs)
        if not isinstance(globs, tuple) or len(globs) > MAX_EDITABLE_GLOBS:
            raise fix_error(FIX_SCOPE_REJECTED, "at most %d editable globs" % MAX_EDITABLE_GLOBS)
        for glob in globs:
            _text(glob, "editable glob", limit=MAX_GLOB_CHARS, required=True)
        timeout = self.timeout_seconds
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)
                                    or not MIN_FIX_TIMEOUT_SECONDS <= timeout <= MAX_FIX_TIMEOUT_SECONDS):
            raise fix_error(FIX_SCOPE_REJECTED, "timeout_seconds must be within %d..%d"
                            % (MIN_FIX_TIMEOUT_SECONDS, MAX_FIX_TIMEOUT_SECONDS))


def clamp_fix_timeout(requested: int | None, operator_max: int) -> int:
    """The fix's wall budget: default 3600, never above the operator's cap."""
    ceiling = max(MIN_FIX_TIMEOUT_SECONDS, min(int(operator_max or DEFAULT_FIX_TIMEOUT_SECONDS),
                                               MAX_FIX_TIMEOUT_SECONDS))
    value = DEFAULT_FIX_TIMEOUT_SECONDS if requested is None else int(requested)
    return max(MIN_FIX_TIMEOUT_SECONDS, min(value, ceiling))


@dataclass(frozen=True)
class BuildFixPlan:
    request: BuildFixRequest
    build_plan: "BuildJobPlan"
    scope: "EditScope"
    attempts: int
    apply: bool
    revert_after: bool
    grant_spec: BuildFixGrantSpec
    plan_digest: str
    timeout_seconds: int
    project_root: str
    project_label: str
    model_digest: str
    template_ids: tuple[str, ...]
    world: str = "host"
    network: str = "advisory_off"
    isolation_truth: str = "unverified"
    notes: tuple[str, ...] = ()

    def scope_summary(self) -> dict:
        scope = self.scope
        return {
            "root": self.project_label,
            "globs": list(getattr(scope, "globs", ()) or ()),
            "excluded": len(getattr(scope, "excluded_rel", ()) or ()),
            "generated": len(getattr(scope, "generated_rel", ()) or ()),
            "digest": self.grant_spec.scope_digest,
        }

    def resolved_command(self) -> dict:
        """The approval-binding view: the build plan plus the fix's own bounds."""
        wire = dict(self.build_plan.resolved_command())
        wire.update({
            "tool": "build_fix",
            "attempts": self.attempts,
            "apply": self.apply,
            "revert_after": self.revert_after,
            "verify_dependents": self.request.verify_dependents,
            "focus_file": self.request.focus_file,
            "timeout_seconds": self.timeout_seconds,
            "scope": self.scope_summary(),
            "grant_spec_digest": self.grant_spec.digest(),
            "plan_digest": self.plan_digest,
        })
        return wire


def fix_plan_digest(build_resolved: Mapping[str, Any], *, request: BuildFixRequest,
                    grant_spec: BuildFixGrantSpec, timeout_seconds: int) -> str:
    """Stable over re-planning the same request: no run tokens, no clock values."""
    return sha256_hex({
        "build": {key: build_resolved.get(key) for key in sorted(build_resolved)},
        "attempts": request.attempts,
        "apply": request.apply,
        "revert_after": request.revert_after,
        "verify_dependents": request.verify_dependents,
        "focus_file": request.focus_file,
        "globs": list(request.editable_globs),
        "timeout_seconds": timeout_seconds,
        "grant_spec": grant_spec.digest(),
    })


# ---------------------------------------------------------------------------
# Editing


@dataclass(frozen=True)
class EditContext:
    """One fix job's edit authority: the caller's context plus its grant token."""

    operation: OperationContext
    project_root: str
    job_id: str = ""
    grant_token: str = ""


@dataclass(frozen=True)
class EditReceipt:
    rel: str
    before: str
    after: str
    receipt_id: str
    effect_intent_id: str = ""
    tool: str = ""


# ---------------------------------------------------------------------------
# Strategy


@dataclass(frozen=True)
class FixDecision:
    """What the loop does next; both progress readings are kept (F11)."""

    action: str
    reason: str = ""
    controller_action: str = ""
    dominance: str = ""
    lexicographic: str = ""

    def __post_init__(self) -> None:
        if self.action not in FIX_ACTIONS:
            raise ValueError("fix decision action must be one of %s" % ", ".join(FIX_ACTIONS))


# ---------------------------------------------------------------------------
# Status


@dataclass(frozen=True)
class BuildFixStatusView:
    job_id: str
    status: str
    attempt: int
    attempts: int
    elapsed_seconds: float
    child_job_id: str = ""
    preimage_label: str = ""
    notes: tuple[str, ...] = field(default=())

    def to_wire(self) -> dict:
        wire = {
            "object": "build_fix_status",
            "job_id": self.job_id,
            "status": self.status,
            "attempt": self.attempt,
            "attempts": self.attempts,
            "elapsed_seconds": round(float(self.elapsed_seconds), 1),
            "next": "call build_fix_result with this job_id to wait for the report",
        }
        if self.child_job_id:
            wire["child_job_id"] = self.child_job_id
        if self.preimage_label:
            wire["preimage_label"] = self.preimage_label
        if self.notes:
            wire["notes"] = list(self.notes)
        return wire


@dataclass(frozen=True)
class PreimageEntry:
    rel: str
    original_sha256: str
    last_written_sha256: str = ""


# ---------------------------------------------------------------------------
# Ports


class SourceEditor(Protocol):
    def read(self, rel: str, ctx: EditContext) -> tuple[str, str]:
        """(text, sha256 of its UTF-8 bytes); raises EditRefused or EditConflict."""

    def replace(self, rel: str, new_text: str, *, expected_sha256: str,
                ctx: EditContext) -> EditReceipt:
        """Write ``new_text`` when the file still hashes to ``expected_sha256``."""


class CandidateGenerator(Protocol):
    def propose(self, evidence: "RepairEvidence", ctx: OperationContext, *,
                route_hint: str = "") -> "CandidatePatch":
        """One candidate patch; raises ResidencyRefused before any model call."""


class FixStrategyPort(Protocol):
    def begin(self, run_id: str, objective_digest: str, *, attempts: int = DEFAULT_ATTEMPTS,
              max_model_calls: int = 12, wall_seconds: float = 3600.0) -> None: ...

    def observe(self, attempt: Any, *, before: "BuildProgress | None",
                after: "BuildProgress | None", failure: str | None,
                hypothesis_digest: str = "", focus: str = "",
                model_calls: int = 0, verifier_calls: int = 0) -> FixDecision: ...


class BuildNavigator(Protocol):
    def context_for(self, file_rel: str, diagnostics: tuple, ctx: OperationContext, *,
                    max_items: int = 8) -> tuple[str, ...]: ...

    def prefetch_diagnostics(self, file_rel: str, ctx: OperationContext) -> tuple: ...

    def close(self) -> None: ...


class PreimageStore(Protocol):
    def begin(self, job_id: str, manifest: Mapping[str, Any]) -> None: ...

    def save(self, job_id: str, rel: str, text: str, sha256: str) -> None: ...

    def record_write(self, job_id: str, rel: str, sha256: str) -> None: ...

    def load(self, job_id: str, rel: str) -> tuple[str, str]: ...

    def list(self, job_id: str) -> tuple[PreimageEntry, ...]: ...

    def manifest(self, job_id: str) -> Mapping[str, Any] | None: ...

    def set_status(self, job_id: str, status: str) -> None: ...

    def jobs(self) -> tuple[str, ...]: ...

    def purge(self, job_id: str) -> None: ...

    def label(self, job_id: str) -> str: ...


class FixGrantIssuer(Protocol):
    def issue(self, spec: BuildFixGrantSpec, *, principal_id: str, job_id: str,
              plan_digest: str) -> BuildFixGrant | None: ...

    def revoke(self, grant_or_job: BuildFixGrant | str | None) -> None: ...


NavigatorFactory = Callable[[Any, OperationContext], "BuildNavigator | None"]


__all__ = [
    "BUILD_BUSY", "BUILD_FIX_ID_RE", "BuildFixPlan", "BuildFixRequest", "BuildFixStatusView",
    "BuildNavigator", "CandidateGenerator", "DEFAULT_ATTEMPTS", "DEFAULT_FIX_TIMEOUT_SECONDS",
    "EditConflict", "EditContext", "EditReceipt", "EditRefused", "FIX_ACTIONS",
    "FIX_SCOPE_REJECTED", "FixDecision", "FixGrantIssuer", "FixStrategyPort", "JOB_NOT_FOUND",
    "MAX_ATTEMPTS", "MAX_FIX_TIMEOUT_SECONDS", "MAX_RESTORE_FILES", "MIN_FIX_TIMEOUT_SECONDS",
    "NavigatorFactory", "PreimageEntry", "PreimageIntegrityError", "PreimageStore",
    "RESIDENCY_REFUSED", "RESTORE_CONFLICT", "ResidencyRefused", "SourceEditor",
    "clamp_fix_timeout", "fix_error", "fix_plan_digest",
]
