"""Focused production-boundary composition tests for SEC-001..009."""

from __future__ import annotations

from sonder_runtime.adapters.security.credential_provider import BrokerCredentialProvider
from sonder_runtime.adapters.web_provider import LegacyWebProvider
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.web import EgressPolicy, WebRequest
from sonder_runtime.application.security.credential_broker import CredentialBroker
from sonder_runtime.domain.security.credential_egress import EgressPolicy as DomainEgressPolicy


class _Response:
    status = 200

    def __init__(self) -> None:
        self.headers = {}

    def read(self, _limit: int) -> bytes:
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class _Transport:
    USER_AGENT = "test"

    def __init__(self) -> None:
        self.requests = []

    def enabled(self):
        return True

    def _validated_public_target(self, url):
        return None, ("203.0.113.10",)

    def _urlopen(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response()

    def _decode_content_encoding(self, body, _encoding):
        return body


def test_named_credential_is_authorized_at_the_concrete_web_boundary():
    broker = CredentialBroker(DomainEgressPolicy(allowed_hosts=("api.example.com",)))
    handle = broker.issue(
        issuer="test", secret="Bearer test-only", hosts=("api.example.com",)
    )
    provider = BrokerCredentialProvider(broker, {"api": handle})
    transport = _Transport()
    web = LegacyWebProvider(transport, credential_provider=provider)

    response = web.request(
        WebRequest("https://api.example.com/data", credential_name="api"),
        EgressPolicy(allowed_hosts=("api.example.com",)),
        local_owner_context(correlation_id="security-test", cloud_allowed=True),
    )

    assert response.body == b"ok"
    assert transport.requests[0][0].headers["Authorization"] == "Bearer test-only"


def test_credential_values_cannot_be_used_for_header_injection():
    broker = CredentialBroker(DomainEgressPolicy(allowed_hosts=("api.example.com",)))
    for secret in ("bad\nvalue", "bad\rvalue", "bad\x00value"):
        try:
            broker.issue(issuer="test", secret=secret, hosts=("api.example.com",))
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe credential value was accepted")
