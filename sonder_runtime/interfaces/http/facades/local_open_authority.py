"""Which HTTP callers hold no credential, and what restores the authority.

Under ``local-open`` (the loopback default, and ``api-key`` with no key on a
loopback bind, which serve.py downgrades to local-open) every local process
passes the developer and administrator checks with no secret at all. Routes
that grant autonomy -- raising the permission mode, approving a refused call
-- ask :func:`credential_less` and refuse such a caller; they still let it
lower the mode or withdraw an approval.

The check fails closed: a context that is not a mapping (``None`` included),
or whose ``mode`` is not one of the authenticated auth modes, counts as
credential-less. An account token sent to a local-open server does not
change that: the effective auth mode stays ``local-open``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The effective auth modes in which passing the authority check needed a
# credential (serve._auth_context's mode table, minus "local-open").
AUTHENTICATED_AUTH_MODES = frozenset({"api-key", "account", "both", "either"})

# The operator's way out, worded for an error message.
AUTHENTICATED_ADMIN_REMEDY = (
    "restart the server with SONDER_API_KEY set and send that key, or with "
    "SONDER_REQUIRE_ACCOUNT=1 and send an administrator account token (an "
    "account token sent to a local-open server does not count, because the "
    "server still runs in local-open mode)"
)


def credential_less(context: Any) -> bool:
    """True unless *context* is an authenticated (non-local-open) auth context.

    ``context`` is serve.py's request auth context (``_auth_context``), whose
    ``mode`` is the effective auth mode. Pure; safe from any thread.
    """
    if not isinstance(context, Mapping):
        return True
    return context.get("mode") not in AUTHENTICATED_AUTH_MODES


__all__ = ["AUTHENTICATED_ADMIN_REMEDY", "AUTHENTICATED_AUTH_MODES", "credential_less"]
