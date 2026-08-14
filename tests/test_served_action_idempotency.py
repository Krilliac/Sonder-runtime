"""HTTP long-running control retries must not duplicate work."""

import server
import sonder_lifecycle
import sonder_serve as serve


def _account(name):
    return {"account": {"username": name, "role": "developer"}}


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
