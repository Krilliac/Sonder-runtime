"""Run a model-written build script in headless Blender.

``build_candidate`` writes a JSON job file, runs ``blender -b --factory-startup
--disable-autoexec --python harness.py -- job.json`` (no shell, bounded by a
timeout, ``cwd`` the output directory) and parses the harness' final
``MESHGEN_RESULT {json}`` stdout line.  The harness is defence in depth, not a
sandbox: it runs with the same authority as ``run_code``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

RESULT_MARKER = "MESHGEN_RESULT "
HARNESS_PATH = Path(__file__).with_name("harness.py")
_TAIL_LINES = 8


def locate_blender(env=os.environ) -> str | None:
    """``SONDER_BLENDER`` when it names an existing file, else ``blender`` on PATH."""
    configured = str(env.get("SONDER_BLENDER", "") or "").strip().strip('"')
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which("blender")


def build_argv(blender: str, job_path: str, harness: str = str(HARNESS_PATH)) -> list[str]:
    return [blender, "-b", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1",
            "--python", harness, "--", job_path]


def _tail(text: str) -> list[str]:
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-_TAIL_LINES:]


def parse_result(stdout: str) -> dict | None:
    """The last well-formed ``MESHGEN_RESULT`` object in ``stdout``, if any."""
    for line in reversed(str(stdout or "").splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                value = json.loads(line[len(RESULT_MARKER):])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def build_candidate(blender, code_path, out_dir, *, name, kit_path=None, max_tris,
                    height_m, export="glb", render=True, timeout=180) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    harness = HARNESS_PATH
    if not harness.is_file():
        return {"ok": False, "error": "harness missing: %s" % harness}
    job = {
        "code_path": str(Path(code_path).resolve()),
        "out_dir": str(out.resolve()),
        "name": name,
        "kit_path": str(Path(kit_path).resolve()) if kit_path else "",
        "max_tris": int(max_tris),
        "height_m": float(height_m or 0.0),
        "export": "fbx" if str(export).lower() == "fbx" else "glb",
        "render": bool(render),
    }
    job_path = out / ("%s.job.json" % name)
    job_path.write_text(json.dumps(job), encoding="utf-8")
    argv = build_argv(str(blender), str(job_path), str(harness))
    try:
        completed = subprocess.run(
            argv, cwd=str(out), shell=False, capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return {"ok": False, "error": "blender timed out after %ss" % timeout, "tail": _tail(partial)}
    except OSError as exc:
        return {"ok": False, "error": "could not start blender: %s: %s" % (type(exc).__name__, exc)}
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    result = parse_result(stdout)
    if result is None:
        return {"ok": False, "error": "no result; rc=%s" % completed.returncode,
                "tail": _tail(stdout + "\n" + stderr)}
    return result
