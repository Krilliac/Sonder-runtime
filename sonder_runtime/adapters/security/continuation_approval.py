"""Private bridge from the actual permission gate to continuation receipts."""

import math
import json
import time
import uuid

from sonder_runtime.application.ports.lane_continuation import (
    GrantedApprovalEvidence, PendingApprovalEvidence, VerificationApprovalPending,
)
from .approval_ledger import Approval, PendingCall, PENDING_RETENTION_SECONDS


class ApprovalOutcomeUnknown(PermissionError):
    """A potentially consumed approval must not return to resumable pending."""


class _ObservedLedger:
    def __init__(self, ledger):
        self.ledger = ledger
        self.consumed = None
        self.pending = None
        self.consume_failed = False

    def consume(self, *args, **kwargs):
        try:
            self.consumed = self.ledger.consume(*args, **kwargs)
        except Exception:
            self.consume_failed = True
            raise
        return self.consumed

    def record_pending(self, *args, **kwargs):
        if self.consume_failed:
            raise ApprovalOutcomeUnknown("APPROVAL_OUTCOME_UNKNOWN")
        self.pending = self.ledger.record_pending(*args, **kwargs)
        return self.pending


class ContinuationApprovalBridge:
    def __init__(self, *, ledger, decide, digest_call):
        # Composition supplies the live fenced host gate, never model overrides.
        if ledger is None or not callable(decide) or not callable(digest_call):
            raise ValueError("trusted gate, ledger and digest function required")
        self._ledger, self._decide, self._digest = ledger, decide, digest_call

    def authorize(self, tool, arguments, *, surface, expires_at):
        if (type(expires_at) not in {int, float} or not math.isfinite(expires_at)
                or expires_at <= time.time()):
            raise PermissionError("original approval authority has expired")
        # Detached canonical bytes bind policy decisions to the same complete
        # payload even if the original caller mutates its argument dictionary.
        if not isinstance(arguments, dict):
            raise ValueError("exact approval arguments must be a mapping")
        encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > 65536:
            raise ValueError("exact approval arguments exceed bound")
        frozen_arguments = json.loads(encoded)
        digest = self._digest(tool, frozen_arguments)
        # Validate exact identity before a potentially consuming operation.
        PendingApprovalEvidence(tool, digest, surface, digest[:16], expires_at)
        pinned = self._ledger.pinned()
        observer = _ObservedLedger(pinned)
        decision = self._decide(tool, arguments=frozen_arguments, surface=surface,
                                approval_ledger=observer)
        if observer.consume_failed:
            raise ApprovalOutcomeUnknown("APPROVAL_OUTCOME_UNKNOWN")
        if (decision.tool != tool or decision.call_id != digest[:16]
                or self._digest(tool, frozen_arguments) != digest
                or time.time() >= expires_at):
            raise PermissionError("permission decision identity or deadline changed")
        if decision.allowed is True:
            if decision.source == "approval":
                receipt = observer.consumed
                if (not isinstance(receipt, Approval) or receipt.tool != tool
                        or receipt.digest != digest or receipt.consumed_surface != surface
                        or not receipt.spent or receipt.revoked
                        or receipt.expires_ts <= time.time()):
                    raise PermissionError("exact consumed approval evidence unavailable")
                confirmed = pinned.get(receipt.nonce)
                if confirmed != receipt:
                    raise PermissionError("consumed approval receipt changed")
                return GrantedApprovalEvidence(tool, digest, surface,
                    "approval:" + receipt.nonce, receipt.nonce,
                    min(expires_at, receipt.expires_ts), "approval")
            if decision.source not in {"rule", "mode"}:
                raise PermissionError("unsupported continuation policy allowance")
            return GrantedApprovalEvidence(tool, digest, surface,
                "policy:" + decision.source + ":" + uuid.uuid4().hex, "", expires_at, "policy")
        if decision.source == "unattended":
            pending = observer.pending
            if (isinstance(pending, PendingCall) and pending.tool == tool
                    and pending.digest == digest and pending.surface == surface):
                confirmed = pinned.resolve_call(digest)
                if (confirmed.tool != tool or confirmed.digest != digest
                        or confirmed.surface != surface
                        or confirmed.first_ts != pending.first_ts):
                    raise PermissionError("pending approval identity changed")
                deadline = min(expires_at, pending.first_ts + PENDING_RETENTION_SECONDS)
                if deadline > time.time():
                    raise VerificationApprovalPending(PendingApprovalEvidence(
                        tool, digest, surface, digest[:16], deadline))
        raise PermissionError("exact continuation permission was refused")
