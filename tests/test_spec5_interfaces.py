"""SPEC-5 WP10 — Thin interfaces tests.

Covers:
- HTTP handlers: parse → context → delegate → map errors → JSON
- MCP handlers: parse → context → delegate → map errors → result dict
- CLI commands: parse → context → delegate → format output → exit code
- REPL handler: line → context → delegate → print
- No interface imports concrete adapters
- OperationContext flows through every handler
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest

from sonder_runtime.domain.common.errors import (
    Forbidden,
    InvalidInput,
    NotFound,
    SonderError,
)


# ---------- stub services ----------

class StubRecallService:
    def __init__(self, results=None, error=None):
        self._results = results or []
        self._error = error
        self.last_task = None
        self.last_k = None

    def recall(self, task, *, k=2, project=None):
        self.last_task = task
        self.last_k = k
        if self._error:
            raise self._error
        return self._results


class StubOutcomeService:
    def __init__(self, score=0.5, error=None):
        self._score = score
        self._error = error
        self.last_id = None
        self.last_signal = None

    def record(self, interaction_id, signal):
        self.last_id = interaction_id
        self.last_signal = signal
        if self._error:
            raise self._error
        return self._score


# ---------- HTTP tests ----------

@dataclass
class FakeRequest:
    method: str = "POST"
    path: str = "/"
    body: bytes = b"{}"
    headers: dict = None

    def __post_init__(self):
        if self.headers is None:
            self.headers = {}


class TestHTTPHandlers:
    def test_health(self):
        from sonder_runtime.interfaces.http.handlers import HealthHandler
        h = HealthHandler()
        resp = h.handle(FakeRequest())
        assert resp.status == 200
        assert resp.body["status"] == "ok"

    def test_recall_success(self):
        from sonder_runtime.interfaces.http.handlers import RecallHandler
        svc = StubRecallService(results=["fact1", "fact2"])
        h = RecallHandler(svc)
        req = FakeRequest(body=json.dumps({"task": "test task", "k": 3}).encode())
        resp = h.handle(req)
        assert resp.status == 200
        assert resp.body["results"] == ["fact1", "fact2"]
        assert svc.last_task == "test task"
        assert svc.last_k == 3

    def test_recall_bad_json(self):
        from sonder_runtime.interfaces.http.handlers import RecallHandler
        svc = StubRecallService()
        h = RecallHandler(svc)
        req = FakeRequest(body=b"not json")
        resp = h.handle(req)
        assert resp.status == 400

    def test_recall_service_error(self):
        from sonder_runtime.interfaces.http.handlers import RecallHandler
        svc = StubRecallService(error=NotFound("no such project"))
        h = RecallHandler(svc)
        req = FakeRequest(body=b'{"task":"x"}')
        resp = h.handle(req)
        assert resp.status == 404
        assert resp.body["error"] == "NOT_FOUND"

    def test_outcome_success(self):
        from sonder_runtime.interfaces.http.handlers import OutcomeHandler
        svc = StubOutcomeService(score=0.7)
        h = OutcomeHandler(svc)
        req = FakeRequest(body=json.dumps({"interaction_id": "abc", "signal": "positive"}).encode())
        resp = h.handle(req)
        assert resp.status == 200
        assert resp.body["score"] == 0.7

    def test_outcome_missing_fields(self):
        from sonder_runtime.interfaces.http.handlers import OutcomeHandler
        svc = StubOutcomeService()
        h = OutcomeHandler(svc)
        req = FakeRequest(body=b'{"interaction_id": "abc"}')
        resp = h.handle(req)
        assert resp.status == 400

    def test_context_from_request_has_source(self):
        from sonder_runtime.interfaces.http.handlers import context_from_request
        req = FakeRequest(headers={"X-Correlation-Id": "test-123"})
        ctx = context_from_request(req)
        assert ctx.source == "http"
        assert ctx.correlation_id == "test-123"

    def test_error_status_mapping(self):
        from sonder_runtime.interfaces.http.handlers import error_response
        resp = error_response(Forbidden("nope"))
        assert resp.status == 403
        resp = error_response(InvalidInput("bad"))
        assert resp.status == 400

    def test_response_serialize(self):
        from sonder_runtime.interfaces.http.handlers import Response
        r = Response(200, {"key": "value"})
        raw = r.serialize()
        parsed = json.loads(raw)
        assert parsed["key"] == "value"


# ---------- MCP tests ----------

class TestMCPHandlers:
    def test_recall(self):
        from sonder_runtime.interfaces.mcp.handlers import McpRecallHandler
        svc = StubRecallService(results=["r1"])
        h = McpRecallHandler(svc)
        result = h.handle({"task": "test", "k": 1})
        assert result["results"] == ["r1"]

    def test_recall_error(self):
        from sonder_runtime.interfaces.mcp.handlers import McpRecallHandler
        svc = StubRecallService(error=Forbidden("not allowed"))
        h = McpRecallHandler(svc)
        result = h.handle({"task": "test"})
        assert result["isError"] is True
        assert result["error"] == "FORBIDDEN"

    def test_outcome(self):
        from sonder_runtime.interfaces.mcp.handlers import McpOutcomeHandler
        svc = StubOutcomeService(score=0.9)
        h = McpOutcomeHandler(svc)
        result = h.handle({"interaction_id": "i1", "signal": "positive"})
        assert result["score"] == 0.9

    def test_outcome_missing_fields(self):
        from sonder_runtime.interfaces.mcp.handlers import McpOutcomeHandler
        svc = StubOutcomeService()
        h = McpOutcomeHandler(svc)
        result = h.handle({"interaction_id": ""})
        assert result["isError"] is True

    def test_context_source_is_mcp(self):
        from sonder_runtime.interfaces.mcp.handlers import context_for_mcp_call
        ctx = context_for_mcp_call()
        assert ctx.source == "mcp"


# ---------- CLI tests ----------

class TestCLICommands:
    def test_status_no_service(self):
        from sonder_runtime.interfaces.cli.commands import StatusCommand
        out = io.StringIO()
        code = StatusCommand().run(out=out)
        assert code == 0
        assert "ok" in out.getvalue()

    def test_recall(self):
        from sonder_runtime.interfaces.cli.commands import RecallCommand
        svc = StubRecallService(results=["fact1", "fact2"])
        out = io.StringIO()
        code = RecallCommand(svc).run("test task", out=out)
        assert code == 0
        assert "fact1" in out.getvalue()
        assert "fact2" in out.getvalue()

    def test_recall_error(self):
        from sonder_runtime.interfaces.cli.commands import RecallCommand
        svc = StubRecallService(error=NotFound("nope"))
        out = io.StringIO()
        code = RecallCommand(svc).run("test", out=out)
        assert code == 1
        assert "NOT_FOUND" in out.getvalue()

    def test_outcome(self):
        from sonder_runtime.interfaces.cli.commands import OutcomeCommand
        svc = StubOutcomeService(score=0.8)
        out = io.StringIO()
        code = OutcomeCommand(svc).run("id1", "positive", out=out)
        assert code == 0
        assert "0.8" in out.getvalue()


# ---------- REPL tests ----------

class TestReplHandler:
    def test_recall(self):
        from sonder_runtime.interfaces.repl.handler import ReplHandler
        svc = StubRecallService(results=["r1", "r2"])
        out = io.StringIO()
        h = ReplHandler(recall_service=svc)
        result = h.dispatch("recall test task", out=out)
        assert "r1" in result
        assert "r2" in result

    def test_outcome(self):
        from sonder_runtime.interfaces.repl.handler import ReplHandler
        svc = StubOutcomeService(score=0.6)
        out = io.StringIO()
        h = ReplHandler(outcome_service=svc)
        result = h.dispatch("outcome id1 positive", out=out)
        assert "0.6" in result

    def test_outcome_usage(self):
        from sonder_runtime.interfaces.repl.handler import ReplHandler
        svc = StubOutcomeService()
        out = io.StringIO()
        h = ReplHandler(outcome_service=svc)
        result = h.dispatch("outcome", out=out)
        assert "usage" in result

    def test_unknown_command(self):
        from sonder_runtime.interfaces.repl.handler import ReplHandler
        out = io.StringIO()
        h = ReplHandler()
        result = h.dispatch("foobar", out=out)
        assert "unknown" in result

    def test_empty_line(self):
        from sonder_runtime.interfaces.repl.handler import ReplHandler
        h = ReplHandler()
        result = h.dispatch("", out=io.StringIO())
        assert result == ""

    def test_error_handling(self):
        from sonder_runtime.interfaces.repl.handler import ReplHandler
        svc = StubRecallService(error=Forbidden("nope"))
        out = io.StringIO()
        h = ReplHandler(recall_service=svc)
        result = h.dispatch("recall test", out=out)
        assert "FORBIDDEN" in result


# ---------- Architecture: no concrete adapter imports ----------

class TestInterfaceIsolation:
    """Verify interface modules don't import concrete adapters."""

    BANNED_IMPORTS = [
        "adapters.ollama",
        "adapters.openai_compat",
        "adapters.persistence",
        "adapters.memory_store",
        "sqlite3",
    ]

    @pytest.mark.parametrize("module_path", [
        "sonder_runtime.interfaces.http.handlers",
        "sonder_runtime.interfaces.mcp.handlers",
        "sonder_runtime.interfaces.cli.commands",
        "sonder_runtime.interfaces.repl.handler",
    ])
    def test_no_concrete_adapter_imports(self, module_path):
        import importlib
        import inspect
        mod = importlib.import_module(module_path)
        source = inspect.getsource(mod)
        for banned in self.BANNED_IMPORTS:
            assert banned not in source, (
                f"{module_path} imports {banned} — interfaces must not "
                "import concrete adapters"
            )
