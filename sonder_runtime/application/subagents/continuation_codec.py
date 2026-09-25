"""Roundtrip typed continuation snapshots independently of a SQL dialect."""

from ..ports.subagents import (
    InvalidSubagentRequest,
    SubagentRequest,
    SubagentBudget,
    SubagentStatus,
    SubagentUsage,
    SubagentResult,
    SubagentError,
)
from .continuable import CheckpointProvenance, ContinuableCheckpoint

_PROVENANCE_FIELDS = frozenset(CheckpointProvenance.__dataclass_fields__)
_CHECKPOINT_FIELDS = frozenset(ContinuableCheckpoint.__dataclass_fields__)


def provenance_from_data(value):
    """Decode host-stamped provenance; ``None`` marks a provenance-absent row."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _PROVENANCE_FIELDS:
        raise InvalidSubagentRequest("checkpoint provenance snapshot is malformed")
    return CheckpointProvenance(**value)


def checkpoint_from_data(value):
    """Decode a checkpoint snapshot, including ones written before provenance."""
    if not value:
        return None
    value = dict(value)
    if not set(value) <= _CHECKPOINT_FIELDS:
        raise InvalidSubagentRequest("checkpoint snapshot is malformed")
    value["provenance"] = provenance_from_data(value.get("provenance"))
    return ContinuableCheckpoint(**value)


def result_from_data(value):
    if value is None:
        return None
    value = dict(value)
    value["status"] = SubagentStatus(value["status"])
    value["usage"] = SubagentUsage(**value["usage"])
    value["error"] = SubagentError(**value["error"]) if value["error"] else None
    return SubagentResult(**value)


def session_from_data(value):
    from ..ports.continuation_records import DurableChildSession, ChildSessionLineage

    value = dict(value)
    value.setdefault("terminal_verification", {})
    request = dict(value["request"])
    lineage = dict(value["lineage"])
    request["budget"] = SubagentBudget(**request["budget"])
    request["metadata"] = tuple(tuple(pair) for pair in request["metadata"])
    lineage["ancestors"] = tuple(lineage["ancestors"])
    value.update(
        request=SubagentRequest(**request),
        lineage=ChildSessionLineage(**lineage),
        status=SubagentStatus(value["status"]),
        usage=SubagentUsage(**value["usage"]),
        checkpoint=checkpoint_from_data(value["checkpoint"]),
        result=result_from_data(value["result"]),
    )
    return DurableChildSession(**value)


def decode_call(prepared):
    import json

    value = json.loads(prepared.payload)
    if prepared.kind == "create":
        return (session_from_data(value["session"]),), {}
    if prepared.kind == "save_checkpoint":
        return (checkpoint_from_data(value.pop("checkpoint")),), value
    if prepared.kind == "update":
        value["status"] = SubagentStatus(value["status"])
        if value.get("usage") is not None:
            value["usage"] = SubagentUsage(**value["usage"])
        if value.get("result") is not None:
            value["result"] = result_from_data(value["result"])
    return (prepared.child_id,), value
