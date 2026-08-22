from __future__ import annotations

import subprocess

import server
from sonder_runtime.platform import version


def test_running_source_commit_reads_current_head(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(version.subprocess, "run", fake_run)

    assert version.running_source_commit_at_import(tmp_path) == "a" * 40
    assert calls[0][0] == ["git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD"]
    assert calls[0][1]["check"] is False


def test_running_source_commit_returns_empty_for_git_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        version.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 128, "", "missing"),
    )

    assert version.running_source_commit_at_import(tmp_path) == ""


def test_server_helper_is_compatibility_delegate(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        server,
        "_running_source_commit",
        lambda root: seen.append(root) or "b" * 40,
    )

    assert server._running_source_commit_at_import() == "b" * 40
    assert seen == [server.Path(server.__file__).resolve().parent]
