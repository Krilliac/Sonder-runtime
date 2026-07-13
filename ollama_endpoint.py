"""Fail-closed Ollama endpoint parsing and transport policy."""
from __future__ import annotations

import ipaddress
import os
import urllib.parse
import urllib.request


DEFAULT_HOST = "127.0.0.1:11434"
REMOTE_OPT_IN = "SONDER_ALLOW_REMOTE_OLLAMA"
_TRUE = {"1", "true", "yes", "on"}


def remote_allowed() -> bool:
    return os.environ.get(REMOTE_OPT_IN, "").strip().lower() in _TRUE


def _remote_allowed_in(environment) -> bool:
    return str((environment or {}).get(REMOTE_OPT_IN, "")).strip().lower() in _TRUE


def _candidate(value=None) -> str:
    raw = str(
        os.environ.get("OLLAMA_HOST", DEFAULT_HOST) if value is None else value
    ).strip()
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


def policy_error(value=None, *, allow_remote=None) -> str:
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
    consent = remote_allowed() if allow_remote is None else allow_remote is True
    if consent:
        return ""
    return (
        "non-loopback OLLAMA_HOST is blocked because prompts and embeddings "
        "would leave this machine; set %s=1 to opt in explicitly" % REMOTE_OPT_IN
    )


def configured_origin(value=None, *, allow_remote=None) -> str:
    origin = normalize(value)
    error = policy_error(origin, allow_remote=allow_remote)
    if error:
        raise ValueError(error)
    return origin


def client_environment(environment=None, *, allow_remote=None) -> dict:
    """Copy an environment and pin Ollama client traffic to canonical origin."""
    source = dict(os.environ if environment is None else environment)
    consent = (
        _remote_allowed_in(source) if allow_remote is None else allow_remote is True
    )
    source["OLLAMA_HOST"] = configured_origin(
        source.get("OLLAMA_HOST", DEFAULT_HOST),
        allow_remote=consent,
    )
    return source


def locality(value=None) -> str:
    origin = normalize(value)
    if policy_error(origin, allow_remote=True):
        return "invalid"
    if is_loopback(origin):
        return "loopback"
    return "remote-opt-in" if remote_allowed() else "remote-blocked"


def safe_display(value=None) -> str:
    origin = normalize(value)
    try:
        parsed = urllib.parse.urlparse(origin)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid Ollama endpoint>"
    if not host or parsed.scheme.lower() not in {"http", "https"}:
        return "<invalid Ollama endpoint>"
    rendered_host = "[%s]" % host if ":" in host else host
    suffix = ":%d" % port if port is not None else ""
    return "%s://%s%s" % (parsed.scheme.lower(), rendered_host, suffix)


def _origin_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Ollama request URL must not contain inline credentials")
    host = parsed.hostname
    if not host:
        return url
    rendered_host = "[%s]" % host if ":" in host else host
    suffix = ":%d" % parsed.port if parsed.port is not None else ""
    return "%s://%s%s" % (parsed.scheme.lower(), rendered_host, suffix)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PROXY_HANDLER = urllib.request.ProxyHandler({})
_OPENER = urllib.request.build_opener(_PROXY_HANDLER, _NoRedirect())


def open_url(request, timeout=30, *, allow_remote=None):
    """Open one Ollama request without environment proxies or redirects."""
    url = request.full_url if hasattr(request, "full_url") else str(request)
    parsed = urllib.parse.urlsplit(url)
    origin = configured_origin(
        _origin_from_url(url), allow_remote=allow_remote,
    )
    canonical_url = urllib.parse.urlunsplit((
        urllib.parse.urlsplit(origin).scheme,
        urllib.parse.urlsplit(origin).netloc,
        parsed.path,
        parsed.query,
        "",
    ))
    if hasattr(request, "full_url"):
        headers = dict(request.header_items())
        canonical_request = urllib.request.Request(
            canonical_url,
            data=request.data,
            headers=headers,
            method=request.get_method(),
        )
    else:
        canonical_request = canonical_url
    return _OPENER.open(canonical_request, timeout=timeout)
