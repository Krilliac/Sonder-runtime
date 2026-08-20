"""WP3 SEAM-011: provider-neutral web and credential port tests."""
from datetime import datetime, timezone

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.web import (
    CredentialLease,
    CredentialRequest,
    CredentialScope,
    EgressPolicy,
    ProviderHealth,
    ProviderHealthSnapshot,
    WebPolicyError,
    WebRequest,
    redact,
)


def _context(*, cloud_allowed=False):
    return local_owner_context(correlation_id="seam-011", cloud_allowed=cloud_allowed)


def test_egress_policy_is_explicit_and_requires_consent():
    policy = EgressPolicy(("api.example.test",))
    assert policy.allows("https://api.example.test/v1", _context(cloud_allowed=True))
    assert not policy.allows("https://api.example.test/v1", _context())
    assert not policy.allows("https://other.example.test/v1", _context(cloud_allowed=True))
    with pytest.raises(WebPolicyError):
        EgressPolicy(("api.example.test",), schemes=("file",))


def test_request_rejects_credential_or_routing_ambiguity():
    with pytest.raises(WebPolicyError, match="credential-bearing"):
        WebRequest("https://api.example.test", headers={"Authorization": "secret"})
    with pytest.raises(WebPolicyError, match="userinfo"):
        WebRequest("https://user:secret@api.example.test/v1")


def test_credential_lease_scope_and_repr_redact_value():
    request = CredentialRequest("weather", "api.example.test")
    lease = CredentialLease(request.name, request.audience, request.scope, "top-secret")
    assert lease.scope is CredentialScope.REQUEST
    assert "top-secret" not in repr(lease)
    with pytest.raises(WebPolicyError):
        CredentialLease("weather", "api.example.test", CredentialScope.REQUEST, "")


def test_redact_replaces_all_known_secret_values():
    output = redact("Authorization=abc and cookie=abc", ("abc",))
    assert output == "Authorization=<redacted> and cookie=<redacted>"


def test_health_snapshot_is_safe_bounded_metadata():
    snapshot = ProviderHealthSnapshot(
        ProviderHealth.DEGRADED,
        datetime.now(timezone.utc),
        consecutive_failures=2,
        detail="timeout",
    )
    assert snapshot.status is ProviderHealth.DEGRADED
    with pytest.raises(ValueError, match="single line"):
        ProviderHealthSnapshot(ProviderHealth.UNAVAILABLE, datetime.now(timezone.utc), detail="secret\nurl")
