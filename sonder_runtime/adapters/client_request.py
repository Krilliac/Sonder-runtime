"""Outbound request construction for the standalone Sonder client."""
from __future__ import annotations

import json
import urllib.parse

from sonder_runtime.platform.unsafe_lab_policy import is_loopback


class InsecureKeyTransportError(ValueError):
    """Raised instead of sending the API key over a channel that exposes it."""


def require_secure_key_transport(url):
    """Refuse to attach the bearer key to ``url`` unless the channel is safe.

    The key may travel over ``https://`` to any host, or over plaintext
    ``http://`` only to a loopback host. Every other scheme or host is
    refused before any network I/O, so a misconfigured ``SONDER_SERVER``
    cannot disclose the key to the network.
    """
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        host = parsed.hostname or ""
    except ValueError as exc:
        raise InsecureKeyTransportError(
            "refusing to send the API key: server URL is malformed (%s)" % exc
        ) from exc
    scheme = parsed.scheme.lower()
    if scheme == "https" and host:
        return
    if scheme == "http" and host and is_loopback(host):
        return
    raise InsecureKeyTransportError(
        "refusing to send the API key to %r: use an https:// server URL "
        "(plaintext http:// is allowed only for a loopback host)" % (url,)
    )


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
        require_secure_key_transport(url)
        headers["Authorization"] = "Bearer " + api_key
    return url, headers, body


__all__ = [
    "InsecureKeyTransportError",
    "build_chat_request",
    "require_secure_key_transport",
]
