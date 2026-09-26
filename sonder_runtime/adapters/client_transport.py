"""HTTP transport for the standalone Sonder client."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

from .client_request import build_chat_request, require_secure_key_transport


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
    """Open ``request`` through an opener that never redirects the key."""
    opener = urllib.request.build_opener(_AuthenticatedRedirectRefusal)
    return opener.open(request)


def send_chat_prompt(server, api_key, prompt, *, request_builder=None):
    """Send one chat request and return the assistant content.

    ``request_builder`` is injectable so the root standalone-client delegate
    retains its historical request-construction seam for callers and tests.
    Whatever the builder returns, a request carrying ``Authorization`` is
    sent only over https or loopback http and is never redirected.
    Network and JSON errors intentionally propagate unchanged to the caller.
    """
    builder = request_builder or build_chat_request
    url, headers, body = builder(server, api_key, prompt)
    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST"
    )
    if request.has_header("Authorization"):
        require_secure_key_transport(request.full_url)
    with _open(request) as response:
        raw = response.read().decode("utf-8")
    payload = json.loads(raw)
    return payload["choices"][0]["message"]["content"]


__all__ = ["send_chat_prompt"]
