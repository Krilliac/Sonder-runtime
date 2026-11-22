"""SPEC-5 thin routes ``POST /v1/recall`` and ``POST /v1/outcome`` over HTTP.

Two boundary defects, both on the thin-route branch of ``do_POST``:

* the body was read a second time after ``_read_json`` had consumed it, so an
  honest POST blocked for the read timeout and a client that sent the body
  twice got the duplicate used;
* the branch skipped the hosted-account global memory refusal, the role gate
  and the permission decision that a ``/<tool>`` spelling gets.

``/v1/outcome`` is decided as ``record_outcome`` (the same two arguments).
``/v1/recall`` pages past interaction episodes out of the memory store and
writes nothing; no catalogued tool performs that read, so it is decided under
its own name ``recall``, graded read-only like ``memory_search`` -- not under
``apply_learned``, whose rules, grade and refusal receipts belong to another
operation.
"""
from __future__ import annotations

import json
import socket
import time

import pytest

import permission_modes as pm
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.interfaces.http.handlers import OutcomeHandler, RecallHandler
from tests.test_serve_auth import _http_server

pytestmark = pytest.mark.unit

API_KEY = "k" * 40
CLIENT_TIMEOUT = 10.0


class _Memory:
    def __init__(self):
        self.recalls = []
        self.outcomes = []

    def recall(self, task, k=2, project=None):
        self.recalls.append((task, k, project))
        return ["lesson for %s" % task]

    def record(self, interaction_id, signal):
        self.outcomes.append((interaction_id, signal))
        return 1.0


@pytest.fixture
def memory(monkeypatch):
    store = _Memory()
    monkeypatch.setattr(ts, "_THIN_HANDLERS", {})
    ts.configure_thin_handlers({
        "/v1/recall": RecallHandler(store),
        "/v1/outcome": OutcomeHandler(store),
    })
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    yield store
    ts._detach_thin_handlers()


def _accounts(monkeypatch, mode):
    accounts = {
        "user-token": {"username": "ursula", "role": "user"},
        "dev-token": {"username": "dev", "role": "developer"},
        "admin-token": {"username": "ada", "role": "admin"},
    }
    monkeypatch.setattr(ts, "API_KEY", API_KEY if mode in ("api-key", "both") else "")
    monkeypatch.setattr(ts, "AUTH_MODE", mode)
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", mode in ("account", "both"))
    monkeypatch.setattr(
        ts, "_auth_account",
        lambda header: accounts.get(str(header or "").replace("Bearer ", "", 1).strip()))


def _raw_post(port, path, payload, *, extra=b"", headers=()):
    """POST over a raw socket so the test controls exactly what is on the wire."""
    body = json.dumps(payload).encode("utf-8")
    head = [
        "POST %s HTTP/1.1" % path,
        "Host: 127.0.0.1:%d" % port,
        "Content-Type: application/json",
        "Content-Length: %d" % len(body),
        "Connection: close",
    ]
    head.extend(headers)
    wire = ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body + extra
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=CLIENT_TIMEOUT) as conn:
        conn.sendall(wire)
        chunks = []
        while True:
            data = conn.recv(65536)
            if not data:
                break
            chunks.append(data)
    elapsed = time.monotonic() - started
    raw = b"".join(chunks)
    status_line, _, rest = raw.partition(b"\r\n")
    status = int(status_line.split()[1])
    response_body = rest.split(b"\r\n\r\n", 1)[1]
    return status, json.loads(response_body), elapsed


def _local_open(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)


# --- H2: the body is read once and that body is the one used ------------------

def test_recall_post_answers_promptly_from_the_single_read_body(memory, monkeypatch):
    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        status, body, elapsed = _raw_post(port, "/v1/recall", {"task": "first", "k": 3})
    assert status == 200, body
    assert body == {"results": ["lesson for first"]}
    assert memory.recalls == [("first", 3, None)]
    assert elapsed < CLIENT_TIMEOUT / 2


def test_outcome_post_uses_the_validated_body_not_a_duplicate(memory, monkeypatch):
    _local_open(monkeypatch)
    duplicate = json.dumps({"interaction_id": "SECOND", "signal": "bad"}).encode("utf-8")
    with _http_server(monkeypatch) as port:
        status, body, elapsed = _raw_post(
            port, "/v1/outcome", {"interaction_id": "FIRST", "signal": "good"},
            extra=duplicate)
    assert status == 200, body
    assert memory.outcomes == [("FIRST", "good")]
    assert elapsed < CLIENT_TIMEOUT / 2


# --- H1: the same memory refusal and role gate as /apply_learned, /record_outcome

@pytest.mark.parametrize("token", ["user-token", "dev-token", "admin-token"])
@pytest.mark.parametrize("path, payload", [
    ("/v1/recall", {"task": "secret"}),
    ("/v1/outcome", {"interaction_id": "abc", "signal": "good"}),
])
def test_hosted_account_cannot_reach_global_memory_through_thin_routes(
        memory, monkeypatch, token, path, payload):
    _accounts(monkeypatch, "account")
    with _http_server(monkeypatch) as port:
        status, body, _ = _raw_post(port, path, payload,
                                    headers=("Authorization: Bearer " + token,))
    assert status == 403, body
    assert body["error"] == "FORBIDDEN"
    assert "global memory tool is local-operator only" in body["message"]
    assert memory.recalls == [] and memory.outcomes == []


@pytest.mark.parametrize("path, payload", [
    ("/v1/recall", {"task": "secret"}),
    ("/v1/outcome", {"interaction_id": "abc", "signal": "good"}),
])
def test_unauthenticated_thin_route_is_refused(memory, monkeypatch, path, payload):
    _accounts(monkeypatch, "account")
    with _http_server(monkeypatch) as port:
        status, _body, _ = _raw_post(port, path, payload)
    assert status == 401
    assert memory.recalls == [] and memory.outcomes == []


def test_thin_route_refusal_is_the_catalogued_tool_refusal(monkeypatch):
    """The thin route and the ``/<tool>`` spelling agree for every caller."""
    from sonder_runtime.interfaces.http import thin_routes

    assert dict(thin_routes.ROUTE_TOOLS) == {"/v1/recall": "recall",
                                             "/v1/outcome": "record_outcome"}
    seen = []

    def refusal(tools, label, context=None):
        seen.append((tools, label, context))
        return "refused %s: nope" % label

    class _Handler:
        headers = {}
        sent = []

        def _send_auth_error(self):
            raise AssertionError("authorized caller")

        def _send_json_payload(self, payload, status=200):
            self.sent.append((status, payload))

    class _Never:
        def handle(self, _request):
            raise AssertionError("refused route reached its handler")

    handler, context = _Handler(), {"authorized": True, "mode": "account"}
    thin_routes.serve(handler, _Never(), "/v1/recall", {}, context, tool_refusal=refusal)
    thin_routes.serve(handler, _Never(), "/v1/outcome", {}, context, tool_refusal=refusal)
    thin_routes.serve(handler, _Never(), "/v1/unbound", {}, context, tool_refusal=refusal)
    assert seen == [(("recall",), "/v1/recall", context),
                    (("record_outcome",), "/v1/outcome", context)]
    assert [status for status, _ in handler.sent] == [403, 403, 403]
    assert "not bound to a gated tool" in handler.sent[2][1]["message"]


@pytest.mark.parametrize("mode", ["api-key", "local-open"])
def test_operator_still_reaches_thin_routes(memory, monkeypatch, mode):
    if mode == "local-open":
        _local_open(monkeypatch)
        headers = ()
    else:
        _accounts(monkeypatch, mode)
        headers = ("Authorization: Bearer " + API_KEY,)
    with _http_server(monkeypatch) as port:
        recall_status, recall_body, _ = _raw_post(
            port, "/v1/recall", {"task": "mine"}, headers=headers)
        outcome_status, outcome_body, _ = _raw_post(
            port, "/v1/outcome", {"interaction_id": "i1", "signal": "good"}, headers=headers)
    assert recall_status == 200, recall_body
    assert outcome_status == 200, outcome_body
    assert memory.recalls == [("mine", 2, None)]
    assert memory.outcomes == [("i1", "good")]


# --- /v1/recall is decided as ``recall``, a read-only memory lookup ----------

@pytest.fixture
def rules(monkeypatch):
    """Operator rules by tool name (none on disk) and a clean receipt counter."""
    table = {}
    monkeypatch.setattr(pm, "_rule_lookup", lambda tool: table.get(tool))
    pm.reset_unattended_for_tests()
    yield table
    pm.reset_unattended_for_tests()


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(pm, "current_mode", lambda: mode)


def test_recall_is_declared_read_only_route_work_bound_to_its_route():
    """``recall`` is graded like ``memory_search`` and pinned to its route."""
    import command_catalog
    from sonder_runtime.interfaces.http import thin_routes

    bound = set(thin_routes.ROUTE_TOOLS.values())
    assert pm.HTTP_ROUTE_WORK == {"recall": "safe"}
    for name in pm.HTTP_ROUTE_WORK:
        assert name in bound, "%s is declared route work but no thin route is bound to it" % name
        assert command_catalog.by_name("/" + name) is None, (
            "%s is a registered tool now; let the catalog grade it" % name)
    assert pm.risk_of("recall") == pm.risk_of("memory_search") == "safe"
    assert all(pm.risk_of(name) != pm.UNCLASSIFIED for name in bound)
    assert "recall" in ts._ACCOUNT_GLOBAL_MEMORY_TOOLS


@pytest.mark.parametrize("mode", pm.MODES)
def test_recall_is_allowed_unattended_in_every_mode(monkeypatch, mode):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    decision = pm.decide_for_caller("recall", interactive=False, gate_control_exempt=True,
                                    surface="http", mode=mode, record=False)
    assert decision.action == "allow", decision.reason


def test_recall_route_is_decided_as_recall_not_apply_learned(memory, rules, monkeypatch):
    _local_open(monkeypatch)
    _set_mode(monkeypatch, pm.MANUAL)
    rules["apply_learned"] = {"action": "deny", "pattern": "apply_learned"}
    with _http_server(monkeypatch) as port:
        allowed, allowed_body, _ = _raw_post(port, "/v1/recall", {"task": "mine"})
        assert "apply_learned" not in str(pm._UNATTENDED)
        rules.clear()
        rules["recall"] = {"action": "deny", "pattern": "recall"}
        refused, refused_body, _ = _raw_post(port, "/v1/recall", {"task": "theirs"})
    assert allowed == 200, allowed_body
    assert refused == 403, refused_body
    assert "rule denies this tool (pattern 'recall')" in refused_body["message"]
    assert pm._UNATTENDED["last_refusal"] == "recall via http"
    assert memory.recalls == [("mine", 2, None)]


@pytest.mark.parametrize("mode", ["api-key", "local-open"])
def test_owner_recall_is_allowed_in_plan_mode(memory, rules, monkeypatch, mode):
    if mode == "local-open":
        _local_open(monkeypatch)
        headers = ()
    else:
        _accounts(monkeypatch, mode)
        headers = ("Authorization: Bearer " + API_KEY,)
    _set_mode(monkeypatch, pm.PLAN)
    with _http_server(monkeypatch) as port:
        recall_status, recall_body, _ = _raw_post(
            port, "/v1/recall", {"task": "mine"}, headers=headers)
        outcome_status, outcome_body, _ = _raw_post(
            port, "/v1/outcome", {"interaction_id": "i1", "signal": "good"}, headers=headers)
    assert recall_status == 200, recall_body
    assert memory.recalls == [("mine", 2, None)]
    # Plan still refuses the write the outcome route performs.
    assert outcome_status == 403, outcome_body
    assert "(mode: plan)" in outcome_body["message"]
    assert memory.outcomes == []


@pytest.mark.parametrize("token", ["user-token", "dev-token", "admin-token"])
def test_hosted_account_recall_is_refused_in_plan_mode(memory, rules, monkeypatch, token):
    _accounts(monkeypatch, "account")
    _set_mode(monkeypatch, pm.PLAN)
    with _http_server(monkeypatch) as port:
        status, body, _ = _raw_post(port, "/v1/recall", {"task": "secret"},
                                    headers=("Authorization: Bearer " + token,))
    assert status == 403, body
    assert "global memory tool is local-operator only" in body["message"]
    assert memory.recalls == []
