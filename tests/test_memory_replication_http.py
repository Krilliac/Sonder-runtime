from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
import json

import pytest

from sonder_runtime.adapters.memory_replication.http_client import (
    HttpsMemoryReplicationSink,
)
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicaReceipt,
    MemoryReplicationBatch,
    MemoryReplicationError,
)
from sonder_runtime.interfaces.http.memory_replication import (
    MemoryReplicationReceiver,
    handle_memory_replication,
    is_memory_replication_route,
)


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, limit: int) -> bytes:
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _batch(*, source_id: str = "node-a", sequence: int = 1) -> MemoryReplicationBatch:
    record = MemoryMutation(
        source_id=source_id,
        source_epoch=1,
        sequence=sequence,
        entity_kind="fact",
        entity_id=f"fact-{sequence}",
        version=sequence,
        operation="upsert",
        project="project-a",
        payload={"text": f"fact {sequence}"},
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    return MemoryReplicationBatch(
        source_id=source_id,
        source_epoch=1,
        after_sequence=sequence - 1,
        records=(record,),
        next_sequence=sequence,
        has_more=False,
    )


class _Sink:
    identity = "node-b"

    def __init__(self):
        self.batches = []

    def apply(self, batch: MemoryReplicationBatch) -> MemoryReplicaReceipt:
        self.batches.append(batch)
        return MemoryReplicaReceipt(
            replica_id=self.identity,
            source_id=batch.source_id,
            source_epoch=batch.source_epoch,
            next_sequence=batch.next_sequence,
            batch_digest=batch.digest,
            durable=True,
            inserted_records=len(batch.records),
        )


def _receipt_body(batch: MemoryReplicationBatch) -> bytes:
    receipt = MemoryReplicaReceipt(
        replica_id="node-b",
        source_id=batch.source_id,
        source_epoch=batch.source_epoch,
        next_sequence=batch.next_sequence,
        batch_digest=batch.digest,
        durable=True,
        inserted_records=len(batch.records),
    )
    return json.dumps(
        {"object": "memory_replication_receipt", "receipt": receipt.as_dict()},
        sort_keys=True,
    ).encode()


def test_memory_sink_posts_canonical_batch_with_auth_and_validates_receipt():
    batch = _batch()
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(202, _receipt_body(batch))

    sink = HttpsMemoryReplicationSink(
        identity="node-b",
        origin="https://node-b.example:8443",
        api_key="memory-secret",
        opener=opener,
    )

    receipt = sink.apply(batch)

    assert receipt.replica_id == "node-b"
    assert captured["url"] == "https://node-b.example:8443/v1/memory/replication/batches"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer memory-secret"
    assert captured["body"] == {
        "object": "memory_replication_batch",
        "batch": batch.as_dict(),
    }
    assert captured["timeout"] == 5.0


@pytest.mark.parametrize(
    ("status", "body", "message"),
    (
        (302, b"", "redirect"),
        (202, b"{not-json", "JSON"),
        (202, b"x" * 4097, "size"),
    ),
)
def test_memory_sink_rejects_redirect_invalid_json_and_oversize(status, body, message):
    sink = HttpsMemoryReplicationSink(
        identity="node-b",
        origin="https://node-b.example:8443",
        api_key="memory-secret",
        max_response_bytes=4096,
        opener=lambda *_args, **_kwargs: _Response(status, body),
    )

    with pytest.raises(DependencyUnavailable, match=message):
        sink.apply(_batch())


def test_memory_sink_rejects_tampered_receipt_and_oversized_wire_before_network():
    batch = _batch()
    body = json.loads(_receipt_body(batch))
    body["receipt"]["batch_digest"] = "0" * 64
    sink = HttpsMemoryReplicationSink(
        identity="node-b",
        origin="https://node-b.example:8443",
        api_key="memory-secret",
        opener=lambda *_args, **_kwargs: _Response(200, json.dumps(body).encode()),
    )
    with pytest.raises(DependencyUnavailable, match="digest"):
        sink.apply(batch)

    large = MemoryMutation(
        source_id="node-a",
        source_epoch=1,
        sequence=1,
        entity_kind="fact",
        entity_id="fact-large",
        version=1,
        operation="upsert",
        project="project-a",
        payload={"text": "x" * 65500},
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    large_batch = MemoryReplicationBatch("node-a", 1, 0, (large,), 1, False)
    sink = HttpsMemoryReplicationSink(
        identity="node-b",
        origin="https://node-b.example:8443",
        api_key="memory-secret",
        max_request_bytes=1024,
        opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized batch must fail before transport")
        ),
    )
    with pytest.raises(DependencyUnavailable, match="request bound"):
        sink.apply(large_batch)


def test_receiver_authenticates_source_and_returns_self_authenticating_receipt():
    sink = _Sink()
    receiver = MemoryReplicationReceiver(
        sink,
        api_key="memory-secret",
        accepted_source_ids=("node-a",),
    )
    batch = _batch()

    receipt = receiver.receive("Bearer memory-secret", {
        "object": "memory_replication_batch",
        "batch": batch.as_dict(),
    })

    assert receipt.replica_id == "node-b"
    assert sink.batches == [batch]
    assert MemoryReplicaReceipt.from_dict(receipt.as_dict()) == receipt

    with pytest.raises(PermissionError, match="source"):
        receiver.receive("Bearer memory-secret", {
            "object": "memory_replication_batch",
            "batch": _batch(source_id="untrusted").as_dict(),
        })
    with pytest.raises(PermissionError, match="authentication"):
        receiver.receive("Bearer wrong", {
            "object": "memory_replication_batch",
            "batch": batch.as_dict(),
        })


def test_receiver_rejects_malformed_wire_without_calling_sink():
    sink = _Sink()
    receiver = MemoryReplicationReceiver(
        sink,
        api_key="memory-secret",
        accepted_source_ids=("node-a",),
    )

    with pytest.raises(MemoryReplicationError, match="envelope"):
        receiver.receive("Bearer memory-secret", {"object": "wrong", "batch": {}})
    with pytest.raises(MemoryReplicationError, match="request"):
        receiver.receive("Bearer memory-secret", {
            "object": "memory_replication_batch",
            "batch": _batch().as_dict(),
            "extra": True,
        })
    assert sink.batches == []


def test_memory_replication_route_is_exact_and_bounded():
    assert is_memory_replication_route("/v1/memory/replication/batches")
    assert not is_memory_replication_route("/v1/memory/replication/batches/")
    assert not is_memory_replication_route("/v1/memory/replication/batches?x=1")
    assert not is_memory_replication_route("/v1/memory/replication")


class _Handler:
    def __init__(self, body: bytes, *, authorization: str = "Bearer memory-secret"):
        self.path = "/v1/memory/replication/batches"
        self.headers = Message()
        self.headers["Authorization"] = authorization
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(body))
        self.rfile = BytesIO(body)
        self.responses = []
        self._request_body_consumed = False

    def _send_json_payload(self, payload, *, status, headers):
        self.responses.append((status, payload, headers))


def test_http_adapter_frames_authenticated_batch_and_maps_receiver_result():
    sink = _Sink()
    receiver = MemoryReplicationReceiver(
        sink,
        api_key="memory-secret",
        accepted_source_ids=("node-a",),
    )
    body = json.dumps(
        {"object": "memory_replication_batch", "batch": _batch().as_dict()},
        sort_keys=True,
    ).encode()
    handler = _Handler(body)

    assert handle_memory_replication(handler, "POST", receiver) is True
    assert handler.responses[0][0] == 202
    assert handler.responses[0][1]["object"] == "memory_replication_receipt"
    assert handler._request_body_consumed is True


def test_http_adapter_rejects_origin_and_disabled_receiver_without_reading_body():
    body = b"{}"
    handler = _Handler(body)
    handler.headers["Origin"] = "https://browser.example"
    receiver = MemoryReplicationReceiver(
        _Sink(),
        api_key="memory-secret",
        accepted_source_ids=("node-a",),
    )
    assert handle_memory_replication(handler, "POST", receiver) is True
    assert handler.responses[0][0] == 403
    assert handler.rfile.read() == body

    disabled = _Handler(body)
    assert handle_memory_replication(disabled, "POST", None) is True
    assert disabled.responses[0][0] == 503
