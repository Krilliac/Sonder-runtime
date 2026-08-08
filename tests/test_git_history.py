import io
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

import file_ops
import git_history
import server


def _git(repo, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Test Author")
    _git(repo, "config", "user.email", "author@example.test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "--quiet", "-m", "first commit")
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "--quiet", "-m", "second commit")
    (repo / "a.txt").write_text("one\nthree\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "--quiet", "-m", "third commit")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: repo)
    return repo


def test_repo_log_is_structured_ordered_and_count_bounded(repository):
    report = git_history.repo_log(repository, count=2)
    assert report["ok"] is True
    assert report["repository"] == str(repository.resolve())
    assert report["limit"] == 2
    assert report["count"] == 2
    assert report["truncated"] is True
    assert [row["subject"] for row in report["commits"]] == [
        "third commit", "second commit",
    ]
    assert all(len(row["commit"]) == 40 for row in report["commits"])
    assert report["commits"][0]["author"] == {
        "name": "Test Author", "email": "author@example.test",
    }


def test_repo_log_path_filter_is_literal_and_contained(repository):
    report = git_history.repo_log(repository, file_path="a.txt", count=10)
    assert report["path_filter"] == "a.txt"
    assert [row["subject"] for row in report["commits"]] == [
        "third commit", "first commit",
    ]


def test_repo_show_returns_metadata_message_and_bounded_patch(repository):
    report = git_history.repo_show(repository, revision="HEAD")
    assert report["subject"] == "third commit"
    assert report["message"].strip() == "third commit"
    assert "diff --git a/a.txt b/a.txt" in report["patch"]
    assert "+three" in report["patch"]
    assert report["truncated"] is False

    prior = git_history.repo_show(
        repository, revision="HEAD~1", file_path="b.txt",
    )
    assert prior["subject"] == "second commit"
    assert prior["path_filter"] == "b.txt"
    assert "+two" in prior["patch"]


def test_repo_blame_returns_structured_bounded_lines(repository):
    report = git_history.repo_blame(
        repository, file_path="a.txt", start_line=1, end_line=2,
    )
    assert report["ok"] is True
    assert report["path"] == "a.txt"
    assert report["limit"] == 2
    assert report["count"] == 2
    assert report["truncated"] is False
    assert [row["final_line"] for row in report["lines"]] == [1, 2]
    assert [row["text"] for row in report["lines"]] == ["one", "three"]
    assert all(row["author"] == {
        "name": "Test Author", "email": "author@example.test",
    } for row in report["lines"])
    assert all(len(row["commit"]) == 40 for row in report["lines"])


def test_repo_blame_always_passes_explicit_range_and_safety_flags(
    monkeypatch, repository,
):
    captured = {}
    commit = "a" * 40
    porcelain = (
        "%s 1 1 1\nauthor Test Author\nauthor-mail <author@example.test>\n"
        "author-time 1\nauthor-tz +0000\nsummary first\nfilename a.txt\n\tone\n"
    ) % commit

    def fake_run(root, arguments, **kwargs):
        captured["root"] = root
        captured["arguments"] = arguments
        return {
            "stdout": porcelain.encode(), "stderr": b"", "truncated": False,
        }

    monkeypatch.setattr(git_history, "_run_git", fake_run)
    report = git_history.repo_blame(repository, file_path="a.txt")
    arguments = captured["arguments"]
    assert arguments[:4] == [
        "blame", "--line-porcelain", "--no-progress", "--no-textconv",
    ]
    assert arguments[arguments.index("-L") + 1] == "1,100"
    assert arguments[-3:] == ["HEAD", "--", "a.txt"]
    assert report["limit"] == git_history.DEFAULT_BLAME_LINES


@pytest.mark.parametrize(
    "start_line,end_line",
    [
        (True, 2), ("1", 2), (0, 1), (-1, 1),
        (2, 1), (1, True), (1, "2"), (1, 501),
        (git_history.MAX_BLAME_LINE_NUMBER + 1, 0),
    ],
)
def test_repo_blame_rejects_invalid_or_unbounded_ranges(
    repository, start_line, end_line,
):
    with pytest.raises(git_history.GitHistoryError, match="blame"):
        git_history.repo_blame(
            repository, file_path="a.txt",
            start_line=start_line, end_line=end_line,
        )


@pytest.mark.parametrize("file_path", ["", "missing.txt", ".", "src"])
def test_repo_blame_requires_one_existing_regular_file(repository, file_path):
    if file_path == "src":
        (repository / "src").mkdir()
    with pytest.raises(git_history.GitHistoryError, match="blame|path filter"):
        git_history.repo_blame(repository, file_path=file_path)


def test_repo_blame_reuses_revision_and_path_injection_guards(repository):
    with pytest.raises(git_history.GitHistoryError, match="unsafe syntax"):
        git_history.repo_blame(
            repository, file_path="a.txt", revision="--contents=outside",
        )
    with pytest.raises(git_history.GitHistoryError, match="path filter rejected"):
        git_history.repo_blame(repository, file_path="../outside.txt")


@pytest.mark.parametrize(
    "revision",
    [
        "--all", "HEAD..main", "HEAD:path", "HEAD@{1}", "HEAD --stat",
        "refs/heads/../secret", "refs//heads/main", "main.lock", "a" * 257,
    ],
)
def test_revision_grammar_rejects_option_and_extended_injection(revision):
    with pytest.raises(git_history.GitHistoryError, match="unsafe syntax"):
        git_history.validate_revision(revision)


@pytest.mark.parametrize(
    "revision",
    ["HEAD", "HEAD~2", "HEAD^", "HEAD^2", "deadbeef", "main", "v1.2.3", "refs/tags/v1"],
)
def test_revision_grammar_accepts_narrow_commitish_forms(revision):
    assert git_history.validate_revision(revision) == revision


def test_exact_root_disables_parent_discovery(repository):
    child = repository / "src"
    child.mkdir()
    with pytest.raises(git_history.GitHistoryError, match="upward discovery"):
        git_history.repo_log(child)


def test_path_filter_rejects_escape_metadata_and_foreign_absolute(repository):
    with pytest.raises(git_history.GitHistoryError, match="path filter rejected"):
        git_history.repo_log(repository, file_path="../outside.txt")
    with pytest.raises(git_history.GitHistoryError, match="path filter rejected"):
        git_history.repo_log(repository, file_path=".git/config")
    foreign = "/etc/passwd" if os.name == "nt" else "C:\\outside.txt"
    with pytest.raises(git_history.GitHistoryError, match="non-native"):
        git_history.repo_log(repository, file_path=foreign)


def test_path_filter_rejects_symlink_escape(repository, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = repository / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    with pytest.raises(git_history.GitHistoryError, match="path filter rejected"):
        git_history.repo_show(repository, file_path="linked.txt")
    with pytest.raises(git_history.GitHistoryError, match="path filter rejected"):
        git_history.repo_blame(repository, file_path="linked.txt")


def test_repo_show_hard_output_ceiling_returns_parseable_truncation(repository):
    large = repository / "large.txt"
    large.write_text("line\n" * 5000, encoding="utf-8")
    _git(repository, "add", "large.txt")
    _git(repository, "commit", "--quiet", "-m", "large patch")
    report = git_history.repo_show(repository, max_bytes=1024)
    assert report["truncated"] is True
    assert report["output_bytes"] <= 1024
    assert report["subject"] == "large patch"


def test_repo_blame_hard_output_ceiling_keeps_only_complete_records(repository):
    large = repository / "large.txt"
    large.write_text("line %d\n" % 0 + "".join(
        "line %d\n" % number for number in range(1, 200)
    ), encoding="utf-8")
    _git(repository, "add", "large.txt")
    _git(repository, "commit", "--quiet", "-m", "large blame")
    report = git_history.repo_blame(
        repository, file_path="large.txt", start_line=1, end_line=200,
        max_bytes=1024,
    )
    assert report["truncated"] is True
    assert report["output_bytes"] <= 1024
    assert 0 <= report["count"] < 200
    assert all(row["text"].startswith("line ") for row in report["lines"])


def test_invalid_but_safe_revision_reports_bounded_git_error(repository):
    with pytest.raises(git_history.GitHistoryError, match="git history command failed"):
        git_history.repo_show(repository, revision="deadbeef")


def test_runner_uses_argv_no_shell_and_scrubs_git_environment(
    monkeypatch, tmp_path,
):
    captured = {}

    class Process:
        returncode = 0

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setenv("GIT_DIR", "outside")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.pager")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "evil")
    monkeypatch.setattr(git_history.subprocess, "Popen", Process)
    monkeypatch.setattr(git_history, "_git_executable", lambda: "C:/git.exe")
    result = git_history._run_git(
        tmp_path, ["log", "--no-color"], timeout=1, max_bytes=1024,
    )
    assert result["truncated"] is False
    assert isinstance(captured["argv"], list)
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    env = captured["kwargs"]["env"]
    assert "GIT_DIR" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert env["GIT_PAGER"] == "cat"
    assert env["GIT_LITERAL_PATHSPECS"] == "1"
    assert "diff.external=" in captured["argv"]


def test_runner_enforces_timeout(monkeypatch, tmp_path):
    class HangingProcess:
        def __init__(self, argv, **kwargs):
            self.returncode = None
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(git_history.subprocess, "Popen", HangingProcess)
    monkeypatch.setattr(git_history, "_git_executable", lambda: "git")
    started = time.monotonic()
    with pytest.raises(git_history.GitHistoryError, match="timeout ceiling"):
        git_history._run_git(
            tmp_path, ["log"], timeout=0.1, max_bytes=1024,
        )
    assert time.monotonic() - started < 1.0


def test_server_registration_read_only_dispatch_project_scope_and_dedup(
    repository,
):
    output = server._agent_dispatch(
        "repo_log",
        {"path": str(repository), "count": 1},
        read_only=True,
        repository_extra_roots=str(repository),
    )
    assert json.loads(output)["count"] == 1
    blame_output = server._agent_dispatch(
        "repo_blame",
        {"path": str(repository), "file_path": "a.txt", "end_line": 2},
        read_only=True,
        repository_extra_roots=str(repository),
    )
    assert json.loads(blame_output)["count"] == 2
    assert "repo_log/repo_show/repo_blame" in server.tool_manifest()
    help_text = server._agent_tool_help(read_only=True)
    assert all("- %s:" % name in help_text for name in (
        "repo_log", "repo_show", "repo_blame",
    ))
    git_tools = {"repo_log", "repo_show", "repo_blame"}
    assert git_tools.issubset(server.REPOSITORY_READ_ONLY_TOOLS)
    assert git_tools.issubset(server._PROJECT_SCOPED_PATH_TOOLS)
    assert git_tools.issubset(server._WORK_INSPECTION_TOOLS)
    assert git_tools.issubset(
        server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    )
    assert git_tools.issubset(server._AUTOPILOT_OBSERVE_TOOLS)
    scoped = server._project_scope_args(
        "repo_show", {"path": ".", "revision": "HEAD"}, str(repository),
    )
    assert scoped["path"] == str(repository)
    assert server._repository_scope_path_error(
        "repo_show", scoped, str(repository),
    ) == ""


def test_server_activity_records_direct_git_tool(monkeypatch, repository):
    calls = []
    monkeypatch.setattr(
        server.activity_tracker, "record_tool_result",
        lambda name, args, **kwargs: calls.append((name, kwargs)),
    )
    output = server.repo_show(str(repository), max_bytes=4096)
    assert json.loads(output)["subject"] == "third commit"
    assert calls[-1][0] == "repo_show"
    assert calls[-1][1]["ok"] is True
    output = server.repo_blame(
        str(repository), file_path="a.txt", end_line=2, max_bytes=4096,
    )
    assert json.loads(output)["count"] == 2
    assert calls[-1][0] == "repo_blame"
    assert calls[-1][1]["ok"] is True
