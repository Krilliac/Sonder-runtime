"""GET /v1/tools/inventory and POST /v1/tools/inventory/refresh (admin only)."""
from types import SimpleNamespace
import json
import urllib.error
import urllib.request

import pytest

from sonder_runtime.application.host_tools.service import HostToolInventoryService
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.host_tools.model import (
    DiscoverySource,
    ToolCategory,
    ToolRecord,
    VersionStatus,
    build_snapshot,
)
from sonder_runtime.interfaces.http import serve
from sonder_runtime.interfaces.http.facades import host_tools as facade
from tests.test_compute_job_http import _post
from tests.test_compute_snapshot_http import _get, http_server  # noqa: F401 - fixture


def _snapshot(now, count=2):
    names = ["gcc", "git", "cmake", "python3"][:count]
    categories = [ToolCategory.COMPILER, ToolCategory.VCS, ToolCategory.BUILD_SYSTEM, ToolCategory.RUNTIME]
    records = [
        ToolRecord(name=name, category=category, path=f"/home/alice/.local/bin/{name}",
                   source=DiscoverySource.PATH, on_path=True, version="1.2.3",
                   version_status=VersionStatus.OK, identity="1:1")
        for name, category in zip(names, categories)
    ]
    return build_snapshot(os="Linux", os_release="x", machine="x86_64", created_at=now,
                          duration_ms=3, tools=records)


class Discovery:
    def __init__(self):
        self.calls = []

    def discover(self, *, previous, full):
        self.calls.append(full)
        return _snapshot(1000.0, count=4)


class Store:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load(self):
        return self.snapshot

    def save(self, snapshot):
        self.snapshot = snapshot


def _service(discovery=None):
    return HostToolInventoryService(
        discovery or Discovery(), Store(_snapshot(1000.0)), clock=lambda: 1010.0,
        redact_path=lambda p: p.replace("/home/alice", "~"), executable_guard=lambda p: True,
    )


# -- facade ------------------------------------------------------------------


def test_facade_200_shape_is_redacted():
    status, body = facade.dispatch_tool_inventory(_service, {})
    assert status == 200
    assert body["object"] == "tool_inventory"
    assert [t["name"] for t in body["tools"]] == ["gcc", "git"]
    assert body["tools"][0]["path"] == "~/.local/bin/gcc"
    assert "/home/alice" not in json.dumps(body)
    status, body = facade.dispatch_tool_inventory(_service, {"category": ["vcs"]})
    assert status == 200 and [t["name"] for t in body["tools"]] == ["git"]
    status, body = facade.dispatch_tool_inventory(_service, {"name": ["gcc"]})
    assert status == 200 and [t["name"] for t in body["tools"]] == ["gcc"]


@pytest.mark.parametrize("query", [
    {"refresh": ["1"]},
    {"category": ["warp_drive"]},
    {"category": [""]},
    {"name": ["rm -rf /"]},
    {"name": ["a", "b"]},
    {"category": ["vcs"], "path": ["/etc"]},
])
def test_facade_rejects_bad_queries(query):
    status, body = facade.dispatch_tool_inventory(_service, query)
    assert status == 400 and body["error"]["code"] == "INVALID_TOOL_INVENTORY_QUERY"


def test_facade_error_mapping():
    def forbidden():
        raise PermissionError()

    class Invalid:
        def view(self, **kwargs):
            raise InvalidInput("x")

    class Broken:
        def view(self, **kwargs):
            raise RuntimeError("secret detail")

    class Huge:
        def view(self, **kwargs):
            return _service().view()

    assert facade.dispatch_tool_inventory(forbidden, {})[0] == 403
    assert facade.dispatch_tool_inventory(lambda: Invalid(), {})[0] == 400
    status, body = facade.dispatch_tool_inventory(lambda: Broken(), {})
    assert status == 503 and "secret" not in json.dumps(body)
    assert facade.dispatch_tool_inventory(None, {})[0] == 503
    assert facade.dispatch_tool_inventory(lambda: None, {})[1]["error"]["code"] == "TOOL_INVENTORY_UNAVAILABLE"


def test_facade_bounds_response_size(monkeypatch):
    monkeypatch.setattr(facade, "MAX_RESPONSE_BYTES", 64)
    status, body = facade.dispatch_tool_inventory(_service, {})
    assert status == 413 and body["error"]["code"] == "TOOL_INVENTORY_TOO_LARGE"


def test_facade_refresh_validates_payload_and_forces_discovery():
    discovery = Discovery()
    service = _service(discovery)
    assert facade.dispatch_tool_inventory_refresh(lambda: service, {"full": "yes"})[0] == 400
    assert facade.dispatch_tool_inventory_refresh(lambda: service, {"argv": ["x"]})[0] == 400
    assert discovery.calls == []
    status, body = facade.dispatch_tool_inventory_refresh(lambda: service, {"full": True})
    assert status == 200 and len(body["tools"]) == 4 and discovery.calls == [True]
    assert facade.dispatch_tool_inventory_refresh(lambda: service, {})[0] == 200
    assert discovery.calls == [True, False]


# -- serve integration ----------------------------------------------------------


def _as(monkeypatch, *, authorized, role):
    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: {
        "authorized": authorized, "mode": "account", "account": {"role": role}, "api_key": False})


def _app(service):
    return SimpleNamespace(developer_tools=SimpleNamespace(inventory=service))


@pytest.mark.parametrize("authorized,role,status", [(False, None, 401), (True, "user", 403)])
def test_http_is_admin_only_before_application_read(http_server, monkeypatch, authorized, role, status):
    _as(monkeypatch, authorized=authorized, role=role)
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: pytest.fail("denied inventory request constructed the app"))
    assert _get(http_server, "/v1/tools/inventory")[0] == status
    assert _post(http_server, "/v1/tools/inventory/refresh", {})[0] == status


def test_http_admin_read_refresh_and_query_validation(http_server, monkeypatch):
    discovery = Discovery()
    service = _service(discovery)
    _as(monkeypatch, authorized=True, role="admin")
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: _app(service))
    status, body = _get(http_server, "/v1/tools/inventory?category=compiler")
    assert status == 200 and [t["name"] for t in body["tools"]] == ["gcc"]
    assert "/home/alice" not in json.dumps(body)
    assert _get(http_server, "/v1/tools/inventory?refresh=1")[0] == 400
    assert _get(http_server, "/v1/tools/inventory?category=vcs&category=vcs")[0] == 400
    assert discovery.calls == []
    status, body = _post(http_server, "/v1/tools/inventory/refresh", {"full": True})
    assert status == 200 and len(body["tools"]) == 4 and discovery.calls == [True]
    assert _post(http_server, "/v1/tools/inventory/refresh", {"origin": "x"})[0] == 400


def test_http_get_with_a_body_is_rejected(http_server, monkeypatch):
    _as(monkeypatch, authorized=True, role="admin")
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: _app(_service()))
    request = urllib.request.Request(http_server + "/v1/tools/inventory", data=b"{}", method="GET",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    assert status == 400


def test_http_unavailable_until_developer_tools_are_composed(http_server, monkeypatch):
    _as(monkeypatch, authorized=True, role="admin")
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: SimpleNamespace())
    status, body = _get(http_server, "/v1/tools/inventory")
    assert status == 503 and body["error"]["code"] == "TOOL_INVENTORY_UNAVAILABLE"


def test_http_busy_admission_is_nonblocking(http_server, monkeypatch):
    from threading import BoundedSemaphore

    gate = BoundedSemaphore(1)
    monkeypatch.setattr(facade, "_TOOL_INVENTORY_SLOTS", gate)
    _as(monkeypatch, authorized=True, role="admin")
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: _app(_service()))
    gate.acquire()
    try:
        status, body = _get(http_server, "/v1/tools/inventory")
        assert status == 429 and body["error"]["code"] == "TOOL_INVENTORY_BUSY"
    finally:
        gate.release()
    assert _get(http_server, "/v1/tools/inventory")[0] == 200
