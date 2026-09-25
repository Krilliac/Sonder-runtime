"""/goal slash-command branches that previously referenced unbound names.

``/goal adopt`` called an undefined ``_fmt`` helper and ``/goal set --auto``
guarded an unbound ``project`` with a ``dir()`` probe, so adoption raised
NameError and auto-launched runs silently dropped the session project.
"""
import pytest

import goal_store
import server


@pytest.fixture(autouse=True)
def _goal_db(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_GOAL_DB", str(tmp_path / "goals.db"))
    goal_store.reset_for_tests()
    yield
    goal_store.reset_for_tests()


def test_goal_adopt_returns_the_formatted_adopted_goal():
    proposal = goal_store.propose(
        "memory: refresh stale embeddings", ["backfill reports 0 remaining"],
    )

    out = server.control_command(
        "/goal adopt %s" % proposal["id"], operator_approved=True,
    )

    adopted = goal_store.get_active()
    assert adopted is not None
    assert adopted["objective"] == "memory: refresh stale embeddings"
    assert goal_store.proposals() == []
    assert out == "adopted\n" + server._format_goal(adopted)
    assert "criterion: backfill reports 0 remaining" in out


def test_goal_adopt_of_unknown_proposal_reports_goal_error():
    out = server._goal_command("adopt missing-id")
    assert out.startswith("ERROR: ")
    assert goal_store.get_active() is None


def _capture_autopilot(monkeypatch):
    calls = []

    def fake_goal_to_autopilot(goal, **kwargs):
        calls.append(kwargs)
        return {"run_id": "auto-test"}

    monkeypatch.setattr(
        server._composition, "goal_to_autopilot", fake_goal_to_autopilot,
    )
    monkeypatch.setattr(server, "_launch_autopilot", lambda run_id, **_kw: True)
    return calls


def test_goal_set_auto_scopes_the_run_to_the_session_project(monkeypatch):
    calls = _capture_autopilot(monkeypatch)

    out = server.control_command(
        "/goal set --auto Ship the release", project="alpha",
        operator_approved=True,
        autopilot_request_owner="owner@example.com",
    )

    assert "autopilot: auto-test started" in out
    assert calls == [{"project": "alpha", "request_owner": "owner@example.com"}]


def test_goal_set_auto_without_project_matches_autopilot_goal(monkeypatch):
    """No session project resolves exactly as ``/autopilot --goal`` does."""
    calls = _capture_autopilot(monkeypatch)

    server._goal_command("set --auto Ship the release")

    assert calls[0]["project"] == (server._resolve_project("") or "")
