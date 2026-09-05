"""Immutable child-control mutations; identities describe writes, never authority."""

from dataclasses import dataclass, asdict
import hashlib
import json
import uuid
from typing import Literal

from .subagents import SubagentStatus, InvalidSubagentRequest

MAX_MUTATION_BYTES = 2_000_000
KINDS = frozenset(
    {"create", "save_checkpoint", "update", "claim_resume", "request_cancel"}
)


def canonical(value):
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(data) > MAX_MUTATION_BYTES:
        raise InvalidSubagentRequest("continuation mutation payload exceeds bound")
    return data


@dataclass(frozen=True)
class PreparedContinuationMutation:
    kind: Literal[
        "create", "save_checkpoint", "update", "claim_resume", "request_cancel"
    ]
    child_id: str
    operation_id: str
    payload: bytes
    request_sha256: str

    def __post_init__(self):
        if (
            self.kind not in KINDS
            or not isinstance(self.child_id, str)
            or not self.child_id.strip()
        ):
            raise InvalidSubagentRequest("invalid continuation mutation identity")
        if (
            not isinstance(self.operation_id, str)
            or not 1 <= len(self.operation_id) <= 160
        ):
            raise InvalidSubagentRequest("invalid operation identity")
        if (
            not isinstance(self.payload, bytes)
            or not 1 <= len(self.payload) <= MAX_MUTATION_BYTES
        ):
            raise InvalidSubagentRequest("invalid continuation mutation payload")
        value = json.loads(self.payload)
        if not isinstance(value, dict) or canonical(value) != self.payload:
            raise InvalidSubagentRequest("mutation must contain canonical arguments")
        expected = hashlib.sha256(
            canonical([self.kind, self.child_id, value])
        ).hexdigest()
        if self.request_sha256 != expected:
            raise InvalidSubagentRequest("mutation identity digest mismatch")


def prepare_call(kind, *args, operation_id=None, **kwargs):
    if kind not in KINDS or len(args) != 1:
        raise InvalidSubagentRequest("invalid continuation operation")
    value = args[0]
    if kind == "create":
        from .continuation_records import DurableChildSession

        if not isinstance(value, DurableChildSession):
            raise InvalidSubagentRequest("child session required")
        child_id = value.request.child_id
        payload = {"session": asdict(value)}
    elif kind == "save_checkpoint":
        from ..subagents.continuable import ContinuableCheckpoint

        if not isinstance(value, ContinuableCheckpoint):
            raise InvalidSubagentRequest("checkpoint required")
        child_id = value.child_id
        payload = {"checkpoint": asdict(value)}
    else:
        child_id = value
        payload = {}
    payload.update(kwargs)
    allowed = {
        "create": {"session"},
        "save_checkpoint": {"checkpoint", "expected_sequence"},
        "update": {
            "status",
            "expected_revision",
            "usage",
            "result",
            "recovery_required",
        },
        "claim_resume": {"expected_revision"},
        "request_cancel": {"reason"},
    }[kind]
    required = {
        "create": {"session"},
        "save_checkpoint": {"checkpoint", "expected_sequence"},
        "update": {"status"},
        "claim_resume": {"expected_revision"},
        "request_cancel": {"reason"},
    }[kind]
    if set(payload) - allowed or not required.issubset(payload):
        raise InvalidSubagentRequest("invalid mutation arguments")
    for name, minimum in (("expected_sequence", -1), ("expected_revision", 0)):
        if name in payload and payload[name] is not None:
            if type(payload[name]) is not int or payload[name] < minimum:
                raise InvalidSubagentRequest(
                    "invalid mutation compare-and-set revision"
                )
    required_revision = {
        "claim_resume": "expected_revision",
        "save_checkpoint": "expected_sequence",
    }.get(kind)
    if required_revision is not None and payload[required_revision] is None:
        raise InvalidSubagentRequest("compare-and-set revision required")
    if (
        payload.get("recovery_required") is not None
        and type(payload["recovery_required"]) is not bool
    ):
        raise InvalidSubagentRequest("invalid recovery flag")
    if kind == "request_cancel" and not isinstance(payload["reason"], str):
        raise InvalidSubagentRequest("cancellation reason must be a string")
    if kind == "update":
        payload["status"] = SubagentStatus(payload["status"]).value
        for name in ("usage", "result"):
            if payload.get(name) is not None:
                payload[name] = asdict(payload[name])
    encoded = canonical(payload)
    return PreparedContinuationMutation(
        kind,
        child_id,
        "cmut-" + uuid.uuid4().hex if operation_id is None else operation_id,
        encoded,
        hashlib.sha256(canonical([kind, child_id, payload])).hexdigest(),
    )


class ContinuationStorageFailure(RuntimeError):
    """Stop execution; storage failure is not a retryable runner failure."""


class ContinuationCommitAmbiguous(ContinuationStorageFailure):
    def __init__(self, prepared):
        self.prepared = prepared
        super().__init__(
            "continuation storage outcome requires reconciliation: "
            + prepared.operation_id
        )


class ContinuationReceiptCapacity(ContinuationStorageFailure):
    pass


class ContinuationCleanupRequired(ContinuationStorageFailure):
    """Storage may be settled; the old execution owner's cleanup is unproven."""

    def __init__(self, child_id: str):
        self.child_id = child_id
        super().__init__("running child requires proved owner cleanup: " + child_id)


@dataclass(frozen=True)
class ContinuationMutationOutcome:
    disposition: Literal["applied", "precondition_failed", "no_change", "invalid"]
    result_bytes: bytes
    resulting_revision: int | None
    replayed: bool = False
    storage_acknowledgement: Literal["local_committed", "pair_committed"] = "local_committed"

    def __post_init__(self):
        if self.disposition not in {
            "applied",
            "precondition_failed",
            "no_change",
            "invalid",
        }:
            raise ValueError("invalid continuation mutation disposition")
        if (
            not isinstance(self.result_bytes, bytes)
            or not 1 <= len(self.result_bytes) <= MAX_MUTATION_BYTES
        ):
            raise ValueError("invalid continuation result bytes")
        if self.resulting_revision is not None and (
            type(self.resulting_revision) is not int or self.resulting_revision < 0
        ):
            raise ValueError("invalid continuation result revision")
        if (
            self.storage_acknowledgement not in ("local_committed", "pair_committed")
            or type(self.replayed) is not bool
        ):
            raise ValueError("invalid continuation acknowledgement")

    @property
    def value(self):
        from ..subagents.continuation_codec import session_from_data

        value = json.loads(self.result_bytes)
        if self.disposition == "invalid":
            raise InvalidSubagentRequest(value["error"])
        return session_from_data(value) if isinstance(value, dict) else value
