"""outcomes.source — provenance for every recorded verdict (#62).

The store held 9,450 rows of ``(interaction_id, signal, reward, ts)`` and no
record of *who* judged. A machine verdict from ``grounded_outcomes.attribute``
was byte-identical to a human pressing "accepted", forever, which is why three
separate lanes could not quantify their own findings. These tests pin the
column, the vocabulary, the refusal to guess a backfill, the consumers that
must not blend the populations, and the gates that read those numbers.
"""
import ast
import sqlite3
from pathlib import Path

import pytest

import calibration
import learning_health
import memory_store as ms
import retriever
from sonder_runtime.domain.memory import rules


REPO_ROOT = Path(__file__).resolve().parent.parent


def _conn():
    return ms.connect(":memory:")


def _interaction(conn, interaction_id, task="t", response="r", tier="code"):
    ms.log_interaction(conn, interaction_id, task, "", response, tier)


# --- the column itself ------------------------------------------------------


def test_outcomes_carries_a_source_column():
    conn = _conn()
    assert "source" in {r[1] for r in conn.execute("PRAGMA table_info(outcomes)")}


def test_a_row_with_no_source_is_impossible_at_the_storage_layer():
    """The strongest available guard: not a convention, a NOT NULL constraint.

    A default would recreate the defect — a future writer that forgets would
    silently file rows under whatever the default means. There is no default.
    """
    conn = _conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO outcomes(interaction_id, signal, reward) "
            "VALUES('i1', 'accepted', 0.8)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO outcomes(interaction_id, signal, reward, source) "
            "VALUES('i1', 'accepted', 0.8, NULL)"
        )


def test_the_source_vocabulary_is_closed():
    """'human' is not a value. An invented label is worse than 'unknown'."""
    conn = _conn()
    for bad in ("human", "", "Caller", "agent"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO outcomes(interaction_id, signal, reward, source) "
                "VALUES(?, 'accepted', 0.8, ?)",
                ("i-%s" % bad, bad),
            )


def test_record_outcome_row_refuses_to_infer_a_source():
    conn = _conn()
    with pytest.raises(TypeError):
        ms.record_outcome_row(conn, "i1", "accepted", 0.8)
    with pytest.raises(ValueError):
        ms.record_outcome_row(conn, "i1", "accepted", 0.8, source="human")


def test_record_outcome_and_claim_refuses_to_infer_a_source():
    conn = _conn()
    _interaction(conn, "i1")
    with pytest.raises(TypeError):
        ms.record_outcome_and_claim_lesson_distillation(conn, "i1", "accepted", 0.8)


# --- the backfill -----------------------------------------------------------


def test_legacy_rows_become_unknown_and_keep_every_other_field(tmp_path):
    """Pre-column rows are 'unknown'. Nothing about them is inferable.

    A plausible label would be permanently worse than an honest one: the whole
    defect is that the populations cannot be told apart.
    """
    path = tmp_path / "legacy-memory.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE outcomes (interaction_id TEXT, signal TEXT, "
        "reward REAL, ts TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    legacy.executemany(
        "INSERT INTO outcomes(interaction_id, signal, reward, ts) VALUES(?,?,?,?)",
        [
            ("old-1", "tests_passed", 1.0, "2025-01-01T00:00:00"),
            ("old-2", "accepted", 0.8, "2025-01-02T00:00:00"),
            ("old-3", "failed", -1.0, "2025-01-03T00:00:00"),
        ],
    )
    legacy.commit()
    legacy.close()

    conn = ms.connect(path)
    rows = conn.execute(
        "SELECT interaction_id, signal, reward, ts, source FROM outcomes "
        "ORDER BY interaction_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("old-1", "tests_passed", 1.0, "2025-01-01T00:00:00", "unknown"),
        ("old-2", "accepted", 0.8, "2025-01-02T00:00:00", "unknown"),
        ("old-3", "failed", -1.0, "2025-01-03T00:00:00", "unknown"),
    ]
    # and the rebuild leaves the uniqueness invariant standing
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        ("uq_outcomes_interaction_signal_nonnull",),
    ).fetchone() is not None


def test_the_migration_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    first = ms.connect(path)
    ms.record_outcome_row(
        first, "i1", "accepted", 0.8, source=rules.OUTCOME_SOURCE_CALLER,
    )
    first.close()
    second = ms.connect(path)
    assert second.execute(
        "SELECT source FROM outcomes WHERE interaction_id='i1'"
    ).fetchone()[0] == "caller"


# --- separability -----------------------------------------------------------


def test_the_two_populations_are_now_separable():
    conn = _conn()
    ms.record_outcome_row(conn, "a", "accepted", 0.8, source="caller")
    ms.record_outcome_row(conn, "b", "accepted", 0.8, source="machine")
    ms.record_outcome_row(conn, "c", "tests_passed", 1.0, source="self_curriculum")
    assert ms.outcome_signal_counts(conn) == {"accepted": 2, "tests_passed": 1}
    assert ms.outcome_signal_counts(conn, sources={"caller"}) == {"accepted": 1}
    assert ms.outcome_signal_counts(conn, sources={"machine"}) == {"accepted": 1}
    assert ms.outcome_source_counts(conn) == {
        "caller": 1, "machine": 1, "self_curriculum": 1,
    }


# --- consumer: calibration.measure, and the gate that reads it --------------


def test_calibration_caller_population_is_caller_sourced_only():
    """`accepted` is written by machines too (artifact_verify, ground_artifact).

    Before the column, a machine `accepted` counted toward the one number that
    claims to measure how good delegated work is.
    """
    conn = _conn()
    for n in range(30):
        ms.record_outcome_row(conn, "m%d" % n, "accepted", 0.8, source="machine")
    m = calibration.measure(conn, "caller")
    assert m.total == 0
    assert m.verdict == calibration.UNMEASURED

    for n in range(30):
        ms.record_outcome_row(conn, "c%d" % n, "accepted", 0.8, source="caller")
    m = calibration.measure(conn, "caller")
    assert m.total == 30
    assert m.good == 30


def test_should_verify_fails_closed_on_a_store_of_unknown_provenance():
    """9,450 legacy rows are not 9,450 caller judgements. Not knowing = verify."""
    conn = _conn()
    for n in range(200):
        conn.execute(
            "INSERT INTO outcomes(interaction_id, signal, reward, source) "
            "VALUES(?, 'accepted', 0.8, 'unknown')",
            ("u%d" % n,),
        )
    conn.commit()
    verify, reason = calibration.should_verify(conn, "caller")
    assert verify is True
    assert "unknown" in reason.lower() or "too few" in reason.lower()


# --- consumer: lesson_usage_stats, and lesson_quarantine ---------------------


def _usage_row(conn, lesson_id, interaction_id, task, reward, source, ts):
    conn.execute(
        "INSERT INTO lesson_usage(lesson_id, interaction_id, task, "
        "outcome_signal, reward, outcome_ts, outcome_source) "
        "VALUES(?,?,?,?,?,?,?)",
        (lesson_id, interaction_id, task, "failed", reward, ts, source),
    )
    conn.commit()


def test_lesson_usage_stats_ignores_heuristically_attributed_evidence():
    """This is the gate that evicts lessons from retrieval.

    Machine-attributed verdicts must not drive it: that is what made routing
    `_record_outcome_signal` through the wrapper unsafe before this column.
    """
    conn = _conn()
    ms.add_lesson(conn, "L1", "a lesson", None, "src")
    for n in range(8):
        _usage_row(
            conn, "L1", "mi%d" % n, "task %d" % n, -1.0, "attributed",
            "2025-02-%02dT00:00:00" % (n + 1),
        )
    stats = ms.lesson_usage_stats(conn)
    assert stats.get("L1", {}).get("losses_since_win", 0) == 0
    assert retriever.lesson_quarantine(stats.get("L1", {})).get("active") is not True


def test_lesson_usage_stats_still_reads_caller_and_legacy_evidence():
    """The filter must not silently shrink the population it measures.

    A gate that stops firing because its input vanished is the classic
    'improvement that is really a floor'.
    """
    conn = _conn()
    ms.add_lesson(conn, "L1", "a lesson", None, "src")
    for n in range(8):
        _usage_row(
            conn, "L1", "ci%d" % n, "task %d" % n, -1.0, "caller",
            "2025-02-%02dT00:00:00" % (n + 1),
        )
    ms.add_lesson(conn, "L2", "legacy lesson", None, "src")
    for n in range(8):
        # NULL outcome_source: written before the column existed
        _usage_row(
            conn, "L2", "li%d" % n, "task %d" % n, -1.0, None,
            "2025-02-%02dT00:00:00" % (n + 1),
        )
    stats = ms.lesson_usage_stats(conn)
    assert stats["L1"]["losses_since_win"] == 8
    assert stats["L2"]["losses_since_win"] == 8


def test_the_lifetime_counters_also_exclude_attributed_evidence():
    """`wins`/`losses`/`avg_reward` are a second path into the same gate.

    `lesson_quarantine` reads `wins + losses` as its sample size and feeds it
    to `band_loss_rate`, which sets the reference class a lesson is judged
    against. Filtering only the epoch walk and leaving these would let
    attributed rows move the threshold while being invisible in the evidence
    -- caught here because a mutation of exactly this filter survived every
    other test in this file.
    """
    conn = _conn()
    ms.add_lesson(conn, "L1", "a lesson", None, "src")
    _usage_row(conn, "L1", "keep", "task", -1.0, "caller", "2025-05-01T00:00:00")
    for n in range(9):
        _usage_row(
            conn, "L1", "drop%d" % n, "task %d" % n, -1.0, "attributed",
            "2025-05-%02dT00:00:00" % (n + 2),
        )
    row = ms.lesson_usage_stats(conn)["L1"]
    assert row["losses"] == 1
    assert row["wins"] == 0
    assert row["avg_reward"] == -1.0
    # `uses` still counts every retrieval: that is what the word means, and
    # shrinking it here would misreport how often the lesson was served.
    assert row["uses"] == 10


def test_directly_graded_machine_evidence_still_drives_the_gate():
    """The line is NOT machine-versus-human, and getting that wrong is a regression.

    The code gate running a reply's own code and finding it broken is direct
    evidence that the lesson retrieved for that reply did not help, and it has
    driven quarantine for as long as it has existed. Excluding it because it is
    "machine" would be a gate that silently stopped firing.
    """
    conn = _conn()
    ms.add_lesson(conn, "L1", "a lesson", None, "src")
    for n in range(8):
        _usage_row(
            conn, "L1", "gi%d" % n, "task %d" % n, -1.0, "machine",
            "2025-04-%02dT00:00:00" % (n + 1),
        )
    assert ms.lesson_usage_stats(conn)["L1"]["losses_since_win"] == 8


def test_attributable_losses_also_skip_heuristically_attributed_evidence():
    conn = _conn()
    ms.add_lesson(conn, "L1", "a lesson", None, "src")
    for n in range(6):
        _usage_row(
            conn, "L1", "mi%d" % n, "task %d" % n, -1.0, "attributed",
            "2025-03-%02dT00:00:00" % (n + 1),
        )
    stats = retriever.usage_stats_with_attribution(conn)
    assert stats.get("L1", {}).get("attributable_losses_since_win", 0) == 0


# --- consumer: learning_health ---------------------------------------------


def test_learning_health_reviewed_rate_is_caller_sourced_only():
    conn = _conn()
    for n in range(40):
        _interaction(conn, "m%d" % n)
        ms.record_outcome_row(conn, "m%d" % n, "accepted", 0.8, source="machine")
    for n in range(10):
        _interaction(conn, "c%d" % n)
        ms.record_outcome_row(conn, "c%d" % n, "rejected", -0.5, source="caller")
    report = learning_health.build_report(conn)
    assert report["reviewed_outcomes"] == 10
    assert report["reviewed_positive_percent"] == 0.0
    assert report["outcomes_by_source"] == {"caller": 10, "machine": 40}


def test_learning_health_reports_the_unknown_population_rather_than_hiding_it():
    conn = _conn()
    for n in range(5):
        _interaction(conn, "u%d" % n)
        conn.execute(
            "INSERT INTO outcomes(interaction_id, signal, reward, source) "
            "VALUES(?, 'accepted', 0.8, 'unknown')",
            ("u%d" % n,),
        )
    conn.commit()
    report = learning_health.build_report(conn)
    assert report["unknown_source_outcomes"] == 5
    assert report["reviewed_outcomes"] == 0


# --- writers ----------------------------------------------------------------


def test_the_heuristic_attribution_path_writes_attributed(tmp_path, monkeypatch):
    import server

    path = tmp_path / "srv.db"
    monkeypatch.setattr(server, "_DB_PATH", str(path))
    conn = ms.connect(path)
    _interaction(conn, "gen-1")
    conn.close()
    server._record_outcome_signal("gen-1", "tests_passed")
    conn = ms.connect(path)
    assert conn.execute(
        "SELECT source FROM outcomes WHERE interaction_id='gen-1'"
    ).fetchone()[0] == "attributed"


def test_the_caller_tool_writes_caller(tmp_path, monkeypatch):
    import server

    path = tmp_path / "srv2.db"
    monkeypatch.setattr(server, "_DB_PATH", str(path))
    conn = ms.connect(path)
    _interaction(conn, "call-1")
    conn.close()
    server.record_outcome("call-1", "accepted")
    conn = ms.connect(path)
    assert conn.execute(
        "SELECT source FROM outcomes WHERE interaction_id='call-1'"
    ).fetchone()[0] == "caller"


def test_the_runtime_code_gate_negative_writes_machine(tmp_path, monkeypatch):
    import server

    path = tmp_path / "srv3.db"
    monkeypatch.setattr(server, "_DB_PATH", str(path))
    conn = ms.connect(path)
    _interaction(conn, "gate-1")
    conn.close()
    server._record_code_gate_failure("gate-1")
    conn = ms.connect(path)
    assert conn.execute(
        "SELECT source FROM outcomes WHERE interaction_id='gate-1'"
    ).fetchone()[0] == "machine"


# --- item 5: the bypass, now that provenance exists -------------------------


def test_the_attribution_writer_now_credits_lesson_usage(tmp_path, monkeypatch):
    """`_record_outcome_signal` skipped the lesson_usage credit entirely.

    With provenance it can route through the wrapper: the credit is recorded
    and tagged `machine`, and the eviction gate filters it out.
    """
    import server

    path = tmp_path / "srv4.db"
    monkeypatch.setattr(server, "_DB_PATH", str(path))
    conn = ms.connect(path)
    _interaction(conn, "used-1")
    ms.add_lesson(conn, "L1", "a lesson", None, "src")
    ms.log_lesson_usage(conn, ["L1"], "used-1", "the task")
    conn.close()

    server._record_outcome_signal("used-1", "failed")

    conn = ms.connect(path)
    row = conn.execute(
        "SELECT reward, outcome_source FROM lesson_usage "
        "WHERE interaction_id='used-1'"
    ).fetchone()
    assert row is not None
    assert row["reward"] == -1.0
    assert row["outcome_source"] == "attributed"
    # ...and it does not reach the eviction gate
    assert ms.lesson_usage_stats(conn)["L1"]["losses_since_win"] == 0


def test_the_attribution_writer_will_not_write_an_orphan_row(tmp_path, monkeypatch):
    """The wrapper's interaction-existence precondition now binds here too."""
    import server

    path = tmp_path / "srv5.db"
    monkeypatch.setattr(server, "_DB_PATH", str(path))
    ms.connect(path).close()
    server._record_outcome_signal("no-such-interaction", "failed")
    conn = ms.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_the_attribution_writer_does_not_claim_a_distillation(tmp_path, monkeypatch):
    """It has no path to FINISH one, and a stranded claim burns the only slot.

    The interaction is seeded with a caller's own good outcome first, so
    ``_distillation_evidence`` reports ``has_good`` on its own merits. Without
    that the case is decided by the provenance filter on good evidence and this
    test would pass whether ``claim_distillation`` were False or True -- which
    is exactly what a mutation showed before the setup was strengthened.
    """
    import server

    path = tmp_path / "srv6.db"
    monkeypatch.setattr(server, "_DB_PATH", str(path))
    conn = ms.connect(path)
    _interaction(conn, "good-1")
    ms.record_outcome_row(conn, "good-1", "accepted", 0.8, source="caller")
    has_good, contradiction = ms._distillation_evidence(conn, "good-1")
    assert (has_good, contradiction) == (True, False)
    conn.execute("DELETE FROM lesson_distillations")
    conn.commit()
    conn.close()

    server._record_outcome_signal("good-1", "tests_passed")

    conn = ms.connect(path)
    state = conn.execute(
        "SELECT state FROM lesson_distillations WHERE interaction_id='good-1'"
    ).fetchone()
    assert state is None or state[0] != ms.DISTILLATION_CLAIMED


def test_attributed_evidence_can_block_a_lesson_but_never_ground_one(tmp_path):
    """A lesson is durable; the evidence grounding it must not be a guess.

    `compiled` (0.70) is a weak positive: not good enough to ground a lesson,
    not negative enough to contradict one. Before the provenance filter, a
    caller recording it could claim a distillation whose only *good* evidence
    was an attributed row nobody reviewed.
    """
    conn = ms.connect(tmp_path / "grounding.db")
    _interaction(conn, "i1")
    ms.record_outcome_row(conn, "i1", "tests_passed", 1.0, source="attributed")
    assert ms._distillation_evidence(conn, "i1") == (False, False)

    result = ms.record_outcome_and_claim_lesson_distillation(
        conn, "i1", "compiled", 0.7, source="caller",
    )
    assert result["claimed"] is False

    # A caller's own good evidence still grounds it...
    _interaction(conn, "i2")
    ms.record_outcome_row(conn, "i2", "tests_passed", 1.0, source="caller")
    assert ms._distillation_evidence(conn, "i2") == (True, False)

    # ...and an attributed NEGATIVE still contradicts, because losing a real
    # negative is the worse mistake.
    _interaction(conn, "i3")
    ms.record_outcome_row(conn, "i3", "tests_passed", 1.0, source="caller")
    ms.record_outcome_row(conn, "i3", "failed", -1.0, source="attributed")
    assert ms._distillation_evidence(conn, "i3") == (True, True)


# --- the guard --------------------------------------------------------------


_PRODUCTION_SKIP = ("tests", "app", "proposals", ".git", "seed", "datasets")
# Directories that are not this repository's source wherever they appear.
# _PRODUCTION_SKIP only matches the first path component, so a nested git
# worktree under .claude/worktrees/ or a virtualenv under .runtime/ was walked
# in full -- and the worktree's own tests/ directory then reported as a
# production offender. These are checkouts of other code, not code to audit.
_NESTED_SKIP = frozenset({
    ".claude", ".runtime", ".git", ".venv", "venv", "env",
    "node_modules", "site-packages", "__pycache__", ".tox", "build", "dist",
})


def _production_python_files():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in _PRODUCTION_SKIP:
            continue
        if _NESTED_SKIP.intersection(rel.parts):
            continue
        yield path


def _outcome_inserts(tree):
    """Every string constant in the file that inserts into `outcomes`."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = " ".join(node.value.split()).lower()
            if "into outcomes" in text:
                found.append(node.value)
    return found


def test_no_production_insert_into_outcomes_omits_the_source_column():
    """A new writer cannot omit provenance and still look like it wrote a row.

    The storage layer refuses at runtime (NOT NULL); this catches it at the
    source, before it ships, and names the file.
    """
    offenders = []
    for path in _production_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for statement in _outcome_inserts(tree):
            if "source" not in statement.lower():
                offenders.append("%s: %s" % (path.relative_to(REPO_ROOT), statement))
    assert offenders == []


def test_the_guard_would_catch_a_writer_that_omitted_it(tmp_path):
    """The scanner is proved by a plant held OUTSIDE the repository tree."""
    planted = tmp_path / "planted_writer.py"
    planted.write_text(
        'def w(conn):\n'
        '    conn.execute("INSERT INTO outcomes(interaction_id, signal, reward) '
        'VALUES(?,?,?)", ("i", "accepted", 0.8))\n',
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    statements = _outcome_inserts(tree)
    assert statements, "the scanner did not see the planted INSERT at all"
    assert all("source" not in s.lower() for s in statements)


def test_production_scanner_still_covers_the_repo_after_the_nested_skip():
    """The nested-checkout exclusion must not quietly empty the scan.

    An exclusion added to silence a false positive is one edit away from
    excluding everything, and a guard that walks nothing passes for free -- it
    reports "no offenders" exactly as loudly when it examined 368 files as when
    it examined none. Pin both ends: real production modules are still scanned,
    and nothing from a nested checkout is.
    """
    scanned = {
        path.relative_to(REPO_ROOT).as_posix() for path in _production_python_files()
    }
    assert "memory_store.py" in scanned
    assert "server.py" in scanned
    assert len(scanned) > 100
    assert not [name for name in scanned if _NESTED_SKIP.intersection(name.split("/"))]
