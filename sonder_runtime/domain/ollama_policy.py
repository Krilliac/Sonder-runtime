"""Pure, fail-closed policy for configured Ollama origins.

This module deliberately contains only endpoint parsing and security policy.
It has no transport or adapter dependency, so platform configuration and the
unsafe-lab gate can share the same decision without creating a layer cycle.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import urllib.parse

logger = logging.getLogger(__name__)
_LOG_LOCK = threading.Lock()
_LOGGED_ORIGINS: set[tuple[str, str]] = set()
_MAX_LOGGED_ORIGINS = 256


def _first_acceptance_log(origin: str, kind: str) -> bool:
    """Emit acceptance INFO/WARNING logs at most once per canonical origin."""
    key = (origin, kind)
    with _LOG_LOCK:
        if key in _LOGGED_ORIGINS:
            return False
        if len(_LOGGED_ORIGINS) >= _MAX_LOGGED_ORIGINS:
            _LOGGED_ORIGINS.clear()
        _LOGGED_ORIGINS.add(key)
        return True


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


def _has_space_or_control(text: str) -> bool:
    return any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text)


def text_error(value=None) -> str:
    """Return why the configured text can never name an origin, or ``""``.

    Every gate (``policy_error``, worker validation) asks this about the text
    as configured, trimmed once at both ends like ``_candidate`` does.  The
    answer is the same before and after ``normalize``, which never removes an
    interior whitespace, control or backslash character.
    """
    raw = str(DEFAULT_HOST if value is None else value).strip()
    if "\\" in raw:
        return "must not contain a backslash"
    if _has_space_or_control(raw):
        return "is malformed or contains whitespace or control characters"
    return ""


def _trim_slashes(text: str) -> str:
    """Drop trailing "/" only, never past the first ``://``."""
    floor = text.find("://") + 3
    return text[:max(floor, len(text.rstrip("/")))]


def normalize(value=None) -> str:
    """Return a canonical origin and rewrite exact bind-all hosts to loopback.

    Only the two ends are trimmed: whitespace, then trailing "/".  Text holding
    whitespace or a control character anywhere else is returned as it is, so a
    later gate still sees it; other text that is not a recognisable origin is
    returned with its trailing "/" removed.  Every return value is a fixed
    point: ``normalize(normalize(x)) == normalize(x)``.
    """
    candidate = _candidate(value)
    if _has_space_or_control(candidate):
        # urlparse silently deletes tab/CR/LF and int() accepts padded ports;
        # canonicalising here would launder the text for whoever gates next.
        return candidate
    candidate = _trim_slashes(candidate)
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
        logger.warning(f"OLLAMA_HOST uses bind-all address 0.0.0.0, rewriting to loopback 127.0.0.1 -- check if this is intentional")
        replacement = "127.0.0.1"
    elif host == "::":
        logger.warning(f"OLLAMA_HOST uses IPv6 bind-all address ::, rewriting to loopback [::1] -- check if this is intentional")
        replacement = "[::1]"
    else:
        # All identity consumers share this spelling without resolving DNS.
        # DNS is case insensitive; IPv6 compression must happen before roster
        # uniqueness is checked, including equivalent bind-all literals.
        host = host.casefold().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            replacement = host
        else:
            if address.is_unspecified:
                address = ipaddress.ip_address("::1" if address.version == 6 else "127.0.0.1")
            replacement = "[%s]" % address.compressed if address.version == 6 else address.compressed
    suffix = ":%d" % port if port is not None else ""
    return "%s://%s%s" % (parsed.scheme.lower(), replacement, suffix)


def is_loopback(value=None) -> bool:
    try:
        host = urllib.parse.urlparse(normalize(value)).hostname
        if not host:
            return False
        if "%" in host:
            return False
        if host.casefold().rstrip(".") == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def policy_error(value=None, *, allow_remote=False) -> str:
    """Return a security error, or ``""`` for an accepted origin."""
    reason = text_error(value)
    if reason:
        return "OLLAMA_HOST " + reason
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
    if "%" in host:
        return "OLLAMA_HOST must not contain a zone id or percent escape"
    if parsed.username is not None or parsed.password is not None:
        logger.error("OLLAMA_HOST contains inline credentials, origin rejected for security")
        return "OLLAMA_HOST must not contain inline credentials"
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        return "OLLAMA_HOST must be an origin without a path, query, or fragment"
    # Logged only once the value is a bare origin: a password with an unencoded
    # "/" parses as host and port with the rest in the path, rejected above.
    logger.debug("policy_error: checking a bare origin, allow_remote=%s", allow_remote is True)
    if port is None:
        return "OLLAMA_HOST must include an explicit port"
    if is_loopback(origin):
        if _first_acceptance_log(origin, "loopback"):
            logger.info("ollama origin accepted (loopback)")
        else:
            logger.debug("ollama origin accepted (loopback)")
        logger.debug("policy_error: origin is loopback, accepted")
        return ""
    consent = allow_remote is True
    if not consent:
        logger.info("ollama origin rejected: non-loopback origin, remote access not enabled")
        logger.debug("policy_error: non-loopback origin blocked, remote not allowed")
        return (
            "non-loopback OLLAMA_HOST is blocked because prompts and embeddings "
            "would leave this machine; set %s=1 to opt in explicitly" % REMOTE_OPT_IN
        )
    if parsed.scheme.lower() != "https":
        logger.warning("non-loopback OLLAMA_HOST rejected: plain HTTP would expose prompts and embeddings in transit")
        return "non-loopback OLLAMA_HOST must use https to protect prompts and embeddings in transit"
    if _first_acceptance_log(origin, "remote"):
        logger.warning("remote Ollama origin accepted -- prompts and embeddings will leave this machine")
        logger.info("ollama origin accepted (remote, https)")
    else:
        logger.debug("remote Ollama origin accepted (cached)")
    return ""
