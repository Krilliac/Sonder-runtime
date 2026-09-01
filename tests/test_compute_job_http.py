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
from sonder_runtime.application.compute_fabric.jobs import RemoteJobEnvelope, RemoteJobReceipt
from sonder_runtime.application.compute_fabric.wire import job_envelope_to_wire
from sonder_runtime.domain.compute_fabric import WorkloadKind


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
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    sonder_lifecycle.reset_for_tests()


def _envelope() -> RemoteJobEnvelope:
    return RemoteJobEnvelope.create(
        controller_job_id="controller-job",
        idempotency_key="idem-1",
        workload=WorkloadKind.BUILD,
        catalog_entry_id="cmake-build",
        workspace_mapping="sonder",
        deadline_seconds=60,
        idempotent=True,
    )


def _post(base: str, path: str, payload: dict):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _get(base: str, path: str):
    request = urllib.request.Request(
        base + path,
        headers={"Authorization": "Bearer test"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


class _Worker:
    def __init__(self):
        envelope = _envelope()
        self.receipt = RemoteJobReceipt(
            worker_id="local",
            remote_job_id="remote-1",
            controller_job_id=envelope.controller_job_id,
            idempotency_key=envelope.idempotency_key,
            request_sha256=envelope.request_sha256,
            state="running",
        )

    def submit(self, _value):
        return self.receipt

    def status(self, remote_job_id):
        if remote_job_id != self.receipt.remote_job_id:
            raise KeyError(remote_job_id)
        return self.receipt

    def by_idempotency(self, key):
        return self.receipt if key == self.receipt.idempotency_key else None

    def cancel(self, remote_job_id, *, reason):
        assert reason == "operator requested"
        return self.status(remote_job_id)


def _worker():
    return _Worker()


def test_job_submit_requires_admin_and_rejects_unknown_fields(http_server, monkeypatch) -> None:
    monkeypatch.setattr(
        sonder_serve.Handler,
        "_request_auth_context",
        lambda _self: {"authorized": True, "mode": "api-key", "account": None},
    )
    monkeypatch.setattr(sonder_serve, "_admin_authorized", lambda _context: False)
    status, _ = _post(http_server, "/v1/compute/jobs", job_envelope_to_wire(_envelope()))
    assert status == 403

    monkeypatch.setattr(sonder_serve, "_admin_authorized", lambda _context: True)
    monkeypatch.setattr(
        "sonder_runtime.bootstrap.app.default_app",
        lambda: SimpleNamespace(compute_job_worker=lambda: _worker()),
    )
    payload = {**job_envelope_to_wire(_envelope()), "program": "cmd"}
    status, body = _post(http_server, "/v1/compute/jobs", payload)
    assert status == 400
    assert body["error"]["type"] == "invalid_request"


def test_job_submit_returns_bounded_receipt(http_server, monkeypatch) -> None:
    monkeypatch.setattr(
        sonder_serve.Handler,
        "_request_auth_context",
        lambda _self: {"authorized": True, "mode": "api-key", "account": None},
    )
    monkeypatch.setattr(sonder_serve, "_admin_authorized", lambda _context: True)
    monkeypatch.setattr(
        "sonder_runtime.bootstrap.app.default_app",
        lambda: SimpleNamespace(compute_job_worker=lambda: _worker()),
    )
    status, body = _post(http_server, "/v1/compute/jobs", job_envelope_to_wire(_envelope()))
    assert status == 202
    assert body["object"] == "compute_job"
    assert body["job"]["remote_job_id"] == "remote-1"


def test_job_status_idempotency_lookup_and_cancel_use_exact_admin_routes(
    http_server, monkeypatch
) -> None:
    monkeypatch.setattr(
        sonder_serve.Handler,
        "_request_auth_context",
        lambda _self: {"authorized": True, "mode": "api-key", "account": None},
    )
    monkeypatch.setattr(sonder_serve, "_admin_authorized", lambda _context: True)
    worker = _worker()
    monkeypatch.setattr(
        "sonder_runtime.bootstrap.app.default_app",
        lambda: SimpleNamespace(compute_job_worker=lambda: worker),
    )

    status, body = _get(http_server, "/v1/compute/jobs/remote-1")
    assert status == 200
    assert body["job"]["remote_job_id"] == "remote-1"

    status, body = _get(http_server, "/v1/compute/jobs/by-idempotency/idem-1")
    assert status == 200
    assert body["job"]["idempotency_key"] == "idem-1"

    status, body = _post(
        http_server,
        "/v1/compute/jobs/remote-1/cancel",
        {"reason": "operator requested"},
    )
    assert status == 200
    assert body["job"]["remote_job_id"] == "remote-1"

    status, _body = _get(http_server, "/v1/compute/jobs/remote-1/extra")
    assert status == 404
