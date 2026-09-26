"""API-007/008 over HTTP: the served client schema and the reconnect route.

The application already owned the portable client schema, the resumable
stream semantics and the reconnect planner, but no route served them, and the
composed protocol facade had no stream, so a reconnect could only ever be
rejected.  The HTTP host now serves ``GET /v1/client/schema`` and
``POST /v1/client/reconnect`` and owns one real stream, ``control.<instance>``,
that records permission-mode changes; the schema route advertises its id.
"""
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
from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.protocol.client_schema import build_client_schema
from sonder_runtime.application.protocol.facade import (
    ProtocolApplicationFacade,
    ProtocolAuthorizationError,
)
from sonder_runtime.application.protocol.mobile_parity import (
    MobileWireError,
    decode_reconnect_request,
)
from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs
from sonder_runtime.interfaces.http.facades.client_protocol import (
    CONTROL_STREAM_PREFIX,
    ClientProtocolHost,
)

pytestmark = pytest.mark.integration


def _catalogs():
    return GeneratedCatalogs.generate(
        InMemoryToolRegistry((ToolDescriptor("status"), ToolDescriptor("read_file"))),
        commands=("help",),
    )


class _Mode:
    def __init__(self, value="manual"):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture()
def mode(monkeypatch):
    current = _Mode()
    monkeypatch.setattr(sonder_serve.permission_policy, "current_mode", current, raising=False)
    return current


@pytest.fixture()
def application(monkeypatch):
    catalogs = _catalogs()
    app = SimpleNamespace(protocol=ProtocolApplicationFacade.compose(catalogs), catalogs=catalogs)
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: app)
    monkeypatch.setattr(sonder_serve, "_CLIENT_PROTOCOL_HOST", None)
    return app


@pytest.fixture()
def authorized(monkeypatch):
    state = {"authorized": True}
    monkeypatch.setattr(
        sonder_serve.Handler, "_request_auth_context",
        lambda _self: {"authorized": state["authorized"], "mode": "api-key",
                       "api_key": state["authorized"], "account": None},
    )
    return state


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


def _request(base, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _schema_and_stream(base):
    status, schema = _request(base, "/v1/client/schema")
    assert status == 200
    (stream,) = schema["streams"]
    return schema["schema"]["digest"], stream["stream_id"]


def _reconnect(digest, cursors, batch_limit=256, client_id="flutter-1"):
    return {
        "type": "reconnect", "version": 1, "client_id": client_id,
        "schema_digest": digest, "batch_limit": batch_limit,
        "cursors": [{"stream_id": sid, "watermark": mark} for sid, mark in cursors],
    }


def test_unauthenticated_requests_are_refused_before_composition(
    http_server, authorized, monkeypatch,
):
    authorized["authorized"] = False
    monkeypatch.setattr(
        "sonder_runtime.bootstrap.app.default_app",
        lambda: pytest.fail("an unauthenticated request must not compose the host"),
    )
    status, _ = _request(http_server, "/v1/client/schema")
    assert status == 401
    status, _ = _request(http_server, "/v1/client/reconnect", _reconnect(None, ()))
    assert status == 401


def test_the_served_schema_is_the_one_built_from_the_catalog(
    http_server, authorized, application, mode,
):
    status, body = _request(http_server, "/v1/client/schema")
    assert status == 200
    assert body["type"] == "client_schema" and body["version"] == 1
    expected = build_client_schema(application.catalogs)
    assert body["schema"]["digest"] == expected.digest
    assert body["schema"] == expected.as_dict()
    (stream,) = body["streams"]
    assert stream["stream_id"].startswith(CONTROL_STREAM_PREFIX + ".")
    assert stream["event_types"] == ["control.snapshot"]


def test_a_stale_digest_is_told_to_refresh_the_schema(
    http_server, authorized, application, mode,
):
    stale = "0" * 64
    _, stream_id = _schema_and_stream(http_server)
    status, body = _request(
        http_server, "/v1/client/reconnect", _reconnect(stale, [(stream_id, 0)]),
    )
    assert status == 200
    assert body["type"] == "reconnect_response"
    assert body["freshness"]["state"] == "stale"
    assert body["freshness"]["expected_digest"] == build_client_schema(application.catalogs).digest
    assert [r["disposition"] for r in body["results"]] == ["refresh_schema"]


def test_a_missing_digest_is_told_to_refresh_the_schema(
    http_server, authorized, application, mode,
):
    _, stream_id = _schema_and_stream(http_server)
    status, body = _request(
        http_server, "/v1/client/reconnect", _reconnect(None, [(stream_id, 0)]),
    )
    assert status == 200
    assert body["freshness"]["state"] == "stale"
    assert [r["disposition"] for r in body["results"]] == ["refresh_schema"]


def test_a_watermark_resumes_the_control_stream_in_bounded_batches(
    http_server, authorized, application, mode,
):
    digest, stream_id = _schema_and_stream(http_server)
    # The host published the starting mode when it was composed; two more
    # changes are observed through the mode read the app already makes.
    for value in ("auto", "plan"):
        mode.value = value
        status, _ = _request(http_server, "/v1/permission-mode")
        assert status == 200

    status, first = _request(
        http_server, "/v1/client/reconnect",
        _reconnect(digest, [(stream_id, 0)], batch_limit=2),
    )
    assert status == 200
    assert first["freshness"]["state"] == "current"
    result = first["results"][0]
    assert result["disposition"] == "resumed"
    batch = result["batch"]
    assert [e["payload"]["permission_mode"] for e in batch["events"]] == ["manual", "auto"]
    assert all(e["event_type"] == "control.snapshot" for e in batch["events"])
    assert batch["has_more"] is True
    assert batch["next_watermark"] == 2

    status, rest = _request(
        http_server, "/v1/client/reconnect",
        _reconnect(digest, [(stream_id, batch["next_watermark"])], batch_limit=2),
    )
    batch = rest["results"][0]["batch"]
    assert [e["payload"]["permission_mode"] for e in batch["events"]] == ["plan"]
    assert batch["has_more"] is False
    assert batch["next_watermark"] == 3


def test_an_in_process_change_outside_the_api_is_recorded_before_the_reconnect_plan(
    http_server, authorized, application, mode,
):
    digest, stream_id = _schema_and_stream(http_server)
    # Switched in this process by a path other than the API (for example the
    # permission_mode tool during a served chat).  A change made in another
    # process is not visible here: permission_modes loads its file once.
    mode.value = "acceptEdits"
    _, body = _request(
        http_server, "/v1/client/reconnect", _reconnect(digest, [(stream_id, 1)]),
    )
    events = body["results"][0]["batch"]["events"]
    assert [e["payload"]["permission_mode"] for e in events] == ["acceptEdits"]


def test_unknown_streams_and_future_watermarks_are_rejected(
    http_server, authorized, application, mode,
):
    digest, stream_id = _schema_and_stream(http_server)
    _, body = _request(
        http_server, "/v1/client/reconnect",
        _reconnect(digest, [("not-a-stream", 0), (stream_id, 99)]),
    )
    assert [r["disposition"] for r in body["results"]] == ["rejected", "rejected"]


@pytest.mark.parametrize("body", [
    {"type": "reconnect", "version": 2, "client_id": "x"},
    {"type": "reconnect", "version": 1, "client_id": 7},
    {"type": "reconnect", "version": 1, "client_id": "x", "provider": "ollama"},
    {"type": "reconnect", "version": 1, "client_id": "x", "schema_digest": "short"},
    {"type": "reconnect", "version": 1, "client_id": "x",
     "cursors": [{"stream_id": "control", "watermark": -1}]},
    ["not", "an", "object"],
])
def test_a_malformed_reconnect_is_a_400(http_server, authorized, application, mode, body):
    status, payload = _request(http_server, "/v1/client/reconnect", body)
    assert status == 400
    assert payload["error"]["type"] == "invalid_request"


def test_the_host_is_rebuilt_for_a_new_application_graph(authorized, application, mode, monkeypatch):
    first = sonder_serve._client_protocol_host()
    assert sonder_serve._client_protocol_host() is first
    replacement = SimpleNamespace(protocol=ProtocolApplicationFacade.compose(_catalogs()))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: replacement)
    second = sonder_serve._client_protocol_host()
    assert second is not first and second.protocol is replacement.protocol


def test_the_application_graph_serves_its_tool_catalog_schema(
    http_server, authorized, mode, monkeypatch,
):
    from sonder_runtime.bootstrap.app import build_application

    app = build_application()
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: app)
    monkeypatch.setattr(sonder_serve, "_CLIENT_PROTOCOL_HOST", None)
    assert app.protocol is not None
    status, body = _request(http_server, "/v1/client/schema")
    assert status == 200
    assert body["schema"]["digest"] == build_client_schema(app.tools.catalogs).digest
    assert body["schema"]["sdk"]["tools"], "the served tool catalog is not empty"


# --- the host, without HTTP -------------------------------------------------


def test_only_the_host_opens_streams_and_only_authenticated_views_reconnect():
    protocol = ProtocolApplicationFacade.compose(_catalogs())
    host = ClientProtocolHost(protocol, control_state=lambda: {"permission_mode": "manual"})
    with pytest.raises(ProtocolAuthorizationError):
        protocol.open_stream("other", client_id="flutter-1")
    request = _reconnect(protocol.schema.digest, [(host.stream_id, 0)])
    with pytest.raises(ProtocolAuthorizationError):
        host.reconnect(request, authenticated=False)
    assert host.reconnect(request, authenticated=True)["results"][0]["disposition"] == "resumed"


def _resume(host, digest, stream_id, watermark):
    body = host.reconnect(_reconnect(digest, [(stream_id, watermark)]), authenticated=True)
    (result,) = body["results"]
    return result


def test_a_cursor_from_an_earlier_host_is_rejected_not_silently_resumed():
    # A restart builds a new host whose stream starts again at sequence 1.  A
    # client that last saw sequence 1 of the old stream must not be told it
    # is up to date with the new one while the mode has changed.
    first_protocol = ProtocolApplicationFacade.compose(_catalogs())
    first = ClientProtocolHost(first_protocol, control_state=lambda: {"permission_mode": "auto"})
    digest = first_protocol.schema.digest
    seen = _resume(first, digest, first.stream_id, 0)
    assert [e["payload"]["permission_mode"] for e in seen["batch"]["events"]] == ["auto"]
    watermark = seen["batch"]["next_watermark"]
    assert watermark == 1

    restarted_protocol = ProtocolApplicationFacade.compose(_catalogs())
    restarted = ClientProtocolHost(
        restarted_protocol, control_state=lambda: {"permission_mode": "manual"},
    )
    assert restarted_protocol.schema.digest == digest, "the schema itself did not change"
    assert restarted.stream_id != first.stream_id
    stale = _resume(restarted, digest, first.stream_id, watermark)
    assert stale["disposition"] == "rejected"
    assert stale["reason"] == "unknown stream"

    advertised = [s["stream_id"] for s in restarted.schema_payload()["streams"]]
    assert advertised == [restarted.stream_id]
    fresh = _resume(restarted, digest, restarted.stream_id, 0)
    assert [e["payload"]["permission_mode"] for e in fresh["batch"]["events"]] == ["manual"]


def test_a_new_application_graph_rejects_the_previous_hosts_cursor(
    http_server, authorized, application, mode, monkeypatch,
):
    digest, old_stream = _schema_and_stream(http_server)
    _, body = _request(http_server, "/v1/client/reconnect", _reconnect(digest, [(old_stream, 0)]))
    assert body["results"][0]["batch"]["next_watermark"] == 1

    replacement = SimpleNamespace(protocol=ProtocolApplicationFacade.compose(_catalogs()))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: replacement)
    mode.value = "plan"
    _, body = _request(http_server, "/v1/client/reconnect", _reconnect(digest, [(old_stream, 1)]))
    assert [(r["disposition"], r["reason"]) for r in body["results"]] == [
        ("rejected", "unknown stream"),
    ]
    _, new_stream = _schema_and_stream(http_server)
    assert new_stream != old_stream
    _, body = _request(http_server, "/v1/client/reconnect", _reconnect(digest, [(new_stream, 0)]))
    events = body["results"][0]["batch"]["events"]
    assert [e["payload"]["permission_mode"] for e in events] == ["plan"]


def test_a_full_stream_is_compacted_into_a_snapshot_not_refused():
    protocol = ProtocolApplicationFacade.compose(_catalogs())
    current = {"permission_mode": "m0"}
    host = ClientProtocolHost(protocol, control_state=lambda: dict(current), capacity=2)
    for index in range(1, 5):
        current["permission_mode"] = "m%d" % index
        assert host.observe() is True
    assert host.observe() is False, "an unchanged state publishes nothing"
    body = host.reconnect(
        _reconnect(protocol.schema.digest, [(host.stream_id, 0)]), authenticated=True,
    )
    batch = body["results"][0]["batch"]
    assert batch["snapshot"] is not None
    assert batch["snapshot"]["state"] == {"permission_mode": "m3"}
    assert [e["payload"]["permission_mode"] for e in batch["events"]] == ["m4"]
    assert batch["next_watermark"] == 5


def test_concurrent_observers_never_leave_a_stale_state_newest():
    # A mode POST racing a reconnect: observer A reads the mode, stalls, the
    # mode changes and observer B runs.  A must not then publish its stale
    # read after B's newer one.
    protocol = ProtocolApplicationFacade.compose(_catalogs())
    current = {"permission_mode": "manual"}
    stalled, release = threading.Event(), threading.Event()

    def control_state():
        value = dict(current)
        if threading.current_thread().name == "observer-a" and not release.is_set():
            stalled.set()
            assert release.wait(10)
        return value

    host = ClientProtocolHost(protocol, control_state=control_state)
    current["permission_mode"] = "auto"
    a = threading.Thread(target=host.observe, name="observer-a")
    a.start()
    assert stalled.wait(10)
    current["permission_mode"] = "plan"
    b = threading.Thread(target=host.observe, name="observer-b")
    b.start()
    b.join(0.5)  # finished (unserialized) or blocked behind A (serialized)
    release.set()
    a.join(10)
    b.join(10)
    assert not a.is_alive() and not b.is_alive()

    body = host.reconnect(
        _reconnect(protocol.schema.digest, [(host.stream_id, 0)]), authenticated=True,
    )
    modes = [e["payload"]["permission_mode"] for e in body["results"][0]["batch"]["events"]]
    assert modes == ["manual", "auto", "plan"]
    assert modes[-1] == current["permission_mode"]


def test_the_wire_decoder_refuses_a_non_string_client_id():
    with pytest.raises(MobileWireError, match="client_id"):
        decode_reconnect_request({"type": "reconnect", "version": 1, "client_id": ["x"]})
    with pytest.raises(MobileWireError, match="client_id"):
        decode_reconnect_request({"type": "reconnect", "version": 1, "client_id": "x" * 257})
