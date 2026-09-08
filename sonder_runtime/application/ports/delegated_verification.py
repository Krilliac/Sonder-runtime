"""Immutable host approval and verification verdict values."""

from dataclasses import dataclass, asdict
import hashlib
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class PreparedCheck:
    target: str
    catalog_digest: str
    argv_digest: str
    workspace_root: str
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedVerification:
    verification_id: str
    parent_session_id: str
    principal_id: str
    parent_grant_revision: int
    generation: int
    children: tuple[tuple[str, int, int], ...]
    roots: tuple[str, ...]
    checks: tuple[PreparedCheck, ...]
    context_fingerprint: str
    bundle_digest: str

    def approval_payload(self):
        """A fresh JSON-ready value; no capability token or child message content."""
        return json.loads(canonical(asdict(self)))

    @classmethod
    def from_payload(cls, value):
        value = dict(value)
        value["checks"] = tuple(
            PreparedCheck(**dict(c, argv=tuple(c.get("argv", ()))))
            for c in value["checks"]
        )
        value["children"] = tuple(tuple(c) for c in value["children"])
        value["roots"] = tuple(value["roots"])
        return cls(**value)


@dataclass(frozen=True)
class VerificationVerdict:
    valid: bool
    code: str
    certificate_id: str = ""
    generation: int = 0
    parent_session_id: str = ""
    parent_grant_revision: int = 0
    roots: tuple[str, ...] = ()
    children: tuple[tuple[str, int, int], ...] = ()

    def as_dict(self):
        return asdict(self)


from dataclasses import dataclass, field


@dataclass(frozen=True)
class _PreparedCheckPermit:
    prepared: PreparedVerification
    approval_id: str
    check: PreparedCheck
    call_id: str
    issuer: object = field(repr=False, compare=False)
