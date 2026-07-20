import os

import pytest

import file_ops
import server


def test_read_only_denies_mutation_before_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "task_create", lambda *a, **k: calls.append((a, k)))
    assert server._agent_dispatch("task_create", {"title": "x"}, read_only=True).startswith("ERROR:")
    assert calls == []


def test_read_only_denies_bypass_args(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "file_read", lambda *a, **k: calls.append((a, k)))
    out = server._agent_dispatch(
        "file_read", {"path": "README.md", "token": "x", "extra_roots": "C:\\"}, read_only=True
    )
    assert out.startswith("ERROR:")
    assert calls == []


def test_read_only_denies_untrusted_extra_root_without_token(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "file_read", lambda *a, **k: calls.append((a, k)))

    out = server._agent_dispatch(
        "file_read", {"path": "outside.txt", "extra_roots": "C:\\"}, read_only=True
    )

    assert out.startswith("ERROR:")
    assert "extra_roots" in out
    assert calls == []


def test_project_scope_replaces_model_supplied_extra_roots():
    # Host-portable: the host-selected project root must replace the
    # model-supplied extra_roots verbatim, and a relative path must rebase onto
    # it using the host's own path separator (os.path.join). Asserting a
    # hard-coded Windows separator would spuriously fail on POSIX, where
    # pathlib/os.path join with "/".
    project = os.path.join("host-project", "sub")
    scoped = server._project_scope_args(
        "file_read",
        {"path": "README.md", "extra_roots": os.path.join("model-chosen", "elsewhere")},
        project,
    )

    assert scoped["extra_roots"] == project
    assert scoped["path"] == os.path.join(project, "README.md")


def test_project_scoped_read_only_dispatch_reads_host_authorized_root(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    target = project / "answer.txt"
    target.write_text("trusted cross-root read", encoding="utf-8")
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: workspace)

    out = server._agent_dispatch_observed(
        "file_read_range",
        {"path": "answer.txt", "start_line": 1, "end_line": 5},
        read_only=True,
        project=str(project),
    )

    assert "trusted cross-root read" in out


def test_direct_project_dispatch_rebases_relative_path_before_file_handler(
    monkeypatch, tmp_path,
):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    (workspace / "answer.txt").write_text("wrong cwd evidence", encoding="utf-8")
    (project / "answer.txt").write_text("requested project evidence", encoding="utf-8")
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: workspace)

    out = server._agent_dispatch(
        "file_read_range",
        {"path": "answer.txt", "start_line": 1, "end_line": 5},
        read_only=True,
        repository_extra_roots=str(project),
    )

    assert "requested project evidence" in out
    assert "wrong cwd evidence" not in out


def test_project_scoped_read_rejects_sonder_workspace_even_when_normally_authorized(
    monkeypatch, tmp_path,
):
    workspace = tmp_path / "sonder-workspace"
    project = tmp_path / "requested-project"
    workspace.mkdir()
    project.mkdir()
    outside = workspace / "server.py"
    outside.write_text("wrong repository", encoding="utf-8")
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: workspace)
    calls = []
    monkeypatch.setattr(server, "file_read", lambda *a, **k: calls.append((a, k)))

    out = server._agent_dispatch_observed(
        "file_read",
        {"path": str(outside)},
        read_only=True,
        project=str(project),
    )

    assert out.startswith("ERROR:")
    assert "outside the host-selected project root" in out
    assert calls == []


def test_read_only_allows_guarded_read(monkeypatch):
    monkeypatch.setattr(server, "file_read", lambda path, **kwargs: "read:" + path)
    assert server._agent_dispatch("file_read", {"path": "README.md"}, read_only=True) == "read:README.md"


def test_read_only_allows_normal_top_level_source(monkeypatch):
    monkeypatch.setattr(server, "file_read", lambda path, **kwargs: "read:" + path)
    assert server._agent_dispatch(
        "file_read", {"path": "server.py"}, read_only=True
    ) == "read:server.py"


@pytest.mark.parametrize("path", ["C:\\outside.txt", "../outside.txt", "~/.env"])
def test_read_only_rejects_absolute_or_escaping_read(monkeypatch, path):
    calls = []
    monkeypatch.setattr(server, "file_read", lambda *a, **k: calls.append((a, k)))

    out = server._agent_dispatch("file_read", {"path": path}, read_only=True)

    assert out.startswith("ERROR:")
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [".env", "permissions.json", "memory.db", ".git/config", "nested/secrets.json"],
)
def test_read_only_rejects_secret_or_control_state(monkeypatch, path):
    calls = []
    monkeypatch.setattr(server, "file_read", lambda *a, **k: calls.append((a, k)))

    out = server._agent_dispatch("file_read", {"path": path}, read_only=True)

    assert out.startswith("ERROR:")
    assert calls == []


@pytest.mark.parametrize("root", ["C:\\", "..", "../outside"])
def test_read_only_rejects_absolute_or_escaping_find_root(monkeypatch, root):
    calls = []
    monkeypatch.setattr(server, "file_find", lambda *a, **k: calls.append((a, k)))

    out = server._agent_dispatch("file_find", {"root": root}, read_only=True)

    assert out.startswith("ERROR:")
    assert calls == []


def test_read_only_allows_workspace_find_root(monkeypatch):
    monkeypatch.setattr(server, "file_find", lambda **kwargs: "find:" + kwargs["root"])
    assert server._agent_dispatch(
        "file_find", {"root": ".", "query": "*.py"}, read_only=True
    ) == "find:."


def test_read_only_help_is_filtered():
    help_text = server._agent_tool_help(read_only=True)
    advertised = {line[2:].split(":", 1)[0] for line in help_text.splitlines() if line.startswith("- ")}
    assert advertised == set(server.REPOSITORY_READ_ONLY_TOOLS)
    assert "run_code" not in help_text and "file_write" not in help_text
