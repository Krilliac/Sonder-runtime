"""Host binding authority is independent of global administrator authority."""
from dataclasses import replace
import time

import pytest

from sonder_runtime.platform.config import SonderConfig, Secrets, StateConfig, ConfigError
from sonder_runtime.application.errors import DependencyUnavailable, Unauthenticated


def configured(tmp_path):
    from sonder_runtime.platform.artifact_transfer_config import ArtifactTransferConfig
    return SonderConfig(
        state=StateConfig(home=str(tmp_path / "state")),
        secrets=Secrets(api_key="administrator-" + "a" * 32,
                        artifact_transfer_key="artifact-" + "b" * 32),
        artifact_transfer=ArtifactTransferConfig(
            enabled=True, store_dir=str(tmp_path / "private-store"),
            principal_id="alice", project_id="project-a", peer_node_id="peer-b",
            grant_id="grant-one", expires_at=int(time.time()) + 3600,
            can_read=True, can_write=True,
        ),
    )


def test_disabled_binding_is_unavailable_without_importing_core():
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
    binding = ArtifactTransferBinding(lambda: SonderConfig())
    with pytest.raises(DependencyUnavailable) as error:
        binding.authenticate("Bearer any", correlation_id="test")
    assert str(error.value) == "UNAVAILABLE"


def test_existing_secret_positional_constructor_is_preserved():
    secrets = Secrets("api", "auth", "backup")
    assert secrets.auth_secret == "auth"
    assert secrets.backup_key_file == "backup"
    assert secrets.artifact_transfer_key == ""


def test_enabled_invalid_or_shared_key_fails_startup(tmp_path):
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
    config = configured(tmp_path)
    config = replace(config, secrets=replace(config.secrets,
                     artifact_transfer_key=config.secrets.api_key))
    with pytest.raises(ConfigError, match="distinct"):
        ArtifactTransferBinding(lambda: config)
    config = replace(config, secrets=replace(config.secrets, artifact_transfer_key=""))
    with pytest.raises(ConfigError, match="dedicated"):
        ArtifactTransferBinding(lambda: config)


def test_invocation_binds_fixed_identity_and_live_config(tmp_path):
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
    current = [configured(tmp_path)]
    binding = ArtifactTransferBinding(lambda: current[0])
    with pytest.raises(Unauthenticated) as error:
        binding.authenticate("Bearer " + current[0].secrets.api_key, correlation_id="test")
    assert str(error.value) == "UNAUTHORIZED"
    context = binding.authenticate("Bearer " + current[0].secrets.artifact_transfer_key,
                                   correlation_id="test")
    assert context.principal_id == "alice"
    assert current[0].secrets.artifact_transfer_key not in repr(context)
    binding.validate_context(context)
    current[0] = replace(current[0], artifact_transfer=replace(
        current[0].artifact_transfer, grant_revision=2))
    with pytest.raises(PermissionError):
        binding.validate_context(context)


@pytest.mark.parametrize("change", ["disabled", "expired", "rotated", "scope", "root"])
def test_live_revocation_invalidates_existing_proof(tmp_path, change):
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
    current = [configured(tmp_path)]
    binding = ArtifactTransferBinding(lambda: current[0])
    context = binding.authenticate("Bearer " + current[0].secrets.artifact_transfer_key,
                                   correlation_id="test")
    section = current[0].artifact_transfer
    if change == "rotated":
        current[0] = replace(current[0], secrets=replace(current[0].secrets,
                             artifact_transfer_key="rotated-" + "c" * 32))
    else:
        changes = {"disabled": {"enabled": False}, "expired": {"expires_at": 1},
                   "scope": {"project_id": "other"},
                   "root": {"store_dir": str(tmp_path / "another")}}[change]
        current[0] = replace(current[0], artifact_transfer=replace(section, **changes))
    with pytest.raises(PermissionError):
        binding.validate_context(context)


def test_typed_loader_dedicated_secret_and_redaction(tmp_path):
    from sonder_runtime.platform.config import load_config
    path = tmp_path / "sonder.toml"
    path.write_text('''[artifact_transfer]
enabled = true
principal_id = "alice"
project_id = "project-a"
peer_node_id = "peer-b"
grant_id = "grant-a"
can_read = true
expires_at = ''' + str(int(time.time()) + 3600), encoding="utf-8")
    key = "dedicated-" + "z" * 32
    config = load_config(path, env={"SONDER_ARTIFACT_TRANSFER_KEY": key})
    assert config.secrets.artifact_transfer_key == key
    assert key not in str(config.as_redacted_dict())
    assert key not in repr(config)
    with pytest.raises(ConfigError, match="dedicated"):
        load_config(path, env={})
    path.write_text('[artifact_transfer]\nartifact_transfer_key = "secret"', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, env={})


@pytest.mark.parametrize("changes", [
    {"grant_revision": 0}, {"principal_id": "x" * 129}, {"project_id": "a\nb"},
    {"expires_at": True}, {"quota_bytes": 0}, {"total_bytes": 129 * 1024**3},
    {"store_dir": "relative"}, {"ttl_seconds": 86401},
])
def test_invalid_typed_binding_rejected_before_start(tmp_path, changes):
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
    config = configured(tmp_path)
    config = replace(config, artifact_transfer=replace(config.artifact_transfer, **changes))
    with pytest.raises(ConfigError):
        ArtifactTransferBinding(lambda: config)


def test_plain_operation_context_cannot_forge_binding(tmp_path):
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
    from sonder_runtime.application.context import local_owner_context
    config = configured(tmp_path)
    binding = ArtifactTransferBinding(lambda: config)
    forged = replace(local_owner_context(correlation_id="test"), principal_id="alice")
    with pytest.raises(PermissionError):
        binding.validate_context(forged)


def test_default_store_and_configured_root_overlap(tmp_path):
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
    from sonder_runtime.platform.artifact_transfer_config import private_store_path
    config = configured(tmp_path)
    config = replace(config, artifact_transfer=replace(config.artifact_transfer, store_dir=""))
    assert private_store_path(config) == tmp_path / "state-artifact-private"
    config = replace(config, state=replace(config.state, workspace_roots=(str(tmp_path),)))
    binding = ArtifactTransferBinding(lambda: config)
    with pytest.raises(PermissionError, match="overlaps"):
        binding.start()


def test_http_configuration_composes_receiver_before_exposure(tmp_path, monkeypatch):
    from sonder_runtime.interfaces.http import serve
    config = configured(tmp_path)
    # Preserve every configuration global touched by this public composition seam.
    for name in ("_ARTIFACT_TRANSFER_BINDING", "_ARTIFACT_TRANSFER_CONFIG", "CONFIGURED_PORT",
                 "API_KEY", "AUTH_SECRET", "HOST", "REQUIRE_ACCOUNT", "AUTH_MODE", "CORS_ORIGINS",
                 "TLS_TERMINATED_BY_PROXY", "ALLOW_REGISTRATION", "MAX_REQUEST_BYTES",
                 "MAX_DISCARDED_BODY_BYTES", "REQUEST_TIMEOUT_SECONDS", "STREAM_IDLE_TIMEOUT_SECONDS",
                 "HTTP_SESSION_STATE_LIMIT", "HTTP_SESSION_STATE_OWNER_LIMIT", "TRAIN_MAX_N",
                 "_HEALTH_STATUS_FACADE", "_TRUSTED_PROXY_NETWORKS"):
        monkeypatch.setattr(serve, name, getattr(serve, name))
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", None)
    serve.configure_typed_config(config)
    binding = serve._ARTIFACT_TRANSFER_BINDING
    assert binding.service() is binding.service()
    assert (tmp_path / "private-store").is_dir()
    context = binding.authenticate("Bearer " + config.secrets.artifact_transfer_key, correlation_id="test")
    serve.configure_typed_config(replace(config, artifact_transfer=replace(config.artifact_transfer, enabled=False)))
    with pytest.raises(PermissionError):
        binding.validate_context(context)
    serve._ARTIFACT_TRANSFER_BINDING.close()
