"""HTTP transport for the standalone Sonder client."""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request

from .client_request import build_chat_request, require_secure_key_transport
from .tls_contexts import https_handler

# Per socket operation (connect, send, each read) -- a server that stops
# talking raises ``TimeoutError`` after this long.
REQUEST_TIMEOUT_SECONDS = 120.0
# Whole exchange, so a body dripped out a byte at a time cannot keep the
# client blocked indefinitely.  Generous: a local model may be slow.
REQUEST_DEADLINE_SECONDS = 900.0
# A chat completion is one JSON object; a larger body is never buffered.
RESPONSE_BODY_LIMIT = 8 * 1024 * 1024
_READ_CHUNK = 65_536


class ClientResponseLimitError(RuntimeError):
    """The server's response broke the client's size or time ceiling."""


class _AuthenticatedRedirectRefusal(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect of a request that carries the bearer key.

    The stock handler copies ``Authorization`` onto the redirected request,
    so a redirect from the server, a proxy, or an on-path attacker would send
    the key to whatever origin and scheme ``Location`` names. Authenticated
    chat requests are therefore never redirected; unauthenticated requests
    keep the standard redirect behaviour because they carry no credential.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.has_header("Authorization"):
            if fp is not None:
                fp.close()
            reason = (
                "refusing to follow HTTP %d redirect to %s for a request that "
                "carries the API key; set SONDER_SERVER to the final https URL"
                % (code, newurl)
            )
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                reason,
                headers,
                io.BytesIO(reason.encode("utf-8")),
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(request):
    """Open ``request`` through an opener that never redirects the key.

    A keyed plaintext request (only ever to loopback, see
    :func:`require_secure_key_transport`) also bypasses every environment or
    system proxy: urllib has no implicit loopback bypass, so with
    ``http_proxy`` set and no matching ``no_proxy`` entry the Bearer header
    would travel in plaintext to the proxy host. Keyed https requests keep
    proxy support because CONNECT tunnels the header inside TLS.
    """
    handlers = [_AuthenticatedRedirectRefusal, https_handler()]
    if (
        request.has_header("Authorization")
        and request.type.lower() == "http"
    ):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)


def _read_limited(response, deadline):
    """Read the body up to ``RESPONSE_BODY_LIMIT`` bytes before ``deadline``.

    ``read1`` returns after one receive, so a slow drip is checked against
    the deadline between chunks; each receive is itself bounded by the
    socket timeout.  Reads at most ``limit + 1`` bytes so overflow is seen.
    """
    limit = RESPONSE_BODY_LIMIT
    read = getattr(response, "read1", None) or response.read
    chunks = []
    total = 0
    while total <= limit:
        if time.monotonic() > deadline:
            raise ClientResponseLimitError(
                "server response exceeded the %.0fs deadline"
                % REQUEST_DEADLINE_SECONDS
            )
        chunk = read(min(_READ_CHUNK, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        raise ClientResponseLimitError(
            "server response exceeds %d bytes; refusing to parse it" % limit
        )
    return b"".join(chunks)


def send_chat_prompt(server, api_key, prompt, *, request_builder=None):
    """Send one chat request and return the assistant content.

    ``request_builder`` is injectable so the root standalone-client delegate
    retains its historical request-construction seam for callers and tests.
    Whatever the builder returns, a request carrying ``Authorization`` is
    sent only over https or loopback http and is never redirected.
    Network and JSON errors intentionally propagate unchanged to the caller;
    a response over ``RESPONSE_BODY_LIMIT`` bytes or still arriving after
    ``REQUEST_DEADLINE_SECONDS`` raises :class:`ClientResponseLimitError`.
    """
    builder = request_builder or build_chat_request
    url, headers, body = builder(server, api_key, prompt)
    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST"
    )
    if request.has_header("Authorization"):
        require_secure_key_transport(request.full_url)
    deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
    with _open(request) as response:
        raw = _read_limited(response, deadline).decode("utf-8")
    payload = json.loads(raw)
    return payload["choices"][0]["message"]["content"]


__all__ = ["ClientResponseLimitError", "send_chat_prompt"]
