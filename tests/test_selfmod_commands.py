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
