"""Semantic recall implementation over the migrated memory adapter."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
import math

import sonder_runtime.adapters.embeddings as embeddings
import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.application.ports.recall import validate_recall_request
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.memory import rules as _rules
from sonder_runtime.application.memory.hybrid_retrieval import (
    HybridMemoryRetriever, MemoryCandidate, RetrievalQuery,
)
from sonder_runtime.application.memory.memory_policy import (
    MemoryClass, PrivacyClass,
)
from sonder_runtime.application.memory.memory_policy import TemporalTruth


DEFAULT_MIN_SIM = _rules.DEFAULT_RECALL_MIN_SIM
MAX_RESP_CHARS = 400


@dataclass(frozen=True)
class RecallItem:
    """One recalled memory with bounded, inspectable evidence.

    ``text`` intentionally remains the exact compatibility rendering returned
    by :func:`recall`.  The additional fields let a UI or an agent explain why
    a result was selected without exposing raw storage rows or changing the
    legacy list API.
    """

    interaction_id: str
    text: str
    score: float
    score_components: dict[str, float]
    provenance: tuple[str, ...]
    freshness: float | None
    confidence: float | None
    evidence: tuple[str, ...]
    degradation_reasons: tuple[str, ...] = ()

    @property
    def memory_id(self) -> str:
        """Stable memory identity used by application and UI callers."""
        return self.interaction_id


@dataclass(frozen=True)
class RecallPage:
    results: tuple[str, ...]
    incomplete: bool
    next_cursor: str | None
    termination: str
    candidates_examined: int
    candidates_scored: int
    items: tuple[RecallItem, ...] = ()
    degradation_reasons: tuple[str, ...] = ()


_FRESHNESS_HALF_LIFE_SECONDS = 180.0 * 24.0 * 60.0 * 60.0


def _freshness_score(timestamp, *, now=None):
    """Return a clamped time-decay score, or ``None`` for legacy timestamps."""
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    value = timestamp.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        observed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    else:
        observed = observed.astimezone(timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)
    age_seconds = max(0.0, (reference - observed).total_seconds())
    try:
        score = math.exp(-math.log(2.0) * age_seconds / _FRESHNESS_HALF_LIFE_SECONDS)
    except (OverflowError, ValueError):
        return None
    return min(1.0, max(0.0, score))


def _bounded_confidence(reward):
    if isinstance(reward, bool):
        return None
    try:
        value = float(reward)
    except (TypeError, ValueError, OverflowError):
        return None
    return min(1.0, max(0.0, value)) if math.isfinite(value) else None


def _recall_item(row, text, similarity):
    interaction_id = row.get("id")
    outcome_signal = row.get("outcome_signal")
    outcome_source = row.get("outcome_source")
    model = row.get("task_embedding_model")
    revision = row.get("task_embedding_revision")
    provenance = []
    if isinstance(interaction_id, str) and interaction_id:
        provenance.append("interaction:%s" % interaction_id)
    if isinstance(outcome_signal, str) and isinstance(outcome_source, str):
        provenance.append("outcome:%s:%s" % (outcome_signal, outcome_source))
    if isinstance(model, str) and model and isinstance(revision, str) and revision:
        provenance.append("embedding:%s@%s" % (model, revision))
    evidence = (
        (outcome_signal, outcome_source)
        if isinstance(outcome_signal, str) and isinstance(outcome_source, str)
        else ()
    )
    freshness = _freshness_score(row.get("ts"))
    confidence = _bounded_confidence(row.get("outcome_reward"))
    degradations = []
    if freshness is None:
        degradations.append("freshness_unavailable")
    if not evidence:
        degradations.append("outcome_provenance_unavailable")
    return RecallItem(
        interaction_id=interaction_id if isinstance(interaction_id, str) else "",
        text=text,
        score=float(similarity),
        score_components={"semantic": float(similarity)},
        provenance=tuple(provenance),
        freshness=freshness,
        confidence=confidence,
        evidence=evidence,
        degradation_reasons=tuple(degradations),
    )


def _fence_count(text):
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("```"))


def _format(task, response, max_len=MAX_RESP_CHARS):
    resp = response or ""
    truncated = len(resp) > max_len
    if truncated:
        resp = resp[:max_len].rstrip() + " \u2026"
    line = "%s -> %s" % (task, resp)
    if truncated and _fence_count(line) % 2:
        line += "\n```"
    return line


_RECALL_MODES = frozenset(("hybrid", "exact", "temporal"))


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hybrid_order(task, scored, *, project, include_all_projects, limit,
                  mode="hybrid", at=None):
    """Apply the typed hybrid ranking to already scope-filtered rows.

    SQLite remains responsible for project/session/outcome eligibility. This
    adapter adds the explainable deterministic ordering without widening that
    privacy boundary or re-reading unscoped rows.
    """
    # The legacy recall port permits an empty query when the caller supplied
    # its vector. Keep that established ordering rather than making the new
    # lexical reranker reject a request that already passed port validation.
    if not task.strip():
        return scored[:limit]
    candidates = []
    rows_by_id = {}
    now = at or datetime.now(timezone.utc)
    query_project = None if include_all_projects else project
    scope = "project" if query_project is not None else "global"
    for similarity, rank, row in scored:
        interaction_id = row.get("id")
        task_text = row.get("task")
        if not isinstance(interaction_id, str) or not interaction_id:
            continue
        if not isinstance(task_text, str) or not task_text:
            continue
        created = _parse_timestamp(row.get("ts"))
        if created is None:
            # A malformed stored timestamp must not gain a fabricated
            # recency advantage over well-formed recall candidates. Preserve
            # legacy ordering-key rows as recallable evidence.
            created = datetime(1970, 1, 1, tzinfo=timezone.utc)
        outcome = row.get("outcome_signal")
        source = row.get("outcome_source")
        provenance = tuple(
            value for value in (
                f"interaction:{interaction_id}",
                f"outcome:{outcome}:{source}" if isinstance(outcome, str) and isinstance(source, str) else "",
            ) if value
        )
        memory_project = row.get("project")
        candidates.append(MemoryCandidate(
            memory_id=interaction_id,
            text=task_text,
            memory_class=(
                MemoryClass.FAILURE if outcome == "failed"
                else MemoryClass.PROJECT if memory_project else MemoryClass.SEMANTIC
            ),
            created_at=created,
            confidence=_bounded_confidence(row.get("outcome_reward")) or 0.0,
            project=memory_project if not include_all_projects else None,
            privacy=PrivacyClass.PROJECT if memory_project else PrivacyClass.PUBLIC,
            semantic_score=float(similarity),
            provenance=provenance,
            temporal=(
                TemporalTruth(valid_from=created, confidence=1.0,
                              last_revalidated_at=created)
                if _parse_timestamp(row.get("ts")) is not None else None
            ),
        ))
        rows_by_id[interaction_id] = (similarity, rank, row)
    ranked = HybridMemoryRetriever().retrieve(
        candidates,
        RetrievalQuery(
            task, mode=mode, limit=limit, scope=scope, project=query_project,
            at=now,
        ),
    )
    return [rows_by_id[item.candidate.memory_id] for item in ranked]


def recall_page(conn, task, k=2, embed_fn=None, min_sim=None,
                qv=None, exclude_session=None, project=None,
                include_all_projects=False, embedding_model=None,
                embedding_revision=None, candidate_cursor=None,
                mode="hybrid", at=None):
    """Return bounded recall results with truthful enumeration evidence."""
    include_all_projects = include_all_projects is True
    if min_sim is None:
        try:
            min_sim = float(
                os.environ.get("SONDER_RECALL_MIN_SIM", str(DEFAULT_MIN_SIM))
            )
        except (TypeError, ValueError) as exc:
            raise InvalidInput("recall similarity threshold is invalid") from exc
    validate_recall_request(task, k, min_sim)
    if not isinstance(mode, str) or mode not in _RECALL_MODES:
        raise InvalidInput(
            "recall mode is unavailable; supported modes are hybrid, exact, "
            "and temporal"
        )
    if mode != "hybrid" and (not isinstance(task, str) or not task.strip()):
        raise InvalidInput("specialized recall modes require a non-empty query")
    if at is not None:
        at = _parse_timestamp(at)
        if at is None:
            raise InvalidInput("temporal recall point must be an ISO timestamp")
    elif mode == "temporal":
        at = datetime.now(timezone.utc)
    specialized = mode != "hybrid"
    lexical_fallback = False
    runtime_default = embed_fn is None
    query_provenance = {}
    if not specialized:
        embed_fn = embed_fn or embeddings.embed
        if qv is None:
            qv = embed_fn(task)
        if qv is None or not embeddings.valid_vector(qv):
            lexical_fallback = True
        else:
            query_provenance = embeddings.trusted_provenance(qv, embed_fn, runtime_default)
            if embedding_model is None:
                embedding_model = query_provenance.get("model")
            if embedding_revision is None:
                embedding_revision = query_provenance.get("revision")

    candidates = memory_store.good_interaction_candidate_page(
        conn,
        exclude_session,
        project=project,
        include_all_projects=include_all_projects,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
        require_embedding=not (specialized or lexical_fallback),
        max_created_at=(at.isoformat() if mode == "temporal" else None),
        embedding_dim=(len(qv) if qv is not None else None),
        cursor=candidate_cursor,
    )
    scored = []
    scored_count = 0
    for candidate_rank, row in enumerate(candidates.rows):
        if not isinstance(row.get("task"), str):
            continue
        response = row.get("response")
        if response is not None and not isinstance(response, str):
            continue
        if specialized or lexical_fallback:
            scored_count += 1
            scored.append((0.0, candidate_rank, row))
            continue
        emb = row.get("task_embedding")
        if not emb:
            continue
        try:
            stored = embeddings.from_blob(emb)
        except (TypeError, ValueError, EOFError):
            continue
        if not embeddings.valid_vector(stored) or len(stored) != len(qv):
            continue
        stored_dimension = row.get("task_embedding_dim")
        if (
            isinstance(stored_dimension, bool)
            or not isinstance(stored_dimension, int)
            or stored_dimension <= 0
            or stored_dimension != len(stored)
            or stored_dimension != len(qv)
        ):
            continue
        stored_model = row.get("task_embedding_model")
        stored_revision = row.get("task_embedding_revision")
        if embedding_model and stored_model != embedding_model:
            continue
        if (
            embedding_revision is not None
            and (stored_revision or None) != (embedding_revision or None)
        ):
            continue
        sim = embeddings.cosine(qv, stored)
        scored_count += 1
        if _rules.passes_similarity(sim, min_sim):
            scored.append((sim, candidate_rank, row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ordered = _hybrid_order(
        task, scored, project=project,
        include_all_projects=include_all_projects, limit=k, mode=mode,
        at=at,
    )
    selected = ordered[:k]
    formatted = tuple(
        _format(row["task"], row["response"])
        for _, _, row in selected
    )
    items = tuple(
        _recall_item(row, text, similarity)
        for (similarity, _, row), text in zip(selected, formatted)
    )
    degradation_reasons = []
    if lexical_fallback:
        degradation_reasons.append("embedding_unavailable_lexical_fallback")
    if candidates.incomplete:
        degradation_reasons.append(
            "candidate_enumeration_%s" % candidates.termination
        )
    degradation_reasons.extend(
        reason for item in items for reason in item.degradation_reasons
        if reason not in degradation_reasons
    )
    return RecallPage(
        results=formatted,
        incomplete=candidates.incomplete,
        next_cursor=candidates.next_cursor,
        termination=candidates.termination,
        candidates_examined=candidates.rows_examined,
        candidates_scored=scored_count,
        items=items,
        degradation_reasons=tuple(degradation_reasons),
    )


def recall(conn, task, k=2, embed_fn=None, min_sim=None,
           qv=None, exclude_session=None, project=None,
           include_all_projects=False, embedding_model=None,
           embedding_revision=None, mode="hybrid", at=None):
    """Compatibility list API over the bounded semantic-recall page."""
    page = recall_page(
        conn, task, k=k, embed_fn=embed_fn, min_sim=min_sim, qv=qv,
        exclude_session=exclude_session, project=project,
        include_all_projects=include_all_projects,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision, mode=mode, at=at,
    )
    return list(page.results)
