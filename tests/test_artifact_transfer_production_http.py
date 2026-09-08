"""The production HTTP handler uses dedicated, live, fixed-scope authority."""
from dataclasses import replace
from email.message import Message
import hashlib
import http.client
import io
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from sonder_runtime.platform.config import SonderConfig, Secrets, StateConfig
from sonder_runtime.platform.artifact_transfer_config import ArtifactTransferConfig
from sonder_runtime.interfaces.http import serve


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    config = SonderConfig(
        state=StateConfig(home=str(tmp_path / "home")),
        secrets=Secrets(api_key="admin-" + "a" * 32,
                        artifact_transfer_key="transfer-" + "b" * 32),
        artifact_transfer=ArtifactTransferConfig(
            enabled=True, store_dir=str(tmp_path / "private"),
            principal_id="alice", project_id="project-a", peer_node_id="peer-b",
            receiver_identity_id="receiver-a",
            grant_id="grant-a", expires_at=int(time.time()) + 3600,
            can_read=True, can_write=True,
        ),
    )
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
    current = [config]
    binding = ArtifactTransferBinding(lambda: current[0])
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", binding)
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address, current, binding
    server.shutdown()
    server.server_close()
    binding.close()
    thread.join(timeout=5)


def request(receiver, method, path, payload=None, *, key=None, raw=None, headers=None):
    address, current, _ = receiver
    connection = http.client.HTTPConnection(*address, timeout=10)
    fields = {"Authorization": "Bearer " + (key or current[0].secrets.artifact_transfer_key)}
    data = raw
    if payload is not None:
        data = json.dumps(payload).encode()
        fields["Content-Type"] = "application/json"
    fields.update(headers or {})
    connection.request(method, path, data, fields)
    response = connection.getresponse()
    body = response.read()
    result = (response.status, dict(response.getheaders()), body)
    connection.close()
    return result


def begin(receiver, data=b"abc", **extra):
    return request(receiver, "POST", "/v1/artifact-transfers", {
        "spec": {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                 "media_type": "application/octet-stream"},
        "command_id": "begin-one", **extra,
    })


def mobility_headers(capability="a" * 64):
    return {
        "X-Sonder-Artifact-Mobility-Version": "mobility-v1",
        "X-Sonder-Artifact-Mobility-Receipt-Capability": capability,
    }


def test_mobility_v1_begin_returns_a_durable_envelope(receiver):
    data = b"mobility receiver contract"
    status, _, body = request(
        receiver,
        "POST",
        "/v1/artifact-transfers",
        {
            "spec": {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "media_type": "application/octet-stream",
            },
            "command_id": "mobility-begin",
        },
        headers=mobility_headers(),
    )
    assert status == 200, body
    envelope = json.loads(body)
    assert set(envelope) == {
        "protocol_version",
        "recipient_attestation",
        "command_id",
        "spec",
        "receipt",
    }
    assert envelope["protocol_version"] == "mobility-v1"
    assert envelope["command_id"] == "mobility-begin"
    assert envelope["spec"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert envelope["receipt"]["transfer_id"]


def test_mobility_attestation_is_canonical_and_binding_scoped(receiver):
    status, _, body = request(
        receiver, "GET", "/v1/artifact-transfers/recipient-attestation"
    )
    assert status == 200, body
    attestation = json.loads(body)
    expected = {
        "protocol_version",
        "receiver_identity_id",
        "principal_id",
        "project_id",
        "authorized_source_owner_id",
        "grant_id",
        "grant_revision",
        "can_write",
        "max_object_bytes",
        "sha256",
    }
    assert set(attestation) == expected
    canonical = {name: value for name, value in attestation.items() if name != "sha256"}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert attestation["sha256"] == hashlib.sha256(encoded).hexdigest()
    assert "private" not in json.dumps(attestation)
    prior = attestation["sha256"]
    changes = {
        "receiver_identity_id": "receiver-b",
        "peer_node_id": "source-owner-b",
        "grant_id": "grant-b",
        "grant_revision": 2,
        "can_write": False,
        "max_object_bytes": 4096,
    }
    for name, value in changes.items():
        current = receiver[1]
        current[0] = replace(
            current[0], artifact_transfer=replace(current[0].artifact_transfer, **{name: value})
        )
        status, _, body = request(
            receiver, "GET", "/v1/artifact-transfers/recipient-attestation"
        )
        assert status == 200, body
        changed = json.loads(body)
        assert changed["sha256"] != prior
        prior = changed["sha256"]


def test_mobility_attestation_requires_the_receiver_binding(receiver, monkeypatch):
    path = "/v1/artifact-transfers/recipient-attestation"
    assert request(receiver, "GET", path, key="wrong-transfer-bearer")[0] == 401
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", None)
    assert request(receiver, "GET", path)[0] == 503


def test_mobility_attestation_is_unavailable_when_receiver_is_disabled(receiver):
    current = receiver[1]
    current[0] = replace(
        current[0], artifact_transfer=replace(current[0].artifact_transfer, enabled=False)
    )
    assert request(receiver, "GET", "/v1/artifact-transfers/recipient-attestation")[0] == 503


def test_missing_receiver_identity_rejects_mobility_before_legacy_dedup(receiver):
    current = receiver[1]
    current[0] = replace(
        current[0], artifact_transfer=replace(current[0].artifact_transfer, receiver_identity_id="")
    )
    data = b"identity required before mobility row"
    payload = {
        "spec": {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "media_type": "application/octet-stream",
        },
        "command_id": "identity-required",
    }
    status, _, body = request(
        receiver, "POST", "/v1/artifact-transfers", payload, headers=mobility_headers()
    )
    assert status == 403
    assert json.loads(body) == {"error": {"code": "FORBIDDEN"}}
    status, _, body = request(receiver, "POST", "/v1/artifact-transfers", payload)
    assert status == 200, body
    assert "protocol_version" not in json.loads(body)


def test_write_only_mobility_receipt_is_capability_and_transfer_bound(receiver):
    current = receiver[1]
    current[0] = replace(
        current[0], artifact_transfer=replace(current[0].artifact_transfer, can_read=False)
    )
    data = b"write-only mobility receipt"
    status, _, body = request(
        receiver,
        "POST",
        "/v1/artifact-transfers",
        {
            "spec": {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "media_type": "application/octet-stream",
            },
            "command_id": "write-only-begin",
        },
        headers=mobility_headers(),
    )
    assert status == 200, body
    envelope = json.loads(body)
    transfer_id = envelope["receipt"]["transfer_id"]
    status, _, body = request(
        receiver,
        "POST",
        f"/v1/artifact-transfers/{transfer_id}/mobility-receipt",
        {"command_id": "write-only-begin"},
        headers=mobility_headers(),
    )
    assert status == 200, body
    assert json.loads(body) == envelope
    assert request(receiver, "GET", f"/v1/artifact-transfers/{transfer_id}")[0] == 403
    assert request(receiver, "GET", f"/v1/artifacts/{transfer_id}")[0] == 403
    status, _, body = request(
        receiver,
        "POST",
        f"/v1/artifact-transfers/{transfer_id}/mobility-receipt",
        {"command_id": "write-only-begin"},
        headers=mobility_headers("c" * 64),
    )
    assert status == 403
    assert json.loads(body) == {"error": {"code": "FORBIDDEN"}}
    status, _, body = request(
        receiver,
        "POST",
        "/v1/artifact-transfers/" + "f" * 32 + "/mobility-receipt",
        {"command_id": "write-only-begin"},
        headers=mobility_headers(),
    )
    assert status == 403
    assert json.loads(body) == {"error": {"code": "FORBIDDEN"}}
    status, _, body = request(
        receiver,
        "POST",
        f"/v1/artifact-transfers/{transfer_id}/mobility-receipt",
        {"command_id": "write-only-begin"},
        headers={"X-Sonder-Artifact-Mobility-Version": "mobility-v1"},
    )
    assert status == 400
    assert "recipient_attestation" not in body.decode()
    assert "private" not in body.decode()


def test_mobility_headers_are_strict_and_value_free(receiver):
    secretish = "https://capability.example.invalid/private"
    status, _, body = request(
        receiver,
        "POST",
        "/v1/artifact-transfers",
        {
            "spec": {
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "size_bytes": 1,
                "media_type": "application/octet-stream",
            },
            "command_id": "bad-header",
        },
        headers={
            "X-Sonder-Artifact-Mobility-Version": "mobility-v1",
            "X-Sonder-Artifact-Mobility-Receipt-Capability": secretish,
        },
    )
    assert status == 400
    assert json.loads(body) == {"error": {"code": "MOBILITY_PROTOCOL"}}
    assert secretish not in body.decode()
    status, _, body = request(
        receiver,
        "POST",
        "/v1/artifact-transfers",
        {
            "spec": {
                "sha256": hashlib.sha256(b"y").hexdigest(),
                "size_bytes": 1,
                "media_type": "application/octet-stream",
            },
            "command_id": "empty-version-header",
        },
        headers={"X-Sonder-Artifact-Mobility-Version": ""},
    )
    assert status == 400
    assert json.loads(body) == {"error": {"code": "MOBILITY_PROTOCOL"}}
    status, _, body = request(
        receiver,
        "GET",
        "/v1/artifact-transfers/" + "a" * 32,
        headers=mobility_headers(),
    )
    assert status == 400
    assert json.loads(body) == {"error": {"code": "MOBILITY_PROTOCOL"}}


def test_duplicate_mobility_headers_are_rejected_without_echo(receiver):
    connection = http.client.HTTPConnection(*receiver[0], timeout=10)
    payload = json.dumps(
        {
            "spec": {
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "size_bytes": 1,
                "media_type": "application/octet-stream",
            },
            "command_id": "duplicate-mobility-header",
        }
    ).encode()
    connection.putrequest("POST", "/v1/artifact-transfers")
    connection.putheader("Authorization", "Bearer " + receiver[1][0].secrets.artifact_transfer_key)
    connection.putheader("Content-Length", str(len(payload)))
    connection.putheader("Content-Type", "application/json")
    connection.putheader("X-Sonder-Artifact-Mobility-Version", "mobility-v1")
    connection.putheader("X-Sonder-Artifact-Mobility-Version", "mobility-v1")
    connection.endheaders(payload)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    assert response.status == 400
    assert json.loads(body) == {"error": {"code": "INVALID_HEADERS"}}
    assert "duplicate-mobility-header" not in body.decode()


def test_mobility_rows_require_the_versioned_capability_for_append(receiver):
    data = b"versioned mobility append"
    status, _, body = request(
        receiver,
        "POST",
        "/v1/artifact-transfers",
        {
            "spec": {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "media_type": "application/octet-stream",
            },
            "command_id": "versioned-append",
        },
        headers=mobility_headers(),
    )
    assert status == 200, body
    transfer_id = json.loads(body)["receipt"]["transfer_id"]
    upload_path = f"/v1/artifact-transfers/{transfer_id}/chunks/0"
    upload_headers = {
        "Content-Type": "application/octet-stream",
        "X-Sonder-Chunk-Sha256": hashlib.sha256(data).hexdigest(),
    }
    status, _, body = request(receiver, "PUT", upload_path, raw=data, headers=upload_headers)
    assert status == 400
    assert json.loads(body) == {"error": {"code": "MOBILITY_PROTOCOL"}}
    status, _, body = request(
        receiver,
        "PUT",
        upload_path,
        raw=data,
        headers={**upload_headers, **mobility_headers()},
    )
    assert status == 200, body
    assert json.loads(body)["next_offset"] == len(data)
    status, _, body = request(
        receiver,
        "POST",
        f"/v1/artifact-transfers/{transfer_id}/mobility-receipt",
        {"command_id": "versioned-append"},
        headers=mobility_headers(),
    )
    assert status == 200, body
    assert json.loads(body)["receipt"]["offset"] == len(data)


def test_mobility_seal_and_final_confirmation_stay_versioned(receiver):
    data = b"versioned mobility seal"
    status, _, body = request(
        receiver,
        "POST",
        "/v1/artifact-transfers",
        {
            "spec": {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "media_type": "application/octet-stream",
            },
            "command_id": "versioned-seal",
        },
        headers=mobility_headers(),
    )
    assert status == 200, body
    transfer_id = json.loads(body)["receipt"]["transfer_id"]
    assert request(
        receiver,
        "PUT",
        f"/v1/artifact-transfers/{transfer_id}/chunks/0",
        raw=data,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Sonder-Chunk-Sha256": hashlib.sha256(data).hexdigest(),
            **mobility_headers(),
        },
    )[0] == 200
    seal_path = f"/v1/artifact-transfers/{transfer_id}/seal"
    assert request(receiver, "POST", seal_path, {"command_id": "seal-v1"})[0] == 400
    status, _, body = request(
        receiver,
        "POST",
        seal_path,
        {"command_id": "seal-v1"},
        headers=mobility_headers(),
    )
    initial = json.loads(body)
    assert status == (202 if initial["receipt"]["state"] == "verifying" else 200), body
    assert set(initial) == {
        "protocol_version", "recipient_attestation", "command_id", "spec", "receipt"
    }
    until = time.monotonic() + 10
    while time.monotonic() < until:
        status, _, body = request(
            receiver,
            "POST",
            f"/v1/artifact-transfers/{transfer_id}/mobility-receipt",
            {"command_id": "versioned-seal"},
            headers=mobility_headers(),
        )
        final = json.loads(body)
        assert status == (202 if final["receipt"]["state"] == "verifying" else 200), body
        if final["receipt"]["state"] == "sealed":
            break
        time.sleep(.01)
    assert final["receipt"]["state"] == "sealed"
    assert request(receiver, "GET", f"/v1/artifacts/{transfer_id}")[0] == 400


def test_mobility_abort_is_versioned_and_capability_bound(receiver):
    data = b"versioned mobility abort"
    status, _, body = request(
        receiver,
        "POST",
        "/v1/artifact-transfers",
        {
            "spec": {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "media_type": "application/octet-stream",
            },
            "command_id": "versioned-abort",
        },
        headers=mobility_headers(),
    )
    assert status == 200, body
    transfer_id = json.loads(body)["receipt"]["transfer_id"]
    abort_path = f"/v1/artifact-transfers/{transfer_id}/abort"
    assert request(receiver, "POST", abort_path, {"command_id": "abort-v1"})[0] == 400
    status, _, body = request(
        receiver,
        "POST",
        abort_path,
        {"command_id": "abort-v1"},
        headers=mobility_headers(),
    )
    assert status == 200, body
    aborted = json.loads(body)
    assert aborted["receipt"]["state"] == "aborted"
    status, _, body = request(
        receiver,
        "POST",
        f"/v1/artifact-transfers/{transfer_id}/mobility-receipt",
        {"command_id": "versioned-abort"},
        headers=mobility_headers(),
    )
    assert status == 200, body
    assert json.loads(body)["receipt"]["state"] == "aborted"


def test_real_handler_round_trip(receiver):
    data = b"bounded opaque bytes"
    status, _, body = begin(receiver, data)
    assert status == 200, body
    transfer_id = json.loads(body)["transfer_id"]
    status, _, body = request(receiver, "PUT", f"/v1/artifact-transfers/{transfer_id}/chunks/0",
        raw=data, headers={"Content-Type": "application/octet-stream",
                          "X-Sonder-Chunk-Sha256": hashlib.sha256(data).hexdigest()})
    assert status == 200, body
    status, _, body = request(receiver, "POST", f"/v1/artifact-transfers/{transfer_id}/seal",
                             {"command_id": "seal-one"})
    assert status in (200, 202), body
    until = time.monotonic() + 10
    while time.monotonic() < until:
        status, _, body = request(receiver, "GET", f"/v1/artifact-transfers/{transfer_id}")
        receipt = json.loads(body)
        if receipt["state"] != "verifying":
            break
        time.sleep(.01)
    assert receipt["state"] == "sealed", receipt
    artifact_id = receipt["artifact"]["artifact_id"]
    status, headers, body = request(receiver, "GET", f"/v1/artifacts/{artifact_id}/bytes?offset=0&length={len(data)}")
    assert status == 200 and body == data
    assert headers["X-Sonder-Offset"] == "0"
    assert headers["X-Sonder-Size"] == str(len(data))
    assert headers["X-Sonder-Artifact-Sha256"] == hashlib.sha256(data).hexdigest()
    assert headers["X-Sonder-Chunk-Sha256"] == hashlib.sha256(data).hexdigest()


def test_global_admin_key_and_forwarded_identity_do_not_grant_access(receiver):
    status, _, _ = request(receiver, "POST", "/v1/artifact-transfers", {},
                           key=receiver[1][0].secrets.api_key,
                           headers={"X-Sonder-Principal": "alice"})
    assert status == 401
    assert begin(receiver, principal_id="mallory")[0] == 400


def test_live_scope_change_hides_prior_receipt(receiver):
    status, _, body = begin(receiver)
    assert status == 200, body
    identity = json.loads(body)["transfer_id"]
    current = receiver[1]
    current[0] = replace(current[0], artifact_transfer=replace(
        current[0].artifact_transfer, project_id="different-project", grant_revision=2))
    assert request(receiver, "GET", "/v1/artifact-transfers/" + identity)[0] == 404
    current[0] = replace(current[0], artifact_transfer=replace(current[0].artifact_transfer, enabled=False))
    assert request(receiver, "GET", "/v1/artifact-transfers/" + identity)[0] == 503


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/artifact-transfers/" + "a" * 32 + "?principal_id=alice"),
    ("GET", "/v1/artifact-transfers/%61" + "a" * 31),
    ("GET", "/v1/artifacts/" + "a" * 32 + "/bytes?offset=0&offset=1&length=1"),
    ("GET", "/v1/artifacts/" + "a" * 32 + "/bytes?offset=0&length=1048577"),
])
def test_strict_routes(receiver, method, path):
    assert request(receiver, method, path)[0] == 400


def test_body_caps_reject_without_reading_or_discarding(receiver):
    from sonder_runtime.interfaces.http.artifact_transfer import handle_artifact_transfer
    class Unreadable(io.BytesIO):
        def read(self, *args):
            raise AssertionError("oversized body was read")
    for method, path, size in [
        ("POST", "/v1/artifact-transfers", 32769),
        ("PUT", "/v1/artifact-transfers/" + "a" * 32 + "/chunks/0", 1048577),
    ]:
        handler = object.__new__(serve.Handler)
        handler.path = path
        handler.client_address = ("127.0.0.1", 1234)
        handler.connection = None
        handler.headers = Message()
        handler.headers["Content-Length"] = str(size)
        handler.headers["Authorization"] = "Bearer " + receiver[1][0].secrets.artifact_transfer_key
        handler.rfile = Unreadable()
        handler._request_body_consumed = False
        handler._correlation_id = "test"
        replies = []
        handler._send_json_payload = lambda body, status=200, **kwargs: replies.append(status)
        assert handle_artifact_transfer(handler, method, receiver[2])
        assert replies == [413]
        assert handler._settle_unread_request_body() is True


def test_transport_uses_socket_and_host_config_not_forwarded_claim(receiver):
    from sonder_runtime.interfaces.http.artifact_transfer import transport_allowed
    config = receiver[1][0]
    assert transport_allowed(None, "127.0.0.1", config)
    assert not transport_allowed(None, "192.0.2.9", config)
    config = replace(config, server=replace(config.server,
        tls_terminated_by_proxy=True, trusted_proxy_cidrs=("192.0.2.8/32",)))
    assert not transport_allowed(None, "192.0.2.9", config)
    assert transport_allowed(None, "192.0.2.8", config)


def test_artifact_access_log_omits_raw_url_and_query(capsys):
    handler = object.__new__(serve.Handler)
    handler.path = "/v1/artifacts/private-id?secret=do-not-log"
    handler.log_message('"%s" %s %s', "GET " + handler.path, "403", "-")
    output = capsys.readouterr().err
    assert "private-id" not in output and "do-not-log" not in output


def test_rejected_transfer_does_not_linger_read_socket():
    class Unreadable:
        def recv(self, size):
            raise AssertionError("rejected transfer drained socket")
        def settimeout(self, timeout):
            raise AssertionError("rejected transfer attempted lingering close")
    handler = object.__new__(serve.Handler)
    handler._artifact_transfer_request = True
    handler.close_connection = True
    handler._request_body_consumed = False
    handler.connection = Unreadable()
    handler.wfile = io.BytesIO()
    handler._linger_before_close()


def test_artifact_cors_does_not_log_header(receiver, monkeypatch, caplog):
    origin = "https://private-origin.example"
    current = receiver[1]
    current[0] = replace(current[0], server=replace(current[0].server, cors_origins=(origin,)))
    monkeypatch.setattr(serve, "CORS_ORIGINS", frozenset((origin,)))
    caplog.set_level("DEBUG", logger=serve.__name__)
    assert request(receiver, "GET", "/v1/artifact-transfers/" + "a" * 32, headers={"Origin": origin})[0] == 404
    assert origin not in caplog.text


@pytest.mark.parametrize("body,headers,status", [
    (b'{"command_id":"one","command_id":"two"}', {"Content-Type": "application/json"}, 400),
    (b'{"value":NaN}', {"Content-Type": "application/json"}, 400),
    (b'{}', {"Content-Type": "text/plain"}, 415),
    (b'{}', {"Transfer-Encoding": "chunked", "Content-Type": "application/json"}, 400),
])
def test_control_body_parsing_is_strict(receiver, body, headers, status):
    assert request(receiver, "POST", "/v1/artifact-transfers", raw=body, headers=headers)[0] == status


def test_duplicate_authorization_is_rejected(receiver):
    connection = http.client.HTTPConnection(*receiver[0], timeout=10)
    connection.putrequest("GET", "/v1/artifact-transfers/" + "a" * 32)
    auth = "Bearer " + receiver[1][0].secrets.artifact_transfer_key
    connection.putheader("Authorization", auth)
    connection.putheader("Authorization", auth)
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 400
    response.read()
    connection.close()


def test_read_only_binding_cannot_begin(receiver):
    current = receiver[1]
    current[0] = replace(current[0], artifact_transfer=replace(current[0].artifact_transfer, can_write=False))
    assert begin(receiver)[0] == 403


def test_disabled_production_route_does_not_use_global_admin(receiver, monkeypatch):
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", None)
    assert request(receiver, "POST", "/v1/artifact-transfers", {},
                   key=receiver[1][0].secrets.api_key)[0] == 503


def test_revocation_during_async_seal_prevents_publication(receiver, monkeypatch):
    data = b"revoked before seal"
    status, _, body = begin(receiver, data)
    assert status == 200
    identity = json.loads(body)["transfer_id"]
    assert request(receiver, "PUT", f"/v1/artifact-transfers/{identity}/chunks/0", raw=data,
        headers={"Content-Type": "application/octet-stream", "X-Sonder-Chunk-Sha256": hashlib.sha256(data).hexdigest()})[0] == 200
    binding = receiver[2]
    service = binding.service()
    context = binding.authenticate("Bearer " + receiver[1][0].secrets.artifact_transfer_key, correlation_id="inspect-test")
    grant = binding.authorize(context, "read")
    entered, release = threading.Event(), threading.Event()
    original = service.store.seal
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.store, "seal", paused)
    try:
        assert request(receiver, "POST", f"/v1/artifact-transfers/{identity}/seal", {"command_id": "seal-one"})[0] == 202
        assert entered.wait(5)
        receiver[1][0] = replace(receiver[1][0], artifact_transfer=replace(receiver[1][0].artifact_transfer, enabled=False))
    finally:
        release.set()
    service.close()  # Wait for the actual verifier to reach the grant callback.
    receipt = service.store.inspect(identity, grant)
    assert receipt["state"] != "sealed"
    assert "artifact" not in receipt


def test_saturated_transfer_gate_rejects_without_reading(receiver, monkeypatch):
    from sonder_runtime.interfaces.http import artifact_transfer as routes
    gate = threading.BoundedSemaphore(1)
    assert gate.acquire(blocking=False)
    monkeypatch.setattr(routes, "_REQUEST_SLOTS", gate)
    connection = http.client.HTTPConnection(*receiver[0], timeout=5)
    connection.putrequest("POST", "/v1/artifact-transfers")
    connection.putheader("Content-Length", "20")
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Authorization", "Bearer " + receiver[1][0].secrets.artifact_transfer_key)
    connection.endheaders()  # Deliberately never send the declared body.
    response = connection.getresponse()
    assert response.status == 429
    assert response.getheader("Connection") == "close"
    response.read()
    connection.close()
    gate.release()


@pytest.mark.parametrize("failure", ["auth", "parser", "service", "send"])
def test_transfer_slot_released_on_every_error(receiver, monkeypatch, failure):
    from sonder_runtime.interfaces.http import artifact_transfer as routes
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(routes, "_REQUEST_SLOTS", gate)
    handler = object.__new__(serve.Handler)
    handler.path = "/v1/artifact-transfers"
    handler.client_address = ("127.0.0.1", 1234)
    handler.connection = None
    handler.headers = Message()
    raw = b"bad" if failure == "parser" else b'{"spec":{},"command_id":"one"}'
    handler.headers["Content-Length"] = str(len(raw))
    handler.headers["Content-Type"] = "application/json"
    handler.headers["Authorization"] = "Bearer " + ("wrong" if failure == "auth" else receiver[1][0].secrets.artifact_transfer_key)
    handler.rfile = io.BytesIO(raw)
    handler._request_body_consumed = False
    handler._correlation_id = "test"
    if failure == "service":
        monkeypatch.setattr(receiver[2], "service", lambda: (_ for _ in ()).throw(RuntimeError("private detail")))
    if failure == "send":
        handler._send_json_payload = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disconnected"))
        with pytest.raises(OSError):
            routes.handle_artifact_transfer(handler, "POST", receiver[2])
    else:
        replies = []
        handler._send_json_payload = lambda *args, **kwargs: replies.append(kwargs["status"])
        routes.handle_artifact_transfer(handler, "POST", receiver[2])
        assert replies == [{"auth": 401, "parser": 400, "service": 503}[failure]]
    assert gate.acquire(blocking=False)
    gate.release()


def test_raw_upload_respects_smaller_host_body_limit(receiver, monkeypatch):
    monkeypatch.setattr(serve, "MAX_REQUEST_BYTES", 2)
    status, _, _ = request(receiver, "PUT", "/v1/artifact-transfers/" + "a" * 32 + "/chunks/0",
        raw=b"abc", headers={"Content-Type": "application/octet-stream",
                             "X-Sonder-Chunk-Sha256": hashlib.sha256(b"abc").hexdigest()})
    assert status == 413
