"""Persistent goals: durable across turns, closed only by an explicit user."""
import pytest

import goal_store
import server


@pytest.fixture(autouse=True)
def _goal_db(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_GOAL_DB", str(tmp_path / "goals.db"))
    goal_store.reset_for_tests()
    yield
    goal_store.reset_for_tests()


def test_goal_survives_and_renders_into_turn_context():
    goal_store.set_goal(
        "Make the whole suite green on Windows",
        ["pytest -q exits 0", "no new warnings"],
    )
    # A later "turn" reads the same durable state.
    goal_store.reset_for_tests()
    block = goal_store.context_block()
    assert "Make the whole suite green on Windows" in block
    assert "pytest -q exits 0" in block
    assert "do not redefine success" in block
    assert "cannot mark this goal complete" in block


def test_notes_are_bounded_and_appear_in_context():
    goal_store.set_goal("Ship the release")
    goal_store.add_note("cut the branch")
    goal_store.add_note("x" * 5000)
    goal = goal_store.get_active()
    assert len(goal["notes"]) == 2
    assert len(goal["notes"][-1]["text"]) <= goal_store.MAX_NOTE_CHARS
    assert "cut the branch" in goal_store.context_block()


@pytest.mark.parametrize("actor", ["", "model", "agent", "autopilot", "USER "])
def test_only_an_explicit_user_can_close_a_goal(actor):
    """The whole trust boundary: model output can never end a goal."""
    goal_store.set_goal("Do the hard thing")
    with pytest.raises(goal_store.GoalError):
        goal_store.complete("all done", actor=actor)
    with pytest.raises(goal_store.GoalError):
        goal_store.abandon("giving up", actor=actor)
    assert goal_store.get_active()["status"] == goal_store.STATUS_ACTIVE


def test_user_can_close_a_goal():
    goal_store.set_goal("Do the hard thing")
    closed = goal_store.complete("verified green", actor="user")
    assert closed["status"] == goal_store.STATUS_COMPLETED
    assert goal_store.get_active() is None
    assert goal_store.context_block() == ""
    assert goal_store.history()[0]["status"] == goal_store.STATUS_COMPLETED


def test_setting_a_new_goal_supersedes_the_old_one():
    first = goal_store.set_goal("First objective")
    second = goal_store.set_goal("Second objective")
    assert goal_store.get_active()["id"] == second["id"]
    assert goal_store.get(first["id"])["status"] == goal_store.STATUS_ABANDONED


def test_proposals_are_inert_until_a_user_adopts_them():
    proposal = goal_store.propose("memory: refresh stale embeddings",
                                  ["backfill reports 0 remaining"])
    assert proposal["status"] == goal_store.STATUS_PROPOSED
    # A proposal is not a goal.
    assert goal_store.get_active() is None
    assert goal_store.context_block() == ""
    for actor in ("", "model", "self"):
        with pytest.raises(goal_store.GoalError):
            goal_store.adopt(proposal["id"], actor=actor)
    assert goal_store.get_active() is None

    adopted = goal_store.adopt(proposal["id"], actor="user")
    assert adopted["status"] == goal_store.STATUS_ACTIVE
    assert "refresh stale embeddings" in goal_store.get_active()["objective"]


def test_duplicate_proposals_are_ignored():
    assert goal_store.propose("store: search index drift") is not None
    assert goal_store.propose("store: search index drift") is None
    assert len(goal_store.proposals()) == 1


def test_declining_a_proposal_requires_a_user_and_removes_it():
    proposal = goal_store.propose("runtime: something to consider")
    with pytest.raises(goal_store.GoalError):
        goal_store.decline(proposal["id"], actor="model")
    goal_store.decline(proposal["id"], actor="user")
    assert goal_store.proposals() == []


def test_refresh_proposes_only_significant_findings(monkeypatch):
    monkeypatch.setattr(server, "improvement_report_data", lambda **kw: {
        "issues": [
            {"area": "memory", "severity": "medium",
             "title": "36 lesson embeddings lack provenance.",
             "action": "Run memory_embedding_backfill."},
            {"area": "memory", "severity": "low",
             "title": "Some lessons are vague.", "action": "Prefer anchors."},
        ],
    })
    result = server.refresh_goal_proposals()
    assert result["proposed"] == 1
    assert result["skipped"] == 1
    rows = goal_store.proposals()
    assert len(rows) == 1
    assert "lack provenance" in rows[0]["objective"]
    # Proposing never activates anything.
    assert goal_store.get_active() is None


def test_goal_command_round_trip():
    assert "no active goal" in server._goal_command("show")
    out = server._goal_command(
        "set Finish the migration --criteria all tests pass; docs updated")
    assert "Finish the migration" in out
    assert "all tests pass" in out
    assert "noted" in server._goal_command("note halfway through")
    assert "halfway through" in server._goal_command("show")
    assert "goal completed" in server._goal_command("done shipped it")
    assert "no active goal" in server._goal_command("show")


def test_build_system_injects_the_active_goal():
    goal_store.set_goal("Keep the fleet healthy")
    system = server._build_system("base instructions", False, "")
    assert "Keep the fleet healthy" in system
    assert "base instructions" in system
    goal_store.complete("done", actor="user")
    assert "Keep the fleet healthy" not in server._build_system(
        "base instructions", False, "")
