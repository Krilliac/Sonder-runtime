"""Pure, task-sized projections of the host's advertised tool catalog."""
from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from ..cloud_access import LEGACY_ERROR_PREFIX


_INSPECT = ("directory_tree", "file_find", "text_search", "file_read",
            "file_read_range", "repository_symbol_index", "repo_status")
_CORE = {
    "inspect": _INSPECT, "research": _INSPECT,
    "implement": _INSPECT + ("file_edit", "file_write", "text_patch", "file_check",
                              "workspace_run", "test_run", "lint_run"),
    "validate": ("test_run", "workspace_run", "lint_run", "repo_diff", "file_read", "file_check"),
    "report": _INSPECT + ("output_digest",),
}
_EXAMPLES = {
    "directory_tree": {"path": "."}, "file_find": {"query": "*.py"},
    "text_search": {"query": "symbol"}, "file_read": {"path": "file.py"},
    "file_read_range": {"path": "file.py", "start_line": 1, "end_line": 80},
    "repository_symbol_index": {"path": "."}, "repo_status": {"root": "."},
    "file_edit": {"path": "file.py", "old": "before", "new": "after"},
    "file_write": {"path": "file.py", "content": "..."},
    "text_patch": {"patch": "<unified diff>", "apply": True},
    "file_check": {"path": "file.py"},
    "workspace_run": {"program": "python", "args": ["-m", "pytest", "-q", "tests/test_x.py"]},
    "test_run": {"root": ".", "path": "tests/test_x.py"},
    "lint_run": {"root": ".", "path": "file.py"}, "repo_diff": {"root": "."},
    "output_digest": {"path": "test.log"}, "tool_help": {"query": "search"},
}
_SYNONYMS = {
    "read": ("file_read", "file_read_range", "text_search"),
    "search": ("file_find", "text_search", "repository_symbol_index"),
    "edit": ("file_edit", "file_write", "text_patch"),
    "check": ("file_check", "lint_run", "test_run"),
    "test": ("test_run", "workspace_run"),
}
_LINE = re.compile(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
_SCOPE: ContextVar[frozenset[str] | None] = ContextVar("agent_help_names", default=None)
_FOOTER = ('Reply with exactly one JSON object:\n'
           '{"tool":"tool_name","args":{...}} or {"final":"your final answer"}')


@contextmanager
def advertised_scope(names):
    """Bind visibility, never permission, for one host-owned agent dispatch."""
    token = _SCOPE.set(None if names is None else frozenset(names))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _visible_catalog(catalog):
    source = dict(catalog or {})
    names = _SCOPE.get()
    return source if names is None else {n: row for n, row in source.items() if n in names}


def tool_advertised(name):
    names = _SCOPE.get()
    return names is None or name in names


def _knob(name):
    return name in {"extra_roots", "token", "approval", "wait_seconds"} or name.startswith(
        ("max_", "timeout", "include_"))


def filtered_schema(schema):
    """Keep required fields, including required knobs, and ordinary arguments."""
    if not isinstance(schema, Mapping):
        return {}
    required = set(schema.get("required", ()))
    properties = schema.get("properties", {})
    return {
        "type": "object",
        "properties": {key: value for key, value in properties.items()
                       if key in required or not _knob(key)},
        "required": list(schema.get("required", ())),
    }


def _schema(row):
    if isinstance(row, Mapping):
        return row.get("input_schema", row.get("schema", row.get("parameters", {}))) or {}
    return getattr(row, "parameters", getattr(row, "input_schema", {})) or {}


def _placeholder(key, spec):
    if "default" in spec:
        return spec["default"]
    if spec.get("enum"):
        return spec["enum"][0]
    kind = spec.get("type")
    if kind in {"integer", "number"}:
        return spec.get("minimum", 1)
    return {"boolean": False, "array": [], "object": {}}.get(kind, "<" + key + ">")


def _example(name, row, raw=""):
    schema = filtered_schema(_schema(row))
    required = set(schema.get("required", ()))
    if name in _EXAMPLES:
        value = dict(_EXAMPLES[name])
    else:
        try:
            value, _ = json.JSONDecoder().raw_decode(raw)
        except (TypeError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
    value = {key: val for key, val in value.items() if key in required or not _knob(key)}
    for key in schema.get("required", ()):
        value.setdefault(key, _placeholder(key, schema["properties"].get(key, {})))
    return value


def render_tool_help(help_lines, *, allowlist=None, task_kind=None, catalog=None):
    """Preserve legacy help verbatim unless this run supplied an allowlist."""
    original = help_lines if isinstance(help_lines, str) else "\n".join(help_lines)
    if allowlist is None:
        return original
    raw = {m.group(1): m.group(2) for line in original.splitlines() if (m := _LINE.match(line))}
    catalog = catalog or {}
    admitted = set(allowlist) & (set(raw) | set(catalog))
    core = [name for name in _CORE.get(task_kind, _INSPECT) if name in admitted]
    lines = ["Available tools: find the relevant file or symbol.",
             "Workflow: find -> read -> edit -> run tests -> final.",
             "Use task-relevant paths; ask tool_help for arguments."]
    for name in core:
        row = catalog.get(name, {})
        if raw.get(name, "").startswith("input_schema="):
            try:
                schema, _ = json.JSONDecoder().raw_decode(raw[name][len("input_schema="):])
            except (TypeError, ValueError):
                schema = _schema(row)
            body = "input_schema=" + json.dumps(filtered_schema(schema), separators=(",", ":"))
        else:
            body = json.dumps(_example(name, row, raw.get(name, "")), separators=(",", ":"))
        lines.append(f"- {name}: {body}")
    other = sorted(admitted - set(core))
    if other:
        lines.append('other tools (same JSON shape; ask tool_help {"name": ...}): ' + ", ".join(other))
    lines.append(_FOOTER)
    return "\n".join(lines)


def tool_help_text(*, name="", query="", catalog=None):
    """Describe registered metadata only; no filesystem, model or network calls."""
    if not isinstance(name, str) or not isinstance(query, str):
        return f"{LEGACY_ERROR_PREFIX} tool_help selectors must be strings"
    name, query = name.strip(), query.strip()
    if bool(name) == bool(query):
        return f"{LEGACY_ERROR_PREFIX} tool_help requires exactly one of name or query"
    catalog = _visible_catalog(catalog)
    names = sorted(catalog)
    if name and name in catalog:
        row = catalog[name]
        schema = filtered_schema(_schema(row))
        args = ", ".join(key + (" (required)" if key in schema["required"] else "")
                         for key in schema["properties"])
        desc = row.get("description", "") if isinstance(row, Mapping) else getattr(row, "description", "")
        sentence = " ".join(str(desc).split()).split(". ", 1)[0].rstrip(".")
        example = json.dumps({"tool": name, "args": _example(name, row)}, separators=(",", ":"))
        return (f"Example: {example}\nArguments: {args or 'none'}.\n"
                f"When to use: {sentence or 'use for ' + name.replace('_', ' ')}.")[:1200]
    if name:
        closest = difflib.get_close_matches(name[:512], names, n=3, cutoff=0)
        return ("unknown tool; closest: " + ", ".join(closest))[:1200]
    term = query[:512].lower()
    hits = [n for n in names if term in n.lower()]
    hits += [n for n in _SYNONYMS.get(term, ()) if n in catalog and n not in hits]
    return "matching tools: " + (", ".join(hits[:5]) if hits else "none")


tool_help = tool_help_text
