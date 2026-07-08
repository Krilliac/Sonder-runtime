"""Small bounded code runner exposed through the MCP server.

This is intentionally lightweight: it is not a security sandbox. It gives an
agent a Claude-like way to execute short local snippets while keeping the
interface predictable: known languages, workspace-confined cwd, timeout, and
trimmed output.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time


SUPPORTED_LANGUAGES = {
    "python": {
        "aliases": {"python", "py"},
        "suffix": ".py",
        "cmd": lambda path: [sys.executable, path],
        "missing": "python executable not available",
    },
    "javascript": {
        "aliases": {"javascript", "js", "node"},
        "suffix": ".js",
        "cmd": lambda path: ["node", path],
        "missing": "node executable not found on PATH",
    },
    "powershell": {
        "aliases": {"powershell", "pwsh", "ps1"},
        "suffix": ".ps1",
        "cmd": lambda path: [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
        "missing": "PowerShell executable not found on PATH",
    },
}

DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 60
MAX_OUTPUT_CHARS = 12000
DEFAULT_LOOP_ITERATIONS = 5
MAX_LOOP_ITERATIONS = 50
MAX_LOOP_DELAY_SECONDS = 10.0


def _powershell_exe():
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def workspace_root():
    return os.path.abspath(os.path.dirname(__file__))


def normalize_language(language):
    wanted = (language or "python").strip().lower()
    for canonical, cfg in SUPPORTED_LANGUAGES.items():
        if wanted in cfg["aliases"]:
            return canonical
    raise ValueError(
        "unsupported language %r. Supported: %s"
        % (language, ", ".join(sorted(SUPPORTED_LANGUAGES)))
    )


def resolve_cwd(cwd=None):
    root = workspace_root()
    if not cwd:
        return root
    path = cwd
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    path = os.path.abspath(path)
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("cwd must stay inside workspace: %r" % cwd)
    if not os.path.isdir(path):
        raise ValueError("cwd does not exist: %r" % cwd)
    return path


def _clamp_timeout(timeout):
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(1, min(value, MAX_TIMEOUT))


def _trim_output(text, limit=MAX_OUTPUT_CHARS):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _clamp_iterations(max_iterations):
    try:
        value = int(max_iterations)
    except (TypeError, ValueError):
        value = DEFAULT_LOOP_ITERATIONS
    return max(1, min(value, MAX_LOOP_ITERATIONS))


def _clamp_delay(delay_seconds):
    try:
        value = float(delay_seconds)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(value, MAX_LOOP_DELAY_SECONDS))


def run_code(code, language="python", stdin="", timeout=DEFAULT_TIMEOUT, cwd=None):
    """Run a snippet and return a result dict.

    The caller receives:
      ok: process exited 0
      returncode: int or None on timeout/unavailable
      stdout/stderr: trimmed process streams
      language/cwd/timeout: resolved execution metadata
      error: runner-level error text, when applicable
    """
    if not (code or "").strip():
        raise ValueError("code is empty")
    language = normalize_language(language)
    timeout = _clamp_timeout(timeout)
    cwd = resolve_cwd(cwd)
    cfg = SUPPORTED_LANGUAGES[language]

    with tempfile.TemporaryDirectory(prefix="trilobite-run-") as tmp:
        path = os.path.join(tmp, "snippet" + cfg["suffix"])
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        cmd = cfg["cmd"](path)
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                input=stdin or "",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "language": language,
                "cwd": cwd,
                "timeout": timeout,
                "error": cfg["missing"],
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "returncode": None,
                "stdout": _trim_output(exc.stdout if isinstance(exc.stdout, str) else ""),
                "stderr": _trim_output(exc.stderr if isinstance(exc.stderr, str) else ""),
                "language": language,
                "cwd": cwd,
                "timeout": timeout,
                "error": "timed out after %ss" % timeout,
            }

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _trim_output(proc.stdout),
        "stderr": _trim_output(proc.stderr),
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": "",
    }


def format_result(result):
    status = "ok" if result.get("ok") else "failed"
    rc = result.get("returncode")
    lines = [
        "status: %s" % status,
        "language: %s" % result.get("language"),
        "cwd: %s" % result.get("cwd"),
        "timeout: %ss" % result.get("timeout"),
        "returncode: %s" % ("(none)" if rc is None else rc),
    ]
    if result.get("error"):
        lines.append("error: %s" % result["error"])
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if stdout:
        lines.extend(["", "stdout:", stdout])
    if stderr:
        lines.extend(["", "stderr:", stderr])
    if not stdout and not stderr and not result.get("error"):
        lines.append("")
        lines.append("(no output)")
    return "\n".join(lines)


def run_loop(
    actions,
    dispatch_action,
    max_iterations=DEFAULT_LOOP_ITERATIONS,
    stop_on_failure=True,
    stop_on_success=False,
    delay_seconds=0,
):
    """Run a bounded action loop using an injected action dispatcher.

    `dispatch_action(action)` must return a dict containing at least `ok`.
    The loop stops when a requested condition is met or max_iterations is reached.
    """
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a non-empty JSON list")
    max_iterations = _clamp_iterations(max_iterations)
    delay_seconds = _clamp_delay(delay_seconds)

    iterations = []
    stop_reason = "max_iterations reached"
    for iteration in range(1, max_iterations + 1):
        action_rows = []
        iteration_ok = True
        failed_index = None
        for index, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                result = {
                    "ok": False,
                    "type": "(invalid)",
                    "summary": "action must be an object",
                    "output": repr(action),
                }
            else:
                try:
                    result = dispatch_action(action)
                except Exception as exc:
                    result = {
                        "ok": False,
                        "type": action.get("type", "(unknown)"),
                        "summary": "%s: %s" % (exc.__class__.__name__, exc),
                        "output": "",
                    }
            if not result.get("ok"):
                iteration_ok = False
                failed_index = index
            action_rows.append({"index": index, "result": result})
            if failed_index is not None and stop_on_failure:
                break

        iterations.append({
            "iteration": iteration,
            "ok": iteration_ok,
            "actions": action_rows,
        })

        if stop_on_failure and not iteration_ok:
            stop_reason = "action %d failed in iteration %d" % (failed_index, iteration)
            break
        if stop_on_success and iteration_ok:
            stop_reason = "iteration %d succeeded" % iteration
            break
        if iteration < max_iterations and delay_seconds:
            time.sleep(delay_seconds)

    return {
        "ok": iterations[-1]["ok"] if iterations else False,
        "iterations": iterations,
        "stop_reason": stop_reason,
        "max_iterations": max_iterations,
        "delay_seconds": delay_seconds,
    }


def format_loop_result(loop_result):
    iterations = loop_result.get("iterations") or []
    lines = [
        "loop status: %s" % ("ok" if loop_result.get("ok") else "failed"),
        "iterations: %d/%d" % (len(iterations), loop_result.get("max_iterations")),
        "stop reason: %s" % loop_result.get("stop_reason"),
    ]
    for iteration in iterations:
        lines.append("")
        lines.append(
            "iteration %d: %s"
            % (iteration["iteration"], "ok" if iteration.get("ok") else "failed")
        )
        for row in iteration.get("actions", []):
            result = row["result"]
            action_type = result.get("type") or "(unknown)"
            status = "ok" if result.get("ok") else "failed"
            summary = result.get("summary") or ""
            lines.append("  [%d] %s: %s%s" % (
                row["index"],
                action_type,
                status,
                (" - " + summary) if summary else "",
            ))
            output = _trim_output(result.get("output") or "", 3000)
            if output:
                for line in output.splitlines():
                    lines.append("      " + line)
    return "\n".join(lines)
