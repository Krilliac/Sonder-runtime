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

_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
DEFAULT_TIMEOUT = 8
MAX_TIMEOUT = 60


def extract_code_block(text):
    """Return the best runnable Python block from a model response.

    Prefer explicit ```python fences. If only bare fences exist, skip blocks that
    look like shell instructions such as `/run python file.py` or `pip install`.
    """
    blocks = [
        ((lang or "").strip().lower(), body.strip())
        for lang, body in _CODE_BLOCK_RE.findall(text or "")
    ]
    for lang, body in reversed(blocks):
        if lang in ("python", "py"):
            return body
    for lang, body in reversed(blocks):
        if lang == "" and not _looks_like_shell_block(body):
            return body
    return None


def _looks_like_shell_block(body):
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return True
    first = lines[0].lower()
    shell_prefixes = (
        "/run ",
        "$ ",
        "> ",
        "python ",
        "python3 ",
        "py ",
        "pip ",
        "cd ",
        "bash ",
        "sh ",
        "powershell ",
        "pwsh ",
    )
    return first.startswith(shell_prefixes)


def _decode_timeout_stream(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def clamp_timeout(timeout, default=DEFAULT_TIMEOUT, maximum=MAX_TIMEOUT):
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def run_code_detail(code, extra="", timeout=DEFAULT_TIMEOUT, interp=None, stdin=""):
    """Run code in a fresh subprocess and return structured execution details."""
    timeout = clamp_timeout(timeout)
    interp = interp or sys.executable
    src = code + (("\n\n" + extra) if extra else "")
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            p = subprocess.run(
                [interp, path],
                input=stdin or "",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "ok": p.returncode == 0,
                "returncode": p.returncode,
                "stdout": (p.stdout or "").strip(),
                "stderr": (p.stderr or "").strip(),
                "timeout": timeout,
                "timed_out": False,
                "error": "",
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "returncode": None,
                "stdout": _decode_timeout_stream(exc.stdout).strip(),
                "stderr": _decode_timeout_stream(exc.stderr).strip(),
                "timeout": timeout,
                "timed_out": True,
                "error": "timed out after %ss" % timeout,
            }
    finally:
        os.unlink(path)


def format_run_result(result):
    """Format a structured run result for humans using the REPL/HTTP /run command."""
    if result.get("timed_out"):
        status = "timed out"
    else:
        status = "ok" if result.get("ok") else "failed"
    rc = result.get("returncode")
    lines = [
        "status: %s" % status,
        "timeout: %ss" % result.get("timeout"),
        "returncode: %s" % ("(none)" if rc is None else rc),
    ]
    if result.get("error"):
        lines.append("error: %s" % result["error"])
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if "EOFError" in stderr:
        lines.append(
            "note: this program tried to read keyboard input, but /run is "
            "non-interactive. Generate a scripted smoke test or pass data another way."
        )
    if result.get("timed_out"):
        lines.append(
            "note: the process was still running. For /run, games and demos need "
            "a bounded smoke-test path or an auto-exit timer."
        )
    if stdout:
        lines.extend(["", "stdout:", stdout])
    if stderr:
        lines.extend(["", "stderr:", stderr])
    if not stdout and not stderr:
        lines.extend(["", "(no stdout/stderr captured)"])
    return "\n".join(lines)


def run_code(code, extra="", timeout=DEFAULT_TIMEOUT, interp=None):
    """Run `code` (plus optional `extra` appended, e.g. assertions) in a fresh
    subprocess. Returns (ok, output) where ok is True iff the process exited 0,
    and output is combined stdout+stderr.
    """
    result = run_code_detail(code, extra=extra, timeout=timeout, interp=interp)
    output = "\n".join(
        part for part in (
            result.get("stdout") or "",
            result.get("stderr") or "",
            result.get("error") or "",
        )
        if part
    ).strip()
    return result.get("ok") is True, output
