"""Pure permission-policy rules (SPEC-3 Phase 5 extraction).

The tool-visibility policy is a small, auditable rule list evaluated by
first-match glob. This module owns the pure parts — the default rules,
valid actions, rule normalization, and evaluation — with no filesystem
I/O. ``permission_rules.py`` keeps load/save/path handling and delegates
the logic here, preserving behavior exactly.

This policy records local preferences ("ask before file_delete"); it
does not replace OS guardrails, workspace containment, or approvals.
"""
from __future__ import annotations

import fnmatch

VALID_ACTIONS = frozenset({"allow", "ask", "deny"})

DEFAULT_RULES = [
    {"pattern": "status", "action": "allow", "note": "read-only runtime status"},
    {"pattern": "context_*", "action": "allow", "note": "read-only context/status tools"},
    {"pattern": "tool_manifest", "action": "allow", "note": "read-only tool list"},
    {"pattern": "file_read", "action": "ask", "note": "reads local files"},
    {"pattern": "file_find", "action": "ask", "note": "enumerates local files"},
    {"pattern": "file_write", "action": "ask", "note": "writes local files"},
    {"pattern": "file_edit", "action": "ask", "note": "edits local files"},
    {"pattern": "file_copy", "action": "ask", "note": "copies one local file"},
    {"pattern": "file_move", "action": "ask", "note": "moves one local file"},
    {"pattern": "file_delete", "action": "deny", "note": "destructive by default"},
    {"pattern": "run_*", "action": "ask", "note": "executes generated code"},
    {"pattern": "web_*", "action": "ask", "note": "uses network access"},
    {"pattern": "admin_private_chain_of_thought", "action": "deny",
     "note": "private chain-of-thought is never exposed"},
]

NO_MATCH_RULE = {"pattern": "*", "action": "ask", "note": "no matching rule"}


def default_rules() -> list[dict]:
    return [dict(rule) for rule in DEFAULT_RULES]


def normalize_action(action: str) -> str:
    return str(action or "").strip().lower()


def is_valid_action(action: str) -> bool:
    return normalize_action(action) in VALID_ACTIONS


def normalize_rule(item) -> dict | None:
    """Coerce one raw rule dict into a valid rule, or None if unusable."""
    if not isinstance(item, dict):
        return None
    action = normalize_action(item.get("action", "ask"))
    pattern = str(item.get("pattern", "")).strip()
    if pattern and action in VALID_ACTIONS:
        return {
            "pattern": pattern,
            "action": action,
            "note": str(item.get("note", "")),
        }
    return None


def normalize_rules(data) -> list[dict]:
    """Filter/normalize a raw rules list; empty means 'use defaults'."""
    if not isinstance(data, list):
        return []
    rules = []
    for item in data:
        rule = normalize_rule(item)
        if rule is not None:
            rules.append(rule)
    return rules


def evaluate(rules, tool_name: str) -> dict:
    """First-match glob evaluation; returns a copy of the matched rule."""
    name = (tool_name or "").strip()
    for rule in rules:
        if fnmatch.fnmatchcase(name, rule["pattern"]):
            return dict(rule)
    return dict(NO_MATCH_RULE)


def upsert_rule(rules, pattern: str, action: str, note: str = "") -> list[dict]:
    """Return a new rules list with ``pattern`` set to ``action`` at the front.

    Raises ValueError on an empty pattern or invalid action (the same
    contract permission_rules.add_rule enforced inline).
    """
    pattern = (pattern or "").strip()
    action = normalize_action(action)
    if not pattern:
        raise ValueError("pattern is required")
    if action not in VALID_ACTIONS:
        raise ValueError(
            "action must be one of: %s" % ", ".join(sorted(VALID_ACTIONS))
        )
    kept = [rule for rule in rules if rule["pattern"] != pattern]
    kept.insert(0, {"pattern": pattern, "action": action, "note": note or ""})
    return kept
