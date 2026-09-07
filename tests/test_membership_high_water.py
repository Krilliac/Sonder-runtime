"""Durable, atomic replay protection with isolated restart fixtures."""
from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from sonder_runtime.adapters.inference.membership_high_water import MembershipHighWaterStore, MembershipStateError
from sonder_runtime.domain.inference_membership import MembershipSnapshot

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def snapshot(generation=1, *, issuer="issuer", expiry=60, signature="verified", workers=()):
    raw = json.dumps(dict(payload=dict(cluster_id="cluster", issuer_id=issuer, generation=generation,
        protocol_version=1, issued_at=(NOW-timedelta(seconds=120)).isoformat(),
        expires_at=(NOW+timedelta(seconds=expiry)).isoformat(), workers=list(workers)), signature=signature),
        sort_keys=True, separators=(",", ":")).encode("ascii")
    return MembershipSnapshot.from_signed_envelope(raw, verify=lambda _: True)


def store(path):
    return MembershipHighWaterStore(path, cluster_id="cluster", issuer_id="issuer", clock=lambda: NOW)


def test_restart_rejects_rollback_and_equivocation(tmp_path):
    path = tmp_path / "private" / "high-water.json"
    original = store(path)
    assert not path.exists()  # construction is offline and read-free
    water = original.compare_and_advance(snapshot(2))
    restarted = store(path)
    assert restarted.read() == water
    assert restarted.compare_and_advance(snapshot(2)) == water
    for bad in (snapshot(1), snapshot(2, signature="different"), snapshot(2, expiry=-1), snapshot(3, issuer="other")):
        with pytest.raises(MembershipStateError): restarted.compare_and_advance(bad)
        assert restarted.read() == water


def test_high_water_failure_never_downgrades_or_resets(tmp_path, monkeypatch):
    path = tmp_path / "private" / "high-water.json"
    value = store(path)
    water = value.compare_and_advance(snapshot(2))
    def fail(*_a, **_kw): raise OSError("private state path failure")
    monkeypatch.setattr(value, "_commit", fail)
    with pytest.raises(MembershipStateError) as caught:
        value.compare_and_advance(snapshot(3))
    assert str(caught.value) == "membership replay state unavailable"
    assert store(path).read() == water
    foreign = MembershipHighWaterStore(path, cluster_id="changed", issuer_id="issuer", clock=lambda: NOW)
    with pytest.raises(MembershipStateError): foreign.read()
    assert store(path).read() == water


def test_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "private" / "high-water.json"
    store(path).compare_and_advance(snapshot())
    path.write_bytes(b"corrupt private record")
    with pytest.raises(MembershipStateError): store(path).compare_and_advance(snapshot())
    assert path.read_bytes() == b"corrupt private record"


def test_equal_digest_is_idempotent_only_until_its_original_expiry(tmp_path):
    clock = [NOW]
    value = MembershipHighWaterStore(tmp_path / "private" / "high-water.json", cluster_id="cluster", issuer_id="issuer", clock=lambda: clock[0])
    signed = snapshot(2)
    water = value.compare_and_advance(signed)
    assert value.compare_and_advance(signed) == water
    clock[0] += timedelta(seconds=61)
    with pytest.raises(MembershipStateError): value.compare_and_advance(signed)
    assert value.read() == water


def test_record_is_exact_canonical_four_field_json(tmp_path):
    path = tmp_path / "private" / "high-water.json"
    water = store(path).compare_and_advance(snapshot(2))
    expected = dict(cluster="cluster", issuer="issuer", generation=2, digest=water.digest)
    assert path.read_bytes() == json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("ascii")


def test_missing_record_cannot_reset_an_initialized_store_after_restart(tmp_path):
    path = tmp_path / "private" / "high-water.json"
    original = store(path)
    original.compare_and_advance(snapshot(2))
    path.unlink()
    for value in (original, store(path)):
        with pytest.raises(MembershipStateError): value.read()
        with pytest.raises(MembershipStateError): value.compare_and_advance(snapshot(1))
    assert not path.exists()


def test_hardlinked_record_is_rejected_without_writing_other_name(tmp_path):
    path = tmp_path / "private" / "high-water.json"
    store(path).compare_and_advance(snapshot(2))
    alias = tmp_path / "alias"
    os.link(path, alias)
    original = alias.read_bytes()
    with pytest.raises(MembershipStateError): store(path).compare_and_advance(snapshot(3))
    assert alias.read_bytes() == original


@pytest.mark.parametrize("damage", [b"{", b"{}", b" " * 2049,
    b'{"cluster":"cluster","issuer":"issuer","generation":2,"generation":1,"digest":"' + b"0" * 64 + b'"}'])
def test_partial_noncanonical_or_duplicate_record_is_not_repaired(tmp_path, damage):
    path = tmp_path / "private" / "high-water.json"
    store(path).compare_and_advance(snapshot(2))
    path.write_bytes(damage)
    for operation in (lambda: store(path).read(), lambda: store(path).compare_and_advance(snapshot(3))):
        with pytest.raises(MembershipStateError): operation()
        assert path.read_bytes() == damage


def test_os_lock_contention_is_nonblocking_and_preserves_record(tmp_path):
    path = tmp_path / "private" / "high-water.json"
    original = store(path)
    water = original.compare_and_advance(snapshot(2))
    with original._session():
        with pytest.raises(MembershipStateError): store(path).read()
        with pytest.raises(MembershipStateError): store(path).compare_and_advance(snapshot(3))
    assert store(path).read() == water


def test_lock_and_anchor_are_held_through_atomic_publication(tmp_path, monkeypatch):
    path = tmp_path / "private" / "high-water.json"
    original = store(path)
    water = original.compare_and_advance(snapshot(2))
    def tamper(publish):
        with pytest.raises(MembershipStateError): store(path).compare_and_advance(snapshot(1))
        if os.name == "nt":
            with pytest.raises(OSError): (path.parent / "state.lock").rename(path.parent / "moved.lock")
            with pytest.raises(OSError): path.parent.rename(tmp_path / "moved")
        # A privileged owner replacing the record between read and publish
        # cannot cause a commit based on the stale read.
        path.write_bytes(b"partial private record")
        publish()
    monkeypatch.setattr(original, "_commit", tamper)
    with pytest.raises(MembershipStateError): original.compare_and_advance(snapshot(3))
    with pytest.raises(MembershipStateError): store(path).read()
    assert path.read_bytes() == b"partial private record"
    assert water.generation == 2


def test_junction_or_symlink_parent_is_rejected_before_state_creation(tmp_path):
    target, link = tmp_path / "target", tmp_path / "link"
    target.mkdir()
    if os.name == "nt":
        import subprocess
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, timeout=5)
        assert result.returncode == 0
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(MembershipStateError):
        store(link / "private" / "high-water.json").compare_and_advance(snapshot())
    assert list(target.iterdir()) == []


def test_fresh_controller_uses_durable_high_water_before_admission(tmp_path):
    from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool, WorkerPoolUnavailable
    from sonder_runtime.application.inference_membership.controller import MembershipController
    origin = "https://worker.example:11434"
    workers = [dict(worker_id="worker", origin=origin, member_generation=1,
        lifecycle_state="active", models=[], advertised_capacity=1)]
    path = tmp_path / "private" / "high-water.json"
    class Source:
        def __init__(self, candidate): self.candidate = candidate
        def read_snapshot(self, *, limits): return self.candidate
        def close(self, *, timeout): return True
    def build(candidate):
        pool = OllamaWorkerPool(origin, allow_remote=True,
            capability_prober=lambda _: {"models":["code"]})
        value = MembershipController(Source(candidate), pool, clock=lambda: NOW, cluster_id="cluster",
            issuer_id="issuer", high_water_store=store(path))
        return value, pool
    signed = snapshot(2, workers=workers)
    original, pool = build(signed)
    try:
        original._refresh_once(1, True)
        assert pool.request(lambda endpoint: endpoint, model="code") == origin
    finally: assert original.close()
    for bad in (snapshot(1, workers=workers), snapshot(2, workers=workers, signature="conflict"),
                snapshot(2, workers=workers, expiry=-1)):
        restarted, pool = build(bad)
        try:
            restarted._refresh_once(1, True)
            with pytest.raises(WorkerPoolUnavailable): pool.request(lambda _: pytest.fail("replayed worker dispatched"), model="code")
            assert store(path).read().generation == 2
        finally: assert restarted.close()
    restarted, pool = build(signed)
    try:
        restarted._refresh_once(1, False)
        with pytest.raises(WorkerPoolUnavailable): pool.request(lambda _: pytest.fail("evidence survived restart"), model="code")
        restarted._refresh_once(1, True)
        assert pool.request(lambda endpoint: endpoint, model="code") == origin
        restarted._source.candidate = snapshot(3)  # signed removal advances durable authority
        restarted._refresh_once(1, True)
        assert store(path).read().generation == 3
    finally: assert restarted.close()
    restarted, pool = build(signed)
    try:
        restarted._refresh_once(1, True)
        with pytest.raises(WorkerPoolUnavailable): pool.request(lambda _: pytest.fail("revoked worker returned"), model="code")
        assert store(path).read().generation == 3
    finally: assert restarted.close()
