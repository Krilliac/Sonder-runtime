"""Pure, fail-closed policy for configured Ollama origins.

This module deliberately contains only endpoint parsing and security policy.
It has no transport or adapter dependency, so platform configuration and the
unsafe-lab gate can share the same decision without creating a layer cycle.
"""
from __future__ import annotations

import ipaddress
import urllib.parse


DEFAULT_HOST = "127.0.0.1:11434"
REMOTE_OPT_IN = "SONDER_ALLOW_REMOTE_OLLAMA"
_TRUE = {"1", "true", "yes", "on"}


def remote_allowed(environment) -> bool:
    return str((environment or {}).get(REMOTE_OPT_IN, "")).strip().lower() in _TRUE


def _candidate(value=None) -> str:
    raw = str(DEFAULT_HOST if value is None else value).strip()
    if not raw:
        raw = DEFAULT_HOST
    return raw if "://" in raw else "http://%s" % raw


def normalize(value=None) -> str:
    """Return a canonical origin and rewrite exact bind-all hosts to loopback."""
    candidate = _candidate(value).rstrip("/")
    try:
        parsed = urllib.parse.urlparse(candidate)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return candidate
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return candidate
    if host.casefold().rstrip(".") == "localhost":
        replacement = "127.0.0.1"
    elif host == "0.0.0.0":
        replacement = "127.0.0.1"
    elif host == "::":
        replacement = "[::1]"
    else:
        return candidate
    suffix = ":%d" % port if port is not None else ""
    return "%s://%s%s" % (parsed.scheme.lower(), replacement, suffix)


def is_loopback(value=None) -> bool:
    try:
        host = urllib.parse.urlparse(normalize(value)).hostname
        if not host:
            return False
        if host.casefold().rstrip(".") == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def policy_error(value=None, *, allow_remote=False) -> str:
    """Return a security error, or ``""`` for an accepted origin."""
    origin = normalize(value)
    try:
        parsed = urllib.parse.urlparse(origin)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "OLLAMA_HOST is malformed or has an invalid port"
    if parsed.scheme.lower() not in {"http", "https"}:
        return "OLLAMA_HOST must use http or https"
    if not host:
        return "OLLAMA_HOST must include a hostname"
    if parsed.username is not None or parsed.password is not None:
        return "OLLAMA_HOST must not contain inline credentials"
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        return "OLLAMA_HOST must be an origin without a path, query, or fragment"
    if port is None:
        return "OLLAMA_HOST must include an explicit port"
    if is_loopback(origin):
        return ""
    consent = allow_remote is True
    if not consent:
        return (
            "non-loopback OLLAMA_HOST is blocked because prompts and embeddings "
            "would leave this machine; set %s=1 to opt in explicitly" % REMOTE_OPT_IN
        )
    if parsed.scheme.lower() != "https":
        return "non-loopback OLLAMA_HOST must use https to protect prompts and embeddings in transit"
    return ""
