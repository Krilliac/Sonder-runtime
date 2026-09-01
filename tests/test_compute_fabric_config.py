from __future__ import annotations

import pytest

from sonder_runtime.platform.config import ConfigError, load_config


def test_default_compute_config_is_local_only_and_remote_disabled() -> None:
    config = load_config(env={})
    assert config.compute.allow_remote is False
    assert config.compute.node_id == "local"
    assert config.compute.nodes == ()
    assert config.compute.jobs == ()
    assert config.compute.snapshot_ttl_seconds == 30
    assert config.compute.probe_timeout_ms == 2_000


def test_compute_nodes_and_catalog_entries_load_as_typed_toml(tmp_path) -> None:
    path = tmp_path / "sonder.toml"
    path.write_text(
        """
[compute]
allow_remote = true
node_id = "controller"
snapshot_ttl_seconds = 45

[[compute.nodes]]
id = "linux-node"
origin = "https://10.20.0.2:8443"
workloads = ["build", "test", "render"]
capabilities = ["cpu", "ram", "cmake", "ffmpeg"]
workspace_mappings = ["sonder"]
preference_weight = 2.5

[[compute.jobs]]
id = "pytest"
workload = "test"
program = "/opt/sonder/venv/bin/python"
fixed_args = ["-m", "pytest"]
argument_policy = "relative-paths-and-test-selectors"
environment_allowlist = ["PYTEST_ADDOPTS"]
workspace_mappings = ["sonder"]
allowed_flags = ["-q"]
allowed_bounded_options = ["--color"]
allowed_relative_path_options = ["--basetemp"]
memory_limit_bytes = 536870912
artifact_paths = ["reports/junit.xml"]
""",
        encoding="utf-8",
    )
    config = load_config(path, env={"SONDER_API_KEY": "x" * 24})
    assert config.compute.allow_remote is True
    assert config.compute.node_id == "controller"
    assert config.compute.snapshot_ttl_seconds == 45
    assert config.compute.nodes[0].node_id == "linux-node"
    assert config.compute.nodes[0].workloads == ("build", "test", "render")
    assert config.compute.nodes[0].preference_weight == 2.5
    assert config.compute.jobs[0].job_id == "pytest"
    assert config.compute.jobs[0].fixed_args == ("-m", "pytest")
    assert config.compute.jobs[0].allowed_flags == ("-q",)
    assert config.compute.jobs[0].allowed_bounded_options == ("--color",)
    assert config.compute.jobs[0].allowed_relative_path_options == ("--basetemp",)
    assert config.compute.jobs[0].memory_limit_bytes == 536870912
    assert config.compute.jobs[0].artifact_paths == ("reports/junit.xml",)
    assert config.features.cloud is False


def test_remote_compute_consent_does_not_enable_cloud_and_requires_auth(tmp_path) -> None:
    path = tmp_path / "sonder.toml"
    path.write_text(
        """[compute]\nallow_remote=true\n[[compute.nodes]]\nid='n1'\norigin='https://n1:8443'\nworkloads=['build']\n""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="SONDER_API_KEY"):
        load_config(path, env={})

    config = load_config(path, env={"SONDER_API_KEY": "x" * 24})
    assert config.compute.allow_remote is True
    assert config.features.cloud is False


@pytest.mark.parametrize(
    ("body", "message"),
    (
        (
            """[compute]\nallow_remote = true\n[[compute.nodes]]\nid='n1'\norigin='http://n1:8443'\nworkloads=['build']\n""",
            "HTTPS",
        ),
        (
            """[compute]\n[[compute.nodes]]\nid='n1'\norigin='https://n1:8443'\nworkloads=['build']\n""",
            "allow_remote",
        ),
        (
            """[compute]\nallow_remote=true\n[[compute.nodes]]\nid='n1'\norigin='https://u:p@n1:8443'\nworkloads=['build']\n""",
            "credentials",
        ),
        (
            """[compute]\nallow_remote=true\n[[compute.nodes]]\nid='n1'\norigin='https://n1:8443'\nworkloads=['teleport']\n""",
            "workload",
        ),
        (
            """[compute]\nallow_remote=true\n[[compute.nodes]]\nid='n1'\norigin='https://n1:8443'\nworkloads=['build']\ncapabilities=['quantum']\n""",
            "capability",
        ),
    ),
)
def test_remote_node_configuration_fails_closed(tmp_path, body: str, message: str) -> None:
    path = tmp_path / "sonder.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path, env={})


def test_compute_configuration_rejects_duplicate_identities_and_unknown_keys(tmp_path) -> None:
    path = tmp_path / "sonder.toml"
    path.write_text(
        """
[compute]
allow_remote = true
surprise = "no"

[[compute.nodes]]
id = "same"
origin = "https://n1:8443"
workloads = ["build"]

[[compute.nodes]]
id = "same"
origin = "https://n2:8443"
workloads = ["test"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    joined = "\n".join(excinfo.value.errors)
    assert "[compute].surprise" in joined
    assert "duplicate" in joined


def test_compute_configuration_limits_remote_nodes(tmp_path) -> None:
    path = tmp_path / "sonder.toml"
    nodes = "\n".join(
        "[[compute.nodes]]\nid='n{0}'\norigin='https://n{0}:8443'\nworkloads=['build']".format(index)
        for index in range(16)
    )
    path.write_text("[compute]\nallow_remote=true\n" + nodes, encoding="utf-8")
    with pytest.raises(ConfigError, match="at most 15"):
        load_config(path, env={})


def test_compute_catalog_rejects_unknown_argument_policy(tmp_path) -> None:
    path = tmp_path / "sonder.toml"
    path.write_text(
        """[compute]\n[[compute.jobs]]\nid='build'\nworkload='build'\nprogram='cmake'\nargument_policy='shell'\n""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="argument_policy"):
        load_config(path, env={})


@pytest.mark.parametrize(
    ("job_body", "message"),
    (
        ("program='python'", "absolute"),
        ("program='/usr/bin/python'\nenvironment_allowlist=['BAD-NAME']", "environment"),
        ("program='/usr/bin/python'\nworkspace_mappings=['bad/name']", "workspace"),
        ("program='/usr/bin/python'\nartifact_paths=['../escape']", "artifact"),
    ),
)
def test_compute_catalog_rejects_unsafe_worker_owned_fields(
    tmp_path, job_body: str, message: str,
) -> None:
    path = tmp_path / "sonder.toml"
    path.write_text(
        "[compute]\n[[compute.jobs]]\nid='job'\nworkload='test'\n" + job_body + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=message):
        load_config(path, env={})


def test_redacted_dump_serializes_nested_compute_configuration() -> None:
    dumped = load_config(env={}).as_redacted_dict()
    assert dumped["compute"] == {
        "allow_remote": False,
        "node_id": "local",
        "snapshot_ttl_seconds": 30,
        "probe_timeout_ms": 2000,
        "nodes": [],
        "jobs": [],
    }
