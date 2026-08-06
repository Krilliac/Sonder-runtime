"""Hybrid lexical+semantic retrieval over distilled lessons. RRF fusion."""
from datetime import datetime, timedelta, timezone
import hashlib
import os
import re

import embeddings
import mmr_rerank
import memory_store

# Recalibrated 2026-07-06 against the 557-lesson corpus via tune_min_sim.py
# (nomic-embed-text). Over 22 natural-language coding intents vs 15 off-domain
# noise probes, top-1 cosine separated cleanly: positives min 0.612 / median
# 0.728; negatives max 0.611. 0.62 is the lowest zero-noise threshold — recall
# 0.95, noise 0.00 (best Youden's J). The old 0.65, tuned on the tiny
# game-ladder corpus, dropped genuine 0.60-0.65 hits (e.g. the sql-injection
# lesson at 0.650) with no precision gain. Re-run tune_min_sim.py after large
# corpus changes.
DEFAULT_MIN_SIM = 0.62
# Borderline semantic-only hits need a second signal before prompt injection.
# The live 979-lesson corpus produced a reproduced cross-domain false positive
# at 0.650211.  Keep the calibrated 0.62 gate for candidates with at least two
# content-word anchors, while requiring uncorroborated semantic transfer to
# clear 0.70 (still below the prior positive median of 0.728).
DEFAULT_UNCORROBORATED_MIN_SIM = 0.70
MIN_LEXICAL_ANCHORS = 2

# --- Quarantine evidence gates -------------------------------------------
#
# Raw floor (unchanged, retained as a conservative sample-size guard).
QUARANTINE_MIN_LOSSES = 5
QUARANTINE_REPEAT_TASK_MIN_LOSSES = 6
QUARANTINE_MIN_DISTINCT_TASKS = 2
QUARANTINE_MAX_AVG_REWARD = -0.5
QUARANTINE_COOLDOWN_HOURS = 24 * 7

# A failing task writes its reward onto EVERY lesson retrieved for it
# (memory_store.record_lesson_usage_outcome updates by interaction_id, not by
# lesson), so N loss rows are not N independent failures. Measured 2026-08-06
# on the live store: 493 loss rows trace to only 144 distinct failing
# interactions -- a mean cohort of 3.424 lessons blamed per failure, with 55 of
# those 144 interactions blaming five lessons at once. Blame is therefore split
# evenly across the cohort (the symmetric, no-extra-information attribution):
# one lesson's share of one failure is 1/cohort_size, and the quarantine gate
# counts those shares rather than rows.
#
# 2.0 shares means "two failures this lesson is individually answerable for":
# two solo failures, or ~7 failures at the measured mean cohort. It is the
# floor that binds for frequently-retrieved lessons, where the band test below
# would otherwise fire on a single loss. It is reachable -- 18 of the 144
# failing interactions blamed exactly one lesson, and one lesson has 5 solo
# lifetime failures -- but no lesson in the live corpus currently reaches 2.0
# within its since-last-win epoch (the maximum is 1.67).
QUARANTINE_MIN_ATTRIBUTABLE_LOSSES = 2.0

# Loss rate is dominated by retrieval frequency, not lesson quality. Retrieval
# is similarity-driven, so a rarely-retrieved lesson is by construction the one
# pulled in for an unusual task, and unusual tasks fail. Measured 2026-08-06
# over 9256 scored retrievals across 346 lessons (corpus-wide rate 5.33%):
#
#   scored retrievals   lessons   retrievals   losses   loss rate
#   1                        81           81       41      50.62%
#   2-4                      54          149       62      41.61%
#   5-9                      98          562       77      13.70%
#   10-24                    48          698       53       7.59%
#   25-49                    22          788       72       9.14%
#   50-99                    25         1722      123       7.14%
#   100+                     18         5256       65       1.24%
#
# Judging every lesson against the 5.33% corpus rate therefore quarantines
# lessons for being unusual. Each lesson is instead judged against its own
# band. Re-measure after large corpus changes.
QUARANTINE_FREQUENCY_BANDS = (
    (1, 0.5062),
    (4, 0.4161),
    (9, 0.1370),
    (24, 0.0759),
    (49, 0.0914),
    (99, 0.0714),
    (None, 0.0124),
)
# losses_since_win is a run of consecutive non-wins, so the band base rate is
# the per-retrieval probability of each link in that run. Quarantine requires
# the run to be improbable for the lesson's OWN band at p <= 0.01, which is
# what "a 5-loss lesson retrieved 5 times is unremarkable, a 5-loss lesson
# retrieved 500 times is not" actually cashes out to. Required attributable
# losses by band: 6.76 (scored 1), 5.25 (2-4), 2.32 (5-9), then below the 2.0
# floor for every band from 10 retrievals up.
QUARANTINE_BAND_ALPHA = 0.01

# Quarantine excludes a lesson from retrieval, so the win that would clear it
# is unreachable and only the cooldown timer can lift it -- one live
# quarantined lesson has 18 lifetime wins and no way to earn a 19th. After a
# day, a quarantined lesson is admitted on a deterministic ~1-in-20 sample of
# tasks so it can earn its way out on production traffic. Deterministic in
# (lesson, task) so a repeated task always makes the same call. 5% bounds the
# exposure to a genuinely harmful lesson far below the status quo, where the
# timer restores it to 100% of traffic after a week regardless of evidence.
QUARANTINE_PROBATION_AFTER_HOURS = 24
QUARANTINE_PROBATION_ONE_IN = 20

# Confidence in a lesson's historical reward comes from SCORED outcomes, not
# from having been retrieved. Measured 2026-08-06: 715 of 1061 lessons have
# never been retrieved on a scored task, 39 lessons have retrievals but zero
# scored outcomes, and 63 lessons had their _usage_boost confidence weight
# inflated by unscored retrievals. Shrinking toward the neutral prior with
# pseudo-count 10 (the old saturation point) keeps a single lucky outcome from
# earning the same tiebreak as a hundred graded ones.
USAGE_BOOST_PRIOR_SCORED_USES = 10

_LEXICAL_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "before", "by", "do",
    "build", "code", "compact", "create", "design", "designer",
    "deterministic", "example", "for", "from", "generate", "how", "in",
    "implement", "implementation", "into", "is", "it", "of", "on", "or",
    "review", "run", "should", "task", "test", "testing", "tests", "that",
    "the", "this", "to", "use", "using", "with", "without", "write",
}


def rrf(rank_lists, k=60):
    scores = {}
    for lst in rank_lists:
        for rank, item in enumerate(lst):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda i: -scores[i])


def rrf_scores(rank_lists, k=60):
    scores = {}
    for lst in rank_lists:
        for rank, item in enumerate(lst):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _content_terms(text):
    return {
        term
        for term in re.findall(r"[^\W_]+", str(text or "").casefold())
        if len(term) > 2 and term not in _LEXICAL_STOP_WORDS
    }


def _has_lexical_corroboration(task_terms, lesson_text):
    anchors = task_terms.intersection(_content_terms(lesson_text))
    return len(anchors) >= MIN_LEXICAL_ANCHORS


def _candidate_min_sim(task_terms, lesson_text, min_sim):
    if _has_lexical_corroboration(task_terms, lesson_text):
        return min_sim
    return max(min_sim, DEFAULT_UNCORROBORATED_MIN_SIM)


def _stored_vector(row, qv, embedding_model=None, embedding_revision=None):
    def value(key):
        return row.get(key) if hasattr(row, "get") else row[key]

    emb = value("embedding")
    if not emb:
        return None
    try:
        vector = embeddings.from_blob(emb)
    except (TypeError, ValueError, EOFError):
        return None
    if not embeddings.valid_vector(vector) or len(vector) != len(qv):
        return None
    stored_dimension = value("embedding_dim")
    if (
        isinstance(stored_dimension, bool)
        or not isinstance(stored_dimension, int)
        or stored_dimension <= 0
        or stored_dimension != len(vector)
        or stored_dimension != len(qv)
    ):
        return None
    stored_model = value("embedding_model")
    stored_revision = value("embedding_revision")
    if embedding_model and stored_model != embedding_model:
        return None
    if (
        embedding_revision is not None
        and (stored_revision or None) != (embedding_revision or None)
    ):
        return None
    return vector


def _semantic_rank_with_compatibility(
    conn, qv, limit=10, exclude_ids=None,
    embedding_model=None, embedding_revision=None,
):
    """Return ranked IDs plus whether any usable corpus vector matched ``qv``.

    Embedding models can change dimensions across runtime-policy or bundle
    updates. A stored vector from a different model is not semantic evidence:
    skip it rather than assigning an artificial cosine of zero. The boolean
    lets callers distinguish "compatible corpus, but no relevant hit" from
    "no compatible semantic corpus", where lexical fallback is appropriate.
    """
    excluded = set(exclude_ids or ())
    if not embeddings.valid_vector(qv):
        return [], False
    scored = []
    compatible = False
    for les in memory_store.all_lessons(conn):
        if les["id"] in excluded:
            continue
        v = _stored_vector(
            les, qv,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
        )
        if v is None:
            continue
        compatible = True
        scored.append((embeddings.cosine(qv, v), les["id"]))
    scored.sort(reverse=True)
    return [lid for _, lid in scored[:limit]], compatible


def _semantic_rank(
    conn, qv, limit=10, exclude_ids=None,
    embedding_model=None, embedding_revision=None,
):
    ranked, _compatible = _semantic_rank_with_compatibility(
        conn, qv, limit=limit, exclude_ids=exclude_ids,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
    )
    return ranked


def semantic_search(conn, task, embed_fn=None, limit=10):
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    qv = embed_fn(task)
    if qv is not None and not embeddings.valid_vector(qv):
        qv = None
    if qv is None:
        return []
    quarantined = quarantined_lessons(usage_stats_with_attribution(conn), task)
    query_provenance = embeddings.provenance(qv)
    model = query_provenance.get("model") if (
        runtime_default or embed_fn is embeddings.embed
    ) else None
    revision = query_provenance.get("revision") if (
        runtime_default or embed_fn is embeddings.embed
    ) else None
    return _semantic_rank(
        conn, qv, limit=limit, exclude_ids=quarantined,
        embedding_model=model, embedding_revision=revision,
    )


def _lesson_data(conn, ids):
    """Load candidate text and embeddings in bounded batches, preserving IDs."""
    unique_ids = list(dict.fromkeys(ids))
    rows = {}
    for offset in range(0, len(unique_ids), 900):
        batch = unique_ids[offset:offset + 900]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            "SELECT id, text, embedding, embedding_model, embedding_revision, "
            "embedding_dim FROM lessons WHERE id IN (%s)"
            % placeholders,
            tuple(batch),
        ).fetchall():
            rows[row["id"]] = row
    return rows


def _relevant_ids(
    conn, task, qv, ids, min_sim, lesson_data=None,
    embedding_model=None, embedding_revision=None,
):
    """Filter fused candidates through semantic and lexical relevance gates.

    Lessons with no stored embedding are dropped (relevance can't be judged).
    A candidate with fewer than two content-word anchors must clear the stronger
    semantic-only gate.  This rejects acronym and embedding collisions without
    disabling genuinely high-confidence semantic transfer.
    """
    data = lesson_data if lesson_data is not None else _lesson_data(conn, ids)
    task_terms = _content_terms(task)
    kept = []
    vectors = {}
    for lid in ids:
        row = data.get(lid)
        if row is None:
            continue
        v = _stored_vector(
            dict(row), qv,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
        )
        if v is None:
            continue
        required_sim = _candidate_min_sim(task_terms, row["text"], min_sim)
        if embeddings.cosine(qv, v) >= required_sim:
            kept.append(lid)
            vectors[lid] = v
    return kept, vectors


def retrieve(conn, task, k=5, embed_fn=None, min_sim=None):
    rows = retrieve_with_ids(conn, task, k=k, embed_fn=embed_fn, min_sim=min_sim)
    return [r["text"] for r in rows]


def band_loss_rate(scored_retrievals):
    """Base loss rate for the reference class a lesson's retrieval count puts it in."""
    count = max(0, int(scored_retrievals or 0))
    for upper, rate in QUARANTINE_FREQUENCY_BANDS:
        if upper is None or count <= upper:
            return rate
    return QUARANTINE_FREQUENCY_BANDS[-1][1]


def attributable_losses(conn):
    """Per-lesson blame-adjusted loss count for the epoch since its last win.

    A failing task's reward lands on every lesson retrieved for it, so the raw
    loss count double-counts one failure across its whole cohort. Each loss row
    is worth 1/(lessons blamed for that interaction); summing the shares gives
    the number of failures a lesson is individually answerable for.

    Epoch boundaries mirror memory_store.lesson_usage_stats: a positive reward
    starts a fresh epoch. The per-interaction cohort sizes that make the
    discount possible are not exposed by that API, which is why the walk is
    repeated here rather than read off the stats rows.
    """
    cohort = {
        row["interaction_id"]: int(row["blamed"])
        for row in conn.execute(
            "SELECT interaction_id, COUNT(DISTINCT lesson_id) AS blamed "
            "FROM lesson_usage WHERE reward IS NOT NULL AND reward < 0 "
            "AND interaction_id IS NOT NULL GROUP BY interaction_id"
        ).fetchall()
    }
    shares = {}
    current_id = None
    total = 0.0
    for row in conn.execute(
        "SELECT lesson_id, interaction_id, reward, "
        "COALESCE(outcome_ts, ts) AS evidence_ts "
        "FROM lesson_usage WHERE reward IS NOT NULL "
        "ORDER BY lesson_id, datetime(evidence_ts), rowid"
    ).fetchall():
        if row["lesson_id"] != current_id:
            if current_id is not None:
                shares[current_id] = total
            current_id = row["lesson_id"]
            total = 0.0
        value = float(row["reward"])
        if value > 0:
            total = 0.0
        elif value < 0:
            blamed = cohort.get(row["interaction_id"], 1) or 1
            total += 1.0 / blamed
    if current_id is not None:
        shares[current_id] = total
    return shares


def usage_stats_with_attribution(conn):
    """lesson_usage_stats enriched with the blame-adjusted loss evidence."""
    stats = memory_store.lesson_usage_stats(conn)
    for lesson_id, share in attributable_losses(conn).items():
        if lesson_id in stats:
            stats[lesson_id]["attributable_losses_since_win"] = share
    return stats


def _attribution(stats):
    """Attributable losses for this epoch, and whether blame could be split.

    A stats row alone does not say which lessons shared each failure, so a
    caller that has not enriched it gets the undiscounted count. That is a
    deliberate upper bound: the alternative -- assuming the corpus mean cohort
    of 3.424 -- would divide the evidence of a lesson that fails *alone* by
    3.4, and a lesson failing alone is both the most identifiable and the most
    likely to be genuinely harmful. Over-reporting on a monitoring surface is
    recoverable; silently discounting the one unambiguous signal is not.
    Callers holding a connection should use usage_stats_with_attribution.
    """
    measured = stats.get("attributable_losses_since_win")
    if measured is not None:
        return float(measured), "measured"
    return float(int(stats.get("losses_since_win") or 0)), "unattributed"


def lesson_quarantine(stats, now=None):
    """Return the active quarantine decision and its auditable evidence.

    Evidence is scoped to the epoch since the last grounded success so a lesson
    can recover and can also relapse, and three gates must all agree:

    * a raw floor -- five losses spanning two task formulations, or six
      identical-task failures, with a mean reward at or below -0.5;
    * blame-adjusted volume -- at least ``QUARANTINE_MIN_ATTRIBUTABLE_LOSSES``
      failures this lesson is individually answerable for, so a cluster of
      lessons that always fail together cannot each claim the same failures as
      independent evidence;
    * reference class -- the run of losses must be improbable for the base loss
      rate of the lesson's own retrieval-frequency band, so a rarely-retrieved
      lesson is not punished for the unusual tasks that retrieval sends it.

    After ``QUARANTINE_PROBATION_AFTER_HOURS`` the lesson becomes eligible for
    sampled probation retrieval, giving it a production path to a win; the
    cooldown then lifts quarantine entirely.
    """
    if not stats:
        return {"active": False}
    losses = int(stats.get("losses_since_win") or 0)
    distinct_tasks = int(stats.get("distinct_loss_tasks_since_win") or 0)
    avg = stats.get("avg_reward_since_win")
    attributable, attribution_source = _attribution(stats)
    scored = int(stats.get("wins") or 0) + int(stats.get("losses") or 0)
    base_rate = band_loss_rate(scored)
    enough_evidence = losses >= QUARANTINE_MIN_LOSSES and (
        distinct_tasks >= QUARANTINE_MIN_DISTINCT_TASKS
        or losses >= QUARANTINE_REPEAT_TASK_MIN_LOSSES
    )
    # Blame shares are an accumulated sum of 1/cohort, so a lesson that is
    # exactly at the threshold can land just under it: six failures at a cohort
    # of three sum to 1.9999999999999998, not 2.0. Compare with a tolerance so
    # the gate turns on the evidence rather than on binary representation.
    attributable_met = (
        attributable >= QUARANTINE_MIN_ATTRIBUTABLE_LOSSES - 1e-9
    )
    run_probability = base_rate ** attributable if attributable > 0 else 1.0
    band_anomalous = run_probability <= QUARANTINE_BAND_ALPHA
    threshold_met = bool(
        enough_evidence
        and attributable_met
        and band_anomalous
        and avg is not None
        and float(avg) <= QUARANTINE_MAX_AVG_REWARD
    )
    retry_after = None
    probation_eligible = False
    last_failure = stats.get("last_failure_ts")
    if threshold_met and last_failure:
        try:
            failed_at = datetime.fromisoformat(str(last_failure).replace("Z", "+00:00"))
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=timezone.utc)
            retry_at = failed_at + timedelta(hours=QUARANTINE_COOLDOWN_HOURS)
            retry_after = retry_at.isoformat()
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            threshold_met = current < retry_at
            probation_eligible = threshold_met and current >= (
                failed_at + timedelta(hours=QUARANTINE_PROBATION_AFTER_HOURS)
            )
        except (TypeError, ValueError):
            # Malformed evidence timestamps fail safe and stay visible in health.
            retry_after = None
    return {
        "active": threshold_met,
        "losses_since_win": losses,
        "attributable_losses_since_win": attributable,
        "attribution_source": attribution_source,
        "distinct_loss_tasks_since_win": distinct_tasks,
        "scored_retrievals": scored,
        "band_loss_rate": base_rate,
        "loss_run_probability": run_probability,
        "avg_reward_since_win": avg,
        "last_failure_ts": last_failure,
        "retry_after": retry_after,
        "probation_eligible": probation_eligible,
    }


def _lesson_quarantined(stats, now=None):
    return bool(lesson_quarantine(stats, now=now).get("active"))


def _probation_admits(lesson_id, task):
    """Deterministically admit a probationary lesson on ~1 task in N.

    Keyed on (lesson, task) so a repeated task always makes the same call --
    retrieval stays reproducible, while a quarantined lesson still meets enough
    distinct tasks over the cooldown week to earn a grounded win.
    """
    if QUARANTINE_PROBATION_ONE_IN <= 1:
        return True
    digest = hashlib.blake2b(
        ("%s\x00%s" % (lesson_id, task)).encode("utf-8", "replace"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") % QUARANTINE_PROBATION_ONE_IN == 0


def quarantined_lessons(usage_stats, task, now=None):
    """Lesson IDs retrieval must skip for ``task``, after probation sampling."""
    excluded = set()
    for lesson_id, stats in usage_stats.items():
        decision = lesson_quarantine(stats, now=now)
        if not decision.get("active"):
            continue
        if decision.get("probation_eligible") and _probation_admits(lesson_id, task):
            continue
        excluded.add(lesson_id)
    return excluded


def _usage_boost(stats):
    if not stats:
        return 0.0
    avg = stats.get("avg_reward")
    if avg is None:
        return 0.0
    # Confidence is earned by graded outcomes, not by being retrieved: `uses`
    # counts unscored retrievals too, which handed 63 lessons more tiebreak
    # weight than their evidence supports. Shrink toward the neutral prior so a
    # synthetic lesson with no scored history stays at 0.0, and an earned one
    # approaches the cap only as its graded evidence accumulates.
    scored = int(stats.get("wins") or 0) + int(stats.get("losses") or 0)
    if scored <= 0:
        return 0.0
    confidence = scored / float(scored + USAGE_BOOST_PRIOR_SCORED_USES)
    # Keep historical outcome as a gentle tiebreaker, not a relevance override.
    return max(-0.01, min(0.01, float(avg) * confidence * 0.01))


def _mmr_lambda():
    """Relevance/diversity tradeoff for final lesson selection (see
    mmr_rerank). 0.5 balances relevance against redundancy — the first pick
    is always the most relevant lesson, and later picks trade relevance for
    novelty; SONDER_MMR_LAMBDA=1 disables diversification entirely."""
    try:
        value = float(os.environ.get("SONDER_MMR_LAMBDA", "0.5"))
    except ValueError:
        return 0.5
    return max(0.0, min(1.0, value))


def retrieve_with_ids(
    conn, task, k=5, embed_fn=None, min_sim=None,
    embedding_model=None, embedding_revision=None,
):
    if min_sim is None:
        min_sim = float(os.environ.get("SONDER_MIN_SIM", str(DEFAULT_MIN_SIM)))

    usage_stats = usage_stats_with_attribution(conn)
    quarantined = quarantined_lessons(usage_stats, task)
    candidate_limit = max(10, int(k) * 4)
    # Over-fetch enough lexical hits that filtering cannot starve valid matches
    # behind quarantined results ranked ahead of them by FTS.
    lexical_limit = candidate_limit + len(quarantined)
    lexical = [
        lesson_id
        for lesson_id in memory_store.fts_search(
            conn, task, limit=lexical_limit,
        )
        if lesson_id not in quarantined
    ]
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    qv = embed_fn(task)
    query_provenance = embeddings.provenance(qv) if qv is not None else {}
    if embedding_model is None and (runtime_default or embed_fn is embeddings.embed):
        embedding_model = query_provenance.get("model")
    if embedding_revision is None and (runtime_default or embed_fn is embeddings.embed):
        embedding_revision = query_provenance.get("revision")

    semantic = []
    compatible_semantic_corpus = False
    if qv is not None:
        semantic, compatible_semantic_corpus = _semantic_rank_with_compatibility(
            conn, qv, limit=candidate_limit, exclude_ids=quarantined,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
        )

    if qv is None or not compatible_semantic_corpus:
        # Embeddings unavailable, or all non-quarantined stored vectors belong
        # to an incompatible embedding space: soft-fail to lexical-only. With
        # no comparable vectors there is no semantic threshold to apply, so
        # require two content-word anchors instead of trusting one ambiguous
        # acronym or generic token.
        task_terms = _content_terms(task)
        data = _lesson_data(conn, lexical)
        lexical = [
            lid for lid in lexical
            if data.get(lid) is not None
            and _has_lexical_corroboration(task_terms, data[lid]["text"])
        ]
        scores = rrf_scores([lexical, []])
        fused = sorted(
            scores,
            key=lambda lid: -(scores[lid] + _usage_boost(usage_stats.get(lid))),
        )[:k]
        rows = []
        for lid in fused:
            row = data.get(lid)
            text = row["text"] if row else None
            if text:
                rows.append({"id": lid, "text": text, "score": scores[lid]})
        return rows

    scores = rrf_scores([lexical, semantic])
    fused = sorted(
        scores,
        key=lambda lid: -(scores[lid] + _usage_boost(usage_stats.get(lid))),
    )
    data = _lesson_data(conn, fused)
    relevant, relevant_vectors = _relevant_ids(
        conn, task, qv, fused, min_sim, lesson_data=data,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
    )
    # MMR-select the final k: with 1000+ lessons, plain fused-order
    # truncation stacks near-duplicate restatements of the strongest lesson
    # and crowds out genuinely different ones. lambda=1 restores pure
    # relevance order (the pre-MMR behavior).
    relevant = mmr_rerank.mmr_rerank(
        qv,
        [(lid, relevant_vectors[lid]) for lid in relevant],
        k=k,
        lambda_mult=_mmr_lambda(),
    )
    rows = []
    for lid in relevant:
        row = data.get(lid)
        text = row["text"] if row else None
        if text:
            rows.append({"id": lid, "text": text, "score": scores.get(lid, 0.0)})
    return rows
