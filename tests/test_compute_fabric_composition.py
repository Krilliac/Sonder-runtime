from __future__ import annotations

from sonder_runtime.bootstrap.app import build_application
from sonder_runtime.domain.compute_fabric import WorkloadKind
from sonder_runtime.platform.config import SonderConfig


def test_application_composes_local_fabric_lazily() -> None:
    app = build_application(config=SonderConfig())
    registry = app.compute_registry()
    local = registry.get_node("local")
    assert local.local
    assert WorkloadKind.INFERENCE not in local.allowed_workloads
    assert app.compute_scheduler is not None


def test_application_composes_configured_remote_nodes_without_probing_them() -> None:
    from sonder_runtime.platform.config import ComputeConfig, ComputeNodeConfig

    config = SonderConfig(compute=ComputeConfig(
        allow_remote=True,
        nodes=(ComputeNodeConfig(
            node_id="linux-node",
            origin="https://linux-node:8443",
            workloads=("build", "test"),
            capabilities=("cpu", "cmake"),
            workspace_mappings=("sonder",),
        ),),
    ))
    app = build_application(config=config)
    assert app.compute_registry().get_node("linux-node").origin == "https://linux-node:8443"
    assert app.compute_registry().last_observation("linux-node") is None
