import server
import selfmod
import trilobite_serve


def test_selfmod_command_surface(monkeypatch, tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("TRILOBITE_SELFMOD_HOME", str(state))
    monkeypatch.setenv("TRILOBITE_SELFMOD_DB", str(state / "selfmod.db"))
    monkeypatch.setattr(server, "system_improvement_report", lambda: "measured sample defect")

    assert "Trilobite self-modification" in server._selfmod_command("status", repository_root=repo)
    assert "usage:" in server._selfmod_command("plan fix sample", repository_root=repo)
    output = server._selfmod_command(
        "plan fix sample --files sample.py --tests python -m pytest -q",
        repository_root=repo,
    )
    assert "phase=proposed" in output
    run = selfmod.list_runs(1)[0]
    assert run["files"] == ["sample.py"]
    assert "explicit user-authorized objective" in run["evidence"][0]


def test_hosted_selfmod_reads_are_safe_but_mutations_need_developer():
    assert not trilobite_serve._dangerous_http_slash("/selfmod status")
    assert not trilobite_serve._dangerous_http_slash("/selfmod diff selfmod-1")
    assert trilobite_serve._dangerous_http_slash("/selfmod run fix --files x.py")
    assert trilobite_serve._dangerous_http_slash("/selfmod approve selfmod-1")
    assert trilobite_serve._dangerous_http_slash("/selfmod rollback selfmod-1")


def test_selfmod_relative_tool_path_dispatches_only_to_candidate(monkeypatch, tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    target = repo / "docs" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    monkeypatch.setenv("TRILOBITE_SELFMOD_HOME", str(state))
    monkeypatch.setenv("TRILOBITE_SELFMOD_DB", str(state / "selfmod.db"))

    run = selfmod.create_plan(
        "correct a documentation defect", repo,
        evidence=["docs/note.txt contains measured wrong text"],
        files=["docs/note.txt"], criteria=["corrected text is present"],
        risk="low",
    )
    selfmod.create_backup(run["id"])
    run = selfmod.prepare_workspace(run["id"])
    workspace = selfmod.candidate_path(run["id"])
    monkeypatch.setenv("TRILOBITE_FILE_ROOTS", str(workspace))

    args = {"path": "docs/note.txt", "old": "before", "new": "after"}
    assert server._selfmod_agent_policy(run)("file_edit", args) == ""
    assert args["path"] == str((workspace / "docs/note.txt").resolve())
    assert not server._agent_dispatch("file_edit", args).startswith("ERROR:")

    assert (workspace / "docs/note.txt").read_text(encoding="utf-8") == "after\n"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_selfmod_policy_pins_default_read_root_and_rejects_authority(monkeypatch, tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.txt").write_text("sample\n", encoding="utf-8")
    monkeypatch.setenv("TRILOBITE_SELFMOD_HOME", str(state))
    monkeypatch.setenv("TRILOBITE_SELFMOD_DB", str(state / "selfmod.db"))
    run = selfmod.create_plan(
        "inspect a bounded sample", repo, evidence=["sample evidence"],
        files=["sample.txt"], criteria=["sample remains bounded"], risk="low",
    )
    selfmod.create_backup(run["id"])
    run = selfmod.prepare_workspace(run["id"])
    policy = server._selfmod_agent_policy(run)

    read_args = {"query": "sample"}
    assert policy("text_search", read_args) == ""
    assert read_args["root"] == str(selfmod.candidate_path(run["id"]).resolve())

    expanded = {"path": "sample.txt", "extra_roots": str(tmp_path)}
    assert "cannot be expanded" in policy("file_read", expanded)
