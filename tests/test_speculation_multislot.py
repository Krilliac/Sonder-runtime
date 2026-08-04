"""Multi-slot reorder buffer and per-tool latency tables.

These tests extend the speculation engine coverage without disturbing the
single-in-flight behavior exercised by ``tests/test_speculation.py``.  Every
speculation here injects a fake dispatch so nothing touches a model, the
network, a clock we do not control, or the filesystem outside ``tmp_path``.
Concurrency is proven deterministically with a barrier of events rather than
sleeps: workers block until the test releases them, so "two in flight" is an
observed fact, never a timing race.
"""
from __future__ import annotations

import json
import threading

import pytest

import sonder_speculation
from sonder_speculation import BranchPredictor, SpeculationEngine


@pytest.fixture()
def predictor(tmp_path):
    return BranchPredictor(tmp_path / "predictor.json")


class _Gate:
    """A dispatch that parks each worker until the test releases it.

    Records which tools entered dispatch (proving concurrency) and how many
    are parked at once, so a test can assert 2+ workers ran simultaneously.
    """

    def __init__(self):
        self._release = threading.Event()
        self._entered = threading.Semaphore(0)
        self._lock = threading.Lock()
        self.seen: list[str] = []

    def __call__(self, tool, args):
        with self._lock:
            self.seen.append(tool)
        self._entered.release()
        # Bounded wait so a bug can never hang the suite forever.
        self._release.wait(5)
        return ("observation:%s" % tool), True

    def wait_entered(self, n: int, timeout: float = 5.0) -> bool:
        for _ in range(n):
            if not self._entered.acquire(timeout=timeout):
                return False
        return True

    def release(self):
        self._release.set()


# -- bounded reorder buffer: concurrency -------------------------------------

def test_multislot_runs_two_speculations_concurrently(predictor):
    gate = _Gate()
    engine = SpeculationEngine(predictor, gate, slots=2)

    assert engine.begin("workspace_inventory", "sig-1", {}) is True
    assert engine.begin("directory_tree", "sig-2", {}) is True
    # Both workers must be inside dispatch at the same time.
    assert gate.wait_entered(2) is True
    assert sorted(gate.seen) == ["directory_tree", "workspace_inventory"]
    assert engine.inflight == 2
    assert engine.busy is True

    # A third begin is refused: the buffer is full at its two-slot capacity.
    assert engine.begin("status", "sig-3", {}) is False

    gate.release()
    r1 = engine.resolve("sig-1")
    r2 = engine.resolve("sig-2")
    assert r1 is not None and r1.tool_name == "workspace_inventory"
    assert r2 is not None and r2.tool_name == "directory_tree"
    assert engine.busy is False


def test_slots_one_preserves_single_in_flight(predictor):
    gate = _Gate()
    engine = SpeculationEngine(predictor, gate, slots=1)
    assert engine.max_slots == 1

    assert engine.begin("status", "s1", {}) is True
    # With a single slot the second begin is refused while one is in flight,
    # exactly as the original engine behaved.
    assert engine.begin("status", "s2", {}) is False
    assert engine.inflight == 1

    gate.release()
    result = engine.resolve("s1")
    assert result is not None and result.tool_name == "status"
    assert engine.busy is False


def test_engine_reads_env_slot_count_when_unset(predictor, monkeypatch):
    monkeypatch.setenv("SONDER_SPECULATION_SLOTS", "3")

    def dispatch(tool, args):
        return "ok", True

    engine = SpeculationEngine(predictor, dispatch)  # slots default -> env
    assert engine.max_slots == 3
    assert engine.begin("status", "a", {}) is True
    assert engine.begin("workspace_inventory", "b", {}) is True
    assert engine.begin("directory_tree", "c", {}) is True
    # Fourth is refused: env said three slots.
    assert engine.begin("file_read", "d", {"path": "x"}) is False


# -- bounded reorder buffer: retirement/squash semantics ---------------------

def test_resolve_retires_matching_slot_and_leaves_others_in_flight(predictor):
    def dispatch(tool, args):
        return ("out:%s" % tool), True

    engine = SpeculationEngine(predictor, dispatch, slots=3)
    engine.begin("status", "s1", {})
    engine.begin("workspace_inventory", "s2", {})
    engine.begin("directory_tree", "s3", {})

    retired = engine.resolve("s2")  # commit the middle branch
    assert retired is not None and retired.tool_name == "workspace_inventory"
    # A retirement squashes nothing: the other two stay in the buffer.
    assert predictor.stats()["squashes"] == 0
    assert engine.inflight == 2
    assert engine.busy is True


def test_resolve_miss_squashes_only_the_stalest_slot(predictor):
    def dispatch(tool, args):
        return "ok", True

    engine = SpeculationEngine(predictor, dispatch, slots=3)
    engine.begin("status", "s1", {})       # stalest
    engine.begin("workspace_inventory", "s2", {})

    # Committed branch matches no buffered slot: squash exactly one (the
    # stalest), not the whole buffer.
    assert engine.resolve("no-such-sig") is None
    assert predictor.stats()["squashes"] == 1
    assert engine.inflight == 1

    # The surviving slot (s2) can still be retired on its own signature.
    retired = engine.resolve("s2")
    assert retired is not None and retired.tool_name == "workspace_inventory"
    assert predictor.stats()["squashes"] == 1


def test_discard_squashes_all_remaining_slots(predictor):
    def dispatch(tool, args):
        return "ok", True

    engine = SpeculationEngine(predictor, dispatch, slots=4)
    engine.begin("status", "s1", {})
    engine.begin("workspace_inventory", "s2", {})
    engine.begin("directory_tree", "s3", {})

    engine.discard()
    assert engine.busy is False
    assert engine.inflight == 0
    # One squash per outstanding slot; discard is the full pipeline flush.
    assert predictor.stats()["squashes"] == 3
    assert predictor.stats()["speculations"] == 3


def test_resolve_on_empty_buffer_is_noop(predictor):
    def dispatch(tool, args):
        return "ok", True

    engine = SpeculationEngine(predictor, dispatch, slots=2)
    assert engine.resolve("anything") is None
    assert predictor.stats()["squashes"] == 0


def test_worker_exception_becomes_failed_result_multislot(predictor):
    def dispatch(tool, args):
        raise RuntimeError("kaboom")

    engine = SpeculationEngine(predictor, dispatch, slots=2)
    engine.begin("status", "s1", {})
    engine.begin("workspace_inventory", "s2", {})
    # Both retire as failed results; neither begin/resolve ever raised.
    r1 = engine.resolve("s1")
    r2 = engine.resolve("s2")
    for r in (r1, r2):
        assert r is not None and r.ok is False and "kaboom" in r.observation


def test_mutation_never_speculated_even_with_free_slots(predictor):
    def dispatch(tool, args):
        raise AssertionError("mutation must never be dispatched speculatively")

    engine = SpeculationEngine(predictor, dispatch, slots=4)
    assert engine.begin("file_write", "s1", {"content": "x"}) is False
    assert engine.inflight == 0


# -- env clamp ---------------------------------------------------------------

def test_env_slot_count_is_clamped(monkeypatch):
    cases = {
        "99": sonder_speculation._MAX_SLOTS,  # over the max -> clamped down
        "4": 4,
        "3": 3,
        "1": 1,
        "0": 1,          # below one -> clamped up
        "-5": 1,         # negative -> clamped up
        "not-a-number": 1,  # garbage -> default
        "": 1,           # empty -> default
    }
    for raw, expected in cases.items():
        monkeypatch.setenv("SONDER_SPECULATION_SLOTS", raw)
        assert sonder_speculation.speculation_slots() == expected, raw

    monkeypatch.delenv("SONDER_SPECULATION_SLOTS", raising=False)
    assert sonder_speculation.speculation_slots() == 1  # unset -> default


def test_engine_clamps_explicit_slot_argument(predictor):
    def dispatch(tool, args):
        return "ok", True

    assert SpeculationEngine(predictor, dispatch, slots=99).max_slots \
        == sonder_speculation._MAX_SLOTS
    assert SpeculationEngine(predictor, dispatch, slots=0).max_slots == 1


# -- per-tool latency tables -------------------------------------------------

def test_per_tool_latency_tracked_alongside_global(predictor):
    # Two read-only tools with very different latencies. A named observation
    # folds into both the global figure and that tool's own EWMA, so the
    # per-tool table diverges by tool while the global stays an aggregate.
    for _ in range(5):
        predictor.observe_decision_latency(2.0)
        predictor.observe_tool_latency(0.01, "status")        # fast probe
        predictor.observe_tool_latency(1.5, "text_search")    # slow scan

    # Per-tool figures are exact for constant samples and clearly distinct.
    assert predictor.ewma_tool_by_name["status"] == pytest.approx(
        0.01, rel=1e-6)
    assert predictor.ewma_tool_by_name["text_search"] == pytest.approx(
        1.5, rel=1e-6)
    assert predictor.stats()["tool_latency_tools"] == 2
    # The global aggregate sits between the two per-tool extremes.
    assert 0.01 < predictor.ewma_tool_s < 1.5


def test_expected_saving_uses_per_tool_figure_when_available(predictor):
    for _ in range(5):
        predictor.observe_decision_latency(2.0)
        predictor.observe_tool_latency(0.01, "status")        # fast tool
        predictor.observe_tool_latency(1.5, "text_search")    # slow tool

    # Named fast tool -> its per-tool figure: min(2.0, 0.01) * 1.0.
    assert predictor.expected_saving_seconds(1.0, "status") == pytest.approx(
        0.01, rel=1e-6)
    # Named slow tool -> its per-tool figure: min(2.0, 1.5) * 1.0.
    assert predictor.expected_saving_seconds(1.0, "text_search") == \
        pytest.approx(1.5, rel=1e-6)
    # Unknown tool -> falls back to the global aggregate, not a per-tool value.
    unknown = predictor.expected_saving_seconds(1.0, "unknown_tool")
    assert unknown == pytest.approx(
        min(2.0, predictor.ewma_tool_s), rel=1e-6)


def test_should_speculate_respects_per_tool_latency(predictor, monkeypatch):
    for _ in range(5):
        predictor.observe_decision_latency(2.0)
        predictor.observe_tool_latency(0.005, "status")       # 5ms probe
        predictor.observe_tool_latency(1.5, "text_search")    # 1.5s scan
    monkeypatch.setenv("SONDER_SPECULATION_MIN_SAVING_MS", "40")

    # The fast tool is well under the 40ms floor: not worth speculating.
    assert predictor.should_speculate(0.9, "status") is False
    # The slow tool clears the floor comfortably: engage for that tool.
    assert predictor.should_speculate(0.9, "text_search") is True


def test_per_tool_latency_persists_across_runs(tmp_path):
    path = tmp_path / "predictor.json"
    p1 = BranchPredictor(path)
    for _ in range(5):
        p1.observe_decision_latency(2.0)
        p1.observe_tool_latency(0.01)
        p1.observe_tool_latency(1.5, "file_read")
    p1.save()

    p2 = BranchPredictor(path).load()
    assert p2.ewma_tool_by_name.get("file_read") == pytest.approx(1.5, rel=1e-6)
    # The persisted per-tool figure drives the decision after a warm restart.
    assert p2.expected_saving_seconds(1.0, "file_read") == pytest.approx(
        1.5, rel=1e-6)


def test_load_tolerates_old_cost_payload_without_per_tool(tmp_path):
    path = tmp_path / "predictor.json"
    # A file written before per-tool tables existed: cost has no such key.
    path.write_text(json.dumps({
        "version": sonder_speculation._PREDICTOR_VERSION,
        "openers": {},
        "transitions": {},
        "cost": {"ewma_decision_s": 2.0, "ewma_tool_s": 1.0},
    }), encoding="utf-8")

    p = BranchPredictor(path).load()
    assert p.ewma_tool_by_name == {}
    # Global figures still load and drive the default (no tool_name) decision.
    assert p.expected_saving_seconds(1.0) == pytest.approx(1.0, rel=1e-6)
    # An unknown tool falls back to the global figure with no per-tool table.
    assert p.expected_saving_seconds(1.0, "file_read") == pytest.approx(
        1.0, rel=1e-6)


def test_load_drops_malformed_per_tool_entries(tmp_path):
    path = tmp_path / "predictor.json"
    path.write_text(json.dumps({
        "version": sonder_speculation._PREDICTOR_VERSION,
        "openers": {},
        "transitions": {},
        "cost": {
            "ewma_decision_s": 2.0,
            "ewma_tool_s": 1.0,
            "ewma_tool_by_name": {
                "good_tool": 1.5,
                "bad_string": "not-a-float",
                "zero_tool": 0.0,        # non-positive dropped
                "negative_tool": -1.0,   # non-positive dropped
            },
        },
    }), encoding="utf-8")

    p = BranchPredictor(path).load()
    assert p.ewma_tool_by_name == {"good_tool": 1.5}


def test_observe_tool_latency_without_name_keeps_signature(predictor):
    # The original single-argument call must keep working unchanged.
    for _ in range(5):
        predictor.observe_tool_latency(0.5)
    assert predictor.ewma_tool_s == pytest.approx(0.5, rel=1e-6)
    assert predictor.ewma_tool_by_name == {}
