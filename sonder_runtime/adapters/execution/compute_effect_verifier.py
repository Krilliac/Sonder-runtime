"""Reconcile local compute submit and cancel effects from durable job evidence.

A job's exit status is not the submit effect.  The process registry must show
that the exact journaled request was attached to a real process, then reached
a terminal status before a new owner can settle the interrupted intent.
A cancel is proven only by a terminal ``cancelled`` record with immutable
cleanup evidence and a durable binding of the exact journaled cancel request.
Unknown, remote, legacy, or still-running work remains fenced.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from sonder_runtime.adapters.execution.process_jobs import DurableProcessEffectVerifier
from sonder_runtime.application.compute_fabric.jobs import MAX_COMPUTE_CANCEL_ATTEMPTS
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent,
    EffectState,
    ReconciliationProof,
)
from sonder_runtime.application.jobs.durable_registry import (
    CANCEL_REQUEST_DIGESTS,
    _validate_cleanup_evidence,
)
from sonder_runtime.domain.compute_fabric import WorkloadKind

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_CANCEL_ATTEMPT = re.compile(r"attempt-([1-9][0-9]{0,2})\Z")
_LOCAL_KINDS = frozenset(
    f"compute-{kind.value}" for kind in WorkloadKind if kind is not WorkloadKind.INFERENCE
)


def _compute_job_id(worker_id: str, idempotency_key: str) -> str:
    return "cf-" + hashlib.sha256(
        f"{worker_id}\x00{idempotency_key}".encode()
    ).hexdigest()[:24]


def _attached_compute_job(registry_getter: Callable[[], Any], worker_id: str,
                          idempotency_key: str) -> tuple[str, Any, Any, dict] | None:
    """Return only a terminal, durably attached and identity-bound local job."""
    job_id = _compute_job_id(worker_id, idempotency_key)
    try:
        view = registry_getter().view(job_id)
    except KeyError:
        return None
    record = getattr(view, "record", None)
    identity = getattr(record, "identity", None)
    metadata = getattr(view, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    controller = metadata.get("compute_controller_job_id")
    process_id = getattr(view, "process_id", None)
    revision = getattr(record, "revision", None)
    status = getattr(getattr(record, "status", None), "value", None)
    if (
        identity is None
        or getattr(identity, "job_id", None) != job_id
        or getattr(identity, "kind", None) not in _LOCAL_KINDS
        or getattr(identity, "idempotency_key", None) != idempotency_key
        or not isinstance(controller, str) or not _IDENTITY.fullmatch(controller)
        or getattr(identity, "operation_id", None) != controller
        or metadata.get("compute_worker_id") != worker_id
        or not isinstance(metadata.get("compute_request_sha256"), str)
        or not _SHA256.fullmatch(metadata["compute_request_sha256"])
        or not isinstance(metadata.get("compute_effect_request_digest"), str)
        or not _SHA256.fullmatch(metadata["compute_effect_request_digest"])
        or not isinstance(metadata.get("process_request_digest"), str)
        or not _SHA256.fullmatch(metadata["process_request_digest"])
        or metadata.get("require_job_scope") != "1"
        or metadata.get("launch_state") != "attached"
        or type(process_id) is not int or process_id <= 0
        or status not in _TERMINAL
        or type(revision) is not int or revision < 1
    ):
        return None
    return job_id, view, identity, metadata


class DurableComputeSubmitVerifier:
    """Host-owned proof of a local compute process launch, never job success."""

    verifier_id = "durable-compute-submit-process-v1"
    operation_ids = frozenset({"compute-submit"})

    def __init__(self, registry_getter: Callable[[], Any]) -> None:
        if not callable(registry_getter):
            raise TypeError("registry_getter must be callable")
        self._registry_getter = registry_getter

    def verify(self, intent: EffectIntent) -> ReconciliationProof | None:
        fields = intent.operation_id.split(":")
        if len(fields) != 3 or fields[0] != "compute-submit":
            return None
        worker_id, idempotency_key = fields[1:]
        if (
            not _IDENTITY.fullmatch(worker_id)
            or not _IDENTITY.fullmatch(idempotency_key)
            or intent.idempotency_key != idempotency_key
            or intent.worker_id != f"compute:{worker_id}"
            or intent.run_id != "runtime:compute-jobs"
            or intent.scope != "compute-jobs"
            or intent.reconciliation != "idempotent"
            or not _SHA256.fullmatch(intent.request_digest)
        ):
            return None
        attached = _attached_compute_job(self._registry_getter, worker_id, idempotency_key)
        if attached is None:
            return None
        job_id, view, identity, metadata = attached
        record = view.record
        status = record.status.value
        revision = getattr(record, "revision", None)
        process_id = getattr(view, "process_id", None)
        if metadata["compute_effect_request_digest"] != intent.request_digest:
            return None
        canonical = {
            "job_id": job_id,
            "kind": identity.kind,
            "controller_job_id": identity.operation_id,
            "idempotency_key": idempotency_key,
            "compute_request_sha256": metadata["compute_request_sha256"],
            "effect_request_digest": intent.request_digest,
            "process_id": process_id,
            "status": status,
            "revision": revision,
        }
        return ReconciliationProof(
            intent_id=intent.intent_id,
            operation_id=intent.operation_id,
            receipt_key=job_id,
            outcome_digest=hashlib.sha256(json.dumps(
                canonical, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            state=EffectState.COMPLETED,
            verifier_id=self.verifier_id,
            external_reference=f"job-registry:{job_id}:{revision}",
        )


def _cancel_attempt(operation_id: str) -> tuple[str, str, str] | None:
    """Parse ``compute-cancel`` identities exactly as the worker mints them.

    Returns ``(worker_id, remote_job_id, idempotency_key)``.  Attempt 1 keeps
    the historical three-field form; later attempts carry ``attempt-N``.
    """
    fields = operation_id.split(":")
    if len(fields) == 3 and fields[0] == "compute-cancel":
        worker_id, job_id = fields[1:]
        key = f"cancel:{job_id}"
    elif len(fields) == 4 and fields[0] == "compute-cancel":
        worker_id, marker, job_id = fields[1:]
        match = _CANCEL_ATTEMPT.fullmatch(marker)
        if match is None or not 2 <= int(match.group(1)) <= MAX_COMPUTE_CANCEL_ATTEMPTS:
            return None
        key = f"cancel:{job_id}:{marker}"
    else:
        return None
    if not _IDENTITY.fullmatch(worker_id) or not _IDENTITY.fullmatch(job_id):
        return None
    return worker_id, job_id, key


def _cleanup_evidence(registry: Any, record: Any) -> dict | None:
    """Return the immutable cleanup proof bound to this exact terminal record."""
    read = getattr(registry, "process_cleanup_proof", None)
    if not callable(read):
        return None
    proof = read(record.identity.job_id)
    if not isinstance(proof, dict):
        return None
    try:
        _validate_cleanup_evidence(record, proof)
    except (AttributeError, TypeError, ValueError):
        return None
    digest = proof.get("digest")
    body = {key: value for key, value in proof.items() if key != "digest"}
    try:
        expected = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
    except (TypeError, ValueError):
        return None
    if not isinstance(digest, str) or digest != expected:
        return None
    return proof


class DurableComputeCancelVerifier:
    """Prove an interrupted local compute cancel from durable registry state.

    Proof requires, all from the durable job registry: the exact remote job
    minted by this worker for its own submit idempotency key, a local compute
    kind and controller binding, a durable binding of this attempt's
    idempotency key to the journaled request digest (written after the intent
    committed and before the provider was asked to cancel), a terminal
    ``cancelled`` status, and immutable cleanup evidence for that exact
    record revision.  ``cancellation_requested``, any other terminal status,
    a missing or legacy binding, or missing cleanup evidence yields no proof.
    Caller text, process output and in-memory handles are never consulted.
    """

    verifier_id = "durable-compute-cancel-v1"
    operation_ids = frozenset({"compute-cancel"})

    def __init__(self, registry_getter: Callable[[], Any]) -> None:
        if not callable(registry_getter):
            raise TypeError("registry_getter must be callable")
        self._registry_getter = registry_getter

    def verify(self, intent: EffectIntent) -> ReconciliationProof | None:
        parsed = _cancel_attempt(intent.operation_id)
        if parsed is None:
            return None
        worker_id, job_id, idempotency_key = parsed
        if (
            intent.idempotency_key != idempotency_key
            or intent.worker_id != f"compute:{worker_id}"
            or intent.run_id != "runtime:compute-jobs"
            or intent.scope != "compute-jobs"
            or intent.reconciliation != "idempotent"
            or not _SHA256.fullmatch(intent.request_digest)
        ):
            return None
        registry = self._registry_getter()
        try:
            view = registry.view(job_id)
        except KeyError:
            return None
        record = getattr(view, "record", None)
        identity = getattr(record, "identity", None)
        metadata = getattr(view, "metadata", None)
        if not isinstance(metadata, dict) or identity is None:
            return None
        controller = metadata.get("compute_controller_job_id")
        bindings = metadata.get(CANCEL_REQUEST_DIGESTS)
        status = getattr(getattr(record, "status", None), "value", None)
        revision = getattr(record, "revision", None)
        job_key = getattr(identity, "idempotency_key", None)
        if (
            getattr(identity, "job_id", None) != job_id
            or getattr(identity, "kind", None) not in _LOCAL_KINDS
            or not isinstance(job_key, str) or not _IDENTITY.fullmatch(job_key)
            # The job must be the one this worker minted for its own submit;
            # a record swapped under another job id cannot satisfy this.
            or _compute_job_id(worker_id, job_key) != job_id
            or metadata.get("compute_worker_id") != worker_id
            or not isinstance(controller, str) or not _IDENTITY.fullmatch(controller)
            or getattr(identity, "operation_id", None) != controller
            or metadata.get("require_job_scope") != "1"
            or not isinstance(bindings, dict)
            or bindings.get(idempotency_key) != intent.request_digest
            or status != "cancelled"
            or type(revision) is not int or revision < 1
        ):
            return None
        cleanup = _cleanup_evidence(registry, record)
        if cleanup is None:
            return None
        canonical = {
            "job_id": job_id,
            "kind": identity.kind,
            "controller_job_id": controller,
            "job_idempotency_key": job_key,
            "cancel_idempotency_key": idempotency_key,
            "effect_request_digest": intent.request_digest,
            "status": status,
            "revision": revision,
            "cleanup_digest": cleanup["digest"],
        }
        return ReconciliationProof(
            intent_id=intent.intent_id,
            operation_id=intent.operation_id,
            # The live receipt shape for a cleaned cancellation.
            receipt_key=f"{job_id}:cancelled",
            outcome_digest=hashlib.sha256(json.dumps(
                canonical, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            state=EffectState.COMPLETED,
            verifier_id=self.verifier_id,
            external_reference=f"job-registry:{job_id}:{revision}",
        )


class DurableComputeProcessStartVerifier:
    """Prove the inner process-start intent for the same local compute job."""

    verifier_id = "durable-compute-process-start-v1"
    operation_ids = frozenset({"process-start"})

    def __init__(self, registry_getter: Callable[[], Any]) -> None:
        if not callable(registry_getter):
            raise TypeError("registry_getter must be callable")
        self._registry_getter = registry_getter

    def verify(self, intent: EffectIntent) -> ReconciliationProof | None:
        prefix, separator, job_id = intent.operation_id.partition(":")
        if prefix != "process-start" or not separator or not job_id.startswith("cf-"):
            return None
        if (
            intent.run_id != "runtime:process-jobs"
            or intent.scope != "process-jobs"
            or intent.reconciliation != "idempotent"
            or not _IDENTITY.fullmatch(intent.idempotency_key)
            or not _SHA256.fullmatch(intent.request_digest)
        ):
            return None
        worker_id = intent.worker_id.removeprefix("process:")
        if (
            intent.worker_id != f"process:{worker_id}"
            or not _IDENTITY.fullmatch(worker_id)
        ):
            return None
        attached = _attached_compute_job(
            self._registry_getter, worker_id, intent.idempotency_key,
        )
        if attached is None or attached[0] != job_id:
            return None
        _, view, identity, metadata = attached
        if metadata["process_request_digest"] != intent.request_digest:
            return None
        canonical = {
            "job_id": job_id,
            "kind": identity.kind,
            "operation_id": identity.operation_id,
            "idempotency_key": intent.idempotency_key,
            "process_id": view.process_id,
            "status": view.record.status.value,
            "revision": view.record.revision,
        }
        return ReconciliationProof(
            intent_id=intent.intent_id,
            operation_id=intent.operation_id,
            receipt_key=f"{job_id}:{view.process_id}",
            outcome_digest=hashlib.sha256(json.dumps(
                canonical, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            state=EffectState.COMPLETED,
            verifier_id=self.verifier_id,
            external_reference=f"job-registry:{job_id}:{view.record.revision}",
        )


class DurableLocalProcessStartVerifier:
    """Keep ordinary process proof and admit only bounded compute process proof."""

    verifier_id = "durable-local-process-start-v1"
    operation_ids = frozenset({"process-start"})

    def __init__(self, registry_getter: Callable[[], Any]) -> None:
        self._registry_getter = registry_getter
        self._ordinary = DurableProcessEffectVerifier(registry_getter)
        self._compute = DurableComputeProcessStartVerifier(registry_getter)

    def verify(self, intent: EffectIntent) -> ReconciliationProof | None:
        prefix, separator, job_id = intent.operation_id.partition(":")
        if prefix != "process-start" or not separator or not job_id:
            return None
        try:
            view = self._registry_getter().view(job_id)
        except KeyError:
            return None
        metadata = getattr(view, "metadata", None)
        # A compute record with a corrupted kind must not fall through to the
        # generic verifier, whose legitimate scope is plain process jobs.
        if isinstance(metadata, dict) and any(
            key.startswith("compute_") for key in metadata if isinstance(key, str)
        ):
            return self._compute.verify(intent)
        return self._ordinary.verify(intent)


__all__ = [
    "DurableComputeCancelVerifier",
    "DurableComputeSubmitVerifier",
    "DurableLocalProcessStartVerifier",
]
