"""Bridge proposed playbooks into the existing operator approval inbox."""
from __future__ import annotations

import logging

from sonder_runtime.domain.memory.playbooks import content_digest
from sonder_runtime.platform.logging import Redactor


def record_pending(entry: dict) -> str:
    """Record a proposed entry as an approval-ledger pending item.

    The digest covers the complete entry content. Existing inbox approval is
    consumed by explicit list/reload reconciliation, which checks the same
    digest again while holding the playbook write lock.
    """
    digest = content_digest(entry)
    try:
        from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
        ApprovalLedger(redact=Redactor().redact).record_pending(
            "playbook_entry_approval", digest, surface="playbook",
            preview="%s/%s [%s]" % (
                entry.get("topic", ""), entry.get("id", ""), entry.get("title", ""),
            ),
        )
    except (OSError, ValueError, RuntimeError):
        logging.getLogger(__name__).warning("Playbook saved as proposed; approval inbox unavailable")
    return digest


def approve_pending(entry: dict, *, approver: str = "owner") -> str:
    """Issue the existing one-shot ledger approval for this exact entry."""
    digest = content_digest(entry)
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    ledger = ApprovalLedger()
    ledger.issue(
        "playbook_entry_approval", digest, approver=approver,
        surface="playbook", preview="approved %s/%s" % (entry.get("topic", ""), entry.get("id", "")),
    )
    ledger.consume("playbook_entry_approval", digest, surface="playbook-owner-review")
    return digest


def reconcile(store, ledger=None) -> int:
    """Apply matching owner approvals during explicit maintenance/review only."""
    if ledger is None:
        from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
        ledger = ApprovalLedger()
    entries = []
    for topic in store.list_topics():
        entries.extend(store.read(topic["topic"], approved_only=False))
    changed = 0
    for approval in ledger.approvals():
        if approval.tool != "playbook_entry_approval":
            continue
        entry = next((item for item in entries if content_digest(item) == approval.digest), None)
        if entry is None or entry.get("status") != "proposed":
            continue
        consumed = ledger.consume(approval.tool, approval.digest, surface="playbook")
        if consumed is None:
            continue
        try:
            store.review(entry["topic"], entry["id"], "approved", expected_digest=approval.digest)
        except (KeyError, ValueError, OSError):
            ledger.restore(consumed.nonce, consumed.digest)
            continue
        changed += 1
    return changed


__all__ = ["approve_pending", "content_digest", "record_pending", "reconcile"]
