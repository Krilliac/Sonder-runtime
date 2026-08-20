"""Immutable, resource-aware tool policy (TOOL-003/004/005).

The evaluator is deliberately an application contract.  It performs no
filesystem, DNS, network, process, or persistence I/O.  A rule can narrow a
request by every security-relevant dimension, and every evaluation produces a
truthful immutable receipt.  Startup authorities are copied into an immutable
snapshot; there is no runtime setter or mutation path for them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import fnmatch
import hashlib
import json
import posixpath
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping


class PolicyInputError(ValueError):
    """Raised when a policy or resource request is malformed."""


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    SESSION_GRANT = "session_grant"
    PROJECT_GRANT = "project_grant"
    SANDBOX_ONLY = "sandbox_only"
    ATTENDED_ONLY = "attended_only"


_CONDITIONAL = {Decision.SANDBOX_ONLY, Decision.ATTENDED_ONLY}
_GRANTS = {Decision.ALLOW, Decision.ALLOW_ONCE, Decision.SESSION_GRANT, Decision.PROJECT_GRANT}
_FIELDS = (
    "tool", "operation", "path", "host", "resource", "agent_preset",
    "workspace", "origin", "side_effect_class", "persistence", "secret_exposure",
)


def _text(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PolicyInputError(f"{name} must be a string")
    return value.strip()


def _path(value: str) -> str:
    if not value:
        return ""
    # Policy paths use a stable POSIX representation even on Windows.
    candidate = value.replace("\\", "/")
    drive = re.match(r"^[A-Za-z]:", candidate)
    prefix = candidate[:2] if drive else ""
    rest = candidate[2:] if drive else candidate
    normalized = posixpath.normpath(rest)
    if normalized == ".":
        normalized = ""
    return (prefix + normalized).lower()


def _host(value: str) -> str:
    return value.lower().rstrip(".")


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """All resource and security dimensions relevant to one tool decision."""

    request_id: str
    tool: str
    operation: str = ""
    path: str = ""
    host: str = ""
    resource: str = ""
    agent_preset: str = ""
    workspace: str = ""
    origin: str = ""
    side_effect_class: str = ""
    persistence: str = ""
    secret_exposure: str = ""
    sandboxed: bool = False
    attended: bool = False
    authority: str = ""

    def __post_init__(self) -> None:
        if not _text(self.request_id, "request_id") or not _text(self.tool, "tool"):
            raise PolicyInputError("request_id and tool are required")
        object.__setattr__(self, "request_id", self.request_id.strip())
        object.__setattr__(self, "tool", self.tool.strip())
        for name in _FIELDS:
            value = _text(getattr(self, name), name)
            if name in {"path", "workspace"}:
                value = _path(value)
            elif name == "host":
                value = _host(value)
            object.__setattr__(self, name, value)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any], *, request_id: str = "policy-request") -> "ResourceRequest":
        if not isinstance(values, Mapping):
            raise PolicyInputError("resource request must be a mapping")
        known = {key: values[key] for key in (*_FIELDS, "sandboxed", "attended", "authority") if key in values}
        known.setdefault("tool", "")
        known.setdefault("request_id", request_id)
        return cls(**known)


@dataclass(frozen=True, slots=True)
class StartupAuthoritySnapshot:
    """Bootstrap-only, independently captured unrestricted capabilities."""

    unrestricted_tools: bool = False
    unrestricted_selfmod: bool = False
    captured_at: str = ""
    source: str = "bootstrap"
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.unrestricted_tools, bool) or not isinstance(self.unrestricted_selfmod, bool):
            raise PolicyInputError("startup authorities must be booleans")
        captured = self.captured_at.strip() or datetime.now(timezone.utc).isoformat()
        source = self.source.strip()
        if not source:
            raise PolicyInputError("authority source is required")
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(self, "source", source)
        payload = json.dumps({"unrestricted_tools": self.unrestricted_tools,
                              "unrestricted_selfmod": self.unrestricted_selfmod,
                              "captured_at": captured, "source": source}, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "digest", hashlib.sha256(payload.encode()).hexdigest())

    @classmethod
    def capture(cls, *, unrestricted_tools: bool = False, unrestricted_selfmod: bool = False,
                captured_at: str = "", source: str = "bootstrap") -> "StartupAuthoritySnapshot":
        return cls(unrestricted_tools, unrestricted_selfmod, captured_at, source)

    def permits(self, authority: str) -> bool:
        if authority == "unrestricted_tools":
            return self.unrestricted_tools
        if authority == "unrestricted_selfmod":
            return self.unrestricted_selfmod
        return False


@dataclass(frozen=True, slots=True)
class PolicyRule:
    rule_id: str
    decision: Decision
    tool: str = ""
    operation: str = ""
    path: str = ""
    host: str = ""
    resource: str = ""
    agent_preset: str = ""
    workspace: str = ""
    origin: str = ""
    side_effect_class: str = ""
    persistence: str = ""
    secret_exposure: str = ""
    required_authority: str = ""
    priority: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        if not _text(self.rule_id, "rule_id"):
            raise PolicyInputError("rule_id is required")
        if not isinstance(self.decision, Decision):
            try:
                object.__setattr__(self, "decision", Decision(self.decision))
            except ValueError as exc:
                raise PolicyInputError("invalid policy decision") from exc
        for name in _FIELDS:
            value = _text(getattr(self, name), name)
            if name in {"path", "workspace"}:
                value = _path(value)
            elif name == "host":
                value = _host(value)
            object.__setattr__(self, name, value)
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise PolicyInputError("priority must be an integer")

    def matches(self, request: ResourceRequest) -> bool:
        for name in _FIELDS:
            pattern = getattr(self, name)
            if pattern and not _matches(name, getattr(request, name), pattern):
                return False
        return True


@dataclass(frozen=True, slots=True)
class PolicyReceipt:
    request_id: str
    decision: Decision
    allowed: bool
    approval_required: bool
    matched_rule_id: str
    reason: str
    resource: Mapping[str, str]
    authority_digest: str
    authority: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource", MappingProxyType(dict(self.resource)))


@dataclass(frozen=True, slots=True)
class PolicyResult:
    receipt: PolicyReceipt

    @property
    def decision(self) -> Decision:
        return self.receipt.decision

    @property
    def allowed(self) -> bool:
        return self.receipt.allowed

    @property
    def approval_required(self) -> bool:
        return self.receipt.approval_required


class ResourcePolicy:
    """Evaluate immutable rules against immutable startup authority."""

    def __init__(self, rules: Iterable[PolicyRule] = (), *, authorities: StartupAuthoritySnapshot | None = None):
        self._rules = tuple(sorted(tuple(rules), key=lambda rule: (-rule.priority, rule.rule_id)))
        if any(not isinstance(rule, PolicyRule) for rule in self._rules):
            raise PolicyInputError("rules must contain PolicyRule values")
        self._authorities = authorities or StartupAuthoritySnapshot.capture()

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        return self._rules

    @property
    def authorities(self) -> StartupAuthoritySnapshot:
        return self._authorities

    def evaluate(self, request: ResourceRequest) -> PolicyResult:
        if not isinstance(request, ResourceRequest):
            raise PolicyInputError("evaluate requires ResourceRequest")
        rule = next((candidate for candidate in self._rules if candidate.matches(request)), None)
        if rule is None:
            decision, rule_id, reason = Decision.DENY, "default-deny", "no policy rule matched"
        elif rule.required_authority and not self._authorities.permits(rule.required_authority):
            decision, rule_id, reason = Decision.DENY, rule.rule_id, "required startup authority is not enabled"
        elif request.authority and not self._authorities.permits(request.authority):
            decision, rule_id, reason = Decision.DENY, rule.rule_id, "requested authority is not enabled at startup"
        else:
            decision, rule_id, reason = rule.decision, rule.rule_id, rule.reason or "matched policy rule"
        allowed = decision in _GRANTS or (decision is Decision.SANDBOX_ONLY and request.sandboxed) or (decision is Decision.ATTENDED_ONLY and request.attended)
        receipt = PolicyReceipt(
            request_id=request.request_id,
            decision=decision,
            allowed=allowed,
            approval_required=decision in {Decision.ASK, Decision.ATTENDED_ONLY} or (decision is Decision.SANDBOX_ONLY and not request.sandboxed),
            matched_rule_id=rule_id,
            reason=reason,
            resource={name: getattr(request, name) for name in _FIELDS if getattr(request, name)},
            authority_digest=self._authorities.digest,
            authority=request.authority,
        )
        return PolicyResult(receipt)

    def with_rule(self, rule: PolicyRule) -> "ResourcePolicy":
        """Return a new policy; the existing policy and authorities never mutate."""
        return ResourcePolicy((*self._rules, rule), authorities=self._authorities)


def _matches(name: str, value: str, pattern: str) -> bool:
    if name == "host":
        if pattern.startswith("*."):
            return value != pattern[2:] and value.endswith("." + pattern[2:])
        return value == pattern
    if name in {"path", "workspace"}:
        if pattern.endswith("/**"):
            root = pattern[:-3].rstrip("/")
            return value == root or value.startswith(root + "/")
        return value == pattern
    return fnmatch.fnmatchcase(value, pattern)


__all__ = [
    "Decision", "PolicyInputError", "PolicyReceipt", "PolicyResult", "PolicyRule",
    "ResourcePolicy", "ResourceRequest", "StartupAuthoritySnapshot",
]
