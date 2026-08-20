"""Fallback orchestration for the standalone Sonder client."""

from __future__ import annotations

import urllib.error

from .client_endpoint import same_server
from sonder_runtime.platform.client_fallback import enabled as fallback_enabled


def send_prompt_with_fallback(
    server,
    api_key,
    prompt,
    fallback_server=None,
    *,
    sender,
    fallback_policy=fallback_enabled,
):
    """Try the hosted endpoint, then local Sonder on connection failure."""
    try:
        return sender(server, api_key, prompt), server, ""
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as first_error:
        if (
            not fallback_policy()
            or not fallback_server
            or same_server(server, fallback_server)
        ):
            raise
        reply = sender(fallback_server, "", prompt)
        warning = (
            "WARNING: hosted server %s was unreachable (%s). "
            "Fell back to local server %s for this request."
            % (server, first_error, fallback_server)
        )
        return reply, fallback_server, warning


__all__ = ["send_prompt_with_fallback"]
