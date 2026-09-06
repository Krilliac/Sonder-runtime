from sonder_runtime.domain.operational_capabilities import (
    build_operational_capabilities,
)
from sonder_runtime.platform.config import (
    ComputeConfig,
    ComputeNodeConfig,
    SonderConfig,
)


def test_default_surface_is_explicitly_local_and_fail_closed():
    surface = build_operational_capabilities(
        config=SonderConfig(),
        inference_pool_status={
            "enabled": False,
            "worker_count": 1,
            "healthy_worker_count": 1,
            "remote_worker_count": 0,
        },
    )

    assert surface["schema_version"] == 1
    assert surface["compute"]["configured_peer_count"] == 0
    assert surface["compute"]["remote_enabled"] is False
    assert surface["control"]["managed_app_work"]["available"] is False
    assert surface["control"]["managed_app_work"]["reason"] == (
        "Managed app work is disabled by configuration."
    )
    assert surface["inference"]["request_level_pooling"]["available"] is False
    assert surface["inference"]["model_sharding"]["available"] is False
    assert surface["mobility"]["memory_replication_transport"]["available"] is False
    assert surface["mobility"]["artifact_transfer_transport"]["available"] is False
    assert surface["mobility"]["automatic_memory_migration"]["available"] is False
    assert surface["mobility"]["automatic_artifact_migration"]["available"] is False


def test_surface_distinguishes_pooled_requests_from_sharding_and_mobility():
    config = SonderConfig(
        compute=ComputeConfig(
            node_id="node-a",
            allow_remote=True,
            nodes=(ComputeNodeConfig(node_id="node-b"),),
        )
    )
    surface = build_operational_capabilities(
        config=config,
        inference_pool_status={
            "enabled": True,
            "worker_count": 2,
            "healthy_worker_count": 1,
            "remote_worker_count": 1,
            "routing": "latency-aware-least-inflight",
            "admission": "accepting",
        },
        memory_receiver_configured=True,
    )

    assert surface["compute"]["configured_peer_count"] == 1
    assert surface["compute"]["remote_enabled"] is True
    assert surface["inference"]["request_level_pooling"]["available"] is True
    assert surface["inference"]["pool"]["remote_worker_count"] == 1
    assert surface["inference"]["model_sharding"]["available"] is False
    assert surface["mobility"]["memory_replication_transport"]["available"] is True
    assert surface["mobility"]["automatic_memory_migration"]["available"] is False


def test_surface_never_probes_or_uses_unbounded_pool_fields():
    surface = build_operational_capabilities(
        config=None,
        inference_pool_status={
            "enabled": True,
            "worker_count": 10**9,
            "healthy_worker_count": 10**9,
            "remote_worker_count": 10**9,
        },
    )
    assert surface["inference"]["pool"]["worker_count"] == 1024
    assert surface["inference"]["pool"]["healthy_worker_count"] == 1024
    assert surface["inference"]["pool"]["remote_worker_count"] == 1024


def test_surface_reports_owned_work_only_when_enabled_and_composed():
    from dataclasses import replace
    from sonder_runtime.platform.app_control_config import AppControlConfig

    config = replace(SonderConfig(), app_control=AppControlConfig(enabled=True))
    unavailable = build_operational_capabilities(
        config=config,
        managed_work_configured=False,
    )
    assert unavailable["control"]["managed_app_work"]["available"] is False
    assert "dispatcher is not composed" in unavailable["control"]["managed_app_work"]["reason"]

    available = build_operational_capabilities(
        config=config,
        managed_work_configured=True,
    )
    assert available["control"]["managed_app_work"]["available"] is True
