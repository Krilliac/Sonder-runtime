"""Roundtrip typed continuation snapshots independently of a SQL dialect."""

from ..ports.subagents import (
    SubagentRequest,
    SubagentBudget,
    SubagentStatus,
    SubagentUsage,
    SubagentResult,
    SubagentError,
)
from .continuable import ContinuableCheckpoint


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
        checkpoint=(
            ContinuableCheckpoint(**value["checkpoint"])
            if value["checkpoint"]
            else None
        ),
        result=result_from_data(value["result"]),
    )
    return DurableChildSession(**value)


def decode_call(prepared):
    import json

    value = json.loads(prepared.payload)
    if prepared.kind == "create":
        return (session_from_data(value["session"]),), {}
    if prepared.kind == "save_checkpoint":
        return (ContinuableCheckpoint(**value.pop("checkpoint")),), value
    if prepared.kind == "update":
        value["status"] = SubagentStatus(value["status"])
        if value.get("usage") is not None:
            value["usage"] = SubagentUsage(**value["usage"])
        if value.get("result") is not None:
            value["result"] = result_from_data(value["result"])
    return (prepared.child_id,), value
