"""Original host evidence survives restart without a default-clean projection."""

from dataclasses import dataclass, replace, asdict
import hashlib
import json

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)


@dataclass(frozen=True)
class HostProjection:
    binding: object
    dirty: bool
    terminal_class: str
    issuer: object


class Codec:
    """Test host issuer: external dictionaries cannot be encoded as evidence."""

    def __init__(self):
        self.issuer = object()

    def encode(self, projection):
        if (
            not isinstance(projection, HostProjection)
            or projection.issuer is not self.issuer
        ):
            raise PermissionError("host issuer required")
        return json.dumps(
            dict(
                binding=asdict(projection.binding),
                dirty=projection.dirty,
                terminal_class=projection.terminal_class,
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def decode(self, payload):
        from sonder_runtime.application.ports.lane_continuation import ProjectionBinding

        value = json.loads(payload)
        data = value["binding"]
        data["project_roots"] = tuple(data["project_roots"])
        return HostProjection(
            ProjectionBinding(**data),
            value["dirty"],
            value["terminal_class"],
            self.issuer,
        )

    def binding(self, projection):
        if (
            not isinstance(projection, HostProjection)
            or projection.issuer is not self.issuer
        ):
            raise PermissionError("host issuer required")
        return projection.binding


def setup_projection(tmp_path):
    from sonder_runtime.application.ports.lane_continuation import ProjectionBinding

    binding = ProjectionBinding(
        "continuation", "owner", "run", "host-task", "parent", 1, "verify", "b" * 64, (str(tmp_path),), 1
    )
    codec = Codec()
    return codec, HostProjection(binding, True, "VALIDATION_FAILED", codec.issuer)


def test_projection_roundtrip_keeps_dirty_failure_and_rejects_model_dict(tmp_path):
    from sonder_runtime.application.ports.lane_continuation import (
        seal_projection,
        open_projection,
    )

    codec, original = setup_projection(tmp_path)
    sealed = seal_projection(codec, original, original.binding)
    restored = open_projection(codec, sealed, original.binding)
    assert restored.dirty is True
    assert restored.terminal_class == "VALIDATION_FAILED"
    assert sealed.sha256 == hashlib.sha256(sealed.payload).hexdigest()
    with pytest.raises(PermissionError):
        seal_projection(codec, {"dirty": False}, original.binding)
    with pytest.raises(PermissionError):
        seal_projection(None, original, original.binding)


def test_projection_tampering_and_changed_binding_fail_before_decode(tmp_path):
    from sonder_runtime.application.ports.lane_continuation import (
        seal_projection,
        open_projection,
    )

    codec, original = setup_projection(tmp_path)
    sealed = seal_projection(codec, original, original.binding)
    with pytest.raises(ValueError):
        open_projection(
            codec,
            replace(sealed, payload=sealed.payload.replace(b"true", b"false")),
            original.binding,
        )
    with pytest.raises(PermissionError):
        open_projection(
            codec, sealed, replace(original.binding, parent_session_id="foreign")
        )


def test_projection_rejects_noncanonical_and_oversize_codec_output(tmp_path):
    from sonder_runtime.application.ports.lane_continuation import seal_projection

    codec, original = setup_projection(tmp_path)
    encode = codec.encode
    codec.encode = lambda value: encode(value) + b"\n"
    with pytest.raises(ValueError):
        seal_projection(codec, original, original.binding)
    codec.encode = lambda value: b" " * 65537
    with pytest.raises(ValueError):
        seal_projection(codec, original, original.binding)


def test_scoped_projection_link_is_immutable_and_survives_store_reopen(tmp_path):
    from sonder_runtime.application.ports.lane_continuation import (
        seal_projection,
        open_projection,
    )

    codec, original = setup_projection(tmp_path)
    sealed = seal_projection(codec, original, original.binding)
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    with store.transaction() as tx:
        tx.link_terminal_projection("continuation", "owner", sealed)
        tx.link_terminal_projection("continuation", "owner", sealed)
    reopened = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    with reopened.transaction() as tx:
        restored = tx.terminal_projection("continuation", "owner", "verify")
        with pytest.raises(KeyError):
            tx.terminal_projection("continuation", "other", "verify")
        changed = seal_projection(
            codec, replace(original, dirty=False), original.binding
        )
        with pytest.raises(ValueError):
            tx.link_terminal_projection("continuation", "owner", changed)
        with pytest.raises(PermissionError):
            tx.link_terminal_projection("foreign-continuation", "owner", sealed)
        with pytest.raises(PermissionError):
            tx.link_terminal_projection("continuation", "foreign-principal", sealed)
    assert open_projection(codec, restored, original.binding) == original


def test_projection_store_corruption_is_not_decoded_as_clean(tmp_path):
    from sonder_runtime.application.ports.lane_continuation import seal_projection

    codec, original = setup_projection(tmp_path)
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    with store.transaction() as tx:
        tx.link_terminal_projection(
            "continuation", "owner", seal_projection(codec, original, original.binding)
        )
    with store.connect() as conn:
        conn.execute("UPDATE agent_lane_terminal_projections SET payload=?", (b"{}",))
    with store.transaction() as tx:
        with pytest.raises(ValueError):
            tx.terminal_projection("continuation", "owner", "verify")


@pytest.mark.parametrize("column,value", [("principal", "foreign"), ("continuation_id", "foreign")])
def test_projection_store_scope_tamper_is_detected_on_read(tmp_path, column, value):
    from sonder_runtime.application.ports.lane_continuation import seal_projection
    codec, original = setup_projection(tmp_path)
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", SQLiteSessionRepository(tmp_path / "sessions.db"))
    with store.transaction() as tx:
        tx.link_terminal_projection("continuation", "owner", seal_projection(codec, original, original.binding))
        tx.conn.execute("UPDATE agent_lane_terminal_projections SET " + column + "=?", (value,))
        with pytest.raises(PermissionError):
            tx.terminal_projection(value if column == "continuation_id" else "continuation",
                                   value if column == "principal" else "owner", "verify")
