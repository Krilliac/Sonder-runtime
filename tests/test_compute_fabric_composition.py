from __future__ import annotations

import sys

import pytest

from sonder_runtime.bootstrap.app import build_application
from sonder_runtime.domain.compute_fabric import WorkloadKind
from sonder_runtime.platform.config import ComputeConfig, ComputeJobConfig, SonderConfig, StateConfig


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


def test_local_registry_and_worker_share_resolved_workspace_mapping(tmp_path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")
    home = tmp_path / "state"
    home.mkdir()
    config = SonderConfig(
        state=StateConfig(home=str(home), workspace_roots=(str(alias),)),
        compute=ComputeConfig(jobs=(
            ComputeJobConfig(
                job_id="local-build", workload="build", program=sys.executable,
                workspace_mappings=("actual",),
            ),
        )),
    )
    app = build_application(config=config)
    assert "actual" in app.compute_registry().get_node("local").workspace_mappings
    assert app.compute_job_worker()._workspaces["actual"] == actual.resolve()
