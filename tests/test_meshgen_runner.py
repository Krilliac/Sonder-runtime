"""Blender runner: argv construction, file-based result handling, forged-output defence."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

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
    code = tmp_path / "ops.json"
    code.write_text('{"version": 1, "kit": "", "seed": 0, "ops": [], "log": []}')
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
        (out / "n.fbx").write_bytes(_glb({"asset": {"version": "2.0"}, "nodes": [{"name": "Cube"}]}))
        _write_result(argv, {"ok": True, "tris": 12, "files": {"model": str(out / "n.fbx")}})
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
    assert job["kit_allowed"] == [] and job["ops_path"] == str((tmp_path / "ops.json").resolve())
    assert "code_path" not in job and "kit_excluded" not in job
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
    payloads = [{"files": {"model": "OUT/../secret.txt"}}, {"files": {"model": "OUT/n.glb"}}]

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
    sysroot = "C:/Windows" if os.name == "nt" else "/mnt/c/Windows"  # a root the host can spell
    parent = {
        "PATH": os.pathsep.join(["/opt/python", "/opt/secret-tools"]), "SYSTEMROOT": sysroot, "WINDIR": sysroot,
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
    assert env["PATH"].split(runner.os.pathsep)[1:] == [runner.os.path.join(sysroot, "System32"), sysroot]
    for key in ("USERPROFILE", "APPDATA", "HOME", "TEMP", "TMP"):
        assert env[key].startswith(out), key        # profile and temp redirected into the build directory
    assert env["NUMBER_OF_PROCESSORS"] == "8" and env["SYSTEMROOT"] == sysroot


def test_allowed_kit_names_travel_in_the_job_file(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["job"] = _job(argv)
        return _completed()

    _patch_run(monkeypatch, fake_run)
    _build(tmp_path, kit_allowed=frozenset({"box", "PALETTE"}))
    assert seen["job"]["kit_allowed"] == ["PALETTE", "box"]


def test_the_trusted_harness_files_are_staged_next_to_the_harness_and_removed_afterwards(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        harness = Path(argv[argv.index("--python") + 1])
        seen["files"] = sorted(item.name for item in harness.parent.iterdir())
        seen["same"] = [(harness.parent / source.name).read_bytes() == source.read_bytes()
                        for source in runner.HARNESS_FILES]
        seen["dir"] = harness.parent
        return _completed()

    _patch_run(monkeypatch, fake_run)
    _build(tmp_path)
    assert seen["files"] == ["connectivity.py", "harness.py", "opschema.py", "replay.py"] and all(seen["same"])
    assert not seen["dir"].exists()


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

    # On Linux the cap is RLIMIT_AS = memory_mb * POSIX_ADDRESS_SPACE_FACTOR, so allocate well above that.
    grab = "b = bytearray(%d * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); print('allocated')" % (512 * runner.POSIX_ADDRESS_SPACE_FACTOR + 1024)
    code, out, err = runner.run_limited([sys.executable, "-c", grab], cwd=str(tmp_path),
                                         env=dict(runner.os.environ), timeout=60, memory_mb=512)
    assert b"allocated" not in out and code != 0


# --- output paths are never redacted or truncated (a long slug is a legitimate directory name) ---

def _files_result(path, key="model"):
    return {"ok": True, "tris": 1, "files": {key: path}}


def test_long_slug_and_long_segment_paths_survive_sanitising_and_confinement(tmp_path):
    slug = "a-very-long-asset-slug-ab"  # 25 characters, as produced from a long asset name
    folder = tmp_path / ("%s-20261101T120000Z" % slug)
    folder.mkdir()
    long_name = "x" * 60
    for name, key, target in ((slug, "model", folder / (slug + ".glb")), (long_name, "model", folder / (long_name + ".glb")),
                              (long_name, "sheet", folder / (long_name + "_sheet.png"))):
        target.write_text("g")
        cleaned = runner.sanitize_result(_files_result(str(target), key))
        assert cleaned["files"][key] == str(target)
        assert runner.confine_files(cleaned, folder, name) == cleaned


def test_over_long_or_nul_paths_are_refused_with_the_key_named(tmp_path):
    for bad in ("p" * (runner.MAX_PATH_CHARS + 1), str(tmp_path / "n.glb") + chr(0), 12345):
        refused = runner.confine_files(runner.sanitize_result(_files_result(bad)), tmp_path, "n")
        assert refused["ok"] is False and "model" in refused["error"]


def _confine(tmp_path, value, key="model", name="n", export="glb"):
    return runner.confine_files({"ok": True, "files": {key: value}}, tmp_path, name, export)


def test_only_the_files_the_harness_writes_are_accepted(tmp_path):
    for fname in ("n.glb", "n_sheet.png", "n_view.png"):
        (tmp_path / fname).write_text("x")
    assert _confine(tmp_path, str(tmp_path / "n.glb"))["ok"] is True
    assert _confine(tmp_path, str(tmp_path / "n_sheet.png"), "sheet")["ok"] is True
    assert _confine(tmp_path, str(tmp_path / "n_view.png"), "view")["ok"] is True
    (tmp_path / "n.fbx").write_text("x")
    assert _confine(tmp_path, str(tmp_path / "n.fbx"), export="fbx")["ok"] is True
    refused = _confine(tmp_path, str(tmp_path / "n.glb"), export="fbx")
    assert refused["ok"] is False and "does not write" in refused["error"]
    assert _confine(tmp_path, str(tmp_path / "n_sheet.png"), "model")["ok"] is False  # right file, wrong key
    assert _confine(tmp_path, str(tmp_path / "n.glb"), "extra")["ok"] is False  # a key the harness never sets


def test_a_secret_shaped_file_name_is_refused_not_redacted(tmp_path):
    secret = tmp_path / "sk-CANARYCANARYCANARY0000.glb"
    secret.write_text("g")
    refused = _confine(tmp_path, str(secret))
    assert refused["ok"] is False and "does not write" in refused["error"]


def test_a_path_that_is_not_lexically_inside_the_output_dir_is_refused_before_any_filesystem_access(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    touched = []
    monkeypatch.setattr(runner.os.path, "realpath", lambda value, *a, **k: touched.append(value) or value)
    for value in (chr(92) * 2 + "attacker.example" + chr(92) + "share" + chr(92) + "n.glb", "//attacker.example/share/n.glb",
                  "n.glb", str(tmp_path / "n.glb"), str(out / ".." / "n.glb")):
        refused = runner.confine_files({"ok": True, "files": {"model": value}}, out, "n")
        assert refused["ok"] is False and "outside the output directory" in refused["error"], value
    assert all(str(item) == str(out) for item in touched)  # only the trusted output directory is resolved


def _symlink_or_skip(link, target):
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError) as exc:
        pytest.skip("cannot create a file symlink here (%s)" % exc.__class__.__name__)


def test_an_expected_name_that_is_a_symlink_to_a_file_outside_the_output_dir_is_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    canary = tmp_path / "canary.txt"
    canary.write_text("secret")
    for fname, key in (("n.glb", "model"), ("n_sheet.png", "sheet"), ("n_view.png", "view")):
        _symlink_or_skip(out / fname, canary)
        # passes the lexical check (direct child of out, exact expected name); only the realpath containment refuses it
        refused = runner.confine_files({"ok": True, "files": {key: str(out / fname)}}, out, "n")
        assert refused["ok"] is False and "outside the output directory" in refused["error"], key


def test_a_symlink_to_a_file_inside_the_output_dir_is_still_accepted(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "real.bin").write_text("x")
    _symlink_or_skip(out / "n.glb", out / "real.bin")
    assert runner.confine_files({"ok": True, "files": {"model": str(out / "n.glb")}}, out, "n")["ok"] is True


def test_the_lexical_prefix_check_ignores_case(tmp_path):
    (tmp_path / "n.glb").write_text("x")
    shouted = str(tmp_path).upper() + chr(92) + "n.glb" if os.name == "nt" else str(tmp_path / "n.glb")
    assert _confine(tmp_path, shouted)["ok"] is True


def test_alternate_data_streams_and_trailing_dots_or_spaces_are_refused(tmp_path):
    (tmp_path / "n.glb").write_text("x")
    base = str(tmp_path / "n.glb")
    for value in (base + ":hidden", base + "::$DATA", base + ".", base + " ", str(tmp_path) + "/ " + "/n.glb"):
        refused = _confine(tmp_path, value)
        assert refused["ok"] is False, value
    assert "colon" in _confine(tmp_path, base + ":hidden")["error"]
    assert "dot or a space" in _confine(tmp_path, base + ".")["error"]


def test_a_floater_report_survives_sanitising_and_lexical_confinement_alongside_the_result_files(tmp_path):
    """The connectivity harness reports through ``geometry`` (a result key, not a file): it must come out of
    ``sanitize_result`` and ``confine_files`` intact next to the exact-name result files."""
    for fname in ("n.glb", "n_sheet.png"):
        (tmp_path / fname).write_text("x")
    geometry = {"parts": 2, "height": 1.0, "dims": [1.0, 1.0, 1.0], "detached": [],
                "floating": [{"parts": ["loaf"], "part_count": 1, "lines": [34], "centre": [0.0, 0.0, 1.0],
                              "gap": 0.12, "gap_vec": [0.0, 0.0, 0.12], "nearest": "plate"}]}
    raw = {"ok": True, "tris": 10, "within_budget": True, "geometry": geometry,
           "files": {"model": str(tmp_path / "n.glb"), "sheet": str(tmp_path / "n_sheet.png")}}
    cleaned = runner.confine_files(runner.sanitize_result(raw), tmp_path, "n")
    assert cleaned.get("ok") is True and cleaned["geometry"] == geometry
    assert cleaned["files"] == raw["files"]
