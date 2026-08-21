from __future__ import annotations

from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.web_provider import LegacyWebProvider
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.web import EgressPolicy, WebPolicyError, WebRequest


class FakeResponse:
    def __init__(self, status=200, body=b"private test payload", headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _limit=-1):
        return self._body


class FakeTransport:
    USER_AGENT = "test-agent"

    def __init__(self, response):
        self.response = response
        self.requests = []

    def enabled(self):
        return True

    def _validated_public_target(self, url):
        return SimpleNamespace(), ("203.0.113.10",)

    def _urlopen(self, request, timeout=10):
        self.requests.append((request, timeout))
        return self.response

    @staticmethod
    def _decode_content_encoding(body, _encoding):
        return body


def _context(**kwargs):
    return local_owner_context(correlation_id="web-provider", **kwargs)


def _policy(**kwargs):
    return EgressPolicy(allowed_hosts=("public.example",), **kwargs)


def test_provider_applies_typed_policy_and_returns_response():
    transport = FakeTransport(FakeResponse(headers={"Content-Type": "text/plain"}))
    provider = LegacyWebProvider(transport)

    response = provider.request(
        WebRequest("https://public.example/data"), _policy(), _context(cloud_allowed=True)
    )

    assert response.status_code == 200
    assert response.body == b"private test payload"
    assert transport.requests[0][0].get_method() == "GET"


def test_provider_rechecks_redirect_policy_without_network():
    transport = FakeTransport(FakeResponse(status=302, headers={"Location": "https://other.example/"}))
    provider = LegacyWebProvider(transport)

    with pytest.raises(WebPolicyError, match="redirect target"):
        provider.request(
            WebRequest("https://public.example/start"),
            _policy(max_redirects=1),
            _context(cloud_allowed=True),
        )
    assert len(transport.requests) == 1


def test_provider_enforces_policy_byte_limit_and_consent():
    transport = FakeTransport(FakeResponse(body=b"12345"))
    provider = LegacyWebProvider(transport)

    with pytest.raises(WebPolicyError, match="byte limit"):
        provider.request(
            WebRequest("https://public.example/data"),
            _policy(max_response_bytes=4),
            _context(cloud_allowed=True),
        )
    with pytest.raises(WebPolicyError, match="egress policy"):
        provider.request(
            WebRequest("https://public.example/data"), _policy(), _context()
        )


def test_provider_health_contains_no_endpoint_or_secret():
    health = LegacyWebProvider(FakeTransport(FakeResponse())).health()
    assert health.status.value == "healthy"
    assert health.detail == "legacy pinned transport enabled"
