"""Small local permission policy for tool/action visibility.

Rules are intentionally simple and auditable. They do not replace the OS,
filesystem guardrails, or Codex approvals; they give Sonder a stable place to
record local preferences such as "ask before file_delete" or "allow status".

SPEC-3 Phase 5: the pure defaults, action validation, rule normalization,
and glob evaluation live in ``sonder_runtime.domain.execution.policy``;
this module keeps the filesystem load/save and delegates the logic, with
identical behavior.
"""

import json
from pathlib import Path

from sonder_runtime.domain.execution import policy as _policy

VALID_ACTIONS = set(_policy.VALID_ACTIONS)
DEFAULT_RULES = _policy.DEFAULT_RULES


def policy_path(home):
    return Path(home) / "permissions.json"


def load(home):
    path = policy_path(home)
    if not path.exists():
        return _policy.default_rules()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _policy.default_rules()
    rules = _policy.normalize_rules(data)
    return rules or _policy.default_rules()


def save(home, rules):
    path = policy_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
    return str(path)


def add_rule(home, pattern, action, note=""):
    rules = _policy.upsert_rule(load(home), pattern, action, note)
    save(home, rules)
    return rules


def check(home, tool_name):
    return _policy.evaluate(load(home), tool_name)


def format_policy(home, tool_name=""):
    if tool_name:
        rule = check(home, tool_name)
        return (
            "permission check: %s\n"
            "  action: %s\n"
            "  matched: %s\n"
            "  note: %s"
        ) % (tool_name, rule["action"], rule["pattern"], rule["note"])
    lines = ["sonder permission rules", "  path: %s" % policy_path(home)]
    for rule in load(home):
        lines.append("  %(action)-5s %(pattern)-32s %(note)s" % rule)
    return "\n".join(lines)
