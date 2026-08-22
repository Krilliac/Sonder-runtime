from pathlib import Path

import pytest

from sonder_runtime.application.protocol.editor_interop import (
    EditorInteropError,
    ProtocolEnvelope,
    RuleDocument,
    export_documents,
    import_documents,
)


def test_envelope_round_trip_is_explicit_and_versioned():
    envelope = ProtocolEnvelope.create("editor.apply", {"path": "AGENTS.md"})
    decoded = ProtocolEnvelope.from_dict(envelope.to_dict())
    assert decoded == envelope
    assert set(envelope.to_dict()) == {"protocol_version", "message_type", "message_id", "payload"}


def test_envelope_rejects_unknown_fields_and_bad_version():
    envelope = ProtocolEnvelope.create("editor.apply", {})
    with pytest.raises(EditorInteropError):
        ProtocolEnvelope.from_dict({**envelope.to_dict(), "extra": True})
    with pytest.raises(EditorInteropError):
        ProtocolEnvelope.from_dict({**envelope.to_dict(), "protocol_version": "99"})


def test_import_export_supports_agents_skill_and_rule_formats(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (source / "SKILL.md").write_text("skill rules", encoding="utf-8")
    (source / "policy.yaml").write_text("enabled: true\n", encoding="utf-8")
    docs = import_documents(source, ["AGENTS.md", "SKILL.md", "policy.yaml"])
    destination = tmp_path / "destination"
    assert export_documents(destination, docs) == ("AGENTS.md", "SKILL.md", "policy.yaml")
    assert import_documents(destination, [doc.path for doc in docs]) == docs


def test_paths_are_root_bounded_and_normalized(tmp_path: Path):
    with pytest.raises(EditorInteropError):
        import_documents(tmp_path, ["../AGENTS.md"])
    with pytest.raises(EditorInteropError):
        export_documents(tmp_path, [RuleDocument("C:/outside.md", "x", "md")])


def test_content_and_document_count_are_bounded(tmp_path: Path):
    with pytest.raises(EditorInteropError):
        RuleDocument("AGENTS.md", "x" * (256 * 1024 + 1), "agents")
    with pytest.raises(EditorInteropError):
        export_documents(tmp_path, [RuleDocument(f"rule{i}.md", "x", "md") for i in range(129)])


def test_symlink_import_cannot_escape_root(tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "policy.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(EditorInteropError):
        import_documents(root, ["policy.md"])
