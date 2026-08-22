"""Pure unsafe-lab configuration policy.

This module deliberately accepts all inputs explicitly.  It owns no process
state, environment lookup, privilege probing, or audit I/O; those concerns
belong to the security adapter.
"""
from __future__ import annotations

import ipaddress


ACK_ENV = "SONDER_UNSAFE_LAB_ACK"
ACKNOWLEDGEMENT = (
    "I UNDERSTAND SONDER UNSAFE LAB MODE GIVES MODELS UNRESTRICTED HOST TOOL "
    "ACCESS AND I AM RUNNING IN A DISPOSABLE ISOLATED ENVIRONMENT"
)
_TRUE = frozenset({"1", "true", "yes", "on"})
_OLLAMA_REMOTE_OPT_IN = "SONDER_ALLOW_REMOTE_OLLAMA"
_OLLAMA_DEFAULT_HOST = "127.0.0.1:11434"


def is_loopback(host: str) -> bool:
    value = str(host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _ollama_policy_error(value=None) -> str:
    raw = str(_OLLAMA_DEFAULT_HOST if value is None else value).strip() or _OLLAMA_DEFAULT_HOST
    if "://" in raw:
        scheme, authority = raw.split("://", 1)
    else:
        scheme, authority = "http", raw
    scheme = scheme.lower()
    if scheme not in {"http", "https"}:
        return "OLLAMA_HOST must use http or https"
    if any(marker in authority for marker in ("/", "?", "#")):
        return "OLLAMA_HOST must be an origin without a path, query, or fragment"
    if "@" in authority:
        return "OLLAMA_HOST must not contain inline credentials"
    host = ""
    port_text = ""
    if authority.startswith("["):
        close = authority.find("]")
        if close < 0:
            return "OLLAMA_HOST is malformed or has an invalid port"
        host = authority[1:close]
        suffix = authority[close + 1:]
        if not suffix.startswith(":"):
            return "OLLAMA_HOST must include an explicit port"
        port_text = suffix[1:]
    elif authority.count(":") == 1:
        host, port_text = authority.rsplit(":", 1)
    else:
        # Bare IPv6 must be bracketed, and a host without a port is invalid.
        return "OLLAMA_HOST must include an explicit port"
    if not host:
        return "OLLAMA_HOST must include a hostname"
    if not port_text or not port_text.isdecimal():
        return "OLLAMA_HOST is malformed or has an invalid port"
    try:
        port = int(port_text)
    except ValueError:
        return "OLLAMA_HOST is malformed or has an invalid port"
    if not 1 <= port <= 65535:
        return "OLLAMA_HOST is malformed or has an invalid port"
    if host.casefold().rstrip(".") == "localhost":
        loopback = True
    else:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if loopback:
        return ""
    return (
        "non-loopback OLLAMA_HOST is blocked because prompts and embeddings "
        "would leave this machine; set %s=1 to opt in explicitly"
        % _OLLAMA_REMOTE_OPT_IN
    )


def validation_error(env, host=None) -> str:
    """Return the fail-closed activation error for explicit inputs."""
    if ACK_ENV not in env:
        return ""
    raw = str(env.get(ACK_ENV, ""))
    configured_host = str(
        host if host is not None else env.get("SONDER_HOST", "127.0.0.1")
    ).strip()
    if raw != ACKNOWLEDGEMENT:
        return (
            f"{ACK_ENV} does not exactly match the required acknowledgement; "
            "boolean, truthy, abbreviated, and whitespace-modified values are refused"
        )
    if not is_loopback(configured_host):
        return "unsafe lab mode refuses non-loopback SONDER_HOST exposure"
    if str(env.get("SONDER_ALLOW_CLOUD", "")).strip().lower() in _TRUE:
        return "unsafe lab mode refuses hosted/cloud model opt-in"
    value = str(env.get("OLLAMA_HOST", "127.0.0.1:11434")).strip()
    try:
        ollama_error = _ollama_policy_error(value)
    except Exception as exc:
        return "unsafe lab mode could not validate OLLAMA_HOST: %s" % exc
    if ollama_error:
        return "unsafe lab mode requires a loopback OLLAMA_HOST: %s" % ollama_error
    return ""
