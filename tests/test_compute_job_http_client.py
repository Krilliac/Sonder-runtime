from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.compute_fabric.http_client import HttpsComputeJobTransport
from sonder_runtime.application.compute_fabric.jobs import RemoteJobEnvelope
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.compute_fabric import ComputeNode, WorkloadKind


class _Response:
    def __init__(self, status: int, body: bytes, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, limit: int) -> bytes:
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _node() -> ComputeNode:
    return ComputeNode(
        node_id="linux-node",
        origin="https://linux-node.example:8443",
        local=False,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
    )


def _envelope() -> RemoteJobEnvelope:
    return RemoteJobEnvelope.create(
        controller_job_id="controller-job",
        idempotency_key="idem-1",
        workload=WorkloadKind.BUILD,
        catalog_entry_id="cmake-build",
        workspace_mapping="sonder",
        deadline_seconds=60,
        idempotent=True,
    )


def _receipt_body() -> bytes:
    envelope = _envelope()
    return json.dumps({
        "object": "compute_job",
        "job": {
            "worker_id": "linux-node",
            "remote_job_id": "remote-1",
            "controller_job_id": "controller-job",
            "idempotency_key": "idem-1",
            "request_sha256": envelope.request_sha256,
            "state": "running",
            "process_id": 42,
            "artifacts": [],
        },
    }).encode()


def test_job_client_submits_digest_bound_envelope_without_redirects() -> None:
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(202, _receipt_body())

    transport = HttpsComputeJobTransport(api_key="secret", opener=opener)
    receipt = transport.submit(_node(), _envelope())
    assert receipt.remote_job_id == "remote-1"
    assert captured["url"] == "https://linux-node.example:8443/v1/compute/jobs"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer secret"
    assert captured["body"]["request_sha256"] == _envelope().request_sha256
    assert captured["timeout"] == 5.0


def test_job_client_rejects_worker_identity_mismatch_and_oversize() -> None:
    body = json.loads(_receipt_body())
    body["job"]["worker_id"] = "different"
    transport = HttpsComputeJobTransport(
        api_key="secret", opener=lambda *_args, **_kwargs: _Response(202, json.dumps(body).encode())
    )
    with pytest.raises(DependencyUnavailable, match="identity"):
        transport.submit(_node(), _envelope())

    transport = HttpsComputeJobTransport(
        api_key="secret",
        max_response_bytes=1024,
        opener=lambda *_args, **_kwargs: _Response(202, b"x" * 1025),
    )
    with pytest.raises(DependencyUnavailable, match="size"):
        transport.submit(_node(), _envelope())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("controller_job_id", "other-controller", "controller"),
        ("idempotency_key", "other-idem", "idempotency"),
        ("request_sha256", "0" * 64, "digest"),
    ),
)
def test_job_client_submit_validates_complete_receipt_ownership(field, value, message) -> None:
    body = json.loads(_receipt_body())
    body["job"][field] = value
    transport = HttpsComputeJobTransport(
        api_key="secret",
        opener=lambda *_args, **_kwargs: _Response(202, json.dumps(body).encode()),
    )
    with pytest.raises(DependencyUnavailable, match=message):
        transport.submit(_node(), _envelope())


def test_job_client_status_cancel_and_lookup_validate_requested_identity() -> None:
    body = json.loads(_receipt_body())
    body["job"]["remote_job_id"] = "different-job"
    transport = HttpsComputeJobTransport(
        api_key="secret",
        opener=lambda *_args, **_kwargs: _Response(200, json.dumps(body).encode()),
    )
    with pytest.raises(DependencyUnavailable, match="remote job"):
        transport.status(_node(), "remote-1")
    with pytest.raises(DependencyUnavailable, match="remote job"):
        transport.cancel(_node(), "remote-1", reason="stop")

    body["job"]["remote_job_id"] = "remote-1"
    body["job"]["idempotency_key"] = "different-idem"
    transport = HttpsComputeJobTransport(
        api_key="secret",
        opener=lambda *_args, **_kwargs: _Response(200, json.dumps(body).encode()),
    )
    with pytest.raises(DependencyUnavailable, match="idempotency"):
        transport.by_idempotency(_node(), "idem-1")


def test_job_client_status_lookup_and_cancel_use_exact_paths() -> None:
    seen = []

    def opener(request, *, timeout):
        seen.append((request.method, request.full_url, request.data, timeout))
        if request.full_url.endswith("/by-idempotency/missing"):
            return _Response(404, b"")
        return _Response(200, _receipt_body())

    transport = HttpsComputeJobTransport(api_key="secret", opener=opener)
    assert transport.status(_node(), "remote-1").remote_job_id == "remote-1"
    assert transport.by_idempotency(_node(), "idem-1").idempotency_key == "idem-1"
    assert transport.by_idempotency(_node(), "missing") is None
    assert transport.cancel(
        _node(), "remote-1", reason="operator requested"
    ).remote_job_id == "remote-1"

    assert [item[0:2] for item in seen] == [
        ("GET", "https://linux-node.example:8443/v1/compute/jobs/remote-1"),
        ("GET", "https://linux-node.example:8443/v1/compute/jobs/by-idempotency/idem-1"),
        ("GET", "https://linux-node.example:8443/v1/compute/jobs/by-idempotency/missing"),
        ("POST", "https://linux-node.example:8443/v1/compute/jobs/remote-1/cancel"),
    ]
    assert json.loads(seen[-1][2]) == {"reason": "operator requested"}


def test_job_client_fetches_and_revalidates_digest_bound_artifact() -> None:
    import hashlib
    from sonder_runtime.application.compute_fabric.jobs import RemoteArtifactReceipt

    content = b'{"ok":true}'
    digest = hashlib.sha256(content).hexdigest()
    expected = RemoteArtifactReceipt(
        "reports/result.json", len(content), "application/json", digest,
    )
    seen = {}

    def opener(request, *, timeout):
        seen["url"] = request.full_url
        return _Response(200, content, {
            "Content-Length": str(len(content)),
            "Content-Type": "application/json",
            "X-Sonder-Artifact-Sha256": digest,
        })

    transport = HttpsComputeJobTransport(api_key="secret", opener=opener)
    payload = transport.fetch_artifact(_node(), "remote-1", expected)
    assert payload.content == content
    assert seen["url"].endswith(
        "/v1/compute/jobs/remote-1/artifacts/reports%2Fresult.json"
    )

    transport = HttpsComputeJobTransport(
        api_key="secret",
        opener=lambda *_args, **_kwargs: _Response(200, b"changed", {
            "Content-Length": "7",
            "Content-Type": "application/json",
            "X-Sonder-Artifact-Sha256": hashlib.sha256(b"changed").hexdigest(),
        }),
    )
    with pytest.raises(DependencyUnavailable, match="receipt"):
        transport.fetch_artifact(_node(), "remote-1", expected)
