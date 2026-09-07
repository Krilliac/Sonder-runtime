"""Fixed-peer mobility transport: pins before bearer or artifact bytes."""

from dataclasses import replace
import hashlib
import json

import pytest

from sonder_runtime.application.artifacts.transfer import (
    TransferError,
    recipient_mobility_attestation,
)
from sonder_runtime.application.artifacts import mobility
from sonder_runtime.application.artifacts.mobility import MobilityJournalError
from sonder_runtime.platform.artifact_mobility_config import ArtifactMobilityConfig
from sonder_runtime.platform.artifact_mobility_source_config import (
    ArtifactMobilitySourceConfig,
)
from sonder_runtime.platform.config import Secrets, SonderConfig


_CERTIFICATE = b"synthetic peer leaf certificate"
_CAPABILITY = "c" * 64
_TRANSFER_ID = "a" * 32
_CHUNK = b"m" * 65536


class _Response:
    def __init__(self, value, *, status=200):
        self.status = status
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.body))}

    def read(self, limit):
        assert limit >= len(self.body)
        return self.body


class _Socket:
    def __init__(self, certificate):
        self.certificate = certificate

    def getpeercert(self, *, binary_form=False):
        assert binary_form is True
        return self.certificate


class _Connection:
    def __init__(self, events, response, certificate):
        self.events = events
        self.response = response
        self.sock = _Socket(certificate)

    def connect(self):
        self.events.append("connect")

    def request(self, method, path, body=None, headers=None):
        self.events.append(("request", method, path, body, dict(headers or {})))

    def getresponse(self):
        self.events.append("response")
        return self.response

    def close(self):
        self.events.append("close")


class _Connections:
    def __init__(self, responses, *, certificate=_CERTIFICATE):
        self.responses = list(responses)
        self.certificate = certificate
        self.events = []
        self.calls = []

    def __call__(self, host, port, timeout, context):
        self.calls.append((host, port, timeout, context))
        return _Connection(self.events, self.responses.pop(0), self.certificate)

    @property
    def requests(self):
        return [event for event in self.events if isinstance(event, tuple)]


class _RequestFences:
    """Probe the exact target sequence consumed by a real private fence."""

    def __init__(self, expected, *, fail_at=None):
        self.expected = tuple(expected)
        self.fail_at = fail_at
        self.calls = []

    def before_request(self, method, path):
        assert len(self.calls) < len(self.expected)
        assert (method, path) == self.expected[len(self.calls)]
        self.calls.append((method, path))
        if self.fail_at == len(self.calls) - 1:
            raise MobilityJournalError("LEASE_LOST")


_ALLOW_REQUEST_FENCES = object()


class _TestFenceRepository:
    """Minimal lease store that records every proof made by the real fence."""

    def __init__(self, probe, *, start=0):
        self._probe = probe
        self._index = start
        self.assertions = 0
        self.renewals = 0

    def assert_current_lease(self, lease, *, lock, now):
        method, path = self._probe.expected[self._index]
        self._probe.before_request(method, path)
        self._index += 1
        self.assertions += 1

    def renew_dispatch(self, lease, *, lock, now, lease_seconds):
        self.renewals += 1
        return lease


class _FenceClock:
    """Deterministic clock for a request-fence time-crossing regression."""

    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class _AssertionCrossingFenceRepository:
    """Advance real time after the second assertion, before renewal starts."""

    def __init__(self, clock):
        self._clock = clock
        self.assertion_times = []
        self.renewal_times = []
        self._assertion_count = 0

    def assert_current_lease(self, lease, *, lock, now):
        self.assertion_times.append(now)
        self._assertion_count += 1
        if self._assertion_count == 2:
            self._clock.value = 1003.0

    def renew_dispatch(self, lease, *, lock, now, lease_seconds):
        self.renewal_times.append(now)
        if now >= lease.expires_at:
            raise MobilityJournalError("LEASE_LOST")
        return replace(lease, expires_at=now + lease_seconds)


class _RenewalCrossingFenceRepository:
    """Advance real time after a successful renewal returns its new lease."""

    def __init__(self, clock):
        self._clock = clock
        self.assertion_times = []
        self.renewal_times = []

    def assert_current_lease(self, lease, *, lock, now):
        self.assertion_times.append(now)

    def renew_dispatch(self, lease, *, lock, now, lease_seconds):
        self.renewal_times.append(now)
        if now >= lease.expires_at:
            raise MobilityJournalError("LEASE_LOST")
        renewed = replace(lease, expires_at=now + lease_seconds)
        self._clock.value = 1003.0
        return renewed


def _test_attempt_fences(targets, request_fences):
    expected = tuple(targets)
    if isinstance(request_fences, _RequestFences):
        start = len(request_fences.calls)
        assert request_fences.expected[start : start + len(expected)] == expected
        probe = request_fences
    else:
        start = 0
        probe = _RequestFences(expected)
    repository = _TestFenceRepository(probe, start=start)
    fences = mobility._AttemptRequestFences(
        repository=repository,
        lease=mobility.DispatchLease(
            operation_id="f" * 32,
            source_owner_id="source-a",
            epoch=1,
            token="e" * 64,
            expires_at=1002.0,
        ),
        lock=object(),
        now=lambda: 1000.0,
        lease_seconds=2,
        targets=expected,
    )
    return fences


class _FencedPeer:
    """Test facade that enters the real peer's private fence scope."""

    def __init__(self, peer):
        self._peer = peer

    def _call(self, targets, method, *args, request_fences=None):
        fences = _test_attempt_fences(
            targets, request_fences or _ALLOW_REQUEST_FENCES
        )
        try:
            with self._peer._request_fence_scope(fences):
                result = method(*args)
                fences.finish()
            return result
        except BaseException:
            fences.close()
            raise

    def recipient_attestation(self, spec, *, request_fences=None):
        return self._call(
            [("GET", "/v1/artifact-transfers/recipient-attestation")],
            self._peer.recipient_attestation,
            spec,
            request_fences=request_fences,
        )

    def begin(self, spec, command_id, receipt_capability, *, request_fences=None):
        return self._call(
            [
                ("GET", "/v1/artifact-transfers/recipient-attestation"),
                ("POST", "/v1/artifact-transfers"),
            ],
            self._peer.begin,
            spec,
            command_id,
            receipt_capability,
            request_fences=request_fences,
        )

    def inspect_receipt(
        self, transfer_id, command_id, spec, receipt_capability, *, request_fences=None
    ):
        return self._call(
            [
                ("GET", "/v1/artifact-transfers/recipient-attestation"),
                (
                    "POST",
                    "/v1/artifact-transfers/"
                    + transfer_id
                    + "/mobility-receipt",
                ),
            ],
            self._peer.inspect_receipt,
            transfer_id,
            command_id,
            spec,
            receipt_capability,
            request_fences=request_fences,
        )

    def append(
        self, envelope, immutable_spec, body, receipt_capability, *, request_fences=None
    ):
        receipt = envelope["receipt"]
        return self._call(
            [
                ("GET", "/v1/artifact-transfers/recipient-attestation"),
                (
                    "PUT",
                    "/v1/artifact-transfers/"
                    + receipt["transfer_id"]
                    + "/chunks/"
                    + str(receipt["offset"]),
                ),
            ],
            self._peer.append,
            envelope,
            immutable_spec,
            body,
            receipt_capability,
            request_fences=request_fences,
        )

    def seal(
        self,
        envelope,
        immutable_spec,
        seal_command_id,
        receipt_capability,
        *,
        request_fences=None,
    ):
        return self._call(
            [
                ("GET", "/v1/artifact-transfers/recipient-attestation"),
                (
                    "POST",
                    "/v1/artifact-transfers/"
                    + envelope["receipt"]["transfer_id"]
                    + "/seal",
                ),
            ],
            self._peer.seal,
            envelope,
            immutable_spec,
            seal_command_id,
            receipt_capability,
            request_fences=request_fences,
        )


def _attestation(*, receiver="receiver-a", owner="source-a", grant="grant-a", maximum=65536):
    return recipient_mobility_attestation(
        {
            "protocol_version": "mobility-v1",
            "receiver_identity_id": receiver,
            "principal_id": "principal-a",
            "project_id": "project-a",
            "authorized_source_owner_id": owner,
            "grant_id": grant,
            "grant_revision": 1,
            "can_write": True,
            "max_object_bytes": maximum,
        }
    )


def _config(tmp_path, *, attestation=None, certificate=_CERTIFICATE):
    source = ArtifactMobilitySourceConfig(
        enabled=True,
        store_dir=str(tmp_path / "private-source"),
        principal_id="principal-a",
        project_id="project-a",
        source_owner_id="source-a",
        max_object_bytes=65536,
        total_bytes=131072,
    )
    attestation = attestation or _attestation()
    return SonderConfig(
        secrets=Secrets(artifact_mobility_peer_key="peer-" + "x" * 32),
        artifact_mobility_source=source,
        artifact_mobility=ArtifactMobilityConfig(
            enabled=True,
            destination_label="receiver-b",
            destination_origin="https://receiver.example.test:9443",
            destination_tls_certificate_sha256=hashlib.sha256(certificate).hexdigest(),
            expected_recipient_attestation_sha256=attestation["sha256"],
            destination_credential_id="receiver-b-key-v1",
            max_object_bytes=65536,
        ),
    ), attestation


def _spec(data=_CHUNK):
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": "application/octet-stream",
    }


def _envelope(attestation, spec, *, transfer_id=_TRANSFER_ID, state="open", offset=0):
    receipt = {
        "transfer_id": transfer_id,
        "state": state,
        "offset": offset,
        "chunk_bytes": 65536,
        "expires_at": 2_000_000_000.0,
        "revision": 1,
    }
    if state == "sealed":
        receipt["artifact"] = {"artifact_id": transfer_id, **spec}
    return {
        "protocol_version": "mobility-v1",
        "recipient_attestation": attestation,
        "command_id": "mobility-v1.command-a",
        "spec": spec,
        "receipt": receipt,
    }


def _configured_peer(config, connections, credential_provider=None):
    from sonder_runtime.adapters.compute_fabric.artifact_mobility import (
        ConfiguredArtifactMobilityPeer,
    )

    return ConfiguredArtifactMobilityPeer(
        config,
        credential_provider=credential_provider or (lambda _credential_id: "receiver-" + "k" * 32),
        connection_factory=connections,
    )


def _peer(config, connections, credential_provider=None):
    return _FencedPeer(
        _configured_peer(config, connections, credential_provider=credential_provider)
    )


def test_pinned_connection_validates_leaf_before_credential_or_http_request(tmp_path, monkeypatch):
    config, attestation = _config(tmp_path)
    connections = _Connections([_Response(attestation)], certificate=b"wrong leaf")
    supplied = []
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.invalid:9443")
    peer = _peer(
        config,
        connections,
        credential_provider=lambda credential_id: supplied.append(credential_id) or "receiver-" + "k" * 32,
    )

    with pytest.raises(TransferError, match="MOBILITY_TLS"):
        peer.recipient_attestation(_spec(), request_fences=_ALLOW_REQUEST_FENCES)

    assert supplied == []
    assert connections.requests == []
    assert connections.calls[0][0:2] == ("receiver.example.test", 9443)


def test_configured_peer_keeps_request_fences_out_of_public_methods(tmp_path):
    config, attestation = _config(tmp_path)
    connections = _Connections([_Response(attestation)])
    peer = _configured_peer(config, connections)

    class _ForgedRequestFences(mobility._ArtifactMobilityPeerRequestFences):
        def before_request(self, method, path):
            raise AssertionError("a forged callback must not be installed")

    with pytest.raises(TransferError, match="MOBILITY_ENVELOPE"):
        peer.recipient_attestation(_spec())
    with pytest.raises(TypeError):
        peer.recipient_attestation(_spec(), request_fences=_ALLOW_REQUEST_FENCES)
    with pytest.raises(TransferError, match="MOBILITY_ENVELOPE"):
        peer._request_fence_scope(object())
    with pytest.raises(TransferError, match="MOBILITY_ENVELOPE"):
        peer._request_fence_scope(_ForgedRequestFences())

    assert connections.requests == []


def test_peer_uses_pinned_attestation_envelopes_and_capability_across_lifecycle(tmp_path):
    config, attestation = _config(tmp_path)
    spec = _spec()
    opening = _envelope(attestation, spec)
    verifying = _envelope(attestation, spec, state="verifying", offset=len(_CHUNK))
    sealed = _envelope(attestation, spec, state="sealed", offset=len(_CHUNK))
    connections = _Connections(
        [
            _Response(attestation),
            _Response(opening),
            _Response(attestation),
            _Response(
                {
                    "offset": 0,
                    "next_offset": len(_CHUNK),
                    "chunk_sha256": spec["sha256"],
                    "revision": 2,
                }
            ),
            _Response(attestation),
            _Response(verifying, status=202),
            _Response(attestation),
            _Response(sealed),
        ]
    )
    peer = _peer(config, connections)

    envelope = peer.begin(
        spec,
        "mobility-v1.command-a",
        _CAPABILITY,
        request_fences=_ALLOW_REQUEST_FENCES,
    )
    ack = peer.append(
        envelope, spec, _CHUNK, _CAPABILITY, request_fences=_ALLOW_REQUEST_FENCES
    )
    pending = peer.seal(
        envelope,
        spec,
        "seal-a",
        _CAPABILITY,
        request_fences=_ALLOW_REQUEST_FENCES,
    )
    final = peer.inspect_receipt(
        _TRANSFER_ID,
        "mobility-v1.command-a",
        spec,
        _CAPABILITY,
        request_fences=_ALLOW_REQUEST_FENCES,
    )

    assert envelope == opening
    assert ack["next_offset"] == len(_CHUNK)
    assert pending == verifying
    assert final == sealed
    requests = connections.requests
    assert [request[2] for request in requests] == [
        "/v1/artifact-transfers/recipient-attestation",
        "/v1/artifact-transfers",
        "/v1/artifact-transfers/recipient-attestation",
        f"/v1/artifact-transfers/{_TRANSFER_ID}/chunks/0",
        "/v1/artifact-transfers/recipient-attestation",
        f"/v1/artifact-transfers/{_TRANSFER_ID}/seal",
        "/v1/artifact-transfers/recipient-attestation",
        f"/v1/artifact-transfers/{_TRANSFER_ID}/mobility-receipt",
    ]
    for _, _, path, _, headers in requests:
        if path.endswith("recipient-attestation"):
            continue
        assert headers["X-Sonder-Artifact-Mobility-Version"] == "mobility-v1"
        assert headers["X-Sonder-Artifact-Mobility-Receipt-Capability"] == _CAPABILITY


def test_configured_peer_consumes_a_target_pinned_fence_for_each_real_request(tmp_path):
    config, attestation = _config(tmp_path)
    spec = _spec()
    opening = _envelope(attestation, spec)
    verifying = _envelope(attestation, spec, state="verifying", offset=len(_CHUNK))
    sealed = _envelope(attestation, spec, state="sealed", offset=len(_CHUNK))
    connections = _Connections(
        [
            _Response(attestation),
            _Response(opening),
            _Response(attestation),
            _Response(
                {
                    "offset": 0,
                    "next_offset": len(_CHUNK),
                    "chunk_sha256": spec["sha256"],
                    "revision": 2,
                }
            ),
            _Response(attestation),
            _Response(verifying, status=202),
            _Response(attestation),
            _Response(sealed),
        ]
    )
    expected = [
        ("GET", "/v1/artifact-transfers/recipient-attestation"),
        ("POST", "/v1/artifact-transfers"),
        ("GET", "/v1/artifact-transfers/recipient-attestation"),
        ("PUT", f"/v1/artifact-transfers/{_TRANSFER_ID}/chunks/0"),
        ("GET", "/v1/artifact-transfers/recipient-attestation"),
        ("POST", f"/v1/artifact-transfers/{_TRANSFER_ID}/seal"),
        ("GET", "/v1/artifact-transfers/recipient-attestation"),
        ("POST", f"/v1/artifact-transfers/{_TRANSFER_ID}/mobility-receipt"),
    ]
    request_fences = _RequestFences(expected)
    peer = _peer(config, connections)

    envelope = peer.begin(
        spec, "mobility-v1.command-a", _CAPABILITY, request_fences=request_fences
    )
    peer.append(envelope, spec, _CHUNK, _CAPABILITY, request_fences=request_fences)
    peer.seal(
        envelope, spec, "seal-a", _CAPABILITY, request_fences=request_fences
    )
    peer.inspect_receipt(
        _TRANSFER_ID,
        "mobility-v1.command-a",
        spec,
        _CAPABILITY,
        request_fences=request_fences,
    )

    assert request_fences.calls == expected
    assert [(request[1], request[2]) for request in connections.requests] == expected


@pytest.mark.parametrize("operation", ("begin", "append", "seal", "inspect"))
def test_nested_attestation_expiry_stops_before_later_configured_peer_request(
    tmp_path, operation
):
    config, attestation = _config(tmp_path)
    spec = _spec()
    envelope = _envelope(attestation, spec)
    paths = {
        "begin": ("POST", "/v1/artifact-transfers"),
        "append": ("PUT", f"/v1/artifact-transfers/{_TRANSFER_ID}/chunks/0"),
        "seal": ("POST", f"/v1/artifact-transfers/{_TRANSFER_ID}/seal"),
        "inspect": (
            "POST",
            f"/v1/artifact-transfers/{_TRANSFER_ID}/mobility-receipt",
        ),
    }
    expected = [
        ("GET", "/v1/artifact-transfers/recipient-attestation"),
        paths[operation],
    ]
    request_fences = _RequestFences(expected, fail_at=1)
    connections = _Connections([_Response(attestation)])
    peer = _peer(config, connections)

    with pytest.raises(MobilityJournalError, match="LEASE_LOST"):
        if operation == "begin":
            peer.begin(
                spec,
                "mobility-v1.command-a",
                _CAPABILITY,
                request_fences=request_fences,
            )
        elif operation == "append":
            peer.append(
                envelope,
                spec,
                _CHUNK,
                _CAPABILITY,
                request_fences=request_fences,
            )
        elif operation == "seal":
            peer.seal(
                envelope,
                spec,
                "seal-a",
                _CAPABILITY,
                request_fences=request_fences,
            )
        else:
            peer.inspect_receipt(
                _TRANSFER_ID,
                "mobility-v1.command-a",
                spec,
                _CAPABILITY,
                request_fences=request_fences,
            )

    assert request_fences.calls == expected
    assert [(request[1], request[2]) for request in connections.requests] == [
        expected[0]
    ]
    assert [request[3] for request in connections.requests] == [None]


def test_fresh_renewal_time_blocks_configured_begin_after_assertion_crosses_expiry(
    tmp_path,
):
    """The later nested POST must not use the assertion's stale timestamp."""
    config, attestation = _config(tmp_path)
    spec = _spec()
    opening = _envelope(attestation, spec)
    connections = _Connections([_Response(attestation), _Response(opening)])
    peer = _configured_peer(config, connections)
    clock = _FenceClock()
    repository = _AssertionCrossingFenceRepository(clock)
    fences = mobility._AttemptRequestFences(
        repository=repository,
        lease=mobility.DispatchLease(
            operation_id="f" * 32,
            source_owner_id="source-a",
            epoch=1,
            token="e" * 64,
            expires_at=1002.0,
        ),
        lock=object(),
        now=clock,
        lease_seconds=2,
        targets=(
            ("GET", "/v1/artifact-transfers/recipient-attestation"),
            ("POST", "/v1/artifact-transfers"),
        ),
    )

    try:
        with pytest.raises(MobilityJournalError, match="LEASE_LOST"):
            with peer._request_fence_scope(fences):
                peer.begin(spec, "mobility-v1.command-a", _CAPABILITY)
    finally:
        fences.close()

    assert repository.assertion_times == [1000.0, 1000.0]
    assert repository.renewal_times == [1000.0, 1003.0]
    assert [
        (request[1], request[2], request[3]) for request in connections.requests
    ] == [("GET", "/v1/artifact-transfers/recipient-attestation", None)]


def test_post_renewal_time_blocks_configured_transport_after_renewal_crosses_expiry(
    tmp_path,
):
    """A renewal that blocks past its returned expiry cannot start a request."""
    config, attestation = _config(tmp_path)
    connections = _Connections([_Response(attestation)])
    peer = _configured_peer(config, connections)
    clock = _FenceClock()
    repository = _RenewalCrossingFenceRepository(clock)
    fences = mobility._AttemptRequestFences(
        repository=repository,
        lease=mobility.DispatchLease(
            operation_id="f" * 32,
            source_owner_id="source-a",
            epoch=1,
            token="e" * 64,
            expires_at=1002.0,
        ),
        lock=object(),
        now=clock,
        lease_seconds=2,
        targets=(("GET", "/v1/artifact-transfers/recipient-attestation"),),
    )

    try:
        with pytest.raises(MobilityJournalError, match="LEASE_LOST"):
            with peer._request_fence_scope(fences):
                peer.recipient_attestation(_spec())
    finally:
        fences.close()

    assert repository.assertion_times == [1000.0]
    assert repository.renewal_times == [1000.0]
    assert connections.requests == []


@pytest.mark.parametrize(
    ("status", "receiver_code", "expected"),
    (
        (503, "UNAVAILABLE", "MOBILITY_UNAVAILABLE"),
        (429, "BUSY", "MOBILITY_BUSY"),
    ),
)
def test_retryable_receiver_preflight_statuses_are_constrained_and_byte_free(
    tmp_path, status, receiver_code, expected
):
    config, _attestation_value = _config(tmp_path)
    connections = _Connections(
        [_Response({"error": {"code": receiver_code}}, status=status)]
    )
    peer = _peer(config, connections)

    with pytest.raises(mobility.MobilityPeerAvailabilityError, match=expected):
        peer.recipient_attestation(_spec(), request_fences=_ALLOW_REQUEST_FENCES)

    assert [(request[1], request[2]) for request in connections.requests] == [
        ("GET", "/v1/artifact-transfers/recipient-attestation")
    ]
    assert [request[3] for request in connections.requests] == [None]


@pytest.mark.parametrize(
    ("status", "body"),
    (
        (503, {"error": {"code": "UNKNOWN"}}),
        (429, {"error": {"code": "UNKNOWN"}}),
        (503, b"not-json"),
        (429, {"error": []}),
    ),
)
def test_unknown_or_malformed_receiver_availability_stays_redacted_and_terminal(
    tmp_path, status, body
):
    config, _attestation_value = _config(tmp_path)
    connections = _Connections([_Response(body, status=status)])
    peer = _peer(config, connections)

    with pytest.raises(TransferError, match="MOBILITY_PEER_STATUS") as raised:
        peer.recipient_attestation(_spec(), request_fences=_ALLOW_REQUEST_FENCES)

    assert "UNKNOWN" not in str(raised.value)
    assert [(request[1], request[2], request[3]) for request in connections.requests] == [
        ("GET", "/v1/artifact-transfers/recipient-attestation", None)
    ]


@pytest.mark.parametrize(
    "attestation",
    (
        _attestation(owner="other-source"),
        _attestation(receiver="receiver-b"),
        _attestation(grant="grant-b"),
        _attestation(maximum=1),
    ),
)
def test_attestation_mismatch_stops_before_begin_or_bytes(tmp_path, attestation):
    config, expected = _config(tmp_path)
    connections = _Connections([_Response(attestation)])
    peer = _peer(config, connections)

    with pytest.raises(TransferError, match="MOBILITY_(ATTESTATION|LIMIT)"):
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert expected != attestation or attestation["max_object_bytes"] == 1
    assert [request[2] for request in connections.requests] == [
        "/v1/artifact-transfers/recipient-attestation"
    ]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda envelope: envelope.update(protocol_version="mobility-v2"),
        lambda envelope: envelope.update(command_id="wrong-command"),
        lambda envelope: envelope["spec"].update(size_bytes=1),
        lambda envelope: envelope["receipt"].update(transfer_id="not-an-id"),
        lambda envelope: envelope["receipt"].update(offset=65537),
        lambda envelope: envelope["receipt"].update(chunk_bytes=1),
        lambda envelope: envelope["receipt"].update(expires_at=float("inf")),
        lambda envelope: envelope.update(receipt_capability=_CAPABILITY),
    ),
)
def test_invalid_begin_envelope_stops_before_any_append(tmp_path, mutate):
    config, attestation = _config(tmp_path)
    envelope = _envelope(attestation, _spec())
    mutate(envelope)
    connections = _Connections([_Response(attestation), _Response(envelope)])
    peer = _peer(config, connections)

    with pytest.raises(TransferError, match="MOBILITY_ENVELOPE"):
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert [request[2] for request in connections.requests] == [
        "/v1/artifact-transfers/recipient-attestation",
        "/v1/artifact-transfers",
    ]


@pytest.mark.parametrize("code", ("QUOTA", "CAPACITY", "FORBIDDEN"))
def test_dynamic_begin_rejections_are_constrained_and_byte_free(tmp_path, code):
    config, attestation = _config(tmp_path)
    connections = _Connections(
        [_Response(attestation), _Response({"error": {"code": code}}, status=429)]
    )
    peer = _peer(config, connections)

    with pytest.raises(TransferError, match=code):
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert all("/chunks/" not in request[2] for request in connections.requests)


def test_changed_attestation_before_append_stops_before_body(tmp_path):
    config, attestation = _config(tmp_path)
    opening = _envelope(attestation, _spec())
    changed = _attestation(grant="grant-b")
    connections = _Connections(
        [_Response(attestation), _Response(opening), _Response(changed)]
    )
    peer = _peer(config, connections)
    envelope = peer.begin(
        _spec(),
        "mobility-v1.command-a",
        _CAPABILITY,
        request_fences=_ALLOW_REQUEST_FENCES,
    )

    with pytest.raises(TransferError, match="MOBILITY_ATTESTATION"):
        peer.append(
            envelope,
            _spec(),
            _CHUNK,
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert [request[2] for request in connections.requests] == [
        "/v1/artifact-transfers/recipient-attestation",
        "/v1/artifact-transfers",
        "/v1/artifact-transfers/recipient-attestation",
    ]


def test_caller_mutated_envelope_spec_stops_before_chunk_request(tmp_path):
    config, attestation = _config(tmp_path)
    spec = _spec()
    opening = _envelope(attestation, spec)
    connections = _Connections(
        [_Response(attestation), _Response(opening), _Response(attestation)]
    )
    peer = _peer(config, connections)
    envelope = peer.begin(
        spec,
        "mobility-v1.command-a",
        _CAPABILITY,
        request_fences=_ALLOW_REQUEST_FENCES,
    )
    envelope["spec"]["size_bytes"] = 1

    with pytest.raises(TransferError, match="MOBILITY_ENVELOPE"):
        peer.append(
            envelope,
            spec,
            _CHUNK,
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert [request[2] for request in connections.requests] == [
        "/v1/artifact-transfers/recipient-attestation",
        "/v1/artifact-transfers",
        "/v1/artifact-transfers/recipient-attestation",
    ]


@pytest.mark.parametrize(
    "changed",
    (_attestation(receiver="receiver-b"), _attestation(grant="grant-b")),
)
def test_same_origin_peer_identity_or_grant_rotation_stops_before_append(
    tmp_path, changed
):
    config, attestation = _config(tmp_path)
    opening = _envelope(attestation, _spec())
    connections = _Connections(
        [_Response(attestation), _Response(opening), _Response(changed)]
    )
    peer = _peer(config, connections)
    envelope = peer.begin(
        _spec(),
        "mobility-v1.command-a",
        _CAPABILITY,
        request_fences=_ALLOW_REQUEST_FENCES,
    )

    with pytest.raises(TransferError, match="MOBILITY_ATTESTATION"):
        peer.append(
            envelope,
            _spec(),
            _CHUNK,
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert all("/chunks/" not in request[2] for request in connections.requests)


def test_attested_static_limit_rejects_before_begin(tmp_path):
    limited = _attestation(maximum=1)
    config, _ = _config(tmp_path, attestation=limited)
    connections = _Connections([_Response(limited)])
    peer = _peer(config, connections)

    with pytest.raises(TransferError, match="MOBILITY_LIMIT"):
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert [request[2] for request in connections.requests] == [
        "/v1/artifact-transfers/recipient-attestation"
    ]


def test_invalid_receipt_capability_is_rejected_before_any_peer_request(tmp_path):
    config, _ = _config(tmp_path)
    connections = _Connections([])
    peer = _peer(config, connections)

    with pytest.raises(TransferError, match="MOBILITY_ENVELOPE"):
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            "C" * 64,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert connections.calls == []


@pytest.mark.parametrize(
    "credential",
    ("", "a" * 31, "receiver-\nkey", "https://credential.example.invalid/private"),
)
def test_absent_or_malformed_credential_never_reaches_http_or_diagnostics(
    tmp_path, caplog, credential
):
    config, attestation = _config(tmp_path)
    connections = _Connections([_Response(attestation)])
    peer = _peer(config, connections, credential_provider=lambda _identifier: credential)

    with pytest.raises(TransferError, match="MOBILITY_CREDENTIAL") as raised:
        peer.recipient_attestation(_spec(), request_fences=_ALLOW_REQUEST_FENCES)

    if credential:
        assert credential not in str(raised.value)
        assert credential not in repr(raised.value)
        assert credential not in caplog.text
    assert connections.requests == []


def test_length_and_unexpected_transport_failures_are_stable_and_redacted(tmp_path):
    config, attestation = _config(tmp_path)
    malformed_length = _Response(attestation)
    malformed_length.headers["Content-Length"] = "0"
    peer = _peer(config, _Connections([malformed_length]))
    with pytest.raises(TransferError, match="MOBILITY_LENGTH"):
        peer.recipient_attestation(_spec(), request_fences=_ALLOW_REQUEST_FENCES)

    private_detail = "https://receiver.example.invalid/secret"

    def fail_connection(*_args):
        raise RuntimeError(private_detail)

    peer = _peer(config, fail_connection)
    with pytest.raises(TransferError, match="MOBILITY_UNAVAILABLE") as raised:
        peer.recipient_attestation(_spec(), request_fences=_ALLOW_REQUEST_FENCES)
    assert private_detail not in str(raised.value)
    assert private_detail not in repr(raised.value)


def test_unknown_remote_error_payload_is_redacted_to_one_status_code(tmp_path, caplog):
    config, attestation = _config(tmp_path)
    private_detail = "https://receiver.example.invalid/private-error"
    connections = _Connections(
        [
            _Response(attestation),
            _Response({"error": {"code": private_detail}}, status=500),
        ]
    )
    peer = _peer(config, connections)

    with pytest.raises(TransferError, match="MOBILITY_PEER_STATUS") as raised:
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    assert private_detail not in str(raised.value)
    assert private_detail not in repr(raised.value)
    assert private_detail not in caplog.text


def test_malformed_remote_error_shape_has_no_raw_exception(tmp_path):
    config, attestation = _config(tmp_path)
    connections = _Connections(
        [_Response(attestation), _Response({"error": []}, status=500)]
    )
    peer = _peer(config, connections)

    with pytest.raises(TransferError, match="MOBILITY_PEER_STATUS"):
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )


@pytest.mark.parametrize(
    "origin",
    (
        "http://receiver.example.test:9443",
        "https://receiver.example.test:9443/not-root",
        "https://receiver.example.test:9443?query=private",
        "https://receiver.example.test:9443#fragment",
    ),
)
def test_non_root_or_non_https_destination_is_rejected_without_echo(tmp_path, origin):
    config, _ = _config(tmp_path)
    config = replace(
        config,
        artifact_mobility=replace(config.artifact_mobility, destination_origin=origin),
    )
    with pytest.raises(TransferError, match="MOBILITY_CONFIG") as raised:
        _peer(config, _Connections([]))

    assert origin not in str(raised.value)
    assert origin not in repr(raised.value)


def test_redirect_and_url_like_credential_are_redacted(tmp_path, caplog):
    config, attestation = _config(tmp_path)
    redirect = _Connections([_Response(b"", status=302)])
    peer = _peer(config, redirect)
    with pytest.raises(TransferError, match="MOBILITY_REDIRECT"):
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    rejected = "https://credential.example.invalid/private"
    connections = _Connections([_Response(attestation)])
    peer = _peer(config, connections, credential_provider=lambda _identifier: rejected)
    with pytest.raises(TransferError, match="MOBILITY_CREDENTIAL") as raised:
        peer.begin(
            _spec(),
            "mobility-v1.command-a",
            _CAPABILITY,
            request_fences=_ALLOW_REQUEST_FENCES,
        )

    rendered = json.dumps({"error": str(raised.value)})
    assert rejected not in str(raised.value)
    assert rejected not in repr(raised.value)
    assert rejected not in rendered
    assert rejected not in caplog.text
    assert connections.requests == []


def test_config_origin_with_credentials_is_rejected_without_echo(tmp_path):
    config, _ = _config(tmp_path)
    rejected = "https://user:password@receiver.example.test:9443"
    config = replace(
        config,
        artifact_mobility=replace(config.artifact_mobility, destination_origin=rejected),
    )
    connections = _Connections([])

    with pytest.raises(TransferError, match="MOBILITY_CONFIG") as raised:
        _peer(config, connections)

    assert rejected not in str(raised.value)
    assert rejected not in repr(raised.value)
