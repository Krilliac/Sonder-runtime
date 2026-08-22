"""Durable approve/edit/reject envelopes for human-in-the-loop actions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any


_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARGUMENT_BYTES = 256 * 1024


class ApprovalError(ValueError):
    """Raised when an approval request or decision is invalid."""


class ApprovalDecisionKind(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


def _required_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ApprovalError(f"{name} is invalid")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ApprovalError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _canonical(value: object, name: str) -> tuple[Mapping[str, Any], str]:
    if not isinstance(value, Mapping):
        raise ApprovalError(f"{name} must be an object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        raw = encoded.encode("utf-8")
        if len(raw) > _MAX_ARGUMENT_BYTES:
            raise ApprovalError(f"{name} exceeds the argument size limit")
        # Round-trip rejects non-JSON values while also detaching caller state.
        copied = json.loads(encoded)
    except ApprovalError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApprovalError(f"{name} must be JSON-serializable") from exc
    return MappingProxyType(copied), hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    action: str
    arguments_sha256: str
    allowed_decisions: frozenset[ApprovalDecisionKind] = frozenset({
        ApprovalDecisionKind.APPROVE,
        ApprovalDecisionKind.EDIT,
        ApprovalDecisionKind.REJECT,
    })
    expires_at: str | None = None

    def __post_init__(self) -> None:
        _required_id(self.approval_id, "approval_id")
        _required_id(self.action, "action")
        _digest(self.arguments_sha256, "arguments_sha256")
        decisions = frozenset(self.allowed_decisions)
        if not decisions or not decisions.issubset(frozenset(ApprovalDecisionKind)):
            raise ApprovalError("allowed_decisions is invalid")
        object.__setattr__(self, "allowed_decisions", decisions)
        if self.expires_at is not None:
            _parse_time(self.expires_at)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ApprovalError("now must be timezone-aware")
        return current >= _parse_time(self.expires_at)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approval_id: str
    decision: ApprovalDecisionKind
    actor: str
    reason: str = ""
    edited_arguments: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _required_id(self.approval_id, "approval_id")
        try:
            kind = self.decision if isinstance(self.decision, ApprovalDecisionKind) else ApprovalDecisionKind(self.decision)
        except (TypeError, ValueError) as exc:
            raise ApprovalError("decision must be approve, edit, or reject") from exc
        object.__setattr__(self, "decision", kind)
        _required_id(self.actor, "actor")
        if not isinstance(self.reason, str) or len(self.reason) > 4096:
            raise ApprovalError("reason must be bounded text")
        if kind is ApprovalDecisionKind.EDIT:
            copied, _ = _canonical(self.edited_arguments, "edited_arguments")
            object.__setattr__(self, "edited_arguments", copied)
        elif self.edited_arguments is not None:
            raise ApprovalError("edited_arguments is allowed only for edit")

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "decision": self.decision.value,
            "actor": self.actor,
            "reason": self.reason,
            "edited_arguments": None if self.edited_arguments is None else dict(self.edited_arguments),
        }


@dataclass(frozen=True, slots=True)
class ResolvedApproval:
    approval_id: str
    decision: ApprovalDecisionKind
    accepted: bool
    arguments_sha256: str | None
    arguments: Mapping[str, Any] | None


def resolve_approval(
    request: ApprovalRequest,
    decision: ApprovalDecision,
    *,
    now: datetime | None = None,
) -> ResolvedApproval:
    """Validate and bind one durable decision to its original request."""
    if not isinstance(request, ApprovalRequest) or not isinstance(decision, ApprovalDecision):
        raise ApprovalError("request and decision must use typed approval contracts")
    if request.approval_id != decision.approval_id:
        raise ApprovalError("approval decision does not match request")
    if request.is_expired(now):
        raise ApprovalError("approval request has expired")
    if decision.decision not in request.allowed_decisions:
        raise ApprovalError("approval decision is not allowed for this request")
    if decision.decision is ApprovalDecisionKind.REJECT:
        return ResolvedApproval(request.approval_id, decision.decision, False, None, None)
    if decision.decision is ApprovalDecisionKind.APPROVE:
        return ResolvedApproval(request.approval_id, decision.decision, True, request.arguments_sha256, None)
    arguments, edited_digest = _canonical(decision.edited_arguments, "edited_arguments")
    return ResolvedApproval(request.approval_id, decision.decision, True, edited_digest, arguments)


def _parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ApprovalError("expires_at must be ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalError("expires_at must be ISO-8601 text") from exc
    if parsed.tzinfo is None:
        raise ApprovalError("expires_at must include a timezone")
    return parsed


__all__ = [
    "ApprovalDecision", "ApprovalDecisionKind", "ApprovalError", "ApprovalRequest",
    "ResolvedApproval", "resolve_approval",
]
