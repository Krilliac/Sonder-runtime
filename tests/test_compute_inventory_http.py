from types import SimpleNamespace
import io
import pytest

from tests.test_compute_snapshot_http import http_server, _get
from tests.test_compute_index import inventory, NOW
from tests.test_compute_job_http import _post
from sonder_runtime.interfaces.http import serve


def test_refresh_uses_shared_failed_auth_admission_before_body_or_app(http_server, monkeypatch):
    failures = []
    auth_calls = []
    lifecycle = serve.sonder_lifecycle.get()
    monkeypatch.setattr(lifecycle, "auth_attempt_allowed", lambda client: not failures)
    monkeypatch.setattr(lifecycle, "record_auth_failure", lambda client, reason: failures.append(reason))
    def denied(self):
        auth_calls.append(True)
        return {"authorized": False}
    monkeypatch.setattr(serve.Handler, "_request_auth_context", denied)
    monkeypatch.setattr(serve.Handler, "_read_json", lambda *a, **k: pytest.fail("denied request body read"))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: pytest.fail("denied request constructed app"))
    assert _post(http_server, "/v1/compute/nodes/refresh", {})[0] == 401
    assert _post(http_server, "/v1/compute/nodes/refresh", {})[0] == 429
    assert auth_calls == [True]
    assert len(failures) == 1


@pytest.mark.parametrize("authorized,role,status", [(False, None, 401), (True, "user", 403)])
def test_inventory_is_admin_only_before_application_read(http_server, monkeypatch, authorized, role, status):
    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: {
        "authorized": authorized, "mode": "account", "account": {"role": role}, "api_key": False})
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: pytest.fail("denied inventory read"))
    assert _get(http_server, "/v1/compute/nodes")[0] == status


def test_inventory_http_pages_without_probe_and_rejects_unknown_query(http_server, monkeypatch):
    registry, _ = inventory(64)
    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: {
        "authorized": True, "mode": "account", "account": {"role": "admin"}, "api_key": False})
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: SimpleNamespace(
        compute_inventory_page=lambda **kwargs: registry.inventory_page(now=NOW, **kwargs)))
    status, page = _get(http_server, "/v1/compute/nodes?limit=17")
    assert status == 200 and len(page["nodes"]) == 17 and page["has_more"]
    assert _get(http_server, "/v1/compute/nodes?limit=17&limit=18")[0] == 400
    assert _get(http_server, "/v1/compute/nodes?origin=https://injected.example")[0] == 400
    assert _get(http_server, "/v1/compute/nodes?cursor=bad")[0] == 400


def test_inventory_facade_bounds_output():
    from sonder_runtime.interfaces.http.facades.compute_inventory import dispatch_compute_inventory
    status, body = dispatch_compute_inventory(lambda **kwargs: {"nodes": ["x" * (256 * 1024)]}, {})
    assert status == 413 and body["error"]["code"] == "INVENTORY_PAGE_TOO_LARGE"


def test_native_cleanup_only_for_explicitly_owned_application():
    from tests.test_native_mcp import _app
    from sonder_runtime.bootstrap.native_mcp import run_native_mcp
    app = _app()
    calls = []
    app.close_compute = lambda: calls.append("closed")
    for _ in range(2):
        run_native_mcp(app, input_stream=io.StringIO(""), output_stream=io.StringIO())
    assert calls == []
    run_native_mcp(app, input_stream=io.StringIO(""), output_stream=io.StringIO(), close_compute_on_exit=True)
    assert calls == ["closed"]


@pytest.mark.parametrize("authorized,role,status", [(False, None, 401), (True, "user", 403)])
def test_refresh_denial_never_constructs_service_or_probes(http_server, monkeypatch, authorized, role, status):
    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: {
        "authorized": authorized, "mode": "account", "account": {"role": role}, "api_key": False})
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: pytest.fail("denied refresh constructed application"))
    assert _post(http_server, "/v1/compute/nodes/refresh", {})[0] == status


def test_refresh_pages_are_partial_and_global_optin_cannot_be_overridden(http_server, monkeypatch):
    from datetime import timedelta
    from sonder_runtime.application.compute_fabric.coordinator import ComputeRefreshCoordinator
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig
    registry, snapshots = inventory(64, homogeneous=True)
    calls = []
    class Source:
        def snapshot(self, node, *, now):
            calls.append(node.node_id)
            return snapshots[int(node.node_id[-3:])]
    coordinator = ComputeRefreshCoordinator(registry, Source(), now=lambda: NOW, refresh_after=timedelta(seconds=10))
    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: {
        "authorized": True, "mode": "account", "account": {"role": "admin"}, "api_key": False})
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: SimpleNamespace(compute_refresh_page=coordinator.refresh_page))
    try:
        status, page = _post(http_server, "/v1/compute/nodes/refresh", {"limit": 17})
        assert status == 200 and page["partial_inventory"] and page["has_more"]
        assert page["probed_count"] == 16 and len(calls) == 16
        assert _post(http_server, "/v1/compute/nodes/refresh", {"origin": "https://injected.example"})[0] == 400
        assert len(calls) == 16
        disabled = build_application(config=SonderConfig())
        monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: disabled)
        status, error = _post(http_server, "/v1/compute/nodes/refresh", {})
        assert status == 403 and error["error"]["code"] == "REMOTE_COMPUTE_DISABLED"
    finally:
        coordinator.close()


def test_refresh_rejects_large_body_before_read_or_discard(monkeypatch):
    from email.message import Message
    class Unreadable(io.BytesIO):
        def read(self, *args):
            raise AssertionError("oversized refresh body read")
    handler = object.__new__(serve.Handler)
    handler.path = "/v1/compute/nodes/refresh"
    handler.client_address = ("127.0.0.1", 0)
    handler.command = "POST"
    handler.headers = Message()
    handler.headers["Content-Length"] = "32769"
    handler.headers["Content-Type"] = "application/json"
    handler.rfile = Unreadable()
    handler._request_body_consumed = False
    handler._reject_disallowed_origin = lambda: False
    handler._request_auth_context = lambda: {"authorized": True, "mode": "account", "account": {"role": "admin"}}
    replies = []
    handler._send_json_payload = lambda body, *, status, **kwargs: replies.append(status)
    assert handler._handle_compute_inventory_refresh()
    assert replies == [413]
    assert handler._settle_unread_request_body()


def test_inventory_request_admission_is_nonblocking_and_released_on_delivery_failure(monkeypatch):
    from threading import BoundedSemaphore
    from sonder_runtime.interfaces.http.facades import compute_inventory
    gate = BoundedSemaphore(1)
    monkeypatch.setattr(compute_inventory, "_INVENTORY_REQUEST_SLOTS", gate)
    handler = object.__new__(serve.Handler)
    replies = []
    handler._send_json_payload = lambda body, *, status: replies.append(status)
    gate.acquire()
    handler._with_compute_inventory_admission(lambda: pytest.fail("saturated request did work"))
    assert replies == [429]
    gate.release()
    with pytest.raises(OSError):
        handler._with_compute_inventory_admission(lambda: (_ for _ in ()).throw(OSError("disconnected")))
    assert gate.acquire(blocking=False)
    gate.release()
