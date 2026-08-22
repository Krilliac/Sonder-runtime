"""HTTP handler coverage for the production extension routes."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.extensions.host import ExtensionHost
from sonder_runtime.application.extensions.experiments import EphemeralExperimentManager
from sonder_runtime.application.extensions.facade import ExtensionApplicationFacade
from sonder_runtime.application.extensions.registry import ExtensionRegistry
from sonder_runtime.bootstrap import app as bootstrap_app
import sonder_runtime.interfaces.http.serve as serve


SERVER = 'import json,sys; print(json.dumps({"type":"ready"}), flush=True); sys.stdin.read()'


def _admin_context():
    return {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "operator", "role": "admin"},
    }


@pytest.fixture()
def extension_http(tmp_path, monkeypatch):
    def factory(definition, directory):
        return ExtensionHost(definition.argv, cwd=directory)

    manager = EphemeralExperimentManager(
        lambda _definition: True,
        host_factory=factory,
        temp_root=tmp_path,
    )
    facade = ExtensionApplicationFacade(ExtensionRegistry(), manager)
    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda _self: _admin_context())
    monkeypatch.setattr(
        bootstrap_app,
        "default_app",
        lambda: SimpleNamespace(extension_facade=lambda: facade),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    manager.close()


def _request(base, path, *, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_routes_reach_typed_facade_and_preserve_boundaries(extension_http):
    status, health = _request(extension_http, "/v1/extensions")
    assert status == 200
    assert health["object"] == "extension_registry_health"
    assert health["persistence"] == "in-memory-only"

    status, defined = _request(
        extension_http, "/v1/extensions/experiments/define", method="POST",
        body={"experiment_id": "http-trial", "argv": ["python", "-c", SERVER]},
    )
    assert status == 201
    assert defined["experiment"]["state"] == "defined"

    status, inspected = _request(
        extension_http, "/v1/extensions/experiments/http-trial/inspect",
    )
    assert status == 200
    assert inspected["experiment"]["state"] == "defined"

    status, started = _request(
        extension_http, "/v1/extensions/experiments/http-trial/start", method="POST", body={},
    )
    assert status == 200
    assert started["experiment"]["state"] == "running"

    status, stopped = _request(
        extension_http, "/v1/extensions/experiments/http-trial/stop", method="POST", body={},
    )
    assert status == 200
    assert stopped["experiment"]["state"] == "stopped"
