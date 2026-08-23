from __future__ import annotations

from sonder_runtime.interfaces.http.facades.observability import dispatch_trace_route


class _Projection:
    def to_dict(self):
        return {"schema": "sonder.trace-span.v1", "spans": [], "redacted": True}


class _Events:
    def __init__(self):
        self.limits = []
        self.calls = []

    def trace_projection(self, *, limit, correlation_id="", category="", severity=""):
        self.limits.append(limit)
        self.calls.append((limit, correlation_id, category, severity))
        return _Projection()


def test_trace_http_facade_projects_redacted_events_with_bounded_limit():
    events = _Events()
    result = dispatch_trace_route(events, "GET", "/v1/observability/trace", {"limit": ["12"]})
    assert result.status_code == 200
    assert result.body["object"] == "trace_projection"
    assert result.body["redacted"] is True
    assert events.limits == [12]
    assert events.calls == [(12, "", "", "")]


def test_trace_http_facade_rejects_bad_limits_and_methods():
    events = _Events()
    assert dispatch_trace_route(events, "POST", "/v1/observability/trace").status_code == 405
    assert dispatch_trace_route(events, "GET", "/v1/observability/trace", {"limit": ["0"]}).status_code == 400
    assert dispatch_trace_route(events, "GET", "/v1/observability/trace", {"limit": ["x"]}).status_code == 400
    assert dispatch_trace_route(events, "GET", "/v1/observability/trace", {"limit": ["1", "2"]}).status_code == 400


def test_trace_http_facade_fails_closed_when_projection_is_unavailable():
    result = dispatch_trace_route(object(), "GET", "/v1/observability/trace")
    assert result.status_code == 503
    assert result.body["error"]["type"] == "server_error"


def test_trace_http_facade_passes_through_correlation_run_and_worker_filters():
    events = _Events()
    result = dispatch_trace_route(
        events, "GET", "/v1/observability/trace",
        {"limit": ["5"], "correlation_id": ["req-42"], "category": ["inference"], "severity": ["error"]},
    )
    assert result.status_code == 200
    assert events.calls == [(5, "req-42", "inference", "error")]


def test_trace_http_facade_rejects_repeated_or_oversized_filters():
    events = _Events()
    assert dispatch_trace_route(
        events, "GET", "/v1/observability/trace",
        {"correlation_id": ["a", "b"]},
    ).status_code == 400
    assert dispatch_trace_route(
        events, "GET", "/v1/observability/trace",
        {"category": ["x" * 97]},
    ).status_code == 400
    assert events.calls == []
