"""``POST /v1/permission-mode`` under local-open: lowering only.

Under ``local-open`` (the loopback default, and ``api-key`` with no key on a
loopback bind) the endpoint's administrator check passes for every local
process, with no secret. Such a caller must not be able to raise the mode --
to ``auto``, where execution tools are ALLOW unattended, and then run host
programs through chat -- nor leave a raised mode saved for the next start.
It may still lower the mode, or return from ``plan`` to ``manual``. Every
other auth mode reached this point through ``_admin_authorized`` and keeps
the attended ``set_mode``. Who counts as credential-less, failing closed for
a missing or unknown context, is ``local_open_authority.credential_less``.

``policy`` is the permission-policy port (``set_mode``,
``set_mode_without_escalation``, ``mode_escalation_refused``) and
``envelope`` the lifecycle error-envelope builder; both arrive from serve.py,
like the ports ``approval_routes`` receives.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .local_open_authority import AUTHENTICATED_ADMIN_REMEDY, credential_less

logger = logging.getLogger(__name__)

ESCALATION_CODE = "PERMISSION_MODE_ESCALATION_REFUSED"


def mode_applier(policy: Any, context: Any, wanted: str) -> Callable[[], Any]:
    """The idempotent action body for this request's mode change.

    A refused raise is *returned* (the exception instance), not raised: an
    exception inside the idempotent action marks its receipt "uncertain",
    although nothing changed.
    """
    held_to_lowering = credential_less(context)
    refused_type = policy.mode_escalation_refused()

    def apply() -> Any:
        if not held_to_lowering:
            return policy.set_mode(wanted)
        try:
            return policy.set_mode_without_escalation(wanted)
        except refused_type as refused:
            return refused

    return apply


def send_escalation_refusal(handler: Any, policy: Any, result: Any,
                            envelope: Callable[..., dict[str, Any]]) -> bool:
    """Send the 403 when ``result`` is a refused raise; True when sent.

    ``handler`` is serve.py's request handler (its correlation id and JSON
    sender). The body is the standard error envelope plus ``mode``, the mode
    still in force.
    """
    if not isinstance(result, policy.mode_escalation_refused()):
        return False
    current = str(getattr(result, "current", ""))
    target = str(getattr(result, "target", ""))
    logger.warning(f"local-open permission-mode raise refused: {current!r} -> {target!r}")
    payload = envelope(
        ESCALATION_CODE,
        "raising the permission mode from %s to %s is refused: this server "
        "runs in local-open mode, so the request carries no credential and "
        "could come from any local process. Run /mode %s at the Sonder "
        "console, or %s. Lowering the mode, or returning from plan to "
        "manual, is allowed here." % (current, target, target, AUTHENTICATED_ADMIN_REMEDY),
        handler._correlation(),
        retryable=False,
    )
    payload["mode"] = current
    handler._send_json_payload(payload, status=403)
    return True


__all__ = ["ESCALATION_CODE", "mode_applier", "send_escalation_refusal"]
