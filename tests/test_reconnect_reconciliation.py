from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.domain.reconnect_reconciliation import (
    AuthorityLease,
    ClientReconnectRequest,
    DiscoveryDisposition,
    DiscoveryMember,
    DiscoverySnapshot,
    ReceiptDisposition,
    ReceiptReconciliationRequest,
    ReconnectReason,
    ReconnectReconciliationPolicy,
    WorkerReceipt,
    WorkerReceiptState,
)


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def _authority(*, epoch: int = 7, lease_id: str = "lease-7", expires: datetime | None = None) -> AuthorityLease:
    return AuthorityLease(
        cluster_id="cluster-a",
        owner_id="owner-a",
        owner_epoch=epoch,
        lease_id=lease_id,
        expires_at=expires or NOW + timedelta(minutes=5),
    )


def _member(
    node_id: str,
    *,
    authority: AuthorityLease | None = None,
    endpoint_id: str | None = None,
    reachable: bool = True,
    protocols: tuple[int, ...] = (1,),
) -> DiscoveryMember:
    return DiscoveryMember(
        node_id=node_id,
        endpoint_id=endpoint_id or f"endpoint-{node_id}",
        authority=authority or _authority(),
        reachable=reachable,
        protocol_versions=protocols,
    )


def _snapshot(
    *members: DiscoveryMember,
    authority: AuthorityLease | None = None,
    revision: int = 11,
) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        cluster_id="cluster-a",
        authority=authority or _authority(),
        revision=revision,
        members=tuple(members),
    )


def _request(**changes) -> ClientReconnectRequest:
    values = {
        "client_id": "client-1",
        "cluster_id": "cluster-a",
        "protocol_version": 1,
        "last_authority": _authority(),
        "session_id": "session-1",
        "preferred_endpoint_id": None,
        "last_receipt_revision": 0,
    }
    values.update(changes)
    return ClientReconnectRequest(**values)


def _receipt(**changes) -> WorkerReceipt:
    values = {
        "client_id": "client-1",
        "cluster_id": "cluster-a",
        "operation_id": "operation-1",
        "idempotency_key": "idem-1",
        "request_digest": "a" * 64,
        "worker_id": "worker-1",
        "remote_job_id": "remote-1",
        "owner_id": "owner-a",
        "owner_epoch": 7,
        "lease_id": "lease-7",
        "state": WorkerReceiptState.RUNNING,
        "revision": 3,
        "output_watermark": 9,
    }
    values.update(changes)
    return WorkerReceipt(**values)


def _reconcile_request(**changes) -> ReceiptReconciliationRequest:
    values = {
        "client_id": "client-1",
        "cluster_id": "cluster-a",
        "operation_id": "operation-1",
        "idempotency_key": "idem-1",
        "request_digest": "a" * 64,
        "authority": _authority(),
        "last_seen_revision": 0,
    }
    values.update(changes)
    return ReceiptReconciliationRequest(**values)


def test_discovery_selects_a_deterministic_reachable_endpoint_for_the_current_authority():
    policy = ReconnectReconciliationPolicy()
    snapshot = _snapshot(_member("node-b"), _member("node-a"))

    decision = policy.discover(_request(), snapshot, now=NOW)

    assert decision.disposition is DiscoveryDisposition.CONNECTED
    assert decision.reason is ReconnectReason.DISCOVERY_ACCEPTED
    assert decision.selected_node_id == "node-a"
    assert decision.selected_endpoint_id == "endpoint-node-a"
    assert decision.candidate_node_ids == ("node-a", "node-b")
    assert decision.authority == snapshot.authority


def test_discovery_honors_preferred_endpoint_only_when_it_is_eligible():
    policy = ReconnectReconciliationPolicy()
    snapshot = _snapshot(_member("node-a"), _member("node-b"))

    preferred = policy.discover(
        _request(preferred_endpoint_id="endpoint-node-b"), snapshot, now=NOW
    )
    unavailable_preference = policy.discover(
        _request(preferred_endpoint_id="endpoint-gone"), snapshot, now=NOW
    )

    assert preferred.selected_node_id == "node-b"
    assert unavailable_preference.selected_node_id == "node-a"


def test_discovery_reports_unavailable_when_all_members_are_unreachable():
    policy = ReconnectReconciliationPolicy()
    snapshot = _snapshot(_member("node-a", reachable=False))

    decision = policy.discover(_request(), snapshot, now=NOW)

    assert decision.disposition is DiscoveryDisposition.UNAVAILABLE
    assert decision.reason is ReconnectReason.MEMBER_UNAVAILABLE
    assert decision.selected_node_id is None


def test_discovery_reports_protocol_mismatch_when_reachable_members_cannot_speak_client_version():
    policy = ReconnectReconciliationPolicy()
    snapshot = _snapshot(_member("node-a", protocols=(2,)))

    decision = policy.discover(_request(), snapshot, now=NOW)

    assert decision.disposition is DiscoveryDisposition.UNAVAILABLE
    assert decision.reason is ReconnectReason.PROTOCOL_MISMATCH


def test_discovery_excludes_an_endpoint_with_an_expired_member_lease():
    policy = ReconnectReconciliationPolicy()
    expired_member = _member("node-a", authority=_authority(expires=NOW))
    snapshot = _snapshot(expired_member)

    decision = policy.discover(_request(), snapshot, now=NOW)

    assert decision.disposition is DiscoveryDisposition.UNAVAILABLE
    assert decision.reason is ReconnectReason.MEMBER_UNAVAILABLE


def test_discovery_rejects_a_snapshot_larger_than_the_policy_bound():
    policy = ReconnectReconciliationPolicy(max_members=1)
    snapshot = _snapshot(_member("node-a"), _member("node-b"))

    with pytest.raises(ValueError, match="member bound"):
        policy.discover(_request(), snapshot, now=NOW)


@pytest.mark.parametrize(
    ("last_authority", "current_authority", "expected"),
    (
        (_authority(epoch=8, lease_id="lease-8"), _authority(), ReconnectReason.AUTHORITY_STALE),
        (_authority(), _authority(epoch=8, lease_id="lease-8"), ReconnectReason.AUTHORITY_AHEAD),
        (_authority(), _authority(lease_id="lease-other"), ReconnectReason.AUTHORITY_AMBIGUOUS),
    ),
)
def test_discovery_pauses_for_stale_ahead_or_ambiguous_authority(
    last_authority, current_authority, expected
):
    policy = ReconnectReconciliationPolicy()
    snapshot = _snapshot(
        _member("node-a", authority=current_authority),
        authority=current_authority,
    )

    decision = policy.discover(
        _request(last_authority=last_authority), snapshot, now=NOW
    )

    assert decision.disposition is DiscoveryDisposition.PAUSED
    assert decision.reason is expected
    assert decision.selected_node_id is None


def test_discovery_never_promotes_a_new_authority_without_a_fresh_client_binding():
    policy = ReconnectReconciliationPolicy()
    current = _authority(epoch=8, lease_id="lease-8")
    snapshot = _snapshot(_member("node-a", authority=current), authority=current)

    decision = policy.discover(
        _request(last_authority=None), snapshot, now=NOW
    )

    assert decision.disposition is DiscoveryDisposition.CONNECTED
    assert decision.authority is current


def test_discovery_rejects_cluster_mismatch_and_expired_authority():
    policy = ReconnectReconciliationPolicy()
    snapshot = _snapshot(_member("node-a"), authority=_authority(expires=NOW))

    expired = policy.discover(_request(), snapshot, now=NOW)
    mismatched = policy.discover(
        _request(cluster_id="other-cluster"), snapshot, now=NOW
    )

    assert expired.disposition is DiscoveryDisposition.PAUSED
    assert expired.reason is ReconnectReason.AUTHORITY_EXPIRED
    assert mismatched.disposition is DiscoveryDisposition.REJECTED
    assert mismatched.reason is ReconnectReason.CLUSTER_MISMATCH


def test_reconciliation_returns_the_latest_matching_running_receipt_for_resume():
    policy = ReconnectReconciliationPolicy()
    older = _receipt(revision=2, output_watermark=4)
    latest = _receipt(revision=3, output_watermark=9)

    decision = policy.reconcile(
        _reconcile_request(), (latest, older, latest), current_authority=_authority(), now=NOW
    )

    assert decision.disposition is ReceiptDisposition.RESUME
    assert decision.reason is ReconnectReason.RECEIPT_RESUMABLE
    assert decision.receipt == latest
    assert decision.candidate_count == 3
    assert decision.deduplicated_count == 1


def test_reconciliation_replays_terminal_receipts_without_resubmitting_work():
    policy = ReconnectReconciliationPolicy()
    terminal = _receipt(state=WorkerReceiptState.SUCCEEDED, revision=8)

    decision = policy.reconcile(
        _reconcile_request(), (terminal,), current_authority=_authority(), now=NOW
    )

    assert decision.disposition is ReceiptDisposition.REPLAY
    assert decision.reason is ReconnectReason.RECEIPT_TERMINAL
    assert decision.resume_allowed is False


def test_reconciliation_explicitly_reports_missing_receipt_as_unavailable():
    decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (), current_authority=_authority(), now=NOW
    )

    assert decision.disposition is ReceiptDisposition.UNAVAILABLE
    assert decision.reason is ReconnectReason.RECEIPT_NOT_FOUND
    assert decision.receipt is None


def test_reconciliation_pauses_stale_receipts_and_never_resumes_old_owner_work():
    stale = _receipt(owner_epoch=6, lease_id="lease-6")

    decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (stale,), current_authority=_authority(), now=NOW
    )

    assert decision.disposition is ReceiptDisposition.PAUSED
    assert decision.reason is ReconnectReason.RECEIPT_STALE
    assert decision.resume_allowed is False


def test_reconciliation_pauses_receipts_from_ahead_or_ambiguous_leases():
    current = _authority()
    ahead = _receipt(owner_epoch=8, lease_id="lease-8")
    ambiguous = _receipt(lease_id="lease-other")

    ahead_decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (ahead,), current_authority=current, now=NOW
    )
    ambiguous_decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (ambiguous,), current_authority=current, now=NOW
    )

    assert ahead_decision.disposition is ReceiptDisposition.PAUSED
    assert ahead_decision.reason is ReconnectReason.RECEIPT_AHEAD
    assert ambiguous_decision.disposition is ReceiptDisposition.PAUSED
    assert ambiguous_decision.reason is ReconnectReason.LEASE_MISMATCH


def test_reconciliation_rejects_two_distinct_remote_jobs_for_one_idempotency_key():
    first = _receipt()
    second = _receipt(worker_id="worker-2", remote_job_id="remote-2")

    decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (first, second), current_authority=_authority(), now=NOW
    )

    assert decision.disposition is ReceiptDisposition.REJECTED
    assert decision.reason is ReconnectReason.RECEIPT_CONFLICT
    assert decision.resume_allowed is False


def test_reconciliation_rejects_idempotency_digest_conflict():
    conflicting = _receipt(request_digest="b" * 64)

    decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (conflicting,), current_authority=_authority(), now=NOW
    )

    assert decision.disposition is ReceiptDisposition.REJECTED
    assert decision.reason is ReconnectReason.IDEMPOTENCY_CONFLICT


def test_reconciliation_rejects_a_receipt_owned_by_another_stable_client():
    foreign = _receipt(client_id="client-2")

    decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (foreign,), current_authority=_authority(), now=NOW
    )

    assert decision.disposition is ReceiptDisposition.REJECTED
    assert decision.reason is ReconnectReason.CLIENT_MISMATCH


def test_reconciliation_pauses_worker_paused_or_interrupted_state():
    paused = _receipt(state=WorkerReceiptState.PAUSED)
    interrupted = _receipt(state=WorkerReceiptState.INTERRUPTED, revision=4)

    paused_decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (paused,), current_authority=_authority(), now=NOW
    )
    interrupted_decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(), (interrupted,), current_authority=_authority(), now=NOW
    )

    assert paused_decision.reason is ReconnectReason.WORKER_PAUSED
    assert interrupted_decision.reason is ReconnectReason.WORKER_INTERRUPTED
    assert paused_decision.disposition is ReceiptDisposition.PAUSED
    assert interrupted_decision.disposition is ReceiptDisposition.PAUSED


def test_reconciliation_rejects_a_client_authority_that_does_not_match_current():
    decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(authority=_authority(epoch=6, lease_id="lease-6")),
        (_receipt(),),
        current_authority=_authority(),
        now=NOW,
    )

    assert decision.disposition is ReceiptDisposition.PAUSED
    assert decision.reason is ReconnectReason.AUTHORITY_AHEAD


def test_reconciliation_respects_client_seen_revision_and_explicitly_pauses_when_history_is_older():
    decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(last_seen_revision=4),
        (_receipt(revision=3),),
        current_authority=_authority(),
        now=NOW,
    )

    assert decision.disposition is ReceiptDisposition.PAUSED
    assert decision.reason is ReconnectReason.RECEIPT_STALE


def test_reconciliation_reports_expired_current_authority_as_paused():
    decision = ReconnectReconciliationPolicy().reconcile(
        _reconcile_request(),
        (_receipt(),),
        current_authority=_authority(expires=NOW),
        now=NOW,
    )

    assert decision.disposition is ReceiptDisposition.PAUSED
    assert decision.reason is ReconnectReason.AUTHORITY_EXPIRED


@pytest.mark.parametrize(
    "factory",
    (
        lambda: AuthorityLease("", "owner-a", 1, "lease", NOW),
        lambda: AuthorityLease("cluster-a", "owner-a", 0, "lease", NOW),
        lambda: AuthorityLease("cluster-a", "owner-a", 1, "lease", datetime(2026, 9, 5)),
        lambda: DiscoveryMember("node-a", "endpoint-a", _authority(), True, ()),
        lambda: WorkerReceipt(
            "client-1", "cluster-a", "op", "idem", "a" * 64, "worker", "remote",
            "owner-a", 1, "lease", "running", 0, 0,
        ),
    ),
)
def test_contract_types_reject_unbounded_or_ambiguous_identity(factory):
    with pytest.raises(ValueError):
        factory()


def test_contract_bounds_reject_duplicate_members_and_receipt_overflow():
    authority = _authority()
    with pytest.raises(ValueError, match="unique"):
        _snapshot(_member("node-a"), _member("node-a"))

    policy = ReconnectReconciliationPolicy(max_receipts=2)
    with pytest.raises(ValueError, match="receipt bound"):
        policy.reconcile(
            _reconcile_request(),
            (_receipt(), _receipt(revision=4), _receipt(revision=5)),
            current_authority=authority,
            now=NOW,
        )


def test_decisions_are_safe_json_ready_status_projections():
    policy = ReconnectReconciliationPolicy()
    discovery = policy.discover(_request(), _snapshot(_member("node-a")), now=NOW)
    reconciliation = policy.reconcile(
        _reconcile_request(), (_receipt(),), current_authority=_authority(), now=NOW
    )

    assert discovery.as_dict()["authority"]["lease_id"] == "lease-7"
    assert discovery.takeover_safe is False
    assert reconciliation.as_dict()["receipt"]["request_digest"] == "a" * 64
    assert reconciliation.as_dict()["resume_allowed"] is True
    assert reconciliation.available is True
    assert reconciliation.takeover_safe is False
