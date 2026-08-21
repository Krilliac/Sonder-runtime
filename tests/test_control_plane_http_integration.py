from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from sonder_runtime.application.control_plane import (
    CONTROL_PLANE_SECTIONS,
    ControlPlaneSnapshotService,
)
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
