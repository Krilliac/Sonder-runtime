"""Bounded host proposals and evidence-only delegated child projections.

Child output may suggest work; only the host can turn a typed proposal into a
durably admitted descendant. These values contain references, not transcripts.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    IntegrationError,
    WorkspaceAssignment,
)
from sonder_runtime.application.ports.subagents import (
    SubagentBudget,
    SubagentHandle,
    SubagentStatus,
    SubagentUsage,
)
from sonder_runtime.application.ports.worker_registry import WorkerExecutionContract

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
MAX_PROPOSALS = 3
MAX_REFERENCES = 16


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise IntegrationError(f"{name} must be a bounded identity")
    return value.strip()


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise IntegrationError(f"{name} must be a SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class SpecialistRequest:
    """Host-reviewed narrow work and resource reservation for one descendant."""

    child_id: str
    preset: str
    prompt: str
    workspace: WorkspaceAssignment
    budget: SubagentBudget
    contract: WorkerExecutionContract
    hypothesis_digest: str = ""
    speculative_lane_id: str = ""

    def __post_init__(self) -> None:
        _identifier(self.child_id, "child_id")
        _identifier(self.preset, "preset")
        if not isinstance(self.prompt, str) or not self.prompt.strip() or len(self.prompt) > 16_000:
            raise IntegrationError("specialist prompt exceeds its bound")
        if not isinstance(self.workspace, WorkspaceAssignment) or not isinstance(self.budget, SubagentBudget):
            raise IntegrationError("specialist requires typed workspace and budget")
        if not isinstance(self.contract, WorkerExecutionContract) or not self.contract.requested:
            raise IntegrationError("specialist requires an explicit execution contract")
        if self.contract.speculative_lane:
            _digest(self.hypothesis_digest, "hypothesis_digest")
            normalized_hypothesis = " ".join(self.prompt.casefold().split())
            if self.hypothesis_digest != hashlib.sha256(normalized_hypothesis.encode("utf-8")).hexdigest():
                raise IntegrationError("hypothesis digest must bind its actual proposed work")
            _identifier(self.speculative_lane_id, "speculative_lane_id")
            if not self.contract.task_scope or self.contract.owned_files:
                raise IntegrationError("speculative lane requires a question and no owned files")
        elif self.hypothesis_digest or self.speculative_lane_id:
            raise IntegrationError("hypothesis identity requires an explicit speculative lane")


@dataclass(frozen=True, slots=True)
class NeedsDelegation:
    """Proposal bound to the already integrated parent's durable output."""

    parent_child_id: str
    source_output_digest: str
    specialists: tuple[SpecialistRequest, ...]
    proposal_id: str

    def __post_init__(self) -> None:
        _identifier(self.parent_child_id, "parent_child_id")
        _identifier(self.proposal_id, "proposal_id")
        _digest(self.source_output_digest, "source_output_digest")
        if (
            type(self.specialists) is not tuple or not 1 <= len(self.specialists) <= MAX_PROPOSALS
            or any(not isinstance(item, SpecialistRequest) for item in self.specialists)
        ):
            raise IntegrationError("delegation requires bounded typed specialists")
        if len({item.child_id for item in self.specialists}) != len(self.specialists):
            raise IntegrationError("specialist child identities must be unique")
        hypotheses = tuple(item for item in self.specialists if item.hypothesis_digest)
        if hypotheses:
            if len(hypotheses) != len(self.specialists) or len(hypotheses) < 2:
                raise IntegrationError("hypothesis search requires at least two explicit speculative lanes")
            if len({item.hypothesis_digest for item in hypotheses}) != len(hypotheses):
                raise IntegrationError("hypotheses must be materially distinct")
            if len({item.speculative_lane_id for item in hypotheses}) != len(hypotheses):
                raise IntegrationError("speculative lane identities must be distinct")
            if len({item.contract.task_scope for item in hypotheses}) != 1:
                raise IntegrationError("hypotheses must investigate the same owned question")


@dataclass(frozen=True, slots=True)
class DescendantDispatch:
    request: DelegationRequest
    handle: SubagentHandle


class PartialDelegationError(IntegrationError):
    """The caller retains the already launched handles when a later lane fails."""

    def __init__(self, dispatched: tuple[DescendantDispatch, ...]):
        self.dispatched = dispatched
        super().__init__("some descendants were admitted; supervise their durable handles")


@dataclass(frozen=True, slots=True)
class StructuredChildResult:
    """Host-derived bounded terminal projection suitable for parent fan-in."""

    child_id: str
    status: SubagentStatus
    conclusion: str
    output_digest: str
    usage: SubagentUsage
    evidence_refs: tuple[str, ...]
    failure_code: str = ""
    verifier_receipts: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    mutations: tuple[str, ...] = ()
    suggested_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.child_id, "child_id")
        _digest(self.output_digest, "output_digest")
        if self.conclusion not in {"supported", "rejected", "unresolved"}:
            raise IntegrationError("structured conclusion must be an evidence status")
        if not isinstance(self.status, SubagentStatus) or not isinstance(self.usage, SubagentUsage):
            raise IntegrationError("structured child result requires typed terminal status and usage")
        if (
            not isinstance(self.failure_code, str) or len(self.failure_code) > 128
            or (self.status is SubagentStatus.SUCCEEDED and self.failure_code)
            or (self.status is not SubagentStatus.SUCCEEDED and not self.failure_code)
        ):
            raise IntegrationError("structured failure code must match the terminal status")
        for name in ("evidence_refs", "verifier_receipts", "artifact_refs"):
            values = getattr(self, name)
            if type(values) is not tuple or len(values) > MAX_REFERENCES:
                raise IntegrationError(f"{name} exceeds its bound")
            for value in values:
                _digest(value, name)
        for name in ("assumptions", "unresolved_questions", "mutations", "suggested_actions"):
            values = getattr(self, name)
            if type(values) is not tuple or len(values) > MAX_REFERENCES:
                raise IntegrationError(f"{name} exceeds its bound")
            for value in values:
                _digest(value, name)


@dataclass(frozen=True, slots=True)
class HypothesisDecision:
    """Deterministic selection over comparable canonical child evidence."""

    winning_child_id: str | None
    ranking: tuple[str, ...]
    results: tuple[StructuredChildResult, ...]
    reason: str


__all__ = ["DescendantDispatch", "HypothesisDecision", "NeedsDelegation",
           "PartialDelegationError", "SpecialistRequest", "StructuredChildResult"]
