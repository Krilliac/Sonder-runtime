"""Evidence-level memory diagnostics: conflicts, staleness, duplicate facts.

These pin the newly wired read-only findings in memory_quality (backed by the
previously caller-less lesson_decay logic) and the write-path duplicate-fact
guard. Everything runs offline and deterministically: embeddings are literal
float vectors stored with explicit provenance, outcomes are written through the
same store functions production uses, and staleness takes an injected clock.

The recurring assertion is fail-closed provenance: a lesson with no stored
embedding, an incomparable embedding space, or no scored outcome must never
appear in a contradiction or staleness claim, however suggestive its text.
"""
from datetime import datetime, timedelta, timezone

import grounded_outcomes
import memory_quality
import memory_store
import sonder_runtime.adapters.embeddings as embeddings
import sonder_runtime.adapters.learning_health as learning_health


def _add_embedded_lesson(conn, lesson_id, text, vector, model="test-model",
                         revision="r1"):
    memory_store.add_lesson(
        conn, lesson_id, text, embeddings.to_blob(vector), "i-%s" % lesson_id,
        embedding_model=model, embedding_revision=revision,
        embedding_dim=len(vector),
    )


def _score(conn, lesson_id, interaction_id, task, signal, reward, source):
    memory_store.log_lesson_usage(conn, [lesson_id], interaction_id, task)
    memory_store.record_lesson_usage_outcome(
        conn, interaction_id, signal, reward, source=source,
    )


# --- contradiction findings ------------------------------------------------

def test_conflicting_lessons_detected_from_grounded_outcomes():
    conn = memory_store.connect(":memory:")
    _add_embedded_lesson(
        conn, "retry-yes", "Always retry HTTP calls on timeout.", [1.0, 0.0, 0.0]
    )
    _add_embedded_lesson(
        conn, "retry-no", "Never retry HTTP calls on timeout.", [0.999, 0.02, 0.0]
    )
    _add_embedded_lesson(
        conn, "unrelated", "Use collections.deque for FIFO queues.", [0.0, 1.0, 0.0]
    )
    _score(conn, "retry-yes", "i1", "add http retry", "accepted", 0.8, "caller")
    _score(conn, "retry-no", "i2", "add http retry", "rejected", -0.5, "caller")
    _score(conn, "unrelated", "i3", "queue task", "accepted", 0.8, "caller")

    findings = memory_quality.contradiction_findings(conn)

    assert len(findings) == 1
    pair = {findings[0]["a_id"], findings[0]["b_id"]}
    assert pair == {"retry-yes", "retry-no"}
    assert findings[0]["similarity"] >= 0.8
    assert findings[0]["a_evidence"]["population"] == "caller"
    assert findings[0]["b_evidence"]["population"] == "caller"
    # Deterministic: the same store yields the same findings, in order.
    assert memory_quality.contradiction_findings(conn) == findings
    report = memory_quality.audit(conn)
    assert report["conflicting_lesson_pairs"] == 1
    assert report["samples"]["conflicts"] == findings
    conn.close()


def test_conflict_claims_fail_closed_without_embedding_provenance():
    conn = memory_store.connect(":memory:")
    _add_embedded_lesson(
        conn, "retry-yes", "Always retry HTTP calls on timeout.", [1.0, 0.0, 0.0]
    )
    # Opposite outcome but no stored vector: similarity cannot be judged.
    memory_store.add_lesson(
        conn, "no-vector", "Never retry HTTP calls on timeout.", None, "i-nv"
    )
    # Opposite outcome, vector present, but a different embedding model:
    # vectors from incomparable spaces are not semantic evidence.
    _add_embedded_lesson(
        conn, "other-space", "Never ever retry HTTP calls on timeout.",
        [0.999, 0.02, 0.0], model="other-model",
    )
    _score(conn, "retry-yes", "i1", "add http retry", "accepted", 0.8, "caller")
    _score(conn, "no-vector", "i2", "add http retry", "rejected", -0.5, "caller")
    _score(conn, "other-space", "i3", "add http retry", "rejected", -0.5, "caller")

    assert memory_quality.contradiction_findings(conn) == []
    conn.close()


def test_conflict_claims_fail_closed_without_scored_outcomes():
    """Directive-sounding text alone must never produce a contradiction claim:
    polarity comes from graded outcomes, and a lesson nobody has graded has no
    polarity, however negative its phrasing reads."""
    conn = memory_store.connect(":memory:")
    _add_embedded_lesson(
        conn, "retry-yes", "Always retry HTTP calls on timeout.", [1.0, 0.0, 0.0]
    )
    _add_embedded_lesson(
        conn, "retry-no", "Never retry HTTP calls on timeout.", [0.999, 0.02, 0.0]
    )
    _score(conn, "retry-yes", "i1", "add http retry", "accepted", 0.8, "caller")
    # retry-no is retrieved but never graded: usage without a scored outcome.
    memory_store.log_lesson_usage(conn, ["retry-no"], "i2", "add http retry")

    assert memory_quality.contradiction_findings(conn) == []
    conn.close()


def test_caller_judgement_decides_polarity_over_execution_grades():
    """A caller's rejection is not laundered away by the runtime passing its
    own tests -- the same two-population doctrine as retriever._usage_boost."""
    conn = memory_store.connect(":memory:")
    _add_embedded_lesson(
        conn, "rejected-by-caller", "Cache tokens in a module global.",
        [1.0, 0.0, 0.0],
    )
    _add_embedded_lesson(
        conn, "accepted-by-caller", "Never cache tokens in a module global.",
        [0.999, 0.02, 0.0],
    )
    _score(conn, "rejected-by-caller", "i1", "token cache", "rejected", -0.5, "caller")
    _score(conn, "rejected-by-caller", "i2", "token cache", "tests_passed", 1.0, "machine")
    _score(conn, "rejected-by-caller", "i3", "token cache", "tests_passed", 1.0, "machine")
    _score(conn, "accepted-by-caller", "i4", "token cache", "accepted", 0.8, "caller")

    findings = memory_quality.contradiction_findings(conn)

    assert len(findings) == 1
    by_id = {findings[0]["a_id"]: findings[0]["a_evidence"],
             findings[0]["b_id"]: findings[0]["b_evidence"]}
    assert by_id["rejected-by-caller"]["population"] == "caller"
    assert by_id["rejected-by-caller"]["mean_reward"] < 0
    conn.close()


# --- stale lesson findings -------------------------------------------------

def test_stale_lessons_require_aged_positive_evidence():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "aged-win", "Use pathlib.Path for joins.", None, "i-a")
    memory_store.add_lesson(conn, "aged-loss", "Use eval for config.", None, "i-b")
    memory_store.add_lesson(conn, "ungraded", "Use enumerate for indexes.", None, "i-c")
    _score(conn, "aged-win", "i1", "path join", "accepted", 0.8, "caller")
    _score(conn, "aged-loss", "i2", "config parse", "rejected", -0.5, "caller")

    recent = datetime.now(timezone.utc) + timedelta(days=10)
    assert memory_quality.stale_lesson_findings(conn, now=recent) == []

    aged = datetime.now(timezone.utc) + timedelta(days=90)
    findings = memory_quality.stale_lesson_findings(conn, now=aged)

    # Only the positive lesson goes stale: measured harm is quarantine's
    # question and an ungraded lesson has no evidence to age.
    assert [f["id"] for f in findings] == ["aged-win"]
    assert findings[0]["population"] == "caller"
    assert findings[0]["age_days"] >= memory_quality.STALE_MIN_AGE_DAYS
    assert findings[0]["effective_score"] < memory_quality.STALE_EFFECTIVE_FLOOR
    # Deterministic under an injected clock.
    assert memory_quality.stale_lesson_findings(conn, now=aged) == findings
    conn.close()


def test_fresh_win_keeps_an_old_lesson_out_of_stale():
    """Staleness ages from the newest scored evidence, not the lesson row: an
    old lesson still earning wins is current, whatever its creation date."""
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "old-but-live", "Prefer f-strings.", None, "i-a")
    conn.execute(
        "UPDATE lessons SET ts='2020-01-01 00:00:00' WHERE id='old-but-live'"
    )
    conn.commit()
    _score(conn, "old-but-live", "i1", "format strings", "accepted", 0.8, "caller")

    soon = datetime.now(timezone.utc) + timedelta(days=10)
    assert memory_quality.stale_lesson_findings(conn, now=soon) == []
    conn.close()


# --- duplicate facts -------------------------------------------------------

def test_duplicate_fact_findings_stay_inside_project_scope():
    conn = memory_store.connect(":memory:")
    memory_store.add_fact(conn, "f1", "p1", "The build uses Ninja.", None)
    memory_store.add_fact(conn, "f2", "p1", "  the build   uses ninja. ", None)
    memory_store.add_fact(conn, "f3", "p2", "The build uses Ninja.", None)

    findings = memory_quality.duplicate_fact_findings(conn)

    # One group inside p1; the identical statement in p2 is NOT part of it --
    # project scope is a privacy boundary, and a cross-project match would
    # reveal one project's facts while auditing another.
    assert len(findings) == 1
    assert findings[0]["project"] == "p1"
    assert findings[0]["keeper_id"] == "f1"
    assert findings[0]["duplicate_ids"] == ["f2"]
    report = memory_quality.audit(conn)
    assert report["duplicate_fact_groups"] == 1
    assert report["duplicate_fact_rows"] == 1
    conn.close()


def test_find_duplicate_fact_is_project_scoped_and_normalized():
    conn = memory_store.connect(":memory:")
    memory_store.add_fact(conn, "f1", "p1", "The build uses Ninja.", None)

    hit = memory_store.find_duplicate_fact(conn, "p1", "THE BUILD  USES NINJA.")
    assert hit is not None and hit["id"] == "f1"
    # The same statement is unknown to every other project.
    assert memory_store.find_duplicate_fact(conn, "p2", "The build uses Ninja.") is None
    # Empty text can never match anything.
    assert memory_store.find_duplicate_fact(conn, "p1", "   ") is None
    conn.close()


def test_remember_fact_tool_refuses_duplicates(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(server.embeddings, "embed", lambda text: None)

    first = server.sonder_remember_fact("The build uses Ninja.", project="p1")
    repeat = server.sonder_remember_fact("  the build uses NINJA. ", project="p1")
    other_project = server.sonder_remember_fact("The build uses Ninja.", project="p2")

    assert "Remembered fact" in first
    assert "Already known" in repeat and "(1 total)" in repeat
    assert "Remembered fact" in other_project

    conn = server._open_db()
    try:
        assert memory_store.count_facts(conn, "p1") == 1
        assert memory_store.count_facts(conn, "p2") == 1
    finally:
        conn.close()


# --- learning health surfaces ----------------------------------------------

def test_learning_health_reports_new_diagnostics_and_attribution_counters():
    grounded_outcomes.reset()
    try:
        conn = memory_store.connect(":memory:")
        _add_embedded_lesson(
            conn, "retry-yes", "Always retry HTTP calls on timeout.",
            [1.0, 0.0, 0.0],
        )
        _add_embedded_lesson(
            conn, "retry-no", "Never retry HTTP calls on timeout.",
            [0.999, 0.02, 0.0],
        )
        _score(conn, "retry-yes", "i1", "add http retry", "accepted", 0.8, "caller")
        _score(conn, "retry-no", "i2", "add http retry", "rejected", -0.5, "caller")
        memory_store.add_fact(conn, "f1", "p1", "The build uses Ninja.", None)
        memory_store.add_fact(conn, "f2", "p1", "the build uses ninja.", None)
        grounded_outcomes.note_generation("i9", "sonder", "p1")

        report = learning_health.build_report(conn)

        assert report["conflicting_lesson_pairs"] == 1
        assert report["duplicate_fact_rows"] == 1
        assert report["stale_lessons"] == 0
        attribution = report["outcome_attribution"]
        assert attribution["noted"] == 1
        assert attribution["pending"] == 1
        assert attribution["attributed"] == 0

        rendered = learning_health.format_report(report)
        assert "conflicting pairs=1" in rendered
        assert "duplicate fact rows=1" in rendered
        assert "outcome attribution (this process, not the store)" in rendered
        # A report built before these keys existed renders without inventing
        # zero-rows for a lane it never measured.
        legacy = {key: value for key, value in report.items()
                  if key != "outcome_attribution"}
        assert "outcome attribution" not in learning_health.format_report(legacy)
        conn.close()
    finally:
        grounded_outcomes.reset()
