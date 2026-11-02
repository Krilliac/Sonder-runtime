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
    # no sandbox exists off Windows: fail closed unless explicitly opted out (review I7)
    with pytest.raises(sandbox_win.SandboxUnavailable, match="SONDER_MESHGEN_UNSANDBOXED"):
        sandbox_win.decide_mode({}, "posix")
    assert sandbox_win.decide_mode({"SONDER_MESHGEN_UNSANDBOXED": "1"}, "posix") == "unsandboxed"


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


# ---- review round 5: host-side caps, redaction, grant validation, wipe, memory plumbing -------------

def test_redaction_removes_secret_shapes_and_bounds_length():
    text = runner.redact_and_bound("ValueError: API_KEY=abcd1234 sk-" + "a" * 30 + " " + "Z" * 60)
    assert "abcd1234" not in text and "sk-aaaa" not in text and "ZZZZZZZZZZ" not in text
    assert "[redacted]" in text
    long = runner.redact_and_bound("x " * 5000)
    assert len(long) <= runner.MAX_TEXT_CHARS + 20 and long.endswith("...[truncated]")
    assert runner.redact_and_bound(None) == ""


def test_sanitize_result_keeps_known_typed_keys_only():
    hostile = {"ok": 1, "error": "token=hunter2 " + "e" * 9000, "tris": "9", "extra": "x", "size": [1, "a", True, 2, 3, 4],
               "tail": ["password: pw" + str(i) for i in range(20)], "files": {"model": "/m.glb"},
               "parts": [{"name": "n" * 500, "tris": 3}] * 20}
    clean = runner.sanitize_result(hostile)
    assert "extra" not in clean and clean["ok"] is True and clean["tris"] == 0
    assert "hunter2" not in clean["error"] and len(clean["error"]) < 2100
    assert clean["size"] == [1.0]  # first three entries only; non-numbers and bools dropped
    assert len(clean["tail"]) == 8 and not any("pw1" in line for line in clean["tail"])
    assert len(clean["parts"]) == 8 and len(clean["parts"][0]["name"]) < 60


def test_read_result_refuses_an_oversized_or_malformed_file(tmp_path):
    big = tmp_path / "big.json"
    big.write_text('{"ok": true, "error": "' + "a" * (runner.MAX_RESULT_BYTES + 10) + '"}', encoding="utf-8")
    assert runner.read_result(big) is None
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    assert runner.read_result(bad) is None
    assert runner.read_result(tmp_path / "missing.json") is None


def test_copy_back_enforces_per_file_caps(tmp_path, monkeypatch):
    src = tmp_path / "run"
    src.mkdir()
    (src / "m.glb").write_bytes(b"x" * 100)
    (src / "p.png").write_bytes(b"y" * 100)
    dest = tmp_path / "out"
    dest.mkdir()
    moved = runner._copy_back({"model": str(src / "m.glb"), "preview": str(src / "p.png")}, dest)
    assert {Path(v).name for v in moved.values()} == {"m.glb", "p.png"}
    monkeypatch.setattr(runner, "MAX_MODEL_BYTES", 50)
    with pytest.raises(ValueError, match="over the"):
        runner._copy_back({"model": str(src / "m.glb")}, dest)


def test_tail_bytes_keeps_only_the_end(tmp_path):
    path = tmp_path / "o.txt"
    path.write_bytes(b"A" * 1000 + b"END")
    assert sandbox_win.tail_bytes(path, 10) == b"AAAAAAAEND"
    assert sandbox_win.tail_bytes(path, 5000).endswith(b"END")
    assert sandbox_win.tail_bytes(tmp_path / "none") == b""


def test_tree_size_counts_files_and_stops_at_the_cap(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"1" * 100)
    (tmp_path / "g").write_bytes(b"2" * 100)
    assert sandbox_win.tree_size(tmp_path) == 200
    assert sandbox_win.tree_size(tmp_path, cap=50) >= 100  # stopped early, over the cap


def test_wipe_removes_contents_but_keeps_top_level_structure(tmp_path):
    root = tmp_path / "ac"
    (root / "LocalCache" / "deep").mkdir(parents=True)
    (root / "LocalCache" / "deep" / "f.txt").write_text("x")
    (root / "LocalCache" / "g.txt").write_text("x")
    (root / "stray.txt").write_text("x")
    assert sandbox_win.wipe_directory_contents(root) == []
    assert (root / "LocalCache").is_dir() and list((root / "LocalCache").iterdir()) == []
    assert not (root / "stray.txt").exists()


def test_wipe_does_not_follow_links_out_of_the_tree(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    root = tmp_path / "ac"
    (root / "Local").mkdir(parents=True)
    link = root / "Local" / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    sandbox_win.wipe_directory_contents(root)
    assert (outside / "keep.txt").read_text() == "keep" and not link.exists()


def test_blender_dir_validation(tmp_path):
    home = tmp_path / "home"
    (home / "apps" / "blender").mkdir(parents=True)
    kit_dir = tmp_path / "repo" / "tools"
    kit_dir.mkdir(parents=True)
    ok = tmp_path / "apps" / "blender"
    ok.mkdir(parents=True)
    assert sandbox_win.check_blender_dir(ok, kit_dir, [home])
    for bad in (home, home / "apps" / "blender", tmp_path, tmp_path / "repo"):
        with pytest.raises(sandbox_win.SandboxUnavailable):
            sandbox_win.check_blender_dir(bad, kit_dir, [home])
    with pytest.raises(sandbox_win.SandboxUnavailable, match="drive root"):
        sandbox_win.check_blender_dir(Path(tmp_path.anchor), None, [])


def test_blender_dir_symlink_into_home_is_resolved_and_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    link = tmp_path / "shim"
    try:
        os.symlink(home, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    with pytest.raises(sandbox_win.SandboxUnavailable):
        sandbox_win.check_blender_dir(link, None, [home])


def test_build_candidate_honours_memory_mb_on_the_sandboxed_path(tmp_path, monkeypatch):
    seen = {}

    def fake(blender, code_path, out, *, memory_mb, **kwargs):
        seen["memory_mb"] = memory_mb
        return {"ok": True}

    monkeypatch.setattr(runner, "_sandboxed", fake)
    code = tmp_path / "c.py"
    code.write_text("def build(): pass" + chr(10))
    args = dict(name="n", max_tris=10, height_m=0, sandbox="appcontainer")
    runner.build_candidate("blender", str(code), str(tmp_path / "o"), memory_mb=1234, **args)
    assert seen["memory_mb"] == 1234
    monkeypatch.setenv("SONDER_MESHGEN_MEM_MB", "2048")
    runner.build_candidate("blender", str(code), str(tmp_path / "o"), **args)
    assert seen["memory_mb"] == 2048


def test_non_windows_build_is_refused_without_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.delenv("SONDER_MESHGEN_UNSANDBOXED", raising=False)
    monkeypatch.setattr(runner.os, "name", "posix")
    monkeypatch.setattr(runner, "run_limited", lambda *a, **k: pytest.fail("ran unsandboxed"))
    code = tmp_path / "c.py"
    code.write_text("def build(): pass" + chr(10))
    result = runner.build_candidate("blender", str(code), str(tmp_path / "o"), name="n", max_tris=10, height_m=0)
    assert result["ok"] is False and "SONDER_MESHGEN_UNSANDBOXED" in result["error"]
