"""Unattended permission decisions leave a durable, content-free receipt.

``permission_modes.decide`` is pure and opens no database, so the receipt is an
observer's job: the composition root installs
``sonder_runtime.adapters.security.permission_receipts`` and every unattended
refusal -- or allow of anything but a ``safe`` read -- becomes one event on
the application event sink. These tests pin what is recorded, what is not
(arguments, paths, prompts), which decisions are worth a receipt at all, and
that a broken sink can neither block nor change a decision.
"""
from __future__ import annotations

import pytest

import permission_modes as pm
from sonder_runtime.adapters.security import permission_receipts

pytestmark = pytest.mark.unit


class _Sink:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def emit(self, event_code, *, summary, detail=None, severity="INFO",
             correlation_id=None, operation_id=None):
        if self.fail:
            raise RuntimeError("sink is down")
        self.events.append({
            "code": event_code, "summary": summary, "detail": dict(detail or {}),
            "severity": severity, "correlation_id": correlation_id,
        })


@pytest.fixture
def sink():
    """A collecting sink installed for the test and removed afterwards.

    The legacy server module installs the default sink when it loads and the
    composition root installs the application's; whichever was active is put
    back (``snapshot``/``restore``) so no other test file notices this one ran.
    """
    token = permission_receipts.snapshot()
    collector = _Sink()
    permission_receipts.install(lambda: collector)
    pm.reset_unattended_for_tests()
    yield collector
    permission_receipts.restore(token)
    pm.reset_unattended_for_tests()


def _no_rules(_tool):
    return None


def test_an_unattended_refusal_leaves_one_content_free_receipt(sink):
    decision = pm.decide(
        "file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
        surface="mcp",
    )
    assert decision.action == pm.DENY

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event["code"] == permission_receipts.REFUSAL_EVENT
    assert event["severity"] == "WARNING"
    assert event["detail"] == {
        "category": "permission", "tool": "file_write", "surface": "mcp",
        "mode": "manual", "risk": "mutation", "source": "unattended",
        "action": "deny",
    }
    assert "file_write" in event["summary"] and "mcp" in event["summary"]


def test_an_unattended_allow_of_an_effect_is_recorded_too(sink):
    pm.decide("file_write", mode=pm.AUTO, interactive=False, rule_lookup=_no_rules,
              surface="http")
    pm.decide("task_create", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
              surface="agent")

    codes = [event["code"] for event in sink.events]
    assert codes == [permission_receipts.ALLOW_EVENT] * 2
    assert [event["severity"] for event in sink.events] == ["INFO", "INFO"]
    assert sink.events[0]["detail"]["source"] == "mode"
    assert sink.events[1]["detail"]["source"] == "non-interactive"


def test_a_safe_read_and_an_attended_decision_leave_nothing(sink):
    pm.decide("status", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
              surface="mcp")
    pm.decide("file_write", mode=pm.MANUAL, interactive=True, rule_lookup=_no_rules,
              surface="repl")

    assert sink.events == []
    assert pm.unattended_summary().startswith(
        "unattended decisions since start: 0 refused, 0 allowed"
    )


def test_a_preflight_decision_is_never_recorded(sink):
    pm.decide("file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
              surface="preflight", record=False)

    assert sink.events == []


def test_the_receipt_never_carries_arguments_or_prompts(sink):
    """The observer only ever sees the ``Decision``; nothing else exists to leak."""
    pm.decide("file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
              surface="mcp")

    detail = sink.events[0]["detail"]
    assert set(detail) == {"category", "tool", "surface", "mode", "risk", "source", "action"}
    assert all(isinstance(value, str) for value in detail.values())


def test_the_summary_line_counts_both_outcomes_and_names_the_last(sink):
    pm.decide("file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
              surface="mcp")
    pm.decide("run_code", mode=pm.AUTO, interactive=False, rule_lookup=_no_rules,
              surface="loop")

    summary = pm.unattended_summary()
    assert "1 refused, 1 allowed" in summary
    assert "last refusal: file_write via mcp" in summary
    assert "last allow: run_code via loop" in summary


def test_an_unspecified_surface_is_named_as_such(sink):
    pm.decide("file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules)

    assert sink.events[0]["detail"]["surface"] == "unspecified"


def test_a_broken_sink_neither_blocks_nor_changes_the_decision():
    token = permission_receipts.snapshot()
    permission_receipts.install(lambda: _Sink(fail=True))
    try:
        decision = pm.decide(
            "file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
            surface="mcp",
        )
    finally:
        permission_receipts.restore(token)
    assert decision.action == pm.DENY
    assert decision.source == "unattended"


def test_an_observer_that_raises_is_skipped_for_that_decision():
    calls = []

    def bad(decision, surface):
        raise RuntimeError("observer bug")

    def good(decision, surface):
        calls.append((decision.tool, surface))

    pm.add_decision_observer(bad)
    pm.add_decision_observer(good)
    try:
        pm.decide("file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
                  surface="mcp")
    finally:
        pm.remove_decision_observer(bad)
        pm.remove_decision_observer(good)
    assert calls == [("file_write", "mcp")]


def test_install_replaces_the_previous_sink_rather_than_stacking():
    token = permission_receipts.snapshot()
    first, second = _Sink(), _Sink()
    permission_receipts.install(lambda: first)
    permission_receipts.install(lambda: second)
    try:
        pm.decide("file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
                  surface="mcp")
    finally:
        permission_receipts.restore(token)
    assert first.events == []
    assert len(second.events) == 1


def test_install_default_does_not_demote_an_application_sink():
    token = permission_receipts.snapshot()
    collector = _Sink()
    permission_receipts.install(lambda: collector)
    try:
        permission_receipts.install_default()
        assert permission_receipts.installed() == "application"
        pm.decide("file_write", mode=pm.MANUAL, interactive=False, rule_lookup=_no_rules,
                  surface="mcp")
        assert len(collector.events) == 1
    finally:
        permission_receipts.restore(token)


def test_the_legacy_server_installs_a_sink_when_it_loads():
    import server  # noqa: F401

    assert permission_receipts.installed() in ("default", "application")


def test_the_composition_root_routes_receipts_to_the_application_sink():
    from sonder_runtime.bootstrap import app as bootstrap_app

    token = permission_receipts.snapshot()
    try:
        application = bootstrap_app.build_application()
        assert permission_receipts.installed() == "application"
        assert application.events is not None
    finally:
        permission_receipts.restore(token)


def test_the_durable_store_receives_the_receipt(tmp_path):
    """End to end through the real default sink into a private operations store.

    The sink resolves the store the first time it writes, so pointing the
    process home at a fresh directory proves the receipt lands in whichever
    ``operations.db`` the running configuration names. It also keeps the read
    below out of a race: event timestamps are whole seconds, and a worker that
    has just run a few hundred unattended decisions has a few hundred receipts
    in the same second as this one.
    """
    from sonder_runtime.adapters.persistence.operations_store import OperationsStore
    from sonder_runtime.platform import paths as runtime_paths

    previous_home = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    token = permission_receipts.snapshot()
    permission_receipts.uninstall()
    permission_receipts.install_default()
    try:
        pm.decide("receipt_probe_zz", mode=pm.MANUAL, interactive=False,
                  rule_lookup=_no_rules, surface="mcp")
        store = OperationsStore()
        rows = store.recent_events(limit=50)
    finally:
        permission_receipts.restore(token)
        if previous_home is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous_home)

    assert store._db_path.startswith(str(tmp_path / "home"))
    hits = [row for row in rows if "receipt_probe_zz" in row.summary]
    assert hits, "no receipt reached %s" % store._db_path
    assert hits[-1].event_code == permission_receipts.REFUSAL_EVENT
    assert hits[-1].detail["source"] == "unclassified"
