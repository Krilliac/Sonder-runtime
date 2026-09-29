"""Write-time quality checks for memory facts and lessons (report-only)."""
import time

import pytest

import memory_quality
import memory_store
from sonder_runtime.domain.memory import write_quality as wq

# --- atomic (one claim) -----------------------------------------------------


@pytest.mark.parametrize("text", [
    "Use pathlib.Path for path joins.",
    "Run pytest with -n 6. It keeps the suite under two minutes.",
    "Prefer `a; b; c` style only inside shell snippets.",
    "Build and test with build.ps1 before pushing.",  # list "and", not a clause
    "The repo uses ruff, mypy and pytest in CI.",
])
def test_atomic_text_is_not_multi_claim(text):
    assert wq.MULTI_CLAIM not in wq.classify(text)


@pytest.mark.parametrize("text", [
    "Use pathlib for paths; prefer f-strings, and always run black before commit.",
    "The server listens on 11435. Logs go to runtime/logs. Backups run nightly.",
    "Tests live in tests/, but fixtures live in conftest.py; additionally the CI uses -n 6.",
])
def test_conjunction_heavy_text_is_multi_claim(text):
    assert wq.MULTI_CLAIM in wq.classify(text)


# --- self-contained ---------------------------------------------------------


@pytest.mark.parametrize("text", [
    "This project builds with build.ps1 only.",       # scope resolves "this project"
    "It is safer to pin ruff in CI configs.",          # expletive "it"
    "There are two gates: lint ratchet and architecture.",  # existential "there"
    "Sonder stores facts per project in memory.db.",
    "Iterate over dict.items() when both key and value are needed.",
])
def test_self_contained_text_has_no_unresolved_reference(text):
    assert wq.UNRESOLVED_REFERENCE not in wq.classify(text)


@pytest.mark.parametrize("text", [
    "This breaks when the cache is cold.",
    "It needs a restart after config changes.",
    "The above applies to release builds too.",
    "That file must never be edited by hand.",
    "Always run the linter, same as before.",
    "Remember to regenerate the baseline as mentioned for server.py.",
])
def test_leading_or_backward_reference_is_unresolved(text):
    assert wq.UNRESOLVED_REFERENCE in wq.classify(text)


# --- dated when time-sensitive -----------------------------------------------


@pytest.mark.parametrize("text", [
    "Use pathlib.Path for path joins.",                     # not time-sensitive
    "Node1 is currently at 10.77.0.2 (verified 2026-08-17).",  # dated
    "As of September 2026 the latest CMake is 4.1.",        # dated
    "Python 3.12 is the venv interpreter since 2025.",      # dated version
    "As of v2.4 the runtime currently defaults to WAL.",    # version-anchored
    "Reward thresholds sit at 0.71 for distillation.",      # bare decimal, not a version
])
def test_timeless_or_dated_text_is_not_flagged_time_sensitive(text):
    assert wq.UNDATED_TIME_SENSITIVE not in wq.classify(text)


@pytest.mark.parametrize("text", [
    "The venv currently runs Python on Windows.",
    "Always install the latest ruff before linting.",
    "Unreal builds need UE 5.8 installed.",
    "Pin requests==2.31.0 in the lock file.",
    "The runtime now defaults to WAL journaling.",
    "As of now the runtime defaults to WAL journaling.",  # "as of" without a date
    "The latest ruff is required as of today.",
])
def test_time_sensitive_text_without_a_date_is_flagged(text):
    assert wq.UNDATED_TIME_SENSITIVE in wq.classify(text)


# --- bounded length ---------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Use pathlib.Path for path joins.",
    "x" * 10 + " spans three words",
    "word " * 50,  # 250 chars, under the cap
])
def test_bounded_length_is_not_flagged(text):
    assert wq.LENGTH_OUT_OF_BOUNDS not in wq.classify(text)


@pytest.mark.parametrize("text", [
    "ok",
    "use ruff",
    "a " + "long " * 80,  # > MAX_CHARS
])
def test_too_short_or_too_long_is_flagged(text):
    assert wq.LENGTH_OUT_OF_BOUNDS in wq.classify(text)


def test_empty_text_is_not_a_writing_finding():
    assert wq.classify("") == []
    assert wq.classify("   ") == []


def test_well_written_fact_passes_every_check():
    assert wq.classify("Sonder writes lesson rows through memory_store.add_lesson.") == []


def test_reasons_follow_the_stable_check_order():
    text = "It currently works; restart it, and " + "padding words " * 30
    reasons = wq.classify(text)
    assert reasons == [name for name in wq.CHECKS if name in reasons]
    assert set(reasons) == set(wq.CHECKS)


# --- store integration ------------------------------------------------------


def _fixture_store():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "good", "Use pathlib.Path for joins.", None, "seed")
    memory_store.add_lesson(conn, "dup1", "Prefer early returns.", None, "seed")
    memory_store.add_lesson(conn, "dup2", "Prefer early returns.", None, "seed")
    memory_store.add_lesson(conn, "vague", "It currently needs care", None, "seed")
    memory_store.add_fact(conn, "f-good", "proj", "The repo builds with build.ps1 only.")
    memory_store.add_fact(conn, "f-bad", "proj", "This is the latest one")
    memory_store.add_fact(conn, "f-dup", "proj", "This is the latest one")
    return conn


def test_findings_count_facts_and_lessons_with_id_only_samples():
    conn = _fixture_store()
    section = memory_quality.write_quality_findings(conn)

    assert section["checked_facts"] == 3
    assert section["checked_lessons"] == 4
    assert section["flagged_facts"] == 2
    assert section["flagged_lessons"] == 1
    assert [(s["kind"], s["id"]) for s in section["samples"]] == [
        ("fact", "f-bad"), ("fact", "f-dup"), ("lesson", "vague"),
    ]
    assert section["by_check"][wq.UNRESOLVED_REFERENCE] == 3
    assert section["by_check"][wq.UNDATED_TIME_SENSITIVE] == 3
    # Samples carry ids and reason names, never stored text.
    assert "latest" not in repr(section["samples"])


def test_report_section_is_appended_and_existing_output_is_unchanged():
    conn = _fixture_store()
    base = memory_quality.audit(conn)
    extended = memory_quality.audit_with_write_quality(conn)

    assert "write_quality" not in base
    assert {k: v for k, v in extended.items() if k != "write_quality"} == base

    old_text = memory_quality.format_audit(base, sample_limit=5)
    new_text = memory_quality.format_audit(extended, sample_limit=5)
    assert new_text.startswith(old_text + "\n")
    added = new_text[len(old_text) + 1:].splitlines()
    assert added[0].startswith("  write quality (report-only, never repaired): 2 of 3 fact(s)")
    assert "  write quality samples (ids only):" in added
    assert "    fact f-bad [unresolved_reference,undated_time_sensitive]" in added


def test_sample_limit_bounds_the_printed_examples():
    conn = _fixture_store()
    text = memory_quality.format_audit(
        memory_quality.audit_with_write_quality(conn), sample_limit=1,
    )
    sample_lines = [ln for ln in text.splitlines() if ln.startswith("    fact ")
                    or ln.startswith("    lesson ")]
    assert sample_lines == ["    fact f-bad [unresolved_reference,undated_time_sensitive]"]


def test_repair_ignores_write_quality_findings():
    conn = _fixture_store()
    plan, deleted = memory_quality.repair_exact_duplicates(conn, apply=True)

    # Only the exact lesson duplicate goes; every write-quality-flagged fact
    # and lesson survives, including the flagged exact-duplicate facts.
    assert deleted == 1
    assert [e["prune_ids"] for e in plan] == [["dup2"]]
    assert memory_store.get_lesson_text(conn, "vague") == "It currently needs care"
    fact_ids = {r[0] for r in conn.execute("SELECT id FROM facts").fetchall()}
    assert fact_ids == {"f-good", "f-bad", "f-dup"}


def test_report_is_fast_on_ten_thousand_facts():
    conn = memory_store.connect(":memory:")
    templates = [
        "Sonder module {i} writes rows through memory_store.add_lesson.",
        "It currently needs the latest ruff; also run black, and pin mypy {i}.",
        "This breaks on cold caches for worker {i}",
        "The venv runs Python 3.12 for lane {i} (verified 2026-08-17).",
    ]
    conn.executemany(
        "INSERT INTO facts(id, project, text) VALUES(?, ?, ?)",
        [("f%05d" % i, "proj", templates[i % 4].format(i=i)) for i in range(10_000)],
    )
    conn.commit()

    started = time.perf_counter()
    text = memory_quality.format_audit(
        memory_quality.audit_with_write_quality(conn), sample_limit=20,
    )
    elapsed = time.perf_counter() - started

    assert "of 10000 fact(s)" in text
    assert elapsed < 2.0, "write-quality report took %.2fs on 10k facts" % elapsed
