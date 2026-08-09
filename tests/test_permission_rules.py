import json
import logging

import pytest

import permission_rules
from sonder_runtime.domain.execution import policy as _policy


@pytest.fixture(autouse=True)
def _rearm_warnings():
    """Each test gets a fresh warn-once cache so log assertions are independent."""
    permission_rules.reset_load_warnings()
    yield
    permission_rules.reset_load_warnings()


def _write(home, payload):
    path = home / "permissions.json"
    path.write_text(payload, encoding="utf-8")
    return path


def _defaults_with_broken_deny():
    """The eleven built-in rules, with the file_delete DENY rule typo'd invalid."""
    rules = [dict(rule) for rule in _policy.DEFAULT_RULES]
    for rule in rules:
        if rule["pattern"] == "file_delete":
            rule["action"] = "DENY!"
    return rules


def test_permission_rules_default_match(tmp_path):
    rule = permission_rules.check(tmp_path, "file_delete")
    assert rule["action"] == "deny"


def test_permission_rules_add_rule_takes_precedence(tmp_path):
    permission_rules.add_rule(tmp_path, "file_delete", "ask", "manual approval")
    rule = permission_rules.check(tmp_path, "file_delete")
    assert rule["action"] == "ask"
    assert rule["note"] == "manual approval"


def test_permission_policy_formats_single_tool(tmp_path):
    out = permission_rules.format_policy(tmp_path, "web_search")
    assert "permission check: web_search" in out
    assert "action: ask" in out


# --- load reporting --------------------------------------------------------
#
# Before this, permission_rules.load() had `except Exception: return
# _policy.default_rules()` and delegated to normalize_rules, which drops
# individual malformed rules with no signal at all. Both failures were
# invisible: a corrupt permissions.json and a policy file whose deny rule was
# silently discarded produced byte-identical output to a healthy load.


def test_partial_load_reports_the_dropped_rule(tmp_path):
    """A dropped rule must be counted and named.

    Verified failure on the unfixed code: writing the eleven default rules with
    only file_delete's action typo'd to "DENY!" loaded 10 rules, and
    check(file_delete) returned {'action': 'ask', 'pattern': '*', 'note': 'no
    matching rule'} -- the denial had been downgraded to a prompt with nothing
    logged, printed, or returned to say so. This pins the count and the identity
    of the discarded rule so that downgrade cannot happen quietly again.
    """
    _write(tmp_path, json.dumps(_defaults_with_broken_deny()))

    rules, report = permission_rules.load_report(tmp_path)

    assert report.status == permission_rules.LOAD_PARTIAL
    assert report.degraded is True
    expected_rules = len(_policy.DEFAULT_RULES)
    assert report.rules_seen == expected_rules
    assert report.rules_kept == expected_rules - 1
    assert report.rules_dropped == 1
    assert len(rules) == expected_rules - 1
    # The dropped rule is identified specifically enough to find it on disk.
    assert any("file_delete" in entry for entry in report.dropped)
    assert any("DENY!" in entry for entry in report.dropped)


def test_partial_load_states_the_deny_to_ask_consequence(tmp_path):
    """The report must say what a dropped rule costs, not just that one dropped.

    The concrete failure is a downgrade: with file_delete's rule discarded no
    rule matches "file_delete" at all, so evaluate() falls through to
    NO_MATCH_RULE, whose action is "ask". A bare count would not tell an operator
    that a deny became a prompt.
    """
    _write(tmp_path, json.dumps(_defaults_with_broken_deny()))

    rules, report = permission_rules.load_report(tmp_path)

    # Behavior is unchanged -- still the permissive fall-through.
    assert _policy.evaluate(rules, "file_delete")["action"] == "ask"
    # ...but it is now stated.
    message = report.describe()
    assert "dropped 1 malformed rule(s) of %d" % len(_policy.DEFAULT_RULES) in message
    assert "file_delete" in message
    assert "falls through to 'ask'" in message


def test_partial_load_surfaces_warning_in_format_policy(tmp_path):
    """format_policy is the only operator-facing surface (server.permission_policy).

    Against the unfixed code, format_policy on a policy file with a dropped deny
    rule printed 12 lines that looked entirely healthy -- verified: the string
    contained neither "drop" nor "warn". Both the listing and the single-tool
    check must carry the warning, because the single-tool form is exactly where
    someone asks "is file_delete denied?" and gets "ask".
    """
    _write(tmp_path, json.dumps(_defaults_with_broken_deny()))

    listing = permission_rules.format_policy(tmp_path)
    assert "WARNING:" in listing
    assert "dropped 1 malformed rule(s) of %d" % len(_policy.DEFAULT_RULES) in listing

    single = permission_rules.format_policy(tmp_path, "file_delete")
    assert "action: ask" in single
    assert "WARNING:" in single


def test_invalid_json_is_reported_not_swallowed(tmp_path):
    """A corrupt policy file must not look like a healthy default load.

    Verified on the unfixed code: permissions.json containing "{ this is not
    json" returned exactly _policy.default_rules() -- indistinguishable from
    having no file at all, so a policy the operator believed was in force was
    not, with no diagnostic anywhere.
    """
    _write(tmp_path, "{ this is not json")

    rules, report = permission_rules.load_report(tmp_path)

    assert report.status == permission_rules.LOAD_INVALID_JSON
    assert report.degraded is True
    assert report.used_defaults is True
    assert "JSONDecodeError" in report.error
    # Default-on-failure behavior is preserved exactly, only made visible.
    assert rules == _policy.default_rules()
    assert "WARNING:" in permission_rules.format_policy(tmp_path)


def test_missing_file_is_not_reported_as_degraded(tmp_path):
    """No policy file is the normal first-run state and must stay quiet.

    A warning that fires on every healthy install is one operators learn to
    ignore, which would defeat the point of the partial-load warning above.
    """
    rules, report = permission_rules.load_report(tmp_path)

    assert report.status == permission_rules.LOAD_MISSING
    assert report.degraded is False
    assert rules == _policy.default_rules()
    assert "WARNING:" not in permission_rules.format_policy(tmp_path)


def test_healthy_file_is_not_reported_as_degraded(tmp_path):
    """A fully valid policy file must report ok, with no dropped rules."""
    _write(tmp_path, json.dumps([{"pattern": "status", "action": "allow"}]))

    rules, report = permission_rules.load_report(tmp_path)

    assert report.status == permission_rules.LOAD_OK
    assert report.degraded is False
    assert report.rules_dropped == 0
    assert len(rules) == 1
    assert "WARNING:" not in permission_rules.format_policy(tmp_path)


def test_non_list_and_all_dropped_are_distinguished(tmp_path):
    """"Not a list" and "every rule was garbage" are different repairs.

    Both fell into the same silent `return default_rules()` before. An operator
    who wrote a JSON object instead of a list needs a different fix from one
    whose every rule has a typo'd action, so the report must tell them apart.
    """
    _write(tmp_path, json.dumps({"pattern": "x", "action": "deny"}))
    rules, report = permission_rules.load_report(tmp_path)
    assert report.status == permission_rules.LOAD_NOT_A_LIST
    assert "dict" in report.error
    assert rules == _policy.default_rules()

    _write(tmp_path, json.dumps([{"pattern": "x", "action": "nope"}, "junk"]))
    rules, report = permission_rules.load_report(tmp_path)
    assert report.status == permission_rules.LOAD_NO_USABLE_RULES
    assert report.rules_seen == 2
    assert report.rules_dropped == 2
    assert rules == _policy.default_rules()
    assert "<not an object: str>" in "; ".join(report.dropped)


def test_unreadable_policy_file_degrades_without_raising(tmp_path):
    """load() must stay total: a directory where the file should be must not throw.

    A permission loader that raises is worse than one that is quiet, so the
    fallback stays -- it just gets labelled unreadable instead of vanishing into
    the old bare `except Exception`.
    """
    (tmp_path / "permissions.json").mkdir()

    rules, report = permission_rules.load_report(tmp_path)

    assert report.status == permission_rules.LOAD_UNREADABLE
    assert report.error
    assert rules == _policy.default_rules()
    assert permission_rules.check(tmp_path, "file_delete")["action"] == "deny"


def test_load_is_total_even_for_an_unusable_home():
    """Totality is a hard constraint, so prove it past the filesystem layer too.

    policy_path(home) itself can raise before any file is touched (a home object
    whose __fspath__ fails). load() must still hand the caller usable rules
    rather than propagating, because every tool dispatch goes through it.
    """

    class ExplodingHome:
        def __fspath__(self):
            raise RuntimeError("boom")

    rules, report = permission_rules.load_report(ExplodingHome())

    assert report.status == permission_rules.LOAD_UNREADABLE
    assert "boom" in report.error
    assert rules == _policy.default_rules()
    assert permission_rules.load(ExplodingHome()) == _policy.default_rules()


def test_degraded_load_logs_once_not_once_per_check(tmp_path, caplog):
    """check() calls load() on every tool dispatch; warning each time would flood.

    A per-call warning is the log-level abuse that trains operators to filter the
    line out, which loses the signal this change exists to add. One warning per
    distinct problem, re-armed if the file is repaired and breaks again.
    """
    _write(tmp_path, json.dumps(_defaults_with_broken_deny()))

    with caplog.at_level(logging.WARNING, logger="permission_rules"):
        for _ in range(5):
            permission_rules.check(tmp_path, "file_delete")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "permission policy degraded" in warnings[0].getMessage()
    assert "file_delete" in warnings[0].getMessage()

    # Repair the file, then break it again: the warning must re-arm.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="permission_rules"):
        _write(tmp_path, json.dumps([{"pattern": "status", "action": "allow"}]))
        permission_rules.check(tmp_path, "status")
        _write(tmp_path, json.dumps(_defaults_with_broken_deny()))
        permission_rules.check(tmp_path, "file_delete")

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
