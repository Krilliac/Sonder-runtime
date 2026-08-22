"""Owner-scoped live execution feed.

A caller should be able to watch its OWN work start and complete -- category,
name, state, elapsed time, safe summary -- without receiving another
principal's activity, prompts, tool arguments, paths, or model reasoning.
The process-global snapshot/feed remains an operator surface; these tests pin
the per-owner boundary that sits in front of it.
"""
from contextlib import contextmanager
import http.client
import json
import os
import threading

import pytest

import sonder_runtime.adapters.observability.activity_tracker as at
import sonder_runtime.interfaces.http.serve as ts


@pytest.fixture(autouse=True)
def _reset_activity():
    at.reset_for_tests()
    yield
    at.reset_for_tests()


# ---------------------------------------------------------------------------
# start/complete transitions and current active work
# ---------------------------------------------------------------------------

def test_running_span_is_visible_live_and_moves_to_recent_on_completion():
    with at.response_span(
        "chat:sonder", "what changed today", surface="http", feed_owner="owner-a",
    ):
        feed = at.live_feed_for_owner("owner-a")
        assert feed["known"] is True
        assert feed["owner_scoped"] is True
        (entry,) = feed["active"]
        assert entry["category"] == "response"
        assert entry["name"] == "chat:sonder"
        assert entry["state"] == "running"
        assert entry["elapsed_ms"] >= 0
        assert feed["recent"] == []

    feed = at.live_feed_for_owner("owner-a")
    assert feed["active"] == []
    (entry,) = feed["recent"]
    assert entry["state"] == "completed"
    assert entry["elapsed_ms"] >= 0


def test_failed_span_completes_as_failed():
    with pytest.raises(RuntimeError):
        with at.response_span("agent:build", "", feed_owner="owner-a"):
            raise RuntimeError("boom")
    (entry,) = at.live_feed_for_owner("owner-a")["recent"]
    assert entry["state"] == "failed"


def test_current_operation_shows_running_then_ran():
    with at.response_span("agent:task", "", feed_owner="owner-a"):
        with at.tool_dispatch_context("file_read"):
            (entry,) = at.live_feed_for_owner("owner-a")["active"]
            operation = entry["operation"]
            assert operation["category"] == "tool"
            assert operation["name"] == "Read File"
            assert operation["state"] == "running"
            assert operation["elapsed_ms"] >= 0
        # The dispatcher records the result after the dispatch context exits.
        at.record_tool_result("file_read", {}, ok=True, elapsed_ms=7)
        (entry,) = at.live_feed_for_owner("owner-a")["active"]
        operation = entry["operation"]
        assert operation["category"] == "tool"
        assert operation["name"] == "Read File"
        assert operation["state"] == "completed"

    (entry,) = at.live_feed_for_owner("owner-a")["recent"]
    assert entry["tool_calls"] == 1


def test_concurrent_worker_operations_never_clear_or_resurrect_each_other():
    """Workers bound to one response are concurrent, not a shared stack."""
    first_entered = threading.Event()
    second_entered = threading.Event()
    allow_first_exit = threading.Event()
    first_exited = threading.Event()
    allow_second_exit = threading.Event()

    with at.response_span("agent:task", "", feed_owner="owner-a"):
        response_id = at.current_response_id()

        def first_worker():
            with at.bind_response(response_id):
                with at.tool_dispatch_context("file_read"):
                    first_entered.set()
                    assert second_entered.wait(5)
                    assert allow_first_exit.wait(5)
            first_exited.set()

        def second_worker():
            with at.bind_response(response_id):
                with at.tool_dispatch_context("workspace_run"):
                    assert first_entered.wait(5)
                    second_entered.set()
                    assert allow_second_exit.wait(5)

        first = threading.Thread(target=first_worker, daemon=True)
        second = threading.Thread(target=second_worker, daemon=True)
        first.start()
        second.start()
        try:
            assert second_entered.wait(5)
            allow_first_exit.set()
            assert first_exited.wait(5)

            (entry,) = at.live_feed_for_owner("owner-a")["active"]
            assert entry["operation"]["name"] == "Ran Program"
            assert entry["operation"]["state"] == "running"
        finally:
            allow_first_exit.set()
            allow_second_exit.set()
            first.join(5)
            second.join(5)

        (entry,) = at.live_feed_for_owner("owner-a")["active"]
        assert entry["operation"] is None


def test_failed_operation_reports_failed_state():
    with at.response_span("agent:task", "", feed_owner="owner-a"):
        at.record_tool_result("workspace_run", {}, ok=False, elapsed_ms=3)
        (entry,) = at.live_feed_for_owner("owner-a")["active"]
        assert entry["operation"]["state"] == "failed"


# ---------------------------------------------------------------------------
# owner isolation
# ---------------------------------------------------------------------------

def test_completed_spans_are_isolated_per_owner():
    with at.response_span("chat:sonder", "", feed_owner="owner-a"):
        pass
    with at.response_span("chat:sonder", "", feed_owner="owner-b"):
        pass
    assert len(at.live_feed_for_owner("owner-a")["recent"]) == 1
    assert len(at.live_feed_for_owner("owner-b")["recent"]) == 1
    assert at.live_feed_for_owner("owner-c")["recent"] == []


def test_active_spans_of_another_owner_are_invisible():
    started = threading.Event()
    release = threading.Event()

    def work():
        with at.response_span("chat:sonder", "", feed_owner="owner-a"):
            started.set()
            release.wait(5)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    assert started.wait(5)
    try:
        assert at.live_feed_for_owner("owner-b")["active"] == []
        assert len(at.live_feed_for_owner("owner-a")["active"]) == 1
    finally:
        release.set()
        thread.join(5)


def test_unowned_spans_stay_in_the_local_domain():
    """Spans without a feed owner (REPL/local MCP) never leak to a principal,
    and an owner's spans never leak back into the local unowned domain."""
    with at.response_span("repl:local", ""):
        pass
    with at.response_span("chat:sonder", "", feed_owner="owner-a"):
        pass
    unowned = [entry["name"] for entry in at.live_feed_for_owner("")["recent"]]
    owned = [entry["name"] for entry in at.live_feed_for_owner("owner-a")["recent"]]
    assert unowned == ["repl:local"]
    assert owned == ["chat:sonder"]


# ---------------------------------------------------------------------------
# redaction and disclosure boundaries
# ---------------------------------------------------------------------------

def test_labels_and_summaries_are_redacted():
    with at.response_span("job token=hunter2", "", feed_owner="owner-a"):
        at.set_result_summary("done password=swordfish Bearer abc.def.ghi")
    body = json.dumps(at.live_feed_for_owner("owner-a"))
    assert "hunter2" not in body
    assert "swordfish" not in body
    assert "abc.def.ghi" not in body


def test_feed_never_carries_prompts_args_paths_or_output():
    with at.response_span(
        "chat:sonder", "the private prompt text", feed_owner="owner-a",
    ):
        at.record_tool_result(
            "file_read",
            {"path": "C:/Users/someone/private/notes.txt"},
            ok=True,
            elapsed_ms=2,
            summary="read 12 lines",
            command='"privatebin.exe" ["C:/Users/someone/private/notes.txt"]',
            output="entire private file contents",
        )
    body = json.dumps(at.live_feed_for_owner("owner-a"))
    assert "private prompt text" not in body
    assert "notes.txt" not in body
    assert "entire private file contents" not in body
    assert "privatebin.exe" not in body
    assert "read 12 lines" not in body


def test_feed_never_carries_model_reasoning():
    with at.response_span("chat:sonder", "", feed_owner="owner-a"):
        at.record_reasoning("secret chain of thought", model="sonder")
    body = json.dumps(at.live_feed_for_owner("owner-a"))
    assert "chain of thought" not in body


# ---------------------------------------------------------------------------
# bounded retention
# ---------------------------------------------------------------------------

def test_recent_entries_are_bounded_per_owner_newest_first():
    for index in range(at.MAX_OWNER_FEED_ENTRIES + 5):
        with at.response_span("job-%d" % index, "", feed_owner="owner-a"):
            pass
    recent = at.live_feed_for_owner("owner-a")["recent"]
    assert len(recent) == at.MAX_OWNER_FEED_ENTRIES
    assert recent[0]["name"] == "job-%d" % (at.MAX_OWNER_FEED_ENTRIES + 4)
    assert all(entry["name"] != "job-0" for entry in recent)


def test_owner_buckets_are_bounded_with_oldest_evicted():
    for index in range(at.MAX_FEED_OWNERS + 3):
        with at.response_span("job", "", feed_owner="owner-%d" % index):
            pass
    assert at.live_feed_for_owner("owner-0")["recent"] == []
    newest = "owner-%d" % (at.MAX_FEED_OWNERS + 2)
    assert len(at.live_feed_for_owner(newest)["recent"]) == 1


def test_reset_for_tests_clears_owner_feeds():
    with at.response_span("chat:sonder", "", feed_owner="owner-a"):
        pass
    at.reset_for_tests()
    assert at.live_feed_for_owner("owner-a")["recent"] == []


# ---------------------------------------------------------------------------
# HTTP boundary: GET /v1/sonder/feed
# ---------------------------------------------------------------------------

@contextmanager
def _http_server(monkeypatch):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.headers), response.read()
    finally:
        conn.close()


def _account_context(username):
    return {
        "mode": "account",
        "authorized": True,
        "api_key": False,
        "account": {"username": username, "role": "user"},
    }


def test_feed_endpoint_requires_authorization(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "server-key")
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    with _http_server(monkeypatch) as port:
        status, _, _ = _request(port, "GET", "/v1/sonder/feed")
    assert status == 401


def test_feed_endpoint_serves_only_the_callers_own_work(monkeypatch):
    os.environ["SONDER_AUTH_MODE"] = "account"
    alice = _account_context("alice")
    bob = _account_context("bob")
    with at.response_span(
        "chat:sonder", "", feed_owner=ts._feed_request_owner(alice),
    ):
        pass

    monkeypatch.setattr(ts.Handler, "_request_auth_context", lambda _self: alice)
    with _http_server(monkeypatch) as port:
        status, _, body = _request(port, "GET", "/v1/sonder/feed")
    assert status == 200
    payload = json.loads(body)
    assert payload["owner_scoped"] is True
    assert len(payload["recent"]) == 1

    monkeypatch.setattr(ts.Handler, "_request_auth_context", lambda _self: bob)
    with _http_server(monkeypatch) as port:
        status, _, body = _request(port, "GET", "/v1/sonder/feed")
    assert status == 200
    assert json.loads(body)["recent"] == []


def test_owner_key_is_opaque_and_bound_to_the_principal():
    os.environ["SONDER_AUTH_MODE"] = "account"
    alice = ts._feed_request_owner(_account_context("alice"))
    bob = ts._feed_request_owner(_account_context("bob"))
    assert alice and bob and alice != bob
    assert "alice" not in alice
    # Without caller authentication there is a single trusted local operator
    # and no second party to protect; the owner key collapses to the
    # unowned/local domain.
    os.environ.pop("SONDER_AUTH_MODE", None)
    os.environ.pop("SONDER_API_KEY", None)
    os.environ.pop("SONDER_REQUIRE_ACCOUNT", None)
    assert ts._feed_request_owner(_account_context("alice")) == ""


def test_http_chat_span_lands_in_the_callers_feed(monkeypatch):
    os.environ["SONDER_AUTH_MODE"] = "account"
    alice = _account_context("alice")
    monkeypatch.setattr(ts.Handler, "_request_auth_context", lambda _self: alice)
    monkeypatch.setattr(ts.server, "available_tiers", lambda: {})
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ts.server, "answer_with_history", lambda *args, **kwargs: "answer",
    )
    request = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": "hello private words"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, _ = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )
        assert status == 200
        feed_status, _, feed_body = _request(port, "GET", "/v1/sonder/feed")

    assert feed_status == 200
    payload = json.loads(feed_body)
    (entry,) = payload["recent"]
    assert entry["name"].startswith("chat")
    assert entry["state"] == "completed"
    assert entry["surface"] == "http"
    assert "hello private words" not in feed_body.decode("utf-8")
