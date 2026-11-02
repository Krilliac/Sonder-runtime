"""Pure logic of the Windows sandbox plus the fail-closed and reporting behaviour (no Blender)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.meshgen import blender_runner as runner
from sonder_runtime.adapters.meshgen import sandbox_win
from sonder_runtime.domain.common.errors import SonderError

from tests.test_meshgen_tool import FakeRunner, call


def test_memory_limit_defaults_and_clamps():
    assert sandbox_win.memory_limit_mb({}) == 4096
    assert sandbox_win.memory_limit_mb({"SONDER_MESHGEN_MEM_MB": "1024"}) == 1024
    assert sandbox_win.memory_limit_mb({"SONDER_MESHGEN_MEM_MB": "1"}) == sandbox_win.MIN_MEMORY_MB
    assert sandbox_win.memory_limit_mb({"SONDER_MESHGEN_MEM_MB": "999999999"}) == sandbox_win.MAX_MEMORY_MB
    assert sandbox_win.memory_limit_mb({"SONDER_MESHGEN_MEM_MB": "lots"}) == 4096


def test_mode_is_sandboxed_on_windows_unless_explicitly_opted_out():
    assert sandbox_win.decide_mode({}, "nt") == "appcontainer"
    assert sandbox_win.decide_mode({"SONDER_MESHGEN_UNSANDBOXED": "0"}, "nt") == "appcontainer"
    assert sandbox_win.decide_mode({"SONDER_MESHGEN_UNSANDBOXED": ""}, "nt") == "appcontainer"
    for value in ("1", "true", "YES"):
        assert sandbox_win.decide_mode({"SONDER_MESHGEN_UNSANDBOXED": value}, "nt") == "unsandboxed"
    assert sandbox_win.decide_mode({}, "posix") == "unsandboxed"  # honest: no sandbox implemented there


def test_environment_is_minimal_and_points_inside_the_run_dir(tmp_path):
    run = tmp_path / "run"
    env = sandbox_win.sandbox_env(run, r"D:\Blender", r"C:\Windows")
    assert set(env) == {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA",
                        "LOCALAPPDATA", "BLENDER_USER_CONFIG", "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES",
                        "BLENDER_USER_EXTENSIONS", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"}
    for key in ("TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA") + tuple(
            k for k in env if k.startswith("BLENDER_USER_")):
        assert str(run) in env[key], key
    assert env["PATH"].split(os.pathsep)[0] == r"D:\Blender"
    assert "SONDER" not in "".join(env) and not any("KEY" in k or "TOKEN" in k for k in env)
    for directory in sandbox_win.environment_directories(run):
        assert run in directory.parents


def test_the_environment_never_copies_the_server_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-canary")
    env = sandbox_win.sandbox_env(tmp_path, r"D:\Blender", r"C:\Windows")
    assert "sk-ant-canary" not in "".join(env.values())


def test_grant_plan_is_the_run_dir_and_the_blender_dir_only(tmp_path):
    run = tmp_path / "out" / ".run-1"
    blender = tmp_path / "blender"
    plan = sandbox_win.plan_grants(run, blender, kit_source_dir=tmp_path / "kitsrc", protected=[tmp_path / "home"])
    assert [(g.path, g.permission, g.persistent) for g in plan] == [
        (str(run), "M", False), (str(blender), "RX", True)]


def test_a_grant_never_covers_the_kit_source_directory(tmp_path):
    kit_dir = tmp_path / "repo" / "tools" / "blender"
    for run in (kit_dir, kit_dir / ".run-1", tmp_path / "repo", tmp_path):
        with pytest.raises(sandbox_win.SandboxUnavailable, match="kit source"):
            sandbox_win.plan_grants(run, tmp_path / "blender", kit_source_dir=kit_dir)
    sandbox_win.plan_grants(tmp_path / "elsewhere" / ".run-1", tmp_path / "blender", kit_source_dir=kit_dir)


def test_a_grant_never_covers_a_protected_directory(tmp_path):
    home = tmp_path / "home"
    for run in (home, tmp_path):
        with pytest.raises(sandbox_win.SandboxUnavailable, match="protected"):
            sandbox_win.plan_grants(run, tmp_path / "blender", protected=[home])
    sandbox_win.plan_grants(home / "work" / ".run-1", tmp_path / "blender", protected=[home])  # below is fine


def test_build_fails_closed_when_the_sandbox_cannot_be_set_up(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise sandbox_win.SandboxUnavailable("profile creation denied")

    monkeypatch.setattr(sandbox_win, "granted_run_dir", broken)
    ran = []
    monkeypatch.setattr(runner, "run_limited", lambda *a, **k: ran.append(1))
    code = tmp_path / "c.py"
    code.write_text("def build(): pass\n")
    result = runner.build_candidate("blender", str(code), str(tmp_path / "out"), name="n", max_tris=10, height_m=0,
                                    sandbox="appcontainer")
    assert result["ok"] is False and "profile creation denied" in result["error"]
    assert "not falling back" in result["error"] and ran == []  # nothing was run unsandboxed


def test_unsandboxed_builds_are_flagged(tmp_path, monkeypatch):
    def fake(argv, **kwargs):
        import json
        job = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        Path(job["result_path"]).write_text(json.dumps({"ok": True, "tris": 1, "files": {}}), encoding="utf-8")
        return 0, b"", b""

    monkeypatch.setattr(runner, "run_limited", fake)
    code = tmp_path / "c.py"
    code.write_text("def build(): pass\n")
    result = runner.build_candidate("blender", str(code), str(tmp_path / "out"), name="n", max_tris=10, height_m=0,
                                    sandbox="unsandboxed")
    assert result["ok"] is True and result["sandbox"] == "unsandboxed" and "UNSANDBOXED" in result["warning"]


def test_preflight_reports_the_opt_out(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_MESHGEN_UNSANDBOXED", "1")
    info = runner.preflight(str(tmp_path / "blender.exe"))
    assert info["sandbox"] == "unsandboxed" and "UNSANDBOXED" in info["warning"]


class SandboxedRunner(FakeRunner):
    def __init__(self, info=None, error=None):
        super().__init__()
        self.info, self.error = info or {}, error

    def preflight(self, blender):
        if self.error:
            raise self.error
        return self.info


def test_the_tool_refuses_to_run_when_the_sandbox_is_unavailable(tmp_path):
    fake = SandboxedRunner(error=sandbox_win.SandboxUnavailable("no AppContainer"))
    with pytest.raises(SonderError, match="sandbox is unavailable.*SONDER_MESHGEN_UNSANDBOXED"):
        call(tmp_path, runner=fake, candidates=1, rounds=1)
    assert fake.calls == []  # no model time, no build


def test_the_tool_report_flags_an_unsandboxed_run(tmp_path):
    info = {"sandbox": "unsandboxed", "warning": "Blender runs UNSANDBOXED (explicit opt-out)"}
    payload, _, _ = call(tmp_path, runner=SandboxedRunner(info=info), candidates=1, rounds=1)
    assert payload["sandbox"] == "unsandboxed" and "UNSANDBOXED" in payload["warning"]
    payload, _, _ = call(tmp_path, runner=SandboxedRunner(info={"sandbox": "appcontainer"}), candidates=1, rounds=1)
    assert payload["sandbox"] == "appcontainer" and "warning" not in payload


def test_posix_memory_cap_does_not_use_preexec_fn():
    import inspect

    source = inspect.getsource(runner.run_limited) + inspect.getsource(runner._posix_limit)
    assert "preexec_fn" not in source.replace("``preexec_fn``", "").replace("no preexec_fn", "")
