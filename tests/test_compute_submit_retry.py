"""A compute submit that never reached its node is resumable on retry.

The placement is recorded before dispatch. A retry with the same controller
job id used to report "ambiguous" forever when the node definitively had no
job; replay is now performed when it is safe (idempotent envelope, or the
in-process local worker whose journal is authoritative).
"""
from dataclasses import replace

import pytest

from sonder_runtime.domain.common.errors import DependencyUnavailable

from tests.test_compute_placement_service import (
    _envelope,
    _receipt,
    _request,
    _service,
)


class _PreSendFailureTransport:
    """The node is unreachable once; afterwards it definitively has no job."""

    def __init__(self):
        self.submit_calls = 0
        self.lookup_calls = 0
        self.down = True

    def submit(self, node, _envelope):
        self.submit_calls += 1
        if self.down:
            raise DependencyUnavailable("compute node is unavailable")
        return _receipt(node.node_id, "remote-after-retry")

    def by_idempotency(self, node, _key):
        self.lookup_calls += 1
        if self.down:
            raise DependencyUnavailable("compute node is unavailable")
        return None

    def status(self, node, remote_job_id):
        return _receipt(node.node_id, remote_job_id)


def _with_transport(transport):
    service, _unused, local = _service()
    service._transport = transport
    return service, local


def test_idempotent_submit_that_never_reached_its_node_is_resent_on_retry():
    transport = _PreSendFailureTransport()
    service, _local = _with_transport(transport)
    with pytest.raises(DependencyUnavailable):
        service.submit(_request(), _envelope())
    assert transport.submit_calls == 1

    transport.down = False
    result = service.submit(_request(), _envelope())
    assert result.node_id == "linux-node"
    assert result.receipt.remote_job_id == "remote-after-retry"
    assert transport.submit_calls == 2
    # The recorded job now resolves normally instead of staying ambiguous.
    assert service.status("controller-job").receipt.remote_job_id == "remote-after-retry"


def test_non_idempotent_remote_submit_keeps_the_ambiguity_fence():
    transport = _PreSendFailureTransport()
    service, _local = _with_transport(transport)
    request = replace(_request(), idempotent=False)
    envelope = _envelope()
    envelope = type(envelope).create(
        controller_job_id=envelope.controller_job_id, idempotency_key=envelope.idempotency_key,
        workload=envelope.workload, catalog_entry_id=envelope.catalog_entry_id,
        workspace_mapping=envelope.workspace_mapping, deadline_seconds=300, idempotent=False)
    with pytest.raises(DependencyUnavailable):
        service.submit(request, envelope)
    transport.down = False
    with pytest.raises(DependencyUnavailable, match="ambiguous"):
        service.submit(request, envelope)
    assert transport.submit_calls == 1


def test_local_submit_rejected_before_intent_is_resubmitted_on_retry():
    service, _transport, local = _service()
    attempts = []
    receipts = {}

    def submit(envelope):
        attempts.append(envelope.idempotency_key)
        if len(attempts) == 1:
            raise DependencyUnavailable("local compute worker is busy")
        receipts[envelope.idempotency_key] = _receipt("local", "local-after-retry")
        return receipts[envelope.idempotency_key]

    local.submit = submit
    local.by_idempotency = lambda key: receipts.get(key)
    local.status = lambda remote_job_id: _receipt("local", remote_job_id)
    request = _request(allow_remote=False)
    with pytest.raises(DependencyUnavailable, match="busy"):
        service.submit(request, _envelope())
    result = service.submit(request, _envelope())
    assert result.node_id == "local"
    assert result.receipt.remote_job_id == "local-after-retry"
    assert len(attempts) == 2
