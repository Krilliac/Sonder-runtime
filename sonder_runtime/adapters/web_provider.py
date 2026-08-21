"""Concrete typed web provider backed by the legacy pinned transport.

This is an intentionally narrow migration boundary.  The application port
owns consent and egress policy; ``web_tools`` continues to own DNS
resolution, public-address validation, and pinned socket setup.  Credential
leases are not consumed here because ``WebProvider.request`` does not expose
a credential provider; credential-bearing requests fail closed instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib
import urllib.parse
import urllib.request

from ..application.context import OperationContext
from ..application.ports.web import (
    EgressPolicy,
    ProviderHealth,
    ProviderHealthSnapshot,
    WebPolicyError,
    WebRequest,
    WebResponse,
)


class LegacyWebProvider:
    """Typed ``WebProvider`` implementation over ``web_tools`` transport."""

    def __init__(self, transport=None) -> None:
        self._transport = transport or importlib.import_module("web_tools")

    def request(
        self,
        request: WebRequest,
        policy: EgressPolicy,
        context: OperationContext,
    ) -> WebResponse:
        if request.credential_name is not None:
            raise WebPolicyError(
                "credential_name requires a credential-aware provider boundary"
            )
        if not policy.allows(request.url, context):
            raise WebPolicyError("web request is outside the egress policy")
        if not self._transport.enabled():
            raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")

        current_url = request.url
        redirects = 0
        while True:
            if not policy.allows(current_url, context):
                raise WebPolicyError("redirect target is outside the egress policy")
            _parsed, addresses = self._transport._validated_public_target(current_url)
            outbound = urllib.request.Request(
                current_url,
                data=request.body or None,
                headers={
                    "User-Agent": self._transport.USER_AGENT,
                    "Accept-Encoding": "identity",
                    **dict(request.headers),
                },
                method=request.method,
            )
            outbound._sonder_addresses = addresses
            timeout = self._timeout(context)
            with self._transport._urlopen(outbound, timeout=timeout) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = getattr(response, "code", 200)
                if status in {301, 302, 303, 307, 308}:
                    if redirects >= policy.max_redirects:
                        raise WebPolicyError("too many redirects for egress policy")
                    location = response.headers.get("Location", "")
                    if not location:
                        raise WebPolicyError("redirect response has no Location header")
                    current_url = urllib.parse.urljoin(current_url, location)
                    redirects += 1
                    continue

                if request.method == "HEAD":
                    body = b""
                else:
                    body = response.read(policy.max_response_bytes + 1)
                    if len(body) > policy.max_response_bytes:
                        raise WebPolicyError("HTTP response exceeds egress byte limit")
                    body = self._transport._decode_content_encoding(
                        body, response.headers.get("Content-Encoding", "")
                    )
                    if len(body) > policy.max_response_bytes:
                        raise WebPolicyError("HTTP response exceeds egress byte limit")
                return WebResponse(
                    status_code=status,
                    headers=dict(response.headers.items()),
                    body=body,
                )

    @staticmethod
    def _timeout(context: OperationContext) -> float:
        remaining = context.remaining_seconds
        if remaining is not None and remaining <= 0:
            raise TimeoutError("web operation deadline expired")
        return min(10.0, remaining) if remaining is not None else 10.0

    def health(self) -> ProviderHealthSnapshot:
        enabled = self._transport.enabled()
        return ProviderHealthSnapshot(
            status=ProviderHealth.HEALTHY if enabled else ProviderHealth.UNAVAILABLE,
            checked_at=datetime.now(timezone.utc),
            detail="legacy pinned transport enabled" if enabled else "web tools disabled",
        )


__all__ = ["LegacyWebProvider"]
