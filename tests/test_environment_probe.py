"""environment_probe: deterministic host discovery, no subprocesses."""
import environment_probe as ep


def test_probe_reports_this_host_coherently():
    env = ep.probe(refresh=True)
    assert env["os"] in ("Windows", "Linux", "Darwin")
    assert env["is_windows"] == (env["os"] == "Windows")
    assert env["python_version"].count(".") == 2
    assert env["cpu_count"] >= 1
    # Python is running this test, so its interpreter's presence is a given;
    # the probe must find at least one python entry point.
    assert any(name.startswith("python") for name in env["toolchains"])


def test_probe_is_cached_until_refresh(monkeypatch):
    first = ep.probe(refresh=True)
    calls = []
    monkeypatch.setattr(ep.shutil, "which", lambda name: calls.append(name))
    assert ep.probe() is first
    assert calls == [], "a cached probe must not re-scan PATH"


def test_preferred_shell_matches_platform(monkeypatch):
    monkeypatch.setattr(ep.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        ep.shutil, "which",
        lambda name: "C:\\x\\%s.exe" % name if name in ("powershell", "cmd") else None,
    )
    env = ep.probe(refresh=True)
    assert env["preferred_shell"] == "powershell"

    monkeypatch.setattr(ep.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        ep.shutil, "which",
        lambda name: "/bin/%s" % name if name in ("bash", "sh") else None,
    )
    env = ep.probe(refresh=True)
    assert env["preferred_shell"] == "bash"
    ep.probe(refresh=True)  # restore a real probe for later tests


def test_agent_brief_is_one_line_and_names_the_essentials():
    brief = ep.agent_brief(refresh=True)
    assert "\n" not in brief
    assert brief.startswith("environment: ")
    assert "preferred shell:" in brief and "tools:" in brief


def test_format_profile_lists_shells_and_toolchains():
    text = ep.format_profile(refresh=True)
    assert "host environment" in text
    assert "shells:" in text and "toolchains:" in text
