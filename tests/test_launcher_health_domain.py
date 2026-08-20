import sonder_health
from sonder_runtime.domain import launcher_health

def test_root_health_exports_are_packaged_implementations():
    assert sonder_health.response_payload is launcher_health.response_payload
    assert sonder_health.payload_matches is launcher_health.payload_matches
    assert sonder_health.PATH == launcher_health.PATH

def test_packaged_health_contract_round_trips():
    token = "t" * launcher_health.MIN_TOKEN_LENGTH
    nonce = launcher_health.new_nonce()
    payload = launcher_health.response_payload(token, nonce, 11435, pid=123)
    assert launcher_health.payload_matches(payload, token=token, nonce=nonce, port=11435)
