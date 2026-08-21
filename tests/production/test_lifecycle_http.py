"""SPEC-2 WP3/WP4: lifecycle endpoints, admission, drain over real HTTP."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import sonder_runtime.adapters.web.lifecycle as sonder_lifecycle
import sonder_runtime.interfaces.http.serve as sonder_serve
from sonder_service_state import DependencyState

pytestmark = pytest.mark.integration

@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    sonder_lifecycle.reset_for_tests()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), sonder_serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()
    sonder_lifecycle.reset_for_tests()


def _get(base, path, headers=None):
    request = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def _post(base, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else b""
    all_headers = {"Content-Type": "application/json"}
    all_headers.update(headers or {})
    request = urllib.request.Request(
        base + path, data=data, headers=all_headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_live_is_unauthenticated_and_minimal(http_server):
    status, body, _ = _get(http_server, "/live")
    assert status == 200
    assert json.loads(body) == {"status": "alive"}


def test_startup_reconciles_before_publishing_ready(monkeypatch):
    events = []
    lifecycle = sonder_lifecycle.RuntimeLifecycle(
        startup_reconciler=lambda: events.append(
            lifecycle.tracker.snapshot().process.value
        ) or 3,
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.persistence.migrations.migrate_all",
        lambda: events.append("migrated") or {},
    )

    lifecycle.startup()

    assert events == ["migrated", "migrating"]
    assert lifecycle._startup_reconciled == 3
    assert lifecycle.tracker.snapshot().process.value == "ready"


def test_startup_reconciliation_failure_does_not_publish_ready(monkeypatch):
    lifecycle = sonder_lifecycle.RuntimeLifecycle(
        startup_reconciler=lambda: (_ for _ in ()).throw(RuntimeError("reconcile failed")),
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.persistence.migrations.migrate_all",
        lambda: {},
    )

    with pytest.raises(RuntimeError, match="reconcile failed"):
        lifecycle.startup()
    assert lifecycle.tracker.snapshot().process.value == "recovery_required"


def test_version_reports_build(http_server):
    status, body, _ = _get(http_server, "/version")
    assert status == 200
    payload = json.loads(body)
    assert payload["version"]
    assert "commit_sha" in payload


def test_admin_job_list_and_poll_surface_is_bounded(http_server, tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.application.ports.jobs import JobIdentity
    from sonder_runtime.bootstrap import app as bootstrap_app

    database = tmp_path / "jobs.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    registry = SQLiteDurableJobRegistry(database)
    registry.create(JobIdentity("job-http", "shell", "op-http", "idem-http"))

    status, body, _ = _get(http_server, "/v1/jobs?limit=1")
    assert status == 200
    payload = json.loads(body)
    assert payload["object"] == "list"
    assert payload["data"][0]["job_id"] == "job-http"

    status, body, _ = _get(http_server, "/v1/jobs/job-http")
    assert status == 200
    assert json.loads(body)["status"] == "pending"

    status, body, _ = _get(http_server, "/v1/jobs?limit=101")
    assert status == 400
    assert "limit must be an integer" in json.loads(body)["error"]["message"]

    status, _, _ = _get(http_server, "/v1/jobs/does-not-exist")
    assert status == 404

    status, body = _post(
        http_server,
        "/v1/jobs/job-http/cancel",
        {"reason": "operator requested cancellation"},
    )
    assert status == 200
    assert json.loads(body)["cancelled_count"] == 1

    status, body, _ = _get(http_server, "/v1/jobs/job-http")
    assert status == 200
    assert json.loads(body)["status"] == "cancelled"

    status, body = _post(http_server, "/v1/jobs/job-http/cancel", {})
    assert status == 400
    assert "reason must be a non-empty string" in json.loads(body)["error"]["message"]


def test_admin_job_cancel_surface_validates_reason_and_bounds_response(
    http_server, tmp_path, monkeypatch,
):
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.application.capabilities.jobs import JobRegistryService
    from sonder_runtime.application.ports.jobs import JobIdentity
    from sonder_runtime.bootstrap import app as bootstrap_app

    database = tmp_path / "jobs.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    registry = SQLiteDurableJobRegistry(database)
    registry.create(JobIdentity("job-cancel", "shell", "op-cancel", "idem-cancel"))
    monkeypatch.setattr(
        bootstrap_app, "default_app",
        lambda: SimpleNamespace(job_service=lambda: JobRegistryService(registry)),
    )

    status, body = _post(
        http_server, "/v1/jobs/job-cancel/cancel",
        {"reason": "operator requested cancellation"},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["object"] == "job_cancel"
    assert payload["job"]["status"] == "cancelled"
    assert payload["cancelled_count"] == 1

    status, body = _post(http_server, "/v1/jobs/job-cancel/cancel", {"reason": ""})
    assert status == 400
    assert "reason must be" in json.loads(body)["error"]["message"]

    status, body = _post(http_server, "/v1/jobs/missing/cancel", {"reason": "cleanup"})
    assert status == 404

    ordinary = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "ordinary", "role": "user"},
    }
    monkeypatch.setattr(
        sonder_serve.Handler, "_request_auth_context",
        lambda _self: ordinary,
    )
    status, body = _post(http_server, "/v1/jobs/job-cancel/cancel", {"reason": "again"})
    assert status == 403
    assert "administrator authorization is required" in json.loads(body)["error"]["message"]
    bootstrap_app.reset_for_tests()


def test_admin_job_stream_surface_serializes_events_and_bounds_cursor(
    http_server, tmp_path, monkeypatch,
):
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.application.execution.world_control import OutputStream
    from sonder_runtime.application.ports.jobs import JobIdentity
    from sonder_runtime.bootstrap import app as bootstrap_app

    database = tmp_path / "jobs-stream.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    registry = SQLiteDurableJobRegistry(database)
    registry.create(JobIdentity("job-stream", "shell", "op-stream", "idem-stream"))
    registry.append_output("job-stream", OutputStream.STDOUT, "hello")
    registry.append_output("job-stream", OutputStream.STDERR, "warning")

    status, body, _ = _get(
        http_server, "/v1/jobs/job-stream/stream?after=0&max_events=1&max_bytes=5"
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["object"] == "job_output"
    assert payload["job_id"] == "job-stream"
    assert payload["events"] == [{
        "sequence": 1,
        "stream": "stdout",
        "data": "hello",
        "spill": None,
    }]
    assert payload["next_watermark"] == 1
    assert payload["has_more"] is True

    status, body, _ = _get(http_server, "/v1/jobs/job-stream/stream?after=1")
    assert status == 200
    assert json.loads(body)["events"][0]["stream"] == "stderr"

    status, body, _ = _get(
        http_server, "/v1/jobs/job-stream/stream?max_events=257"
    )
    assert status == 400
    assert "max_events must be an integer" in json.loads(body)["error"]["message"]
    bootstrap_app.reset_for_tests()


def test_admin_job_result_surface_distinguishes_missing_nonterminal_and_terminal(
    http_server, tmp_path, monkeypatch,
):
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
    from sonder_runtime.bootstrap import app as bootstrap_app

    database = tmp_path / "jobs-result.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    registry = SQLiteDurableJobRegistry(database)
    registry.create(JobIdentity("job-result", "shell", "op-result", "idem-result"))

    status, _, _ = _get(http_server, "/v1/jobs/missing/result")
    assert status == 404
    status, body, _ = _get(http_server, "/v1/jobs/job-result/result")
    assert status == 409
    assert json.loads(body)["error"]["type"] == "conflict"

    registry.transition("job-result", JobStatus.SUCCEEDED, result={"answer": 42})
    status, body, _ = _get(http_server, "/v1/jobs/job-result/result")
    assert status == 200
    payload = json.loads(body)
    assert payload["object"] == "job_result"
    assert payload["result"] == {"answer": 42}
    assert payload["job"]["status"] == "succeeded"
    bootstrap_app.reset_for_tests()


def test_ready_reflects_ollama_outage_without_false_success(http_server):
    # Hermetic env points Ollama at a closed port: readiness must be 503
    # with the dependency named, while liveness stays 200.
    lifecycle = sonder_lifecycle.get()
    lifecycle.probe_ollama_once(timeout=0.5)
    status, body, _ = _get(http_server, "/ready")
    assert status == 503
    payload = json.loads(body)
    assert payload["ready"] is False
    assert "ollama" in payload["reason"]
    status, _, _ = _get(http_server, "/live")
    assert status == 200


def test_ready_succeeds_when_dependencies_are_healthy(http_server):
    lifecycle = sonder_lifecycle.get()
    lifecycle.adopt_legacy_start()
    lifecycle.tracker.set_dependency("ollama", DependencyState.READY)
    status, body, _ = _get(http_server, "/ready")
    assert status == 200
    assert json.loads(body)["ready"] is True


def test_health_reports_state_dependencies_schemas(http_server):
    status, body, _ = _get(http_server, "/health")
    assert status == 200
    payload = json.loads(body)
    assert payload["process_state"] in ("ready", "degraded")
    assert "ollama" in payload["dependencies"]
    assert "operations" in payload["schemas"]
    assert "build" in payload


def test_metrics_endpoint_renders(http_server):
    status, body, headers = _get(http_server, "/metrics")
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    # Either real exposition or the explicit disabled comment.
    assert b"sonder_" in body or b"metrics disabled" in body


def test_admin_drain_is_idempotent_and_blocks_new_chat(http_server):
    key = "drain-key-1"
    status, body = _post(
        http_server, "/v1/admin/drain", headers={"Idempotency-Key": key}
    )
    assert status == 202
    first = json.loads(body)
    assert first["draining"] is True
    status, body = _post(
        http_server, "/v1/admin/drain", headers={"Idempotency-Key": key}
    )
    assert status == 202
    assert json.loads(body) == first

    lifecycle = sonder_lifecycle.get()
    for _ in range(100):
        if lifecycle.coordinator.draining:
            break
        threading.Event().wait(0.02)
    assert lifecycle.coordinator.draining

    status, body = _post(
        http_server,
        "/v1/chat/completions",
        body={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 503
    envelope = json.loads(body)["error"]
    assert envelope["code"] == "DRAINING"
    assert envelope["retryable"] is True
    assert envelope["correlation_id"].startswith("req_")


def test_idempotent_serializes_concurrent_requests_for_same_key():
    lifecycle = sonder_lifecycle.RuntimeLifecycle()
    first_factory_entered = threading.Event()
    duplicate_factory_entered = threading.Event()
    release_factory = threading.Event()
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            first_factory_entered.set()
        else:
            duplicate_factory_entered.set()
        release_factory.wait(2)
        return {"call": len(calls)}

    results = []
    first = threading.Thread(
        target=lambda: results.append(lifecycle.idempotent("same", factory))
    )
    second = threading.Thread(
        target=lambda: results.append(lifecycle.idempotent("same", factory))
    )
    first.start()
    assert first_factory_entered.wait(1)
    second.start()
    try:
        assert not duplicate_factory_entered.wait(0.2)
    finally:
        release_factory.set()
        first.join(2)
        second.join(2)

    assert calls == [1]
    assert results == [{"call": 1}, {"call": 1}]


def test_idempotent_ttl_recomputes_after_process_cache_expiry(monkeypatch):
    lifecycle = sonder_lifecycle.RuntimeLifecycle()
    now = [100.0]
    monkeypatch.setattr(sonder_lifecycle.time, "monotonic", lambda: now[0])
    calls = []

    assert lifecycle.idempotent(
        "expiring", lambda: calls.append("first") or "first", cache_ttl_seconds=5,
    ) == "first"
    now[0] = 104.0
    assert lifecycle.idempotent(
        "expiring", lambda: calls.append("wrong") or "wrong", cache_ttl_seconds=5,
    ) == "first"
    now[0] = 106.0
    assert lifecycle.idempotent(
        "expiring", lambda: calls.append("second") or "second", cache_ttl_seconds=5,
    ) == "second"
    assert calls == ["first", "second"]


def test_auth_failure_limiter_and_events(http_server, monkeypatch):
    monkeypatch.setattr(sonder_serve, "API_KEY", "k" * 32)
    saw_429 = False
    for _ in range(14):
        status, body, _ = _get(
            http_server,
            "/v1/models",
            headers={"Authorization": "Bearer wrong-key"},
        )
        if status == 429:
            saw_429 = True
            envelope = json.loads(body)["error"]
            assert envelope["code"] == "AUTH_RATE_LIMITED"
            break
        assert status == 401
    assert saw_429

    from sonder_runtime.adapters.persistence.operations_store import OperationsStore

    events = OperationsStore().recent_events(limit=50)
    assert any(e.event_code == "AUTH_FAILED" for e in events)
    # The wrong key never lands in the audit trail.
    assert "wrong-key" not in json.dumps(
        [e.detail for e in events] + [e.summary for e in events]
    )


def test_correlation_id_header_present_on_errors(http_server, monkeypatch):
    monkeypatch.setattr(sonder_serve, "API_KEY", "k" * 32)
    status, body = _post(
        http_server,
        "/v1/chat/completions",
        body={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer nope"},
    )
    assert status in (401, 429)
    payload = json.loads(body)["error"]
    assert payload.get("correlation_id", "").startswith("req_") or status == 429
