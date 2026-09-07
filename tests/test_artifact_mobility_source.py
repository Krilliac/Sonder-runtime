"""Local-only source-spool acceptance tests for outbound mobility Task 2."""

from dataclasses import replace
import gc
import hashlib
import io
from pathlib import Path
import socket
import sqlite3

import pytest

from sonder_runtime.application.artifacts.mobility_source import MobilitySourceError
from sonder_runtime.application.errors import DependencyUnavailable
from sonder_runtime.application.ports.artifact_mobility import (
    inspect_sealed,
    publish_sealed,
    read_range,
)
from sonder_runtime.platform.artifact_mobility_source_config import (
    ArtifactMobilitySourceConfig,
    source_scope_id,
)
from sonder_runtime.platform.config import ConfigError, SonderConfig, StateConfig


def _reachable_from(value: object, *, limit: int = 128) -> set[int]:
    """Follow only object-owned references from an untrusted port value.

    This intentionally does not inspect a module registry or referrers: a port
    holder has the value, not the host's process globals.  The regression makes
    the capability requirement concrete by detecting a future bound method,
    closure, instance dictionary, or object field that reconnects a port to
    host authority.
    """
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > limit:
            pytest.fail("opaque port exposed an unbounded object graph")
        pending.extend(gc.get_referents(current))
    return seen


def _config(tmp_path, *, principal="principal-a", project="project-a", owner="owner-a",
            max_object_bytes=256 * 1024 * 1024, total_bytes=2 * 1024 * 1024 * 1024):
    return SonderConfig(
        state=StateConfig(home=str(tmp_path / "state")),
        artifact_mobility_source=ArtifactMobilitySourceConfig(
            enabled=True,
            store_dir=str(tmp_path / "private-source"),
            principal_id=principal,
            project_id=project,
            source_owner_id=owner,
            max_object_bytes=max_object_bytes,
            total_bytes=total_bytes,
            ttl_seconds=3600,
        ),
    )


def _spec(data):
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": "application/octet-stream",
    }


def _ports(config, *, publisher_capability=None, reader_capability=None):
    from sonder_runtime.bootstrap.artifact_mobility_source import (
        ArtifactMobilitySourceBinding,
    )

    publisher_capability = publisher_capability or object()
    reader_capability = reader_capability or object()
    binding = ArtifactMobilitySourceBinding(
        lambda: config,
        publisher_capability=publisher_capability,
        reader_capability=reader_capability,
    )
    return (
        binding,
        binding.publisher_for(publisher_capability),
        binding.reader_for(reader_capability),
        publisher_capability,
    )


def test_source_ports_are_in_process_capabilities_and_construct_without_network(
    tmp_path, monkeypatch
):
    from sonder_runtime.bootstrap.artifact_mobility_source import (
        ArtifactMobilitySourceBinding,
    )

    config = _config(tmp_path)
    source_dir = Path(config.artifact_mobility_source.store_dir)

    def no_socket(*_args, **_kwargs):
        pytest.fail("source binding attempted to construct a network socket")

    monkeypatch.setattr(socket, "socket", no_socket)
    with pytest.raises(TypeError, match="opaque"):
        ArtifactMobilitySourceBinding(lambda: config, publisher_capability="replayable")
    uncomposed = ArtifactMobilitySourceBinding(lambda: config)
    assert not source_dir.exists()
    with pytest.raises(DependencyUnavailable, match="UNAVAILABLE"):
        uncomposed.publisher_for(object())
    with pytest.raises(DependencyUnavailable, match="UNAVAILABLE"):
        uncomposed.reader_for(object())

    publisher_capability, reader_capability = object(), object()
    binding = ArtifactMobilitySourceBinding(
        lambda: config,
        publisher_capability=publisher_capability,
        reader_capability=reader_capability,
    )
    with pytest.raises(PermissionError, match="FORBIDDEN"):
        binding.publisher_for(object())
    with pytest.raises(PermissionError, match="FORBIDDEN"):
        binding.reader_for(object())
    publisher = binding.publisher_for(publisher_capability)
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        publish_sealed(publisher, io.BytesIO(b"trusted"), _spec(b"trusted"), object())
    assert not source_dir.exists()


def test_ports_are_plain_opaque_capabilities_without_cross_port_or_path_references(tmp_path):
    config = _config(tmp_path)
    publisher_capability, reader_capability = object(), object()
    from sonder_runtime.bootstrap.artifact_mobility_source import (
        ArtifactMobilitySourceBinding,
    )
    from sonder_runtime.application.ports.artifact_mobility import (
        _OpaqueMobilityPort,
        _PORTS,
    )

    binding = ArtifactMobilitySourceBinding(
        lambda: config,
        publisher_capability=publisher_capability,
        reader_capability=reader_capability,
    )
    publisher = binding.publisher_for(publisher_capability)
    reader = binding.reader_for(reader_capability)
    receipt = publish_sealed(
        publisher,
        io.BytesIO(b"opaque-port-authority"),
        _spec(b"opaque-port-authority"),
        publisher_capability,
    )
    assert inspect_sealed(reader, receipt["source_artifact_id"]) == receipt
    assert binding._service is not None

    # These handles are data-free identity tokens.  A caller holding one has
    # no instance graph that can disclose the other authority, the binding,
    # its configuration, service, or private root.
    assert vars(publisher) == {}
    assert vars(reader) == {}
    # The host and registry keep weak references only, so the token also has
    # no direct reverse edge back to host authority.
    assert binding not in gc.get_referrers(publisher)
    assert binding not in gc.get_referrers(reader)
    assert _PORTS not in gc.get_referrers(publisher)
    assert _PORTS not in gc.get_referrers(reader)
    for port, forbidden in (
        (
            reader,
            (
                publisher,
                binding,
                publisher_capability,
                reader_capability,
                config,
                config.artifact_mobility_source,
                binding._config_provider,
                binding._service,
                binding._issuer,
                config.artifact_mobility_source.store_dir,
                config.state.home,
            ),
        ),
        (
            publisher,
            (
                reader,
                binding,
                publisher_capability,
                reader_capability,
                config,
                config.artifact_mobility_source,
                binding._config_provider,
                binding._service,
                binding._issuer,
                config.artifact_mobility_source.store_dir,
                config.state.home,
            ),
        ),
    ):
        assert vars(port) == {}
        reachable = _reachable_from(port)
        assert all(id(item) not in reachable for item in forbidden)
        assert config.artifact_mobility_source.store_dir not in repr(port)
        assert config.state.home not in repr(port)

    # The registry is role-specific.  Possession of one unforgeable token is
    # never sufficient to invoke the opposite operation.
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        inspect_sealed(publisher, "a" * 32)
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        read_range(publisher, "a" * 32, 0, 1)
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        publish_sealed(reader, io.BytesIO(b"x"), _spec(b"x"), reader_capability)
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        inspect_sealed(object(), "a" * 32)
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        inspect_sealed([], "a" * 32)
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        inspect_sealed(_OpaqueMobilityPort(), "a" * 32)
    binding.close()
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        inspect_sealed(reader, "a" * 32)


def test_fsynced_payload_metadata_failure_is_accounted_and_reaped_on_reopen(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, max_object_bytes=32, total_bytes=32)
    binding, publisher, _, capability = _ports(config)
    data = b"pending-metadata-payload"
    from sonder_runtime.adapters.persistence.artifact_mobility_source import (
        SQLiteArtifactMobilitySourceStore,
    )

    def fail_metadata(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected metadata failure")

    monkeypatch.setattr(
        SQLiteArtifactMobilitySourceStore, "_seal_pending", fail_metadata, raising=False
    )
    # `_seal_pending` runs only after `_copy_stream` has fsynced and published
    # the immutable payload, so this injects the formerly orphaning boundary.
    with pytest.raises(MobilitySourceError, match="UNAVAILABLE") as error:
        publish_sealed(publisher, io.BytesIO(data), _spec(data), capability)
    assert "injected metadata failure" not in repr(error.value)

    database = Path(config.artifact_mobility_source.store_dir) / "mobility-source.sqlite"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM mobility_source_artifacts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mobility_source_artifacts WHERE state='pending'"
        ).fetchone()[0] == 1
        pending_id, pending_digest = connection.execute(
            "SELECT id, sha256 FROM mobility_source_artifacts WHERE state='pending'"
        ).fetchone()
        assert connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM mobility_source_artifacts"
        ).fetchone()[0] == len(data)
    pending_payload = (
        Path(config.artifact_mobility_source.store_dir)
        / source_scope_id(config.artifact_mobility_source)
        / pending_id
        / pending_digest
    )
    assert pending_payload.is_file()
    binding.close()

    monkeypatch.undo()
    # Reopening the store reaps the exact pending directory before any future
    # publisher can calculate available object or byte capacity.
    recovered_store = SQLiteArtifactMobilitySourceStore(
        config.artifact_mobility_source.store_dir
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mobility_source_artifacts WHERE state='pending'"
        ).fetchone()[0] == 0
    assert not pending_payload.exists()
    recovered_store.close()

    reopened, reopened_publisher, _, reopened_capability = _ports(config)
    receipt = publish_sealed(reopened_publisher,
        io.BytesIO(data), _spec(data), reopened_capability
    )
    assert receipt["size_bytes"] == len(data)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mobility_source_artifacts WHERE state='pending'"
        ).fetchone()[0] == 0
    reopened.close()


def test_sealed_source_survives_reopen_only_with_the_same_scope(tmp_path):
    config = _config(tmp_path)
    binding, publisher, reader, capability = _ports(config)
    data = b"sealed-local-source-artifact"
    receipt = publish_sealed(publisher, io.BytesIO(data), _spec(data), capability)

    assert set(receipt) == {
        "source_artifact_id", "sha256", "size_bytes", "media_type",
    }
    assert inspect_sealed(reader, receipt["source_artifact_id"]) == receipt
    ranged = read_range(reader, receipt["source_artifact_id"], 3, 8)
    assert ranged.source_artifact_id == receipt["source_artifact_id"]
    assert ranged.body == data[3:11]
    assert ranged.chunk_sha256 == hashlib.sha256(data[3:11]).hexdigest()
    assert "private-source" not in repr(receipt)
    binding.close()

    reopened, _, reopened_reader, _ = _ports(config)
    assert inspect_sealed(reopened_reader, receipt["source_artifact_id"]) == receipt
    assert read_range(reopened_reader, receipt["source_artifact_id"], 0, len(data)).body == data
    reopened.close()


def test_source_range_checks_every_returned_chunk_before_disclosing_bytes(tmp_path):
    config = _config(tmp_path)
    binding, publisher, reader, capability = _ports(config)
    chunk = 1024 * 1024
    data = b"a" * (chunk - 3) + b"boundary" + b"z" * 8
    receipt = publish_sealed(publisher, io.BytesIO(data), _spec(data), capability)
    source_id = receipt["source_artifact_id"]
    assert read_range(reader, source_id, chunk - 4, 12).body == data[chunk - 4:chunk + 8]

    object_path = (
        Path(config.artifact_mobility_source.store_dir)
        / source_scope_id(config.artifact_mobility_source)
        / source_id
        / receipt["sha256"]
    )
    object_path.chmod(0o600)
    object_path.write_bytes(b"tampered")
    with pytest.raises(MobilitySourceError, match="INTEGRITY"):
        read_range(reader, source_id, 0, 1)
    binding.close()


def test_tampered_metadata_fails_closed_without_returning_control_text(tmp_path):
    config = _config(tmp_path)
    binding, publisher, reader, capability = _ports(config)
    data = b"metadata-integrity"
    receipt = publish_sealed(publisher, io.BytesIO(data), _spec(data), capability)
    injected = "application/octet-stream\r\nX-Injected: metadata"
    database = Path(config.artifact_mobility_source.store_dir) / "mobility-source.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE mobility_source_artifacts SET media_type=? WHERE id=?",
            (injected, receipt["source_artifact_id"]),
        )

    with pytest.raises(MobilitySourceError, match="INTEGRITY") as error:
        inspect_sealed(reader, receipt["source_artifact_id"])
    assert injected not in str(error.value)
    assert injected not in repr(error.value)
    binding.close()


@pytest.mark.parametrize(
    "changes",
    (
        {"principal_id": "principal-b"},
        {"project_id": "project-b"},
        {"source_owner_id": "owner-b"},
    ),
)
def test_changed_source_scope_cannot_inspect_or_read_prior_artifact(tmp_path, changes):
    original = _config(tmp_path)
    first, publisher, _, capability = _ports(original)
    data = b"scope-bound-source"
    receipt = publish_sealed(publisher, io.BytesIO(data), _spec(data), capability)
    first.close()

    changed = replace(
        original,
        artifact_mobility_source=replace(original.artifact_mobility_source, **changes),
    )
    second, _, reader, _ = _ports(changed)
    with pytest.raises(MobilitySourceError, match="NOT_FOUND"):
        inspect_sealed(reader, receipt["source_artifact_id"])
    with pytest.raises(MobilitySourceError, match="NOT_FOUND"):
        read_range(reader, receipt["source_artifact_id"], 0, 1)
    second.close()


def test_live_scope_change_revokes_existing_source_ports(tmp_path):
    config = [_config(tmp_path)]
    from sonder_runtime.bootstrap.artifact_mobility_source import (
        ArtifactMobilitySourceBinding,
    )

    publisher_capability, reader_capability = object(), object()
    binding = ArtifactMobilitySourceBinding(
        lambda: config[0],
        publisher_capability=publisher_capability,
        reader_capability=reader_capability,
    )
    publisher = binding.publisher_for(publisher_capability)
    reader = binding.reader_for(reader_capability)
    data = b"live-revocation"
    receipt = publish_sealed(
        publisher, io.BytesIO(data), _spec(data), publisher_capability
    )
    config[0] = replace(
        config[0],
        artifact_mobility_source=replace(
            config[0].artifact_mobility_source, source_owner_id="owner-b"
        ),
    )
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        inspect_sealed(reader, receipt["source_artifact_id"])
    binding.close()


def test_stale_reader_cannot_initialize_a_new_scope_store(tmp_path):
    config = [_config(tmp_path)]
    from sonder_runtime.bootstrap.artifact_mobility_source import (
        ArtifactMobilitySourceBinding,
    )

    publisher_capability, reader_capability = object(), object()
    binding = ArtifactMobilitySourceBinding(
        lambda: config[0],
        publisher_capability=publisher_capability,
        reader_capability=reader_capability,
    )
    reader = binding.reader_for(reader_capability)
    replacement_root = tmp_path / "replacement-private-source"
    config[0] = replace(
        config[0],
        artifact_mobility_source=replace(
            config[0].artifact_mobility_source,
            source_owner_id="owner-b",
            store_dir=str(replacement_root),
        ),
    )

    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        inspect_sealed(reader, "a" * 32)
    assert not replacement_root.exists()
    binding.close()


def test_forged_issuer_context_cannot_bypass_the_in_process_ports(tmp_path):
    config = _config(tmp_path)
    binding, publisher, reader, capability = _ports(config)
    data = b"issuer-bound-source"
    receipt = publish_sealed(publisher, io.BytesIO(data), _spec(data), capability)

    from sonder_runtime.bootstrap.artifact_mobility_source import _SourceContext

    valid_context = _SourceContext(
        scope_id=source_scope_id(config.artifact_mobility_source),
        role="read",
        _issuer=binding._issuer,
        _capability=binding._reader_capability,
        _proof=binding._proof(config),
    )
    service = binding._service_for_port(valid_context, "read")
    forged_context = replace(valid_context, _issuer=object())
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        service.inspect_sealed(receipt["source_artifact_id"], forged_context)
    assert inspect_sealed(reader, receipt["source_artifact_id"]) == receipt
    binding.close()


def test_forged_context_scope_cannot_read_another_private_namespace(tmp_path):
    first_config = _config(tmp_path, owner="owner-a")
    first, _, first_reader, _ = _ports(first_config)
    second_config = replace(
        first_config,
        artifact_mobility_source=replace(
            first_config.artifact_mobility_source, source_owner_id="owner-b"
        ),
    )
    second, second_publisher, _, second_capability = _ports(second_config)
    data = b"separate-source-namespace"
    receipt = publish_sealed(
        second_publisher, io.BytesIO(data), _spec(data), second_capability
    )

    from sonder_runtime.bootstrap.artifact_mobility_source import _SourceContext

    valid_context = _SourceContext(
        scope_id=source_scope_id(first_config.artifact_mobility_source),
        role="read",
        _issuer=first._issuer,
        _capability=first._reader_capability,
        _proof=first._proof(first_config),
    )
    service = first._service_for_port(valid_context, "read")
    forged_context = replace(
        valid_context,
        scope_id=source_scope_id(second_config.artifact_mobility_source),
    )
    with pytest.raises(MobilitySourceError, match="FORBIDDEN"):
        service.inspect_sealed(receipt["source_artifact_id"], forged_context)
    first.close()
    second.close()


def test_disabled_source_revokes_existing_ports_with_a_stable_local_error(tmp_path):
    config = [_config(tmp_path)]
    from sonder_runtime.bootstrap.artifact_mobility_source import (
        ArtifactMobilitySourceBinding,
    )

    publisher_capability, reader_capability = object(), object()
    binding = ArtifactMobilitySourceBinding(
        lambda: config[0],
        publisher_capability=publisher_capability,
        reader_capability=reader_capability,
    )
    publisher = binding.publisher_for(publisher_capability)
    reader = binding.reader_for(reader_capability)
    data = b"disabled-revocation"
    receipt = publish_sealed(
        publisher, io.BytesIO(data), _spec(data), publisher_capability
    )
    config[0] = replace(
        config[0],
        artifact_mobility_source=replace(
            config[0].artifact_mobility_source, enabled=False
        ),
    )
    with pytest.raises(MobilitySourceError, match="UNAVAILABLE"):
        inspect_sealed(reader, receipt["source_artifact_id"])
    binding.close()


def test_receiver_ids_and_filesystem_paths_are_not_source_requests(tmp_path):
    config = _config(tmp_path)
    binding, publisher, reader, capability = _ports(config)
    data = b"pathless-source"
    receipt = publish_sealed(publisher, io.BytesIO(data), _spec(data), capability)
    arbitrary = tmp_path / "would-be-source.bin"
    arbitrary.write_bytes(b"never staged by a path")

    with pytest.raises(MobilitySourceError, match="INVALID_STREAM"):
        publish_sealed(publisher, arbitrary, _spec(b"never staged by a path"), capability)
    with pytest.raises(MobilitySourceError, match="INVALID_SPEC"):
        publish_sealed(
            publisher,
            io.BytesIO(data), {**_spec(data), "source_path": str(arbitrary)}, capability
        )
    with pytest.raises(MobilitySourceError, match="NOT_FOUND"):
        inspect_sealed(reader, "a" * 32)
    with pytest.raises(MobilitySourceError, match="NOT_FOUND"):
        read_range(reader, str(arbitrary), 0, 1)
    assert read_range(reader, receipt["source_artifact_id"], 0, 1).body == data[:1]
    binding.close()


def test_source_rejects_partial_streams_before_they_can_expand_metadata(tmp_path):
    config = _config(tmp_path)
    binding, publisher, _, capability = _ports(config)

    class PartialStream:
        def read(self, _size):
            return b"x"

    with pytest.raises(MobilitySourceError, match="INVALID_STREAM"):
        publish_sealed(publisher, PartialStream(), _spec(b"xx"), capability)
    binding.close()


def test_source_limits_and_private_root_overlap_fail_closed(tmp_path, monkeypatch):
    too_small = _config(tmp_path, max_object_bytes=4, total_bytes=4)
    binding, publisher, reader, capability = _ports(too_small)
    with pytest.raises(MobilitySourceError, match="INVALID_BOUND"):
        publish_sealed(publisher, io.BytesIO(b"12345"), _spec(b"12345"), capability)
    receipt = publish_sealed(publisher, io.BytesIO(b"1234"), _spec(b"1234"), capability)
    with pytest.raises(MobilitySourceError, match="QUOTA"):
        publish_sealed(publisher, io.BytesIO(b"x"), _spec(b"x"), capability)
    with pytest.raises(MobilitySourceError, match="INVALID_BOUND"):
        read_range(reader, receipt["source_artifact_id"], 0, 5)
    binding.close()

    unsafe_root = tmp_path / "workspace"
    overlapping = replace(
        _config(tmp_path),
        state=StateConfig(home=str(tmp_path / "state"), workspace_roots=(str(unsafe_root),)),
        artifact_mobility_source=replace(
            _config(tmp_path).artifact_mobility_source, store_dir=str(unsafe_root)
        ),
    )
    from sonder_runtime.bootstrap.artifact_mobility_source import (
        ArtifactMobilitySourceBinding,
    )

    with pytest.raises(ConfigError, match="overlaps"):
        ArtifactMobilitySourceBinding(lambda: overlapping)

    ambient = tmp_path / "ambient-file-root"
    ambient_config = replace(
        _config(tmp_path),
        artifact_mobility_source=replace(
            _config(tmp_path).artifact_mobility_source, store_dir=str(ambient)
        ),
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.filesystem.file_ops.allowed_roots", lambda: [ambient]
    )
    with pytest.raises(ConfigError, match="overlaps"):
        ArtifactMobilitySourceBinding(lambda: ambient_config)
