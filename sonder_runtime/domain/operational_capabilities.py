"""Read-only projection of distributed and mobility capability boundaries.

The runtime has several deliberately independent transports.  A configured
peer, an authenticated receiver, or a request pool is evidence for that one
transport only; none of them silently becomes model sharding, ownership
failover, or automatic migration.  This module keeps that distinction in one
small, side-effect-free projection for operator status consumers.
"""
from __future__ import annotations


_SCHEMA_VERSION = 1


def _capability(available: bool, reason: str) -> dict[str, object]:
    return {"available": bool(available), "reason": str(reason)}


def _pool_summary(pool_status: object) -> tuple[dict[str, object], dict[str, object]]:
    """Return a bounded pool summary and the request-pooling capability."""
    if not isinstance(pool_status, dict):
        return (
            {"configured": False, "worker_count": 0, "healthy_worker_count": 0},
            _capability(
                False,
                "The inference worker pool status was not composed.",
            ),
        )
    worker_count = pool_status.get("worker_count", 0)
    healthy = pool_status.get("healthy_worker_count", 0)
    if type(worker_count) is not int or worker_count < 0:
        worker_count = 0
    if type(healthy) is not int or healthy < 0:
        healthy = 0
    enabled = pool_status.get("enabled") is True
    remote = pool_status.get("remote_worker_count", 0)
    if type(remote) is not int or remote < 0:
        remote = 0
    summary = {
        "configured": True,
        "worker_count": min(worker_count, 1024),
        "healthy_worker_count": min(healthy, 1024),
        "remote_worker_count": min(remote, 1024),
        "routing": str(pool_status.get("routing", ""))[:128],
        "admission": str(pool_status.get("admission", ""))[:32],
    }
    if enabled:
        reason = (
            "Inference requests may be routed to one healthy worker at a time; "
            "the pool does not shard one model request across workers."
        )
    elif worker_count <= 1:
        reason = "One inference worker is configured; request pooling is inactive."
    else:
        reason = "The configured inference pool is not accepting pooled requests."
    return summary, _capability(enabled, reason)


def build_operational_capabilities(
    *,
    config: object | None,
    inference_pool_status: object = None,
    memory_receiver_configured: bool = False,
    managed_work_configured: bool = False,
) -> dict[str, object]:
    """Build an admin-safe, non-probing capability snapshot.

    Only already-applied configuration and injected object presence are read.
    No network probe, filesystem walk, model load, or peer enrollment occurs.
    """
    compute = getattr(config, "compute", None)
    node_id = str(getattr(compute, "node_id", "local") or "local")[:128]
    configured_peers = getattr(compute, "nodes", ())
    if not isinstance(configured_peers, tuple):
        configured_peers = tuple(configured_peers or ())
    peer_ids = []
    for node in configured_peers[:64]:
        identity = str(getattr(node, "node_id", "") or "")[:128]
        if identity and identity not in peer_ids and identity != node_id:
            peer_ids.append(identity)
    remote_compute = bool(getattr(compute, "allow_remote", False) and peer_ids)
    pool_summary, pooled_inference = _pool_summary(inference_pool_status)

    artifact = getattr(config, "artifact_transfer", None)
    artifact_enabled = bool(getattr(artifact, "enabled", False))
    artifact_capability = _capability(
        artifact_enabled,
        (
            "Authenticated, bounded artifact transfer is enabled by an explicit "
            "receiver grant."
            if artifact_enabled
            else "Artifact transfer is disabled until an explicit receiver grant is applied."
        ),
    )
    memory_capability = _capability(
        bool(memory_receiver_configured),
        (
            "An authenticated memory replication receiver is explicitly injected; "
            "replication remains a bounded batch transport."
            if memory_receiver_configured
            else "Memory replication is disabled until an authenticated receiver is injected."
        ),
    )
    app_control = getattr(config, "app_control", None)
    app_control_enabled = bool(getattr(app_control, "enabled", False))
    managed_work = _capability(
        bool(managed_work_configured and app_control_enabled),
        (
            "The owned app-work dispatcher is installed; requests remain subject "
            "to the app-control account, grant, and approval gates."
            if managed_work_configured and app_control_enabled
            else (
                "App-control metadata is enabled, but the owned app-work "
                "dispatcher is not composed."
                if app_control_enabled
                else "Managed app work is disabled by configuration."
            )
        ),
    )

    return {
        "schema_version": _SCHEMA_VERSION,
        "control": {
            "managed_app_work": managed_work,
        },
        "inference": {
            "request_level_pooling": pooled_inference,
            "model_sharding": _capability(
                False,
                "Model weights and one request are owned by one worker; tensor/model sharding is not integrated.",
            ),
            "pool": pool_summary,
        },
        "compute": {
            "local_node": node_id,
            "configured_peer_count": len(peer_ids),
            "remote_enabled": remote_compute,
            "whole_job_placement": _capability(
                True,
                "The bounded compute fabric places a complete cataloged job on one eligible node.",
            ),
            "indefinite_scale": _capability(
                False,
                "Worker, queue, and identity bounds are explicit; indefinite scale requires an external provider and admission policy.",
            ),
        },
        "mobility": {
            "memory_replication_transport": memory_capability,
            "artifact_transfer_transport": artifact_capability,
            "automatic_memory_migration": _capability(
                False,
                "Memory ownership, discovery, election, and automatic migration are not integrated.",
            ),
            "automatic_artifact_migration": _capability(
                False,
                "Artifact transfer is explicit and content-addressed; jobs and models are not migrated automatically.",
            ),
        },
    }


__all__ = ["build_operational_capabilities"]
