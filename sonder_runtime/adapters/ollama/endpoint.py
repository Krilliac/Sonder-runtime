"""Fail-closed Ollama endpoint parsing and transport policy."""
from __future__ import annotations

import os
import urllib.parse
import urllib.request

from sonder_runtime.domain import ollama_policy


DEFAULT_HOST = ollama_policy.DEFAULT_HOST
REMOTE_OPT_IN = ollama_policy.REMOTE_OPT_IN


def remote_allowed() -> bool:
    return ollama_policy.remote_allowed(os.environ)


def _remote_allowed_in(environment) -> bool:
    return ollama_policy.remote_allowed(environment)


def _candidate(value=None) -> str:
    return ollama_policy._candidate(
        os.environ.get("OLLAMA_HOST", DEFAULT_HOST) if value is None else value
    )


def normalize(value=None) -> str:
    return ollama_policy.normalize(_candidate(value))


def is_loopback(value=None) -> bool:
    return ollama_policy.is_loopback(value)


def policy_error(value=None, *, allow_remote=None) -> str:
    consent = remote_allowed() if allow_remote is None else allow_remote is True
    return ollama_policy.policy_error(value, allow_remote=consent)


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
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return "invalid"
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return "invalid"
    try:
        if parsed.port is None:
            return "invalid"
    except ValueError:
        return "invalid"
    if is_loopback(origin):
        return "loopback"
    if parsed.scheme.lower() != "https":
        return "remote-insecure"
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
