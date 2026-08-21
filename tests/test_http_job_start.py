"""Focused HTTP coverage for the bounded durable-job start command."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import sonder_runtime.adapters.web.lifecycle as sonder_lifecycle
import sonder_runtime.interfaces.http.serve as sonder_serve


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


def _post(base, path, body, headers=None):
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _admin_context():
    return {
        "mode": "account",
        "authorized": True,
        "api_key": False,
        "account": {"username": "operator", "role": "admin"},
    }


def test_admin_job_start_persists_bounded_identity(
    http_server, tmp_path, monkeypatch,
):
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.application.capabilities.jobs import JobRegistryService
    from sonder_runtime.bootstrap import app as bootstrap_app

    database = tmp_path / "jobs.db"
    registry = SQLiteDurableJobRegistry(database)
    monkeypatch.setattr(sonder_serve.Handler, "_request_auth_context", lambda _self: _admin_context())
    monkeypatch.setattr(
        bootstrap_app,
        "default_app",
        lambda: SimpleNamespace(job_service=lambda: JobRegistryService(registry)),
    )

    status, payload = _post(http_server, "/v1/jobs/start", {
        "job_id": "job-start",
        "kind": "shell",
        "operation_id": "op-start",
        "idempotency_key": "idem-start",
        "parent_session_id": "session-1",
    })

    assert status == 202
    assert payload["object"] == "job_start"
    assert payload["job"]["job_id"] == "job-start"
    assert payload["job"]["status"] == "pending"
    assert registry.get("job-start").identity.parent_session_id == "session-1"


@pytest.mark.parametrize("body", [
    {},
    {"job_id": "x", "kind": "shell", "operation_id": "op", "idempotency_key": "idem", "extra": True},
    {"job_id": "x", "kind": "shell", "operation_id": "op", "idempotency_key": "idem", "parent_job_id": "a/b"},
    {"job_id": "x" * 129, "kind": "shell", "operation_id": "op", "idempotency_key": "idem"},
])
def test_admin_job_start_rejects_unbounded_or_unknown_input(http_server, body, monkeypatch):
    monkeypatch.setattr(sonder_serve.Handler, "_request_auth_context", lambda _self: _admin_context())
    status, payload = _post(http_server, "/v1/jobs/start", body)
    assert status == 400
    assert payload["error"]["type"] == "invalid_request"


def test_job_start_requires_admin_and_reports_duplicate_identity(
    http_server, tmp_path, monkeypatch,
):
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.application.capabilities.jobs import JobRegistryService
    from sonder_runtime.application.ports.jobs import JobIdentity
    from sonder_runtime.bootstrap import app as bootstrap_app

    database = tmp_path / "jobs.db"
    registry = SQLiteDurableJobRegistry(database)
    registry.create(JobIdentity("job-existing", "shell", "op", "idem"))
    monkeypatch.setattr(
        bootstrap_app,
        "default_app",
        lambda: SimpleNamespace(job_service=lambda: JobRegistryService(registry)),
    )
    monkeypatch.setattr(
        sonder_serve.Handler,
        "_request_auth_context",
        lambda _self: {"mode": "account", "authorized": True, "api_key": False,
                        "account": {"username": "user", "role": "user"}},
    )
    body = {"job_id": "job-new", "kind": "shell", "operation_id": "op", "idempotency_key": "idem-new"}
    status, payload = _post(http_server, "/v1/jobs/start", body)
    assert status == 403
    assert payload["error"]["code"] == "FORBIDDEN"

    monkeypatch.setattr(sonder_serve.Handler, "_request_auth_context", lambda _self: _admin_context())
    duplicate = dict(body, job_id="job-existing", idempotency_key="idem-existing")
    status, payload = _post(http_server, "/v1/jobs/start", duplicate)
    assert status == 409
    assert payload["error"]["type"] == "conflict"
