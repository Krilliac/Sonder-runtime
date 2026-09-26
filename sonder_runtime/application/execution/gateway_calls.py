"""Deterministic journal identities for typed gateway calls of a child runner.

Outside a child runner the typed tool gateway journals a mutating call under
the caller's ``request_id``, which is fresh per call.  A resumed child runner
re-issues the calls it made after its last checkpoint, so a fresh id would
never match the receipt that settled before the crash and the effect could
run twice.

While ``LocalSubagentProvider`` runs a journaled child it binds a
``GatewayCallSequence``: the child's durable identity (journal run, worker,
child session and settled dispatch attempt) plus a monotonic per-runner call
ordinal.  The gateway allocates one ordinal for every mutating call it is
about to journal and derives:

* ``operation_id``: ``gateway-call:{child}#dispatch-attempt-{N}#call-{K}``,
  so the journal intent id is fixed by the call's position alone;
* ``request_digest``: the canonical digest of the tool name, arguments and
  declared effects;
* ``idempotency_key``: the identity and ordinal together with that digest.

The ordinal is host state, not runner state.  ``JournalProvenanceStamp``
records the number of ordinals issued in every child checkpoint's
provenance, and a resumed runner restarts its sequence from the value its
validated checkpoint recorded.  The same call re-issued at the same ordinal
therefore carries the settled idempotency key and is refused with
``SettledEffectReplay`` before any journal write.  A different request at
that ordinal addresses the same intent id with a different key and is
refused with ``DivergentEffectReplay``; nothing runs.

An ordinal is consumed only when the journal durably admitted the call or
matched it to a settled receipt.  Any other admission failure (a divergent
replay, a transient journal error) halts the sequence: every later call of
that runner incarnation is refused, no checkpoint can be stamped from it,
and the provider fails the child as ``recovery_required`` even if the runner
swallowed the error.  A runner can therefore never move past a call the
journal did not account for, and a later retry can never land at an ordinal
that a resumed runner would address differently.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from .effect_journal import (
    EffectIntent,
    EffectJournalError,
    JournalBinding,
    SettledEffectReplay,
)

GATEWAY_CALL_CONTRACT = "gateway-call-v1"
_OPERATION_PREFIX = "gateway-call:"
_OPERATION = re.compile(
    r"gateway-call:(?P<child>.+)#dispatch-attempt-(?P<attempt>[1-9][0-9]*)"
    r"#call-(?P<ordinal>[1-9][0-9]*)"
)
_MAX_IDENTITY_TEXT = 512


@dataclass(frozen=True, slots=True)
class GatewayCallIdentity:
    """The journal identity of one gateway call at one ordinal."""

    ordinal: int
    operation_id: str
    idempotency_key: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class GatewayCallOperation:
    """A parsed ``gateway-call`` operation id."""

    child_id: str
    dispatch_attempt: int
    ordinal: int


def gateway_call_operation_id(child_id: str, dispatch_attempt: int, ordinal: int) -> str:
    return f"{_OPERATION_PREFIX}{child_id}#dispatch-attempt-{dispatch_attempt}#call-{ordinal}"


def parse_gateway_call_operation(operation_id: str) -> GatewayCallOperation | None:
    """Return the call identity an operation id names, or ``None``."""
    if not isinstance(operation_id, str):
        return None
    match = _OPERATION.fullmatch(operation_id)
    if match is None:
        return None
    return GatewayCallOperation(
        match["child"], int(match["attempt"]), int(match["ordinal"]),
    )


def gateway_request_digest(tool_name: str, arguments: Mapping[str, Any],
                           effects: Iterable[str]) -> str:
    """Canonical digest of what a gateway call asks the tool to do."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise EffectJournalError("gateway call requires a tool name")
    if not isinstance(arguments, Mapping):
        raise EffectJournalError("gateway call arguments must be a mapping")
    return hashlib.sha256(json.dumps(
        {
            "contract": GATEWAY_CALL_CONTRACT,
            "tool": tool_name,
            "arguments": dict(arguments),
            "effects": sorted(str(effect) for effect in effects),
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str,
    ).encode("utf-8")).hexdigest()


class GatewayCallSequenceHalted(EffectJournalError):
    """A gateway call was refused because an earlier admission failed.

    The runner incarnation that owns the sequence may not issue further
    effects; its child must fail and resume from its last checkpoint.
    """

    def __init__(self, cause: str) -> None:
        super().__init__(
            "gateway call sequence halted after an unaccounted admission failure "
            f"({cause}); the child must resume from its checkpoint"
        )
        self.cause = cause


class GatewayCallSequence:
    """Host-owned ordinal allocator for one child runner incarnation.

    ``issued`` is the number of ordinals consumed so far, starting from the
    value a resumed runner's checkpoint recorded.  ``admit`` holds the
    sequence lock across journal admission, so ordinals are assigned in
    admission order and a failed admission never leaves a gap: the ordinal
    is consumed only when the intent is durably admitted or the call is
    matched to its settled receipt (``SettledEffectReplay``).  Any other
    failure halts the sequence (see ``halted``) instead of handing the next
    call a different ordinal.
    """

    def __init__(self, *, run_id: str, worker_id: str, child_id: str,
                 dispatch_attempt: int, issued: int = 0) -> None:
        for name, value in (("run_id", run_id), ("worker_id", worker_id),
                            ("child_id", child_id)):
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > _MAX_IDENTITY_TEXT):
                raise EffectJournalError(f"gateway call {name} must be bounded text")
        if type(dispatch_attempt) is not int or dispatch_attempt < 1:
            raise EffectJournalError("gateway call dispatch attempt must be positive")
        if type(issued) is not int or issued < 0:
            raise EffectJournalError("gateway call ordinal cannot be negative")
        self.run_id, self.worker_id, self.child_id = run_id, worker_id, child_id
        self.dispatch_attempt = dispatch_attempt
        self._issued = issued
        self._halted: str | None = None
        self._lock = Lock()

    @property
    def issued(self) -> int:
        with self._lock:
            return self._issued

    @property
    def halted(self) -> str | None:
        """Why the sequence halted (the failing admission's error type), or ``None``."""
        with self._lock:
            return self._halted

    def identity(self, ordinal: int, *, tool_name: str, arguments: Mapping[str, Any],
                 effects: Iterable[str]) -> GatewayCallIdentity:
        """The journal identity of a request at ``ordinal``; pure, consumes nothing."""
        if type(ordinal) is not int or ordinal < 1:
            raise EffectJournalError("gateway call ordinal must be positive")
        digest = gateway_request_digest(tool_name, arguments, effects)
        return GatewayCallIdentity(
            ordinal,
            gateway_call_operation_id(self.child_id, self.dispatch_attempt, ordinal),
            json.dumps(
                ["gateway-call", self.run_id, self.worker_id, self.child_id,
                 self.dispatch_attempt, ordinal, digest],
                separators=(",", ":"), ensure_ascii=True,
            ),
            digest,
        )

    def admit(self, binding: JournalBinding, *, tool_name: str,
              arguments: Mapping[str, Any], effects: Iterable[str],
              reconciliation: str = "manual") -> EffectIntent:
        """Journal the call's intent at the next ordinal.

        The journal binding in force must be this child's run and worker, and
        the request must be well formed; either refusal happens before an
        ordinal is addressed and does not halt the sequence.  Then, under
        the sequence lock: ``SettledEffectReplay`` consumes the ordinal (the
        call is the one that settled there) and propagates; any other
        failure halts the sequence without consuming it and propagates;
        success consumes it and returns the admitted intent.
        """
        if not isinstance(binding, JournalBinding) or (
            binding.run_id, binding.worker_id,
        ) != (self.run_id, self.worker_id):
            raise EffectJournalError(
                "gateway call sequence does not belong to the bound journal run"
            )
        effects = tuple(effects)
        gateway_request_digest(tool_name, arguments, effects)
        with self._lock:
            if self._halted is not None:
                raise GatewayCallSequenceHalted(self._halted)
            ordinal = self._issued + 1
            try:
                call = self.identity(ordinal, tool_name=tool_name,
                                     arguments=arguments, effects=effects)
                intent = binding.begin_request(
                    operation_id=call.operation_id,
                    idempotency_key=call.idempotency_key,
                    request_digest=call.request_digest,
                    reconciliation=reconciliation,
                )
            except SettledEffectReplay:
                self._issued = ordinal
                raise
            except BaseException as error:
                # Durability of the failed admission is unknown to the
                # runner; moving on would hand its retry a new ordinal that
                # a resumed runner re-addresses at this one.
                self._halted = type(error).__name__
                raise
            self._issued = ordinal
            return intent


_CURRENT: contextvars.ContextVar[GatewayCallSequence | None] = contextvars.ContextVar(
    "sonder_gateway_call_sequence", default=None,
)


def current() -> GatewayCallSequence | None:
    """The sequence bound for the current child runner, if any."""
    return _CURRENT.get()


@contextlib.contextmanager
def bound(sequence: GatewayCallSequence) -> Iterator[GatewayCallSequence]:
    """Bind ``sequence`` for gateway calls made by the current runner thread."""
    if not isinstance(sequence, GatewayCallSequence):
        raise TypeError("sequence must be a GatewayCallSequence")
    token = _CURRENT.set(sequence)
    try:
        yield sequence
    finally:
        _CURRENT.reset(token)


__all__ = [
    "GATEWAY_CALL_CONTRACT", "GatewayCallIdentity", "GatewayCallOperation",
    "GatewayCallSequence", "GatewayCallSequenceHalted", "bound", "current",
    "gateway_call_operation_id", "gateway_request_digest", "parse_gateway_call_operation",
]
