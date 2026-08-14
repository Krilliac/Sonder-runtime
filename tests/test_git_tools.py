import subprocess
import sys
from datetime import datetime

import pytest

import git_tools
import server
import sonder_serve
import sonder_repl


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Sonder Tests")
    _git(repo, "config", "user.email", "sonder-tests@example.invalid")
    (repo / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _status(repo, **kwargs):
    return git_tools.repo_status(str(repo), bypass=True, **kwargs)


def _diff(repo, **kwargs):
    return git_tools.repo_diff(str(repo), bypass=True, **kwargs)


def test_repo_status_reports_branch_clean_dirty_and_untracked(tmp_path):
    repo = _repo(tmp_path)
    clean = _status(repo)
    assert clean["branch"] == "main"
    assert clean["clean"] is True
    assert clean["change_count"] == 0

    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new file.txt").write_text("new\n", encoding="utf-8")
    dirty = _status(repo)
    assert dirty["clean"] is False
    assert dirty["change_count"] == 2
    assert any("tracked.txt" in row for row in dirty["entries"])
    assert any("new file.txt" in row for row in dirty["entries"])


def test_repo_status_reports_detached_head(tmp_path):
    repo = _repo(tmp_path)
    oid = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach", oid)
    result = _status(repo)
    assert result["detached"] is True
    assert result["branch"] == ""
    assert result["oid"] == oid


def test_truncated_status_never_claims_the_worktree_is_clean(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setattr(git_tools, "_require_repository_root", lambda *a, **k: repo)
    monkeypatch.setattr(git_tools, "_checked_git", lambda *a, **k: {
        "stdout": "# branch.oid abc\n# branch.head main\n",
        "stderr": "", "returncode": 0, "timed_out": False,
        "elapsed_ms": 1, "truncated": True,
        "output_bytes": 500, "output_limit": 40,
    })

    result = _status(repo, max_output=40)

    assert result["truncated"] is True
    assert result["complete"] is False
    assert result["clean"] is None


def test_repo_diff_separates_unstaged_staged_and_path_scope(tmp_path):
    repo = _repo(tmp_path)
    other = repo / "other.txt"
    other.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "other")

    (repo / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    other.write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "other.txt")

    unstaged = _diff(repo, path="tracked.txt")
    assert "+unstaged" in unstaged["diff"]
    assert "other.txt" not in unstaged["diff"]
    staged = _diff(repo, staged=True, path="other.txt", context=0)
    assert "+staged" in staged["diff"]
    assert staged["context"] == 0
    assert staged["staged"] is True


def test_repo_diff_preserves_tracked_symlink_pathspec(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a").write_text("a", encoding="utf-8")
    (repo / "b").write_text("b", encoding="utf-8")
    link = repo / "link"
    try:
        link.symlink_to("a")
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    _git(repo, "add", "a", "b", "link")
    _git(repo, "commit", "-m", "add symlink")
    link.unlink()
    link.symlink_to("../outside-target")

    result = _diff(repo, path="link")

    assert result["path"] == "link"
    assert "diff --git a/link b/link" in result["diff"]
    assert "outside-target" in result["diff"]

    agent_output = server._agent_dispatch(
        "repo_diff", {"root": ".", "path": "link"}, read_only=True,
        repository_extra_roots=str(repo),
    )
    assert not agent_output.startswith("ERROR:")
    assert "diff --git a/link b/link" in agent_output


def test_repo_diff_neutralizes_repository_clean_filter(tmp_path):
    repo = _repo(tmp_path)
    attributes = repo / ".gitattributes"
    attributes.write_text("*.txt filter=hostile\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "attributes")

    marker = tmp_path / "filter-ran.txt"
    script = tmp_path / "filter.py"
    script.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(%r).write_text('ran', encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n" % str(marker),
        encoding="utf-8",
    )
    command = '"%s" "%s"' % (
        str(sys.executable).replace("\\", "/"),
        str(script).replace("\\", "/"),
    )
    _git(repo, "config", "filter.hostile.clean", command)
    _git(repo, "config", "filter.hostile.required", "true")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = _diff(repo, path="tracked.txt")

    assert "+changed" in result["diff"]
    assert not marker.exists()


def test_repo_diff_has_a_shared_hard_output_cap(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text(
        "\n".join("changed-%04d" % index for index in range(2000)) + "\n",
        encoding="utf-8",
    )
    result = _diff(repo, max_output=200)
    assert result["truncated"] is True
    assert result["output_limit"] == 200
    assert len(result["diff"].encode("utf-8")) <= 200
    assert result["output_bytes"] > result["output_limit"]


def test_repo_tools_reject_non_repo_and_upward_top_level_discovery(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="root probe failed"):
        git_tools.repo_status(str(plain), bypass=True)

    repo = _repo(tmp_path)
    child = repo / "src"
    child.mkdir()
    with pytest.raises(PermissionError, match="top-level"):
        git_tools.repo_status(str(child), bypass=True)


@pytest.mark.parametrize("path", ["../outside.txt", ".git/config"])
def test_repo_diff_rejects_escape_and_git_control_state(tmp_path, path):
    repo = _repo(tmp_path)
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        _diff(repo, path=path)


def test_git_processes_are_argv_only_noninteractive_and_lock_free(
    monkeypatch, tmp_path,
):
    repo = _repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.status")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!echo unsafe")
    monkeypatch.setenv("GIT_ASKPASS", "hostile-git-askpass")
    # Use lower case to cover Windows' case-insensitive environment namespace.
    monkeypatch.setenv("ssh_askpass", "hostile-ssh-askpass")
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
    monkeypatch.setenv("GCM_INTERACTIVE", "always")
    real_popen = git_tools.subprocess.Popen
    calls = []

    def observed_popen(command, **kwargs):
        calls.append((command, kwargs))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(git_tools.subprocess, "Popen", observed_popen)
    _status(repo)
    assert calls
    for command, kwargs in calls:
        assert isinstance(command, list)
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["SSH_ASKPASS_REQUIRE"] == "never"
        assert kwargs["env"]["GCM_INTERACTIVE"] == "never"
        assert "GIT_CONFIG_COUNT" not in kwargs["env"]
        assert "GIT_CONFIG_KEY_0" not in kwargs["env"]
        assert "GIT_ASKPASS" not in kwargs["env"]
        assert "SSH_ASKPASS" not in kwargs["env"]


def test_timeout_is_bounded_and_reported(monkeypatch, tmp_path):
    repo = _repo(tmp_path)

    def timed_out(*args, **kwargs):
        return {
            "command": ["git"], "returncode": -1, "timed_out": True,
            "elapsed_ms": 1000, "stdout": "", "stderr": "",
            "output_bytes": 0, "output_limit": 10, "truncated": False,
        }

    monkeypatch.setattr(git_tools, "_run_git", timed_out)
    with pytest.raises(TimeoutError, match="30 second"):
        git_tools.repo_status(str(repo), timeout=999, bypass=True)


def test_repository_agent_scopes_git_tools_to_host_project(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    calls = []

    def fake_status(**kwargs):
        calls.append(kwargs)
        return "scoped:%s" % kwargs["root"]

    monkeypatch.setattr(server, "repo_status", fake_status)
    out = server._agent_dispatch(
        "repo_status", {"root": "."}, read_only=True,
        repository_extra_roots=str(repo),
    )
    assert out == "scoped:%s" % repo
    assert calls[0]["extra_roots"] == str(repo)
    assert calls[0]["approval"] == server._TRUSTED_REPOSITORY_APPROVAL

    calls.clear()
    denied = server._agent_dispatch(
        "repo_status", {"root": ".."}, read_only=True,
        repository_extra_roots=str(repo),
    )
    assert denied.startswith("ERROR: agent project path rejected:")
    assert calls == []


def test_repository_agent_rejects_diff_path_escape_before_dispatch(
    monkeypatch, tmp_path,
):
    repo = _repo(tmp_path)
    calls = []
    monkeypatch.setattr(
        server, "repo_diff", lambda **kwargs: calls.append(kwargs) or "unexpected",
    )
    denied = server._agent_dispatch(
        "repo_diff", {"root": ".", "path": "../outside.txt"},
        read_only=True, repository_extra_roots=str(repo),
    )
    assert denied.startswith("ERROR: agent project path rejected:")
    assert calls == []


def test_git_tools_are_advertised_and_allowed_for_read_only_agents():
    help_text = server._agent_tool_help(read_only=True)
    assert "- repo_status:" in help_text
    assert "- repo_diff:" in help_text
    assert {"repo_status", "repo_diff"} <= server.REPOSITORY_READ_ONLY_TOOLS
    manifest = server.tool_manifest()
    assert "repo_status/repo_diff" in manifest


def test_runtime_update_status_reports_ahead_behind_and_commit_times(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    (repo / "tracked.txt").write_text("local ahead\n", encoding="utf-8")
    _git(repo, "commit", "-am", "ahead")
    result = git_tools.runtime_update_status(repo, refresh=False)

    assert result["ahead"] == 1
    assert result["behind"] == 0
    assert result["state"] == "ahead"
    assert result["installed_commit"] != result["newest_commit"]
    assert result["remote"] == str(remote)
    assert datetime.fromisoformat(result["installed_commit_time"]).tzinfo is not None
    assert datetime.fromisoformat(result["newest_commit_time"]).tzinfo is not None
    assert result["trusted_remote"] is False


def test_runtime_checkout_commit_reads_only_the_current_head(tmp_path):
    repo = _repo(tmp_path)

    assert git_tools.runtime_checkout_commit(repo) == _git(repo, "rev-parse", "HEAD").strip()


def test_runtime_update_refuses_untrusted_or_dirty_checkout(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    with pytest.raises(PermissionError, match="canonical Sonder origin"):
        git_tools.runtime_update(repo)


def test_runtime_update_status_refuses_untrusted_origin_before_fetch(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://example.invalid/not-sonder.git")
    calls = []
    real_checked = git_tools._checked_git

    def observed(root, arguments, **kwargs):
        calls.append(list(arguments))
        return real_checked(root, arguments, **kwargs)

    monkeypatch.setattr(git_tools, "_checked_git", observed)
    with pytest.raises(PermissionError, match="canonical Sonder origin"):
        git_tools.runtime_update_status(repo, refresh=True)
    assert not any("fetch" in call for call in calls)


def test_runtime_update_neutralizes_checkout_filter_processes(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://github.com/Krilliac/Sonder-runtime.git")
    _git(repo, "config", "filter.hostile.smudge", "unsafe-smudge")
    _git(repo, "config", "filter.hostile.process", "unsafe-process")
    calls = []

    def fake_status(_root, **_kwargs):
        return {
            "root": str(repo), "branch": "main", "installed_commit": "a",
            "newest_commit": "b", "ahead": 0, "behind": 1,
            "state": "behind", "clean": True, "trusted_remote": True,
        }

    def fake_checked(_root, arguments, **_kwargs):
        calls.append(list(arguments))
        return {"stdout": "", "stderr": "", "returncode": 0, "timed_out": False,
                "elapsed_ms": 1, "truncated": False, "output_bytes": 0, "output_limit": 65536}

    monkeypatch.setattr(git_tools, "runtime_update_status", fake_status)
    monkeypatch.setattr(git_tools, "_checked_git", fake_checked)
    monkeypatch.setattr(git_tools, "_require_repository_root", lambda *_args, **_kwargs: repo)
    monkeypatch.setattr(
        git_tools, "_runtime_remote_url",
        lambda _root: "https://github.com/Krilliac/Sonder-runtime.git",
    )
    # Keep the real filter probe, which uses _run_git rather than _checked_git.
    result = git_tools.runtime_update(repo)
    merge = next(call for call in calls if "merge" in call)
    assert "filter.hostile.process=" in merge
    assert "filter.hostile.smudge=cat" in merge
    assert "--no-overwrite-ignore" in merge
    assert result["updated"] is True


def test_runtime_source_update_tools_format_and_do_not_hide_refusal(monkeypatch):
    data = {
        "root": "C:/Sonder-runtime",
        "branch": "main",
        "installed_commit": "a" * 40,
        "installed_commit_time": "2026-08-13T01:00:00Z",
        "newest_commit": "b" * 40,
        "newest_commit_time": "2026-08-13T02:00:00Z",
        "ahead": 0, "behind": 3, "state": "behind", "clean": True,
        "remote": "https://github.com/Krilliac/Sonder-runtime.git",
        "trusted_remote": True, "checked_at": "2026-08-13T03:00:00Z",
    }
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "runtime_source_update_status_data", lambda refresh=True: data)
    text = server.runtime_source_update_status()
    assert "aaaaaaaaaaaa" in text
    assert "bbbbbbbbbbbb" in text
    assert "behind=3" in text
    assert "checkout: main" in text
    assert "source root: C:/Sonder-runtime" in text

    monkeypatch.setattr(
        server.git_tools, "runtime_update", lambda _root: {"updated": False, "after": data},
    )
    assert "already current" in server.runtime_source_update()
    assert server.control_command("/updatecheck").startswith("Sonder source update status:")
    assert "usage:" in server.control_command("/update check")
    assert not sonder_serve._dangerous_http_slash("/updatecheck")
    assert sonder_serve._dangerous_http_slash("/update")


def test_runtime_update_refusal_names_current_branch_and_safe_recovery(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    monkeypatch.setattr(git_tools, "_require_repository_root", lambda *_args, **_kwargs: repo)
    monkeypatch.setattr(
        git_tools, "_runtime_remote_url",
        lambda _root: "https://github.com/Krilliac/Sonder-runtime.git",
    )
    monkeypatch.setattr(
        git_tools, "runtime_update_status",
        lambda *_args, **_kwargs: {
            "branch": "feat/experiment", "clean": True, "ahead": 0,
            "state": "behind",
        },
    )

    with pytest.raises(PermissionError) as excinfo:
        git_tools.runtime_update(repo)

    message = str(excinfo.value)
    assert "requires branch 'main'" in message
    assert "current checkout: 'feat/experiment'" in message
    assert "switch the clean canonical checkout to 'main', then retry" in message


def test_update_status_distinguishes_running_source_from_new_checkout(monkeypatch):
    monkeypatch.setattr(server.git_tools, "runtime_update_status", lambda *_args, **_kwargs: {
        "installed_commit": "b" * 40,
    })
    monkeypatch.setattr(server, "RUNNING_SOURCE_COMMIT", "a" * 40)

    data = server.runtime_source_update_status_data(refresh=False)

    assert data["running_commit"] == "a" * 40
    assert data["restart_required"] is True
    assert "restart required" in server._runtime_update_format(data)


def test_repl_startup_banner_reads_cached_update_status(monkeypatch):
    seen = []
    monkeypatch.setattr(
        server, "runtime_source_update_status_data",
        lambda *, refresh: seen.append(refresh) or {
            "installed_commit": "a" * 40, "installed_commit_time": "now",
            "newest_commit": "b" * 40, "newest_commit_time": "later",
            "state": "behind", "behind": 1,
            "running_commit": "a" * 40, "restart_required": True,
        },
    )
    monkeypatch.setattr(sonder_repl, "_paint", lambda text, *_styles: str(text))
    banner = sonder_repl._startup_banner(False, "coder", "")
    assert seen == [False]
    assert "installed source" in banner
    assert "running source" in banner
    assert "restart required" in banner
    assert "/updatecheck | /update" in banner
