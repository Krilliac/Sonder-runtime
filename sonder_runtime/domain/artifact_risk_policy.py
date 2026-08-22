"""Pure execution-risk policy decisions."""
from __future__ import annotations


def policy_denies(policy, risk):
    """Return whether *policy* denies an inspected artifact risk level."""
    if policy == "deny-high":
        return risk == "high"
    if policy == "deny-medium":
        return risk in {"high", "medium"}
    if policy == "deny-unknown":
        return risk in {"high", "medium", "unknown"}
    return False
