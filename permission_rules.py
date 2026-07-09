"""Small local permission policy for tool/action visibility.

Rules are intentionally simple and auditable. They do not replace the OS,
filesystem guardrails, or Codex approvals; they give Trilobite a stable place to
record local preferences such as "ask before file_delete" or "allow status".
"""

import fnmatch
import json
from pathlib import Path

VALID_ACTIONS = {"allow", "ask", "deny"}

DEFAULT_RULES = [
    {"pattern": "status", "action": "allow", "note": "read-only runtime status"},
    {"pattern": "context_*", "action": "allow", "note": "read-only context/status tools"},
    {"pattern": "tool_manifest", "action": "allow", "note": "read-only tool list"},
    {"pattern": "file_read", "action": "ask", "note": "reads local files"},
    {"pattern": "file_find", "action": "ask", "note": "enumerates local files"},
    {"pattern": "file_write", "action": "ask", "note": "writes local files"},
    {"pattern": "file_edit", "action": "ask", "note": "edits local files"},
    {"pattern": "file_delete", "action": "deny", "note": "destructive by default"},
    {"pattern": "run_*", "action": "ask", "note": "executes generated code"},
    {"pattern": "web_*", "action": "ask", "note": "uses network access"},
    {"pattern": "admin_private_chain_of_thought", "action": "deny", "note": "private chain-of-thought is never exposed"},
]


def policy_path(home):
    return Path(home) / "permissions.json"


def load(home):
    path = policy_path(home)
    if not path.exists():
        return [dict(rule) for rule in DEFAULT_RULES]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [dict(rule) for rule in DEFAULT_RULES]
    if not isinstance(data, list):
        return [dict(rule) for rule in DEFAULT_RULES]
    rules = []
    for item in data:
        if isinstance(item, dict):
            action = str(item.get("action", "ask")).strip().lower()
            pattern = str(item.get("pattern", "")).strip()
            if pattern and action in VALID_ACTIONS:
                rules.append({
                    "pattern": pattern,
                    "action": action,
                    "note": str(item.get("note", "")),
                })
    return rules or [dict(rule) for rule in DEFAULT_RULES]


def save(home, rules):
    path = policy_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
    return str(path)


def add_rule(home, pattern, action, note=""):
    pattern = (pattern or "").strip()
    action = (action or "").strip().lower()
    if not pattern:
        raise ValueError("pattern is required")
    if action not in VALID_ACTIONS:
        raise ValueError("action must be one of: %s" % ", ".join(sorted(VALID_ACTIONS)))
    rules = load(home)
    rules = [rule for rule in rules if rule["pattern"] != pattern]
    rules.insert(0, {"pattern": pattern, "action": action, "note": note or ""})
    save(home, rules)
    return rules


def check(home, tool_name):
    name = (tool_name or "").strip()
    for rule in load(home):
        if fnmatch.fnmatchcase(name, rule["pattern"]):
            return dict(rule)
    return {"pattern": "*", "action": "ask", "note": "no matching rule"}


def format_policy(home, tool_name=""):
    if tool_name:
        rule = check(home, tool_name)
        return (
            "permission check: %s\n"
            "  action: %s\n"
            "  matched: %s\n"
            "  note: %s"
        ) % (tool_name, rule["action"], rule["pattern"], rule["note"])
    lines = ["trilobite permission rules", "  path: %s" % policy_path(home)]
    for rule in load(home):
        lines.append("  %(action)-5s %(pattern)-32s %(note)s" % rule)
    return "\n".join(lines)
