"""Pure admitted-membership decisions; no registry, pool, or host I/O."""
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib
import json

import pytest


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
KEY = b"offline-test-signing-key"


def domain():
    return importlib.import_module("sonder_runtime.domain.inference_membership")


def wire_worker(worker_id="worker-a", **changes):
    row = dict(worker_id=worker_id, origin="https://%s.example:11434" % worker_id,
               member_generation=1, lifecycle_state="active", models=["safe:latest"],
               advertised_capacity=2)
    row.update(changes)
    return row


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def envelope(workers=None, **changes):
    payload = dict(cluster_id="cluster-a", issuer_id="issuer-a", generation=1,
                   protocol_version=1, issued_at="2026-09-07T11:59:00Z",
                   expires_at="2026-09-07T12:05:00Z",
                   workers=[wire_worker()] if workers is None else workers)
    payload.update(changes)
    signature = hmac.new(KEY, canonical(payload), hashlib.sha256).hexdigest()
    return canonical(dict(payload=payload, signature=signature))


def verify(raw):
    signed = json.loads(raw)
    expected = hmac.new(KEY, canonical(signed["payload"]), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signed["signature"])


def snapshot(workers=None, **changes):
    return domain().MembershipSnapshot.from_signed_envelope(
        envelope(workers, **changes), verify=verify,
    )


def reconcile(candidate, *, previous=None, high_water=None, now=NOW, **kwargs):
    return domain().reconcile_membership(
        candidate, previous=previous, high_water=high_water,
        cluster_id="cluster-a", issuer_id="issuer-a", clock=lambda: now, **kwargs,
    )


def evidence(worker, *, checked_at=NOW, expires_at=NOW + timedelta(minutes=1)):
    return domain().CapabilityEvidence(
        worker.worker_id, worker.origin, worker.member_generation, checked_at, expires_at,
    )


def active_roster():
    first = reconcile(snapshot())
    return reconcile(first.roster.snapshot, previous=first.roster,
                     high_water=first.high_water,
                     capability_evidence=(evidence(first.roster.members[0].advertisement),))


def test_snapshot_digest_and_fields_come_from_the_same_verified_envelope():
    raw = envelope()
    candidate = domain().MembershipSnapshot.from_signed_envelope(raw, verify=verify)
    assert candidate.digest == hashlib.sha256(raw).hexdigest()
    assert candidate.digest != hashlib.sha256(canonical(json.loads(raw)["payload"])).hexdigest()
    assert candidate.canonical_envelope == raw
    assert candidate.workers[0].worker_id == "worker-a"
    assert candidate.expires_at == NOW + timedelta(minutes=5)
    with pytest.raises(TypeError):
        domain().MembershipSnapshot(workers=(), digest="0" * 64)
    with pytest.raises(FrozenInstanceError):
        candidate.digest = "0" * 64


def test_changed_payload_cannot_reuse_signature_or_supply_an_unbound_digest():
    signed = json.loads(envelope())
    signed["payload"]["workers"][0]["origin"] = "https://changed.example:11434"
    with pytest.raises(ValueError, match="verification"):
        domain().MembershipSnapshot.from_signed_envelope(canonical(signed), verify=verify)
    signed = json.loads(envelope())
    signed["digest"] = "0" * 64
    with pytest.raises(ValueError):
        domain().MembershipSnapshot.from_signed_envelope(canonical(signed), verify=verify)


@pytest.mark.parametrize("raw", [b"{}", b"[]", b'{"payload":1,"payload":2}', b"NaN"])
def test_malformed_or_ambiguous_envelope_fails_closed(raw):
    with pytest.raises(ValueError):
        domain().MembershipSnapshot.from_signed_envelope(raw, verify=verify)


def test_noncanonical_and_oversized_bytes_are_rejected_before_verification():
    def forbidden(_):
        pytest.fail("invalid envelope reached signature verification")
    for raw in (b" " + envelope(), b"x" * 1_048_577):
        with pytest.raises(ValueError):
            domain().MembershipSnapshot.from_signed_envelope(raw, verify=forbidden)


def test_narrower_byte_ceiling_accepts_exact_bound_and_rejects_next_byte():
    raw = envelope()
    assert domain().MembershipSnapshot.from_signed_envelope(raw, verify=verify, max_bytes=len(raw)).workers
    with pytest.raises(ValueError, match="byte limit"):
        domain().MembershipSnapshot.from_signed_envelope(raw, verify=verify, max_bytes=len(raw) - 1)


def test_verifier_exception_never_creates_a_snapshot():
    def unavailable(_):
        raise OSError("test verifier unavailable")
    with pytest.raises(ValueError, match="verification"):
        domain().MembershipSnapshot.from_signed_envelope(envelope(), verify=unavailable)


@pytest.mark.parametrize("result", [False, None, 1, "verified"])
def test_verification_must_explicitly_succeed(result):
    with pytest.raises(ValueError, match="verification"):
        domain().MembershipSnapshot.from_signed_envelope(envelope(), verify=lambda _: result)


@pytest.mark.parametrize("identity", ["", " white", "worker/a", "https://worker", "a\n", "a" * 129, 1])
def test_malformed_worker_identity_is_rejected(identity):
    with pytest.raises(ValueError, match="identity"):
        snapshot([wire_worker(identity)])


@pytest.mark.parametrize("origin", [
    "http://worker.example:11434", "http://127.0.0.1:11434",
    "https://user:secret@worker.example:11434", "https://worker.example:11434/api",
    "https://worker.example:11434?x=1", "https://worker.example", "https://worker.example:0",
    "https://worker.example:65536", "https://bad_host:11434", "https://[::]:11434",
])
def test_only_valid_explicit_https_member_origins_are_accepted(origin):
    with pytest.raises(ValueError, match="origin"):
        snapshot([wire_worker(origin=origin)])


@pytest.mark.parametrize("workers", [
    [wire_worker(), wire_worker(origin="https://other.example:11434")],
    [wire_worker(), wire_worker("worker-b", origin="https://WORKER-A.EXAMPLE.:11434/")],
    [wire_worker(origin="https://[2001:db8::1]:11434"),
     wire_worker("worker-b", origin="https://[2001:0db8:0:0:0:0:0:1]:11434")],
    [wire_worker(origin="https://localhost:11434"),
     wire_worker("worker-b", origin="https://127.0.0.1:11434")],
])
def test_duplicate_ids_and_canonical_origins_are_rejected(workers):
    with pytest.raises(ValueError, match="duplicate"):
        snapshot(workers)


@pytest.mark.parametrize("changes", [
    {"protocol_version": 2}, {"protocol_version": True}, {"generation": 0},
    {"generation": True}, {"cluster_id": ""}, {"issuer_id": "issuer/a"},
    {"issued_at": "2026-09-07T12:06:00Z"}, {"expires_at": "2026-09-07T11:59:00Z"},
    {"issued_at": "2026-09-07T11:59:00"}, {"extra": "unrecognized"},
])
def test_invalid_snapshot_shape_protocol_and_expiry_are_rejected(changes):
    with pytest.raises(ValueError):
        snapshot(**changes)


@pytest.mark.parametrize("changes", [
    {"member_generation": 0}, {"member_generation": True}, {"lifecycle_state": "owner"},
    {"advertised_capacity": 0}, {"advertised_capacity": 65}, {"advertised_capacity": True},
    {"models": ["a"] * 2049}, {"models": ["m" * 257]}, {"models": ["model\nbody"]},
])
def test_worker_metadata_is_bounded(changes):
    with pytest.raises(ValueError):
        snapshot([wire_worker(**changes)])


def test_advertisement_copies_models_into_immutable_bounded_values():
    models = ["safe:latest"]
    worker = domain().WorkerAdvertisement(**wire_worker(models=models))
    models.append("changed")
    assert worker.models == ("safe:latest",)
    with pytest.raises(FrozenInstanceError):
        worker.origin = "https://changed.example:11434"


def test_snapshot_advertisement_limit_and_narrower_source_limit_are_enforced():
    workers = [wire_worker("w%d" % i, models=[]) for i in range(4096)]
    assert len(snapshot(workers).workers) == 4096
    with pytest.raises(ValueError, match="advertisement"):
        snapshot(workers + [wire_worker("overflow", models=[])])
    with pytest.raises(ValueError, match="advertisement"):
        domain().MembershipSnapshot.from_signed_envelope(envelope(workers[:2]), verify=verify,
                                                        max_advertisements=1)


def test_high_water_has_exact_authority_generation_and_digest_fields():
    candidate = snapshot()
    water = domain().validate_high_water(candidate, None, cluster_id="cluster-a",
                                        issuer_id="issuer-a", clock=lambda: NOW)
    assert tuple(field.name for field in fields(water)) == (
        "cluster_id", "issuer_id", "generation", "digest",
    )
    assert (water.cluster_id, water.issuer_id, water.generation, water.digest) == (
        "cluster-a", "issuer-a", 1, candidate.digest,
    )
    assert domain().validate_high_water(candidate, water, cluster_id="cluster-a",
                                       issuer_id="issuer-a", clock=lambda: NOW) == water


@pytest.mark.parametrize("changes,now", [
    ({"generation": 1}, NOW),
    ({"generation": 2, "workers": []}, NOW),
    ({"generation": 2}, NOW + timedelta(minutes=5)),
    ({"generation": 3, "cluster_id": "other-cluster"}, NOW),
    ({"generation": 3, "issuer_id": "other-issuer"}, NOW),
    ({"generation": 3, "issued_at": "2026-09-07T12:01:00Z"}, NOW),
])
def test_replay_equivocation_expiry_and_wrong_authority_are_rejected(changes, now):
    current = reconcile(snapshot(generation=2))
    with pytest.raises(ValueError):
        reconcile(snapshot(**changes), previous=current.roster, high_water=current.high_water, now=now)
    assert current.roster.generation == 1
    assert current.high_water.generation == 2


def test_naive_clock_and_naive_capability_evidence_are_rejected():
    with pytest.raises(ValueError, match="timezone"):
        reconcile(snapshot(), now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone"):
        evidence(snapshot().workers[0], checked_at=NOW.replace(tzinfo=None))


def test_clock_is_injected_once_and_timezone_offsets_are_equivalent():
    calls = []
    result = domain().reconcile_membership(snapshot(), cluster_id="cluster-a", issuer_id="issuer-a",
        clock=lambda: calls.append(1) or NOW.astimezone(timezone(timedelta(hours=-5))))
    assert result.roster.members[0].lifecycle_state == "probation"
    assert calls == [1]


def test_new_members_are_probationary_and_idempotent_reread_does_not_promote():
    candidate = snapshot()
    first = reconcile(candidate, capability_evidence=(evidence(candidate.workers[0]),))
    assert first.additions == candidate.workers
    assert first.activations == ()
    assert first.roster.members[0].lifecycle_state == "probation"
    second = reconcile(candidate, previous=first.roster, high_water=first.high_water)
    assert second.roster_generation == first.roster_generation == 1
    assert second.additions == second.activations == second.drains == second.expirations == ()


def test_only_fresh_exact_generation_and_origin_capability_evidence_activates():
    first = reconcile(snapshot())
    worker = first.roster.members[0].advertisement
    wrong = (
        replace(evidence(worker), origin="https://other.example:11434"),
        replace(evidence(worker), member_generation=2),
        evidence(worker, checked_at=NOW - timedelta(minutes=2), expires_at=NOW),
        evidence(worker, checked_at=NOW + timedelta(seconds=1)),
    )
    for proof in wrong:
        result = reconcile(first.roster.snapshot, previous=first.roster,
                           high_water=first.high_water, capability_evidence=(proof,))
        assert result.activations == ()
        assert result.roster.members[0].lifecycle_state == "probation"
    result = reconcile(first.roster.snapshot, previous=first.roster,
                       high_water=first.high_water, capability_evidence=(evidence(worker),))
    assert result.activations == (worker,)
    assert result.roster.members[0].lifecycle_state == "active"
    assert result.roster_generation == 2


def test_endpoint_replacement_drains_old_endpoint_and_requires_new_member_generation():
    active = active_roster()
    replacement = wire_worker(origin="https://replacement.example:11434", member_generation=2)
    result = reconcile(snapshot([replacement], generation=2), previous=active.roster,
                       high_water=active.high_water)
    assert result.drains == (active.roster.members[0].advertisement,)
    assert result.additions[0].origin == "https://replacement.example:11434"
    assert result.roster.members[0].lifecycle_state == "probation"
    assert result.activations == ()
    assert active.roster.members[0].advertisement.origin == "https://worker-a.example:11434"
    with pytest.raises(ValueError, match="generation"):
        reconcile(snapshot([dict(replacement, member_generation=1)], generation=2),
                  previous=active.roster, high_water=active.high_water)


def test_member_generation_rollback_is_rejected_even_with_newer_snapshot():
    current = reconcile(snapshot([wire_worker(member_generation=2)]))
    with pytest.raises(ValueError, match="generation"):
        reconcile(snapshot(generation=2), previous=current.roster, high_water=current.high_water)


@pytest.mark.parametrize("state", [None, "revoked", "draining", "expired"])
def test_removal_revocation_and_explicit_drain_stop_new_admission(state):
    current = active_roster()
    workers = [] if state is None else [wire_worker(lifecycle_state=state)]
    result = reconcile(snapshot(workers, generation=2), previous=current.roster,
                       high_water=current.high_water)
    assert result.roster.members == ()
    assert result.drains == (current.roster.members[0].advertisement,)
    assert result.activations == result.additions == ()


def test_source_outage_preserves_only_unexpired_trust_and_expires_members_at_deadline():
    current = active_roster()
    fresh = reconcile(None, previous=current.roster, high_water=current.high_water)
    assert fresh.roster == current.roster
    expired = reconcile(None, previous=current.roster, high_water=current.high_water,
                        now=NOW + timedelta(minutes=5))
    assert expired.expirations == (current.roster.members[0].advertisement,)
    assert expired.roster.members[0].lifecycle_state == "expired"
    assert expired.activations == expired.additions == ()
    assert expired.high_water == current.high_water


@pytest.mark.parametrize("workers", [[], [wire_worker(lifecycle_state="revoked")]])
def test_outage_after_high_water_advance_cannot_retain_revoked_roster(workers):
    current = active_roster()
    revoked = snapshot(workers, generation=2)
    advanced = domain().validate_high_water(
        revoked, current.high_water, cluster_id="cluster-a", issuer_id="issuer-a",
        clock=lambda: NOW,
    )
    # The durable comparison may advance before the new roster is applied.
    # Losing the source at this point must not resurrect the older admission.
    result = reconcile(None, previous=current.roster, high_water=advanced)

    assert result.roster is None
    assert result.drains == (current.roster.members[0].advertisement,)
    assert result.additions == result.activations == ()
    assert result.high_water == advanced
    with pytest.raises(ValueError, match="rollback"):
        reconcile(current.roster.snapshot, previous=current.roster, high_water=advanced)
    recovered = reconcile(revoked, previous=current.roster, high_water=advanced)
    assert recovered.roster.members == ()
    assert recovered.high_water == advanced


def test_outage_requires_exact_high_water_digest_even_at_equal_generation():
    current = active_roster()
    conflicting = replace(current.high_water, digest="0" * 64)

    result = reconcile(None, previous=current.roster, high_water=conflicting)

    assert result.roster is None
    assert result.drains == (current.roster.members[0].advertisement,)
    assert result.activations == ()
    assert result.high_water == conflicting
    with pytest.raises(ValueError, match="high-water"):
        reconcile(current.roster.snapshot, previous=current.roster, high_water=conflicting)


@pytest.mark.parametrize("boundary", ["outage", "candidate", "validation", "result"])
def test_hostile_high_water_subclass_is_rejected_before_equality_dispatch(boundary):
    current = active_roster()
    revoked = snapshot([], generation=2)
    equality_calls = []

    class HostileHighWater(domain().MembershipHighWater):
        def __eq__(self, other):
            equality_calls.append("eq")
            return True

        def __ne__(self, other):
            equality_calls.append("ne")
            return False

    hostile = HostileHighWater("cluster-a", "issuer-a", 2, revoked.digest)
    with pytest.raises(ValueError, match="high-water"):
        if boundary == "outage":
            reconcile(None, previous=current.roster, high_water=hostile)
        elif boundary == "candidate":
            reconcile(revoked, previous=current.roster, high_water=hostile)
        elif boundary == "validation":
            domain().validate_high_water(revoked, hostile, cluster_id="cluster-a",
                                        issuer_id="issuer-a", clock=lambda: NOW)
        else:
            domain().MembershipReconciliation(None, hostile)
    assert equality_calls == []


def test_normal_high_water_retention_never_dispatches_record_equality(monkeypatch):
    current = active_roster()
    monkeypatch.setattr(domain().MembershipHighWater, "__eq__",
                        lambda *_: pytest.fail("record equality used for authority"))

    result = reconcile(None, previous=current.roster, high_water=current.high_water)

    assert result.high_water is current.high_water
    assert result.roster.members[0].lifecycle_state == "active"


@pytest.mark.parametrize("name", ["cluster_id", "issuer_id", "digest"])
def test_high_water_fields_reject_equality_overriding_string_subclasses(name):
    class HostileString(str):
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

    values = dict(cluster_id="cluster-a", issuer_id="issuer-a", generation=1, digest="a" * 64)
    values[name] = HostileString(values[name])
    with pytest.raises(ValueError):
        domain().MembershipHighWater(**values)


@pytest.mark.parametrize("kind", ["advertisement", "evidence", "member", "roster"])
def test_other_membership_boundaries_reject_domain_subclasses(kind):
    current = active_roster()
    member = current.roster.members[0]
    value = {
        "advertisement": member.advertisement,
        "evidence": member.evidence,
        "member": member,
        "roster": current.roster,
    }[kind]
    subtype = type("Hostile" + type(value).__name__, (type(value),), {
        "__eq__": lambda *_: True, "__ne__": lambda *_: False,
    })
    hostile = subtype(**{field.name: getattr(value, field.name) for field in fields(value)})
    with pytest.raises(ValueError):
        if kind == "advertisement":
            domain().RosterMember(hostile, "probation")
        elif kind == "evidence":
            reconcile(current.roster.snapshot, previous=current.roster, high_water=current.high_water,
                      capability_evidence=(hostile,))
        elif kind == "member":
            domain().MembershipRoster(current.roster.snapshot, (hostile,), 1)
        else:
            reconcile(None, previous=hostile, high_water=current.high_water)


def test_snapshot_factory_rejects_subclasses_that_could_override_field_equality():
    class HostileSnapshot(domain().MembershipSnapshot):
        def __eq__(self, other):
            return True

    with pytest.raises(ValueError):
        HostileSnapshot.from_signed_envelope(envelope(), verify=verify)


def test_clock_rejects_datetime_subclasses_with_overridden_comparisons():
    class HostileDatetime(datetime):
        def __lt__(self, other):
            return True

        def __le__(self, other):
            return True

    with pytest.raises(ValueError):
        reconcile(snapshot(), now=HostileDatetime(2026, 9, 7, 12, tzinfo=timezone.utc))


@pytest.mark.parametrize("kind", ["sequence", "model", "state"])
def test_member_comparison_fields_cannot_retain_equality_overriding_subclasses(kind):
    class HostileString(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            return True

    class HostileTuple(tuple):
        def __eq__(self, other):
            return True

    changes = {
        "sequence": {"models": HostileTuple(("safe:latest",))},
        "model": {"models": (HostileString("safe:latest"),)},
        "state": {"lifecycle_state": HostileString("active")},
    }[kind]
    with pytest.raises(ValueError):
        domain().WorkerAdvertisement(**wire_worker(**changes))


def test_source_outage_cannot_add_previously_omitted_members_when_limit_grows():
    current = reconcile(snapshot([wire_worker("worker-a"), wire_worker("worker-b")]), max_workers=1)
    result = reconcile(None, previous=current.roster, high_water=current.high_water, max_workers=2)
    assert len(result.roster.members) == 1
    assert result.additions == ()
    assert result.omitted_worker_count == 1


def test_renewal_after_prior_snapshot_expiry_requires_new_probation():
    first = reconcile(snapshot())
    worker = first.roster.members[0].advertisement
    current = reconcile(first.roster.snapshot, previous=first.roster, high_water=first.high_water,
                        capability_evidence=(evidence(worker, expires_at=NOW + timedelta(minutes=10)),))
    renewed = snapshot(generation=2, issued_at="2026-09-07T12:05:00Z", expires_at="2026-09-07T12:10:00Z")
    result = reconcile(renewed, previous=current.roster, high_water=current.high_water,
                       now=NOW + timedelta(minutes=5))
    assert result.roster.members[0].lifecycle_state == "probation"
    assert result.activations == ()


def test_capability_evidence_cannot_outlive_the_existing_ttl_ceiling():
    with pytest.raises(ValueError, match="capability"):
        evidence(snapshot().workers[0], expires_at=NOW + timedelta(seconds=86401))


def test_stale_capability_does_not_remain_active_during_membership_outage():
    current = active_roster()
    result = reconcile(None, previous=current.roster, high_water=current.high_water,
                       now=NOW + timedelta(minutes=1))
    assert result.roster.members[0].lifecycle_state == "probation"
    assert result.activations == ()


def test_unhealthy_advertisement_cannot_be_activated_by_capability_evidence():
    first = reconcile(snapshot([wire_worker(lifecycle_state="unhealthy")]))
    result = reconcile(first.roster.snapshot, previous=first.roster, high_water=first.high_water,
                       capability_evidence=(evidence(first.roster.members[0].advertisement),))
    assert result.roster.members[0].lifecycle_state == "unhealthy"
    assert result.activations == ()


@pytest.mark.parametrize("limit", [16, 64, 256])
def test_active_roster_is_bounded_and_selection_is_deterministic(limit):
    workers = [wire_worker("w%04d" % i) for i in reversed(range(limit + 1))]
    result = reconcile(snapshot(workers), max_workers=limit)
    assert len(result.roster.members) == limit
    assert result.omitted_worker_count == 1
    assert result.additions[0].worker_id == "w0000"
    assert result.additions[-1].worker_id == "w%04d" % (limit - 1)


@pytest.mark.parametrize("limit", [0, 257, True])
def test_roster_limit_cannot_exceed_static_bound(limit):
    with pytest.raises(ValueError):
        reconcile(snapshot(), max_workers=limit)


def test_previous_roster_cannot_bypass_high_water_and_authority():
    current = reconcile(snapshot())
    with pytest.raises(ValueError, match="high-water"):
        reconcile(snapshot(generation=2), previous=current.roster)
    with pytest.raises(ValueError):
        reconcile(None, previous=current.roster,
                  high_water=replace(current.high_water, issuer_id="wrong-issuer"))


@pytest.mark.parametrize("changes", [{"roster": {}}, {"high_water": {}}])
def test_reconciliation_cannot_hold_mutable_unvalidated_authority(changes):
    with pytest.raises(ValueError):
        domain().MembershipReconciliation(**(dict(roster=None, high_water=None) | changes))


@pytest.mark.parametrize("changes", [
    None, {"cluster_id": "other-cluster"}, {"issuer_id": "other-issuer"},
    {"generation": 2}, {"digest": "0" * 64},
])
def test_reconciliation_roster_requires_matching_high_water(changes):
    current = active_roster()
    high_water = replace(current.high_water, **changes) if changes is not None else None
    with pytest.raises(ValueError, match="high-water"):
        domain().MembershipReconciliation(current.roster, high_water)


def test_reconciliation_empty_roster_still_requires_matching_high_water():
    current = reconcile(snapshot([]))
    with pytest.raises(ValueError, match="high-water"):
        domain().MembershipReconciliation(current.roster, replace(current.high_water, generation=2))


@pytest.mark.parametrize("has_high_water", [False, True])
def test_reconciliation_without_roster_preserves_valid_high_water_and_drains(has_high_water):
    current = active_roster()
    high_water = current.high_water if has_high_water else None
    drains = (current.roster.members[0].advertisement,)

    result = domain().MembershipReconciliation(None, high_water, drains=drains)

    assert result.roster is None
    assert result.high_water is high_water
    assert result.drains == drains
    assert result.roster_generation == 0


@pytest.mark.parametrize("matching", [False, True])
def test_reconciliation_binding_never_dispatches_record_equality(monkeypatch, matching):
    current = active_roster()
    high_water = current.high_water if matching else replace(current.high_water, digest="0" * 64)
    for record_type in (domain().MembershipHighWater, domain().MembershipSnapshot):
        for comparison in ("__eq__", "__ne__"):
            monkeypatch.setattr(record_type, comparison,
                                lambda *_: pytest.fail("record equality used for authority"))

    if matching:
        result = domain().MembershipReconciliation(current.roster, high_water)
        assert result.roster is current.roster
        assert result.high_water is high_water
    else:
        with pytest.raises(ValueError, match="high-water"):
            domain().MembershipReconciliation(current.roster, high_water)


@pytest.mark.parametrize("name", ["roster", "high_water"])
def test_reconciliation_binding_rejects_domain_subclasses_without_equality(name):
    current = active_roster()
    value = getattr(current, name)
    equality_calls = []

    def compare(*_):
        equality_calls.append(True)
        return True

    subtype = type("Hostile" + type(value).__name__, (type(value),), {
        "__eq__": compare, "__ne__": compare,
    })
    hostile = subtype(**{field.name: getattr(value, field.name) for field in fields(value)})
    with pytest.raises(ValueError):
        replace(current, **{name: hostile})
    assert equality_calls == []


def test_reconciliation_copies_action_lists_and_roster_rejects_unverified_members():
    current = reconcile(snapshot())
    additions = list(current.additions)
    result = replace(current, additions=additions)
    additions.clear()
    assert result.additions == current.additions
    with pytest.raises(ValueError):
        domain().RosterMember(current.additions[0], "active")
    foreign = domain().WorkerAdvertisement(**wire_worker("other"))
    with pytest.raises(ValueError):
        domain().MembershipRoster(current.roster.snapshot, (domain().RosterMember(foreign, "probation"),), 1)


@pytest.mark.parametrize("changes", [{"generation": 0}, {"generation": True}, {"digest": "bad"}, {"digest": "A" * 64}])
def test_invalid_persisted_high_water_record_is_rejected(changes):
    current = reconcile(snapshot())
    with pytest.raises(ValueError):
        replace(current.high_water, **changes)


@pytest.mark.parametrize("changes", [
    {"max_advertisements": 0}, {"max_advertisements": 4097}, {"max_bytes": 0},
    {"max_bytes": 1_048_577}, {"timeout_seconds": 0}, {"timeout_seconds": 31},
    {"timeout_seconds": float("nan")}, {"max_advertisements": True},
])
def test_source_request_limits_cannot_be_broadened(changes):
    port = importlib.import_module("sonder_runtime.application.ports.inference_membership")
    with pytest.raises(ValueError):
        port.MembershipSourceLimits(**changes)
