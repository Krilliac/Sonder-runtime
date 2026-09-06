from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from sonder_runtime.adapters.cluster.http_control_state import (
    HttpsControlStateProvider,
)
from sonder_runtime.domain.cluster_availability import (
    ControlStateEvent,
    FenceReceipt,
    OwnershipScope,
    PartitionState,
    ReplicatedControlStateCapabilities,
    ReplicationAcknowledgement,
)
from sonder_runtime.domain.common.errors import DependencyUnavailable


class _Response:
    def __init__(self, payload: object, *, status: int = 200):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self._raw


class _Opener:
    def __init__(self, payload: object, *, status: int = 200):
        self.payload = payload
        self.status = status
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request, *, timeout: float):
        self.calls.append((request, timeout))
        return _Response(self.payload, status=self.status)


def _capabilities(**changes) -> ReplicatedControlStateCapabilities:
    values = dict(
        provider_id="provider-1",
        data_replica_ids=("node-a", "node-b"),
        witness_ids=("witness-a",),
        durable_acknowledgements=True,
        external_fencing=True,
        partition_policy=PartitionState.SAFE,
    )
    values.update(changes)
    return ReplicatedControlStateCapabilities(**values)


def _event(**changes) -> ControlStateEvent:
    values = dict(
        event_id="event-1",
        cluster_id="cluster-a",
        resource_kind="job",
        resource_id="job-1",
        owner_id="node-a",
        owner_epoch=3,
        sequence=9,
        payload_digest="a" * 64,
    )
    values.update(changes)
    return ControlStateEvent(**values)


def _ack(event: ControlStateEvent, **changes) -> ReplicationAcknowledgement:
    values = dict(
        event_id=event.event_id,
        cluster_id=event.cluster_id,
        owner_epoch=event.owner_epoch,
        sequence=event.sequence,
        provider_id="provider-1",
        protocol_version=event.protocol_version,
        data_replica_ids=("node-a", "node-b"),
        witness_ids=("witness-a",),
        durable=True,
    )
    values.update(changes)
    return ReplicationAcknowledgement(**values)


def _fence(scope: OwnershipScope, **changes) -> FenceReceipt:
    values = dict(
        receipt_id="fence-1",
        cluster_id=scope.cluster_id,
        resource_kind=scope.resource_kind,
        resource_id=scope.resource_id,
        previous_owner_id=scope.owner_id,
        previous_owner_epoch=scope.epoch,
        provider_id="provider-1",
        protocol_version=1,
        partition_state=PartitionState.SAFE,
        external=True,
        accepted=True,
    )
    values.update(changes)
    return FenceReceipt(**values)


def _provider(opener: _Opener) -> HttpsControlStateProvider:
    return HttpsControlStateProvider(
        origin="https://control.example.test:9443",
        api_key="key-1",
        capabilities=_capabilities(),
        opener=opener,
    )


def test_append_sends_exact_event_and_accepts_matching_durable_acknowledgement():
    event = _event()
    ack = _ack(event)
    opener = _Opener(
        {"object": "replication_acknowledgement", "acknowledgement": ack.as_dict()}
    )

    result = _provider(opener).append(event)

    assert result == ack
    request, timeout = opener.calls[0]
    assert timeout == 5.0
    assert request.get_method() == "POST"
    assert request.full_url == "https://control.example.test:9443/v1/control-state/events"
    assert request.get_header("Authorization") == "Bearer key-1"
    assert request.get_header("X-sonder-control-state-provider") == "provider-1"
    body = json.loads(request.data.decode("utf-8"))
    assert body == {"object": "control_state_event", "event": event.as_dict()}


def test_append_rejects_mismatched_or_non_durable_receipts():
    event = _event()
    for ack in (
        _ack(event, event_id="other-event"),
        _ack(event, provider_id="other-provider"),
        _ack(event, durable=False),
    ):
        opener = _Opener(
            {"object": "replication_acknowledgement", "acknowledgement": ack.as_dict()}
        )
        with pytest.raises(DependencyUnavailable):
            _provider(opener).append(event)


def test_read_returns_bounded_ordered_events_and_binds_query():
    first = _event(sequence=10, event_id="event-10")
    second = _event(sequence=11, event_id="event-11")
    opener = _Opener(
        {"object": "control_state_events", "events": [first.as_dict(), second.as_dict()]}
    )

    result = _provider(opener).read("cluster-a", after_sequence=9, limit=4)

    assert result == (first, second)
    request, _ = opener.calls[0]
    parsed = urlsplit(request.full_url)
    assert parsed.path == "/v1/control-state/events"
    assert parsed.query == "cluster_id=cluster-a&after_sequence=9&limit=4"


def test_read_rejects_out_of_order_or_wrong_cluster_events():
    first = _event(sequence=10, event_id="event-10")
    for events in (
        [first, _event(sequence=10, event_id="event-duplicate")],
        [_event(cluster_id="other-cluster", sequence=10, event_id="event-other")],
    ):
        opener = _Opener(
            {"object": "control_state_events", "events": [item.as_dict() for item in events]}
        )
        with pytest.raises(DependencyUnavailable):
            _provider(opener).read("cluster-a", after_sequence=9)


def test_fence_binds_the_exact_scope_and_returns_denial_without_promoting():
    scope = OwnershipScope("cluster-a", "job", "job-1", "node-a", 3)
    receipt = _fence(scope, accepted=False)
    opener = _Opener({"object": "fence_receipt", "receipt": receipt.as_dict()})

    result = _provider(opener).fence(scope)

    assert result == receipt
    request, _ = opener.calls[0]
    assert request.full_url == "https://control.example.test:9443/v1/control-state/fence"
    assert json.loads(request.data.decode("utf-8")) == {
        "object": "owner_fence_request",
        "ownership": scope.as_dict(),
    }


def test_fence_rejects_receipt_for_another_scope():
    scope = OwnershipScope("cluster-a", "job", "job-1", "node-a", 3)
    receipt = _fence(scope, resource_id="job-2")
    opener = _Opener({"object": "fence_receipt", "receipt": receipt.as_dict()})
    with pytest.raises(DependencyUnavailable):
        _provider(opener).fence(scope)


def test_fence_rejects_a_local_receipt_from_the_external_provider():
    scope = OwnershipScope("cluster-a", "job", "job-1", "node-a", 3)
    receipt = _fence(scope, external=False)
    opener = _Opener({"object": "fence_receipt", "receipt": receipt.as_dict()})
    with pytest.raises(DependencyUnavailable):
        _provider(opener).fence(scope)


@pytest.mark.parametrize(
    "origin",
    [
        "http://control.example.test:8080",
        "https://user:pass@control.example.test:9443",
        "https://control.example.test:9443/path",
        "https://control.example.test:9443?secret=value",
    ],
)
def test_remote_provider_origin_is_tls_and_origin_only(origin):
    with pytest.raises(ValueError):
        HttpsControlStateProvider(
            origin=origin,
            api_key="key-1",
            capabilities=_capabilities(),
            opener=_Opener({}),
        )


def test_plain_http_is_only_allowed_for_explicit_loopback_test_endpoint():
    provider = HttpsControlStateProvider(
        origin="http://127.0.0.1:8080",
        api_key="key-1",
        capabilities=_capabilities(),
        allow_insecure_loopback=True,
        opener=_Opener({"object": "control_state_events", "events": []}),
    )
    assert provider.read("cluster-a") == ()


def test_redirect_status_and_oversized_response_fail_closed():
    event = _event()
    ack = _ack(event)
    for opener in (
        _Opener({}, status=302),
        _Opener({"object": "replication_acknowledgement", "acknowledgement": ack.as_dict()}, status=500),
    ):
        with pytest.raises(DependencyUnavailable):
            _provider(opener).append(event)

    class _HugeResponse(_Response):
        def read(self, limit: int) -> bytes:
            return b"x" * (limit + 1)

    class _HugeOpener(_Opener):
        def __call__(self, request, *, timeout: float):
            self.calls.append((request, timeout))
            return _HugeResponse({})

    with pytest.raises(DependencyUnavailable):
        _provider(_HugeOpener({})).append(event)


def test_non_bytes_response_fails_closed():
    class _TextResponse(_Response):
        def read(self, limit: int):
            return "{}"

    class _TextOpener(_Opener):
        def __call__(self, request, *, timeout: float):
            self.calls.append((request, timeout))
            return _TextResponse({})

    with pytest.raises(DependencyUnavailable):
        _provider(_TextOpener({})).append(_event())


def test_domain_transport_shapes_are_immutable_and_round_trip():
    event = _event()
    scope = event.scope
    assert ControlStateEvent.from_dict(event.as_dict()) == event
    assert ReplicationAcknowledgement.from_dict(_ack(event).as_dict()) == _ack(event)
    assert FenceReceipt.from_dict(_fence(scope).as_dict()) == _fence(scope)
    with pytest.raises(ValueError):
        ReplicationAcknowledgement.from_dict({**_ack(event).as_dict(), "extra": True})
    with pytest.raises(ValueError):
        ReplicationAcknowledgement.from_dict(
            {**_ack(event).as_dict(), "data_replica_ids": "node-a"}
        )
