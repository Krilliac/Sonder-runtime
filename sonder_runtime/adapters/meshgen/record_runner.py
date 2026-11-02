"""Run the build-script recorder in a separate, resource-limited Python process.

The in-process recorder charges work in proportion to the elements it touches, but a hard backstop must not
depend on that accounting being complete: this launcher runs ``compile_script`` in ``python -I`` (same
interpreter, no environment, no site customisation) with a wall-clock kill and a memory ceiling, and takes the
answer back as a bounded JSON file that is validated again on the host.  A timeout, a crash or an over-large
answer becomes the same ``ScriptError`` the model sees for any script that does too much work.

State: nothing is kept between calls (a private temp directory per call).  Thread-safe: every call owns its
process and directory.  The recorder needs no sandbox (it reads nothing but the job file), only limits.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ...domain.meshgen.dsl import opschema
from ...domain.meshgen.dsl.errors import ScriptError
from ...domain.meshgen.dsl.manifest import build_manifest, empty_manifest
from ...domain.meshgen.dsl.recorder import Recording
from .blender_runner import run_limited

WORKER_PATH = Path(__file__).with_name("recorder_worker.py")
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MEMORY_MB = 1024
LIMIT_MESSAGE = "the script did too much work (time/memory limit)"
_MAX_RESULT_BYTES = opschema.MAX_JSON_BYTES + 64 * 1024


def _limit_error(detail: str = "") -> ScriptError:
    return ScriptError(0, LIMIT_MESSAGE)


def record_script(source, *, kit_source=None, kit_module=None, seed=0, timeout=DEFAULT_TIMEOUT_S,
                  memory_mb=DEFAULT_MEMORY_MB, worker=WORKER_PATH) -> Recording:
    """Record ``source`` in a child process; raises ``ScriptError`` (and nothing else) on any failure.

    ``worker`` is the entry script (tests inject a slow or crashing one); ``timeout`` is the hard kill in seconds.
    """
    manifest = build_manifest(kit_source, kit_module) if kit_source is not None and kit_module else empty_manifest()
    if not sys.executable:
        raise ScriptError(0, "the recorder process could not be started (no Python interpreter path)")
    with tempfile.TemporaryDirectory(prefix="meshgen-rec-") as folder:
        root = Path(folder)
        job = root / "job.json"
        result = root / "result.json"
        job.write_text(json.dumps({"source": source, "kit_source": kit_source, "kit_module": kit_module,
                                   "seed": int(seed)}), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key.upper() in ("SYSTEMROOT", "WINDIR")}
        env["TEMP"] = env["TMP"] = str(root)
        try:
            run_limited([sys.executable, "-I", str(worker), str(job), str(result)], cwd=str(root), env=env,
                        timeout=float(timeout), memory_mb=int(memory_mb))
        except subprocess.TimeoutExpired:
            raise _limit_error() from None
        except OSError as exc:
            raise ScriptError(0, "the recorder process could not be started (%s)" % exc.__class__.__name__) from None
        try:
            if not result.is_file() or result.stat().st_size > _MAX_RESULT_BYTES:
                raise _limit_error()
            answer = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise _limit_error() from None
    if not isinstance(answer, dict):
        raise _limit_error()
    if answer.get("ok") is False:
        line, message = answer.get("line"), answer.get("message")
        raise ScriptError(line if isinstance(line, int) and not isinstance(line, bool) else 0,
                          message if isinstance(message, str) else LIMIT_MESSAGE)
    text = answer.get("text")
    if answer.get("ok") is not True or not isinstance(text, str) or len(text) > opschema.MAX_JSON_BYTES:
        raise _limit_error()
    try:
        document = json.loads(text)
        opschema.validate_ops(document, manifest.functions)  # the host re-checks what the child produced
    except (ValueError, opschema.OpsError):
        raise ScriptError(0, "the recorder returned an invalid recording") from None
    return Recording(document, text)


__all__ = ["DEFAULT_MEMORY_MB", "DEFAULT_TIMEOUT_S", "LIMIT_MESSAGE", "WORKER_PATH", "record_script"]
