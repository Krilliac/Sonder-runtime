from dataclasses import replace
import sqlite3

import pytest

import memory_store
import refinement_transactions as tx
from scripts import package_local_system as package


def _store():
    conn = memory_store.connect(":memory:")
    memory_store.upsert_preference(
        conn, "pref-1", "global", "concise", "User prefers concise answers.",
        confidence=0.8,
    )
    conn.execute(
        "INSERT INTO interactions(id, task, response) VALUES('i-1', 'task', 'done')"
    )
    conn.execute(
        "INSERT INTO outcomes(interaction_id, signal, reward) VALUES('i-1', 'worked', 1.0)"
    )
    conn.commit()
    return conn


def _request(version, text="User prefers concise, direct answers."):
    return tx.RefinementRequest(
        preference_id="pref-1",
        expected_version=version,
        patch=tx.PreferencePatch(text=text),
        evidence=(tx.Evidence(
            tx.EvidenceKind.GROUNDED_OUTCOME,
            "The concise response was accepted by the caller.",
            interaction_id="i-1", signal="worked",
        ),),
        expected_outcome="Future answers remain direct while preserving required detail.",
    )


def test_preference_refinement_is_atomic_versioned_and_append_only():
    conn = _store()
    before = tx.preference_snapshot(conn, "pref-1")
    result = tx.apply_preference_refinement(conn, _request(before.revision))

    assert result.before == before
    assert result.after.revision == before.revision + 1
    assert result.after.text == "User prefers concise, direct answers."
    assert result.before_digest == tx.snapshot_digest(result.before)
    assert result.after_digest == tx.snapshot_digest(result.after)
    history = tx.refinement_history(conn, "pref-1")
    assert [row["operation"] for row in history] == ["apply"]
    assert history[0]["execution_scope"] == "local"
    assert history[0]["expected_outcome"].startswith("Future answers")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE refinement_history SET operation='apply'")
    conn.rollback()
    assert "refinement_transactions.py" in package.REQUIRED_FILES


def test_stale_apply_and_missing_grounded_evidence_leave_state_unchanged():
    conn = _store()
    before = tx.preference_snapshot(conn, "pref-1")
    tx.apply_preference_refinement(conn, _request(before.revision))
    after = tx.preference_snapshot(conn, "pref-1")
    with pytest.raises(tx.RefinementConflict, match="expected version"):
        tx.apply_preference_refinement(conn, _request(before.revision, "Different."))
    assert tx.preference_snapshot(conn, "pref-1") == after

    bad = tx.RefinementRequest(
        preference_id="pref-1", expected_version=after.revision,
        patch=tx.PreferencePatch(enabled=False),
        evidence=(tx.Evidence(
            tx.EvidenceKind.GROUNDED_OUTCOME, "Missing reference.",
            interaction_id="missing", signal="worked",
        ),), expected_outcome="Disable the preference safely.",
    )
    with pytest.raises(tx.RefinementValidation, match="does not exist"):
        tx.apply_preference_refinement(conn, bad)
    assert tx.preference_snapshot(conn, "pref-1") == after
    assert len(tx.refinement_history(conn)) == 1


def test_existing_preference_writers_advance_the_same_cas_version():
    conn = _store()
    first = tx.preference_snapshot(conn, "pref-1")
    memory_store.upsert_preference(
        conn, "ignored-on-conflict", "global", "concise",
        "User prefers very concise answers.", confidence=0.9,
    )
    second = tx.preference_snapshot(conn, "pref-1")
    assert second.revision == first.revision + 1
    memory_store.set_preference_enabled(conn, "pref-1", False)
    assert tx.preference_snapshot(conn, "pref-1").revision == second.revision + 1


def test_rollback_restores_content_as_new_version_and_rejects_stale_replay():
    conn = _store()
    applied = tx.apply_preference_refinement(
        conn, _request(tx.preference_snapshot(conn, "pref-1").revision),
    )
    rolled = tx.rollback_preference_refinement(
        conn, applied.refinement_id, applied.after.revision,
    )
    assert rolled.after.text == applied.before.text
    assert rolled.after.revision == applied.after.revision + 1
    assert rolled.parent_refinement_id == applied.refinement_id
    assert [row["operation"] for row in tx.refinement_history(conn)] == [
        "rollback", "apply",
    ]
    with pytest.raises(tx.RefinementConflict, match="expected version"):
        tx.rollback_preference_refinement(
            conn, applied.refinement_id, applied.after.revision,
        )


def test_refinement_is_local_and_strictly_bounded(monkeypatch):
    conn = _store()
    version = tx.preference_snapshot(conn, "pref-1").revision
    request = _request(version)
    with pytest.raises(tx.RefinementValidation, match="must be local"):
        tx.apply_preference_refinement(
            conn, replace(request, execution_scope="cloud"),
        )
    with pytest.raises(tx.RefinementValidation, match="exceeds"):
        tx.apply_preference_refinement(conn, replace(
            request, patch=tx.PreferencePatch(text="x" * (tx.MAX_TEXT + 1)),
        ))
    with pytest.raises(tx.RefinementValidation, match="normalized preference text"):
        tx.apply_preference_refinement(conn, replace(
            request, patch=tx.PreferencePatch(text="x" * tx.MAX_TEXT),
        ))
    with pytest.raises(tx.RefinementValidation, match="must be an integer"):
        tx.apply_preference_refinement(conn, replace(request, expected_version=version + 0.5))

    proposal_evidence = (tx.Evidence(
        tx.EvidenceKind.IMPROVEMENT_PROPOSAL,
        "A reviewed proposal recommends this preference update.",
        proposal_id="prop-reviewed",
    ),)
    monkeypatch.setattr(tx.goal_store, "get", lambda _proposal_id: None)
    with pytest.raises(tx.RefinementValidation, match="does not exist"):
        tx.apply_preference_refinement(conn, replace(request, evidence=proposal_evidence))
    monkeypatch.setattr(
        tx.goal_store,
        "get",
        lambda proposal_id: {"id": proposal_id, "status": tx.goal_store.STATUS_PROPOSED},
    )
    applied = tx.apply_preference_refinement(
        conn, replace(request, evidence=proposal_evidence),
    )
    assert applied.after.revision == version + 1
    assert len(tx.refinement_history(conn)) == 1
