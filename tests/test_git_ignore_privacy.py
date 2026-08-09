import os
import subprocess

import pytest

import file_ops
import server
import workbench
from sonder_runtime.adapters import git_discovery


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _repository(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "--quiet", cwd=root)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: tmp_path / "home")
    return root


def test_discovery_surfaces_hide_git_ignored_names_content_and_aggregates(
    monkeypatch, tmp_path,
):
    root = _repository(monkeypatch, tmp_path)
    (root / ".gitignore").write_text(
        "private-models/\n*.secret\nnested/*\n!nested/keep.txt\n",
        encoding="utf-8",
    )
    (root / ".git" / "info" / "exclude").write_text("private-corpus/\n", encoding="utf-8")
    (root / "private-models").mkdir()
    (root / "private-models" / "weights.gguf").write_bytes(b"private-marker" * 20_000)
    (root / "private-models" / "train.py").write_text("private-marker\n", encoding="utf-8")
    (root / "private-corpus").mkdir()
    (root / "private-corpus" / "training.jsonl").write_bytes(b"private-marker" * 10_000)
    (root / "token.secret").write_text("private-marker\n", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "hidden.txt").write_text("private-marker\n", encoding="utf-8")
    (root / "nested" / "keep.txt").write_text("public-marker\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("public-marker\n", encoding="utf-8")

    inventory = workbench.workspace_inventory(".", top_n=50)
    tree = workbench.directory_tree(".", depth=4, max_entries=100)
    search = workbench.text_search("private-marker", root=".")
    scripts = workbench.script_search("*", root=".")
    found = file_ops.find_files("*", root=".", max_results=200)

    serialized = repr((inventory, tree, search, scripts, found))
    for private_name in ("private-models", "weights.gguf", "train.py", "private-corpus", "training.jsonl", "token.secret", "hidden.txt"):
        assert private_name not in serialized
    assert "src" in serialized
    assert "main.py" in serialized
    assert "nested" in serialized
    assert "keep.txt" in serialized
    assert search["matches"] == []
    assert inventory["bytes"] < 10_000
    assert all(row["reason"] != "git_ignored" for row in inventory["skipped_examples"])


def test_nested_ignore_negation_and_subroot_are_exact(monkeypatch, tmp_path):
    root = _repository(monkeypatch, tmp_path)
    package = root / "packages" / "demo"
    package.mkdir(parents=True)
    (package / ".gitignore").write_text("*.private\n!visible.private\n", encoding="utf-8")
    (package / "hidden.private").write_text("hidden-marker\n", encoding="utf-8")
    (package / "visible.private").write_text("visible-marker\n", encoding="utf-8")
    (package / "ordinary.txt").write_text("ordinary-marker\n", encoding="utf-8")

    inventory = workbench.workspace_inventory(str(package), top_n=20)
    tree = workbench.directory_tree(str(package), depth=2)
    visible = workbench.text_search("visible-marker", root=str(package), glob="*.private")
    hidden = workbench.text_search("hidden-marker", root=str(package), glob="*.private")

    assert "hidden.private" not in repr((inventory, tree, visible, hidden))
    assert {row["relative"] for row in visible["matches"]} == {"visible.private"}
    assert hidden["matches"] == []
    assert inventory["files"] == 2  # hidden .gitignore plus both non-ignored files


def test_git_discovery_failure_is_fail_closed_before_filesystem_scan(
    monkeypatch, tmp_path,
):
    root = _repository(monkeypatch, tmp_path)
    (root / "private.txt").write_text("private\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise git_discovery.GitDiscoveryError("synthetic failure")

    def scanned(*_args, **_kwargs):
        raise AssertionError("filesystem scan ran after Git discovery failure")

    monkeypatch.setattr(git_discovery, "_run_bounded", fail)
    monkeypatch.setattr(workbench.os, "scandir", scanned)
    output = server.workspace_inventory()
    assert output == "ERROR: synthetic failure"


def test_git_discovery_output_truncation_is_explicit(monkeypatch, tmp_path):
    root = _repository(monkeypatch, tmp_path)
    (root / "visible.txt").write_text("visible\n", encoding="utf-8")

    with pytest.raises(git_discovery.GitDiscoveryError, match="bounded limit"):
        git_discovery.visible_paths(root, output_limit=1)


def test_hostile_ambient_git_redirection_and_global_ignore_are_scrubbed(
    monkeypatch, tmp_path,
):
    root = _repository(monkeypatch, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "--quiet", cwd=other)
    global_ignore = tmp_path / "global-ignore"
    global_ignore.write_text("visible.txt\n", encoding="utf-8")
    global_config = tmp_path / "hostile.gitconfig"
    global_config.write_text(
        "[core]\n\texcludesFile = %s\n" % global_ignore.as_posix(), encoding="utf-8",
    )
    (root / "visible.txt").write_text("visible\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.excludesFile")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(global_ignore))

    assert "visible.txt" in git_discovery.visible_paths(root)


def test_non_git_fallback_and_symlink_non_traversal(monkeypatch, tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private-marker\n", encoding="utf-8")
    (root / "ordinary.txt").write_text("ordinary-marker\n", encoding="utf-8")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: tmp_path / "home")

    inventory = workbench.workspace_inventory(".")
    search = workbench.text_search("private-marker", root=".")
    assert inventory["files"] == 1
    assert search["matches"] == []
    assert inventory["skipped_by_reason"]["symlink"] == 1


@pytest.mark.parametrize("tool", sorted(server._GIT_IGNORE_DISCOVERY_TOOLS))
def test_repository_and_autopilot_policy_deny_include_ignored(tool):
    repository_error = server._repository_read_only_error(tool, {"include_ignored": True})
    autopilot_error = server._autopilot_tool_policy({"policy": "observe"})(
        tool, {"include_ignored": True},
    )
    assert "forbids include_ignored=true" in repository_error
    assert "cannot set include_ignored=true" in autopilot_error


def test_direct_include_ignored_requires_developer_and_then_is_explicit(
    monkeypatch, tmp_path,
):
    root = _repository(monkeypatch, tmp_path)
    (root / ".gitignore").write_text("private/\n", encoding="utf-8")
    (root / "private").mkdir()
    (root / "private" / "weights.gguf").write_bytes(b"x" * 100)

    denied = server.workspace_inventory(include_ignored=True)
    assert "requires an explicitly authenticated developer" in denied
    monkeypatch.setattr(server, "_file_developer_allowed", lambda _token="": True)
    allowed = server.workspace_inventory(include_ignored=True, token="developer-token")
    assert "weights.gguf" in allowed
