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
_MAX_RESULT_BYTES = 64 * 1024


def _failure(detail: str) -> ScriptError:
    """The error the model sees when the recorder process could not give an answer; ``detail`` says why."""
    return ScriptError(0, detail)


def _time_error(timeout) -> ScriptError:
    return _failure("the script did too much work (time limit of %g s exceeded)" % float(timeout))


def _memory_error(memory_mb) -> ScriptError:
    return _failure("the script did too much work (memory limit of %d MB exceeded)" % int(memory_mb))


def record_script(source, *, kit_source=None, kit_module=None, seed=0, timeout=DEFAULT_TIMEOUT_S,
                  memory_mb=DEFAULT_MEMORY_MB, worker=WORKER_PATH) -> Recording:
    """Record ``source`` in a child process; raises ``ScriptError`` (and nothing else) on any failure.

    ``worker`` is the entry script (tests inject a slow or crashing one); ``timeout`` is the hard kill in seconds.
    The error text says which limit was hit: time, memory, a crash (with its exit code) or an oversized result.
    """
    manifest = build_manifest(kit_source, kit_module) if kit_source is not None and kit_module else empty_manifest()
    if not sys.executable:
        raise ScriptError(0, "the recorder process could not be started (no Python interpreter path)")
    with tempfile.TemporaryDirectory(prefix="meshgen-rec-") as folder:
        root = Path(folder)
        job = root / "job.json"
        result = root / "result.json"
        ops = root / "ops.json"
        job.write_text(json.dumps({"source": source, "kit_source": kit_source, "kit_module": kit_module,
                                   "seed": int(seed)}), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key.upper() in ("SYSTEMROOT", "WINDIR")}
        env["TEMP"] = env["TMP"] = str(root)
        try:
            code, _, stderr = run_limited([sys.executable, "-I", str(worker), str(job), str(result)], cwd=str(root),
                                          env=env, timeout=float(timeout), memory_mb=int(memory_mb))
        except subprocess.TimeoutExpired:
            raise _time_error(timeout) from None
        except OSError as exc:
            raise ScriptError(0, "the recorder process could not be started (%s)" % exc.__class__.__name__) from None
        try:
            if not result.is_file():
                if b"MemoryError" in (stderr or b""):
                    raise _memory_error(memory_mb)
                raise _failure("the recorder process stopped without an answer (exit code %s)" % code)
            if result.stat().st_size > _MAX_RESULT_BYTES:
                raise _failure("the recorder's answer was larger than %d KB" % (_MAX_RESULT_BYTES >> 10))
            answer = json.loads(result.read_text(encoding="utf-8"))  # RecursionError is handled below
            if not isinstance(answer, dict):
                raise _failure("the recorder's answer was not a JSON object")
            if answer.get("memory") is True:
                raise _memory_error(memory_mb)
            if answer.get("ok") is False:
                line, message = answer.get("line"), answer.get("message")
                if not isinstance(message, str):
                    raise _failure("the recorder's answer was malformed")
                raise ScriptError(line if isinstance(line, int) and not isinstance(line, bool) else 0, message)
            if answer.get("ok") is not True:
                raise _failure("the recorder's answer was malformed")
            if not ops.is_file():
                raise _failure("the recorder produced no recording")
            if ops.stat().st_size > opschema.MAX_JSON_BYTES:
                raise _failure("the recording is larger than %d MB" % (opschema.MAX_JSON_BYTES >> 20))
            text = ops.read_text(encoding="utf-8")
        except (OSError, ValueError, RecursionError):
            raise _failure("the recorder's answer could not be read") from None
    try:
        document = json.loads(text)
        opschema.validate_ops(document, manifest.functions)  # the host re-checks what the child produced
    except (ValueError, RecursionError, opschema.OpsError):
        raise ScriptError(0, "the recorder returned an invalid recording") from None
    return Recording(document, text)


__all__ = ["DEFAULT_MEMORY_MB", "DEFAULT_TIMEOUT_S", "WORKER_PATH", "record_script"]
