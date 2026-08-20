import os
import subprocess
import sys
import time

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import server
import sonder_runtime.adapters.filesystem.workbench as workbench
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


def test_tracked_file_under_ignored_parent_remains_git_visible(monkeypatch, tmp_path):
    root = _repository(monkeypatch, tmp_path)
    tracked = root / "generated" / "tracked.txt"
    tracked.parent.mkdir()
    tracked.write_text("tracked-marker\n", encoding="utf-8")
    _git("add", "-f", "generated/tracked.txt", cwd=root)
    (root / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (root / "generated" / "private.bin").write_text("private-marker\n", encoding="utf-8")

    inventory = workbench.workspace_inventory(".", include_hidden=True)
    search = workbench.text_search("tracked-marker", root=".")
    serialized = repr((inventory, search))
    assert "tracked.txt" in serialized
    assert "private.bin" not in serialized


def test_linked_worktree_gitfile_uses_its_exact_worktree(monkeypatch, tmp_path):
    main = _repository(monkeypatch, tmp_path)
    _git("config", "user.name", "Sonder Test", cwd=main)
    _git("config", "user.email", "sonder-test@example.invalid", cwd=main)
    (main / "tracked.txt").write_text("tracked-marker\n", encoding="utf-8")
    (main / ".gitignore").write_text("private/\n", encoding="utf-8")
    _git("add", ".", cwd=main)
    _git("commit", "--quiet", "-m", "fixture", cwd=main)
    linked = tmp_path / "linked-worktree"
    _git("worktree", "add", "--quiet", "--detach", str(linked), cwd=main)
    (linked / "private").mkdir()
    (linked / "private" / "weights.gguf").write_bytes(b"x" * 1000)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: linked)

    inventory = workbench.workspace_inventory(".", include_hidden=True)
    assert "tracked.txt" in repr(inventory)
    assert "private" not in repr(inventory)
    assert "weights.gguf" not in repr(inventory)


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


def test_bounded_runner_timeout_and_stderr_do_not_leak_details():
    started = time.monotonic()
    with pytest.raises(git_discovery.GitDiscoveryError, match="timed out"):
        git_discovery._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout_seconds=0.05,
            output_limit=1024,
        )
    assert time.monotonic() - started < 2
    with pytest.raises(git_discovery.GitDiscoveryError) as failure:
        git_discovery._run_bounded(
            [
                sys.executable, "-c",
                "import sys; sys.stderr.write('ignored-private-name'); sys.exit(2)",
            ],
            timeout_seconds=1,
            output_limit=1024,
        )
    assert "ignored-private-name" not in str(failure.value)


def test_repository_identity_cannot_escape_nearest_git_marker(monkeypatch, tmp_path):
    root = _repository(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    metadata = (str(outside) + "\n" + str(outside / ".git") + "\n").encode()
    monkeypatch.setattr(git_discovery, "_run_bounded", lambda *_args, **_kwargs: metadata)

    with pytest.raises(git_discovery.GitDiscoveryError, match="outside"):
        git_discovery.visible_paths(root)


def test_git_identity_decode_failure_is_generic_and_precedes_scan(monkeypatch, tmp_path):
    _repository(monkeypatch, tmp_path)
    monkeypatch.setattr(git_discovery, "_run_bounded", lambda *_args, **_kwargs: b"\xff")
    monkeypatch.setattr(
        workbench.os, "scandir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scanned")),
    )

    output = server.workspace_inventory()
    assert output == "ERROR: Git repository identity was not valid UTF-8"


def test_changed_visibility_snapshot_discards_results(monkeypatch, tmp_path):
    root = _repository(monkeypatch, tmp_path)
    target = root / "visible.txt"
    target.write_text("private-marker\n", encoding="utf-8")
    real_visible_paths = git_discovery.visible_paths
    calls = {"count": 0}

    def change_after_snapshot(path, **kwargs):
        snapshot = real_visible_paths(path, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            (root / ".gitignore").write_text("visible.txt\n", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(git_discovery, "visible_paths", change_after_snapshot)
    output = server.text_search("private-marker")
    assert output == "ERROR: Git visibility changed during filesystem scan"
    assert "visible.txt" not in output


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


def test_repository_filter_and_fsmonitor_commands_are_not_executed(
    monkeypatch, tmp_path,
):
    root = _repository(monkeypatch, tmp_path)
    sentinel = tmp_path / "executed.txt"
    probe = tmp_path / ("probe.cmd" if os.name == "nt" else "probe.sh")
    if os.name == "nt":
        probe.write_text("@echo executed>\"%s\"\n" % sentinel, encoding="utf-8")
    else:
        probe.write_text("#!/bin/sh\nprintf executed > '%s'\n" % sentinel, encoding="utf-8")
        probe.chmod(0o700)
    _git("config", "core.fsmonitor", str(probe), cwd=root)
    _git("config", "filter.hostile.process", str(probe), cwd=root)
    (root / ".gitattributes").write_text("* filter=hostile\n", encoding="utf-8")
    (root / "visible.txt").write_text("visible\n", encoding="utf-8")

    assert "visible.txt" in git_discovery.visible_paths(root)
    assert not sentinel.exists()


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
    found = file_ops.find_files("*", root=".")
    assert inventory["files"] == 1
    assert search["matches"] == []
    assert inventory["skipped_by_reason"]["symlink"] == 1
    assert "linked" not in repr(found)


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
