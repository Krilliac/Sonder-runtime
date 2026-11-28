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
_INFERENCE_REMOTE_OPT_IN = "SONDER_ALLOW_REMOTE_INFERENCE"
_INFERENCE_DEFAULT_HOST = "127.0.0.1:11437"
_OLLAMA_REMOTE_OPT_IN = "SONDER_ALLOW_REMOTE_OLLAMA"
_OLLAMA_DEFAULT_HOST = _INFERENCE_DEFAULT_HOST


def is_loopback(host: str) -> bool:
    value = str(host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _endpoint_policy_error(value=None, *, env_name: str = "SONDER_INFERENCE_BASE_URL", allow_path: bool = False) -> str:
    raw = str(_INFERENCE_DEFAULT_HOST if value is None else value).strip() or _INFERENCE_DEFAULT_HOST
    if "://" in raw:
        scheme, authority = raw.split("://", 1)
    else:
        scheme, authority = "http", raw
    scheme = scheme.lower()
    if scheme not in {"http", "https"}:
        return f"{env_name} must use http or https"
    if allow_path:
        if any(marker in authority for marker in ("?", "#")):
            return f"{env_name} must not contain a query or fragment"
        authority = authority.split("/", 1)[0]
    else:
        if authority.endswith("/") and not any(
            marker in authority[:-1] for marker in ("/", "?", "#")
        ):
            authority = authority[:-1]
        if any(marker in authority for marker in ("/", "?", "#")):
            return f"{env_name} must be an origin without a path, query, or fragment"
    if "@" in authority:
        return f"{env_name} must not contain inline credentials"
    host = ""
    port_text = ""
    if authority.startswith("["):
        close = authority.find("]")
        if close < 0:
            return f"{env_name} is malformed or has an invalid port"
        host = authority[1:close]
        suffix = authority[close + 1:]
        if not suffix.startswith(":"):
            return f"{env_name} must include an explicit port"
        port_text = suffix[1:]
    elif authority.count(":") == 1:
        host, port_text = authority.rsplit(":", 1)
    else:
        return f"{env_name} must include an explicit port"
    if not host:
        return f"{env_name} must include a hostname"
    if not port_text or not port_text.isdecimal():
        return f"{env_name} is malformed or has an invalid port"
    try:
        port = int(port_text)
    except ValueError:
        return f"{env_name} is malformed or has an invalid port"
    if not 1 <= port <= 65535:
        return f"{env_name} is malformed or has an invalid port"
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
        f"non-loopback {env_name} is blocked because prompts and embeddings "
        "would leave this machine; set %s=1 to opt in explicitly"
        % _INFERENCE_REMOTE_OPT_IN
    )


def _ollama_policy_error(value=None) -> str:
    return _endpoint_policy_error(value, env_name="OLLAMA_HOST", allow_path=False)


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
    if str(env.get("SONDER_INFERENCE_PRIVATE_WORKERS", "")).strip():
        return "unsafe lab mode refuses remote SONDER_INFERENCE_PRIVATE_WORKERS"
    si_value = str(env.get("SONDER_INFERENCE_BASE_URL", _INFERENCE_DEFAULT_HOST)).strip()
    try:
        si_error = _endpoint_policy_error(
            si_value, env_name="SONDER_INFERENCE_BASE_URL", allow_path=True,
        )
    except Exception as exc:
        return "unsafe lab mode could not validate SONDER_INFERENCE_BASE_URL: %s" % exc
    if si_error:
        return "unsafe lab mode requires a loopback SONDER_INFERENCE_BASE_URL: %s" % si_error
    legacy_ollama = str(env.get("OLLAMA_HOST", "")).strip()
    if legacy_ollama:
        try:
            ollama_error = _ollama_policy_error(legacy_ollama)
        except Exception as exc:
            return "unsafe lab mode could not validate OLLAMA_HOST: %s" % exc
        if ollama_error:
            return "unsafe lab mode requires a loopback OLLAMA_HOST: %s" % ollama_error
    return ""
