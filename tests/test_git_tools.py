import subprocess
import sys
from datetime import datetime

import pytest

import command_router
import git_tools
import server
import sonder_runtime.interfaces.http.serve as sonder_serve
from sonder_runtime.adapters import operator_attendance
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


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


def _runtime_repo(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://github.com/Krilliac/Sonder-runtime.git")
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


@pytest.mark.requires_medium_integrity
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
    assert result["remote_ref_refreshed"] is False


def test_runtime_update_status_marks_only_explicit_fetches_as_refreshed(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://github.com/Krilliac/Sonder-runtime.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    calls = []
    real_checked = git_tools._checked_git

    def observed(root, arguments, **kwargs):
        calls.append(list(arguments))
        if "fetch" in arguments:
            return {"stdout": "", "stderr": "", "returncode": 0, "timed_out": False,
                    "elapsed_ms": 1, "truncated": False, "output_bytes": 0, "output_limit": 16384}
        return real_checked(root, arguments, **kwargs)

    monkeypatch.setattr(git_tools, "_checked_git", observed)

    cached = git_tools.runtime_update_status(repo, refresh=False)
    refreshed = git_tools.runtime_update_status(repo, refresh=True)

    assert cached["remote_ref_refreshed"] is False
    assert refreshed["remote_ref_refreshed"] is True
    assert any("fetch" in call for call in calls)


def test_runtime_checkout_commit_reads_only_the_current_head(tmp_path):
    repo = _repo(tmp_path)

    assert git_tools.runtime_checkout_commit(repo) == _git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.requires_medium_integrity
def test_runtime_update_refuses_untrusted_or_dirty_checkout(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    with pytest.raises(PermissionError, match="canonical Sonder origin"):
        git_tools.runtime_update(repo)


def test_runtime_stash_saves_tracked_edits_without_stashing_untracked(tmp_path):
    repo = _runtime_repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("keep local\n", encoding="utf-8")

    before = git_tools.runtime_stash_status(repo)
    saved = git_tools.runtime_stash(repo, "save")

    assert before["clean"] is False
    assert saved["changed"] is True
    assert saved["after"]["stash_count"] == 1
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "first\n"
    assert (repo / "scratch.txt").exists()
    with pytest.raises(PermissionError, match="requires a clean"), operator_attendance.attended("runtime_source_stash"):
        git_tools.runtime_stash(repo, "pop")


def test_runtime_stash_save_untracked_and_clean_only_pop(tmp_path):
    repo = _runtime_repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("restore me\n", encoding="utf-8")

    saved = git_tools.runtime_stash(repo, "save-untracked")
    assert saved["after"]["clean"] is True
    with operator_attendance.attended("runtime_source_stash"):
        restored = git_tools.runtime_stash(repo, "pop")

    assert restored["changed"] is True
    assert restored["after"]["clean"] is False
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "changed\n"
    assert (repo / "scratch.txt").read_text(encoding="utf-8") == "restore me\n"


def test_runtime_stash_pop_leaves_an_unrelated_newer_stash_untouched(tmp_path):
    repo = _runtime_repo(tmp_path)
    (repo / "tracked.txt").write_text("recovery\n", encoding="utf-8")
    git_tools.runtime_stash(repo, "save")

    (repo / "tracked.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "stash", "push", "--message", "manual checkpoint")

    with operator_attendance.attended("runtime_source_stash"):
        restored = git_tools.runtime_stash(repo, "pop")

    assert restored["changed"] is True
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "recovery\n"
    listed = _git(repo, "stash", "list")
    assert "manual checkpoint" in listed
    assert "sonder runtime recovery" not in listed


def test_runtime_stash_refuses_untrusted_or_invalid_action(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://example.invalid/not-sonder.git")
    with pytest.raises(PermissionError, match="canonical Sonder origin"):
        git_tools.runtime_stash_status(repo)

    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    trusted = _runtime_repo(trusted_root)
    with pytest.raises(ValueError, match="must be save"):
        git_tools.runtime_stash(trusted, "drop")


def test_runtime_stash_commands_are_explicitly_gated(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.git_tools, "runtime_stash_status", lambda _root: {
        "clean": False, "change_count": 2, "stash_count": 1,
    })
    text = server.control_command("/stash")
    assert "changes: 2" in text
    assert "save-untracked" in text
    assert not sonder_serve._dangerous_http_slash("/stash")
    assert sonder_serve._dangerous_http_slash("/stash save")
    assert sonder_serve.SYSTEM_OPERATION_TOOLS["runtime_source_stash"] == "selfmod_deploy"


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


def test_runtime_update_fetch_neutralizes_credential_and_refspec_configuration(
    monkeypatch, tmp_path,
):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://github.com/Krilliac/Sonder-runtime.git")
    calls = []

    def fake_checked(_root, arguments, **_kwargs):
        calls.append(list(arguments))
        if "rev-parse" in arguments:
            return {"stdout": "a" * 40 + "\n", "elapsed_ms": 1}
        if "rev-list" in arguments:
            return {"stdout": "0 0\n", "elapsed_ms": 1}
        if "show" in arguments:
            return {"stdout": "2026-08-13T00:00:00Z\n", "elapsed_ms": 1}
        if "status" in arguments:
            return {
                "stdout": "# branch.oid " + "a" * 40 + "\n# branch.head main\n",
                "elapsed_ms": 1, "truncated": False, "output_bytes": 0,
                "output_limit": 16_384,
            }
        return {"stdout": "", "elapsed_ms": 1}

    monkeypatch.setattr(git_tools, "_checked_git", fake_checked)
    monkeypatch.setattr(git_tools, "_require_repository_root", lambda *_args, **_kwargs: repo)
    monkeypatch.setattr(
        git_tools, "_runtime_remote_url",
        lambda _root: "https://github.com/Krilliac/Sonder-runtime.git",
    )
    result = git_tools.runtime_update_status(repo, refresh=True)

    fetch = next(call for call in calls if "fetch" in call)
    assert "credential.helper=" in fetch
    assert "core.askPass=" in fetch
    assert "--refmap=" in fetch
    assert fetch[-1] == "+refs/heads/main:refs/remotes/origin/main"
    assert result["state"] == "current"


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

    real_spawn = git_tools._spawn_git

    def fake_spawn(root, arguments, **kwargs):
        if arguments[:1] == ["config"]:
            # Keep the real driver probe: the overrides come from real config.
            return real_spawn(root, arguments, **kwargs)
        calls.append(list(arguments))
        return {"stdout": "", "stderr": "", "returncode": 0, "timed_out": False,
                "elapsed_ms": 1, "truncated": False, "output_bytes": 0, "output_limit": 65536}

    monkeypatch.setattr(git_tools, "runtime_update_status", fake_status)
    monkeypatch.setattr(git_tools, "_spawn_git", fake_spawn)
    monkeypatch.setattr(git_tools, "_require_repository_root", lambda *_args, **_kwargs: repo)
    monkeypatch.setattr(
        git_tools, "_runtime_remote_url",
        lambda _root: "https://github.com/Krilliac/Sonder-runtime.git",
    )
    result = git_tools.runtime_update(repo)
    merge = next(call for call in calls if "merge" in call)
    assert "filter.hostile.process=" in merge
    assert "filter.hostile.smudge=cat" in merge
    assert "core.fsmonitor=false" in merge
    assert "--no-overwrite-ignore" in merge
    assert result["updated"] is True


def test_runtime_source_update_tools_format_and_do_not_hide_refusal(every_tool_allowed_by_rule, monkeypatch):
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
    assert "eligible; /update can fast-forward canonical main" in text

    monkeypatch.setattr(
        server.git_tools, "runtime_update", lambda _root: {"updated": False, "after": data},
    )
    assert "already current" in server.runtime_source_update()
    assert server.control_command("/updatecheck").startswith("Sonder source update status:")
    assert "usage:" in server.control_command("/update check")
    assert not sonder_serve._dangerous_http_slash("/updatecheck")
    assert sonder_serve._dangerous_http_slash("/update")


def test_natural_update_check_reuses_guarded_refresh_dispatch(monkeypatch):
    seen = {}

    def status(refresh=True):
        seen["refresh"] = refresh
        return "Sonder source update status: current"

    monkeypatch.setattr(server, "runtime_source_update_status", status)
    line = command_router.resolve("check whether Sonder is up to date")

    assert line == "/updatecheck"
    assert server.control_command(line) == "Sonder source update status: current"
    assert seen == {"refresh": True}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"trusted_remote": False}, "remote is not the canonical Sonder origin"),
        ({"branch": "feature/demo"}, "checkout must be 'main' (current: 'feature/demo')"),
        ({"clean": False}, "source checkout is dirty"),
        ({"ahead": 1}, "local commits require manual reconciliation"),
    ],
)
def test_runtime_update_status_explains_safe_refusal_before_approval(changes, expected):
    data = {
        "branch": "main", "trusted_remote": True, "clean": True,
        "ahead": 0, "state": "behind",
    }
    data.update(changes)

    text = server._runtime_update_format(data)

    assert "update: refused;" in text
    assert expected in text


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
    monkeypatch.setattr(sonder_repl.S, "_CACHED", sonder_repl.S.Caps())
    banner = sonder_repl._startup_banner(False, "coder", "")
    assert seen == [False]
    # REPL redesign spec 2.5: the banner keeps only what needs action; the
    # provenance moved to /about.
    assert "1 commit behind | /update" in banner
    assert "restart required | /restart" in banner
    about = "\n".join(sonder_repl.S.about_lines(
        sonder_repl._banner_state(False, "coder", ""), 100))
    assert seen == [False, False]
    assert "installed" in about and "a" * 12 in about
    assert "running" in about and "(restart required)" in about
    assert "newest known" in about and "b" * 12 in about
    assert "/updatecheck | /update" in about


def test_runtime_update_format_marks_cached_remote_ref_as_known():
    text = server._runtime_update_format({
        "installed_commit": "a" * 40,
        "newest_commit": "b" * 40,
        "remote_ref_refreshed": False,
    })

    assert "newest known origin/main" in text

SECRET_BODY = {
    ".env": "STRIPE_LIVE=sk_live_abcdef0123456789\n",
    "id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n",
    ".git-credentials": "https://user:hunter2token@example.invalid\n",
}
SECRET_MARKERS = ("sk_live_", "b3BlbnNzaC1rZXktdjEAAAAA", "hunter2token")


def _secret_repo(tmp_path):
    repo = _repo(tmp_path)
    (repo / "config").mkdir()
    files = dict(SECRET_BODY)
    files["ordinary.txt"] = "base\n"
    files["config/app.txt"] = "base\n"
    files["config/.env"] = "PGPASS=base\n"
    for name in files:
        (repo / name).write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-f", ".")
    _git(repo, "commit", "-m", "secrets")
    for name, body in files.items():
        if name == "config/.env":
            body = "PGPASS=changed-pgpass\n"
        else:
            body = body + "new-line\n"
        (repo / name).write_text(body, encoding="utf-8")
    return repo


def _assert_no_secret(text):
    for marker in SECRET_MARKERS + ("PGPASS", "changed-pgpass"):
        assert marker not in text


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("scope", ["", "config/", "config"])
def test_repo_diff_unscoped_or_directory_scope_withholds_secret_paths(
    tmp_path, staged, scope,
):
    repo = _secret_repo(tmp_path)
    if staged:
        _git(repo, "add", "-f", "-A")
    result = _diff(repo, staged=staged, path=scope, context=0)
    _assert_no_secret(result["diff"])
    assert "config/app.txt" in result["diff"]
    assert ("ordinary.txt" in result["diff"]) == (scope == "")
    assert result["withheld"] == (1 if scope else 4)


def test_repo_diff_all_changes_withheld_returns_empty_diff_with_count(tmp_path):
    repo = _secret_repo(tmp_path)
    _git(repo, "checkout", "--", "ordinary.txt", "config/app.txt")
    result = _diff(repo, context=0)
    assert result["diff"] == ""
    assert result["withheld"] == 4
    assert result["truncated"] is False


def test_repo_diff_scoped_ordinary_file_is_unchanged(tmp_path):
    repo = _secret_repo(tmp_path)
    result = _diff(repo, path="ordinary.txt")
    assert "+new-line" in result["diff"]
    assert result["withheld"] == 0
    _assert_no_secret(result["diff"])


@pytest.mark.parametrize("staged", [False, True])
def test_repo_diff_withholds_renames_to_and_from_secret_paths(tmp_path, staged):
    repo = _repo(tmp_path)
    (repo / "plain.txt").write_text("plain-body-line\n" * 5, encoding="utf-8")
    (repo / ".env").write_text("STRIPE_LIVE=sk_live_renamed0123\n" * 5, encoding="utf-8")
    _git(repo, "add", "-f", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "mv", "plain.txt", ".env.new")
    _git(repo, "mv", ".env", "ordinary-name.txt")
    if not staged:
        _git(repo, "reset", "-q")
    result = _diff(repo, staged=staged, context=0)
    assert "sk_live_" not in result["diff"]
    assert staged is False or "plain-body-line" not in result["diff"]
    assert result["withheld"] == (4 if staged else 1)


@pytest.mark.parametrize("name", ["*", ":(glob)**"])
def test_repo_diff_pathspec_named_files_cannot_widen_the_diff(tmp_path, name):
    repo = _secret_repo(tmp_path)
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="odd\n", capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        _git(repo, "update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, name))
    except AssertionError:
        pytest.skip("this platform cannot hold a tracked path named %r" % name)
    _git(repo, "commit", "-m", "odd name")
    (repo / "ordinary.txt").write_text("again\n", encoding="utf-8")
    result = _diff(repo, context=0)
    _assert_no_secret(result["diff"])
    assert "+again" in result["diff"]

@pytest.mark.parametrize("name", ["*", ":(glob)**", ":(top)."])
def test_repo_diff_listed_pathspec_looking_names_stay_literal(
    monkeypatch, tmp_path, name,
):
    repo = _secret_repo(tmp_path)
    monkeypatch.setattr(
        git_tools, "_diff_changed_paths", lambda *a, **k: [[name]],
    )
    result = _diff(repo, context=0)
    _assert_no_secret(result["diff"])
    assert result["diff"] == ""

def _file_dir_repo(tmp_path, *, file_to_dir, staged):
    repo = _repo(tmp_path)
    secret = "STRIPE=sk_live_filedir0123\n"
    if file_to_dir:
        (repo / "deploy").write_text("plain\n", encoding="utf-8")
    else:
        (repo / "deploy").mkdir()
        (repo / "deploy" / ".env").write_text(secret, encoding="utf-8")
    _git(repo, "add", "-f", "-A")
    _git(repo, "commit", "-m", "base")
    if file_to_dir:
        _git(repo, "rm", "-q", "-f", "deploy")
        (repo / "deploy").mkdir()
        (repo / "deploy" / ".env").write_text(secret, encoding="utf-8")
        _git(repo, "add", "-f", "deploy/.env")
    else:
        _git(repo, "rm", "-q", "-f", "deploy/.env")
        (repo / "deploy").write_text("plain\n", encoding="utf-8")
        _git(repo, "add", "deploy")
    if not staged:
        _git(repo, "reset", "-q")
    return repo


@pytest.mark.parametrize("staged", [True, False])
@pytest.mark.parametrize("file_to_dir", [True, False])
@pytest.mark.parametrize("scope", ["", "deploy"])
def test_repo_diff_file_directory_swap_never_prints_secret_section(
    tmp_path, staged, file_to_dir, scope,
):
    repo = _file_dir_repo(tmp_path, file_to_dir=file_to_dir, staged=staged)
    result = _diff(repo, staged=staged, path=scope, context=0)
    assert "sk_live_" not in result["diff"]
    assert "deploy/.env" not in result["diff"]
    if staged:
        assert result["withheld"] >= 1


@pytest.mark.parametrize("separator", ["\r", "\f", "\x1c", "\x85", "\u2028"])
def test_repo_diff_content_line_breaks_cannot_forge_a_section_header(tmp_path, separator):
    repo = _file_dir_repo(tmp_path, file_to_dir=True, staged=True)
    forged = "A=1" + separator + "diff --git a/deploy b/deploy\nS=sk_live_forged\n"
    (repo / "deploy" / ".env").write_bytes(forged.encode("utf-8"))
    _git(repo, "add", "-f", "deploy/.env")
    result = _diff(repo, staged=True, context=0)
    assert "sk_live_" not in result["diff"]
    assert result["withheld"] >= 1


def test_repo_diff_section_filter_splits_on_newline_only():
    text = (
        "diff --git a/deploy/.env b/deploy/.env\n"
        "+A=1\rdiff --git a/ok b/ok\n+S=sk_live_cr\n"
        "+B=2\u2028diff --git a/ok b/ok\n+T=sk_live_ls\n"
        "diff --git a/ok b/ok\n+fine\n"
    )
    kept, dropped, unparseable = git_tools._filter_diff_sections(text, {"ok"})
    assert "sk_live_" not in kept
    assert kept == "diff --git a/ok b/ok\n+fine\n"
    assert dropped == ["deploy/.env"]
    assert unparseable == 0


def test_repo_diff_drops_and_counts_sections_outside_the_screened_set(
    monkeypatch, tmp_path,
):
    repo = _secret_repo(tmp_path)
    real = git_tools._checked_git

    def racy(root, arguments, **kwargs):
        result = real(root, arguments, **kwargs)
        if kwargs.get("operation") == "diff":
            result = dict(result)
            result["stdout"] += (
                "diff --git a/raced/.env b/raced/.env\n"
                "+++ b/raced/.env\n+STRIPE=sk_live_race\n"
                "diff --git a/mismatch b/other\n+sk_live_mismatch\n"
            )
        return result

    monkeypatch.setattr(git_tools, "_checked_git", racy)
    result = _diff(repo, context=0)
    assert "sk_live_" not in result["diff"]
    assert "config/app.txt" in result["diff"]
    assert result["withheld"] == 4 + 1 + 1


def test_repo_diff_refuses_unknown_status_token_and_truncated_listing(
    monkeypatch, tmp_path,
):
    repo = _secret_repo(tmp_path)
    real = git_tools._checked_git

    def bad_status(root, arguments, **kwargs):
        result = dict(real(root, arguments, **kwargs))
        if kwargs.get("operation") == "diff path listing":
            result["stdout"] = "Z" + chr(0) + "ordinary.txt" + chr(0)
        return result

    monkeypatch.setattr(git_tools, "_checked_git", bad_status)
    with pytest.raises(ValueError, match="unparseable"):
        _diff(repo)

    def truncated(root, arguments, **kwargs):
        result = dict(real(root, arguments, **kwargs))
        if kwargs.get("operation") == "diff path listing":
            result["truncated"] = True
        return result

    monkeypatch.setattr(git_tools, "_checked_git", truncated)
    with pytest.raises(ValueError, match="too many paths"):
        _diff(repo)


def test_repo_diff_section_parser_handles_quoting_and_fails_closed():
    quoted = 'diff --git "a/tab\\there" "b/tab\\there"'
    assert git_tools._section_path(quoted) == "tab\there"
    octal = 'diff --git "a/caf\\303\\251" "b/caf\\303\\251"'
    assert git_tools._section_path(octal) == "café"
    assert git_tools._section_path("diff --git a/with space b/with space") == "with space"
    assert git_tools._section_path("diff --cc plain.txt") == "plain.txt"
    assert git_tools._section_path("diff --git a/x b/y") is None
    assert git_tools._section_path('diff --git "a/x" b/x') is None
    assert git_tools._section_path('diff --git "a/\\q" "b/\\q"') is None
    text = (
        "diff --git a/ok b/ok\n+1\n"
        "diff --git a/deploy/.env b/deploy/.env\n+secret\n"
    )
    kept, dropped, bad = git_tools._filter_diff_sections(text, {"ok"})
    assert kept == "diff --git a/ok b/ok\n+1\n"
    assert dropped == ["deploy/.env"] and bad == 0

