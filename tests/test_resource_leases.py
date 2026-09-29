"""Durable leases on external resources (domain/resource_leases.py, adapters/resource_leases.py)."""
from __future__ import annotations

import socket

import pytest

from sonder_runtime.adapters.process_liveness import PROCESS_ALIVE, PROCESS_DEAD, PROCESS_UNKNOWN
from sonder_runtime.adapters.resource_leases import SqliteResourceLeaseRegistry, port_is_free
from sonder_runtime.domain.common.errors import Conflict, Forbidden, InvalidInput, NotFound
from sonder_runtime.domain.resource_leases import (
    KIND_DESKTOP_SESSION, KIND_DEV_SERVER, KIND_PORT, KIND_TEMP_DB, RESOURCE_LEASE_BUSY,
    RESOURCE_LEASE_EXHAUSTED, RESOURCE_LEASE_NOT_FOUND, RESOURCE_LEASE_NOT_OWNER,
    parse_port_range, reclaim_decision,
)


class Rig:
    def __init__(self, tmp_path):
        self.now = 1000.0
        self.states: dict[int, str] = {}
        self.busy_ports: set[int] = set()
        self.paths: set[str] = set()
        self.db = tmp_path / "leases.sqlite3"

    def probe(self, pid, identity=None):
        return self.states.get(pid, PROCESS_ALIVE), None

    def registry(self, **kw):
        kw.setdefault("port_range", (47000, 47003))
        return SqliteResourceLeaseRegistry(
            self.db, clock=lambda: self.now, probe=self.probe,
            port_free=lambda p: p not in self.busy_ports,
            path_exists=lambda p: p in self.paths, **kw)


@pytest.fixture()
def rig(tmp_path):
    return Rig(tmp_path)


# -- acquire / refuse / release --------------------------------------------

def test_acquire_refuses_a_second_owner_and_release_frees_it(rig):
    reg = rig.registry()
    lease = reg.acquire(KIND_DEV_SERVER, "web", "worker-a", ttl_seconds=60, owner_pid=11)
    assert reg.holder(KIND_DEV_SERVER, "web").lease_id == lease.lease_id
    with pytest.raises(Conflict) as err:
        reg.acquire(KIND_DEV_SERVER, "web", "worker-b", ttl_seconds=60, owner_pid=12)
    assert err.value.code == RESOURCE_LEASE_BUSY and "worker-a" in str(err.value)
    assert reg.release(KIND_DEV_SERVER, "web", "worker-a") is True
    assert reg.holder(KIND_DEV_SERVER, "web") is None
    assert reg.acquire(KIND_DEV_SERVER, "web", "worker-b", ttl_seconds=60).owner_id == "worker-b"


def test_only_the_owner_may_release_or_heartbeat(rig):
    reg = rig.registry()
    reg.acquire(KIND_TEMP_DB, "scratch", "worker-a", ttl_seconds=60)
    with pytest.raises(Forbidden) as err:
        reg.release(KIND_TEMP_DB, "scratch", "worker-b")
    assert err.value.code == RESOURCE_LEASE_NOT_OWNER
    with pytest.raises(Forbidden):
        reg.heartbeat(KIND_TEMP_DB, "scratch", "worker-b")
    assert reg.holder(KIND_TEMP_DB, "scratch").owner_id == "worker-a"
    assert reg.release(KIND_TEMP_DB, "nothing-here", "worker-a") is False
    with pytest.raises(NotFound) as err:
        reg.heartbeat(KIND_TEMP_DB, "nothing-here", "worker-a")
    assert err.value.code == RESOURCE_LEASE_NOT_FOUND


def test_same_owner_reacquire_renews_instead_of_refusing(rig):
    reg = rig.registry()
    first = reg.acquire(KIND_DEV_SERVER, "web", "worker-a", ttl_seconds=60)
    rig.now += 30
    again = reg.acquire(KIND_DEV_SERVER, "web", "worker-a", ttl_seconds=60, metadata={"pid": 5})
    assert again.lease_id == first.lease_id and again.expires_at == rig.now + 60
    assert again.metadata == {"pid": 5}


def test_input_is_validated(rig):
    reg = rig.registry()
    for bad in (dict(kind="Port"), dict(kind="port", key="http"), dict(ttl_seconds=0),
                dict(owner_id=""), dict(metadata={"secret": object()})):
        args = dict(kind=KIND_DEV_SERVER, key="k", owner_id="o", ttl_seconds=10)
        args.update(bad)
        with pytest.raises(InvalidInput):
            reg.acquire(args.pop("kind"), args.pop("key"), args.pop("owner_id"), **args)
    with pytest.raises(InvalidInput):
        parse_port_range("80-90")
    assert parse_port_range(" 47000 - 47010 ") == (47000, 47010)


def test_custom_kinds_are_accepted(rig):
    reg = rig.registry()
    reg.acquire("gpu_slot", "0", "worker-a", ttl_seconds=5)
    with pytest.raises(Conflict):
        reg.acquire("gpu_slot", "0", "worker-b", ttl_seconds=5)


# -- TTL + heartbeat ----------------------------------------------------------

def test_heartbeat_keeps_a_lease_alive_past_its_original_ttl(rig):
    reg = rig.registry()
    reg.acquire(KIND_DEV_SERVER, "web", "worker-a", ttl_seconds=10, metadata={"pid": 77})
    rig.states[77] = PROCESS_DEAD  # cleanup evidence exists, but the lease is live
    for _ in range(5):
        rig.now += 8
        reg.heartbeat(KIND_DEV_SERVER, "web", "worker-a")
    with pytest.raises(Conflict, match="live owner"):
        reg.acquire(KIND_DEV_SERVER, "web", "worker-b", ttl_seconds=10)


def test_expired_ttl_alone_does_not_reclaim_without_cleanup_evidence(rig):
    reg = rig.registry()
    reg.acquire(KIND_DEV_SERVER, "web", "worker-a", ttl_seconds=10, metadata={"pid": 77})
    rig.now += 11
    with pytest.raises(Conflict, match="nothing proves"):
        reg.acquire(KIND_DEV_SERVER, "web", "worker-b", ttl_seconds=10)
    rig.states[77] = PROCESS_UNKNOWN  # an undecidable probe is not evidence
    with pytest.raises(Conflict):
        reg.acquire(KIND_DEV_SERVER, "web", "worker-b", ttl_seconds=10)
    rig.states[77] = PROCESS_DEAD
    assert reg.acquire(KIND_DEV_SERVER, "web", "worker-b", ttl_seconds=10).owner_id == "worker-b"


def test_lease_with_no_evidence_keys_is_never_reclaimed(rig):
    reg = rig.registry()
    reg.acquire("browser_tab", "tab-1", "worker-a", ttl_seconds=10, owner_pid=5)
    rig.states[5] = PROCESS_DEAD
    rig.now += 100
    with pytest.raises(Conflict, match="nothing proves"):
        reg.acquire("browser_tab", "tab-1", "worker-b", ttl_seconds=10)
    assert reg.sweep() == ()


# -- dead-owner reclaim -------------------------------------------------------

def test_dead_owner_is_reclaimed_inside_ttl_only_with_cleanup_evidence(rig):
    reg = rig.registry()
    rig.paths.add("/tmp/db-a")
    reg.acquire(KIND_TEMP_DB, "db", "worker-a", ttl_seconds=600, owner_pid=41,
                metadata={"path": "/tmp/db-a"})
    rig.states[41] = PROCESS_DEAD
    with pytest.raises(Conflict, match="owner is dead, but nothing proves"):
        reg.acquire(KIND_TEMP_DB, "db", "worker-b", ttl_seconds=60)
    rig.paths.clear()  # the temp database directory is gone
    assert reg.acquire(KIND_TEMP_DB, "db", "worker-b", ttl_seconds=60).owner_id == "worker-b"


def test_unknown_owner_liveness_is_treated_as_alive(rig):
    reg = rig.registry()
    reg.acquire(KIND_TEMP_DB, "db", "worker-a", ttl_seconds=600, owner_pid=41,
                metadata={"path": "/gone"})
    rig.states[41] = PROCESS_UNKNOWN
    with pytest.raises(Conflict, match="live owner"):
        reg.acquire(KIND_TEMP_DB, "db", "worker-b", ttl_seconds=60)


def test_sweep_drops_only_provably_released_leases(rig):
    reg = rig.registry()
    reg.acquire(KIND_DEV_SERVER, "dead", "a", ttl_seconds=600, owner_pid=1, metadata={"pid": 2})
    reg.acquire(KIND_DEV_SERVER, "live", "b", ttl_seconds=600, owner_pid=3, metadata={"pid": 4})
    rig.states.update({1: PROCESS_DEAD, 2: PROCESS_DEAD, 4: PROCESS_DEAD})
    dropped = reg.sweep()
    assert [lease.key for lease in dropped] == ["dead"]
    assert [lease.key for lease in reg.leases()] == ["live"]


def test_reclaim_rule_table():
    assert not reclaim_decision(owner_dead=False, ttl_expired=False, cleanup_evidence=True).reclaim
    assert not reclaim_decision(owner_dead=True, ttl_expired=False, cleanup_evidence=False).reclaim
    assert not reclaim_decision(owner_dead=False, ttl_expired=True, cleanup_evidence=False).reclaim
    assert reclaim_decision(owner_dead=True, ttl_expired=False, cleanup_evidence=True).reclaim
    assert reclaim_decision(owner_dead=False, ttl_expired=True, cleanup_evidence=True).reclaim


# -- ports ------------------------------------------------------------------

def test_acquire_port_skips_leased_and_busy_ports(rig):
    reg = rig.registry()
    first = reg.acquire_port("worker-a", ttl_seconds=60)
    assert first.key == "47000"
    rig.busy_ports.add(47001)  # something outside the registry listens here
    second = reg.acquire_port("worker-b", ttl_seconds=60)
    assert second.key == "47002"
    with pytest.raises(Conflict, match="outside the lease registry"):
        reg.acquire(KIND_PORT, "47001", "worker-c", ttl_seconds=60)
    reg.acquire_port("worker-c", ttl_seconds=60)
    with pytest.raises(Conflict) as err:
        reg.acquire_port("worker-d", ttl_seconds=60)
    assert err.value.code == RESOURCE_LEASE_EXHAUSTED


def test_expired_port_lease_is_reclaimed_only_once_the_port_is_free(rig):
    reg = rig.registry(port_range=(47000, 47000))
    reg.acquire_port("worker-a", ttl_seconds=10)
    rig.now += 11
    rig.busy_ports.add(47000)  # the dead holder's server still listens
    with pytest.raises(Conflict):
        reg.acquire_port("worker-b", ttl_seconds=10)
    rig.busy_ports.clear()
    assert reg.acquire_port("worker-b", ttl_seconds=10).owner_id == "worker-b"


def test_real_bind_test_sees_a_listening_socket(tmp_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert port_is_free(port) is False
        reg = SqliteResourceLeaseRegistry(tmp_path / "l.sqlite3", port_range=(port, port))
        with pytest.raises(Conflict):
            reg.acquire(KIND_PORT, str(port), "worker-a", ttl_seconds=10)
        with pytest.raises(Conflict):
            reg.acquire_port("worker-a", ttl_seconds=10)
    finally:
        listener.close()
    assert port_is_free(port) is True
    reg = SqliteResourceLeaseRegistry(tmp_path / "l.sqlite3", port_range=(port, port))
    assert reg.acquire_port("worker-a", ttl_seconds=10).key == str(port)


# -- persistence -------------------------------------------------------------

def test_leases_survive_a_restart_and_are_shared_between_registries(rig):
    reg = rig.registry()
    held = reg.acquire(KIND_DESKTOP_SESSION, "console", "worker-a", ttl_seconds=60,
                       owner_pid=9, owner_identity="id-9", metadata={"pid": 10})
    del reg
    reopened = rig.registry()  # a new process opening the same file
    back = reopened.holder(KIND_DESKTOP_SESSION, "console")
    assert back == held
    with pytest.raises(Conflict):
        reopened.acquire(KIND_DESKTOP_SESSION, "console", "worker-b", ttl_seconds=60)
    assert reopened.release(KIND_DESKTOP_SESSION, "console", "worker-a")
    assert rig.registry().holder(KIND_DESKTOP_SESSION, "console") is None


def test_owner_identity_is_passed_to_the_liveness_probe(rig):
    seen = []

    def probe(pid, identity=None):
        seen.append((pid, identity))
        return (PROCESS_DEAD, "other") if identity == "old" else (PROCESS_ALIVE, identity)

    reg = SqliteResourceLeaseRegistry(rig.db, clock=lambda: rig.now, probe=probe,
                                      path_exists=lambda p: False)
    reg.acquire(KIND_TEMP_DB, "db", "a", ttl_seconds=600, owner_pid=5, owner_identity="old",
                metadata={"path": "/gone"})
    assert reg.acquire(KIND_TEMP_DB, "db", "b", ttl_seconds=60).owner_id == "b"
    assert (5, "old") in seen


def test_bind_test_sees_a_wildcard_listener():
    # On Windows a 127.0.0.1 bind alone succeeds beside a 0.0.0.0 listener.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("0.0.0.0", 0))
    listener.listen(1)
    try:
        assert port_is_free(listener.getsockname()[1]) is False
    finally:
        listener.close()
