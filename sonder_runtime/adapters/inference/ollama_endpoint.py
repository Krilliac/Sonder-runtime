"""Fail-closed Ollama endpoint parsing and transport policy."""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from pathlib import Path
import ssl
import threading
import weakref
import urllib.parse
import urllib.request

from sonder_runtime.domain import ollama_policy

logger = logging.getLogger(__name__)


DEFAULT_HOST = ollama_policy.DEFAULT_HOST
REMOTE_OPT_IN = ollama_policy.REMOTE_OPT_IN
_configured_endpoint: str | None = None
_configured_ca_bundle: str | None = None
_configuration_lock = threading.RLock()


# The staged live reloader explicitly preserves these guarded process-owned
# resources. Reloading this adapter must never revoke an external owner's fence.
if "_embedding_policy_lock" not in globals():
    _embedding_policy_lock = threading.RLock()
if "_external_membership_owners" not in globals():
    _external_membership_owners = weakref.WeakSet()
if "_embedding_policy_condition" not in globals():
    _embedding_policy_condition = threading.Condition(_embedding_policy_lock)
if "_embedding_operations" not in globals():
    _embedding_operations = {"active": 0, "pending": 0}
if "_embedding_operation_local" not in globals():
    _embedding_operation_local = threading.local()


@contextmanager
def _default_embedding_operation():
    """One read lease spans all default side effects; never lock across I/O.

    Nested provenance calls inherit the outer lease, even while registration
    is waiting. New operations are refused as soon as registration is pending.
    """
    depth = getattr(_embedding_operation_local, "depth", 0)
    if depth:
        _embedding_operation_local.depth = depth + 1
        try:
            yield True
        finally:
            _embedding_operation_local.depth = depth
        return
    with _embedding_policy_condition:
        allowed = not _external_membership_owners and not _embedding_operations["pending"]
        if allowed:
            _embedding_operations["active"] += 1
    if not allowed:
        yield False
        return
    _embedding_operation_local.depth = 1
    try:
        yield True
    finally:
        _embedding_operation_local.depth = 0
        with _embedding_policy_condition:
            _embedding_operations["active"] -= 1
            _embedding_policy_condition.notify_all()


def _restrict_for_external_membership(owner):
    """Deny the process-default adapter while an external source is owned.

    Sources retain this fence when closed, expired or revoked. Weak ownership
    permits an unrelated static-only application after every external owner
    has actually gone away; there is no caller-controlled enable switch.
    """
    if getattr(_embedding_operation_local, "depth", 0):
        raise RuntimeError("external membership cannot compose inside a default embedding operation")
    with _embedding_policy_condition:
        _embedding_operations["pending"] += 1
        _embedding_policy_condition.notify_all()
        try:
            if not _embedding_policy_condition.wait_for(lambda: not _embedding_operations["active"], timeout=5):
                raise RuntimeError("default embedding operations are still active")
            _external_membership_owners.add(owner)
        finally:
            _embedding_operations["pending"] -= 1
            _embedding_policy_condition.notify_all()


def _default_embeddings_disabled():
    with _embedding_policy_lock:
        return bool(_external_membership_owners)


def configure_typed_endpoint(value: str | None) -> None:
    """Make the validated typed Ollama URL authoritative for this process.

    ``None`` restores the compatibility fallback to ``OLLAMA_HOST``.  The
    setter is deliberately tiny: validation remains at the configuration
    boundary and this adapter only stores the already-typed startup choice.
    """
    logger.debug(f"configure_typed_endpoint: value={value!r}")
    logger.info(f"Ollama endpoint configured: {safe_display(value) if value is not None else 'reset to default'}")
    global _configured_endpoint
    with _configuration_lock:
        _configured_endpoint = None if value is None else str(value)


def reset_typed_endpoint() -> None:
    configure_typed_endpoint(None)


def configure_typed_ca_bundle(value: str | None) -> None:
    """Bind an optional absolute CA bundle for verified HTTPS Ollama calls."""
    global _configured_ca_bundle
    raw = str(value or "").strip()
    if not raw:
        with _configuration_lock:
            _configured_ca_bundle = None
        return
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Ollama CA bundle must be an existing absolute file")
    with _configuration_lock:
        _configured_ca_bundle = str(path)


def _ca_bundle() -> str | None:
    with _configuration_lock:
        configured = _configured_ca_bundle
    raw = configured or os.environ.get("SONDER_OLLAMA_CA_BUNDLE", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Ollama CA bundle must be an existing absolute file")
    return str(path)


def remote_allowed() -> bool:
    return ollama_policy.remote_allowed(os.environ)


def _remote_allowed_in(environment) -> bool:
    return ollama_policy.remote_allowed(environment)


def _candidate(value=None) -> str:
    with _configuration_lock:
        configured = _configured_endpoint
    if value is None and configured is not None:
        return ollama_policy._candidate(configured)
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
        logger.debug(f"configured_origin: policy error for {origin!r}: {error}")
        raise ValueError(error)
    logger.debug(f"configured_origin: resolved to {origin!r}")
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
        logger.warning(
            f"remote Ollama endpoint using insecure HTTP: "
            f"{safe_display(origin)}"
        )
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
    logger.debug(f"open_url: url={url!r}, timeout={timeout}")
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
    if urllib.parse.urlsplit(canonical_url).scheme == "https":
        bundle = _ca_bundle()
        if bundle:
            context = ssl.create_default_context(cafile=bundle)
            opener = urllib.request.build_opener(
                _PROXY_HANDLER, _NoRedirect(),
                urllib.request.HTTPSHandler(context=context),
            )
            return opener.open(canonical_request, timeout=timeout)
    return _OPENER.open(canonical_request, timeout=timeout)
