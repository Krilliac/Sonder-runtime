"""Typed, durable retry execution for loop transport calls.

The domain retry policy decides whether replay is permitted.  This module
connects that decision to a transport while making every effect boundary
explicit: idempotency state is written before dispatch, retry evidence is
retained, unknown outcomes require reconciliation, and untyped failures fail
closed instead of being guessed into a retryable class.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Generic, Protocol, TypeVar

from ...domain.cancellation_tree import CancellationNode
from ...domain.loop_retry_policy import ReplayAction, SideEffectClass
from .durable_control import (
    IdempotencyReceipt,
    IdempotencyStore,
    RetryEvidence,
    RetryEvidenceLedger,
)


RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")


class ReconciliationState(str, Enum):
    COMMITTED = "committed"
    RETRY_SAFE = "retry_safe"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TransportFailure(Exception):
    """A transport failure with explicit retry classification metadata.

    ``outcome_known=False`` is the safe default: after dispatch, the caller
    cannot assume that a timeout or connection loss means no side effect.
    """

    code: str
    status: int | None = None
    outcome_known: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if not str(self.code).strip():
            raise ValueError("transport failure code is required")
        Exception.__init__(self, self.detail or self.code)


@dataclass(frozen=True, slots=True)
class ReconciliationResult(Generic[ResponseT]):
    state: ReconciliationState
    result: ResponseT | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.state, ReconciliationState):
            raise TypeError("reconciliation state must be typed")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("reconciliation evidence must be a mapping")


class RetryTransport(Protocol[RequestT, ResponseT]):
    """Concrete typed transport boundary used by the retry executor."""

    def send(self, request: RequestT, *, idempotency_key: str, attempt: int) -> ResponseT: ...

    def reconcile(
        self, request: RequestT, *, idempotency_key: str
    ) -> ReconciliationResult[ResponseT]: ...


@dataclass(frozen=True, slots=True)
class RetryExecutionResult(Generic[ResponseT]):
    result: ResponseT
    attempts: int
    replayed: bool
    evidence: tuple[RetryEvidence, ...]


class RetryExecutionError(RuntimeError):
    """The transport cannot be replayed safely or classified completely."""


class RetryCancelled(RetryExecutionError):
    """Cancellation stopped the executor before another transport effect."""


class TransportRetryExecutor(Generic[RequestT, ResponseT]):
    """Execute one idempotent transport operation with durable retry state."""

    def __init__(
        self,
        transport: RetryTransport[RequestT, ResponseT],
        *,
        idempotency: IdempotencyStore,
        evidence: RetryEvidenceLedger,
        cancellation: CancellationNode | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_sleep_seconds: float = 30.0,
    ) -> None:
        if not callable(getattr(transport, "send", None)) or not callable(getattr(transport, "reconcile", None)):
            raise TypeError("transport must implement typed send and reconcile methods")
        if not callable(sleep) or max_sleep_seconds < 0:
            raise ValueError("sleep callback and non-negative sleep bound are required")
        self._transport = transport
        self._idempotency = idempotency
        self._evidence = evidence
        self._cancellation = cancellation
        self._sleep = sleep
        self._max_sleep_seconds = float(max_sleep_seconds)

    def execute(
        self,
        operation_id: str,
        request: RequestT,
        *,
        fingerprint: str,
        idempotency_key: str,
        max_attempts: int = 3,
        effect: SideEffectClass = SideEffectClass.IDEMPOTENT,
    ) -> RetryExecutionResult[ResponseT]:
        self._validate(operation_id, fingerprint, idempotency_key, max_attempts, effect)
        existing = self._idempotency.begin(idempotency_key, fingerprint)
        if existing.status in {"completed", "reconciled"}:
            return RetryExecutionResult(existing.result, 0, True, ())

        records: list[RetryEvidence] = []
        for attempt in range(1, max_attempts + 1):
            self._check_cancelled()
            try:
                result = self._transport.send(
                    request, idempotency_key=idempotency_key, attempt=attempt
                )
            except TransportFailure as failure:
                decision = self._decision(failure, attempt, max_attempts, effect, idempotency_key)
                record = self._evidence.record(
                    operation_id, decision, attempt=attempt, failure_code=failure.code
                )
                records.append(record)
                if not failure.outcome_known:
                    self._idempotency.mark_unknown(
                        idempotency_key, fingerprint,
                        evidence={"attempt": attempt, "failure_code": failure.code},
                    )
                if decision.action is ReplayAction.RECONCILE_THEN_RETRY:
                    reconciliation = self._reconcile(request, idempotency_key)
                    if reconciliation.state is ReconciliationState.COMMITTED:
                        receipt = self._idempotency.reconcile(
                            idempotency_key, fingerprint, reconciliation.result,
                            evidence=reconciliation.evidence,
                        )
                        return RetryExecutionResult(receipt.result, attempt, True, tuple(records))
                    if reconciliation.state is not ReconciliationState.RETRY_SAFE:
                        raise RetryExecutionError(
                            "transport outcome is not proven safe to replay"
                        ) from failure
                elif decision.action is ReplayAction.DO_NOT_RETRY:
                    raise RetryExecutionError(
                        f"transport failure is not retryable: {failure.code}"
                    ) from failure
                self._wait(decision.backoff.cap_for_attempt(attempt))
                continue
            except Exception as exc:
                # A raw exception has no trustworthy outcome/classification.
                self._idempotency.mark_unknown(
                    idempotency_key, fingerprint,
                    evidence={"attempt": attempt, "failure_type": type(exc).__name__},
                )
                raise RetryExecutionError(
                    "untyped transport failure; reconciliation required"
                ) from exc
            receipt = self._idempotency.complete(idempotency_key, fingerprint, result)
            return RetryExecutionResult(receipt.result, attempt, attempt > 1, tuple(records))

        raise RetryExecutionError("retry attempt limit exhausted")

    def _reconcile(self, request: RequestT, key: str) -> ReconciliationResult[ResponseT]:
        try:
            result = self._transport.reconcile(request, idempotency_key=key)
        except Exception as exc:
            raise RetryExecutionError("transport reconciliation failed") from exc
        if not isinstance(result, ReconciliationResult):
            raise RetryExecutionError("transport returned untyped reconciliation result")
        return result

    def _check_cancelled(self) -> None:
        if self._cancellation is not None and self._cancellation.cancelled:
            raise RetryCancelled("retry operation cancelled")

    def _wait(self, seconds: float) -> None:
        self._check_cancelled()
        self._sleep(min(max(0.0, float(seconds)), self._max_sleep_seconds))
        self._check_cancelled()

    @staticmethod
    def _decision(failure, attempt, max_attempts, effect, key):
        from ...domain.loop_retry_policy import retry_decision

        return retry_decision(
            failure.code, status=failure.status, attempt=attempt,
            max_attempts=max_attempts, outcome_known=failure.outcome_known,
            effect=effect, idempotency_key=key,
        )

    @staticmethod
    def _validate(operation_id, fingerprint, key, max_attempts, effect) -> None:
        if not str(operation_id).strip() or not str(fingerprint).strip() or not str(key).strip():
            raise ValueError("operation_id, fingerprint, and idempotency_key are required")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        try:
            SideEffectClass(effect)
        except (TypeError, ValueError) as exc:
            raise ValueError("effect must be a known side-effect class") from exc


__all__ = [
    "ReconciliationResult", "ReconciliationState", "RetryCancelled",
    "RetryExecutionError", "RetryExecutionResult", "RetryTransport",
    "TransportFailure", "TransportRetryExecutor",
]
