"""Pure detection of repeated host-policy refusals in agent observations."""
from __future__ import annotations

from collections.abc import Iterable


_PREFIX = "ERROR: HOST POLICY:"


def _refusal_text(observation: object) -> str | None:
    """Return the stable refusal detail from a raw or wrapped observation."""
    text = str(observation or "").strip()
    if text.startswith(_PREFIX):
        payload = text[len(_PREFIX):].lstrip()
    else:
        lines = text.splitlines()
        if not lines:
            return None
        header = lines[0].strip()
        recognized = (
            header.startswith("step ") and " tool=" in header and " reason=" in header
        ) or header.startswith("host claim review ")
        if not recognized or len(lines) < 2:
            return None
        payload_line = lines[1].strip()
        if not payload_line.startswith(_PREFIX):
            return None
        payload = "\n".join(lines[1:])[len(_PREFIX):].lstrip()
    payload = payload.split("\nHOST RECOVERY:", 1)[0].rstrip()
    return payload or None


def repeated_policy_refusal(observations: Iterable[object]) -> str | None:
    """Return a repeated host-policy refusal after three identical calls.

    Observations may be the raw model error or the agent's wrapped
    ``step ...\n<model observation>`` record.  Tool names and surrounding
    recovery text are deliberately ignored; only the refusal detail is
    compared.  Fewer than three usable trailing observations never trigger.
    """
    trailing = list(observations)[-3:]
    if len(trailing) != 3:
        return None
    details = [_refusal_text(item) for item in trailing]
    if details[0] is None or any(detail != details[0] for detail in details[1:]):
        return None
    return details[0]


__all__ = ["repeated_policy_refusal"]
