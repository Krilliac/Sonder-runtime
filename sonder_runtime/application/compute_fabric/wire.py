"""Bounded provider-neutral wire projections for the compute fabric."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .jobs import RemoteArtifactReceipt, RemoteJobEnvelope, RemoteJobReceipt

from ...domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
    WorkloadKind,
)


_SNAPSHOT_FIELDS = {
    "node_id",
    "observed_at",
    "health",
    "live_capabilities",
    "advertised_workloads",
    "resources",
    "active_jobs",
    "models",
    "evidence_ref",
}
_RESOURCE_FIELDS = {
    "cpu_count",
    "total_ram_bytes",
    "free_ram_bytes",
    "total_disk_bytes",
    "free_disk_bytes",
    "total_vram_bytes",
    "free_vram_bytes",
    "load_fraction",
    "gpu_utilization_fraction",
}
_JOB_ENVELOPE_FIELDS = {
    "controller_job_id",
    "idempotency_key",
    "workload",
    "catalog_entry_id",
    "workspace_mapping",
    "relative_cwd",
    "arguments",
    "environment",
    "deadline_seconds",
    "idempotent",
    "request_sha256",
}
_JOB_RECEIPT_FIELDS = {
    "worker_id",
    "remote_job_id",
    "controller_job_id",
    "idempotency_key",
    "request_sha256",
    "state",
    "process_id",
    "artifacts",
}
_ARTIFACT_FIELDS = {"name", "size_bytes", "mime_type", "sha256"}


def snapshot_to_wire(snapshot: NodeSnapshot) -> dict[str, Any]:
    return {
        "node_id": snapshot.node.node_id,
        "observed_at": snapshot.observed_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "health": snapshot.health.value,
        "live_capabilities": sorted(item.value for item in snapshot.live_capabilities),
        "advertised_workloads": sorted(
            item.value for item in snapshot.advertised_workloads
        ),
        "resources": snapshot.resources.as_dict(),
        "active_jobs": snapshot.active_jobs,
        "models": list(snapshot.models),
        "evidence_ref": snapshot.evidence_ref,
    }


def _enum_list(
    value: Any,
    enum_type: type,
    label: str,
    *,
    limit: int,
) -> frozenset:
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError(f"{label} must be a bounded string list")
    try:
        return frozenset(enum_type(item) for item in value)
    except ValueError as exc:
        raise ValueError(f"{label} contains an unknown value") from exc


def snapshot_from_wire(
    node: ComputeNode,
    payload: Mapping[str, Any],
    *,
    now: datetime,
    round_trip_ms: float | None = None,
) -> NodeSnapshot:
    if not isinstance(payload, Mapping) or set(payload) != _SNAPSHOT_FIELDS:
        raise ValueError("snapshot fields do not match the bounded schema")
    if payload["node_id"] != node.node_id:
        raise ValueError("snapshot node identity does not match configured identity")
    raw_time = payload["observed_at"]
    if not isinstance(raw_time, str) or len(raw_time) > 64:
        raise ValueError("snapshot observed_at must be a bounded timestamp")
    try:
        observed_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("snapshot observed_at is invalid") from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("snapshot observed_at must be timezone-aware")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if observed_at.astimezone(timezone.utc) > now.astimezone(timezone.utc) + timedelta(seconds=5):
        raise ValueError("snapshot observed_at is in the future")

    resources_raw = payload["resources"]
    if not isinstance(resources_raw, Mapping) or set(resources_raw) != _RESOURCE_FIELDS:
        raise ValueError("snapshot resources do not match the bounded schema")
    models = payload["models"]
    if (
        not isinstance(models, list)
        or len(models) > 256
        or any(not isinstance(item, str) for item in models)
    ):
        raise ValueError("snapshot models must be a bounded string list")
    try:
        health = NodeHealth(payload["health"])
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot health is invalid") from exc
    return NodeSnapshot(
        node=node,
        observed_at=observed_at,
        health=health,
        live_capabilities=_enum_list(
            payload["live_capabilities"],
            ComputeCapability,
            "live_capabilities",
            limit=len(ComputeCapability),
        ),
        advertised_workloads=_enum_list(
            payload["advertised_workloads"],
            WorkloadKind,
            "advertised_workloads",
            limit=len(WorkloadKind),
        ),
        resources=NodeResources(**dict(resources_raw)),
        active_jobs=payload["active_jobs"],
        round_trip_ms=round_trip_ms,
        models=tuple(models),
        evidence_ref=payload["evidence_ref"],
    )


def job_envelope_to_wire(envelope: RemoteJobEnvelope) -> dict[str, Any]:
    envelope.verify()
    return {
        **envelope.canonical(),
        "request_sha256": envelope.request_sha256,
    }


def job_envelope_from_wire(payload: Mapping[str, Any]) -> RemoteJobEnvelope:
    if not isinstance(payload, Mapping) or set(payload) != _JOB_ENVELOPE_FIELDS:
        raise ValueError("compute job fields do not match the bounded schema")
    arguments = payload["arguments"]
    environment = payload["environment"]
    if not isinstance(arguments, list):
        raise ValueError("compute job arguments must be an array")
    if not isinstance(environment, list) or any(
        not isinstance(pair, list) or len(pair) != 2 for pair in environment
    ):
        raise ValueError("compute job environment must be a key/value array")
    try:
        workload = WorkloadKind(payload["workload"])
        return RemoteJobEnvelope(
            controller_job_id=payload["controller_job_id"],
            idempotency_key=payload["idempotency_key"],
            workload=workload,
            catalog_entry_id=payload["catalog_entry_id"],
            workspace_mapping=payload["workspace_mapping"],
            relative_cwd=payload["relative_cwd"],
            arguments=tuple(arguments),
            environment=tuple(tuple(pair) for pair in environment),
            deadline_seconds=payload["deadline_seconds"],
            idempotent=payload["idempotent"],
            request_sha256=payload["request_sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"compute job envelope is invalid: {exc}") from exc


def job_receipt_to_wire(receipt: RemoteJobReceipt) -> dict[str, Any]:
    return {
        "worker_id": receipt.worker_id,
        "remote_job_id": receipt.remote_job_id,
        "controller_job_id": receipt.controller_job_id,
        "idempotency_key": receipt.idempotency_key,
        "request_sha256": receipt.request_sha256,
        "state": receipt.state,
        "process_id": receipt.process_id,
        "artifacts": [
            {
                "name": artifact.name,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
            }
            for artifact in receipt.artifacts
        ],
    }


def job_receipt_from_wire(payload: Mapping[str, Any]) -> RemoteJobReceipt:
    if not isinstance(payload, Mapping) or set(payload) != _JOB_RECEIPT_FIELDS:
        raise ValueError("compute job receipt fields do not match the bounded schema")
    raw_artifacts = payload["artifacts"]
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) > 256:
        raise ValueError("compute job artifacts must be a bounded array")
    artifacts: list[RemoteArtifactReceipt] = []
    for raw in raw_artifacts:
        if not isinstance(raw, Mapping) or set(raw) != _ARTIFACT_FIELDS:
            raise ValueError("compute artifact fields do not match the bounded schema")
        artifacts.append(RemoteArtifactReceipt(**dict(raw)))
    try:
        return RemoteJobReceipt(
            worker_id=payload["worker_id"],
            remote_job_id=payload["remote_job_id"],
            controller_job_id=payload["controller_job_id"],
            idempotency_key=payload["idempotency_key"],
            request_sha256=payload["request_sha256"],
            state=payload["state"],
            process_id=payload["process_id"],
            artifacts=tuple(artifacts),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"compute job receipt is invalid: {exc}") from exc


__all__ = [
    "job_envelope_from_wire",
    "job_envelope_to_wire",
    "job_receipt_from_wire",
    "job_receipt_to_wire",
    "snapshot_from_wire",
    "snapshot_to_wire",
]
