"""Compatibility aliases for the packaged launcher-health status contract."""
from __future__ import annotations

from sonder_runtime.domain import launcher_health as _launcher_health

PATH = _launcher_health.PATH
TOKEN_ENV = _launcher_health.TOKEN_ENV
ROLE_ENV = _launcher_health.ROLE_ENV
MANAGED_ROLE = _launcher_health.MANAGED_ROLE
NONCE_HEADER = _launcher_health.NONCE_HEADER
MIN_TOKEN_LENGTH = _launcher_health.MIN_TOKEN_LENGTH
NONCE_BYTES = _launcher_health.NONCE_BYTES
IDENTITY = _launcher_health.IDENTITY
SERVICE = _launcher_health.SERVICE
VERSION = _launcher_health.VERSION

token_is_configured = _launcher_health.token_is_configured
new_nonce = _launcher_health.new_nonce
nonce_is_valid = _launcher_health.nonce_is_valid
request_path_matches = _launcher_health.request_path_matches
identity_payload = _launcher_health.identity_payload
_identity_is_valid = _launcher_health._identity_is_valid
canonical_message = _launcher_health.canonical_message
response_payload = _launcher_health.response_payload
payload_matches = _launcher_health.payload_matches
