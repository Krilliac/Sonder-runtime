from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from sonder_runtime.application.control_plane import (
    CONTROL_PLANE_SECTIONS,
    ControlPlaneSnapshotService,
)
from sonder_runtime.bootstrap import app as bootstrap_app
import sonder_runtime.interfaces.http.serve as serve


def _providers():
    return {name: (lambda name=name: ({"section": name},)) for name in CONTROL_PLANE_SECTIONS}


def test_production_handler_serves_control_plane_snapshot(monkeypatch):
    monkeypatch.setattr(
        serve.Handler,
        "_request_auth_context",
        lambda _self: {
            "mode": "account",
            "authorized": True,
            "api_key": False,
            "account": {"username": "operator", "role": "admin"},
        },
    )
    service = ControlPlaneSnapshotService(_providers())
    monkeypatch.setattr(serve, "_CONTROL_PLANE_SERVICE", service)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/admin/control-plane"
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read())
        assert response.status == 200
        assert payload["snapshot"]["sections"]["health"]["count"] == 1
        assert len(payload["digest"]) == 64
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_production_handler_rejects_non_admin_control_plane_access(monkeypatch):
    monkeypatch.setattr(
        serve.Handler,
        "_request_auth_context",
        lambda _self: {
            "mode": "account",
            "authorized": True,
            "api_key": False,
            "account": {"username": "user", "role": "member"},
        },
    )
    monkeypatch.setattr(serve, "_CONTROL_PLANE_SERVICE", ControlPlaneSnapshotService(_providers()))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/admin/control-plane"
        try:
            urllib.request.urlopen(url, timeout=10)
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError("non-admin request unexpectedly succeeded")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_production_handler_fails_closed_when_service_is_absent(monkeypatch):
    monkeypatch.setattr(
        serve.Handler,
        "_request_auth_context",
        lambda _self: {
            "mode": "account",
            "authorized": True,
            "api_key": False,
            "account": {"username": "operator", "role": "admin"},
        },
    )
    monkeypatch.setattr(serve, "_CONTROL_PLANE_SERVICE", None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/admin/control-plane"
        try:
            urllib.request.urlopen(url, timeout=10)
        except urllib.error.HTTPError as error:
            assert error.code == 503
            assert json.loads(error.read()) == {"error": "control_plane_unavailable"}
        else:
            raise AssertionError("absent control-plane service unexpectedly succeeded")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_default_application_graph_serves_control_plane(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path))
    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application()
    monkeypatch.setattr(
        serve.Handler,
        "_request_auth_context",
        lambda _self: {
            "mode": "account",
            "authorized": True,
            "api_key": False,
            "account": {"username": "operator", "role": "admin"},
        },
    )
    monkeypatch.setattr(serve, "_CONTROL_PLANE_SERVICE", application.control_plane_snapshot_service)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/admin/control-plane"
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read())
        assert response.status == 200
        assert payload["snapshot"]["sections"]["plans"]["records"] == []
        assert payload["snapshot"]["sections"]["context"]["records"][0]["available"] is False
        assert payload["snapshot"]["sections"]["health"]["count"] >= 1
    finally:
        httpd.shutdown()
        httpd.server_close()
