"""grounding — sandboxed code execution for trilobite's self-learning loop.

Stdlib only. Pulls a fenced python code block out of a model response and
actually runs it (optionally with an appended assertion-based check) in a
subprocess, so pass/fail is grounded in real execution rather than a model's
own say-so.
"""
import os
import re
import subprocess
import sys
import tempfile

_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def extract_code_block(text):
    """Return the last fenced ```python ...``` (or bare ```` ``` ````) block in text, or None."""
    blocks = _CODE_BLOCK_RE.findall(text or "")
    return blocks[-1].strip() if blocks else None


def run_code(code, extra="", timeout=8, interp=None):
    """Run `code` (plus optional `extra` appended, e.g. assertions) in a fresh
    subprocess. Returns (ok, output) where ok is True iff the process exited 0,
    and output is combined stdout+stderr.
    """
    interp = interp or sys.executable
    src = code + (("\n\n" + extra) if extra else "")
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            p = subprocess.run([interp, path], capture_output=True, text=True, timeout=timeout)
            return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
        except subprocess.TimeoutExpired:
            return False, "(timed out after %ss)" % timeout
    finally:
        os.unlink(path)
