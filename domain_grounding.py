"""General-purpose grounding checks beyond code execution.

These checks score text/artifacts against deterministic criteria so Sonder can
learn from domains where "compiled" is not the right success signal.
"""
from __future__ import annotations

import json
import re


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
            ok = bool(re.search(pattern, text, re.MULTILINE))
            detail = "matches /%s/" % pattern
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
