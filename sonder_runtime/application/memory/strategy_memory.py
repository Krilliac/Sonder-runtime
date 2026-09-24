"""Content-free strategy history projected from sealed runtime attempts.

The checkpoint remains the authority for an attempt. The canonical memory
database indexes only typed identities and outcomes for bounded cross-run
retrieval. A retrieved pattern is never a runtime rule; only independently
authenticated verifier observations can advance the existing learning ladder.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from sonder_runtime.application.context_planner import ContextPlan
from sonder_runtime.application.context_priority import (
    ContextItem,
    Selection,
    select_context,
)
from sonder_runtime.application.memory.learning_ladder import (
    LearningLadder,
    LearningObservation,
    LearningStage,
)
from sonder_runtime.application.memory.receipt_observation import (
    ReceiptObservationProducer,
)
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.domain.strategy.models import (
    FAMILIES,
    FailureClass,
    ProgressAssessment,
    StrategyAttempt,
    StrategyUsage,
    assess_progress,
)

MAX_LOOKUP = 32
REF_COST_TOKENS = 48
RECOVERY_HEADER_TOKENS = 32
RECOVERY_RENDER_TOKENS = 128
MAX_RECOVERY_REFS = 8
LANGUAGES = frozenset({"unknown", "py", "rs", "js", "ts", "go", "java", "cs",
                       "cpp", "c", "rb"})


def language_from_path(path: str) -> str:
    """Only a trusted host filename extension can supply language metadata."""
    if not isinstance(path, str) or not 1 <= len(path) <= 4096:
        return "unknown"
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return suffix if suffix in LANGUAGES - {"unknown"} else "unknown"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def _scope(scope: str) -> str:
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 4096:
        raise ValueError("bounded host project scope is required")
    return _digest(scope)


@dataclass(frozen=True, slots=True)
class StrategyExperience:
    experience_id: str
    run_id: str
    attempt_id: str
    project_digest: str
    attempt_digest: str
    signature_digest: str
    objective_digest: str
    family: str
    outcome: str
    failure_class: str
    failure_fingerprint: str
    verifier_fingerprint: str
    language: str
    subsystem_digest: str
    progress: str
    usage: StrategyUsage
    evidence_digests: tuple[str, ...]
    verifier_observation_id: str = ""

    def __post_init__(self) -> None:
        for key in ("experience_id", "run_id", "attempt_id", "project_digest", "attempt_digest", "signature_digest",
                    "objective_digest", "subsystem_digest", "verifier_fingerprint"):
            if not re.fullmatch(r"[a-f0-9]{64}", getattr(self, key)):
                raise ValueError(f"{key} must be a SHA-256 digest")
        if self.failure_fingerprint and not re.fullmatch(r"[a-f0-9]{64}", self.failure_fingerprint):
            raise ValueError("failure fingerprint must be a SHA-256 digest")
        if (
            not self.run_id or len(self.run_id) > 128
            or not self.attempt_id or len(self.attempt_id) > 128
            or self.family not in FAMILIES
            or self.outcome not in {"succeeded", "failed"}
            or (self.failure_class and self.failure_class not in {item.value for item in FailureClass})
            or self.language not in LANGUAGES
            or self.progress not in {item.value for item in ProgressAssessment}
            or not isinstance(self.usage, StrategyUsage)
            or not isinstance(self.evidence_digests, tuple)
            or len(self.evidence_digests) > 64
            or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item)
                   for item in self.evidence_digests)
            or (self.verifier_observation_id and not re.fullmatch(
                r"observation-[a-f0-9]{64}", self.verifier_observation_id
            ))
        ):
            raise ValueError("invalid content-free strategy experience")

    @classmethod
    def from_attempt(cls, attempt: StrategyAttempt, *, project_scope: str,
                     verifier_observation_id: str = "", language: str | None = None) -> StrategyExperience:
        suffixes = {part.rsplit(".", 1)[-1].lower() for part in attempt.signature.target_scope
                    if "." in part}
        inferred = next(iter(suffixes)) if len(suffixes) == 1 and next(iter(suffixes)) in LANGUAGES else "unknown"
        if language is not None and language not in LANGUAGES:
            raise ValueError("unknown host language metadata")
        selected_language = inferred if language is None else language
        return cls(
            _digest((attempt.run_id, attempt.attempt_id)), _digest(attempt.run_id),
            _digest(attempt.attempt_id),
            _scope(project_scope), attempt.digest, attempt.signature.digest,
            attempt.signature.objective_digest, attempt.signature.family,
            attempt.outcome, "" if attempt.failure is None else attempt.failure.classification.value,
            "" if attempt.failure is None else attempt.failure.evidence_digest,
            (attempt.failure.evidence_digest if attempt.failure and attempt.failure.evidence_digest
             else _digest(attempt.signature.verifier_target)), selected_language,
            _digest(attempt.signature.target_scope),
            assess_progress(attempt.progress_before, attempt.progress_after).value,
            attempt.usage, tuple(sorted(ref.digest for ref in attempt.evidence)),
            verifier_observation_id,
        )


@dataclass(frozen=True, slots=True)
class StrategyMemoryRef:
    experience_id: str
    signature_digest: str
    family: str
    outcome: str
    failure_class: str
    progress: str
    confidence: float
    stage: LearningStage
    evidence_digests: tuple[str, ...]
    verified: bool = False
    provenance: tuple[str, ...] = ()
    language: str = "unknown"


@dataclass(frozen=True, slots=True)
class StrategyMemorySelection:
    references: tuple[StrategyMemoryRef, ...]
    context_selection: Selection


@dataclass(frozen=True)
class StrategyRecoveryContext:
    """Host-selected, budgeted experience references exposed before a retry."""

    selection: StrategyMemorySelection
    prompt_brief: str


class StrategyExperienceStore(Protocol):
    def append(self, experience: StrategyExperience) -> StrategyExperience: ...
    def get(self, experience_id: str) -> StrategyExperience | None: ...
    def relevant(self, project_digest: str, *, failure_class: str, family: str,
                 verifier_fingerprint: str, language: str, subsystem_digest: str, outcome: str,
                 limit: int) -> tuple[StrategyExperience, ...]: ...
    def by_signature(self, project_digest: str, signature_digest: str,
                     *, limit: int) -> tuple[StrategyExperience, ...]: ...
    def selections(self, run_id: str, attempt_id: str) -> tuple[str, ...]: ...
    def select(self, run_id: str, attempt_id: str, experience_ids: tuple[str, ...]) -> None: ...
    def complete(self, run_id: str, attempt_id: str, outcome: str) -> None: ...
    def failed_reuses(self, experience_id: str) -> int: ...


class StrategyMemoryService:
    """Index recorded attempts; supply reference-only context before recovery."""

    def __init__(self, trace: StrategyTraceService, unit_of_work: Callable,
                 *, ladder: LearningLadder | None = None) -> None:
        self._trace = trace
        self._unit_of_work = unit_of_work
        self._ladder = ladder or LearningLadder()

    def observe_recorded(self, run_id: str, attempt_id: str, *, project_scope: str,
                         verifier_observation_id: str | None = None,
                         language: str | None = None) -> StrategyExperience:
        """Re-read a sealed attempt; reject caller-supplied verdicts and proof."""
        attempts = tuple(item for item in self._trace.history(run_id) if item.attempt_id == attempt_id)
        if len(attempts) != 1:
            raise ValueError("exactly one durable strategy attempt is required")
        attempt = attempts[0]
        if attempt.outcome not in {"succeeded", "failed"}:
            raise ValueError("only terminal strategy attempts enter memory")
        with self._unit_of_work() as scope:
            if verifier_observation_id is not None:
                pair = scope.verifier_observations.get(verifier_observation_id)
                if pair is None:
                    raise PermissionError("authenticated verifier observation is unavailable")
                receipt, observation = pair
                matching_verdict = (receipt.verifier_outcome == "passed") == (attempt.outcome == "succeeded")
                if (
                    receipt.run_id != run_id or receipt.project_scope != project_scope
                    or receipt.workspace_scope != project_scope
                    or observation.source != ReceiptObservationProducer.SOURCE
                    or observation.trusted_source is not True
                    or observation.positive != (attempt.outcome == "succeeded")
                    or not matching_verdict
                    or not any(ref.kind == "verifier" and ref.digest == receipt.receipt_digest
                               for ref in attempt.evidence)
                ):
                    raise PermissionError("verifier proof is not bound to the durable attempt")
            experience = StrategyExperience.from_attempt(
                attempt, project_scope=project_scope,
                verifier_observation_id=verifier_observation_id or "",
                language=language,
            )
            stored = scope.strategy_experiences.append(experience)
            scope.strategy_experiences.complete(run_id, attempt_id, attempt.outcome)
            return stored

    def _assessment(self, scope, experience: StrategyExperience) -> tuple[LearningStage, float, bool]:
        rows = scope.strategy_experiences.by_signature(
            experience.project_digest, experience.signature_digest, limit=10_001,
        )
        if len(rows) > 10_000:
            raise ValueError("strategy evidence snapshot is incomplete")
        observations = []
        verified = False
        for row in rows:
            pair = scope.verifier_observations.get(row.verifier_observation_id) if row.verifier_observation_id else None
            trusted = False
            independence = row.experience_id
            if pair is not None:
                receipt, original = pair
                trusted = (
                    _digest(receipt.run_id) == row.run_id
                    and _scope(receipt.project_scope) == experience.project_digest
                    and receipt.workspace_scope == receipt.project_scope
                    and receipt.receipt_digest in row.evidence_digests
                    and original.source == ReceiptObservationProducer.SOURCE
                    and original.trusted_source is True
                    and original.positive == (row.outcome == "succeeded")
                )
                if trusted:
                    independence = original.independent_key
            if row.experience_id == experience.experience_id:
                verified = trusted
            observations.append(LearningObservation(
                observation_id=row.experience_id,
                content="strategy:" + experience.project_digest + ":" + experience.signature_digest,
                source="verified_strategy" if trusted else "sealed_attempt",
                independent_key=independence,
                provenance=("attempt:" + row.attempt_digest,
                            *(() if not trusted else ("verifier:" + pair[0].receipt_digest,))),
                confidence=1.0 if trusted else 0.0,
                positive=row.outcome == "succeeded", trusted_source=trusted,
                # A host verifier confirms a result, not held-out policy evaluation.
                evaluation_passed=False, explicit_confirmation=False,
            ))
            if scope.strategy_experiences.failed_reuses(row.experience_id):
                observations.append(LearningObservation(
                    observation_id="reuse-failure-" + row.experience_id,
                    content="strategy:" + experience.project_digest + ":" + experience.signature_digest,
                    source="failed_reuse", independent_key=row.experience_id,
                    provenance=("experience:" + row.experience_id,), positive=False,
                ))
        decisions = self._ladder.evaluate(observations)
        stage = decisions[0].stage if decisions else LearningStage.OBSERVATION
        baseline = (0.80 if verified else 0.30) if experience.outcome == "succeeded" else 0.40
        confidence = baseline * (0.5 ** scope.strategy_experiences.failed_reuses(experience.experience_id))
        return stage, confidence, verified

    def select_for_attempt(self, run_id: str, attempt_id: str, *, project_scope: str,
                           plan: ContextPlan, failure_class: FailureClass | None = None,
                           family: str = "", verifier_fingerprint: str = "", language: str = "",
                           subsystem_digest: str = "", outcome: str = "") -> StrategyMemorySelection:
        """Reserve bounded typed references before a recovery attempt.

        The planner's memories budget remains authoritative; an idempotent
        repeat cannot silently change an earlier selection or borrow tokens.
        """
        if not isinstance(plan, ContextPlan):
            raise TypeError("context planner decision is required")
        if failure_class is not None and not isinstance(failure_class, FailureClass):
            raise TypeError("typed failure class is required")
        if outcome not in {"", "succeeded", "failed"}:
            raise ValueError("unknown strategy outcome")
        memory_budget = plan.budget_for("memories")
        with self._unit_of_work() as scope:
            store: StrategyExperienceStore = scope.strategy_experiences
            existing = store.selections(run_id, attempt_id)
            if not existing and any(item.attempt_id == attempt_id for item in self._trace.history(run_id)):
                raise ValueError("strategy memory must be selected before a durable attempt")
            if existing:
                rows = tuple(store.get(identity) for identity in existing)
                if any(row is None for row in rows):
                    raise ValueError("selected strategy experience is missing")
                if any(row.project_digest != _scope(project_scope) for row in rows):
                    raise ValueError("strategy selection belongs to another project")
            else:
                rows = store.relevant(
                    _scope(project_scope), failure_class="" if failure_class is None else failure_class.value,
                    family=family, verifier_fingerprint=verifier_fingerprint,
                    language=language, subsystem_digest=subsystem_digest,
                    outcome=outcome, limit=MAX_LOOKUP,
                )
            assessments = {row.experience_id: self._assessment(scope, row) for row in rows}
            ranked = list(rows) if existing else sorted(rows, key=lambda row: (
                -(assessments[row.experience_id][0].value),
                -(assessments[row.experience_id][1]), row.experience_id,
            ))
            refs = {
                row.experience_id: StrategyMemoryRef(
                    row.experience_id, row.signature_digest, row.family, row.outcome,
                    row.failure_class, row.progress, assessments[row.experience_id][1],
                    assessments[row.experience_id][0], row.evidence_digests,
                    assessments[row.experience_id][2],
                    ("attempt:" + row.attempt_digest,)
                    + (("verifier:" + row.verifier_observation_id,)
                       if assessments[row.experience_id][2] else ()),
                    row.language,
                ) for row in ranked
            }
            candidates = tuple(ContextItem(
                item_id="strategy:" + row.experience_id, section="memories",
                cost=REF_COST_TOKENS,
                priority=100 * refs[row.experience_id].stage.value + round(100 * refs[row.experience_id].confidence),
                source="strategy-checkpoint-ref", confidence=refs[row.experience_id].confidence,
                ordinal=index,
            ) for index, row in enumerate(ranked))
            selected = select_context(candidates, budget=memory_budget)
            selected_ids = tuple(item.item_id.removeprefix("strategy:") for item in selected.selected)
            if existing and selected_ids != existing:
                raise ValueError("existing strategy selection exceeds or differs from context budget")
            if not existing and selected_ids:
                store.select(run_id, attempt_id, selected_ids)
            return StrategyMemorySelection(tuple(refs[identity] for identity in selected_ids), selected)

    def recovery_context(self, run_id: str, attempt_id: str, *, project_scope: str,
                         plan: ContextPlan, failure_class: FailureClass | None = None,
                         family: str = "", verifier_fingerprint: str = "", language: str = "",
                         subsystem_digest: str = "", outcome: str = "") -> StrategyRecoveryContext:
        """Select first, then render only content-free references within the host plan.

        The model sees past outcomes as observations, never instructions or
        authority. Selection is durable before a model call and later failed
        exposure is attributed only to these exact selected references.
        """
        if not isinstance(plan, ContextPlan):
            raise TypeError("host context plan is required")
        budget = plan.budget_for("memories")
        maximum = min(MAX_RECOVERY_REFS, max(0, (budget - RECOVERY_HEADER_TOKENS)
                                         // RECOVERY_RENDER_TOKENS))
        selection_plan = replace(
            plan,
            section_budgets={**plan.section_budgets, "memories": maximum * REF_COST_TOKENS},
            total_section_tokens=plan.total_section_tokens - budget + maximum * REF_COST_TOKENS,
        )
        selection = self.select_for_attempt(
            run_id, attempt_id, project_scope=project_scope, plan=selection_plan,
            failure_class=failure_class, family=family,
            verifier_fingerprint=verifier_fingerprint, language=language,
            subsystem_digest=subsystem_digest, outcome=outcome,
        )
        if not selection.references:
            return StrategyRecoveryContext(selection, "")
        brief = json.dumps({
            "kind": "past_strategy_observations", "authority": "advisory_only",
            "references": [{
                "id": ref.experience_id, "outcome": ref.outcome,
                "failure": ref.failure_class, "stage": ref.stage.name.lower(),
                "confidence": round(ref.confidence, 2), "verified": ref.verified,
                "language": ref.language,
            } for ref in selection.references],
        }, sort_keys=True, separators=(",", ":"))
        prompt_brief = "\n\n" + brief
        # These fields are ASCII-only and bounded; this conservative byte
        # envelope is a second check. The host still plans the entire prompt
        # against the selected provider's actual context window.
        if len(prompt_brief.encode("utf-8")) > budget * 2:
            raise ValueError("rendered strategy references exceed the host memory budget")
        return StrategyRecoveryContext(selection, prompt_brief)


__all__ = ["StrategyExperience", "StrategyExperienceStore", "StrategyMemoryRef",
           "StrategyMemorySelection", "StrategyRecoveryContext", "StrategyMemoryService",
           "language_from_path"]
