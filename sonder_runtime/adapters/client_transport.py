"""HTTP transport for the standalone Sonder client."""

from __future__ import annotations

import json
import urllib.request

from .client_request import build_chat_request


def send_chat_prompt(server, api_key, prompt, *, request_builder=None):
    """Send one chat request and return the assistant content.

    ``request_builder`` is injectable so the root standalone-client delegate
    retains its historical request-construction seam for callers and tests.
    Network and JSON errors intentionally propagate unchanged to the caller.
    """
    builder = request_builder or build_chat_request
    url, headers, body = builder(server, api_key, prompt)
    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request) as response:
        raw = response.read().decode("utf-8")
    payload = json.loads(raw)
    return payload["choices"][0]["message"]["content"]


__all__ = ["send_chat_prompt"]
