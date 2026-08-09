"""Typed conflict-aware refinements over existing Sonder state.

This is not a second learning loop. The first adapter targets the existing
``preferences`` table and may reference existing grounded ``outcomes``. It
neither proposes changes nor publishes executable skills. Every mutation is an
explicit local-only compare-and-swap journaled in the same SQLite transaction.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterable

import preference_learning
import goal_store


MAX_TEXT = 2048
MAX_EVIDENCE_ITEMS = 16
MAX_EVIDENCE_SUMMARY = 2048
MAX_EXPECTED_OUTCOME = 2048
MAX_HISTORY_LIMIT = 200


class RefinementConflict(RuntimeError):
    """The target changed after the caller's expected snapshot."""


class RefinementValidation(ValueError):
    """The bounded refinement request is invalid."""


class EvidenceKind(str, Enum):
    GROUNDED_OUTCOME = "grounded-outcome"
    MANUAL_VERIFICATION = "manual-verification"
    IMPROVEMENT_PROPOSAL = "improvement-proposal"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    summary: str
    interaction_id: str = ""
    signal: str = ""
    proposal_id: str = ""


@dataclass(frozen=True, slots=True)
class PreferencePatch:
    text: str | None = None
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class PreferenceSnapshot:
    id: str
    scope: str
    key: str
    text: str
    source_interaction: str | None
    confidence: float
    evidence_count: int
    enabled: bool
    revision: int


@dataclass(frozen=True, slots=True)
class RefinementRequest:
    preference_id: str
    expected_version: int
    patch: PreferencePatch
    evidence: tuple[Evidence, ...]
    expected_outcome: str
    execution_scope: str = "local"


@dataclass(frozen=True, slots=True)
class RefinementResult:
    refinement_id: str
    operation: str
    target_id: str
    before: PreferenceSnapshot
    after: PreferenceSnapshot
    before_digest: str
    after_digest: str
    parent_refinement_id: str = ""


def _bounded_text(value, field, maximum, *, required=True) -> str:
    if not isinstance(value, str):
        raise RefinementValidation("%s must be a string" % field)
    value = value.strip()
    if required and not value:
        raise RefinementValidation("%s must not be empty" % field)
    if len(value) > maximum:
        raise RefinementValidation("%s exceeds %d characters" % (field, maximum))
    return value


def _version(value) -> int:
    if isinstance(value, bool):
        raise RefinementValidation("expected_version must be an integer")
    original = value
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise RefinementValidation("expected_version must be an integer") from exc
    if not isinstance(original, str) and original != value:
        raise RefinementValidation("expected_version must be an integer")
    if value < 1:
        raise RefinementValidation("expected_version must be at least 1")
    return value


def _snapshot(row) -> PreferenceSnapshot:
    if row is None:
        raise RefinementValidation("preference does not exist")
    return PreferenceSnapshot(
        id=str(row["id"]), scope=str(row["scope"]), key=str(row["key"]),
        text=str(row["text"]), source_interaction=row["source_interaction"],
        confidence=float(row["confidence"]), evidence_count=int(row["evidence_count"]),
        enabled=bool(row["enabled"]), revision=int(row["revision"]),
    )


def _select_preference(conn, preference_id) -> PreferenceSnapshot:
    preference_id = _bounded_text(preference_id, "preference_id", 128)
    row = conn.execute(
        "SELECT id, scope, key, text, source_interaction, confidence, "
        "evidence_count, enabled, revision FROM preferences WHERE id=?",
        (preference_id,),
    ).fetchone()
    return _snapshot(row)


def preference_snapshot(conn, preference_id) -> PreferenceSnapshot:
    """Read the current version a caller must echo in a refinement request."""
    return _select_preference(conn, preference_id)


def snapshot_digest(snapshot: PreferenceSnapshot) -> str:
    payload = json.dumps(
        asdict(snapshot), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_evidence(conn, evidence: Iterable[Evidence]) -> tuple[Evidence, ...]:
    rows = tuple(evidence)
    if not rows:
        raise RefinementValidation("at least one evidence item is required")
    if len(rows) > MAX_EVIDENCE_ITEMS:
        raise RefinementValidation("evidence exceeds %d items" % MAX_EVIDENCE_ITEMS)
    checked = []
    for item in rows:
        if not isinstance(item, Evidence):
            raise RefinementValidation("evidence items must be Evidence values")
        summary = _bounded_text(item.summary, "evidence summary", MAX_EVIDENCE_SUMMARY)
        if item.kind is EvidenceKind.GROUNDED_OUTCOME:
            interaction_id = _bounded_text(item.interaction_id, "interaction_id", 128)
            signal = _bounded_text(item.signal, "signal", 64)
            exists = conn.execute(
                "SELECT 1 FROM outcomes WHERE interaction_id=? AND signal=? LIMIT 1",
                (interaction_id, signal),
            ).fetchone()
            if exists is None:
                raise RefinementValidation("grounded outcome evidence does not exist")
        elif item.kind is EvidenceKind.IMPROVEMENT_PROPOSAL:
            # Proposal ownership/review stays in the existing improvement
            # store; this history records only a verified bounded reference.
            proposal_id = _bounded_text(item.proposal_id, "proposal_id", 128)
            proposal = goal_store.get(proposal_id)
            if not proposal or proposal.get("status") != goal_store.STATUS_PROPOSED:
                raise RefinementValidation("improvement proposal evidence does not exist")
        elif item.kind is not EvidenceKind.MANUAL_VERIFICATION:
            raise RefinementValidation("unsupported evidence kind")
        checked.append(Evidence(
            kind=item.kind, summary=summary,
            interaction_id=item.interaction_id.strip(), signal=item.signal.strip(),
            proposal_id=item.proposal_id.strip(),
        ))
    if len(_evidence_json(tuple(checked))) > 16384:
        raise RefinementValidation("serialized evidence exceeds 16384 characters")
    return tuple(checked)


def _snapshot_json(snapshot: PreferenceSnapshot) -> str:
    encoded = json.dumps(
        asdict(snapshot), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    if len(encoded) > 16384:
        raise RefinementValidation("preference snapshot exceeds 16384 characters")
    return encoded


def _evidence_json(evidence: tuple[Evidence, ...]) -> str:
    return json.dumps(
        [{**asdict(item), "kind": item.kind.value} for item in evidence],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def _insert_history(
    conn, *, refinement_id, operation, parent_refinement_id, expected_version,
    before, after, evidence, expected_outcome,
):
    conn.execute(
        "INSERT INTO refinement_history("
        "id, target_kind, target_id, operation, parent_refinement_id, "
        "execution_scope, expected_version, before_version, after_version, "
        "before_digest, after_digest, before_json, after_json, evidence_json, "
        "expected_outcome) VALUES(?, 'preference', ?, ?, ?, 'local', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            refinement_id, before.id, operation, parent_refinement_id or None,
            expected_version, before.revision, after.revision,
            snapshot_digest(before), snapshot_digest(after),
            _snapshot_json(before), _snapshot_json(after), _evidence_json(evidence),
            expected_outcome,
        ),
    )


def apply_preference_refinement(conn, request: RefinementRequest) -> RefinementResult:
    """Atomically CAS one existing, non-executable preference and journal it."""
    if not isinstance(request, RefinementRequest):
        raise RefinementValidation("request must be a RefinementRequest")
    if not isinstance(request.patch, PreferencePatch):
        raise RefinementValidation("patch must be a PreferencePatch")
    if request.execution_scope != "local":
        raise RefinementValidation("refinement execution_scope must be local")
    expected = _version(request.expected_version)
    outcome = _bounded_text(
        request.expected_outcome, "expected_outcome", MAX_EXPECTED_OUTCOME,
    )
    if request.patch.text is None and request.patch.enabled is None:
        raise RefinementValidation("preference patch must change text or enabled")
    text = None
    if request.patch.text is not None:
        text = preference_learning.normalize_preference(
            _bounded_text(request.patch.text, "preference text", MAX_TEXT)
        )
        # Normalization may append terminal punctuation. Enforce the public
        # bound on the value that is actually persisted, not only its input.
        text = _bounded_text(text, "normalized preference text", MAX_TEXT)
    if request.patch.enabled is not None and not isinstance(request.patch.enabled, bool):
        raise RefinementValidation("preference enabled must be boolean")
    if conn.in_transaction:
        raise RefinementConflict("refinement requires a connection outside another transaction")
    refinement_id = secrets.token_hex(16)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = _select_preference(conn, request.preference_id)
        if before.revision != expected:
            raise RefinementConflict(
                "preference changed concurrently: expected version %d, found %d"
                % (expected, before.revision)
            )
        evidence = _validate_evidence(conn, request.evidence)
        candidate_text = before.text if text is None else text
        candidate_enabled = before.enabled if request.patch.enabled is None else request.patch.enabled
        if candidate_text == before.text and candidate_enabled == before.enabled:
            raise RefinementValidation("preference patch makes no change")
        cur = conn.execute(
            "UPDATE preferences SET text=?, enabled=?, revision=revision + 1, "
            "updated_ts=CURRENT_TIMESTAMP WHERE id=? AND revision=?",
            (candidate_text, 1 if candidate_enabled else 0, before.id, expected),
        )
        if cur.rowcount != 1:
            raise RefinementConflict("preference changed during compare-and-swap")
        after = _select_preference(conn, before.id)
        _insert_history(
            conn, refinement_id=refinement_id, operation="apply",
            parent_refinement_id="", expected_version=expected,
            before=before, after=after, evidence=evidence, expected_outcome=outcome,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return RefinementResult(
        refinement_id=refinement_id, operation="apply", target_id=before.id,
        before=before, after=after, before_digest=snapshot_digest(before),
        after_digest=snapshot_digest(after),
    )


def rollback_preference_refinement(
    conn, refinement_id: str, expected_version: int,
) -> RefinementResult:
    """Rollback one apply record only if its exact post-state remains current."""
    refinement_id = _bounded_text(refinement_id, "refinement_id", 128)
    expected = _version(expected_version)
    if conn.in_transaction:
        raise RefinementConflict("rollback requires a connection outside another transaction")
    rollback_id = secrets.token_hex(16)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM refinement_history WHERE id=?", (refinement_id,),
        ).fetchone()
        if row is None or row["operation"] != "apply":
            raise RefinementValidation("refinement is not a rollback-capable apply record")
        recorded_before = _snapshot(json.loads(row["before_json"]))
        recorded_after = _snapshot(json.loads(row["after_json"]))
        if (
            recorded_before.id != row["target_id"]
            or recorded_after.id != row["target_id"]
            or snapshot_digest(recorded_before) != row["before_digest"]
            or snapshot_digest(recorded_after) != row["after_digest"]
            or recorded_before.revision != int(row["before_version"])
            or recorded_after.revision != int(row["after_version"])
        ):
            raise RefinementConflict("rollback refused: refinement history is inconsistent")
        current = _select_preference(conn, row["target_id"])
        if expected != current.revision:
            raise RefinementConflict(
                "preference changed concurrently: expected version %d, found %d"
                % (expected, current.revision)
            )
        if current.revision != int(row["after_version"]) or snapshot_digest(current) != row["after_digest"]:
            raise RefinementConflict(
                "rollback refused: preference no longer matches refinement post-state"
            )
        cur = conn.execute(
            "UPDATE preferences SET text=?, enabled=?, revision=revision + 1, "
            "updated_ts=CURRENT_TIMESTAMP WHERE id=? AND revision=?",
            (recorded_before.text, int(recorded_before.enabled), current.id, current.revision),
        )
        if cur.rowcount != 1:
            raise RefinementConflict("preference changed during rollback compare-and-swap")
        restored = _select_preference(conn, current.id)
        evidence = (Evidence(
            EvidenceKind.MANUAL_VERIFICATION,
            "Rollback requested for refinement %s." % refinement_id,
        ),)
        _insert_history(
            conn, refinement_id=rollback_id, operation="rollback",
            parent_refinement_id=refinement_id, expected_version=expected,
            before=current, after=restored, evidence=evidence,
            expected_outcome="Restore the preference state captured before refinement %s." % refinement_id,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return RefinementResult(
        refinement_id=rollback_id, operation="rollback", target_id=current.id,
        before=current, after=restored, before_digest=snapshot_digest(current),
        after_digest=snapshot_digest(restored), parent_refinement_id=refinement_id,
    )


def refinement_history(conn, preference_id="", limit=50) -> list[dict]:
    """Read bounded append-only history, newest first."""
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise RefinementValidation("history limit must be an integer") from exc
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    if preference_id:
        preference_id = _bounded_text(preference_id, "preference_id", 128)
        rows = conn.execute(
            "SELECT * FROM refinement_history WHERE target_id=? "
            "ORDER BY rowid DESC LIMIT ?", (preference_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM refinement_history ORDER BY rowid DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
