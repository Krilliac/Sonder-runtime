"""Small local permission policy for tool/action visibility.

Rules are intentionally simple and auditable. They do not replace the OS,
filesystem guardrails, or Codex approvals; they give Sonder a stable place to
record local preferences such as "ask before file_delete" or "allow status".

SPEC-3 Phase 5: the pure defaults, action validation, rule normalization,
and glob evaluation live in ``sonder_runtime.domain.execution.policy``;
this module keeps the filesystem load/save and delegates the logic, with
identical behavior.

Load failures are *reported*, not *changed*. ``load`` stays total -- it never
raises into a caller and always returns a usable rule list, falling back to the
built-in defaults exactly as before. What is new is that the fallback is no
longer silent: every load also produces a :class:`LoadReport` saying whether the
policy file parsed and how many individual rules were discarded. The partial
case is the dangerous one -- ten of eleven rules load, nothing looks wrong, and
the discarded rule is the one that said ``deny``. A tool whose only rule was
dropped stops matching anything and falls through to the permissive
``NO_MATCH_RULE`` ("ask"), silently downgrading a denial.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sonder_runtime.domain.execution import policy as _policy

VALID_ACTIONS = set(_policy.VALID_ACTIONS)
DEFAULT_RULES = _policy.DEFAULT_RULES

_log = logging.getLogger(__name__)

# Load outcomes. Only OK and MISSING are healthy: an absent policy file is the
# normal first-run state, and defaults are the intended answer there.
LOAD_OK = "ok"
LOAD_MISSING = "missing"
LOAD_UNREADABLE = "unreadable"
LOAD_INVALID_JSON = "invalid-json"
LOAD_NOT_A_LIST = "not-a-list"
LOAD_PARTIAL = "partial"
LOAD_NO_USABLE_RULES = "no-usable-rules"

_HEALTHY_STATUSES = frozenset({LOAD_OK, LOAD_MISSING})

_MAX_LABEL = 60


def _clip(text):
    text = str(text)
    return text if len(text) <= _MAX_LABEL else text[: _MAX_LABEL - 3] + "..."


def _describe_exc(exc):
    return "%s: %s" % (type(exc).__name__, _clip(exc))


def _describe_dropped(index, item):
    """Name a discarded rule well enough to find it in permissions.json."""
    if isinstance(item, dict):
        pattern = str(item.get("pattern", "")).strip() or "<no pattern>"
        action = str(item.get("action", "")).strip() or "<no action>"
        return "#%d pattern=%s action=%s" % (index, _clip(pattern), _clip(action))
    return "#%d <not an object: %s>" % (index, type(item).__name__)


@dataclass(frozen=True)
class LoadReport:
    """What happened on the last policy load. Purely informational."""

    path: str
    status: str
    used_defaults: bool
    rules_seen: int = 0
    rules_kept: int = 0
    dropped: tuple = field(default=())
    error: str = ""

    @property
    def rules_dropped(self):
        return len(self.dropped)

    @property
    def degraded(self):
        """True when the on-disk policy did not fully take effect."""
        return self.status not in _HEALTHY_STATUSES

    def describe(self):
        defaults = len(DEFAULT_RULES)
        if self.status == LOAD_OK:
            return "loaded %d rule(s) from %s" % (self.rules_kept, self.path)
        if self.status == LOAD_MISSING:
            return "no policy file at %s; using %d built-in default rule(s)" % (
                self.path,
                defaults,
            )
        if self.status == LOAD_UNREADABLE:
            return "could not read %s (%s); using %d built-in default rule(s)" % (
                self.path,
                self.error,
                defaults,
            )
        if self.status == LOAD_INVALID_JSON:
            return "%s is not valid JSON (%s); using %d built-in default rule(s)" % (
                self.path,
                self.error,
                defaults,
            )
        if self.status == LOAD_NOT_A_LIST:
            return (
                "%s must hold a JSON list of rules (%s); "
                "using %d built-in default rule(s)"
            ) % (self.path, self.error, defaults)
        if self.status == LOAD_NO_USABLE_RULES:
            return (
                "%s yielded no usable rules (%d seen, %d dropped: %s); "
                "using %d built-in default rule(s)"
            ) % (
                self.path,
                self.rules_seen,
                self.rules_dropped,
                "; ".join(self.dropped) or "none",
                defaults,
            )
        # LOAD_PARTIAL: the quiet, dangerous one. Spell out the consequence.
        return (
            "%s: dropped %d malformed rule(s) of %d [%s]; %d rule(s) in effect -- "
            "any tool matched only by a dropped rule now falls through to '%s'"
        ) % (
            self.path,
            self.rules_dropped,
            self.rules_seen,
            "; ".join(self.dropped),
            self.rules_kept,
            _policy.NO_MATCH_RULE["action"],
        )


# Warn once per distinct problem, not once per permission check: ``check`` calls
# ``load`` on every tool invocation, so an unconditional warning would flood the
# log and train operators to ignore it.
_warned = set()


def reset_load_warnings():
    """Re-arm the once-per-problem load warning (tests, long-lived processes)."""
    _warned.clear()


def _warn_once(report):
    if not report.degraded:
        # A path that recovered should warn again if it breaks a second time.
        for key in [k for k in _warned if k[0] == report.path]:
            _warned.discard(key)
        return
    key = (report.path, report.status, report.dropped, report.error)
    if key in _warned:
        return
    _warned.add(key)
    _log.warning("permission policy degraded: %s", report.describe())


def policy_path(home):
    return Path(home) / "permissions.json"


def _load_report(home):
    path = policy_path(home)
    if not path.exists():
        return _policy.default_rules(), LoadReport(str(path), LOAD_MISSING, True)
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return _policy.default_rules(), LoadReport(
            str(path), LOAD_UNREADABLE, True, error=_describe_exc(exc)
        )
    try:
        data = json.loads(raw)
    except Exception as exc:
        return _policy.default_rules(), LoadReport(
            str(path), LOAD_INVALID_JSON, True, error=_describe_exc(exc)
        )
    if not isinstance(data, list):
        return _policy.default_rules(), LoadReport(
            str(path),
            LOAD_NOT_A_LIST,
            True,
            error="found %s" % type(data).__name__,
        )

    # Same filtering as _policy.normalize_rules, item by item so the discarded
    # entries can be named instead of vanishing.
    rules = []
    dropped = []
    for index, item in enumerate(data):
        rule = _policy.normalize_rule(item)
        if rule is None:
            dropped.append(_describe_dropped(index, item))
        else:
            rules.append(rule)

    if not rules:
        return _policy.default_rules(), LoadReport(
            str(path),
            LOAD_NO_USABLE_RULES,
            True,
            rules_seen=len(data),
            rules_kept=0,
            dropped=tuple(dropped),
        )
    return rules, LoadReport(
        str(path),
        LOAD_PARTIAL if dropped else LOAD_OK,
        False,
        rules_seen=len(data),
        rules_kept=len(rules),
        dropped=tuple(dropped),
    )


def load_report(home):
    """Return ``(rules, LoadReport)``. Total: never raises, always usable rules.

    A permission loader that throws is worse than one that is quiet, so every
    unexpected failure still degrades to the built-in defaults -- it is just
    labelled now.
    """
    try:
        return _load_report(home)
    except Exception as exc:
        return _policy.default_rules(), LoadReport(
            str(home), LOAD_UNREADABLE, True, error=_describe_exc(exc)
        )


def load(home):
    rules, report = load_report(home)
    _warn_once(report)
    return rules


def save(home, rules):
    path = policy_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
    return str(path)


def add_rule(home, pattern, action, note=""):
    # Note: on a corrupt/partial policy file this writes back the *loaded* rules,
    # so unparseable entries are not preserved. That is pre-existing behavior and
    # deliberately unchanged here; the load warning now fires before the write.
    rules = _policy.upsert_rule(load(home), pattern, action, note)
    save(home, rules)
    return rules


def check(home, tool_name):
    return _policy.evaluate(load(home), tool_name)


def format_policy(home, tool_name=""):
    rules, report = load_report(home)
    _warn_once(report)
    if tool_name:
        rule = _policy.evaluate(rules, tool_name)
        lines = [
            "permission check: %s" % tool_name,
            "  action: %s" % rule["action"],
            "  matched: %s" % rule["pattern"],
            "  note: %s" % rule["note"],
        ]
    else:
        lines = ["sonder permission rules", "  path: %s" % report.path]
        for rule in rules:
            lines.append("  %(action)-5s %(pattern)-32s %(note)s" % rule)
    if report.degraded:
        lines.append("  WARNING: %s" % report.describe())
    return "\n".join(lines)
