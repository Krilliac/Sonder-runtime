"""Blender runner: argv construction, file-based result handling, forged-output defence."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sonder_runtime.adapters.meshgen import blender_runner as runner


def _completed(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout.encode(), stderr=stderr.encode())


def _patch_run(monkeypatch, fn):
    """Route ``run_limited`` through a fake that returns a ``CompletedProcess``."""
    def limited(argv, **kwargs):
        completed = fn(argv, **kwargs)
        return completed.returncode, completed.stdout, completed.stderr

    monkeypatch.setattr(runner, "run_limited", limited)


def _job(argv):
    return json.loads(Path(argv[-1]).read_text(encoding="utf-8"))


def _write_result(argv, payload):
    Path(_job(argv)["result_path"]).write_text(json.dumps(payload), encoding="utf-8")


def _build(tmp_path, **kw):
    kw.setdefault("sandbox", "unsandboxed")
    code = tmp_path / "c.py"
    code.write_text("def build(): pass\n")
    kw.setdefault("max_tris", 10)
    kw.setdefault("height_m", 0)
    return runner.build_candidate("blender", str(code), str(tmp_path / "out"), name="n", **kw)


def test_locate_blender_prefers_the_env_file_then_path(tmp_path, monkeypatch):
    exe = tmp_path / "blender.exe"
    exe.write_text("x")
    assert runner.locate_blender({"SONDER_BLENDER": str(exe)}) == str(exe)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/blender")
    assert runner.locate_blender({"SONDER_BLENDER": str(tmp_path / "missing.exe")}) == "/usr/bin/blender"
    assert runner.locate_blender({}) == "/usr/bin/blender"
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner.locate_blender({}) is None


def test_build_argv_has_the_isolation_flags_in_order():
    argv = runner.build_argv("blender", "job.json", "harness.py")
    assert argv[:4] == ["blender", "-b", "--factory-startup", "--disable-autoexec"]
    assert argv[argv.index("--python") + 1] == "harness.py"
    assert argv[-2:] == ["--", "job.json"]


def _glb(document: dict) -> bytes:
    """A minimal valid GLB (header plus one padded JSON chunk)."""
    import json
    import struct

    body = json.dumps(document).encode("utf-8")
    body += b" " * (-len(body) % 4)
    return b"glTF" + struct.pack("<II", 2, 20 + len(body)) + struct.pack("<II", len(body), 0x4E4F534A) + body


def test_build_candidate_runs_without_a_shell_and_reads_the_result_file(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"], seen["job"] = argv, kwargs, _job(argv)
        out = Path(seen["job"]["out_dir"])
        (out / "m.glb").write_bytes(_glb({"asset": {"version": "2.0"}, "nodes": [{"name": "Cube"}]}))
        _write_result(argv, {"ok": True, "tris": 12, "files": {"model": str(out / "m.glb")}})
        return _completed("hello\n")

    _patch_run(monkeypatch, fake_run)
    kit = tmp_path / "kit.py"
    kit.write_text("x = 1\n")
    result = _build(tmp_path, kit_path=str(kit), max_tris=2000, height_m=1.5, export="fbx", render=False,
                    timeout=77)
    assert result["ok"] is True and result["tris"] == 12
    kwargs = seen["kwargs"]
    assert kwargs["timeout"] == 77 and kwargs["cwd"] == str(tmp_path / "out")
    assert "--disable-autoexec" in seen["argv"] and "--factory-startup" in seen["argv"]
    job = seen["job"]
    assert job["name"] == "n" and job["max_tris"] == 2000 and job["height_m"] == 1.5
    assert job["export"] == "fbx" and job["render"] is False and job["kit_path"] == str(kit.resolve())
    assert job["result_path"].startswith(job["out_dir"]) and "result-" in job["result_path"]
    assert not Path(job["result_path"]).exists()  # consumed
    assert job["kit_excluded"] == []
    assert not (tmp_path / "out" / runner.SCRATCH_NAME).exists()  # scratch profile removed after the build


def test_a_result_line_printed_on_stdout_is_never_trusted(tmp_path, monkeypatch):
    forged = 'MESHGEN_RESULT {"ok": true, "tris": 1, "files": {"model": "C:/secret"}}\n'
    _patch_run(monkeypatch, lambda argv, **kw: _completed(forged, rc=0))
    result = _build(tmp_path)
    assert result["ok"] is False and result["error"] == "no result; rc=0"


def test_result_file_names_must_resolve_inside_the_output_directory(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("s")

    def fake_run(argv, **kwargs):
        _write_result(argv, {"ok": True, "tris": 1, "files": {"model": str(secret)}})
        return _completed()

    _patch_run(monkeypatch, fake_run)
    result = _build(tmp_path)
    assert result["ok"] is False and "outside the output directory" in result["error"]


def test_dotdot_and_missing_files_are_refused(tmp_path, monkeypatch):
    (tmp_path / "secret.txt").write_text("s")
    payloads = [{"files": {"model": "OUT/../secret.txt"}}, {"files": {"model": "OUT/absent.glb"}}]

    def fake_run(argv, **kwargs):
        out = _job(argv)["out_dir"]
        payload = payloads.pop(0)
        _write_result(argv, {"ok": True, "files": {k: v.replace("OUT", out) for k, v in payload["files"].items()}})
        return _completed()

    _patch_run(monkeypatch, fake_run)
    assert "outside the output directory" in _build(tmp_path)["error"]
    assert "does not exist" in _build(tmp_path)["error"]


def test_missing_result_reports_rc_and_the_output_tail(tmp_path, monkeypatch):
    noisy = "\n".join("line %d" % n for n in range(30))
    _patch_run(monkeypatch, lambda argv, **kw: _completed(noisy, "Segmentation fault", rc=3))
    result = _build(tmp_path)
    assert result["ok"] is False and result["error"] == "no result; rc=3"
    assert len(result["tail"]) == 8 and result["tail"][-1] == "Segmentation fault"


def test_a_malformed_result_file_counts_as_missing(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        Path(_job(argv)["result_path"]).write_text("not json", encoding="utf-8")
        return _completed()

    _patch_run(monkeypatch, fake_run)
    assert _build(tmp_path)["error"].startswith("no result")


def test_timeout_and_start_failure_are_error_results(tmp_path, monkeypatch):
    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"partial\n")

    _patch_run(monkeypatch, hang)
    result = _build(tmp_path, timeout=5)
    assert result["ok"] is False and "timed out after 5s" in result["error"] and result["tail"] == ["partial"]

    def missing(argv, **kw):
        raise FileNotFoundError("no blender")

    _patch_run(monkeypatch, missing)
    assert "could not start blender" in _build(tmp_path)["error"]


def test_blender_gets_an_allow_listed_environment_only(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return _completed()

    _patch_run(monkeypatch, fake_run)
    parent = {
        "PATH": "C:/Python;C:/secret-tools", "SYSTEMROOT": "C:/Windows", "WINDIR": "C:/Windows",
        "ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-o", "SONDER_TEST_SECRET": "s",
        "GITHUB_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "a", "USERPROFILE": "C:/Users/real",
        "APPDATA": "C:/Users/real/AppData", "HOME": "/home/real", "NUMBER_OF_PROCESSORS": "8",
    }
    monkeypatch.setattr(runner.os, "environ", parent)
    _build(tmp_path)
    env = seen["env"]
    out = str(tmp_path / "out")
    assert not [k for k in env if k.upper() not in (runner.ENV_ALLOWLIST | {"PATH"} | set(runner._SCRATCH_VARIABLES))]
    assert "ANTHROPIC_API_KEY" not in env and "SONDER_TEST_SECRET" not in env and "GITHUB_TOKEN" not in env
    assert "secret-tools" not in env["PATH"] and env["PATH"].startswith(str(Path("blender").resolve().parent))
    assert env["PATH"].split(runner.os.pathsep)[1:] == [runner.os.path.join("C:/Windows", "System32"), "C:/Windows"]
    for key in ("USERPROFILE", "APPDATA", "HOME", "TEMP", "TMP"):
        assert env[key].startswith(out), key        # profile and temp redirected into the build directory
    assert env["NUMBER_OF_PROCESSORS"] == "8" and env["SYSTEMROOT"] == "C:/Windows"


def test_excluded_kit_names_travel_in_the_job_file(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["job"] = _job(argv)
        return _completed()

    _patch_run(monkeypatch, fake_run)
    _build(tmp_path, kit_excluded=frozenset({"run_prop", "KIT_DIR"}))
    assert seen["job"]["kit_excluded"] == ["KIT_DIR", "run_prop"]


def test_scratch_profile_is_removed_even_when_the_build_fails(tmp_path, monkeypatch):
    def start_fails(argv, **kwargs):
        runner.build_env({}, Path(kwargs["env"]["TEMP"]), "blender")
        raise FileNotFoundError("no blender")

    _patch_run(monkeypatch, start_fails)
    assert "could not start blender" in _build(tmp_path)["error"]
    assert not (tmp_path / "out" / runner.SCRATCH_NAME).exists()


def test_run_limited_runs_without_a_shell_and_returns_the_output(tmp_path):
    import sys

    code, out, err = runner.run_limited([sys.executable, "-c", "print('hi'); import sys; sys.stderr.write('e')"],
                                         cwd=str(tmp_path), env=dict(runner.os.environ), timeout=30)
    assert code == 0 and out.strip() == b"hi" and err == b"e"


def test_run_limited_times_out_and_kills_the_child(tmp_path):
    import sys
    import time

    began = time.monotonic()
    try:
        runner.run_limited([sys.executable, "-c", "import time; print('x', flush=True); time.sleep(60)"],
                           cwd=str(tmp_path), env=dict(runner.os.environ), timeout=2)
    except subprocess.TimeoutExpired as exc:
        assert exc.stdout.strip() == b"x"
    else:
        raise AssertionError("expected a timeout")
    assert time.monotonic() - began < 30


def test_run_limited_caps_memory_where_the_platform_can(tmp_path):
    import sys

    grab = "b = bytearray(1500 * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); print('allocated')"
    code, out, err = runner.run_limited([sys.executable, "-c", grab], cwd=str(tmp_path),
                                         env=dict(runner.os.environ), timeout=60, memory_mb=512)
    assert b"allocated" not in out and code != 0
