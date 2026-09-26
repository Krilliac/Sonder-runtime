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

from .effect_journal import EffectJournalError, JournalBinding

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


class GatewayCallSequence:
    """Host-owned ordinal allocator for one child runner incarnation.

    ``issued`` is the number of ordinals allocated so far, starting from the
    value a resumed runner's checkpoint recorded.  Allocation is serialized,
    and an ordinal is consumed even when the journal then refuses the call,
    so a runner that re-issues its calls in order meets them again at the
    same ordinals.
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
        self._lock = Lock()

    @property
    def issued(self) -> int:
        with self._lock:
            return self._issued

    def allocate(self, binding: JournalBinding, *, tool_name: str,
                 arguments: Mapping[str, Any], effects: Iterable[str]) -> GatewayCallIdentity:
        """Allocate the next ordinal and derive the call's journal identity.

        The journal binding in force must be this child's run and worker;
        anything else is refused before an ordinal is consumed.
        """
        if not isinstance(binding, JournalBinding) or (
            binding.run_id, binding.worker_id,
        ) != (self.run_id, self.worker_id):
            raise EffectJournalError(
                "gateway call sequence does not belong to the bound journal run"
            )
        digest = gateway_request_digest(tool_name, arguments, effects)
        with self._lock:
            self._issued += 1
            ordinal = self._issued
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
    "GatewayCallSequence", "bound", "current", "gateway_call_operation_id",
    "gateway_request_digest", "parse_gateway_call_operation",
]
