"""Pure child admission rules; storage executes them under its writer lock.

Steps and output tokens are aggregate parent envelopes. Wall time is billed
additively in worker-seconds, including time spent by concurrent children.
Live/recoverable children reserve their full ceilings; a settled child keeps
its measured steps and wall plus outstanding descendants. Output tokens lack
trusted model telemetry, so terminal children keep their full token reservation
unless cancelled before starting. Each ancestor owns a nested envelope.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from ..ports.continuation_records import DurableChildSession
from ..ports.subagents import (
    TERMINAL_SUBAGENT_STATUSES,
    InvalidSubagentRequest,
    SubagentStatus,
    SubagentUsage,
    validate_child_budget,
)
from ..ports.worker_registry import WorkerExecutionContract, WorkerRegistryError

_RESOURCES = ("max_steps", "max_output_tokens", "max_wall_seconds")
_ACTIVE = {SubagentStatus.CREATED, SubagentStatus.QUEUED, SubagentStatus.RUNNING}


def usage_is_monotonic(previous: SubagentUsage, current: SubagentUsage) -> bool:
    """A later receipt cannot return already consumed resource to a parent."""
    for name in ("steps", "output_tokens", "wall_seconds"):
        old, new = getattr(previous, name), getattr(current, name)
        if old is not None and (new is None or new < old):
            return False
    return True


def _provider_root(record: DurableChildSession) -> bool:
    return (
        record.request.child_id == record.request.parent_id
        and record.lineage.ancestors == ()
        and dict(record.request.metadata).get("provider_root") == "true"
    )


def _spent(record: DurableChildSession, field: str) -> int | float | None:
    if record.status in TERMINAL_SUBAGENT_STATUSES and record.result is None:
        # Legacy/direct terminal transitions can have no usage receipt at all.
        # A zero-valued default is not evidence that execution used nothing.
        return getattr(record.request.budget, field) or float("inf")
    if field == "max_output_tokens" and record.status in TERMINAL_SUBAGENT_STATUSES:
        # The runner's UTF-8 byte/4 estimate limits output length, but cannot
        # prove the actual model-token count. Never refund its token pool on
        # that estimate. A prestart cancellation is the one proven zero-use.
        if (record.status is SubagentStatus.CANCELLED and record.result is not None
                and record.result.error is not None
                and record.result.error.code == "cancelled_before_start"):
            return 0
        return record.request.budget.max_output_tokens or float("inf")
    if field == "max_steps":
        checkpoint = record.checkpoint.sequence + 1 if record.checkpoint else 0
        return max(record.usage.steps, checkpoint)
    if field == "max_output_tokens":
        return record.usage.output_tokens
    return record.usage.wall_seconds


def _ownership(record: DurableChildSession) -> WorkerExecutionContract:
    metadata = dict(record.request.metadata)
    if not any(key in metadata for key in (
        "execution_owned_files", "execution_task_scope", "execution_speculative_lane",
    )):
        return WorkerExecutionContract()
    try:
        owned = json.loads(metadata.get("execution_owned_files", "[]"))
        if type(owned) is not list or any(type(item) is not str for item in owned):
            raise ValueError("owned files must be a list of paths")
        speculative = metadata.get("execution_speculative_lane", "false")
        if speculative not in ("false", "true"):
            raise ValueError("invalid speculative lane")
        return WorkerExecutionContract(
            owned_files=tuple(owned),
            task_scope=metadata.get("execution_task_scope", ""),
            speculative_lane=speculative == "true",
        )
    except (ValueError, TypeError, WorkerRegistryError) as error:
        raise InvalidSubagentRequest("persisted worker ownership contract is invalid") from error


def validate_admission(
    candidate: DurableChildSession, existing: Iterable[DurableChildSession],
    *, resuming: bool = False, new_execution: bool = False,
) -> None:
    """Validate the projected state with one canonical storage snapshot.

    A provider root identifies a durable resource pool. External legacy parent
    IDs remain supported, with a shared concurrency limit but no invented
    aggregate parent pool.
    """
    records = {record.request.child_id: record for record in existing}
    child_id = candidate.request.child_id
    if child_id is None:
        raise InvalidSubagentRequest("durable child sessions require a child_id")
    if resuming and child_id not in records:
        raise InvalidSubagentRequest("unknown resumable child")
    if not resuming and child_id in records:
        raise InvalidSubagentRequest("child_id already exists")
    if len(dict(candidate.request.metadata)) != len(candidate.request.metadata):
        raise InvalidSubagentRequest("subagent metadata must be unique string pairs")
    if _provider_root(candidate):
        # An external legacy parent can share this ID before it is registered.
        # Installing a finite pool afterward would silently grandfather in
        # reservations that might already exceed it.
        if any(record.lineage.chain[0] == child_id for record in records.values()):
            raise InvalidSubagentRequest("register the provider root before admitting children")
        return

    metadata = dict(candidate.request.metadata)
    claimed = _ownership(candidate)
    lane_id = metadata.get("speculative_lane_id", "")
    hypothesis = metadata.get("hypothesis_digest", "")
    if claimed.speculative_lane and (
        ("speculative_lane_id" in metadata and not lane_id)
        or ("hypothesis_digest" in metadata and not hypothesis)
    ):
        raise InvalidSubagentRequest("speculative identity must be non-empty")
    if candidate.status in _ACTIVE and (
        claimed.owned_files or claimed.task_scope
        or (claimed.speculative_lane and (lane_id or hypothesis))
    ):
        for record in records.values():
            if record.request.child_id == child_id or record.status not in _ACTIVE:
                continue
            other = _ownership(record)
            if (claimed.speculative_lane and other.speculative_lane
                    and claimed.task_scope == other.task_scope
                    and candidate.lineage.chain[0] == record.lineage.chain[0]):
                other_metadata = dict(record.request.metadata)
                if ((lane_id and lane_id == other_metadata.get("speculative_lane_id"))
                        or (hypothesis and hypothesis == other_metadata.get("hypothesis_digest"))):
                    raise InvalidSubagentRequest("active speculative hypothesis is already reserved")
            conflict = claimed.conflicts_with(other)
            if conflict:
                raise InvalidSubagentRequest(conflict)

    parent = records.get(candidate.request.parent_id)
    if parent is not None:
        expected_ancestors = () if _provider_root(parent) else parent.lineage.chain
        if candidate.lineage.ancestors != expected_ancestors:
            raise InvalidSubagentRequest("child lineage does not match durable parent")
        if new_execution and (
            parent.cancellation_requested or parent.status is SubagentStatus.CANCELLED
        ):
            raise InvalidSubagentRequest("cancelled parent cannot admit children")
        validate_child_budget(candidate.request.budget, parent.request.budget)

    projected = dict(records)
    projected[child_id] = candidate
    # Correlation IDs intentionally create separate durable operation roots.
    # Their immutable host owner still has one shared live-worker ceiling:
    # otherwise concurrent requests each obtain the complete host width.
    root_record = projected.get(candidate.lineage.chain[0])
    if root_record is not None and _provider_root(root_record):
        host_owner = dict(root_record.request.metadata).get("owner_id", "")
        if host_owner:
            host_roots = {
                record.request.child_id: record for record in projected.values()
                if _provider_root(record)
                and dict(record.request.metadata).get("owner_id") == host_owner
            }
            claiming = {
                record.lineage.chain[0] for record in projected.values()
                if record.request.child_id not in host_roots
                and record.lineage.chain[0] in host_roots
                and (record.status in _ACTIVE or record.recovery_required)
            }
            owner_active = sum(
                record.status in _ACTIVE for record in projected.values()
                if record.request.child_id not in host_roots
                and record.lineage.chain[0] in host_roots
            )
            ceilings = [
                root.request.budget.max_concurrency
                for root_id, root in host_roots.items()
                if (root_id in claiming or root_id == candidate.lineage.chain[0])
                and root.request.budget.max_concurrency is not None
            ]
            if ceilings and owner_active > min(ceilings):
                raise InvalidSubagentRequest("host concurrency budget exhausted")
    children: dict[str, list[DurableChildSession]] = {}
    descendants: dict[str, list[DurableChildSession]] = {}
    for record in projected.values():
        node_id = record.request.child_id
        if node_id != record.request.parent_id:
            children.setdefault(record.request.parent_id, []).append(record)
        for ancestor_id in record.lineage.chain:
            if ancestor_id != node_id:
                descendants.setdefault(ancestor_id, []).append(record)
    ancestry = candidate.lineage.chain
    root_id = ancestry[0]
    if candidate.request.budget.max_depth is not None and len(ancestry) > candidate.request.budget.max_depth:
        raise InvalidSubagentRequest("subagent depth budget exhausted")

    # Legacy callers can use an external host parent without registering an
    # anchor. Its request-local cap remains durable across service instances.
    if parent is None:
        active = sum(
            record.status in _ACTIVE and record.lineage.chain[0] == root_id
            for record in projected.values()
        )
        caps = [
            record.request.budget.max_concurrency
            for record in projected.values()
            if record.status in _ACTIVE and record.lineage.chain[0] == root_id
        ]
        ceilings = [cap for cap in caps if cap is not None]
        if ceilings and active > min(ceilings):
            raise InvalidSubagentRequest("subagent concurrency budget exhausted")

    def liability(record: DurableChildSession, field: str, seen: frozenset[str]) -> int | float:
        node_id = record.request.child_id
        if node_id in seen:
            raise InvalidSubagentRequest("child lineage contains a cycle")
        bound = getattr(record.request.budget, field)
        if record.status in _ACTIVE or record.recovery_required:
            return bound if bound is not None else float("inf")
        spent = _spent(record, field)
        if spent is None:
            # A prestart cancellation consumed nothing. Other old/unknown
            # terminal usage cannot safely return its reservation.
            if (record.status is SubagentStatus.CANCELLED and record.result is not None
                    and record.result.error is not None
                    and record.result.error.code == "cancelled_before_start"):
                spent = 0
            else:
                spent = bound if bound is not None else float("inf")
        delegated = sum(
            liability(child, field, seen | {node_id}) for child in children.get(node_id, ())
        )
        if field == "max_output_tokens" and record.status in TERMINAL_SUBAGENT_STATUSES:
            # The retained parent block already contains the child blocks.
            # Adding them again would charge descendants twice at the root.
            return max(spent, delegated)
        return spent + delegated

    for ancestor_id in ancestry + (child_id,):
        ancestor = projected.get(ancestor_id)
        if ancestor is None:
            continue  # Implicit external legacy ancestor, not a fabricated pool.
        if new_execution and ancestor_id != child_id and (
            ancestor.cancellation_requested or ancestor.status is SubagentStatus.CANCELLED
        ):
            raise InvalidSubagentRequest("cancelled ancestor cannot admit children")
        ceiling = ancestor.request.budget
        if ancestor_id != child_id and ceiling.max_depth is not None and len(ancestry) > ceiling.max_depth:
            raise InvalidSubagentRequest("subagent depth budget exhausted")
        subtree = descendants.get(ancestor_id, ())
        if ceiling.max_children is not None and len(subtree) > ceiling.max_children:
            raise InvalidSubagentRequest("subagent child-count budget exhausted")
        if ceiling.max_concurrency is not None and sum(
            item.status in _ACTIVE for item in subtree
        ) > ceiling.max_concurrency:
            raise InvalidSubagentRequest("subagent concurrency budget exhausted")
        for field in _RESOURCES:
            limit = getattr(ceiling, field)
            if limit is None:
                continue
            spent = _spent(ancestor, field)
            if spent is None:
                spent = 0 if _provider_root(ancestor) else (
                    limit if ancestor.status in TERMINAL_SUBAGENT_STATUSES else 0
                )
            committed = sum(
                liability(item, field, frozenset({ancestor_id}))
                for item in children.get(ancestor_id, ())
            )
            accounted = (
                max(spent, committed)
                if field == "max_output_tokens"
                and ancestor.status in TERMINAL_SUBAGENT_STATUSES
                else spent + committed
            )
            if accounted > limit:
                raise InvalidSubagentRequest(f"parent {field} resource budget exhausted")
