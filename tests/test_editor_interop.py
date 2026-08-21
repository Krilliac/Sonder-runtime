from __future__ import annotations

import json

import pytest

from sonder_runtime.application.protocol.editor_interop import (
    CancellationRequest,
    EditorInteropError,
    ImplementationInfo,
    ProtocolEnvelope,
    RuleDocument,
    export_documents,
    import_documents,
)


def test_protocol_envelope_round_trips_and_rejects_unknown_fields():
    envelope = ProtocolEnvelope.create("rules.import", {"count": 2})
    restored = ProtocolEnvelope.from_dict(envelope.to_dict())
    assert restored == envelope
    assert json.loads(json.dumps(restored.to_dict()))["message_type"] == "rules.import"

    malformed = envelope.to_dict()
    malformed["unexpected"] = True
    with pytest.raises(EditorInteropError, match="unknown fields"):
        ProtocolEnvelope.from_dict(malformed)


def test_acp_peer_metadata_is_bounded_and_cancellation_is_versioned():
    info = ImplementationInfo("sonder", "2026.08", frozenset({"cancel", "diffs"}))
    assert info.to_dict()["capabilities"] == ["cancel", "diffs"]

    request = CancellationRequest(envelope_id := ProtocolEnvelope.create("x", {}).message_id, "session-1")
    envelope = request.to_envelope()
    assert envelope.message_type == "session/cancel_request"
    assert envelope.payload["request_id"] == envelope_id
    with pytest.raises(EditorInteropError):
        ImplementationInfo("sonder", "1", frozenset({"bad capability"}))


def test_rule_documents_import_and_export_are_bounded_and_digest_bound(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "AGENTS.md").write_text("Use the project test command.\n", encoding="utf-8")
    (source / "SKILL.md").write_text("Run the bounded validation.\n", encoding="utf-8")

    documents = import_documents(source, ["AGENTS.md", "SKILL.md"])
    assert [document.kind for document in documents] == ["agents", "skill"]
    assert documents[0].digest

    destination = tmp_path / "destination"
    assert export_documents(destination, documents) == ("AGENTS.md", "SKILL.md")
    assert (destination / "SKILL.md").read_text(encoding="utf-8").startswith("Run")


@pytest.mark.parametrize("path", ["../AGENTS.md", "/absolute.md", "C:/outside.md", "nested//bad.md"])
def test_rule_document_paths_fail_closed(path, tmp_path):
    with pytest.raises(EditorInteropError):
        import_documents(tmp_path, [path])


def test_rule_document_rejects_unsupported_and_escaping_exports(tmp_path):
    with pytest.raises(EditorInteropError, match="unsupported"):
        export_documents(tmp_path, [RuleDocument("notes.txt", "content", "text")])
    with pytest.raises(EditorInteropError, match="unsafe"):
        RuleDocument("../AGENTS.md", "content", "agents")
