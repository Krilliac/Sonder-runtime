"""Blender runner: argv construction, result parsing, missing-result handling."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sonder_runtime.adapters.meshgen import blender_runner as runner


def _completed(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout.encode(), stderr=stderr.encode())


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


def test_parse_result_takes_the_last_well_formed_line():
    out = 'noise\nMESHGEN_RESULT {"ok": false}\nMESHGEN_RESULT not json\nMESHGEN_RESULT {"ok": true, "tris": 5}\nBlender quit\n'
    assert runner.parse_result(out) == {"ok": True, "tris": 5}
    assert runner.parse_result("nothing") is None
    assert runner.parse_result("MESHGEN_RESULT [1,2]") is None


def test_build_candidate_runs_without_a_shell_and_writes_the_job(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        seen["job"] = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        return _completed('MESHGEN_RESULT {"ok": true, "tris": 12, "files": {"model": "m.glb"}}\n')

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    code = tmp_path / "c.py"
    code.write_text("def build(): pass\n")
    kit = tmp_path / "kit.py"
    kit.write_text("x = 1\n")
    out = tmp_path / "out"
    result = runner.build_candidate("blender", str(code), str(out), name="cart", kit_path=str(kit),
                                    max_tris=2000, height_m=1.5, export="fbx", render=False, timeout=77)
    assert result == {"ok": True, "tris": 12, "files": {"model": "m.glb"}}
    kwargs = seen["kwargs"]
    assert kwargs["shell"] is False and kwargs["timeout"] == 77 and kwargs["cwd"] == str(out)
    assert "--disable-autoexec" in seen["argv"] and "--factory-startup" in seen["argv"]
    job = seen["job"]
    assert job["name"] == "cart" and job["max_tris"] == 2000 and job["height_m"] == 1.5
    assert job["export"] == "fbx" and job["render"] is False
    assert job["kit_path"] == str(kit.resolve()) and job["code_path"] == str(code.resolve())


def test_missing_result_reports_rc_and_the_output_tail(tmp_path, monkeypatch):
    noisy = "\n".join("line %d" % n for n in range(30))
    monkeypatch.setattr(runner.subprocess, "run", lambda argv, **kw: _completed(noisy, "Segmentation fault", rc=3))
    code = tmp_path / "c.py"
    code.write_text("x")
    result = runner.build_candidate("blender", str(code), str(tmp_path / "o"), name="n", max_tris=10, height_m=0)
    assert result["ok"] is False and result["error"] == "no result; rc=3"
    assert len(result["tail"]) == 8 and result["tail"][-1] == "Segmentation fault"


def test_timeout_and_start_failure_are_error_results(tmp_path, monkeypatch):
    code = tmp_path / "c.py"
    code.write_text("x")

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"partial\n")

    monkeypatch.setattr(runner.subprocess, "run", hang)
    result = runner.build_candidate("blender", str(code), str(tmp_path / "o"), name="n", max_tris=10,
                                    height_m=0, timeout=5)
    assert result["ok"] is False and "timed out after 5s" in result["error"] and result["tail"] == ["partial"]

    def missing(argv, **kw):
        raise FileNotFoundError("no blender")

    monkeypatch.setattr(runner.subprocess, "run", missing)
    result = runner.build_candidate("blender", str(code), str(tmp_path / "o"), name="n", max_tris=10, height_m=0)
    assert result["ok"] is False and "could not start blender" in result["error"]
