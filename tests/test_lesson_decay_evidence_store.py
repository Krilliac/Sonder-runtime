"""Evidence-typed decay wired through the store: staleness and pruning.

The stale-lesson diagnostic reads the newest outcome's signal to pick its
half-life. These tests pin that wiring, prove evidence with no mapped type
produces identical findings, and prove the near-duplicate pruner -- which
never consulted decay -- plans and deletes exactly the same lessons whatever
evidence the lessons carry.
"""
from datetime import datetime, timedelta, timezone

import lesson_decay
import lesson_pruner
import memory_quality
import memory_store
import sonder_runtime.adapters.embeddings as embeddings


def _score(conn, lesson_id, interaction_id, task, signal, reward, source):
    memory_store.log_lesson_usage(conn, [lesson_id], interaction_id, task)
    memory_store.record_lesson_usage_outcome(
        conn, interaction_id, signal, reward, source=source,
    )


def test_history_rows_carry_the_outcome_signal():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "l1", "Use pathlib.Path for joins.", None, "i-a")
    _score(conn, "l1", "i1", "path join", "tests_passed", 1.0, "machine")
    rows = memory_store.lesson_usage_history(conn)
    assert [r["outcome_signal"] for r in rows] == ["tests_passed"]
    # The columns every existing consumer reads are unchanged.
    for name in ("lesson_id", "interaction_id", "task", "reward", "evidence_ts"):
        assert name in rows[0].keys()
    conn.close()


def test_tested_evidence_ages_on_the_test_half_life():
    """A win proven by tests keeps a 180-day half-life, so at 90 days it is not
    stale; the same age on caller-judged evidence still is (30-day default)."""
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "tested", "Use pathlib.Path for joins.", None, "i-a")
    memory_store.add_lesson(conn, "accepted", "Use enumerate for indexes.", None, "i-b")
    _score(conn, "tested", "i1", "path join", "tests_passed", 1.0, "machine")
    _score(conn, "accepted", "i2", "loop index", "accepted", 0.8, "caller")

    aged = datetime.now(timezone.utc) + timedelta(days=90)
    stale = memory_quality.stale_lesson_findings(conn, now=aged)
    assert [f["id"] for f in stale] == ["accepted"]

    # Five test half-lives later the tested lesson has aged out too.
    much_later = datetime.now(timezone.utc) + timedelta(days=900)
    ids = sorted(f["id"] for f in memory_quality.stale_lesson_findings(conn, now=much_later))
    assert ids == ["accepted", "tested"]
    conn.close()


def test_newest_signal_picks_the_half_life():
    """Age runs from the newest scored outcome, so that outcome's type decays
    it: a tested lesson later judged 'accepted' is back on the default."""
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "l1", "Use pathlib.Path for joins.", None, "i-a")
    _score(conn, "l1", "i1", "path join", "tests_passed", 1.0, "machine")
    _score(conn, "l1", "i2", "path join", "accepted", 0.8, "caller")
    aged = datetime.now(timezone.utc) + timedelta(days=90)
    assert [f["id"] for f in memory_quality.stale_lesson_findings(conn, now=aged)] == ["l1"]
    conn.close()


def _untyped_store():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "w1", "Use pathlib.Path for joins.", None, "i-a")
    memory_store.add_lesson(conn, "w2", "Prefer f-strings.", None, "i-b")
    memory_store.add_lesson(conn, "w3", "Use enumerate for indexes.", None, "i-c")
    _score(conn, "w1", "i1", "path join", "accepted", 0.8, "caller")
    _score(conn, "w2", "i2", "fmt", "used", 0.9, "caller")
    _score(conn, "w2", "i3", "fmt", "failed", -1.0, "machine")
    _score(conn, "w3", "i4", "loop", "edited", 0.75, "caller")
    return conn


def test_untyped_stale_findings_are_identical_without_the_table(monkeypatch):
    """Evidence without a mapped type yields identical findings whether or not
    the typed table exists -- at every age and every caller half-life."""
    conn = _untyped_store()
    base = datetime.now(timezone.utc)
    for days in (7, 30, 61, 90, 120, 180, 400):
        for half_life in (lesson_decay.DEFAULT_HALF_LIFE_DAYS, 10.0):
            now = base + timedelta(days=days)
            typed = memory_quality.stale_lesson_findings(
                conn, now=now, half_life_days=half_life,
            )
            with monkeypatch.context() as patch:
                patch.setattr(lesson_decay, "SIGNAL_EVIDENCE_TYPES", {})
                patch.setattr(lesson_decay, "EVIDENCE_TYPE_HALF_LIFE_DAYS", {})
                untyped = memory_quality.stale_lesson_findings(
                    conn, now=now, half_life_days=half_life,
                )
            assert repr(typed) == repr(untyped)
    # Not vacuous: the untyped store does produce findings at 120 days.
    assert memory_quality.stale_lesson_findings(conn, now=base + timedelta(days=120))
    conn.close()


def _pruner_store(signal):
    conn = memory_store.connect(":memory:")
    seeds = [
        ("a1", "short lesson", [1.0, 0.0, 0.0], "2026-01-01 00:00:01"),
        ("a2", "a much longer and more detailed lesson text", [0.995, 0.005, 0.0],
         "2026-01-01 00:00:02"),
        ("b1", "same length text", [0.0, 1.0, 0.0], "2026-01-01 00:00:03"),
        ("b2", "same length text", [0.0, 0.985, 0.015], "2026-01-01 00:00:04"),
        ("u1", "totally unrelated unique lesson", [0.0, 0.0, 1.0], "2026-01-01 00:00:05"),
    ]
    for lesson_id, text, vector, ts in seeds:
        memory_store.add_lesson(
            conn, lesson_id, text, embeddings.to_blob(vector), source_interaction=None,
            embedding_model="embed-v1", embedding_revision="rev-1",
        )
        conn.execute("UPDATE lessons SET ts=? WHERE id=?", (ts, lesson_id))
        conn.commit()
    if signal is not None:
        reward = {"tests_passed": 1.0, "compiled": 0.7, "accepted": 0.8}[signal]
        for n, lesson_id in enumerate(("a1", "a2", "b1", "b2", "u1")):
            source = "caller" if signal == "accepted" else "machine"
            _score(conn, lesson_id, "i%d" % n, "task", signal, reward, source)
    return conn


def test_pruner_plan_and_deletions_unchanged_by_evidence_type():
    """lesson_pruner never consulted decay; typed evidence must not change the
    keeper, the losers, or what apply_plan deletes."""
    results = {}
    for signal in (None, "accepted", "tests_passed", "compiled"):
        conn = _pruner_store(signal)
        plan = lesson_pruner.build_plan(conn)
        deleted = lesson_pruner.apply_plan(conn, plan)
        remaining = sorted(r[0] for r in conn.execute("SELECT id FROM lessons").fetchall())
        results[signal] = (
            [(e["keeper_id"], e["prune_ids"], e["max_sim"]) for e in plan], deleted, remaining,
        )
        conn.close()
    # Baseline is non-vacuous: two clusters, two deletions, unique survives.
    assert sorted((keeper, prune) for keeper, prune, _sim in results[None][0]) == [
        ("a2", ["a1"]), ("b1", ["b2"]),
    ]
    assert results[None][1] == 2
    assert results[None][2] == ["a2", "b1", "u1"]
    for signal in ("accepted", "tests_passed", "compiled"):
        assert results[signal] == results[None]
