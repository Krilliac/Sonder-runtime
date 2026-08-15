"""HTTP long-running control retries must not duplicate work."""

import server
import served_action_receipts
import sonder_lifecycle
import sonder_serve as serve
from types import SimpleNamespace


def _account(name):
    return {"account": {"username": name, "role": "developer"}}


def _authorized_account(name):
    return {
        "mode": "account",
        "authorized": True,
        "api_key": False,
        "account": {"username": name, "role": "developer"},
    }


def _receipt_store(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SONDER_SERVED_ACTION_RECEIPTS_DB", str(tmp_path / "receipts.db")
    )
    served_action_receipts.reset_for_tests()


def test_restart_never_replays_a_completed_action(monkeypatch, tmp_path):
    _receipt_store(monkeypatch, tmp_path)
    sonder_lifecycle.reset_for_tests()
    calls = []
    context = _account("alice")
    action = "workbench\0demo\0/work repair"
    assert serve._idempotent_http_action(
        context, "lost-response", action, lambda: calls.append("run") or "done"
    ) == "done"
    sonder_lifecycle.reset_for_tests()
    replay = serve._idempotent_http_action(
        context, "lost-response", action, lambda: calls.append("replayed") or "bad"
    )
    assert replay.startswith("idempotent action refused: it already completed")
    assert calls == ["run"]
    sonder_lifecycle.reset_for_tests()


def test_interrupted_action_is_uncertain_and_never_replayed(monkeypatch, tmp_path):
    _receipt_store(monkeypatch, tmp_path)
    sonder_lifecycle.reset_for_tests()
    calls = []
    context = _account("alice")
    action = "natural-work\0demo\0/build"
    key = serve._http_action_idempotency_key(context, "interrupted", action)
    assert served_action_receipts.claim(key) == "claimed"
    sonder_lifecycle.reset_for_tests()
    replay = serve._idempotent_http_action(
        context, "interrupted", action, lambda: calls.append("replayed") or "bad"
    )
    assert replay.startswith("idempotent action refused: it has an uncertain")
    assert calls == []
    sonder_lifecycle.reset_for_tests()


def test_work_retry_is_idempotent_per_principal_and_exact_action(monkeypatch):
    sonder_lifecycle.reset_for_tests()
    calls = []
    monkeypatch.setattr(
        server, "workbench_agent",
        lambda **kwargs: calls.append(kwargs) or "work complete",
    )

    first = serve._handle_slash(
        "/work inspect and repair", project="demo", context={"api_key": True},
        idempotency_key="lost-connection-1",
    )
    retry = serve._handle_slash(
        "/work inspect and repair", project="demo", context={"api_key": True},
        idempotency_key="lost-connection-1",
    )
    changed_action = serve._handle_slash(
        "/work inspect and validate", project="demo", context={"api_key": True},
        idempotency_key="lost-connection-1",
    )

    assert (first, retry, changed_action) == (
        "work complete", "work complete", "work complete",
    )
    assert len(calls) == 2
    sonder_lifecycle.reset_for_tests()


def test_action_replay_keys_do_not_cross_account_principals():
    sonder_lifecycle.reset_for_tests()
    calls = []
    action = "autopilot\0demo\0/autopilot start inspect"

    assert serve._idempotent_http_action(
        _account("alice"), "same-client-key", action,
        lambda: calls.append("alice") or "alice result",
    ) == "alice result"
    assert serve._idempotent_http_action(
        _account("bob"), "same-client-key", action,
        lambda: calls.append("bob") or "bob result",
    ) == "bob result"
    assert calls == ["alice", "bob"]
    sonder_lifecycle.reset_for_tests()


def test_natural_execution_retry_reuses_only_the_same_caller_action(monkeypatch):
    sonder_lifecycle.reset_for_tests()
    calls = []
    monkeypatch.setattr(
        server, "route_work_request",
        lambda prompt, project="": calls.append((prompt, project)) or "started",
    )

    assert serve._handle_work_intent(
        "build and test the app", project="demo", authorized=True,
        context=_account("alice"), idempotency_key="natural-retry",
    ) == "started"
    assert serve._handle_work_intent(
        "build and test the app", project="demo", authorized=True,
        context=_account("alice"), idempotency_key="natural-retry",
    ) == "started"
    assert calls == [("build and test the app", "demo")]
    sonder_lifecycle.reset_for_tests()


def test_non_work_chat_does_not_consume_action_replay_cache(monkeypatch):
    calls = []

    class Lifecycle:
        def idempotent(self, *_args):
            calls.append(_args)
            raise AssertionError("ordinary chat must not enter action cache")

    monkeypatch.setattr(sonder_lifecycle, "get", lambda: Lifecycle())

    assert serve._handle_work_intent(
        "tell me a joke", authorized=True, context=_account("alice"),
        idempotency_key="ordinary-chat-key",
    ) is None
    assert calls == []


def _fanout_handler(monkeypatch, run):
    """Small handler double for direct fanout mutation dispatch."""
    sent = []
    handler = SimpleNamespace(
        headers={"Idempotency-Key": "direct-retry"},
        _send_auth_error=lambda: sent.append(("auth", None, None)),
        _send_json_payload=lambda payload, status=200, headers=None: sent.append(
            (payload, status, headers)
        ),
        _fanout_run_for_context=lambda _context, _run_id: (run, None),
    )
    return handler, sent


def test_direct_fanout_synthesis_retry_runs_once_and_binds_model(monkeypatch):
    sonder_lifecycle.reset_for_tests()
    calls = []
    handler, sent = _fanout_handler(monkeypatch, {"id": "fan-1"})
    monkeypatch.setattr(serve.admin_auth, "rate_limit", lambda *_args: (True, ""))
    monkeypatch.setattr(server, "_open_db", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        server, "_fanout_synthesize_run",
        lambda run, model: calls.append((run["id"], model)) or {"answer": model},
    )

    for _ in range(2):
        assert serve.Handler._handle_fanout_post(
            handler,
            "/v1/fanout/fan-1/synthesize",
            {"synth_model": "local-a"},
            _authorized_account("alice"),
        )
    assert calls == [("fan-1", "local-a")]
    assert [payload for payload, _status, _headers in sent] == [
        {"answer": "local-a"}, {"answer": "local-a"},
    ]

    assert serve.Handler._handle_fanout_post(
        handler,
        "/v1/fanout/fan-1/synthesize",
        {"synth_model": "local-b"},
        _authorized_account("alice"),
    )
    assert calls == [("fan-1", "local-a"), ("fan-1", "local-b")]
    sonder_lifecycle.reset_for_tests()


def test_direct_fanout_resume_and_cancel_retries_do_not_repeat_actions(monkeypatch):
    sonder_lifecycle.reset_for_tests()
    calls = []
    handler, _sent = _fanout_handler(monkeypatch, {"id": "fan-2"})
    monkeypatch.setattr(
        server, "fanout_store",
        SimpleNamespace(
            request_cancel=lambda run_id: calls.append(("cancel", run_id)) or {},
            resume_run=lambda run_id, **kwargs: calls.append(("resume", run_id, kwargs)) or {},
        ),
    )
    monkeypatch.setattr(server, "_execute_fanout_run", lambda run_id: calls.append(("execute", run_id)))
    monkeypatch.setattr(server, "_fanout_receipt", lambda _run_id: {"ok": True})

    for _ in range(2):
        assert serve.Handler._handle_fanout_post(
            handler, "/v1/fanout/fan-2/cancel", {}, _authorized_account("alice"),
        )
        assert serve.Handler._handle_fanout_post(
            handler,
            "/v1/fanout/fan-2/resume",
            {"include_failed": True, "retry_unknown": False},
            _authorized_account("alice"),
        )
    assert calls == [
        ("cancel", "fan-2"),
        ("resume", "fan-2", {"include_failed": True, "retry_unknown": False}),
        ("execute", "fan-2"),
    ]
    sonder_lifecycle.reset_for_tests()


def test_expired_completed_receipts_are_evicted_at_claim(monkeypatch, tmp_path):
    """The durable replay window is the TTL; crash evidence never expires."""
    _receipt_store(monkeypatch, tmp_path)
    base = 1_000_000.0
    ttl = served_action_receipts.completed_ttl_seconds()
    assert served_action_receipts.claim("act-k1", now=base) == "claimed"
    served_action_receipts.finish("act-k1", now=base)
    # act-k2 is deliberately left 'started': an interrupted action.
    assert served_action_receipts.claim("act-k2", now=base) == "claimed"
    later = base + ttl + 1
    # A claim past the window prunes the expired completed receipt ...
    assert served_action_receipts.claim("act-k3", now=later) == "claimed"
    # ... so its key is re-admitted rather than refused as a stale replay.
    assert served_action_receipts.claim("act-k1", now=later) == "claimed"
    # Interrupted actions stay fail-closed regardless of age.
    assert served_action_receipts.claim("act-k2", now=later) == "started"


def test_receipt_admission_is_capacity_bounded(monkeypatch, tmp_path):
    _receipt_store(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_SERVED_RECEIPT_LIMIT", "2")
    assert served_action_receipts.claim("cap-k1", owner_scope="ow-a") == "claimed"
    assert served_action_receipts.claim("cap-k2", owner_scope="ow-a") == "claimed"
    rejected = served_action_receipts.claim("cap-k3", owner_scope="ow-a")
    assert rejected == served_action_receipts.REJECTED
    # A rejected key was never inserted, so it stays rejected while full.
    assert served_action_receipts.claim("cap-k3", owner_scope="ow-a") == (
        served_action_receipts.REJECTED
    )
    # Replays of retained receipts are never capacity-rejected.
    assert served_action_receipts.claim("cap-k1", owner_scope="ow-a") == "started"
    served_action_receipts.finish("cap-k1")
    assert served_action_receipts.claim("cap-k1", owner_scope="ow-a") == "completed"


def test_owner_scoped_admission_protects_other_principals(monkeypatch, tmp_path):
    _receipt_store(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_SERVED_RECEIPT_OWNER_LIMIT", "1")
    assert served_action_receipts.claim("own-a1", owner_scope="ow-a") == "claimed"
    assert served_action_receipts.claim("own-a2", owner_scope="ow-a") == (
        served_action_receipts.REJECTED
    )
    # One principal exhausting its budget must not starve another.
    assert served_action_receipts.claim("own-b1", owner_scope="ow-b") == "claimed"


def test_account_receipt_budget_is_per_principal(monkeypatch, tmp_path):
    _receipt_store(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_SERVED_RECEIPT_OWNER_LIMIT", "1")
    sonder_lifecycle.reset_for_tests()
    calls = []
    action = "workbench\0demo\0/work repair"

    assert serve._idempotent_http_action(
        _account("alice"), "a-key-1", action, lambda: calls.append("a1") or "done-a1"
    ) == "done-a1"
    refused = serve._idempotent_http_action(
        _account("alice"), "a-key-2", action, lambda: calls.append("a2") or "done-a2"
    )
    assert refused.startswith("idempotency receipt capacity exhausted")
    # Rejection is backpressure, not degraded execution: nothing ran.
    assert serve._idempotent_http_action(
        _account("bob"), "b-key-1", action, lambda: calls.append("b1") or "done-b1"
    ) == "done-b1"
    assert calls == ["a1", "b1"]
    # The retained receipt still refuses replay after a restart.
    sonder_lifecycle.reset_for_tests()
    replay = serve._idempotent_http_action(
        _account("alice"), "a-key-1", action, lambda: calls.append("bad") or "bad"
    )
    assert replay.startswith("idempotent action refused: it already completed")
    assert calls == ["a1", "b1"]
    sonder_lifecycle.reset_for_tests()


def test_permission_mode_retry_runs_once_and_binds_requested_mode(monkeypatch):
    sonder_lifecycle.reset_for_tests()
    calls = []
    sent = []
    handler = SimpleNamespace(
        headers={"Idempotency-Key": "mode-retry"},
        _send_json_payload=lambda payload, status=200: sent.append((payload, status)),
    )
    monkeypatch.setattr(
        serve.permission_modes,
        "set_mode",
        lambda wanted: calls.append(wanted),
    )
    monkeypatch.setattr(server, "permission_mode_data", lambda: {"mode": "auto"})

    for _ in range(2):
        serve.Handler._handle_permission_mode_post(
            handler, {"mode": "auto"}, context=_account("alice"),
        )
    serve.Handler._handle_permission_mode_post(
        handler, {"mode": "manual"}, context=_account("alice"),
    )
    assert calls == ["auto", "manual"]
    assert sent == [({"mode": "auto"}, 200)] * 3
    sonder_lifecycle.reset_for_tests()
