"""The shared SQLite store: ordering, reclaim, pins, footprints, fail-closed state."""
from __future__ import annotations

import multiprocessing
import sqlite3
import subprocess
import sys
import time

import pytest

from sonder_runtime.adapters.inference import gpu_admission_store as store_module
from sonder_runtime.adapters.inference.gpu_admission_store import (
    FULL, HELD, WAITING, GpuAdmissionStore, StoreUnavailable,
)

ORIGIN = "http://127.0.0.1:11434"


@pytest.fixture
def store(tmp_path):
    return GpuAdmissionStore(tmp_path / "gpu.sqlite3")


def _join(store, ticket, *, slots=1, priority=0, pool="gpu", ttl=60, depth=8, model="m"):
    return store.acquire(ticket, pool, slots=slots, priority=priority, origin=ORIGIN,
                         model_key=model, ttl_seconds=ttl, max_depth=depth)


def test_first_ticket_holds_and_the_second_waits_until_release(store):
    assert _join(store, "a") == HELD
    assert _join(store, "b") == WAITING
    assert _join(store, "b") == WAITING
    store.release("a")
    assert _join(store, "b") == HELD


def test_slots_bound_the_holders(store):
    assert [_join(store, t, slots=2) for t in ("a", "b", "c")] == [HELD, HELD, WAITING]


def test_interactive_waiters_jump_background_waiters_and_fifo_within_a_class(store):
    assert _join(store, "holder") == HELD
    assert _join(store, "bg1", priority=1) == WAITING
    assert _join(store, "bg2", priority=1) == WAITING
    assert _join(store, "ui", priority=0) == WAITING
    store.release("holder")
    assert _join(store, "bg1", priority=1) == WAITING
    assert _join(store, "bg2", priority=1) == WAITING
    assert _join(store, "ui", priority=0) == HELD
    store.release("ui")
    assert _join(store, "bg2", priority=1) == WAITING
    assert _join(store, "bg1", priority=1) == HELD


def test_the_embed_pool_is_independent_of_the_gpu_pool(store):
    assert _join(store, "chat", pool="gpu") == HELD
    assert _join(store, "embed", pool="embed") == HELD


def test_queue_depth_is_bounded(store):
    assert _join(store, "a", depth=2) == HELD
    assert _join(store, "b", depth=2) == WAITING
    assert _join(store, "c", depth=2) == WAITING
    assert _join(store, "d", depth=2) == FULL


def test_a_holder_whose_process_is_dead_is_reclaimed(tmp_path):
    states = {"alive": True}

    def liveness(pid, identity=None):
        return ("alive" if states["alive"] else "dead"), identity

    ghost = GpuAdmissionStore(tmp_path / "gpu.sqlite3", pid=424242, liveness=liveness)
    live = GpuAdmissionStore(tmp_path / "gpu.sqlite3", liveness=liveness)
    assert _join(ghost, "ghost") == HELD
    assert _join(live, "me") == WAITING
    states["alive"] = False
    assert _join(live, "me") == HELD
    assert [lease.ticket_id for lease in live.leases()] == ["me"]


def test_a_holder_whose_process_is_alive_is_never_reclaimed(tmp_path):
    ghost = GpuAdmissionStore(tmp_path / "gpu.sqlite3", pid=424242,
                              liveness=lambda pid, identity=None: ("alive", identity))
    live = GpuAdmissionStore(tmp_path / "gpu.sqlite3",
                             liveness=lambda pid, identity=None: ("alive", identity))
    assert _join(ghost, "ghost") == HELD
    for _ in range(3):
        assert _join(live, "me") == WAITING


def test_an_unknown_liveness_is_treated_as_alive(tmp_path):
    other = GpuAdmissionStore(tmp_path / "gpu.sqlite3", pid=424242,
                              liveness=lambda pid, identity=None: ("unknown", None))
    live = GpuAdmissionStore(tmp_path / "gpu.sqlite3",
                             liveness=lambda pid, identity=None: ("unknown", None))
    assert _join(other, "other") == HELD
    assert _join(live, "me") == WAITING


def test_an_expired_lease_is_reclaimed_even_if_its_process_looks_alive(tmp_path):
    clock = {"now": 1000.0}
    alive = lambda pid, identity=None: ("alive", identity)  # noqa: E731
    first = GpuAdmissionStore(tmp_path / "gpu.sqlite3", pid=424242, liveness=alive,
                              clock=lambda: clock["now"])
    second = GpuAdmissionStore(tmp_path / "gpu.sqlite3", liveness=alive, clock=lambda: clock["now"])
    assert _join(first, "wedged", ttl=10) == HELD
    assert _join(second, "me", ttl=10) == WAITING
    clock["now"] += 11
    assert _join(second, "me", ttl=10) == HELD


def test_holders_counts_other_tickets_on_the_same_model(store):
    assert _join(store, "a", slots=3, model="x") == HELD
    assert _join(store, "b", slots=3, model="x") == HELD
    assert _join(store, "c", slots=3, model="y") == HELD
    assert store.holders(ORIGIN, "x") == 2
    assert store.holders(ORIGIN, "x", exclude="a") == 1
    assert store.holders(ORIGIN, "y", exclude="c") == 0


def test_pins_round_trip_and_repins_are_counted_within_the_window(tmp_path):
    clock = {"now": 5000.0}
    pins = GpuAdmissionStore(tmp_path / "gpu.sqlite3", clock=lambda: clock["now"])
    assert pins.get_pin(ORIGIN, "m") is None
    pins.set_pin(ORIGIN, "m", 16384)
    assert pins.get_pin(ORIGIN, "m").num_ctx == 16384
    assert pins.repins_in_window(ORIGIN, "m", 600) == 0
    pins.set_pin(ORIGIN, "m", 2048, repin=True, window_seconds=600)
    pins.set_pin(ORIGIN, "m", 4096, repin=True, window_seconds=600)
    assert pins.get_pin(ORIGIN, "m").num_ctx == 4096
    assert pins.repins_in_window(ORIGIN, "m", 600) == 2
    clock["now"] += 601
    assert pins.repins_in_window(ORIGIN, "m", 600) == 0
    pins.set_pin(ORIGIN, "m", 8192, repin=True, window_seconds=600)
    assert pins.repins_in_window(ORIGIN, "m", 600) == 1
    pins.clear_pin(ORIGIN, "m")
    assert pins.get_pin(ORIGIN, "m") is None
    assert pins.pins() == {}


def test_footprints_keep_the_maximum_per_digest_and_window(store):
    store.record_footprint("d", 16384, 10, 20)
    store.record_footprint("d", 16384, 30, 5)
    store.record_footprint("d", 2048, 7, 7)
    rows = {row.num_ctx: row for row in store.footprints_for("d")}
    assert (rows[16384].working_set_bytes, rows[16384].private_bytes) == (30, 20)
    assert (rows[2048].working_set_bytes, rows[2048].private_bytes) == (7, 7)
    assert store.footprints_for("other") == ()


def test_a_corrupt_file_is_unavailable_not_reset(tmp_path):
    path = tmp_path / "gpu.sqlite3"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(StoreUnavailable):
        GpuAdmissionStore(path).verify()
    assert path.read_bytes().startswith(b"this is not")


def test_a_wrong_schema_version_is_unavailable(tmp_path):
    path = tmp_path / "gpu.sqlite3"
    GpuAdmissionStore(path).verify()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE gpu_meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(StoreUnavailable, match="schema"):
        GpuAdmissionStore(path).verify()


def test_a_foreign_database_is_unavailable_and_left_untouched(tmp_path):
    path = tmp_path / "gpu.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(x)")
    with pytest.raises(StoreUnavailable, match="schema"):
        GpuAdmissionStore(path).verify()
    with sqlite3.connect(path) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert names == {"unrelated"}


def test_a_symlinked_store_is_refused(tmp_path):
    target = tmp_path / "real.sqlite3"
    GpuAdmissionStore(target).verify()
    link = tmp_path / "link.sqlite3"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(StoreUnavailable, match="symlink"):
        GpuAdmissionStore(link).verify()


def test_a_store_in_a_missing_directory_is_created(tmp_path):
    nested = GpuAdmissionStore(tmp_path / "a" / "b" / "gpu.sqlite3")
    assert _join(nested, "t") == HELD


def _hold_worker(path, name, hold_seconds, result_queue):
    """Child process: take the one slot, hold it, report the interval."""
    child = GpuAdmissionStore(path)
    ticket = "%s-%d" % (name, time.time_ns())
    while True:
        state = child.acquire(ticket, "gpu", slots=1, priority=0, origin=ORIGIN, model_key="m",
                              ttl_seconds=30, max_depth=8)
        if state == HELD:
            break
        time.sleep(0.01)
    started = time.time()
    time.sleep(hold_seconds)
    ended = time.time()
    child.release(ticket)
    result_queue.put((name, started, ended))


def test_two_processes_never_hold_the_single_slot_at_once(tmp_path):
    path = tmp_path / "gpu.sqlite3"
    GpuAdmissionStore(path).verify()
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    workers = [context.Process(target=_hold_worker, args=(str(path), name, 0.5, results))
               for name in ("one", "two")]
    for worker in workers:
        worker.start()
    intervals = sorted((results.get(timeout=60)[1:] for _ in workers))
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    (_s1, end_first), (start_second, _e2) = intervals
    assert start_second >= end_first - 0.02, "hold intervals overlapped: %r" % (intervals,)


def test_a_killed_holder_process_is_reclaimed_by_pid_liveness(tmp_path):
    path = tmp_path / "gpu.sqlite3"
    script = (
        "import sys, time\n"
        "from sonder_runtime.adapters.inference.gpu_admission_store import GpuAdmissionStore\n"
        "s = GpuAdmissionStore(sys.argv[1])\n"
        "print(s.acquire('doomed', 'gpu', slots=1, priority=0, origin=%r, model_key='m',"
        " ttl_seconds=600, max_depth=8), flush=True)\n"
        "time.sleep(600)\n" % ORIGIN
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout.readline().strip() == HELD
        survivor = GpuAdmissionStore(path)
        assert _join(survivor, "me") == WAITING
        child.kill()
        child.wait(30)
        deadline = time.monotonic() + 15
        state = WAITING
        while state != HELD and time.monotonic() < deadline:
            state = _join(survivor, "me")
            time.sleep(0.05)
        assert state == HELD
        assert [lease.ticket_id for lease in survivor.leases()] == ["me"]
    finally:
        if child.poll() is None:
            child.kill()
        child.stdout.close()


def test_module_constants_are_stable():
    assert store_module.SCHEMA_VERSION == "1"
