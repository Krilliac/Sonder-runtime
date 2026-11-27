"""Pure, task-sized projections of the host's advertised tool catalog."""
from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

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
_EXAMPLES: dict[str, dict[str, Any]] = {
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
    "edit": ("file_edit", "file_write", "text_patch", "apply_patch"),
    "check": ("file_check", "lint_run", "test_run", "typecheck_run"),
    "test": ("test_run", "test_discover", "workspace_run"),
    "git": ("repo_status", "repo_diff", "repo_log", "git_commit", "git_branch"),
    "repo": ("repo_status", "repo_diff", "repo_log", "repo_show", "repo_blame"),
    "notes": ("scratchpad_read", "scratchpad_write", "scratchpad_delete", "playbook_read"),
    "scratchpad": ("scratchpad_read", "scratchpad_write", "scratchpad_delete"),
    "web": ("web_search", "web_fetch", "weather_lookup"),
    "build": ("build_run", "build_clean", "scaffold_project", "test_run"),
    "lint": ("lint_run", "format_code", "typecheck_run", "code_audit"),
    "format": ("format_code", "lint_run"),
    "math": ("math_eval", "logic_verify"),
    "task": ("task_list", "task_create", "task_update", "task_plan", "task_show"),
    "todo": ("task_list", "task_create", "task_update", "checklist_create"),
    "checklist": ("checklist_create", "checklist_show", "checklist_update"),
    "archive": ("archive_list", "archive_create", "archive_extract"),
    "document": ("document_extract", "file_read"),
    "image": ("image_inspect", "media_inspect"),
    "media": ("media_inspect", "image_inspect"),
    "sql": ("data_query", "sqlite_mutate", "data_inspect"),
    "database": ("data_query", "sqlite_mutate", "data_inspect"),
    "playbook": ("playbook_read",),
    "artifact": ("artifact_generate", "artifact_verify", "artifact_ground"),
}
_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]+")
_LINE = re.compile(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
_SCOPE: ContextVar[frozenset[str] | None] = ContextVar("agent_help_names", default=None)
_FOOTER = ('Reply with exactly one JSON object:\n'
           '{"tool":"tool_name","args":{...}} or {"final":"your final answer"}')


@contextmanager
def advertised_scope(names: Iterable[str] | None) -> Iterator[None]:
    """Bind visibility, never permission, for one host-owned agent dispatch."""
    token = _SCOPE.set(None if names is None else frozenset(names))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _visible_catalog(catalog: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(catalog or {})
    names = _SCOPE.get()
    return source if names is None else {n: row for n, row in source.items() if n in names}


def tool_advertised(name: str) -> bool:
    names = _SCOPE.get()
    return names is None or name in names


def has_active_scope() -> bool:
    """Return True when an explicit ``advertised_scope`` is bound on this context."""
    return _SCOPE.get() is not None


def _knob(name: str) -> bool:
    return name in {"extra_roots", "token", "approval", "wait_seconds"} or name.startswith(
        ("max_", "timeout", "include_"))


def filtered_schema(schema: object) -> dict[str, Any]:
    """Keep required fields, including required knobs, and ordinary arguments."""
    if not isinstance(schema, Mapping):
        return {}
    raw_required = schema.get("required", ())
    required_list = (
        list(raw_required)
        if isinstance(raw_required, Iterable) and not isinstance(raw_required, (str, bytes))
        else []
    )
    required = set(required_list)
    raw_properties = schema.get("properties", {})
    properties = raw_properties if isinstance(raw_properties, Mapping) else {}
    return {
        "type": "object",
        "properties": {key: value for key, value in properties.items()
                       if key in required or not _knob(key)},
        "required": required_list,
    }


def _schema(row: object) -> Any:
    if isinstance(row, Mapping):
        return row.get("input_schema", row.get("schema", row.get("parameters", {}))) or {}
    return getattr(row, "parameters", getattr(row, "input_schema", {})) or {}


def _placeholder(key: str, spec: Mapping[str, Any]) -> Any:
    if "default" in spec:
        return spec["default"]
    enum_values = spec.get("enum")
    if isinstance(enum_values, (list, tuple)) and enum_values:
        return enum_values[0]
    kind = spec.get("type")
    if isinstance(kind, (list, tuple)):
        kind = next((item for item in kind if isinstance(item, str) and item != "null"), None)
    if not isinstance(kind, str):
        return "<" + key + ">"
    if kind in {"integer", "number"}:
        return spec.get("minimum", 1)
    return {"boolean": False, "array": [], "object": {}}.get(kind, "<" + key + ">")


def _example(name: str, row: object, raw: str = "") -> dict[str, Any]:
    schema = filtered_schema(_schema(row))
    required = set(schema.get("required", ()))
    if name in _EXAMPLES:
        value: dict[str, Any] = dict(_EXAMPLES[name])
    else:
        try:
            decoded, _ = json.JSONDecoder().raw_decode(raw)
        except (TypeError, ValueError):
            decoded = {}
        value = decoded if isinstance(decoded, dict) else {}
    value = {key: val for key, val in value.items() if key in required or not _knob(key)}
    properties = schema.get("properties", {})
    prop_map = properties if isinstance(properties, Mapping) else {}
    for key in schema.get("required", ()):
        spec = prop_map.get(key, {})
        value.setdefault(key, _placeholder(key, spec if isinstance(spec, Mapping) else {}))
    return value


def render_tool_help(
    help_lines: str | Iterable[str],
    *,
    allowlist: Iterable[str] | None = None,
    task_kind: str | None = None,
    catalog: Mapping[str, Any] | None = None,
    compact: bool = False,
) -> str:
    """Preserve legacy help verbatim unless allowlisted or ``compact=True``."""
    original = help_lines if isinstance(help_lines, str) else "\n".join(help_lines)
    if allowlist is None and not compact:
        return original
    raw = {m.group(1): m.group(2) for line in original.splitlines() if (m := _LINE.match(line))}
    catalog = catalog or {}
    scope_names = set(raw) | set(catalog) if allowlist is None else set(allowlist)
    admitted = scope_names & (set(raw) | set(catalog))
    core = [name for name in _CORE.get(task_kind or "", _INSPECT) if name in admitted]
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
        prefix = 'other tools (same JSON shape; ask tool_help {"name": ...}): '
        full_other = prefix + ", ".join(other)
        base_len = sum(len(line) + 1 for line in lines) + len(_FOOTER) + 1
        budget = 2000 - base_len
        if len(full_other) <= budget or budget <= len(prefix):
            lines.append(full_other)
        else:
            shown: list[str] = []
            for count in range(len(other) - 1, 0, -1):
                omitted = len(other) - count
                trimmed_prefix = f'other tools (+{omitted} more; same JSON shape; ask tool_help {{"name": ...}}): '
                candidate = trimmed_prefix + ", ".join(other[:count])
                if len(candidate) <= budget:
                    shown = other[:count]
                    lines.append(candidate)
                    break
            if not shown:
                lines.append(prefix + other[0])
    lines.append(_FOOTER)
    return "\n".join(lines)


def build_agent_tool_help(
    base_text: str,
    *,
    generated_lines: list[str],
    advertised_names: set[str] | frozenset[str],
    is_refused: Callable[[str], object],
    allowlist: Iterable[str] | None = None,
    task_kind: str | None = None,
    catalog: Mapping[str, Any] | None = None,
    num_ctx: int = 0,
) -> str:
    """Combine static/generated help lines, close admit-then-deny, and size for ``num_ctx``."""
    help_text = base_text.rstrip() + "\n" + "\n".join(generated_lines) + "\n" if generated_lines else base_text
    catalog_map = dict(catalog or {})
    denied = frozenset(name for name in (set(advertised_names) | set(catalog_map)) if is_refused(name))
    filtered = help_text if not denied else "\n".join(
        line for line in help_text.splitlines()
        if not any(line.lstrip().startswith(f"- {name}:") for name in denied)
    )
    filtered_catalog = None if catalog is None else {
        name: row for name, row in catalog_map.items() if name not in denied
    }
    compact = bool(num_ctx and 0 < int(num_ctx) <= 8192)
    return render_tool_help(
        filtered,
        allowlist=None if allowlist is None else set(allowlist) - denied,
        task_kind=task_kind,
        catalog=filtered_catalog,
        compact=compact,
    )


def _row_description(row: Any) -> str:
    if isinstance(row, Mapping):
        return str(row.get("description", "") or "")
    return str(getattr(row, "description", "") or "")


def match_catalog_tools(
    *,
    name: str = "",
    query: str = "",
    catalog: Mapping[str, Any] | None = None,
    limit: int = 8,
) -> list[str]:
    """Return up to ``limit`` ranked matching tool names from ``catalog``."""
    visible = _visible_catalog(catalog)
    names = sorted(visible)
    target = str(name or "").strip()
    if target:
        return [target] if target in visible else []
    term = str(query or "").strip()[:512].lower()
    if not term:
        return []
    hits: list[str] = [n for n in names if term in n.lower()]
    for syn in _SYNONYMS.get(term, ()):
        if syn in visible and syn not in hits:
            hits.append(syn)
    tokens = [tok for tok in _QUERY_TOKEN_RE.findall(term) if len(tok) >= 2]
    if tokens:
        for tok in tokens:
            for syn in _SYNONYMS.get(tok, ()):
                if syn in visible and syn not in hits:
                    hits.append(syn)
        scored: list[tuple[int, str]] = []
        hit_set = set(hits)
        for n in names:
            if n in hit_set:
                continue
            n_low = n.lower()
            desc_low = _row_description(visible[n]).lower()
            score = 0
            for tok in tokens:
                if tok in n_low:
                    score += 3
                elif tok in desc_low:
                    score += 1
            if score > 0:
                scored.append((-score, n))
        scored.sort()
        hits.extend(n for _, n in scored)
    return hits[: max(1, int(limit))]


def tool_help_text(
    *,
    name: str = "",
    query: str = "",
    catalog: Mapping[str, Any] | None = None,
) -> str:
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
        required_keys = schema.get("required", ())
        properties = schema.get("properties", {})
        args = ", ".join(
            key + (" (required)" if key in required_keys else "")
            for key in (properties if isinstance(properties, Mapping) else {})
        )
        desc = _row_description(row)
        sentence = " ".join(desc.split()).split(". ", 1)[0].rstrip(".")
        example = json.dumps({"tool": name, "args": _example(name, row)}, separators=(",", ":"))
        return (f"Example: {example}\nArguments: {args or 'none'}.\n"
                f"When to use: {sentence or 'use for ' + name.replace('_', ' ')}.")[:1200]
    if name:
        closest = difflib.get_close_matches(name[:512], names, n=3, cutoff=0)
        return ("unknown tool; closest: " + ", ".join(closest))[:1200]
    hits = match_catalog_tools(query=query, catalog=catalog, limit=5)
    return "matching tools: " + (", ".join(hits) if hits else "none")


tool_help = tool_help_text
