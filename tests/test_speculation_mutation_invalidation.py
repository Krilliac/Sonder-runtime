"""A committed state-changing call must invalidate every buffered speculation.

The retired-result cache and any in-flight slot hold observations captured
before the committed call ran.  When that call is a write (or any tool the
read-only speculation allowlist does not cover), those observations describe
a tree that may no longer exist, so the next identical read must dispatch for
real instead of being answered from the pre-mutation buffer.
"""
from __future__ import annotations

import pytest

from sonder_speculation import BranchPredictor, SpeculationEngine


@pytest.fixture()
def predictor(tmp_path):
    return BranchPredictor(tmp_path / "predictor.json")


class _World:
    def __init__(self):
        self.version = 1
        self.calls = 0

    def dispatch(self, tool, args):
        self.calls += 1
        return ("%s:v%d" % (tool, self.version)), True


def test_mutating_resolve_drops_squashed_pre_write_read(predictor):
    world = _World()
    engine = SpeculationEngine(predictor, world.dispatch, slots=1)
    assert engine.begin("directory_tree", "sig_tree", {})
    # The model commits to a write instead: the speculative read is squashed.
    assert engine.resolve("sig_write", "file_write") is None
    world.version = 2  # the write lands
    # The model re-reads to verify its write; the stale v1 tree must not return.
    assert engine.resolve("sig_tree", "directory_tree") is None
    assert engine.begin("directory_tree", "sig_tree", {})
    fresh = engine.resolve("sig_tree", "directory_tree")
    assert fresh is not None and fresh.observation == "directory_tree:v2"


def test_mutating_resolve_drops_previously_retired_results(predictor):
    world = _World()
    engine = SpeculationEngine(predictor, world.dispatch, slots=2)
    assert engine.begin("file_read", "sig_b", {"path": "b.py"})
    engine.discard()  # cached as a retired read of b.py (v1)
    assert engine.resolve("sig_write_b", "file_write") is None
    world.version = 2
    assert engine.resolve("sig_b", "file_read") is None


def test_mutating_resolve_squashes_in_flight_slots_without_caching(predictor):
    world = _World()
    engine = SpeculationEngine(predictor, world.dispatch, slots=2)
    assert engine.begin("file_read", "sig_a", {"path": "a.py"})
    assert engine.begin("file_read", "sig_b", {"path": "b.py"})
    assert engine.resolve("sig_exec", "run_code") is None
    assert engine.inflight == 0
    world.version = 2
    assert engine.resolve("sig_a", "file_read") is None
    assert engine.resolve("sig_b", "file_read") is None


def test_read_only_miss_still_keeps_retired_cache(predictor):
    world = _World()
    engine = SpeculationEngine(predictor, world.dispatch, slots=1)
    assert engine.begin("file_read", "sig_gamma", {"path": "gamma"})
    # A different read-only commit squashes the slot into the retired cache.
    assert engine.resolve("sig_beta", "file_read") is None
    retired = engine.resolve("sig_gamma", "file_read")
    assert retired is not None and retired.observation == "file_read:v1"


def test_invalidate_clears_everything(predictor):
    world = _World()
    engine = SpeculationEngine(predictor, world.dispatch, slots=2)
    assert engine.begin("file_read", "sig_a", {"path": "a.py"})
    engine.discard()
    assert engine.begin("file_read", "sig_b", {"path": "b.py"})
    engine.invalidate()
    assert engine.inflight == 0
    assert engine.resolve("sig_a") is None
    assert engine.resolve("sig_b") is None


def test_agent_loop_passes_the_committed_tool_to_resolve():
    """The server dispatch path must tell the engine which tool committed."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "server.py").read_text(
        encoding="utf-8"
    )
    assert "_spec_engine.resolve(call_signature, tool_name)" in source
