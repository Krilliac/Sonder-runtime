"""Editor import/export I/O must be bounded and bound to the validated path.

* ``import_documents`` decoded the whole file with ``read_text`` and only
  then applied the 256 KiB content limit.
* Both directions validated containment with ``resolve()`` and then reopened
  the pathname, so a directory component swapped for a symlink between the
  check and the I/O read or wrote outside the chosen root.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sonder_runtime.application.protocol.editor_interop import (
    MAX_CONTENT_LENGTH,
    EditorInteropError,
    RuleDocument,
    export_documents,
    import_documents,
)


def _forbid_unbounded_reads(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise AssertionError(f"unbounded whole-file read of {self.name}")

    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)


def test_oversized_import_is_rejected_without_reading_the_whole_file(
    tmp_path, monkeypatch,
):
    (tmp_path / "AGENTS.md").write_bytes(b"x" * (MAX_CONTENT_LENGTH * 8))
    _forbid_unbounded_reads(monkeypatch)

    with pytest.raises(EditorInteropError, match="limit"):
        import_documents(tmp_path, ["AGENTS.md"])


def test_import_at_the_limit_still_succeeds(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_bytes(b"x" * MAX_CONTENT_LENGTH)
    _forbid_unbounded_reads(monkeypatch)

    (document,) = import_documents(tmp_path, ["AGENTS.md"])

    assert len(document.content) == MAX_CONTENT_LENGTH


def _swap_dir_for_symlink_after_first_check(monkeypatch, directory: Path, target: Path):
    """Replace ``directory`` with a symlink to ``target`` right after the
    containment check resolves a path below it (the TOCTOU window)."""
    real_resolve = Path.resolve
    state = {"swapped": False}

    def resolve(self, *args, **kwargs):
        result = real_resolve(self, *args, **kwargs)
        if not state["swapped"] and directory.name in self.parts:
            state["swapped"] = True
            shutil.move(str(directory), str(directory.with_name("original")))
            directory.symlink_to(target, target_is_directory=True)
        return result

    monkeypatch.setattr(Path, "resolve", resolve)
    return state


def _symlinks_or_skip(tmp_path):
    probe = tmp_path / "probe-link"
    try:
        probe.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    probe.unlink()


def test_import_rejects_a_component_swapped_outside_root_after_the_check(
    tmp_path, monkeypatch,
):
    _symlinks_or_skip(tmp_path)
    root = tmp_path / "root"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "policy.md").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "policy.md").write_text("secret", encoding="utf-8")
    state = _swap_dir_for_symlink_after_first_check(
        monkeypatch, root / "rules", outside,
    )

    try:
        documents = import_documents(root, ["rules/policy.md"])
    except EditorInteropError:
        documents = ()
    assert state["swapped"]
    assert all(d.content != "secret" for d in documents)


def test_export_does_not_write_through_a_component_swapped_after_the_check(
    tmp_path, monkeypatch,
):
    _symlinks_or_skip(tmp_path)
    root = tmp_path / "root"
    (root / "rules").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "policy.md").write_text("victim", encoding="utf-8")
    state = _swap_dir_for_symlink_after_first_check(
        monkeypatch, root / "rules", outside,
    )

    with pytest.raises(EditorInteropError):
        export_documents(root, [RuleDocument("rules/policy.md", "attacker", "md")])

    assert state["swapped"]
    assert (outside / "policy.md").read_bytes() == b"victim"
    assert sorted(p.name for p in outside.iterdir()) == ["policy.md"]


def test_export_replaces_an_existing_document_atomically(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "policy.md").write_text("old", encoding="utf-8")

    assert export_documents(
        tmp_path, [RuleDocument("rules/policy.md", "new\r\n", "md")]
    ) == ("rules/policy.md",)

    assert (tmp_path / "rules" / "policy.md").read_bytes() == b"new\r\n"
    assert sorted(p.name for p in (tmp_path / "rules").iterdir()) == ["policy.md"]


def test_import_rejects_non_utf8_as_a_protocol_error(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe bad")
    with pytest.raises(EditorInteropError):
        import_documents(tmp_path, ["AGENTS.md"])

