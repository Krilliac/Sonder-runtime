"""Branch predictor and speculative execution engine (CPU-inspired)."""
from __future__ import annotations

import threading
import time

import pytest

import sonder_speculation
from sonder_speculation import BranchPredictor, SpeculationEngine


@pytest.fixture()
def predictor(tmp_path):
    return BranchPredictor(tmp_path / "predictor.json")


# -- branch predictor --------------------------------------------------------

def test_predicts_dominant_next_tool_after_learning(predictor):
    state = BranchPredictor.loop_state((), None, 1)
    for _ in range(8):
        predictor.record_transition(state, "workspace_inventory")
    predictor.record_transition(state, "text_search")
    tool, confidence = predictor.predict_next_tool(state)
    assert tool == "workspace_inventory"
    assert confidence > 0.5


def test_low_confidence_suppresses_prediction(predictor):
    state = BranchPredictor.loop_state((), None, 1)
    # Five distinct tools once each: no majority, below the confidence floor.
    for tool in ("a", "b", "c", "d", "e"):
        predictor.record_transition(state, tool)
    assert predictor.predict_next_tool(state) is None


def test_unknown_state_has_no_prediction(predictor):
    assert predictor.predict_next_tool("never-seen") is None


def test_opener_prediction_by_request_kind(predictor):
    for _ in range(5):
        predictor.record_opener("please fix this bug in the parser", "code")
    predictor.record_opener("fix the failing test", "fast")
    tool, _ = predictor.predict_opener("fix another bug here")
    assert tool == "code"


def test_request_kind_buckets():
    assert BranchPredictor.request_kind("write a function") == "author"
    assert BranchPredictor.request_kind("why does this crash?") == "explain"
    assert BranchPredictor.request_kind("find the config") == "search"
    assert BranchPredictor.request_kind("fix the bug") == "debug"


def test_loop_state_is_stable_and_discrete():
    s1 = BranchPredictor.loop_state(("file_read", "text_search"), "text_search", 2)
    s2 = BranchPredictor.loop_state(("text_search", "file_read"), "text_search", 2)
    # Order-independent tool set, same last tool + phase -> same state.
    assert s1 == s2
    s3 = BranchPredictor.loop_state((), "text_search", 5)
    assert s3 != s1  # different phase


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "predictor.json"
    p1 = BranchPredictor(path)
    state = BranchPredictor.loop_state((), None, 1)
    for _ in range(6):
        p1.record_transition(state, "directory_tree")
    p1.save()
    p2 = BranchPredictor(path).load()
    tool, _ = p2.predict_next_tool(state)
    assert tool == "directory_tree"


def test_speculatable_allowlist_excludes_mutations(predictor):
    assert predictor.speculatable("file_read")
    assert predictor.speculatable("workspace_inventory")
    assert not predictor.speculatable("file_write")
    assert not predictor.speculatable("run_code")
    assert not predictor.speculatable("web_fetch")


# -- speculation engine ------------------------------------------------------

def _engine(predictor, dispatch, enabled=True):
    return SpeculationEngine(predictor, dispatch, enabled=enabled)


def test_hit_retires_buffered_result(predictor):
    calls = []

    def dispatch(tool, args):
        calls.append(tool)
        return "inventory output", True

    engine = _engine(predictor, dispatch)
    issued = engine.begin("workspace_inventory", "sig-1", {})
    assert issued
    result = engine.resolve("sig-1")
    assert result is not None
    assert result.ok and "inventory" in result.observation
    assert calls == ["workspace_inventory"]  # ran exactly once, speculatively


def test_mispredict_squashes(predictor):
    def dispatch(tool, args):
        return "output", True

    engine = _engine(predictor, dispatch)
    engine.begin("workspace_inventory", "sig-A", {})
    # The model committed to a different call signature.
    result = engine.resolve("sig-B")
    assert result is None
    assert predictor.stats()["squashes"] == 1


def test_mutation_never_speculated(predictor):
    def dispatch(tool, args):
        raise AssertionError("mutation must never be dispatched speculatively")

    engine = _engine(predictor, dispatch)
    assert engine.begin("file_write", "sig", {"content": "x"}) is False


def test_disabled_engine_issues_nothing(predictor):
    def dispatch(tool, args):
        raise AssertionError("disabled engine must not dispatch")

    engine = _engine(predictor, dispatch, enabled=False)
    assert engine.begin("workspace_inventory", "sig", {}) is False


def test_one_speculation_in_flight_at_a_time(predictor):
    release = threading.Event()

    def dispatch(tool, args):
        release.wait(5)
        return "done", True

    engine = _engine(predictor, dispatch)
    assert engine.begin("workspace_inventory", "sig-1", {}) is True
    # A second begin while one is in flight is refused (small buffer).
    assert engine.begin("directory_tree", "sig-2", {}) is False
    release.set()
    engine.resolve("sig-1")


def test_discard_clears_leftover_without_orphan(predictor):
    def dispatch(tool, args):
        return "done", True

    engine = _engine(predictor, dispatch)
    engine.begin("status", "sig-1", {})
    engine.discard()
    assert not engine.busy
    # Engine is idle again and can issue a fresh speculation.
    assert engine.begin("status", "sig-2", {}) is True
    engine.resolve("sig-2")


def test_dispatch_exception_becomes_failed_result(predictor):
    def dispatch(tool, args):
        raise RuntimeError("boom")

    engine = _engine(predictor, dispatch)
    engine.begin("status", "sig", {})
    result = engine.resolve("sig")
    assert result is not None
    assert result.ok is False
    assert "boom" in result.observation


def test_stats_track_speculation_hit_rate(predictor):
    def dispatch(tool, args):
        return "ok", True

    engine = _engine(predictor, dispatch)
    engine.begin("status", "sig-hit", {})
    predictor.note_hit()
    engine.resolve("sig-hit")
    engine.begin("status", "sig-miss", {})
    engine.resolve("other-sig")  # squash
    stats = predictor.stats()
    assert stats["speculations"] == 2
    assert stats["squashes"] == 1
