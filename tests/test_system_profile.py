import system_profile


def test_nvidia_profile_selects_requested_physical_gpu(monkeypatch):
    output = (
        "busy small, 8192, 1024, 8.6\n"
        "free large, 24576, 22000, 8.9\n"
    )
    monkeypatch.setattr(
        system_profile.subprocess, "check_output", lambda *args, **kwargs: output
    )
    assert system_profile._nvidia_profile(0)[:3] == ("busy small", 8.0, 1.0)
    assert system_profile._nvidia_profile(1)[:3] == (
        "free large", 24.0, 22000 / 1024,
    )
    assert system_profile._nvidia_profile(2) is None


def test_profile_missing_reads_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    assert system_profile.read_profile() == ""


def test_ensure_profile_creates_default(monkeypatch, tmp_path):
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    text, path = system_profile.ensure_profile()
    assert "standing instructions" in text.lower()
    assert "workspace_inventory" in text
    assert "redacted memory privacy" in text
    assert path.endswith("system_profile.md")


def test_system_prompt_initializes_missing_profile_on_first_use(monkeypatch, tmp_path):
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)

    prompt = system_profile.system_prompt()

    assert prompt.startswith("Standing instructions")
    assert (tmp_path / "system_profile.md").is_file()


def test_system_prompt_preserves_intentionally_empty_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    system_profile.write_profile("")

    assert system_profile.system_prompt() == ""
    assert system_profile.read_profile() == ""


def test_append_profile_preserves_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    system_profile.write_profile("first")
    system_profile.append_profile("- second")
    assert system_profile.read_profile() == "first\n\n- second"


def test_profile_path_must_stay_inside_workspace(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    outside = tmp_path / "outside.md"
    root.mkdir()
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(root))
    try:
        system_profile.write_profile("x", str(outside))
    except ValueError as e:
        assert "inside workspace" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_system_prompt_labels_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    system_profile.write_profile("Always say less.")
    assert system_profile.system_prompt().startswith("Standing instructions")
    assert "Always say less." in system_profile.system_prompt()


def test_build_system_includes_profile(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(server.system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    system_profile.write_profile("Prefer tiny answers.")
    out = server._build_system("Base system", False, "")
    assert "Prefer tiny answers." in out
    assert out.index("Prefer tiny answers.") < out.index("Base system")


def test_update_system_profile_modes(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    assert "Updated system profile" in server.update_system_profile("alpha", mode="replace")
    assert server.system_profile.read_profile() == "alpha"
    server.update_system_profile("beta", mode="append")
    assert server.system_profile.read_profile() == "alpha\n\nbeta"
    server.update_system_profile("", mode="clear")
    assert server.system_profile.read_profile() == ""


def test_diagnostics_reports_sections(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "sonder"}]})
    out = server.diagnostics()
    assert "sonder diagnostics" in out
    assert "system profile: ok" in out
    assert "memory db: ok" in out
    assert "ollama: ok" in out
