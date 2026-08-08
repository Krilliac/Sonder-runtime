from __future__ import annotations

import json
import os

import pytest

import file_ops
import server


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_APPROVAL_CODE", raising=False)
    return root


def test_context_pack_has_deterministic_headers_and_per_file_errors(workspace):
    (workspace / "first.txt").write_text("alpha", encoding="utf-8")
    (workspace / "second.txt").write_text("beta", encoding="utf-8")

    out = server.context_pack(
        json.dumps(["first.txt", "missing.txt", "second.txt"]),
        max_total_bytes=100,
        max_bytes_per_file=100,
    )

    first = "===== CONTEXT FILE 1/3: first.txt ====="
    missing = "===== CONTEXT FILE 2/3: missing.txt ====="
    second = "===== CONTEXT FILE 3/3: second.txt ====="
    assert out.index(first) < out.index(missing) < out.index(second)
    assert "status: error" in out
    assert "FileNotFoundError: file not found" in out
    assert "alpha" in out and "beta" in out
    assert "errors=1" in out


def test_context_pack_enforces_file_count_and_body_budgets(workspace):
    (workspace / "a.txt").write_text("abcdef", encoding="utf-8")
    (workspace / "b.txt").write_text("ghijkl", encoding="utf-8")
    (workspace / "c.txt").write_text("mnopqr", encoding="utf-8")

    out = server.context_pack(
        ["a.txt", "b.txt", "c.txt"],
        max_files=2,
        max_total_bytes=7,
        max_bytes_per_file=4,
    )

    assert "requested=3 selected=2 emitted-bytes=7" in out
    assert "included-bytes: 4" in out
    assert "included-bytes: 3" in out
    assert "truncated: true (per-file byte cap)" in out
    assert "truncated: true (total byte budget)" in out
    assert "pack-truncated: true (1 file(s) omitted by max-files)" in out
    assert "mnopqr" not in out


def test_context_pack_utf8_output_never_exceeds_body_budget(workspace):
    (workspace / "unicode.txt").write_text("ééé", encoding="utf-8")

    out = server.context_pack(
        ["unicode.txt"], max_total_bytes=3, max_bytes_per_file=3,
    )

    assert "emitted-bytes=2" in out
    assert "included-bytes: 2" in out
    assert out.endswith("\né")


def test_context_pack_reports_sensitive_and_outside_paths_without_contents(
    workspace, tmp_path,
):
    (workspace / ".env").write_text("TOKEN=secret-value", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret", encoding="utf-8")

    out = server.context_pack([".env", str(outside)])

    assert out.count("status: error") == 2
    assert "protected Sonder secret/control-plane" in out
    assert "outside allowed roots" in out
    assert "secret-value" not in out
    assert "outside-secret" not in out


def test_context_pack_rejects_symlink_escape(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("symlink-secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlink creation is unavailable: %s" % exc)

    out = server.context_pack(["link.txt"])

    assert "status: error" in out
    assert "outside allowed roots" in out
    assert "symlink-secret" not in out


def test_project_scoped_read_only_agent_rebases_every_context_path(
    monkeypatch, tmp_path,
):
    sonder_workspace = tmp_path / "sonder"
    project = tmp_path / "project"
    home = tmp_path / "home"
    sonder_workspace.mkdir()
    project.mkdir()
    home.mkdir()
    (sonder_workspace / "one.txt").write_text("wrong one", encoding="utf-8")
    (project / "one.txt").write_text("project one", encoding="utf-8")
    (project / "two.txt").write_text("project two", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: sonder_workspace)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: home)

    out = server._agent_dispatch(
        "context_pack",
        {"paths_json": ["one.txt", "two.txt"]},
        read_only=True,
        repository_extra_roots=str(project),
    )

    assert "project one" in out and "project two" in out
    assert "wrong one" not in out


def test_project_scoped_context_pack_rejects_escape_before_handler(
    monkeypatch, tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    called = []
    monkeypatch.setattr(server, "context_pack", lambda *a, **k: called.append((a, k)))

    out = server._agent_dispatch(
        "context_pack",
        {"paths_json": ["inside.txt", str(outside)]},
        read_only=True,
        repository_extra_roots=str(project),
    )

    assert out.startswith("ERROR:")
    assert "outside the host-selected project root" in out
    assert called == []


def test_context_pack_is_advertised_and_read_only():
    assert "context_pack" in server.tool_manifest()
    assert "context_pack" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "- context_pack:" in server._agent_tool_help(read_only=True)


@pytest.mark.parametrize("value", ["not-json", {}, [], ["ok.txt", 3]])
def test_context_pack_rejects_malformed_path_lists(value):
    out = server.context_pack(value)
    assert out.startswith("ERROR: paths_json")
