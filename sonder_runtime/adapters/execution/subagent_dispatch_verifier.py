"""Prove bounded local subagent dispatch only from durable child admission.

``LocalSubagentProvider`` journals ``subagent-dispatch:{child_id}`` around the
admission of one child, not around the child's whole runner.  The dispatch
effect is exactly this: the durable child store retained the canonical child
request and applied the transition that first moved it to ``running``.

Proof therefore comes only from the durable child store.  The verifier
recomputes the canonical request digest from the persisted request and binds
it to the journaled child, parent, idempotency key and request digest.  The
admitted revision comes from the retained receipt of the first applied
``running`` update in the store's mutation log.  Missing rows, mismatched
identities, and legacy rows without a retained admission record produce no
proof.  Terminal child status, result text, runner output and in-memory
handles are never consulted.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from sonder_runtime.application.execution.effect_journal import (
    EffectIntent,
    EffectState,
    ReconciliationProof,
)
from sonder_runtime.application.ports.subagents import SubagentRequest

DISPATCH_FAMILY = "subagent-dispatch"
DISPATCH_CONTRACT = "subagent-dispatch-v1"
DISPATCH_SCOPE = "local-subagents"
DISPATCH_RECONCILIATION = "query"
# The admitting ``running`` update is among a child's first mutations; a
# longer scan cannot be needed for a child this provider admitted and would
# only let a corrupted history consume unbounded verifier time.
MAX_ADMISSION_SCAN = 1000
_PAGE = 100
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _canonical_json(value: Any) -> bytes:
    # Byte-identical to ``worker_bindings._digest`` for JSON-native values,
    # so the live receipt digest and the verifier's proof digest agree.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def dispatch_operation_id(child_id: str) -> str:
    return f"{DISPATCH_FAMILY}:{child_id}"


def dispatch_run_id(child_id: str) -> str:
    return f"subagent:{child_id}"


def dispatch_idempotency_key(request: SubagentRequest) -> str:
    """The journal key: the durable idempotency key, else the child id."""
    if not request.child_id:
        raise ValueError("dispatch requires an allocated child_id")
    return request.idempotency_key or request.child_id


def canonical_dispatch_request(request: SubagentRequest) -> dict[str, Any]:
    """Return the JSON-native identity of one child request.

    Every field is persisted verbatim by the durable child store, so the same
    value can be recomputed from the stored row without a schema change.
    """
    if not request.child_id:
        raise ValueError("dispatch requires an allocated child_id")
    budget = asdict(request.budget)
    if budget["max_wall_seconds"] is not None:
        # Storage backends may round-trip ``30.0`` as ``30``; the digest must
        # not depend on which numeric spelling a backend happened to keep.
        budget["max_wall_seconds"] = float(budget["max_wall_seconds"])
    return {
        "contract": DISPATCH_CONTRACT,
        "child_id": request.child_id,
        "parent_id": request.parent_id,
        "prompt": request.prompt,
        "budget": budget,
        "metadata": [[key, value] for key, value in request.metadata],
        "resume_key": request.resume_key,
        "idempotency_key": request.idempotency_key,
    }


def dispatch_request_digest(request: SubagentRequest) -> str:
    return _sha256(canonical_dispatch_request(request))


def dispatch_receipt_key(child_id: str, admitted_revision: int) -> str:
    return f"{DISPATCH_FAMILY}:{child_id}:{admitted_revision}"


@dataclass(frozen=True, slots=True)
class SubagentDispatchAdmission:
    """Durable evidence that one exact child request was admitted."""

    child_id: str
    parent_id: str
    idempotency_key: str
    request_digest: str
    admitted_revision: int
    admission_operation_id: str

    def receipt(self) -> dict[str, Any]:
        """Content-free receipt value; its digest is the outcome digest."""
        return {
            "contract": DISPATCH_CONTRACT,
            "child_id": self.child_id,
            "parent_id": self.parent_id,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "admitted_revision": self.admitted_revision,
        }

    @property
    def receipt_key(self) -> str:
        return dispatch_receipt_key(self.child_id, self.admitted_revision)

    @property
    def outcome_digest(self) -> str:
        return _sha256(self.receipt())


def _admission_update(repository: Any, child_id: str) -> tuple[str, int] | None:
    """Return the first applied ``running`` update and its resulting revision."""
    after = 0
    scanned = 0
    while scanned < MAX_ADMISSION_SCAN:
        page, truncated = repository.mutation_ids(child_id, after=after, limit=_PAGE)
        if not page:
            return None
        for position, operation_id in page:
            scanned += 1
            after = position
            prepared = repository.read_mutation(operation_id)
            if prepared is None or prepared.child_id != child_id:
                return None
            if prepared.kind != "update":
                continue
            payload = json.loads(prepared.payload)
            if not isinstance(payload, Mapping) or payload.get("status") != "running":
                continue
            receipt = repository.reconcile(prepared)
            if receipt is None or receipt.disposition != "applied":
                # An unreceipted or refused admission is not an admission.
                continue
            revision = receipt.resulting_revision
            if type(revision) is not int or revision < 1:
                return None
            return operation_id, revision
        if not truncated:
            return None
    return None


class DurableSubagentDispatchVerifier:
    """Host-owned dispatch proof from the durable child-session store."""

    verifier_id = "durable-subagent-dispatch-v1"
    operation_ids = frozenset({DISPATCH_FAMILY})

    def __init__(self, repository_getter: Callable[[], Any]) -> None:
        if not callable(repository_getter):
            raise TypeError("repository_getter must be callable")
        self._repository_getter = repository_getter

    def admission(
        self, child_id: str, *, parent_id: str, idempotency_key: str,
        request_digest: str,
    ) -> SubagentDispatchAdmission | None:
        """Return durable admission only for the exact journaled identity."""
        if (
            not isinstance(child_id, str) or not child_id.strip()
            or not isinstance(parent_id, str) or not parent_id.strip()
            or not isinstance(idempotency_key, str) or not idempotency_key.strip()
            or not isinstance(request_digest, str)
            or not _SHA256.fullmatch(request_digest)
        ):
            return None
        repository = self._repository_getter()
        if repository is None:
            return None
        record = repository.get(child_id)
        if record is None:
            return None
        request = record.request
        if (
            request.child_id != child_id
            or request.parent_id != parent_id
            or record.lineage.parent_id != parent_id
            or dispatch_idempotency_key(request) != idempotency_key
            or dispatch_request_digest(request) != request_digest
        ):
            return None
        admitted = _admission_update(repository, child_id)
        if admitted is None:
            return None
        operation_id, revision = admitted
        return SubagentDispatchAdmission(
            child_id, parent_id, idempotency_key, request_digest, revision,
            operation_id,
        )

    def verify(self, intent: EffectIntent) -> ReconciliationProof | None:
        family, separator, child_id = intent.operation_id.partition(":")
        if (
            family != DISPATCH_FAMILY or not separator or not child_id
            or intent.run_id != dispatch_run_id(child_id)
            or intent.scope != DISPATCH_SCOPE
            or not intent.worker_id.startswith("subagent:")
            or intent.reconciliation != DISPATCH_RECONCILIATION
        ):
            return None
        repository = self._repository_getter()
        if repository is None:
            return None
        record = repository.get(child_id)
        if record is None:
            return None
        admission = self.admission(
            child_id,
            parent_id=record.request.parent_id,
            idempotency_key=intent.idempotency_key,
            request_digest=intent.request_digest,
        )
        if admission is None:
            return None
        return ReconciliationProof(
            intent_id=intent.intent_id,
            operation_id=intent.operation_id,
            receipt_key=admission.receipt_key,
            outcome_digest=admission.outcome_digest,
            state=EffectState.COMPLETED,
            verifier_id=self.verifier_id,
            external_reference=(
                f"child-store:{child_id}:{admission.admitted_revision}:"
                f"{admission.admission_operation_id}"
            ),
        )


__all__ = [
    "DISPATCH_CONTRACT", "DISPATCH_FAMILY", "DISPATCH_RECONCILIATION",
    "DISPATCH_SCOPE", "DurableSubagentDispatchVerifier",
    "SubagentDispatchAdmission", "canonical_dispatch_request",
    "dispatch_idempotency_key", "dispatch_operation_id",
    "dispatch_receipt_key", "dispatch_request_digest", "dispatch_run_id",
]
