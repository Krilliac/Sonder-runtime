import sonder_health
from sonder_runtime.domain import launcher_health


def test_root_health_module_preserves_packaged_status_aliases():
    for name in (
        "token_is_configured", "new_nonce", "nonce_is_valid",
        "request_path_matches", "identity_payload", "_identity_is_valid",
        "canonical_message", "response_payload", "payload_matches",
    ):
        assert getattr(sonder_health, name) is getattr(launcher_health, name)

    for name in (
        "PATH", "TOKEN_ENV", "ROLE_ENV", "MANAGED_ROLE", "NONCE_HEADER",
        "MIN_TOKEN_LENGTH", "NONCE_BYTES", "IDENTITY", "SERVICE", "VERSION",
    ):
        assert getattr(sonder_health, name) == getattr(launcher_health, name)


def test_packaged_health_status_preserves_nonce_bound_proof_contract():
    token = "t" * launcher_health.MIN_TOKEN_LENGTH
    nonce = "a" * (launcher_health.NONCE_BYTES * 2)
    payload = launcher_health.response_payload(token, nonce, 11435, pid=123)

    assert launcher_health.payload_matches(
        payload, token=token, nonce=nonce, port=11435
    )
    assert not launcher_health.payload_matches(
        payload, token=token, nonce="b" * len(nonce), port=11435
    )
