"""General-purpose grounding checks beyond code execution.

These checks score text/artifacts against deterministic criteria so Sonder can
learn from domains where "compiled" is not the right success signal.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

# A regex check runs a caller-supplied pattern.  Python's ``re`` backtracks
# without any step limit and cannot be interrupted from another thread, so a
# pattern such as ``^(a+)+$`` would pin a CPU and stall the caller for minutes
# or longer.  The match therefore runs in a short-lived child interpreter that
# is killed at the deadline; the pattern length is capped as well.
MAX_REGEX_PATTERN_CHARS = 2000
REGEX_TIMEOUT_SECONDS = 5.0
_REGEX_CHILD = (
    "import json,re,sys;"
    "d=json.loads(sys.stdin.buffer.read().decode('ascii'));"
    "sys.stdout.write('1' if re.search(d['p'],d['t'],re.MULTILINE) else '0')"
)


def _regex_search(pattern, text):
    """Return ``(matched, detail_suffix)``; ``matched`` is False on timeout or failure."""
    if len(pattern) > MAX_REGEX_PATTERN_CHARS:
        raise ValueError(
            "regex pattern exceeds %d characters" % MAX_REGEX_PATTERN_CHARS
        )
    # Compile here so an invalid pattern still raises re.error to the caller.
    re.compile(pattern, re.MULTILINE)
    payload = json.dumps({"p": pattern, "t": text}).encode("ascii")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _REGEX_CHILD],
            input=payload,
            capture_output=True,
            timeout=REGEX_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, " (timed out after %gs)" % REGEX_TIMEOUT_SECONDS
    output = completed.stdout.strip()
    if completed.returncode != 0 or output not in (b"0", b"1"):
        return False, " (regex worker failed)"
    return output == b"1", ""


def _as_text(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def evaluate(artifact, checks):
    """Run deterministic checks against `artifact`.

    checks may be a list of objects:
      {"type": "contains", "text": "..."}
      {"type": "not_contains", "text": "..."}
      {"type": "regex", "pattern": "..."}
      {"type": "exact", "text": "..."}
      {"type": "json"} or {"type": "json_field", "path": "a.b", "equals": 3}
    Returns {ok, results}.
    """
    if not isinstance(checks, list):
        raise ValueError("checks must be a list")
    text = _as_text(artifact)
    parsed_json = None
    results = []
    for i, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ValueError("check %d must be an object" % (i + 1))
        kind = (check.get("type") or "contains").strip().lower()
        ok = False
        detail = ""
        if kind == "contains":
            needle = str(check.get("text", ""))
            ok = needle in text
            detail = "contains %r" % needle
        elif kind == "not_contains":
            needle = str(check.get("text", ""))
            ok = needle not in text
            detail = "does not contain %r" % needle
        elif kind == "regex":
            pattern = str(check.get("pattern", ""))
            ok, suffix = _regex_search(pattern, text)
            detail = "matches /%s/%s" % (pattern, suffix)
        elif kind == "exact":
            expected = str(check.get("text", ""))
            ok = text.strip() == expected.strip()
            detail = "exact text match"
        elif kind in ("json", "json_field"):
            try:
                if parsed_json is None:
                    parsed_json = json.loads(text)
                ok = True
                detail = "valid json"
            except Exception as exc:
                ok = False
                detail = "invalid json: %s" % exc
            if ok and kind == "json_field":
                current = parsed_json
                path = str(check.get("path", ""))
                for part in [p for p in path.split(".") if p]:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        ok = False
                        detail = "missing json field %s" % path
                        break
                if ok and "equals" in check:
                    ok = current == check.get("equals")
                    detail = "json field %s == %r" % (path, check.get("equals"))
        else:
            raise ValueError("unsupported check type: %s" % kind)
        results.append({"ok": bool(ok), "type": kind, "detail": detail})
    return {"ok": all(r["ok"] for r in results), "results": results}


def format_result(result):
    lines = ["grounding: %s" % ("passed" if result.get("ok") else "failed")]
    for r in result.get("results", []):
        lines.append("  [%s] %s: %s" % (
            "PASS" if r.get("ok") else "FAIL",
            r.get("type"),
            r.get("detail"),
        ))
    return "\n".join(lines)
