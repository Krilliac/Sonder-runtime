"""Speculation wired through the real _agent_impl loop."""
from __future__ import annotations

import pytest

import server
import sonder_speculation


@pytest.fixture(autouse=True)
def isolated_predictor(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_BRANCH_PREDICTOR", str(tmp_path / "bp.json"))
    monkeypatch.setenv("SONDER_SPECULATION", "1")
    sonder_speculation.reset_for_tests()
    yield
    sonder_speculation.reset_for_tests()


def _script(decisions):
    """A fake model that returns queued decisions, then a final answer."""
    queue = list(decisions)

    def gen(prompt, history=None):
        if queue:
            return queue.pop(0)
        return '{"final": "done"}'

    return gen


def test_predicted_readonly_call_is_speculated_and_retired(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    # Pre-train the predictor so step 1 predicts workspace_inventory.
    predictor = sonder_speculation.default_predictor()
    state = sonder_speculation.BranchPredictor.loop_state((), None, 1)
    for _ in range(6):
        predictor.record_transition(state, "workspace_inventory")

    dispatch_calls = []
    real_dispatch = server._agent_dispatch_observed

    def counting_dispatch(tool_name, args, **kwargs):
        dispatch_calls.append(tool_name)
        return real_dispatch(tool_name, args, **kwargs)

    monkeypatch.setattr(server, "_agent_dispatch_observed", counting_dispatch)
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: _script([
            '{"tool": "workspace_inventory", "args": {}}',
        ]),
    )

    out = server._agent_impl(
        "inspect the project",
        tier="code",
        max_steps=3,
        read_only=True,
        project=str(tmp_path),
    )
    assert isinstance(out, str)
    # workspace_inventory ran exactly once total (speculatively), not twice.
    assert dispatch_calls.count("workspace_inventory") == 1
    stats = sonder_speculation.default_predictor().stats()
    assert stats["speculations"] >= 1
    assert stats["hits"] >= 1


def test_mispredicted_speculation_is_squashed_not_reused(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    predictor = sonder_speculation.default_predictor()
    state = sonder_speculation.BranchPredictor.loop_state((), None, 1)
    for _ in range(6):
        predictor.record_transition(state, "workspace_inventory")

    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: _script([
            # Model commits to a DIFFERENT read-only tool than predicted.
            '{"tool": "directory_tree", "args": {}}',
        ]),
    )

    server._agent_impl(
        "inspect the project",
        tier="code",
        max_steps=3,
        read_only=True,
        project=str(tmp_path),
    )
    stats = sonder_speculation.default_predictor().stats()
    assert stats["speculations"] >= 1
    assert stats["squashes"] >= 1


def test_speculation_disabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    sonder_speculation.reset_for_tests()
    predictor = sonder_speculation.default_predictor()
    state = sonder_speculation.BranchPredictor.loop_state((), None, 1)
    for _ in range(6):
        predictor.record_transition(state, "workspace_inventory")

    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: _script(['{"tool": "workspace_inventory", "args": {}}']),
    )
    server._agent_impl(
        "inspect the project", tier="code", max_steps=2,
        read_only=True, project=str(tmp_path),
    )
    # No speculation issued when disabled.
    assert sonder_speculation.default_predictor().stats()["speculations"] == 0


def test_predictor_learns_and_persists_across_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: _script([
            '{"tool": "workspace_inventory", "args": {}}',
            '{"tool": "directory_tree", "args": {}}',
        ]),
    )
    server._agent_impl(
        "inspect", tier="code", max_steps=4, read_only=True,
        project=str(tmp_path),
    )
    # The transition table was persisted on loop teardown.
    reloaded = sonder_speculation.BranchPredictor(
        tmp_path / "bp.json"
    ).load()
    assert reloaded.stats()["transition_states"] >= 1
