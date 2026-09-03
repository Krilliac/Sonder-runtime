"""Identity key for agent model-escalation failure tracking."""

from __future__ import annotations

import hashlib


def escalation_key(tier, prompt) -> str:
    """Identity of one agent run for the failure note: its tier and prompt."""
    digest = hashlib.sha256(
        str(prompt or "").encode("utf-8", "replace")
    ).hexdigest()[:16]
    return "%s:%s" % (str(tier or "").strip().lower(), digest)
