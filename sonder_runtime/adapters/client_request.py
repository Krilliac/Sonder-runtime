"""Outbound request construction for the standalone Sonder client."""
from __future__ import annotations

import json


def build_chat_request(server, api_key, prompt):
    """Return the URL, headers, and encoded body for one chat request."""
    url = server.rstrip("/") + "/v1/chat/completions"
    body = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    return url, headers, body


__all__ = ["build_chat_request"]
