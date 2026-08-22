"""Domain contract for managed launcher health identity proofs."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets

PATH = "/v1/sonder/launcher-health"
TOKEN_ENV = "SONDER_LAUNCHER_HEALTH_TOKEN"
ROLE_ENV = "SONDER_RUNTIME_ROLE"
MANAGED_ROLE = "managed-host"
NONCE_HEADER = "X-Sonder-Launcher-Health-Nonce"
MIN_TOKEN_LENGTH = 32
NONCE_BYTES = 32
IDENTITY = "sonder-launcher-health-v3"
SERVICE = "sonder-serve"
VERSION = 3
_NONCE = re.compile(r"^[0-9a-f]{%d}$" % (NONCE_BYTES * 2))
_PROOF = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_KEYS = frozenset({"identity", "service", "version", "role", "pid", "port", "nonce", "proof"})

def token_is_configured(token: str) -> bool:
    return isinstance(token, str) and len(token) >= MIN_TOKEN_LENGTH

def new_nonce() -> str:
    return secrets.token_hex(NONCE_BYTES)

def nonce_is_valid(nonce: str) -> bool:
    return isinstance(nonce, str) and _NONCE.fullmatch(nonce) is not None

def request_path_matches(raw_path: str) -> bool:
    return isinstance(raw_path, str) and raw_path in {PATH, PATH + "/"}

def identity_payload(port: int, *, pid: int | None = None, role: str = MANAGED_ROLE) -> dict:
    return {"identity": IDENTITY, "service": SERVICE, "version": VERSION, "role": str(role), "pid": os.getpid() if pid is None else int(pid), "port": int(port)}

def _identity_is_valid(payload, *, port: int | None = None) -> bool:
    if not isinstance(payload, dict) or payload.get("identity") != IDENTITY or payload.get("service") != SERVICE or payload.get("version") != VERSION or payload.get("role") != MANAGED_ROLE:
        return False
    pid, payload_port = payload.get("pid"), payload.get("port")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(payload_port, int) or isinstance(payload_port, bool) or not 1 <= payload_port <= 65535:
        return False
    if port is None:
        return True
    try:
        expected_port = int(port)
    except (TypeError, ValueError):
        return False
    return not isinstance(port, bool) and payload_port == expected_port

def canonical_message(payload: dict) -> bytes:
    if not _identity_is_valid(payload) or not nonce_is_valid(payload.get("nonce")):
        raise ValueError("invalid launcher health proof fields")
    return ("contract=sonder-launcher-health-hmac-sha256\nidentity=%s\nservice=%s\nversion=%s\nrole=%s\npid=%s\nport=%s\nnonce=%s" % (payload["identity"], payload["service"], payload["version"], payload["role"], payload["pid"], payload["port"], payload["nonce"])).encode("ascii")

def response_payload(token: str, nonce: str, port: int, *, pid: int | None = None, role: str = MANAGED_ROLE) -> dict:
    if not token_is_configured(token):
        raise ValueError("launcher health proof token is not configured")
    if not nonce_is_valid(nonce):
        raise ValueError("invalid launcher health nonce")
    payload = {**identity_payload(port, pid=pid, role=role), "nonce": nonce}
    payload["proof"] = hmac.new(token.encode("utf-8"), canonical_message(payload), hashlib.sha256).hexdigest()
    return payload

def payload_matches(payload, *, token: str, nonce: str, port: int | None = None, role: str = MANAGED_ROLE) -> bool:
    if (not token_is_configured(token) or not nonce_is_valid(nonce) or not isinstance(payload, dict) or frozenset(payload) != _PAYLOAD_KEYS or not _identity_is_valid(payload, port=port) or payload.get("role") != role or not nonce_is_valid(payload.get("nonce")) or not hmac.compare_digest(payload["nonce"], nonce) or not isinstance(payload.get("proof"), str) or _PROOF.fullmatch(payload["proof"]) is None):
        return False
    try:
        expected = response_payload(token, nonce, payload["port"], pid=payload["pid"], role=payload["role"])["proof"]
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(payload["proof"], expected)
