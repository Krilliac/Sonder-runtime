"""Small local permission policy for tool/action visibility.

Rules are intentionally simple and auditable. They do not replace the OS,
filesystem guardrails, or Codex approvals; they give Sonder a stable place to
record local preferences such as "ask before file_delete" or "allow status".

SPEC-3 Phase 5: the pure defaults, action validation, rule normalization,
and glob evaluation live in ``sonder_runtime.domain.execution.policy``;
this module keeps the filesystem load/save and delegates the logic, with
identical behavior.

Load failures stay observable and total: callers can always render a useful
diagnostic instead of crashing. Enforcement callers also receive the
:class:`LoadReport`, so they can fail closed for non-read operations when a
policy artifact is malformed. A truly missing file remains the normal
first-run case and uses the built-in local defaults.
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
    rule, _report = check_report(home, tool_name)
    return rule


def check_report(home, tool_name):
    """Return one evaluated rule and its load report from the same snapshot.

    Permission gates need to distinguish a normal first-run default from an
    unreadable, malformed, or partial policy artifact.  Keeping the rule and
    report together avoids a second filesystem read that could observe a
    different revision while a policy is being repaired.
    """
    rules, report = load_report(home)
    _warn_once(report)
    return _policy.evaluate(rules, tool_name), report


# fnmatch's metacharacters. A pattern carrying any of them can match more than
# one tool name, which is why such a row never gets a single effective verdict.
_GLOB_CHARS = ("*", "?", "[")


def is_glob(pattern):
    """Whether a rule pattern can match more than one tool name."""
    return any(char in str(pattern) for char in _GLOB_CHARS)


def rule_lookup(rules):
    """A ``permission_modes.decide()`` rule lookup bound to a loaded snapshot.

    ``decide()``'s own default lookup re-reads and re-parses ``permissions.json``
    on every call and resolves its home itself. Both are wrong for rendering a
    table: one read per row against a file that can change between rows, from a
    home that is not the one being displayed. This binds one already-loaded
    snapshot instead, so a whole render sees a single consistent policy.

    Returns ``None`` when nothing genuinely matched, matching
    ``_default_rule_lookup``: ``evaluate`` always returns *some* rule (it falls
    back to the permissive ``NO_MATCH_RULE`` wildcard) and that fallback must
    read as "no rule", not as an explicit ask.
    """
    snapshot = list(rules)

    def lookup(tool_name):
        rule = _policy.evaluate(snapshot, tool_name)
        if rule == dict(_policy.NO_MATCH_RULE):
            return None
        return rule

    return lookup


def _both_callers(action):
    """An action, naming both callers' answers on the one row where they differ.

    ``/permissions`` is rendered with ``interactive=True`` -- the operator's
    view -- because that is the surface it is principally typed at. For a
    caller with nobody to ask (an MCP client, the app, a piped console) the
    same inputs give a different answer, and ``ask`` is the *only* verdict
    where that is true: a ``deny`` is a deny for both, an ``allow`` is an
    allow for both, and ``plan`` -- the one mode where a non-interactive
    caller is still refused -- never produces ``ask`` at all (its matrix row
    has no ASK entry). So naming both answers here, on ask rows only, turns
    this output from disclosure into truth without plumbing a caller flag
    through the renderer.
    """
    if action != "ask":
        return action
    return "ask (console) / allow (non-interactive)"


def _effective_lines(decide, tool_name):
    """The mode/risk/effective/governed-by block for one named tool."""
    if decide is None:
        return []
    try:
        decision = decide(tool_name)
        return [
            "  mode: %s" % decision.mode,
            "  risk: %s" % decision.risk,
            "  effective: %s" % _both_callers(decision.action),
            "  governed by: %s -- %s" % (decision.source, decision.reason),
        ]
    except Exception as exc:
        # Say so rather than quietly falling back to the rule-only view: the
        # whole point of these lines is that the rule alone can be misleading,
        # so their silent absence would be the same lie in a new place.
        return ["  effective: UNAVAILABLE (%s)" % _describe_exc(exc)]


def _effective_suffix(decide, rule):
    """``-> action (source)`` for a row, or "" when the row cannot have one.

    A glob row deliberately gets nothing. ``web_*`` covers tools of differing
    risk classes, so one verdict for the row would be a fresh untruth -- the
    per-tool answer belongs to the single-tool form, which is asked about a
    name rather than a pattern.
    """
    if decide is None or is_glob(rule["pattern"]):
        return ""
    try:
        decision = decide(rule["pattern"])
    except Exception as exc:
        return "  -> UNAVAILABLE (%s)" % _describe_exc(exc)
    if decision.action == "ask":
        return "  -> ask (%s) / allow (non-interactive)" % decision.source
    return "  -> %s (%s)" % (decision.action, decision.source)


def format_policy(home, tool_name="", *, decide=None, snapshot=None):
    """Render the policy; with ``decide``, render what will actually happen.

    Rules alone stopped being the whole answer when they started being
    enforced: they now compose with an autonomy mode, and the two can disagree
    (a ``deny`` rule beats even ``auto``; ``plan`` denies through an ``allow``
    rule). ``decide`` is a ``tool_name -> Decision`` callable -- inject
    ``permission_modes.decide`` bound to one pinned mode and one rule snapshot
    -- and the effective verdict plus the layer that produced it are shown
    alongside the rule. Omitted, the output is byte-for-byte what it always
    was, because every other caller has no mode to speak for.

    ``snapshot`` is the ``(rules, report)`` a caller already loaded in order to
    build ``decide``. Passing it keeps a whole render to a single read of
    ``permissions.json``; omitted, the policy is loaded here as before.
    """
    if snapshot is None:
        rules, report = load_report(home)
    else:
        rules, report = snapshot
    _warn_once(report)
    if tool_name:
        rule = _policy.evaluate(rules, tool_name)
        lines = [
            "permission check: %s" % tool_name,
            "  action: %s" % rule["action"],
            "  matched: %s" % rule["pattern"],
            "  note: %s" % rule["note"],
        ]
        lines.extend(_effective_lines(decide, tool_name))
    else:
        lines = ["sonder permission rules", "  path: %s" % report.path]
        if decide is not None:
            # Without this, a row shadowed by an earlier rule for the same tool
            # reads as though its own action were being contradicted, and the
            # absence of a verdict on a wildcard row reads as an omission.
            lines.append(
                "  -> is the effective decision for that tool name, rule and "
                "mode combined; wildcard rows cover tools of differing risk, "
                "so they get none. An 'ask' row names both callers' answers, "
                "because it is the only verdict where they differ."
            )
        for rule in rules:
            lines.append(
                "  %(action)-5s %(pattern)-32s %(note)s" % rule
                + _effective_suffix(decide, rule)
            )
    if report.degraded:
        lines.append("  WARNING: %s" % report.describe())
    return "\n".join(lines)
