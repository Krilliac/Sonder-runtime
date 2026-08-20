from datetime import datetime, timezone, timedelta

import pytest

from sonder_runtime.application.security.credential_broker import CredentialBroker
from sonder_runtime.domain.security.credential_egress import CredentialHandle, EgressDenied, EgressPolicy, RedirectChain


def broker():
    return CredentialBroker(EgressPolicy(allowed_hosts=("api.example.com", "*.example.com")))


def test_handle_is_opaque_and_host_protocol_scoped():
    service = broker()
    handle = service.issue(issuer="agent-1", secret="Bearer top-secret", hosts=("api.example.com",))
    use = service.authorize(handle, "https://api.example.com/v1")
    assert use.header_value == "Bearer top-secret"
    with pytest.raises(EgressDenied):
        service.authorize(handle, "https://other.example.com/v1")
    with pytest.raises(EgressDenied):
        service.authorize(handle, "http://api.example.com/v1")


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "https://10.0.0.4/x", "https://169.254.169.254/latest", "https://[::1]/x"])
def test_private_loopback_and_link_local_are_denied(url):
    with pytest.raises(EgressDenied):
        EgressPolicy().check(url)


def test_redirects_are_rechecked_and_cannot_widen_credential_scope():
    policy = EgressPolicy(allowed_hosts=("api.example.com", "redirect.example.com"))
    handle = CredentialHandle.mint("agent", ("api.example.com",))
    with pytest.raises(EgressDenied):
        RedirectChain(("https://api.example.com/start", "https://redirect.example.com/next")).validate(policy, handle)
    with pytest.raises(EgressDenied):
        RedirectChain(("https://api.example.com/start", "https://127.0.0.1/metadata")).validate(policy, handle)


def test_expiry_and_revocation_prevent_reuse():
    service = broker()
    expires = datetime.now(timezone.utc) + timedelta(seconds=1)
    handle = service.issue(issuer="agent", secret="s", hosts=("api.example.com",), expires_at=expires)
    assert service.authorize(handle, "https://api.example.com/x").header_value == "s"
    with pytest.raises(EgressDenied):
        service.authorize(handle, "https://api.example.com/x", now=expires)
    service.revoke(handle)
    with pytest.raises(EgressDenied):
        service.authorize(handle, "https://api.example.com/x")
