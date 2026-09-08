"""Source-only configuration must not expose a mobility-source HTTP namespace."""

from dataclasses import replace
from http.server import ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request

from sonder_runtime.domain.operational_capabilities import (
    build_operational_capabilities,
)
from sonder_runtime.platform.artifact_mobility_source_config import (
    ArtifactMobilitySourceConfig,
)
from sonder_runtime.platform.config import SonderConfig, StateConfig


def _source_only_config(tmp_path):
    return SonderConfig(
        state=StateConfig(home=str(tmp_path / "state")),
        artifact_mobility_source=ArtifactMobilitySourceConfig(
            enabled=True,
            store_dir=str(tmp_path / "private-source"),
            principal_id="principal-a",
            project_id="project-a",
            source_owner_id="owner-a",
        ),
    )


def _get(origin, path):
    try:
        with urllib.request.urlopen(origin + path, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_source_only_typed_config_adds_no_mobility_source_http_surface(tmp_path, monkeypatch):
    from sonder_runtime.interfaces.http import serve

    config = _source_only_config(tmp_path)
    # Isolate the global composition seam without closing a binding owned by
    # another test.  Source configuration has no slot in this HTTP boundary.
    names = (
        "_ARTIFACT_TRANSFER_BINDING", "_ARTIFACT_TRANSFER_CONFIG",
        "_APP_CONTROL_BINDING", "_APP_CONTROL_CONFIG", "CONFIGURED_PORT",
        "API_KEY", "AUTH_SECRET", "HOST", "REQUIRE_ACCOUNT", "AUTH_MODE",
        "CORS_ORIGINS", "TLS_TERMINATED_BY_PROXY", "ALLOW_REGISTRATION",
        "MAX_REQUEST_BYTES", "MAX_DISCARDED_BODY_BYTES", "REQUEST_TIMEOUT_SECONDS",
        "STREAM_IDLE_TIMEOUT_SECONDS", "HTTP_SESSION_STATE_LIMIT",
        "HTTP_SESSION_STATE_OWNER_LIMIT", "TRAIN_MAX_N", "_HEALTH_STATUS_FACADE",
        "_TRUSTED_PROXY_NETWORKS",
    )
    for name in names:
        monkeypatch.setattr(serve, name, getattr(serve, name))
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", None)
    monkeypatch.setattr(serve, "_APP_CONTROL_BINDING", None)

    serve.configure_typed_config(config)
    receiver = serve._ARTIFACT_TRANSFER_BINDING
    assert receiver is not None
    assert receiver._service is None
    assert "_ARTIFACT_MOBILITY_SOURCE_BINDING" not in vars(serve)
    assert not (tmp_path / "private-source").exists()

    # This projection feeds ordinary public status. A source-only setting does
    # not project a binding, port, source section, or private path into it.
    projection = json.dumps(build_operational_capabilities(config=config), sort_keys=True)
    for private_value in (
        "artifact_mobility_source",
        "publisher",
        "reader",
        config.artifact_mobility_source.store_dir,
        config.state.home,
    ):
        assert private_value not in projection

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        for path in (
            "/v1/artifact-mobility-sources/" + "a" * 32,
            "/v1/artifact-mobility-source/" + "a" * 32,
        ):
            status, body = _get(origin, path)
            assert status == 404
            assert json.loads(body)["error"]["type"] == "not_found"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        receiver.close()
