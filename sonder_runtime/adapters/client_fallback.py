"""Fallback orchestration for the standalone Sonder client."""

from __future__ import annotations

import urllib.error
from typing import Any, Callable

from .client_endpoint import same_server
from sonder_runtime.platform.client_fallback import enabled as fallback_enabled


def send_prompt_with_fallback(
    server: str,
    api_key: str,
    prompt: str,
    fallback_server: str | None = None,
    *,
    sender: Callable[[str, str, str], Any],
    fallback_policy: Callable[[], bool] = fallback_enabled,
) -> tuple[Any, str, str]:
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
