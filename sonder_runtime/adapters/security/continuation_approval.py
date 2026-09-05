"""Private bridge from the actual permission gate to continuation receipts."""

import math
import time
import uuid

from sonder_runtime.application.ports.lane_continuation import (
    GrantedApprovalEvidence, PendingApprovalEvidence, VerificationApprovalPending,
)
from .approval_ledger import Approval, PendingCall, PENDING_RETENTION_SECONDS


class _ObservedLedger:
    def __init__(self, ledger):
        self.ledger = ledger
        self.consumed = None
        self.pending = None

    def consume(self, *args, **kwargs):
        self.consumed = self.ledger.consume(*args, **kwargs)
        return self.consumed

    def record_pending(self, *args, **kwargs):
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
        digest = self._digest(tool, arguments)
        # Validate exact identity before a potentially consuming operation.
        PendingApprovalEvidence(tool, digest, surface, digest[:16], expires_at)
        observer = _ObservedLedger(self._ledger)
        decision = self._decide(tool, arguments=arguments, surface=surface,
                                approval_ledger=observer)
        if (decision.tool != tool or decision.call_id != digest[:16]
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
                confirmed = self._ledger.get(receipt.nonce)
                if confirmed != receipt:
                    raise PermissionError("consumed approval receipt changed")
                return GrantedApprovalEvidence(tool, digest, surface,
                    "approval:" + receipt.nonce, receipt.nonce,
                    min(expires_at, receipt.expires_ts), "approval")
            if decision.source not in {"rule", "mode"}:
                raise PermissionError("unsupported continuation policy allowance")
            return GrantedApprovalEvidence(tool, digest, surface,
                "policy:" + uuid.uuid4().hex, "", expires_at, "policy")
        if decision.source == "unattended":
            pending = observer.pending
            if (isinstance(pending, PendingCall) and pending.tool == tool
                    and pending.digest == digest and pending.surface == surface):
                confirmed = self._ledger.resolve_call(digest)
                if (confirmed.tool != tool or confirmed.digest != digest
                        or confirmed.surface != surface
                        or confirmed.first_ts != pending.first_ts):
                    raise PermissionError("pending approval identity changed")
                deadline = min(expires_at, pending.first_ts + PENDING_RETENTION_SECONDS)
                if deadline > time.time():
                    raise VerificationApprovalPending(PendingApprovalEvidence(
                        tool, digest, surface, digest[:16], deadline))
        raise PermissionError("exact continuation permission was refused")
