"""Reservation ownership must survive duplicate cleanup and fixture resets."""
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import master_orchestrator as mo
from sonder_runtime.adapters.persistence import fleet_store


def setup_function():
    mo.reset_for_tests()


def test_reset_discards_old_reservations_without_releasing_new_agent():
    old_ids = [mo._new_agent("agent", "old fixture") for _ in range(25)]
    assert mo.reserved_slot_count() == 25

    mo.reset_for_tests()
    assert mo.reserved_slot_count() == 0
    survivor = mo._new_agent("agent", "new fixture")
    assert mo.reserved_slot_count() == 1
    for old_id in old_ids:
        mo._finish(old_id, output="late old completion")
    assert mo.reserved_slot_count() == 1
    mo._finish(survivor, output="done")
    assert mo.reserved_slot_count() == 0


def test_repeated_finish_preserves_another_live_reservation():
    completed = mo._new_agent("agent", "finishing")
    survivor = mo._new_agent("agent", "still running")
    assert mo._start_agent(survivor, "blocked call", in_model_call=True)
    ready = threading.Barrier(3)

    def finish():
        ready.wait(5)
        return mo._finish(completed, output="done")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finish) for _ in range(2)]
        ready.wait(5)
        assert [future.result(5) for future in futures] == ["done", "done"]
    assert mo.reserved_slot_count() == 1
    mo._finish("unknown-agent", output="ignored")
    assert mo.reserved_slot_count() == 1
    mo._finish(survivor, output="done")
    assert mo.reserved_slot_count() == 0


@pytest.mark.parametrize("objectives", [(), ("objective",)], ids=["plain", "provenance"])
def test_external_queued_cancel_releases_without_executing_worker(monkeypatch, objectives):
    agent_id = mo._new_agent("agent", "queued")
    fleet_store.cancel_agents(agent_id)  # Bypass the process-local request hook.
    assert mo.reserved_slot_count() == 1
    before = fleet_store.get_agent(agent_id)

    def forbidden(*_args, **_kwargs):
        pytest.fail("cancelled queued work must not execute or validate")

    monkeypatch.setattr(mo.fleet_provenance, "validate_delegation", forbidden)
    assert mo._run_worker(agent_id, "prompt", forbidden, objectives=objectives) == "CANCELLED"
    assert mo.reserved_slot_count() == 0
    after = fleet_store.get_agent(agent_id)
    assert after["status"] == "cancelled"
    assert after["finished_ts"] == before["finished_ts"]


def test_failed_duplicate_start_retains_running_agent_reservation():
    agent_id = mo._new_agent("agent", "running")
    assert mo._start_agent(agent_id, "model call", in_model_call=True)
    assert not mo._start_agent(agent_id, "duplicate start", in_model_call=True)
    assert mo.reserved_slot_count() == 1
    assert fleet_store.get_agent(agent_id)["status"] == "running"
    mo._finish(agent_id, output="done")
    assert mo.reserved_slot_count() == 0


@pytest.mark.parametrize("row_state", ["foreign", "missing", "unreadable"])
def test_unproved_start_refusal_retains_reservation(monkeypatch, row_state):
    agent_id = mo._new_agent("agent", "possibly live")
    monkeypatch.setattr(fleet_store, "start_agent", lambda *_args, **_kwargs: None)

    def lookup(_agent_id):
        if row_state == "unreadable":
            raise OSError("ledger unavailable")
        if row_state == "missing":
            return None
        return {"id": agent_id, "owner_id": "other-owner", "status": "cancelled",
                "in_model_call": False}

    with monkeypatch.context() as patch:
        patch.setattr(fleet_store, "get_agent", lookup)
        assert not mo._start_agent(agent_id, "start refused")
        assert mo.reserved_slot_count() == 1
    mo._finish(agent_id, output="done")
    assert mo.reserved_slot_count() == 0


def test_creation_failure_preserves_existing_reservation(monkeypatch):
    survivor = mo._new_agent("agent", "existing")

    def fail(*_args, **_kwargs):
        raise OSError("creation unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(fleet_store, "create_agent", fail)
        with pytest.raises(OSError, match="creation unavailable"):
            mo._new_agent("agent", "failed creation")
    assert mo.reserved_slot_count() == 1
    mo._finish(survivor, output="done")
    assert mo.reserved_slot_count() == 0


def test_local_id_collision_does_not_release_existing_reservation(monkeypatch):
    survivor = mo._new_agent("agent", "existing")
    suffix = survivor.removeprefix("agent-")
    uuid_proxy = SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=suffix + "0" * 20))
    monkeypatch.setattr(mo, "uuid", uuid_proxy)
    with pytest.raises(RuntimeError, match="fleet agent ID collision"):
        mo._new_agent("agent", "colliding creation")
    assert mo.reserved_slot_count() == 1
    mo._finish(survivor, output="done")
    assert mo.reserved_slot_count() == 0


def test_inherited_queued_cancellation_has_no_reservation():
    parent = mo._new_agent("master", "parent")
    mo.request_cancel(parent)
    assert mo.reserved_slot_count() == 0
    child = mo._new_agent("agent", "late child", parent_id=parent)
    assert fleet_store.get_agent(child)["status"] == "cancelled"
    assert mo.reserved_slot_count() == 0
    mo._finish(child)
    mo._finish(parent)
    assert mo.reserved_slot_count() == 0


def test_reload_preserves_exact_ownership_and_single_release():
    agent_id = mo._new_agent("agent", "survive reload")
    survivor = mo._new_agent("agent", "second live agent")
    ownership = mo._RESERVED_AGENT_IDS
    reloaded = importlib.reload(mo)
    assert reloaded._RESERVED_AGENT_IDS is ownership
    assert reloaded.reserved_slot_count() == 2
    reloaded._finish(agent_id, output="done")
    reloaded._finish(agent_id, output="again")
    assert reloaded.reserved_slot_count() == 1
    reloaded._finish(survivor, output="done")
    assert reloaded.reserved_slot_count() == 0


def test_first_legacy_reload_preserves_unidentified_surplus(monkeypatch):
    agent_id = mo._new_agent("agent", "known queued agent")
    ownership = mo._RESERVED_AGENT_IDS
    try:
        with mo._LOCK:
            mo._RESERVED_SLOTS = 5  # One known ID plus four legacy reservations.
        with monkeypatch.context() as patch:
            patch.delattr(mo, "_RESERVED_AGENT_IDS")
            reloaded = importlib.reload(mo)
            assert agent_id in reloaded._RESERVED_AGENT_IDS
            assert reloaded.reserved_slot_count() == 5
            reloaded._finish(agent_id, output="done")
            assert reloaded.reserved_slot_count() == 4
            reloaded.reset_for_tests()
    finally:
        ownership.clear()
        mo.reset_for_tests()
    assert mo.reserved_slot_count() == 0


def test_abandon_after_finish_error_preserves_unrelated_reservation(monkeypatch):
    survivor = mo._new_agent("agent", "unrelated reservation")
    monkeypatch.setattr(mo, "max_agents", lambda: 64)
    monkeypatch.setattr(mo, "parallel_worker_slots", lambda _requested: 1)
    monkeypatch.setattr(mo, "physical_memory_bytes", lambda: (64 * 1024 ** 3, 64 * 1024 ** 3))
    monkeypatch.setattr(mo, "gpu_worker_slots", lambda: 0)
    monkeypatch.setattr(mo, "gpu_memory_bytes", lambda: (0, 0))
    monkeypatch.setattr(mo, "fleet_model_bytes", lambda: 0)
    monkeypatch.setattr(mo, "ollama_parallel_limit", lambda: 0)
    real_finish = mo._finish
    doomed = {}

    def finish(agent_id, *args, **kwargs):
        result = real_finish(agent_id, *args, **kwargs)
        if kwargs.get("error") and not doomed:
            doomed["id"] = agent_id
        if doomed.get("id") == agent_id:
            raise OSError("event failure after the reservation was released")
        return result

    def worker(prompt):
        if "subagent 1/" in prompt:
            raise ValueError("permanent worker failure")
        return "ok"

    with monkeypatch.context() as patch:
        patch.setattr(mo, "_finish", finish)
        result = mo.run_delegated("fan out", worker, lambda _prompt: "merged", agents=3)
    assert result["output"] == "merged"
    assert result["concurrency"]["handler_failures"] == 1
    assert mo.reserved_slot_count() == 1
    mo._finish(survivor, output="done")
    assert mo.reserved_slot_count() == 0
