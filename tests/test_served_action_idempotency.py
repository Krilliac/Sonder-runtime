"""HTTP long-running control retries must not duplicate work."""

import server
import sonder_lifecycle
import sonder_serve as serve
import served_action_receipts


def _receipt_store(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SONDER_SERVED_ACTION_RECEIPTS_DB", str(tmp_path / "receipts.db")
    )
    served_action_receipts.reset_for_tests()


def _account(name):
    return {"account": {"username": name, "role": "developer"}}


def test_work_retry_is_idempotent_per_principal_and_exact_action(monkeypatch):
    sonder_lifecycle.reset_for_tests()


def test_restart_never_replays_a_completed_action(monkeypatch, tmp_path):
    _receipt_store(monkeypatch, tmp_path)
    sonder_lifecycle.reset_for_tests()
    calls = []
    context = _account("alice")
    action = "workbench\0demo\0/work repair"

    assert serve._idempotent_http_action(
        context, "lost-response", action, lambda: calls.append("run") or "done"
    ) == "done"
    # A fresh lifecycle models a replaced process: RAM coalescing is gone but
    # the durable receipt remains, so the mutator cannot run a second time.
    sonder_lifecycle.reset_for_tests()
    replay = serve._idempotent_http_action(
        context, "lost-response", action, lambda: calls.append("replayed") or "bad"
    )
    assert replay.startswith("ERROR: this idempotent action already completed")
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
    # This represents a process termination after admission and before a
    # response/terminal receipt. A new process must not execute the factory.
    sonder_lifecycle.reset_for_tests()
    replay = serve._idempotent_http_action(
        context, "interrupted", action, lambda: calls.append("replayed") or "bad"
    )
    assert replay.startswith("ERROR: this idempotent action has an uncertain")
    assert calls == []
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
