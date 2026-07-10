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
