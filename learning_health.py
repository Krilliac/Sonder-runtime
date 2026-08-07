"""Structured, read-only health metrics for Sonder's learning loop."""

from __future__ import annotations

import embeddings
import memory_quality
import memory_store
import retriever
import reward


def _percent(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(100.0 * float(numerator) / float(denominator), 1)


def _lesson_source(source: str | None, grounded: bool) -> str:
    """Bucket a lesson by provenance.

    A lesson that names an interaction id which no longer exists is neither
    "interaction" nor a seed family: it was earned and then lost its evidence.
    Bucketing it by ``split(":")[0]`` put the raw id in its own bucket, so a
    pruned interaction table would have exploded ``lesson_sources`` into one
    entry per orphaned lesson and quietly inflated the synthetic count.
    """
    if grounded:
        return "interaction"
    source = str(source or "").strip()
    if not source:
        return "unknown"
    if memory_quality.INTERACTION_ID_RE.match(source):
        return "orphaned"
    return source.split(":", 1)[0]


def _lesson_sources(conn) -> tuple[dict[str, int], int, int]:
    rows = conn.execute(
        "SELECT l.source_interaction AS source, "
        "CASE WHEN i.id IS NULL THEN 0 ELSE 1 END AS grounded "
        "FROM lessons l LEFT JOIN interactions i "
        "ON i.id=l.source_interaction"
    ).fetchall()
    sources: dict[str, int] = {}
    grounded = 0
    orphaned = 0
    for row in rows:
        is_grounded = bool(row["grounded"])
        bucket = _lesson_source(row["source"], is_grounded)
        sources[bucket] = sources.get(bucket, 0) + 1
        grounded += int(is_grounded)
        orphaned += int(bucket == "orphaned")
    return dict(sorted(sources.items())), grounded, orphaned


# Signals a machine produced by running the code, versus signals something
# with judgement recorded after looking at the answer. They measure different
# things and must not be averaged into one number: the autograded population is
# self-generated curriculum/ladder work (curriculum_run.py, game_ladder.py)
# that the runtime both sets and marks, and it outnumbers reviewed outcomes by
# more than an order of magnitude. A blended "positive percent" therefore
# reports how often the runtime passes its own exams, while reading like how
# often the model is right on a caller's real task.
_AUTOGRADED_SIGNALS = frozenset({"tests_passed", "failed", "compiled"})


def _outcome_metrics(conn) -> dict:
    rows = conn.execute(
        "SELECT signal, COUNT(*) AS count, AVG(reward) AS average_reward "
        "FROM outcomes GROUP BY signal"
    ).fetchall()
    signals = []
    outcomes = 0
    good_outcomes = 0
    autograded = 0
    autograded_good = 0
    reviewed = 0
    reviewed_good = 0
    for row in rows:
        signal = str(row["signal"])
        count = int(row["count"] or 0)
        good = reward.is_good(signal)
        if signal in _AUTOGRADED_SIGNALS:
            autograded += count
            autograded_good += count if good else 0
        else:
            reviewed += count
            reviewed_good += count if good else 0
        signals.append(
            {
                "signal": signal,
                "count": count,
                "average_reward": round(float(row["average_reward"] or 0.0), 3),
                "good": good,
            }
        )
        outcomes += count
        if good:
            good_outcomes += count
    signals.sort(key=lambda item: (-item["count"], item["signal"]))
    outcome_interactions = int(
        conn.execute(
            "SELECT COUNT(DISTINCT interaction_id) FROM outcomes"
        ).fetchone()[0]
    )
    good_signals = sorted(signal for signal in reward.VALID_SIGNALS if reward.is_good(signal))
    good_interactions = 0
    if good_signals:
        placeholders = ",".join("?" for _ in good_signals)
        good_interactions = int(
            conn.execute(
                "SELECT COUNT(DISTINCT interaction_id) FROM outcomes "
                "WHERE signal IN (%s)" % placeholders,
                tuple(good_signals),
            ).fetchone()[0]
        )
    return {
        "outcomes": outcomes,
        "outcome_interactions": outcome_interactions,
        "good_outcomes": good_outcomes,
        "bad_outcomes": outcomes - good_outcomes,
        "good_outcome_interactions": good_interactions,
        "positive_percent": _percent(good_outcomes, outcomes),
        # Split so the blended number cannot hide the one that matters. See
        # _AUTOGRADED_SIGNALS: reviewed_positive_percent is the model's hit rate
        # on work a caller actually delegated and then judged.
        "autograded_outcomes": autograded,
        "autograded_positive_percent": _percent(autograded_good, autograded),
        "reviewed_outcomes": reviewed,
        "reviewed_positive_percent": _percent(reviewed_good, reviewed),
        "reviewed_by_tier": _reviewed_by_tier(conn),
        "signals": signals,
    }


# The tier an outcome is attributed to, when its interaction row is gone. Kept
# in the breakdown rather than dropped: a per-tier table whose rows do not add
# up to reviewed_outcomes is the same "count that is really a floor" bug this
# module already carries scars from.
_UNATTRIBUTED_TIER = "(unattributed)"


def _reviewed_by_tier(conn) -> list[dict]:
    """Caller-judged outcomes split by the tier that produced the work.

    The aggregate reviewed rate says how often delegated work is rejected but
    not by what. Without the split the obvious reading is "the local model is
    too small, route to a bigger one" -- and measured on the live store that
    reading is wrong: the local `code` tier and the `cloud-code` tier land
    within a few points of each other, far inside the noise on the cloud tier's
    sample. The split is what makes that visible, so it ships next to the
    number it qualifies.
    """
    rows = conn.execute(
        "SELECT i.tier AS tier, o.signal AS signal, COUNT(*) AS count "
        "FROM outcomes o LEFT JOIN interactions i ON i.id = o.interaction_id "
        "GROUP BY i.tier, o.signal"
    ).fetchall()
    totals: dict[str, list[int]] = {}
    for row in rows:
        signal = str(row["signal"])
        if signal in _AUTOGRADED_SIGNALS:
            continue
        tier = str(row["tier"] or _UNATTRIBUTED_TIER)
        count = int(row["count"] or 0)
        bucket = totals.setdefault(tier, [0, 0])
        bucket[0] += count
        if reward.is_good(signal):
            bucket[1] += count
    return [
        {
            "tier": tier,
            "outcomes": total,
            "positive_percent": _percent(good, total),
        }
        for tier, (total, good) in sorted(
            totals.items(), key=lambda item: (-item[1][0], item[0])
        )
    ]


# Reference classes for interpreting a lesson's loss record, keyed by how many
# scored retrievals the lesson has. They exist because the loss rate on the
# live store collapses as retrieval count rises -- measured 2026-08-06 over
# 9256 scored retrievals of 346 lessons:
#
#   1 retrieval    81 lessons     81 rows   50.6% lost
#   2-3            41 lessons     97 rows   43.3% lost
#   4-10          119 lessons    694 rows   14.7% lost
#   11-100         87 lessons   3128 rows    7.8% lost
#   100+           18 lessons   5256 rows    1.2% lost
#
# That gradient is not lesson quality. Retrieval is similarity-driven, so a
# rarely-retrieved lesson is by construction the one pulled in for an unusual
# task, and unusual tasks fail. The corpus-wide 5.3% loss rate is set by the 18
# lessons that ride along on the repetitive curriculum (57% of all scored
# retrievals). Judging a single-retrieval lesson against that 5.3% overstates
# its badness by roughly a factor of ten.
_EVIDENCE_BANDS = ((1, 1), (2, 3), (4, 10), (11, 100), (101, None))
# A loss-only record is "beyond its reference class" when the band's own loss
# rate makes it a 1-in-100 event. Under the live band rates that needs about
# four consecutive losses; three or fewer is ordinary.
_LOSS_ONLY_IMPROBABLE_P = 0.01


def _band_key(uses: int) -> str:
    for low, high in _EVIDENCE_BANDS:
        if uses >= low and (high is None or uses <= high):
            return "%d+" % low if high is None else (
                "%d" % low if low == high else "%d-%d" % (low, high)
            )
    return "0"


def _scored_retrievals(conn) -> dict[str, dict]:
    """Per-lesson scored-retrieval counts, straight from ``lesson_usage``.

    ``lesson_usage_stats`` counts ``uses`` across every retrieval including the
    ungraded ones, and its wins/losses ignore rewards of exactly 0.0, so it
    cannot answer "out of how many scored retrievals". This can.
    """
    rows = conn.execute(
        "SELECT lesson_id, COUNT(*) AS scored, "
        "SUM(CASE WHEN reward < 0 THEN 1 ELSE 0 END) AS losses "
        "FROM lesson_usage WHERE reward IS NOT NULL GROUP BY lesson_id"
    ).fetchall()
    return {
        row["lesson_id"]: {
            "scored": int(row["scored"] or 0),
            "losses": int(row["losses"] or 0),
        }
        for row in rows
    }


def _loss_only_reference(scored: dict[str, dict], loss_only_ids: set) -> dict:
    """Expected loss-only count if lessons had no effect on their tasks.

    For each lesson, the reference rate is its own band's loss rate computed
    leave-one-out (the lesson's rows removed), so a lesson cannot be its own
    baseline. Expected P(all scored retrievals lost) is that rate raised to the
    lesson's retrieval count. Summing over lessons gives the count the store
    would show from task difficulty alone.
    """
    bands: dict[str, list[int]] = {}
    for row in scored.values():
        band = bands.setdefault(_band_key(row["scored"]), [0, 0])
        band[0] += row["scored"]
        band[1] += row["losses"]
    total_rows = sum(row["scored"] for row in scored.values())
    total_losses = sum(row["losses"] for row in scored.values())
    overall = (total_losses / total_rows) if total_rows else 0.0

    expected = 0.0
    beyond = 0
    for lesson_id, row in scored.items():
        uses = row["scored"]
        if not uses:
            continue
        band_rows, band_losses = bands[_band_key(uses)]
        remaining = band_rows - uses
        rate = (
            (band_losses - row["losses"]) / remaining if remaining > 0 else overall
        )
        rate = min(1.0, max(0.0, rate))
        probability = rate ** uses
        expected += probability
        if lesson_id in loss_only_ids and probability < _LOSS_ONLY_IMPROBABLE_P:
            beyond += 1
    return {
        "scored_retrievals": total_rows,
        "scored_retrieval_loss_percent": _percent(total_losses, total_rows),
        "loss_only_expected_from_task_difficulty": round(expected, 1),
        "loss_only_beyond_reference_class": beyond,
    }


def _lesson_outcome_metrics(conn) -> dict:
    # Enrich with per-interaction blame before asking about quarantine. A bare
    # usage-stats row says a lesson lost N times but not whether those failures
    # were its own: 493 loss rows on the live store trace to 144 distinct
    # failing interactions, 3.42 lessons blamed each. Without this, retriever
    # falls back to the undiscounted upper bound and this report says 6
    # quarantined while retrieval actually excludes 0 -- a monitoring surface
    # disagreeing with the behaviour it exists to monitor.
    try:
        stats = retriever.usage_stats_with_attribution(conn)
    except Exception:
        stats = memory_store.lesson_usage_stats(conn)
    scored = _scored_retrievals(conn)
    evaluated = 0
    with_losses = 0
    loss_only_ids = set()
    single_evidence = 0
    quarantined = 0
    quarantined_ids = []
    quarantine_details = []
    for lesson_id, row in stats.items():
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        if wins + losses:
            evaluated += 1
        if losses:
            with_losses += 1
        if losses and not wins:
            loss_only_ids.add(lesson_id)
            if scored.get(lesson_id, {}).get("scored", 0) <= 1:
                single_evidence += 1
        decision = retriever.lesson_quarantine(row)
        if decision.get("active"):
            quarantined += 1
            quarantined_ids.append(lesson_id)
            if len(quarantine_details) < 10:
                quarantine_details.append({
                    "lesson_id": lesson_id,
                    "losses_since_win": decision.get("losses_since_win", 0),
                    "distinct_loss_tasks_since_win": decision.get(
                        "distinct_loss_tasks_since_win", 0
                    ),
                    "avg_reward_since_win": decision.get(
                        "avg_reward_since_win"
                    ),
                    "last_failure_ts": decision.get("last_failure_ts"),
                    "retry_after": decision.get("retry_after"),
                })
    reference = _loss_only_reference(scored, loss_only_ids)
    metrics = {
        "evaluated_lessons": evaluated,
        "lessons_with_losses": with_losses,
        "loss_only_lessons": len(loss_only_ids),
        "loss_only_single_retrieval_lessons": single_evidence,
        "quarantined_lessons": quarantined,
        "quarantined_lesson_details": quarantine_details,
        "loss_attribution_note": (
            "A loss is not per-lesson evidence. record_lesson_usage_outcome "
            "writes one task's reward onto every lesson retrieved for it, and "
            "rarely-retrieved lessons are the ones pulled in for unusual "
            "tasks, which fail far more often. Read loss-only against "
            "loss_only_expected_from_task_difficulty, not against zero."
        ),
        "quarantine_review": (
            "Quarantine lifts on a timer (retry_after), not on evidence: a "
            "quarantined lesson is excluded from retrieval, so it cannot earn "
            "the win that would clear it until the cooldown expires. A "
            "grounded success resets the evidence epoch."
        ),
    }
    metrics.update(reference)
    metrics.update(_shared_blame_metrics(conn, loss_only_ids, quarantined_ids))
    return metrics


def _distinct_failed_interactions(conn, lesson_ids) -> int:
    ids = list(lesson_ids)
    seen = set()
    for offset in range(0, len(ids), 900):
        batch = ids[offset:offset + 900]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        seen.update(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT interaction_id FROM lesson_usage "
                "WHERE reward < 0 AND lesson_id IN (%s)" % placeholders,
                tuple(batch),
            ).fetchall()
        )
    return len(seen)


def _shared_blame_metrics(conn, loss_only_ids, quarantined_ids) -> dict:
    """How many distinct failures the loss counts are actually built from.

    One failing task marks every lesson retrieved alongside it -- 3.42 of them on
    average -- so N lessons can be condemned by far fewer than N independent
    failures. On the live store the six quarantined lessons trace to 15 failing
    interactions, and four of the six crossed the threshold on the identical
    five: one co-retrieval cluster, counted four times as separate evidence.
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT interaction_id) AS failures, COUNT(*) AS rows_ "
        "FROM lesson_usage WHERE reward < 0"
    ).fetchone()
    failures = int(row["failures"] or 0)
    marked = int(row["rows_"] or 0)
    return {
        "failed_interactions": failures,
        "lessons_marked_per_failed_interaction": (
            round(marked / failures, 2) if failures else 0.0
        ),
        "loss_only_distinct_failed_interactions": _distinct_failed_interactions(
            conn, loss_only_ids
        ),
        "quarantine_distinct_failed_interactions": _distinct_failed_interactions(
            conn, quarantined_ids
        ),
    }


# The four states a distillation row can finish in.
# ``memory_store.distillation_reason_counts`` takes a state filter and applies
# none by default, so an in-flight claimed or retryable row would otherwise land
# in the unrecorded bucket. That row has no reason YET, which is a different
# thing from having finished without one, and folding the two together would
# make the recorded-coverage figure drift with queue depth.
_TERMINAL_DISTILLATION_STATES = (
    memory_store.DISTILLATION_STORED,
    memory_store.DISTILLATION_NO_LESSON,
    memory_store.DISTILLATION_LEGACY_NO_LESSON,
    memory_store.DISTILLATION_CANCELLED,
)

# What a NULL ``result_reason`` is called when the breakdown is rendered for a
# human. It is never written into the structured rows: those keep ``None``, so
# no consumer can mistake the label for a reason the finalizer actually emitted.
_UNRECORDED_REASON_LABEL = "(not recorded)"


def _distillation_reason_metrics(conn) -> dict:
    """Where distillation yield is lost, grouped by terminal state and reason.

    ``distillation_yield`` says how much yield survived; nothing said where the
    rest went. Answering that meant replaying historical interactions through a
    live model, and the replay is what caught the semantic dedup gate being
    dead in production -- a fault that reads off this breakdown immediately as
    zero ``semantic_duplicate`` rows corpus-wide.

    Rows whose reason is NULL stay ``None`` here and are counted in their own
    bucket. They are the overwhelming majority of the live ledger -- 6712 of
    6981 terminal rows, 96.1%, when this was written -- so reporting them as
    zero, or naming them, would turn "we did not record this" into a confident
    wrong answer about which gate fired.
    """
    rows = memory_store.distillation_reason_counts(
        conn, states=_TERMINAL_DISTILLATION_STATES,
    )
    total = sum(int(row["count"] or 0) for row in rows)
    recorded = sum(
        int(row["count"] or 0) for row in rows if row.get("reason") is not None
    )
    return {
        "distillation_reasons": rows,
        "distillation_reason_rows": total,
        "distillation_reason_recorded": recorded,
        "distillation_reason_unrecorded": total - recorded,
        "distillation_reason_recorded_percent": _percent(recorded, total),
        "distillation_reason_note": (
            "A reason names why one interaction's candidate ended where it "
            "did. It can show which gate refuses the most candidates, and a "
            "gate sitting at zero while its peers are non-zero is evidence "
            "that gate never fires. It cannot speak for the rows reported as "
            "not recorded: those predate the result_reason column or came "
            "from a path that writes none (a cancelled row keeps its "
            "explanation in last_error), so they are unknown, not unrefused. "
            "Counts are one row per interaction, not one per attempt, and a "
            "reason describes only the attempt that made the row terminal."
        ),
    }


# Below this many caller-judged outcomes a tier's rate is quoted with its
# sample size doing the arguing. Four rejections out of five reads as 20% and
# means nothing; the reader needs n in the same breath as the percentage.
_TIER_SAMPLE_NOTE = 20


def _reviewed_by_tier_lines(report: dict) -> list[str]:
    """Render the caller-judged split, or nothing when there is no split to show."""
    tiers = report.get("reviewed_by_tier") or []
    if len(tiers) < 2:
        # One tier (or none) is not a comparison, and printing a single row
        # restating the aggregate is noise.
        return []
    lines = ["      by tier (which work is being rejected, not just how much):"]
    for entry in tiers:
        total = int(entry.get("outcomes", 0))
        note = "" if total >= _TIER_SAMPLE_NOTE else "  <- small sample"
        lines.append(
            "        %-16s n=%-5d %s%% positive%s"
            % (entry.get("tier", "?"), total, entry.get("positive_percent", 0), note)
        )
    # Without this the table reads as a capability ranking and a local tier
    # scoring near a cloud tier reads as parity. It is not one: routing is not
    # random, so the tiers are not solving the same work, and the rate measures
    # the whole pipeline -- what got delegated, how it was asked, and the
    # reviewer's bar -- not the model. A tier drawing the harder tasks and
    # matching on rate is doing BETTER, not the same.
    lines.append(
        "        (not a model ranking: routing is not random, so tiers are not "
        "solving comparable work)"
    )
    return lines


def _distillation_reason_lines(report: dict) -> list[str]:
    """Render the breakdown with recorded and unrecorded rows kept apart.

    Listing them together would put ``(not recorded)`` in the same list as
    ``semantic_duplicate``, where it reads as one more rejection reason instead
    of as the absence of an answer.
    """
    rows = report.get("distillation_reasons") or []
    recorded = [row for row in rows if row.get("reason") is not None]
    unrecorded = [row for row in rows if row.get("reason") is None]
    return [
        "  distillation reasons: %s of %s terminal row(s) carry one (%s%%)"
        % (
            report.get("distillation_reason_recorded", 0),
            report.get("distillation_reason_rows", 0),
            report.get("distillation_reason_recorded_percent", 0.0),
        ),
        "    recorded: %s"
        % (
            ", ".join(
                "%s/%s=%s" % (row["state"], row["reason"], row["count"])
                for row in recorded
            )
            or "(none yet)"
        ),
        "    %s: %s"
        % (
            _UNRECORDED_REASON_LABEL,
            ", ".join("%s=%s" % (row["state"], row["count"]) for row in unrecorded)
            or "none",
        ),
        "    %s" % report.get("distillation_reason_note", ""),
    ]


# Below this many caller-judged outcomes the reviewed rate is too noisy to gate
# on, and the blended rate is the only thing left to look at.
_MIN_REVIEWED_SAMPLE = 20


def _gating_positive_percent(report: dict) -> tuple[float, str]:
    """The positive rate the status gate is allowed to believe.

    ``positive_percent`` blends caller-judged outcomes with the runtime marking
    its own curriculum, and the curriculum outnumbers review by more than an
    order of magnitude. Gating on the blend is how a store sat at "watch" while
    reporting 96.1% positive and 52.7% on the 186 outcomes a caller actually
    judged -- a rate that should have read "attention". Once there is a usable
    reviewed sample, that is the number the thresholds apply to.
    """
    reviewed = int(report.get("reviewed_outcomes") or 0)
    if reviewed >= _MIN_REVIEWED_SAMPLE:
        return float(report.get("reviewed_positive_percent") or 0.0), "reviewed"
    return float(report.get("positive_percent") or 0.0), "blended"


def _status(report: dict) -> str:
    quality = report["quality"]
    task_embeddings = report.get("interaction_task_embeddings") or {}
    severe = (
        quality["path_or_secret_like"]
        + quality["missing_fts"]
        + quality["orphan_fts"]
        + quality.get("embedding_model_mismatch", 0)
        + quality.get("embedding_revision_mismatch", 0)
        + quality.get("embedding_dimension_invalid", 0)
        + quality.get("embedding_dimension_mismatch", 0)
        + quality.get("embedding_vector_invalid", 0)
        + int(bool(quality.get("embedding_mixed_dimensions")))
        + int(task_embeddings.get("model_mismatch", 0))
        + int(task_embeddings.get("revision_mismatch", 0))
        + int(task_embeddings.get("dimension_invalid", 0))
        + int(task_embeddings.get("dimension_mismatch", 0))
        + int(task_embeddings.get("vector_invalid", 0))
        + int(len(task_embeddings.get("dimensions") or {}) > 1)
    )
    positive_percent, _basis = _gating_positive_percent(report)
    if severe or (report["outcomes"] and positive_percent < 60.0):
        return "attention"
    hygiene = (
        quality["exact_duplicate_prunable"]
        + quality["no_embedding"]
        + quality["vague_without_anchor"]
        + quality["missing_source_interaction"]
        + quality.get("embedding_legacy", 0)
        + quality.get("embedding_dimension_missing", 0)
        + int(task_embeddings.get("missing", 0))
        + int(task_embeddings.get("legacy_model", 0))
        + int(task_embeddings.get("dimension_missing", 0))
        + int(report.get("ambiguous_legacy_project_turns", 0))
    )
    if hygiene or (
        report["interactions"] >= 20
        and report["outcome_coverage_percent"] < 35.0
    ) or (report["outcomes"] and positive_percent < 80.0) or (
        report.get("quarantined_lessons", 0)
    ):
        return "watch"
    if not report["interactions"] or not report["outcomes"] or not report["lessons"]:
        return "building"
    return "healthy"


def build_report(conn) -> dict:
    """Build one stable JSON-ready learning and memory-health snapshot."""
    interactions = int(conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0])
    lessons = int(conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0])
    facts = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
    sources, grounded_lessons, orphaned_lessons = _lesson_sources(conn)
    outcomes = _outcome_metrics(conn)
    lesson_outcomes = _lesson_outcome_metrics(conn)
    distillation_reasons = _distillation_reason_metrics(conn)
    audit = memory_quality.audit(conn)
    embedding_state = memory_store.embedding_provenance_stats(
        conn,
        embeddings.EMBED_IDENTITY,
        revision=embeddings.EMBED_REVISION,
        dimension=embeddings.EXPECTED_DIMENSION,
    )
    interaction_embedding_state = (
        memory_store.interaction_task_embedding_provenance_stats(
            conn,
            embeddings.EMBED_IDENTITY,
            revision=embeddings.EMBED_REVISION,
            dimension=embeddings.EXPECTED_DIMENSION,
        )
    )
    ambiguous_legacy_project_turns = (
        memory_store.ambiguous_legacy_project_turn_count(conn)
    )
    unscoped_session_turns = memory_store.unscoped_session_turn_count(conn)
    dimensions = embedding_state.get("dimensions") or {}
    quality = {
        "exact_duplicate_groups": int(audit.get("exact_duplicate_groups", 0)),
        "exact_duplicate_prunable": int(audit.get("exact_duplicate_prunable", 0)),
        "no_embedding": int(audit.get("no_embedding", 0)),
        "vague_without_anchor": int(audit.get("vague_without_anchor", 0)),
        "path_or_secret_like": int(audit.get("path_or_secret_like", 0)),
        "missing_source_interaction": int(audit.get("missing_source_interaction", 0)),
        "missing_fts": int(audit.get("missing_fts", 0)),
        "orphan_fts": int(audit.get("orphan_fts", 0)),
        "embedding_percent": _percent(
            int(embedding_state.get("valid", 0)), lessons,
        ),
        "embedding_legacy": int(embedding_state.get("legacy_model", 0)),
        "embedding_model_mismatch": int(embedding_state.get("model_mismatch", 0)),
        "embedding_revision_mismatch": int(
            embedding_state.get("revision_mismatch", 0)
        ),
        "embedding_dimension_missing": int(
            embedding_state.get("dimension_missing", 0)
        ),
        "embedding_dimension_invalid": int(
            embedding_state.get("dimension_invalid", 0)
        ),
        "embedding_dimension_mismatch": int(
            embedding_state.get("dimension_mismatch", 0)
        ),
        "embedding_vector_invalid": int(
            embedding_state.get("vector_invalid", 0)
        ),
        "embedding_mixed_dimensions": len(dimensions) > 1,
        "embedding_dimensions": dimensions,
    }
    good_interactions = outcomes["good_outcome_interactions"]
    report = {
        "interactions": interactions,
        **outcomes,
        **lesson_outcomes,
        "outcome_coverage_percent": _percent(
            outcomes["outcome_interactions"], interactions
        ),
        "lessons": lessons,
        "facts": facts,
        "grounded_lessons": grounded_lessons,
        # Orphans were earned and lost their interaction; counting them as
        # synthetic would credit seeding for work the runtime actually did.
        "synthetic_lessons": lessons - grounded_lessons - orphaned_lessons,
        "orphaned_lessons": orphaned_lessons,
        # A synthetic lesson has never been validated by a real outcome, and
        # neither has any lesson that was never retrieved on a scored task.
        # Both are asserted, not earned, however clean their text looks.
        "unvalidated_lessons": int(audit.get("unvalidated_lessons", 0)),
        "synthetic_unvalidated_lessons": int(
            audit.get("synthetic_unvalidated_lessons", 0)
        ),
        "lesson_sources": sources,
        "lessons_per_interaction": round(lessons / interactions, 3)
        if interactions
        else 0.0,
        "distillation_yield": round(grounded_lessons / good_interactions, 3)
        if good_interactions
        else None,
        # Yield says how much survived; this says where the rest went.
        **distillation_reasons,
        "quality": quality,
        "embedding_model": embeddings.EMBED_IDENTITY,
        "embedding_revision": embeddings.EMBED_REVISION or None,
        "embedding_expected_dimension": embeddings.EXPECTED_DIMENSION,
        "interaction_task_embeddings": interaction_embedding_state,
        "ambiguous_legacy_project_turns": ambiguous_legacy_project_turns,
        "unscoped_session_turns": unscoped_session_turns,
    }
    report["status"] = _status(report)
    return report


def format_report(report: dict) -> str:
    """Render a compact CLI/MCP view of ``build_report``."""
    quality = report.get("quality") or {}
    yield_value = report.get("distillation_yield")
    lines = [
        "sonder learning health",
        "  status: %s" % report.get("status", "unknown"),
        "  interactions: %s | outcome coverage: %s%% (%s grounded)"
        % (
            report.get("interactions", 0),
            report.get("outcome_coverage_percent", 0),
            report.get("outcome_interactions", 0),
        ),
        "  outcomes: %s | positive: %s%% (blended, not an accuracy figure) "
        "| negative: %s"
        % (
            report.get("outcomes", 0),
            report.get("positive_percent", 0),
            report.get("bad_outcomes", 0),
        ),
        "    reviewed (judged by a caller): %s | positive: %s%%"
        % (
            report.get("reviewed_outcomes", 0),
            report.get("reviewed_positive_percent", 0),
        ),
        "    autograded (runtime marking its own curriculum): %s | positive: %s%%"
        % (
            report.get("autograded_outcomes", 0),
            report.get("autograded_positive_percent", 0),
        ),
        *_reviewed_by_tier_lines(report),
        # State the method's limit next to the number it produces. The split is
        # inferred from the SIGNAL NAME -- there is no recorded source -- so a
        # caller who ran the tests and recorded tests_passed lands in the
        # autograded bucket and drops out of the reviewed rate entirely.
        # Measured: 63 autograded outcomes carry a real chat session_id, under
        # 1% of that population, so the reviewed rate is not materially skewed
        # today. The mechanism is unbounded, though, so it is worth watching.
        "    (split inferred from signal name, not a recorded source: "
        "record_outcome callers who use tests_passed/failed/compiled land in "
        "the autograded bucket)",
        "  lessons: %s | interaction-grounded: %s | synthetic: %s | orphaned: %s"
        % (
            report.get("lessons", 0),
            report.get("grounded_lessons", 0),
            report.get("synthetic_lessons", 0),
            report.get("orphaned_lessons", 0),
        ),
        "    never validated by an outcome: %s (synthetic: %s) "
        "-- asserted, not earned"
        % (
            report.get("unvalidated_lessons", 0),
            report.get("synthetic_unvalidated_lessons", 0),
        ),
        "  lesson feedback: evaluated=%s | with losses=%s | loss-only=%s | quarantined=%s"
        % (
            report.get("evaluated_lessons", 0),
            report.get("lessons_with_losses", 0),
            report.get("loss_only_lessons", 0),
            report.get("quarantined_lessons", 0),
        ),
        "    loss-only expected from task difficulty alone: %s "
        "| beyond that reference class: %s | on a single retrieval: %s"
        % (
            report.get("loss_only_expected_from_task_difficulty", 0.0),
            report.get("loss_only_beyond_reference_class", 0),
            report.get("loss_only_single_retrieval_lessons", 0),
        ),
        "    losses come from %s failing task(s), marking %s lesson(s) each "
        "(loss-only: %s task(s); quarantine: %s task(s))"
        % (
            report.get("failed_interactions", 0),
            report.get("lessons_marked_per_failed_interaction", 0.0),
            report.get("loss_only_distinct_failed_interactions", 0),
            report.get("quarantine_distinct_failed_interactions", 0),
        ),
        "    %s" % report.get("loss_attribution_note", ""),
        "  distillation yield: %s grounded lesson(s) per positive interaction"
        % ("n/a" if yield_value is None else yield_value),
        *_distillation_reason_lines(report),
        "  embeddings: %s%% | duplicate rows: %s | vague: %s | privacy flags: %s"
        % (
            quality.get("embedding_percent", 0),
            quality.get("exact_duplicate_prunable", 0),
            quality.get("vague_without_anchor", 0),
            quality.get("path_or_secret_like", 0),
        ),
        "  embedding provenance: model=%s | revision=%s | legacy=%s | "
        "model mismatch=%s | revision mismatch=%s | dimension missing=%s | "
        "dimension invalid=%s | dimension mismatch=%s | invalid vectors=%s | "
        "mixed=%s | target dimension=%s | dimensions=%s"
        % (
            report.get("embedding_model") or "unknown",
            report.get("embedding_revision") or "unversioned",
            quality.get("embedding_legacy", 0),
            quality.get("embedding_model_mismatch", 0),
            quality.get("embedding_revision_mismatch", 0),
            quality.get("embedding_dimension_missing", 0),
            quality.get("embedding_dimension_invalid", 0),
            quality.get("embedding_dimension_mismatch", 0),
            quality.get("embedding_vector_invalid", 0),
            "yes" if quality.get("embedding_mixed_dimensions") else "no",
            report.get("embedding_expected_dimension") or "unknown",
            ", ".join(
                "%s=%s" % item
                for item in sorted((quality.get("embedding_dimensions") or {}).items())
            ) or "none",
        ),
        "  interaction task embeddings: compatible=%s/%s | refresh needed=%s | "
        "missing=%s | legacy=%s | model mismatch=%s | revision mismatch=%s | "
        "dimension missing=%s | dimension invalid=%s | dimension mismatch=%s | "
        "invalid vectors=%s | mixed=%s | dimensions=%s"
        % (
            (report.get("interaction_task_embeddings") or {}).get(
                "compatible", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get(
                "interactions", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get(
                "refresh_required", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get("missing", 0),
            (report.get("interaction_task_embeddings") or {}).get(
                "legacy_model", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get(
                "model_mismatch", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get(
                "revision_mismatch", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get(
                "dimension_missing", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get(
                "dimension_invalid", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get(
                "dimension_mismatch", 0
            ),
            (report.get("interaction_task_embeddings") or {}).get(
                "vector_invalid", 0
            ),
            "yes" if len(
                (report.get("interaction_task_embeddings") or {}).get(
                    "dimensions", {}
                )
            ) > 1 else "no",
            ", ".join(
                "%s=%s" % item for item in sorted(
                    (report.get("interaction_task_embeddings") or {}).get(
                        "dimensions", {}
                    ).items()
                )
            ) or "none",
        ),
        "  project provenance: ambiguous legacy session turns=%s "
        "| sessioned unscoped turns=%s (excluded from project-scoped history)"
        % (
            report.get("ambiguous_legacy_project_turns", 0),
            report.get("unscoped_session_turns", 0),
        ),
    ]
    for item in report.get("quarantined_lesson_details") or []:
        lines.append(
            "    quarantine %s: losses=%s | tasks=%s | avg=%s | retry after=%s"
            % (
                item.get("lesson_id", "unknown"),
                item.get("losses_since_win", 0),
                item.get("distinct_loss_tasks_since_win", 0),
                item.get("avg_reward_since_win"),
                item.get("retry_after") or "manual review",
            )
        )
    if report.get("quarantined_lessons"):
        lines.append("    %s" % report.get("quarantine_review", ""))
    sources = report.get("lesson_sources") or {}
    lines.append(
        "  lesson sources: %s"
        % (
            ", ".join("%s=%s" % item for item in sorted(sources.items()))
            if sources
            else "(none yet)"
        )
    )
    signals = report.get("signals") or []
    lines.append(
        "  signals: %s"
        % (
            ", ".join(
                "%s=%s" % (item.get("signal"), item.get("count", 0))
                for item in signals
            )
            if signals
            else "(none yet)"
        )
    )
    return "\n".join(lines)
