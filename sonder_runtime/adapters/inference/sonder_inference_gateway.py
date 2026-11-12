"""Sonder Inference (``sonder-infer serve``) as a ModelGateway provider.

Sonder Inference serves an OpenAI-compatible subset plus Sonder extensions
over local HTTP (Inference ADR-020, API version 1).  This adapter reuses the
:class:`OpenAICompatibleGateway` transport -- one HTTP client, one error
taxonomy -- and adds what that generic peer cannot know:

* **Discovery.** ``SONDER_INFERENCE_BASE_URL``, else the ``url`` field of a
  ``serve --ready-file`` named by ``SONDER_INFERENCE_READY_FILE``, else
  ``http://127.0.0.1:11437``.  All configuration is read lazily per call.
* **Consent.** Loopback needs nothing.  A non-loopback endpoint needs
  ``SONDER_ALLOW_REMOTE_INFERENCE=1``, ``https://`` and an API key, *and* an
  OperationContext that allows prompts to leave the machine; anything else is
  refused before a byte is sent.  ``0.0.0.0``/``::`` are bind addresses, so
  they are rewritten to the matching loopback address (Inference's Host check
  rejects them otherwise).
* **Pre-send classification.** :class:`SonderInferenceUnreachable` is raised
  only when the request provably did not execute: connection refused, an
  unresolvable host, cached health that is not ready, or HTTP 503 with code
  ``not_ready``.  It keeps the ``DEPENDENCY_UNAVAILABLE`` code so session
  capture records it like every other provider outage.  Timeouts, 4xx, 500
  and 503 ``backend_unavailable`` are never "unreachable": the request may
  have run and must not be replayed elsewhere.
* **Transport.** Requests go straight to the configured endpoint: HTTP(S)
  proxy settings from the environment or the OS are ignored, redirects are
  never followed (a 3xx is an error), and every exchange -- connect, headers
  and body -- is bounded by one wall-clock budget rather than a per-socket
  timeout, so a peer that trickles bytes cannot hold a probe or a turn open.
* **Version.** The API major version must be 1, read from the health
  document and from the ``sonder.api_version`` body field when present (the
  shared transport cannot read response headers).
* **Health, identity and status** for doctor, the ecosystem route and the
  Flutter card: :meth:`capability_health`, :meth:`backend_identity` and
  :meth:`provider_status`.  None of them generates.

Embeddings are not served by Inference v1; :meth:`embed` tells the operator
to bind ``SONDER_EMBEDDING_PROVIDER=ollama``.
"""
from __future__ import annotations

import functools
import http.client
import io
import ipaddress
import json
import logging
import math
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Sequence
from urllib.parse import quote, urlsplit, urlunsplit

from ...application.chat import stream_sink
from ...application.context import OperationContext, current_operation_context
from ...application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    optional_token_count,
    require_model_text,
)
from ...application.ports.model_gateway_contract import Capability, CapabilityHealth
from ...domain.common.errors import (
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    SonderError,
)
from ...domain.model_capabilities import (
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_FIXED_ENDPOINT,
)
from ...domain.routing.backend_conformance import BackendIdentity
from ...platform.metrics import default_registry
from ..model_request_admission import HostModelRequestAdmission
from ..observability import activity_tracker
from ..provider_bindings import PROVIDER_TIERS
from .openai_compat_gateway import (
    ERROR_BODY_LIMIT,
    GET_BODY_LIMIT,
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from .request_tuning import thinking_supported, tune_request
from .sse_stream import post_streaming
from .telemetry import from_openai_compatible

logger = logging.getLogger(__name__)

PROVIDER_ID = "sonder_inference"
PROVIDER_LABEL = "sonder-inference"
API_VERSION = 1
IDENTITY_SCHEMA = "sonder.inference.identity/1"
DEFAULT_BASE_URL = "http://127.0.0.1:11437"
DEFAULT_MODEL = "default"
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_HEALTH_TTL_SECONDS = 5.0
HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_HEALTH_STALE_SECONDS = 120.0
BUSY_RETRY_SECONDS = 5.0
READY_FILE_LIMIT = 65_536
DETAIL_LIMIT = 240
# A non-streaming chat completion is one JSON object; anything larger than
# this is not a response this runtime will buffer.
RESPONSE_BODY_LIMIT = 16 * 1024 * 1024
_READ_CHUNK = 65_536
# The only host names Inference's Host check accepts on a loopback bind
# (contract 2.3); any other loopback alias would be refused with 403.
ACCEPTED_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

ENV_BASE_URL = "SONDER_INFERENCE_BASE_URL"
ENV_MODEL = "SONDER_INFERENCE_MODEL"
ENV_TIER_MODELS = "SONDER_INFERENCE_TIER_MODELS"
ENV_API_KEY = "SONDER_INFERENCE_API_KEY"
ENV_ALLOW_REMOTE = "SONDER_ALLOW_REMOTE_INFERENCE"
ENV_READY_FILE = "SONDER_INFERENCE_READY_FILE"
ENV_TIMEOUT = "SONDER_INFERENCE_TIMEOUT_SECONDS"
ENV_HEALTH_TTL = "SONDER_INFERENCE_HEALTH_TTL_SECONDS"
ENV_HEALTH_TIMEOUT = "SONDER_INFERENCE_HEALTH_TIMEOUT_SECONDS"
ENV_HEALTH_STALE = "SONDER_INFERENCE_HEALTH_STALE_SECONDS"
ENV_FALLBACK = "SONDER_INFERENCE_FALLBACK"
ENV_PRIVATE_WORKERS = "SONDER_INFERENCE_PRIVATE_WORKERS"
ENV_MAX_INFLIGHT = "SONDER_INFERENCE_MAX_INFLIGHT"
LATENCY_EWMA_ALPHA = 0.3
MAX_PRIVATE_WORKERS = 8
MAX_WORKER_INFLIGHT = 16
MIN_WORKER_TOKEN_LENGTH = 16
PRIVATE_WORKERS_LIMIT = 16_384
_TOKEN_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
_WORKER_KEYS = frozenset({"url", "ca_bundle", "token_env", "max_inflight"})

# Pinned by the ecosystem contract (section 3.4): correlation ids outside this
# alphabet are omitted rather than rewritten, because Inference rejects them.
CORRELATION_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
WORKLOAD_BY_SOURCE = MappingProxyType({
    "http": "interactive_user",
    "repl": "interactive_user",
    "mcp": "owner_orchestrator",
    "worker": "implementation_worker",
    "system": "maintenance",
})
TELEMETRY_PATHS = MappingProxyType({
    "discovery_url": "/.well-known/sonder-telemetry",
    "sse_url": "/v1/telemetry/sse",
    "ndjson_url": "/v1/telemetry/ndjson",
})
CAPABILITIES = frozenset({GATEWAY_CAPABILITY_CHAT, GATEWAY_CAPABILITY_FIXED_ENDPOINT})
STATUS_KEYS = (
    "provider", "state", "healthy", "checked_at", "detail", "capabilities",
    "base_url", "version", "api_version", "models", "synthetic", "identity",
    "telemetry", "fallback", "fallback_count", "tier_models",
    "busy", "workers",
)

_BIND_ADDRESS_REWRITES = {"0.0.0.0": "127.0.0.1", "::": "::1"}
_MODEL_ID = re.compile(r"[^\s]{1,128}\Z")
# Ollama option name -> OpenAI/Sonder request field.  Only options that the
# caller actually set are forwarded; Inference applies its own defaults.
_FORWARDED_OPTIONS = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "typical_p": "typical_p",
    "seed": "seed",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "repeat_penalty": "repeat_penalty",
    "repeat_last_n": "repeat_last_n",
    "num_ctx": "num_ctx",
    "num_predict": "max_tokens",
}
_INTEGER_OPTIONS = frozenset({"top_k", "seed", "repeat_last_n", "num_ctx", "num_predict"})
# Requests Inference v1 rejects with 400 unsupported_parameter.  Refusing them
# here keeps the failure local, loud and free of a wasted network round trip.
_UNSUPPORTED_OPTIONS = ("format", "tools", "tool_choice", "functions", "response_format")


class SonderInferenceUnreachable(DependencyUnavailable):
    """Sonder Inference provably did not execute the request.

    Raised only for: connection refused, an unresolvable host, cached health
    that is not ready, or HTTP 503 ``not_ready``.  It is the sole trigger for
    the fail-closed Ollama fallback, and keeps ``DEPENDENCY_UNAVAILABLE`` as
    its code so evidence capture treats it as an ordinary provider outage.

    ``reason`` is the short cause ("connection refused") and ``summary`` the
    one-line operator statement without remediation; ``str()`` is the full
    message with the fix.
    """

    kind = "provider_unavailable"

    def __init__(self, message: str, *, reason: str | None = None,
                 summary: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or message
        self.summary = summary or message


class SonderInferenceBusyTimeout(DependencyUnavailable):
    """A backend read timeout; retry here once, never on another provider."""

    kind = "busy_timeout"


@dataclass(frozen=True)
class SonderInferenceResponse(ModelResponse):
    """Additive provider measurements without changing other gateways' DTOs."""

    timings: Mapping[str, int | float] | None = None
    finish_reason: str | None = None
    # Scheme and host of the endpoint that served it, when private workers
    # are configured (never a path, query or credential).
    endpoint: str | None = None


# -- transport ---------------------------------------------------------------
#
# One exchange = one request and its bounded response.  The stdlib urllib
# stack is kept (so HTTP errors keep the shape the shared gateway maps), but
# built per exchange without ProxyHandler input from the environment, without
# redirect following, and with a watchdog that shuts the socket down when the
# exchange's wall-clock budget is spent.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _ExchangeBudget:
    """Shut down an exchange's sockets once its total budget is spent."""

    def __init__(self, seconds: float) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self.expired = False
        self._timer = threading.Timer(max(0.0, float(seconds)), self._expire)
        self._timer.daemon = True

    def __enter__(self) -> "_ExchangeBudget":
        self._timer.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._timer.cancel()

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.append(sock)
            expired = self.expired
        if expired:
            _shutdown(sock)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _BudgetedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, budget: _ExchangeBudget, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._budget = budget

    def connect(self) -> None:
        super().connect()
        self._budget.attach(self.sock)


class _BudgetedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, budget: _ExchangeBudget, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._budget = budget

    def connect(self) -> None:
        super().connect()
        self._budget.attach(self.sock)


class _BudgetedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, budget: _ExchangeBudget) -> None:
        super().__init__()
        self._budget = budget

    def http_open(self, req):
        return self.do_open(
            functools.partial(_BudgetedHTTPConnection, budget=self._budget), req,
        )


_TRUST = threading.local()


@contextmanager
def trust_scope(ca_bundle: str):
    """Verify HTTPS exchanges on this thread against exactly ``ca_bundle``.

    A private worker names its own CA bundle; inside this scope the bundle is
    the only trust anchor (the system store is not consulted), so a private
    worker's URL cannot be satisfied by a publicly issued certificate.  An
    empty value keeps the system trust store.  Scopes nest and always restore.
    """
    previous = getattr(_TRUST, "ca_bundle", "")
    _TRUST.ca_bundle = ca_bundle or ""
    try:
        yield
    finally:
        _TRUST.ca_bundle = previous


def _tls_context() -> ssl.SSLContext:
    ca_bundle = getattr(_TRUST, "ca_bundle", "")
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    return ssl.create_default_context()


class _BudgetedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, budget: _ExchangeBudget) -> None:
        super().__init__(context=_tls_context())
        self._budget = budget

    def https_open(self, req):
        return self.do_open(
            functools.partial(_BudgetedHTTPSConnection, budget=self._budget), req,
            context=self._context,
        )


def _read_bounded(stream, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes so an oversize body is detectable."""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = stream.read(min(_READ_CHUNK, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _exchange(
    method: str, url: str, headers: Mapping[str, str], data: bytes | None,
    timeout: float, limit: int,
) -> tuple[int, bytes, Mapping[str, str] | None]:
    """Run one direct, non-redirected, wall-clock-bounded HTTP exchange.

    Returns ``(status, body, None)`` for a 2xx response and
    ``(status, error body, headers)`` otherwise.  Raises ``TimeoutError`` when
    the budget is spent, ``DependencyUnavailable`` for a malformed or
    oversized HTTP response, and the usual ``URLError``/``OSError`` for
    transport failures before a response (refused, unresolvable, ...).
    """
    budget_seconds = float(timeout) if timeout else DEFAULT_TIMEOUT_SECONDS
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    with _ExchangeBudget(budget_seconds) as budget:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            _BudgetedHTTPHandler(budget), _BudgetedHTTPSHandler(budget),
        )
        try:
            try:
                with opener.open(request, timeout=budget_seconds) as response:
                    status = int(response.status)
                    body = _read_bounded(response, limit)
                    error_headers = None
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                try:
                    body = _read_bounded(exc, ERROR_BODY_LIMIT)
                finally:
                    exc.close()
                error_headers = dict(exc.headers or {})
        except Exception as exc:
            if budget.expired:
                raise TimeoutError(
                    "sonder-inference exchange exceeded its %.1fs budget" % budget_seconds
                ) from exc
            if isinstance(exc, http.client.HTTPException):
                raise DependencyUnavailable(
                    "sonder-inference sent a malformed HTTP response (%s)"
                    % type(exc).__name__
                ) from exc
            raise
        if budget.expired:
            raise TimeoutError(
                "sonder-inference exchange exceeded its %.1fs budget" % budget_seconds
            )
    if error_headers is None and len(body) > limit:
        raise DependencyUnavailable("sonder-inference response exceeds %d bytes" % limit)
    return status, body, error_headers


def direct_get_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """GET seam: direct, non-redirected, bounded; non-2xx is returned."""
    status, body, _headers = _exchange("GET", url, headers, None, timeout, GET_BODY_LIMIT)
    return status, body


def direct_post_transport(url: str, payload: dict, headers: dict, timeout) -> dict:
    """POST seam: like :func:`direct_get_transport`, but non-2xx raises
    ``HTTPError`` (carrying only the bounded error body) for the shared
    gateway's error mapping.  ``"stream": true`` reads the event stream
    (:mod:`.sse_stream`) and returns the same aggregated document."""
    if payload.get("stream") is True:
        return post_streaming(
            url, payload, headers, timeout, stream_sink.call_stream(),
            exchange=sys.modules[__name__],
        )
    data = json.dumps(payload).encode("utf-8")
    status, body, error_headers = _exchange(
        "POST", url, headers, data, timeout, RESPONSE_BODY_LIMIT,
    )
    if error_headers is not None:
        raise urllib.error.HTTPError(url, status, "HTTP %d" % status, error_headers, io.BytesIO(body))
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise DependencyUnavailable("sonder-inference returned a non-JSON response") from exc


@dataclass(frozen=True)
class SonderInferenceConfig:
    """Resolved provider settings; build with :func:`config_from_env`."""

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    tier_models: Mapping[str, str] = field(default_factory=dict)
    api_key: str = ""
    allow_remote: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    health_ttl_seconds: float = DEFAULT_HEALTH_TTL_SECONDS
    base_url_source: str = "default"
    health_timeout_seconds: float = HEALTH_PROBE_TIMEOUT_SECONDS
    health_stale_seconds: float = DEFAULT_HEALTH_STALE_SECONDS
    # Private-worker lane (see ``parse_private_workers``).  ``workers`` is set
    # only on the primary; ``private_worker``/``ca_bundle`` only on a worker.
    workers: tuple["PrivateWorkerSpec", ...] = ()
    private_worker: bool = False
    ca_bundle: str = ""
    max_inflight: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        object.__setattr__(self, "workers", tuple(self.workers or ()))
        if type(self.max_inflight) is not int or not 1 <= self.max_inflight <= MAX_WORKER_INFLIGHT:
            raise InvalidInput("sonder-inference max_inflight must be an integer in [1, %d]"
                               % MAX_WORKER_INFLIGHT)
        if not isinstance(self.model, str) or not _MODEL_ID.fullmatch(self.model):
            raise InvalidInput("sonder-inference model id must be 1-128 non-space characters")
        tiers = dict(self.tier_models or {})
        for tier, model in tiers.items():
            if tier not in PROVIDER_TIERS:
                raise InvalidInput(
                    "%s names unknown tier %r (tiers: %s)"
                    % (ENV_TIER_MODELS, tier, ", ".join(PROVIDER_TIERS))
                )
            if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
                raise InvalidInput("%s has an invalid model id for tier %r" % (ENV_TIER_MODELS, tier))
        object.__setattr__(self, "tier_models", MappingProxyType(tiers))
        if not 0.0 < float(self.timeout_seconds) <= 86_400.0:
            raise InvalidInput("%s must be in (0, 86400]" % ENV_TIMEOUT)
        if not 0.0 <= float(self.health_ttl_seconds) <= 3_600.0:
            raise InvalidInput("%s must be in [0, 3600]" % ENV_HEALTH_TTL)
        if not 0.0 < float(self.health_timeout_seconds) <= 6.0:
            raise InvalidInput("%s must be in (0, 6]" % ENV_HEALTH_TIMEOUT)
        if not 0.0 <= float(self.health_stale_seconds) <= 3_600.0:
            raise InvalidInput("%s must be in [0, 3600]" % ENV_HEALTH_STALE)

    @property
    def loopback(self) -> bool:
        return is_loopback_url(self.base_url)

    @property
    def display_base_url(self) -> str:
        """The loopback URL, or only scheme and host for a remote endpoint."""
        if self.loopback:
            return self.base_url
        parts = urlsplit(self.base_url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def normalize_base_url(value: str) -> str:
    """Validate an Inference base URL; map bind-all addresses to loopback."""
    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise InvalidInput("sonder-inference base URL %r is not a valid URL" % raw) from exc
    if (parts.scheme not in ("http", "https") or not host
            or parts.username or parts.password or parts.query or parts.fragment):
        raise InvalidInput(
            "sonder-inference base URL must be http(s)://host[:port][/path] "
            "without credentials, query or fragment"
        )
    host = _BIND_ADDRESS_REWRITES.get(host.lower(), host.lower())
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        host = str(address)  # one spelling of ::1
        if host not in ACCEPTED_LOOPBACK_HOSTS:
            raise InvalidInput(
                "sonder-inference base URL host %s is a loopback alias that "
                "Sonder Inference's Host check refuses; use 127.0.0.1, "
                "localhost or [::1]" % host
            )
    netloc = "[%s]" % host if ":" in host else host
    if port is not None:
        netloc = "%s:%d" % (netloc, port)
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def is_loopback_url(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_tier_models(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        tier, sep, model = item.partition("=")
        if not sep or not tier.strip() or not model.strip():
            raise InvalidInput("%s entries must look like tier=model" % ENV_TIER_MODELS)
        tier = tier.strip().lower()
        if tier in result:
            raise InvalidInput("%s names tier %r twice" % (ENV_TIER_MODELS, tier))
        result[tier] = model.strip()
    return result


def _parse_float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = str(source.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise InvalidInput("%s must be a number, got %r" % (name, raw)) from exc
    if value != value:  # NaN
        raise InvalidInput("%s must be a number, got %r" % (name, raw))
    return value


def read_ready_file(path: str) -> str:
    """Return the ``url`` recorded by ``sonder-infer serve --ready-file``.

    A missing file means the server has not started listening, so nothing
    can have executed: that is :class:`SonderInferenceUnreachable`.  A file
    that exists but is malformed is a configuration error.
    """
    target = Path(path).expanduser()
    try:
        with target.open("rb") as stream:
            data = stream.read(READY_FILE_LIMIT + 1)
    except FileNotFoundError as exc:
        summary = "Sonder Inference ready file %s does not exist" % target
        raise SonderInferenceUnreachable(
            "%s; start `sonder-infer serve --ready-file %s`" % (summary, target),
            reason="ready file does not exist", summary=summary,
        ) from exc
    except OSError as exc:
        raise InvalidInput("cannot read %s %s: %s" % (ENV_READY_FILE, target, exc)) from exc
    if len(data) > READY_FILE_LIMIT:
        raise InvalidInput("%s %s exceeds %d bytes" % (ENV_READY_FILE, target, READY_FILE_LIMIT))
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidInput("%s %s is not JSON" % (ENV_READY_FILE, target)) from exc
    url = document.get("url") if isinstance(document, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise InvalidInput("%s %s has no url field" % (ENV_READY_FILE, target))
    api = document.get("api_version")
    if api is not None and api != API_VERSION:
        raise DependencyUnavailable(
            "incompatible sonder-inference API: ready file reports api_version "
            "%r, this runtime speaks %d" % (api, API_VERSION)
        )
    return url.strip()


@dataclass(frozen=True)
class PrivateWorkerSpec:
    """One operator-approved private Sonder Inference endpoint.

    The token itself is never part of the spec: ``token_env`` names the
    environment variable that holds it, read when the worker is used.
    """

    url: str
    ca_bundle: str
    token_env: str
    max_inflight: int = 1

    @property
    def display_url(self) -> str:
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _private_worker_url(raw: object, where: str) -> str:
    if not isinstance(raw, str):
        raise InvalidInput("%s.url must be a string" % where)
    url = normalize_base_url(raw)
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise InvalidInput("%s.url must use https://" % where)
    try:
        address = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        raise InvalidInput(
            "%s.url host must be an IP literal on a private network (a DNS "
            "name could resolve to a public host)" % where
        ) from None
    if (address.is_loopback or address.is_unspecified or address.is_multicast
            or not (address.is_private or address.is_link_local)):
        raise InvalidInput(
            "%s.url host %s is not a private-network address; private workers "
            "never reach public or loopback hosts" % (where, address)
        )
    if parts.port is None:
        raise InvalidInput("%s.url must name an explicit port" % where)
    return url


def parse_private_workers(raw: str, *, allow_remote: bool,
                          primary_url: str = "") -> tuple[PrivateWorkerSpec, ...]:
    """Parse ``SONDER_INFERENCE_PRIVATE_WORKERS`` (a JSON array of objects).

    Each object has ``url`` (https, private IP literal, explicit port),
    ``ca_bundle`` (absolute path to an existing PEM file, the worker's only
    trust anchor), ``token_env`` (the variable holding its bearer token) and
    optionally ``max_inflight`` (1-16, default 1).  The list is the explicit
    private-node consent for inference; it is honoured only together with the
    provider's remote opt-in (``SONDER_ALLOW_REMOTE_INFERENCE=1``) and never
    implies cloud consent.  Any fault refuses the whole configuration.
    """
    text = str(raw or "").strip()
    if not text:
        return ()
    if not allow_remote:
        raise InvalidInput(
            "%s lists private workers but %s is not 1; private inference "
            "workers require the remote-inference opt-in" % (ENV_PRIVATE_WORKERS, ENV_ALLOW_REMOTE)
        )
    if len(text) > PRIVATE_WORKERS_LIMIT:
        raise InvalidInput("%s exceeds %d characters" % (ENV_PRIVATE_WORKERS, PRIVATE_WORKERS_LIMIT))
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        raise InvalidInput("%s must be a JSON array of worker objects" % ENV_PRIVATE_WORKERS) from None
    if not isinstance(document, list) or not 1 <= len(document) <= MAX_PRIVATE_WORKERS:
        raise InvalidInput(
            "%s must be a JSON array of 1-%d worker objects" % (ENV_PRIVATE_WORKERS, MAX_PRIVATE_WORKERS)
        )
    workers: list[PrivateWorkerSpec] = []
    seen = {primary_url} if primary_url else set()
    token_envs: set[str] = set()
    for index, item in enumerate(document):
        where = "%s[%d]" % (ENV_PRIVATE_WORKERS, index)
        if not isinstance(item, dict) or not {"url", "ca_bundle", "token_env"} <= set(item) \
                or not set(item) <= _WORKER_KEYS:
            raise InvalidInput(
                "%s must be an object with url, ca_bundle, token_env and optional max_inflight" % where
            )
        url = _private_worker_url(item["url"], where)
        if url in seen:
            raise InvalidInput("%s.url duplicates another endpoint" % where)
        seen.add(url)
        ca_bundle = item["ca_bundle"]
        if not isinstance(ca_bundle, str) or not ca_bundle.strip():
            raise InvalidInput("%s.ca_bundle must be a path" % where)
        ca_path = Path(ca_bundle.strip()).expanduser()
        if not ca_path.is_absolute() or not ca_path.is_file():
            raise InvalidInput("%s.ca_bundle must be an absolute path to an existing file" % where)
        token_env = item["token_env"]
        if not isinstance(token_env, str) or not _TOKEN_ENV_NAME.fullmatch(token_env):
            raise InvalidInput("%s.token_env must be an upper-case environment variable name" % where)
        if token_env == ENV_API_KEY:
            raise InvalidInput(
                "%s.token_env must not reuse %s; each endpoint has its own token" % (where, ENV_API_KEY)
            )
        if token_env in token_envs:
            raise InvalidInput(
                "%s.token_env duplicates another worker's; each endpoint has its own token" % where
            )
        token_envs.add(token_env)
        inflight = item.get("max_inflight", 1)
        if type(inflight) is not int or not 1 <= inflight <= MAX_WORKER_INFLIGHT:
            raise InvalidInput("%s.max_inflight must be an integer in [1, %d]" % (where, MAX_WORKER_INFLIGHT))
        workers.append(PrivateWorkerSpec(url, str(ca_path), token_env, inflight))
    return tuple(workers)


def _parse_max_inflight(source: Mapping[str, str]) -> int:
    raw = str(source.get(ENV_MAX_INFLIGHT, "") or "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        raise InvalidInput("%s must be an integer, got %r" % (ENV_MAX_INFLIGHT, raw)) from None
    if not 1 <= value <= MAX_WORKER_INFLIGHT:
        raise InvalidInput("%s must be in [1, %d]" % (ENV_MAX_INFLIGHT, MAX_WORKER_INFLIGHT))
    return value


def config_from_env(env: Mapping[str, str] | None = None) -> SonderInferenceConfig:
    """Resolve settings lazily from the environment (never at import)."""
    source = os.environ if env is None else env
    base_url = str(source.get(ENV_BASE_URL, "") or "").strip()
    origin = "env"
    if not base_url:
        ready_file = str(source.get(ENV_READY_FILE, "") or "").strip()
        if ready_file:
            base_url, origin = read_ready_file(ready_file), "ready_file"
        else:
            base_url, origin = DEFAULT_BASE_URL, "default"
    allow_remote_raw = str(source.get(ENV_ALLOW_REMOTE, "") or "").strip()
    if allow_remote_raw not in ("", "0", "1"):
        raise InvalidInput("%s must be 0 or 1, got %r" % (ENV_ALLOW_REMOTE, allow_remote_raw))
    workers = parse_private_workers(
        str(source.get(ENV_PRIVATE_WORKERS, "") or ""),
        allow_remote=allow_remote_raw == "1", primary_url=normalize_base_url(base_url),
    )
    return SonderInferenceConfig(
        workers=workers,
        base_url=base_url,
        model=str(source.get(ENV_MODEL, "") or "").strip() or DEFAULT_MODEL,
        tier_models=_parse_tier_models(str(source.get(ENV_TIER_MODELS, "") or "")),
        api_key=str(source.get(ENV_API_KEY, "") or "").strip(),
        allow_remote=allow_remote_raw == "1",
        timeout_seconds=_parse_float(source, ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS),
        health_ttl_seconds=_parse_float(source, ENV_HEALTH_TTL, DEFAULT_HEALTH_TTL_SECONDS),
        health_timeout_seconds=_parse_float(source, ENV_HEALTH_TIMEOUT, HEALTH_PROBE_TIMEOUT_SECONDS),
        health_stale_seconds=_parse_float(source, ENV_HEALTH_STALE, DEFAULT_HEALTH_STALE_SECONDS),
        base_url_source=origin,
        max_inflight=_parse_max_inflight(source),
    )


def check_endpoint_policy(settings: SonderInferenceConfig) -> None:
    """Refuse a remote endpoint without explicit opt-in, TLS and a key.

    This is the part of consent that applies to every request, probes
    included, because every request carries the API key.  Prompt-bearing
    calls additionally require ``OperationContext.cloud_allowed`` -- except
    on an approved private worker, whose consent is the private-worker list
    itself (and which must then also carry its own CA bundle).
    """
    if settings.loopback and not settings.private_worker:
        return
    missing = []
    if not settings.allow_remote:
        missing.append("%s=1" % ENV_ALLOW_REMOTE)
    if urlsplit(settings.base_url).scheme != "https":
        missing.append("an https:// base URL")
    if not settings.api_key:
        missing.append("a worker token" if settings.private_worker else ENV_API_KEY)
    if settings.private_worker and not settings.ca_bundle:
        missing.append("a worker CA bundle")
    if missing:
        raise Forbidden(
            "sonder-inference endpoint %s is not loopback; remote inference "
            "requires %s" % (settings.display_base_url, ", ".join(missing))
        )


def _conversation_context(context: OperationContext) -> OperationContext:
    # The bridge may build a new per-call context. The enclosing operation
    # owns the stable session; never substitute a fresh correlation id for it.
    ambient = current_operation_context()
    if context.session_id or ambient is None or ambient.principal_id != context.principal_id:
        return context
    return ambient


def _conversation_key(context: OperationContext) -> str | None:
    operation = _conversation_context(context)
    session = operation.session_id
    if isinstance(session, str) and CORRELATION_VALUE.fullmatch(session):
        return session
    # These entrypoints bind a run-long correlation, unlike tier-helper and
    # offload, which mint an id per call. Do not make those look cache-stable.
    correlation = operation.correlation_id
    if (isinstance(correlation, str) and correlation.startswith(("standalone-", "repl-work-"))
            and CORRELATION_VALUE.fullmatch(correlation)):
        return correlation
    return None


def correlation_headers(context: OperationContext) -> dict[str, str]:
    """Correlation headers for one call (contract section 3.4)."""
    headers: dict[str, str] = {}
    correlation = context.correlation_id
    if isinstance(correlation, str) and CORRELATION_VALUE.fullmatch(correlation):
        headers["X-Sonder-Parent-Request-Id"] = correlation
        headers["X-Sonder-Run-Id"] = correlation
    workload = WORKLOAD_BY_SOURCE.get(context.source)
    if workload is not None:
        headers["X-Sonder-Workload"] = workload
    operation = _conversation_context(context)
    priority = {"http": "interactive", "repl": "interactive", "mcp": "interactive",
                "worker": "subagent", "system": "background"}.get(operation.source)
    session = _conversation_key(context)
    if session and (operation.source == "worker" or session.startswith(("standalone-", "repl-work-"))):
        headers["X-Sonder-Agent-Id"] = session
        priority = "subagent"
    if priority is not None:
        headers["X-Sonder-Priority"] = priority
    return headers


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (moment.microsecond // 1000)
    )


def _bounded(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= DETAIL_LIMIT else value[: DETAIL_LIMIT - 3] + "..."


def _error_fields(body: bytes) -> tuple[str, str]:
    """Return ``(code, message)`` from an Inference error document."""
    try:
        document = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "", ""
    return _error_document_fields(document, bound_message=False)


def _error_document_fields(document: object, *, bound_message: bool = True) -> tuple[str, str]:
    error = document.get("error") if isinstance(document, dict) else None
    if not isinstance(error, dict):
        return "", ""
    code = error.get("code")
    message = error.get("message") or error.get("detail")
    message = message if isinstance(message, str) else ""
    return (
        code if isinstance(code, str) else "",
        _bounded(message) if bound_message else message,
    )


def _response_timings(timings: dict, usage: dict) -> dict[str, int | float] | None:
    """Only bounded backend measurements belong in the response/activity feed."""
    result: dict[str, int | float] = {}
    for key in ("cache_n", "prompt_n", "predicted_n", "queue_ms", "draft_n", "draft_n_accepted"):
        value = timings.get(key)
        if key == "queue_ms":
            valid = type(value) in (int, float) and 0 <= value <= 86_400_000
        else:
            valid = type(value) is int and 0 <= value <= 1_000_000_000
        if valid:
            result[key] = value
    if "cache_n" not in result:
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if type(cached) is int and 0 <= cached <= 1_000_000_000:
            result["cache_n"] = cached
    return result or None


@dataclass(frozen=True)
class HealthSnapshot:
    """One bounded health observation, cached for the configured TTL."""

    base_url: str
    state: str  # ready | degraded | unavailable
    detail: str
    checked_at: datetime
    checked_monotonic: float
    document: Mapping[str, object] | None = None
    api_mismatch: bool = False
    auth_rejected: bool = False
    host_rejected: bool = False
    overloaded: bool = False
    timed_out: bool = False
    busy: bool = False

    @property
    def api_version(self) -> object:
        return None if self.document is None else self.document.get("api_version")


@dataclass(frozen=True)
class Readiness:
    """How a request would fare right now; see ``readiness()``."""

    kind: str  # ready | unreachable | overloaded | misconfigured
    detail: str
    synthetic: bool = False


def _reported_api_version(document: Mapping[str, object]) -> object:
    """Top-level ``api_version``, else the error envelope's ``sonder`` field."""
    api = document.get("api_version")
    if api is None:
        extension = document.get("sonder")
        if isinstance(extension, dict):
            api = extension.get("api_version")
    return api


@dataclass(frozen=True)
class IdentityObservation:
    """What ``/v1/sonder/identity`` reported for one model.

    ``identity`` is ``None`` whenever Inference could not measure every field
    (``reason`` says why).  ``synthetic`` identities (the mock backend) are
    reported for display but are never usable as routing evidence.
    """

    model: str
    identity: BackendIdentity | None
    synthetic: bool
    reason: str | None


class SonderInferenceGateway(OpenAICompatibleGateway):
    """ModelGateway over ``sonder-infer serve`` (Inference HTTP API v1)."""

    def __init__(
        self, config: SonderInferenceConfig | None = None, *,
        transport=None, get_transport=None,
        request_admission: HostModelRequestAdmission | None = None,
        env: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            None,
            transport=transport or direct_post_transport,
            request_admission=request_admission,
            provider_label=PROVIDER_LABEL,
            extra_headers=correlation_headers,
            http_error_classifier=self._classify_http_error,
            connect_error_classifier=self._classify_connect_error,
            get_transport=get_transport or direct_get_transport,
        )
        self._settings_override = config
        self._env = env
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._health_cache: HealthSnapshot | None = None
        self._health_probe_monotonic = float("-inf")
        self._identity_cache: dict[tuple[str, str], tuple[float, IdentityObservation]] = {}
        # Settings of the call in flight on this thread, so the transport's
        # error classifiers can name the endpoint and update its health.
        self._call = threading.local()
        # Private-worker placement (primary only): one child gateway per
        # approved worker, rebuilt when its resolved settings change, plus the
        # in-flight count per endpoint and a rotation cursor for ties.
        self._transport_seams = (transport, get_transport)
        self._workers: dict[str, tuple[tuple, SonderInferenceGateway]] = {}
        self._inflight: dict[str, int] = {}
        self._cursor = 0
        # Observed milliseconds per output token per endpoint (EWMA of
        # successful calls), so a much slower endpoint only gets overflow.
        self._ms_per_token: dict[str, float] = {}

    @property
    def last_response_meta(self) -> dict[str, object]:
        """Measurements for this thread's last call; failures clear old data."""
        return dict(getattr(self._call, "response_meta", {}))

    # -- configuration & consent ------------------------------------------

    @property
    def capabilities(self) -> frozenset[str]:
        return CAPABILITIES

    def settings(self) -> SonderInferenceConfig:
        """Resolve the current settings (env is read on every call)."""
        return self._settings_override or config_from_env(self._env)

    def _resolved_config(self) -> OpenAICompatibleConfig:
        settings = self.settings()
        return OpenAICompatibleConfig(
            base_url=settings.base_url, api_key=settings.api_key, model=settings.model,
        )

    @staticmethod
    def _is_loopback(base_url: str) -> bool:
        return is_loopback_url(base_url)

    def _enforce_consent(self, cfg: OpenAICompatibleConfig, context: OperationContext) -> None:
        settings = self.settings()
        check_endpoint_policy(settings)
        # An approved private worker is its own consent lane: it neither needs
        # nor grants cloud consent.  Every other non-loopback endpoint does.
        if not settings.loopback and not settings.private_worker and not context.cloud_allowed:
            raise Forbidden(
                "sonder-inference endpoint %s is not loopback and this "
                "operation context does not allow prompts to leave the machine"
                % settings.display_base_url
            )

    def select_model(self, request: ModelRequest, settings: SonderInferenceConfig) -> str:
        """Explicit option, else the tier map, else the configured default."""
        explicit = (request.options or {}).get("model")
        if explicit is not None:
            if not isinstance(explicit, str) or not _MODEL_ID.fullmatch(explicit.strip()):
                raise InvalidInput("model option must be a 1-128 character model id")
            return explicit.strip()
        return settings.tier_models.get(request.tier or "", settings.model)

    @staticmethod
    def unreachable_error(settings: SonderInferenceConfig, reason: str) -> SonderInferenceUnreachable:
        """Build the one operator message for "Inference did not get it"."""
        summary = "Sonder Inference at %s is not reachable or not ready (%s)" % (
            settings.display_base_url, _bounded(reason),
        )
        return SonderInferenceUnreachable(
            "%s; start it with `sonder-infer serve`, or set %s=ollama and restart "
            "the runtime to send requests it never received to local Ollama"
            % (summary, ENV_FALLBACK),
            reason=_bounded(reason), summary=summary,
        )

    # -- error classification (hooks of the shared transport) --------------

    def _classify_connect_error(self, reason: BaseException) -> SonderError | None:
        if isinstance(reason, ConnectionRefusedError):
            detail = "connection refused"
        elif isinstance(reason, socket.gaierror):
            detail = "host name does not resolve"
        else:
            return None
        self._mark_unavailable(detail)
        settings = getattr(self._call, "settings", None)
        if settings is None:
            return SonderInferenceUnreachable(
                "Sonder Inference is not reachable (%s); start it with "
                "`sonder-infer serve`" % detail, reason=detail,
            )
        return self.unreachable_error(settings, detail)

    @staticmethod
    def _forbidden(status: int, code: str, message: str) -> Forbidden:
        """403 forbidden_host/forbidden_origin is not a credentials problem."""
        label = "HTTP %d%s" % (status, " %s" % code if code else "")
        suffix = ": %s" % message if message else ""
        if code in ("forbidden_host", "forbidden_origin"):
            return Forbidden(
                "sonder-inference refused the request's %s (%s); the base URL "
                "host must be 127.0.0.1, localhost or [::1]%s"
                % ("Host" if code == "forbidden_host" else "Origin", label, suffix)
            )
        return Forbidden(
            "sonder-inference rejected the credentials (%s); check %s%s"
            % (label, ENV_API_KEY, suffix)
        )

    def _classify_http_error(self, status: int, body: bytes) -> SonderError | None:
        code, message = _error_fields(body)
        busy_timeout = status == 503 and code == "backend_unavailable" and "read timed out" in message.lower()
        message = _bounded(message)
        label = "HTTP %d%s" % (status, " %s" % code if code else "")
        suffix = ": %s" % message if message else ""
        if status == 503 and code == "not_ready":
            detail = "HTTP 503 not_ready"
            self._mark_unavailable(detail, state="degraded")
            settings = getattr(self._call, "settings", None)
            if settings is None:
                return SonderInferenceUnreachable(
                    "Sonder Inference is not ready (%s)" % detail, reason=detail,
                )
            return self.unreachable_error(settings, detail)
        if status == 503 and code == "overloaded":
            return CapacityExceeded("sonder-inference is at its connection limit (%s)" % label)
        if busy_timeout:
            return SonderInferenceBusyTimeout(
                "sonder-inference busy_timeout (%s)%s" % (label, suffix)
            )
        if status in (401, 403):
            return self._forbidden(status, code, message)
        if status == 429:
            return CapacityExceeded("sonder-inference scheduler rejected the request (%s)%s" % (label, suffix))
        if status in (400, 404, 405, 411, 413, 501):
            return InvalidInput("sonder-inference rejected the request (%s)%s" % (label, suffix))
        # 3xx (never followed), 408, 500, 503 backend_unavailable and anything
        # unexpected: the request may have reached the backend, so it is
        # never "unreachable".
        return DependencyUnavailable("sonder-inference failed the request (%s)%s" % (label, suffix))

    # -- health ------------------------------------------------------------

    def _mark_unavailable(self, detail: str, *, state: str = "unavailable") -> None:
        settings = getattr(self._call, "settings", None)
        if settings is None:
            return
        with self._lock:
            self._health_cache = HealthSnapshot(
                base_url=settings.base_url, state=state, detail=_bounded(detail),
                checked_at=self._wall_clock(), checked_monotonic=self._monotonic(),
            )

    def _fresh_cache(self, settings: SonderInferenceConfig, requested_at: float) -> HealthSnapshot | None:
        with self._lock:
            cached = self._health_cache
            probed_at = self._health_probe_monotonic
        if cached is None or cached.base_url != settings.base_url:
            return None
        if cached.busy:
            if requested_at - cached.checked_monotonic >= settings.health_stale_seconds:
                return None
            if probed_at > requested_at or requested_at - probed_at < settings.health_ttl_seconds:
                return cached
            return None
        # A snapshot taken after this caller asked is fresh whatever the TTL:
        # it is the single-flight probe another caller just finished.
        if (cached.checked_monotonic > requested_at
                or requested_at - cached.checked_monotonic < settings.health_ttl_seconds):
            return cached
        return None

    def health(
        self, *, settings: SonderInferenceConfig | None = None,
        timeout: float | None = None, refresh: bool = False,
    ) -> HealthSnapshot:
        """Return cached health, probing ``/v1/sonder/health`` when stale.

        Probes are single-flight: concurrent callers that find the cache
        stale wait for one probe instead of each opening a connection (which
        would itself push a busy server over its connection limit).  An
        Timeout/overload may reuse recent healthy evidence, labelled busy.
        Its age is never renewed without a successful probe.
        """
        settings = settings or self.settings()
        check_endpoint_policy(settings)
        requested_at = self._monotonic()
        if not refresh:
            cached = self._fresh_cache(settings, requested_at)
            if cached is not None:
                return cached
        with self._refresh_lock:
            if not refresh:
                cached = self._fresh_cache(settings, requested_at)
                if cached is not None:
                    return cached
            snapshot = self._probe_health(
                settings, settings.health_timeout_seconds if timeout is None else max(0.001, min(6.0, timeout)),
            )
            with self._lock:
                cached = self._health_cache
                now = self._monotonic()
                if (snapshot.timed_out or snapshot.overloaded) and (
                    cached is not None and cached.base_url == settings.base_url
                    and cached.state == "ready"
                    and 0 <= now - cached.checked_monotonic < settings.health_stale_seconds
                ):
                    snapshot = replace(cached, busy=True, detail="busy: using recent healthy observation")
                if not snapshot.overloaded:
                    self._health_cache = snapshot
                    self._health_probe_monotonic = now
        return snapshot

    def _probe_health(self, settings: SonderInferenceConfig, timeout: float) -> HealthSnapshot:
        def snapshot(state: str, detail: str, document=None, **flags) -> HealthSnapshot:
            return HealthSnapshot(
                base_url=settings.base_url, state=state, detail=_bounded(detail),
                checked_at=self._wall_clock(), checked_monotonic=self._monotonic(),
                document=document, **flags,
            )

        self._call.settings = settings
        cfg = OpenAICompatibleConfig(base_url=settings.base_url, api_key=settings.api_key)
        try:
            with trust_scope(settings.ca_bundle):
                status, document = self.get_json("/v1/sonder/health", timeout=timeout, cfg=cfg)
        except SonderInferenceUnreachable as exc:
            return snapshot("unavailable", exc.reason)
        except DeadlineExceeded:
            return snapshot("unavailable", "health probe timed out after %.1fs" % timeout, timed_out=True)
        except SonderError as exc:
            return snapshot("unavailable", "health probe failed: %s" % exc)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it never crashes its caller
            return snapshot("unavailable", "health probe failed: %s" % type(exc).__name__)
        code, message = _error_document_fields(document)
        if status in (401, 403):
            refused = self._forbidden(status, code, message)
            host_problem = code in ("forbidden_host", "forbidden_origin")
            return snapshot(
                "unavailable", str(refused).replace("sonder-inference ", "", 1),
                host_rejected=host_problem, auth_rejected=not host_problem,
            )
        if document is None:
            return snapshot("unavailable", "health returned HTTP %d without a JSON document" % status)
        api = _reported_api_version(document)
        if api is not None and api != API_VERSION:
            return snapshot(
                "unavailable",
                "incompatible sonder-inference API version %r (this runtime speaks %d)"
                % (api, API_VERSION),
                document=document, api_mismatch=True,
            )
        if code == "overloaded":
            return snapshot(
                "degraded",
                "server is at its connection limit (HTTP %d overloaded)" % status,
                document=document, overloaded=True,
            )
        if code:
            return snapshot(
                "unavailable", "health returned HTTP %d %s%s"
                % (status, code, ": %s" % message if message else ""),
                document=document,
            )
        if api is None:
            return snapshot(
                "unavailable",
                "health returned HTTP %d without an api_version; is this Sonder Inference?"
                % status,
                document=document,
            )
        reported = document.get("status")
        if status == 200 and reported == "ready":
            models = document.get("models") if isinstance(document.get("models"), list) else []
            detail = "ready: %d model(s)%s" % (
                len(models), ", synthetic mock backend" if document.get("synthetic") is True else "",
            )
            return snapshot("ready", detail, document=document)
        if status == 503 and reported in ("starting", "draining"):
            return snapshot("degraded", "server is %s" % reported, document=document)
        return snapshot(
            "unavailable", "health returned HTTP %d status %r" % (status, reported),
            document=document,
        )

    def _require_ready(self, settings: SonderInferenceConfig, timeout: float) -> HealthSnapshot:
        started = time.monotonic()
        budget = min(settings.health_timeout_seconds, timeout)
        snap = self.health(settings=settings, timeout=budget)
        remaining = timeout - (time.monotonic() - started)
        if (snap.timed_out or snap.overloaded) and remaining > 0:
            snap = self.health(settings=settings, timeout=min(2 * budget, 6.0, remaining), refresh=True)
        if snap.api_mismatch:
            raise DependencyUnavailable(snap.detail)
        if snap.auth_rejected or snap.host_rejected:
            raise Forbidden("sonder-inference %s" % snap.detail)
        if snap.overloaded:
            # Preserve capacity classification for an overloaded endpoint with
            # no healthy evidence; this must not activate the Ollama fallback.
            error = CapacityExceeded("sonder-inference at %s %s; retry shortly"
                                     % (settings.display_base_url, snap.detail))
            error.kind = "provider_unavailable"
            raise error
        if snap.state != "ready":
            raise self.unreachable_error(settings, snap.detail)
        return snap

    def readiness(self) -> Readiness:
        """Classify the endpoint the way :meth:`generate` would treat it.

        Never raises and never generates.  ``kind`` is ``ready``,
        ``unreachable`` (a request would be refused before sending, so a
        configured fallback would carry it), ``overloaded`` (transient; the
        request fails with CapacityExceeded, no fallback) or ``misconfigured``
        (configuration, consent, credentials, Host or API version: every
        request fails and no fallback can help).
        """
        try:
            settings = self.settings()
        except SonderInferenceUnreachable as exc:
            return Readiness("unreachable", exc.summary)
        except Exception as exc:  # noqa: BLE001 - classification is total
            return Readiness("misconfigured", "configuration error: %s" % _bounded(exc))
        where = settings.display_base_url
        try:
            check_endpoint_policy(settings)
            snap = self.health(settings=settings)
        except Exception as exc:  # noqa: BLE001 - classification is total
            # Consent refusals already name the endpoint.
            return Readiness("misconfigured", _bounded(exc))
        detail = "%s: %s" % (where, snap.detail)
        if snap.api_mismatch or snap.auth_rejected or snap.host_rejected:
            return Readiness("misconfigured", detail)
        if snap.overloaded:
            return Readiness("overloaded", detail)
        if snap.state == "ready":
            synthetic = (snap.document or {}).get("synthetic") is True
            return Readiness("ready", detail, synthetic=synthetic)
        return Readiness("unreachable", detail)

    def capability_health(self) -> CapabilityHealth:
        """Provider-reported health from the cached, bounded probe."""
        try:
            snap = self.health()
        except Exception as exc:  # noqa: BLE001 - health reports, never raises
            return CapabilityHealth(
                provider=PROVIDER_ID, capabilities=frozenset({Capability.GENERATION}),
                healthy=False, checked_at=self._wall_clock(),
                detail=_bounded(exc if isinstance(exc, SonderError) else type(exc).__name__),
            )
        return CapabilityHealth(
            provider=PROVIDER_ID,
            capabilities=frozenset({Capability.GENERATION}),
            healthy=snap.state == "ready",
            checked_at=snap.checked_at,
            detail=snap.detail,
        )

    # -- identity ----------------------------------------------------------

    def observe_identity(self, model: str | None = None) -> IdentityObservation:
        """Read ``/v1/sonder/identity``; never fabricates a missing value."""
        settings = self.settings()
        check_endpoint_policy(settings)
        key = (settings.base_url, model or "")
        now = self._monotonic()
        with self._lock:
            cached = self._identity_cache.get(key)
        if cached is not None and now - cached[0] < settings.health_ttl_seconds:
            return cached[1]
        path = "/v1/sonder/identity"
        if model:
            path += "?model=" + quote(model, safe="")
        self._call.settings = settings
        cfg = OpenAICompatibleConfig(base_url=settings.base_url, api_key=settings.api_key)
        with trust_scope(settings.ca_bundle):
            status, document = self.get_json(path, timeout=HEALTH_PROBE_TIMEOUT_SECONDS, cfg=cfg)
        if status == 404:
            raise InvalidInput("sonder-inference does not serve model %r" % (model or ""))
        if status in (401, 403):
            raise Forbidden("sonder-inference rejected credentials (HTTP %d); check %s" % (status, ENV_API_KEY))
        if status != 200 or document is None:
            raise DependencyUnavailable("sonder-inference identity returned HTTP %d" % status)
        if document.get("schema") != IDENTITY_SCHEMA:
            raise DependencyUnavailable(
                "incompatible sonder-inference identity schema %r" % document.get("schema")
            )
        synthetic = document.get("synthetic")
        served = document.get("model")
        if type(synthetic) is not bool or not isinstance(served, str) or not served:
            raise DependencyUnavailable("sonder-inference identity document is malformed")
        raw = document.get("backend_identity")
        reason = document.get("reason")
        if raw is None:
            observation = IdentityObservation(
                served, None, synthetic,
                _bounded(reason) if isinstance(reason, str) and reason else "not reported",
            )
        else:
            try:
                identity = BackendIdentity.from_dict(raw)
            except (TypeError, ValueError) as exc:
                raise DependencyUnavailable(
                    "sonder-inference reported an invalid backend identity: %s" % exc
                ) from exc
            observation = IdentityObservation(served, identity, synthetic, None)
        with self._lock:
            self._identity_cache[key] = (self._monotonic(), observation)
        return observation

    def backend_identity(self, model: str | None = None) -> BackendIdentity | None:
        """The measured identity for display; synthetic ones included."""
        return self.observe_identity(model).identity

    def routing_identity(self, model: str | None = None) -> BackendIdentity | None:
        """The identity usable as routing evidence: never a synthetic one."""
        observation = self.observe_identity(model)
        return None if observation.synthetic else observation.identity

    # -- status ------------------------------------------------------------

    @staticmethod
    def _tier_models(settings: SonderInferenceConfig) -> dict[str, str]:
        """The model each tier's request names (``select_model`` without options)."""
        return {tier: settings.tier_models.get(tier, settings.model) for tier in PROVIDER_TIERS}

    def served_tier_models(self) -> Mapping[str, Mapping[str, str]]:
        """``{provider: {tier: model}}`` from configuration alone (no I/O)."""
        return {PROVIDER_ID: self._tier_models(self.settings())}

    def _workers_status(self, settings: SonderInferenceConfig) -> list[dict[str, object]]:
        """One content-free row per approved private worker (never the token)."""
        rows: list[dict[str, object]] = []
        for spec in settings.workers:
            row: dict[str, object] = {
                "base_url": spec.display_url, "state": "unavailable", "healthy": False,
                "detail": None, "models": [], "max_inflight": spec.max_inflight,
            }
            with self._lock:
                row["inflight"] = self._inflight.get(spec.url, 0)
                observed = self._ms_per_token.get(spec.url)
            row["ms_per_token"] = round(observed, 1) if observed is not None else None
            try:
                gateway = self._worker_gateway(settings, spec)
                snap = gateway.health()
            except Exception as exc:  # noqa: BLE001 - status reports, never raises
                row["detail"] = _bounded(exc if isinstance(exc, SonderError) else type(exc).__name__)
                rows.append(row)
                continue
            models = (snap.document or {}).get("models")
            row.update(
                state=snap.state, healthy=snap.state == "ready", detail=snap.detail,
                models=[item["id"] for item in models
                        if isinstance(item, dict) and isinstance(item.get("id"), str)]
                if isinstance(models, list) else [],
            )
            rows.append(row)
        return rows

    def provider_status(self) -> Mapping[str, Mapping[str, object]]:
        """Content-free status for doctor, the ecosystem route and the app."""
        entry: dict[str, object] = {key: None for key in STATUS_KEYS}
        entry.update(
            provider=PROVIDER_ID, state="unavailable", healthy=False,
            capabilities=sorted(CAPABILITIES), models=[], fallback=None,
            fallback_count=0, busy=False,
        )
        try:
            settings = self.settings()
            entry["base_url"] = settings.display_base_url
            entry["tier_models"] = self._tier_models(settings)
            if settings.workers:
                entry["workers"] = self._workers_status(settings)
            snap = self.health(settings=settings)
        except SonderInferenceUnreachable as exc:
            entry["detail"] = _bounded(exc.summary)
            return {PROVIDER_ID: entry}
        except Exception as exc:  # noqa: BLE001 - status reports, never raises
            entry["detail"] = _bounded(exc if isinstance(exc, SonderError) else type(exc).__name__)
            return {PROVIDER_ID: entry}
        document = snap.document or {}
        models = document.get("models") if isinstance(document.get("models"), list) else []
        synthetic = document.get("synthetic")
        entry.update(
            state=snap.state,
            healthy=snap.state == "ready",
            busy=snap.busy,
            checked_at=_rfc3339(snap.checked_at),
            detail=snap.detail,
            version=document.get("version") if isinstance(document.get("version"), str) else None,
            api_version=snap.api_version if isinstance(snap.api_version, int) else None,
            models=[
                item["id"] for item in models
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ],
            synthetic=synthetic if type(synthetic) is bool else None,
        )
        if snap.state in ("ready", "degraded") and not snap.api_mismatch:
            base = settings.base_url
            entry["telemetry"] = {key: base + path for key, path in TELEMETRY_PATHS.items()}
        if snap.state == "ready" and not snap.busy:
            try:
                identity = self.backend_identity()
            except Exception as exc:  # noqa: BLE001 - identity is optional display data
                logger.info("sonder-inference identity unavailable: %s", _bounded(
                    exc if isinstance(exc, SonderError) else type(exc).__name__,
                ))
                identity = None
            entry["identity"] = identity.to_dict() if identity is not None else None
        return {PROVIDER_ID: entry}

    # -- generate ----------------------------------------------------------

    @staticmethod
    def _payload_options(options: Mapping[str, object]) -> dict[str, object]:
        for name in _UNSUPPORTED_OPTIONS:
            if options.get(name) not in (None, False, "", [], {}):
                raise InvalidInput("sonder-inference v1 does not support the %r option" % name)
        payload: dict[str, object] = {}
        for option, wire in _FORWARDED_OPTIONS.items():
            if option not in options or options[option] is None:
                continue
            value = options[option]
            if type(value) is bool or not isinstance(value, (int, float)):
                raise InvalidInput("option %r must be a number" % option)
            if option in _INTEGER_OPTIONS:
                if float(value) != int(value):
                    raise InvalidInput("option %r must be an integer" % option)
                value = int(value)
            payload[wire] = value
        stop = options.get("stop")
        if stop is not None:
            if isinstance(stop, str):
                stop = [stop]
            if (not isinstance(stop, (list, tuple)) or not 1 <= len(stop) <= 4
                    or any(not isinstance(item, str) or not item for item in stop)):
                raise InvalidInput("option 'stop' must be a string or at most 4 strings")
            payload["stop"] = list(stop)
        budget = options.get("reasoning_budget_tokens")
        if budget is not None:
            if type(budget) is not int or not 0 <= budget <= 1_000_000:
                raise InvalidInput("option 'reasoning_budget_tokens' must be an integer in [0, 1000000]")
            payload["reasoning_budget_tokens"] = budget
        message = options.get("reasoning_budget_message")
        if message is not None:
            if not isinstance(message, str) or len(message) > 16_384:
                raise InvalidInput("option 'reasoning_budget_message' must be text of at most 16384 characters")
            payload["reasoning_budget_message"] = message
        return payload

    def _retry_delay(self, error: SonderInferenceBusyTimeout) -> float:
        cause = error.__cause__
        headers = cause.headers if isinstance(cause, urllib.error.HTTPError) else None
        raw = next((value for key, value in (headers or {}).items()
                    if str(key).lower() == "retry-after"), None)
        delay = BUSY_RETRY_SECONDS
        if raw is not None:
            try:
                delay = float(raw)
            except (ValueError, TypeError):
                try:
                    delay = max(0.0, (parsedate_to_datetime(raw) - self._wall_clock()).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        if not math.isfinite(delay) or delay < 0:
            delay = BUSY_RETRY_SECONDS
        return min(delay, BUSY_RETRY_SECONDS)

    def _post_with_busy_retry(self, payload, cfg, settings, context, live) -> dict:
        # Both attempts and the backoff share one transport budget. Only an
        # HTTP error before SSE content can reach this classifier in production.
        deadline = time.monotonic() + self._call_timeout(settings, context)
        for attempt in range(2):
            timeout = min(self._call_timeout(settings, context), deadline - time.monotonic())
            if timeout <= 0:
                raise DeadlineExceeded("sonder-inference retry budget exhausted")
            try:
                with trust_scope(settings.ca_bundle):
                    return self._post("/v1/chat/completions", payload, cfg, timeout, context=context)
            except SonderInferenceBusyTimeout as exc:
                if attempt or (live is not None and (live.generated or live.cancelled)):
                    raise
                delay = self._retry_delay(exc)
                if deadline - time.monotonic() <= delay:
                    raise
                if context.cancellation is not None:
                    context.cancellation.wait(delay)
                else:
                    time.sleep(delay)
                self._check_liveness(context, phase="during busy retry")
        raise AssertionError("bounded retry exhausted")

    def _call_timeout(self, settings: SonderInferenceConfig, context: OperationContext) -> float:
        remaining = self._check_liveness(context)
        timeout = settings.timeout_seconds
        return timeout if remaining is None else min(timeout, remaining)

    # -- private-worker placement -------------------------------------------
    #
    # With approved private workers configured, each request is placed whole
    # on the primary or one worker: the endpoint with the lowest expected
    # cost ((in-flight + 1) / max_inflight x observed ms per output token)
    # among those not known to be down that serve the model, ties rotated.  A request moves to another endpoint only after
    # SonderInferenceUnreachable, i.e. when it provably never executed; any
    # other failure is final, exactly as on a single endpoint.

    def _worker_settings(self, settings: SonderInferenceConfig,
                         spec: PrivateWorkerSpec) -> SonderInferenceConfig:
        source = os.environ if self._env is None else self._env
        token = str(source.get(spec.token_env, "") or "").strip()
        if len(token) < MIN_WORKER_TOKEN_LENGTH:
            raise Forbidden(
                "private sonder-inference worker %s: %s is unset or shorter than %d characters"
                % (spec.display_url, spec.token_env, MIN_WORKER_TOKEN_LENGTH)
            )
        return SonderInferenceConfig(
            base_url=spec.url, model=settings.model, tier_models=dict(settings.tier_models),
            api_key=token, allow_remote=True, timeout_seconds=settings.timeout_seconds,
            health_ttl_seconds=settings.health_ttl_seconds, base_url_source="private_worker",
            health_timeout_seconds=settings.health_timeout_seconds,
            health_stale_seconds=settings.health_stale_seconds,
            private_worker=True, ca_bundle=spec.ca_bundle, max_inflight=spec.max_inflight,
        )

    def _worker_gateway(self, settings: SonderInferenceConfig,
                        spec: PrivateWorkerSpec) -> "SonderInferenceGateway":
        worker = self._worker_settings(settings, spec)
        key = (worker.base_url, worker.api_key, worker.ca_bundle, worker.max_inflight, worker.model,
               tuple(sorted(worker.tier_models.items())), worker.timeout_seconds,
               worker.health_ttl_seconds, worker.health_timeout_seconds, worker.health_stale_seconds)
        with self._lock:
            cached = self._workers.get(spec.url)
            if cached is not None and cached[0] == key:
                return cached[1]
        transport, get_transport = self._transport_seams
        gateway = SonderInferenceGateway(
            worker, transport=transport, get_transport=get_transport,
            request_admission=self._request_admission, env=self._env,
            monotonic=self._monotonic, wall_clock=self._wall_clock,
        )
        with self._lock:
            self._workers[spec.url] = (key, gateway)
        return gateway

    def _peek_health(self, settings: SonderInferenceConfig) -> HealthSnapshot | None:
        """The cached snapshot within its TTL, without probing."""
        with self._lock:
            cached = self._health_cache
        if cached is None or cached.base_url != settings.base_url:
            return None
        if self._monotonic() - cached.checked_monotonic >= settings.health_ttl_seconds and not cached.busy:
            return None
        return cached

    @staticmethod
    def _serves(snapshot: HealthSnapshot | None, model: str) -> bool:
        """False only when fresh health lists models and ``model`` is absent."""
        if snapshot is None or snapshot.document is None or model == DEFAULT_MODEL:
            return True
        models = snapshot.document.get("models")
        if not isinstance(models, list):
            return True
        ids = {item.get("id") for item in models if isinstance(item, dict)}
        return model in ids

    def _placement_candidates(self, request: ModelRequest, settings: SonderInferenceConfig,
                              context: OperationContext):
        """``[(key, gateway, settings)]`` for the primary and each usable worker."""
        candidates = []
        try:
            # The primary is a candidate only when this call may use it at all:
            # a consent refusal there is final and would never reach a worker.
            check_endpoint_policy(settings)
            if settings.loopback or context.cloud_allowed:
                candidates.append((settings.base_url, self, settings))
        except SonderError:
            pass
        for spec in settings.workers:
            try:
                gateway = self._worker_gateway(settings, spec)
            except SonderError as exc:
                logger.warning("private sonder-inference worker skipped: %s", _bounded(exc))
                continue
            candidates.append((spec.url, gateway, gateway.settings()))
        model = self.select_model(request, settings)
        usable = []
        for key, gateway, endpoint in candidates:
            snapshot = gateway._peek_health(endpoint)
            if endpoint.private_worker:
                # A worker is chosen only on positive evidence that it serves
                # this exact model: a missing model would be a 404, which is
                # final and never moves to another endpoint.
                if not self._worker_serves(gateway, endpoint, snapshot, model):
                    continue
                usable.append((key, gateway, endpoint))
                continue
            if snapshot is None:
                # Learn the primary's model list too, so a model only a worker
                # serves is not sent here first (a 404 would be final).
                try:
                    snapshot = self.health(settings=endpoint)
                except Exception:  # noqa: BLE001 - the primary path reports it
                    snapshot = None
            if snapshot is not None and snapshot.state != "ready" and not snapshot.busy:
                continue
            if not self._serves(snapshot, model):
                continue
            usable.append((key, gateway, endpoint))
        return usable

    @staticmethod
    def _worker_serves(gateway: "SonderInferenceGateway", endpoint: SonderInferenceConfig,
                       snapshot: HealthSnapshot | None, model: str) -> bool:
        """True only when the worker's health (probed if stale) lists ``model``.

        The ``default`` alias names whatever each server was started with, so
        it never selects a worker.  The probe is the cached, single-flight,
        bounded health check (at most one per worker per health TTL).
        """
        if model == DEFAULT_MODEL:
            return False
        if snapshot is None:
            try:
                snapshot = gateway.health(settings=endpoint)
            except Exception as exc:  # noqa: BLE001 - an unusable worker is skipped
                logger.info("private sonder-inference worker %s skipped: %s",
                            endpoint.display_base_url,
                            _bounded(exc if isinstance(exc, SonderError) else type(exc).__name__))
                return False
        if snapshot.document is None or (snapshot.state != "ready" and not snapshot.busy):
            return False
        models = snapshot.document.get("models")
        if not isinstance(models, list):
            return False
        return model in {item.get("id") for item in models if isinstance(item, dict)}

    def _acquire_endpoint(self, usable, excluded: set[str]):
        """Pick the endpoint with the lowest expected cost and count it.

        Cost = (in-flight + 1) / max_inflight x observed ms per output token.
        An idle endpoint with no observation yet costs nothing, so each one is
        measured once rather than starved; a busy unobserved one borrows the
        fastest observed value (1.0 when there is none, i.e. plain
        least-in-flight).  Ties rotate.
        """
        with self._lock:
            open_ = [item for item in usable if item[0] not in excluded]
            if not open_:
                return None
            known = [self._ms_per_token[item[0]] for item in open_ if item[0] in self._ms_per_token]
            fallback = min(known) if known else 1.0
            start = self._cursor % len(open_)
            self._cursor += 1
            rotated = open_[start:] + open_[:start]

            def cost(item):
                inflight = self._inflight.get(item[0], 0)
                load = (inflight + 1) / item[2].max_inflight
                if item[0] not in self._ms_per_token and inflight == 0:
                    return (0.0, load)
                return (load * self._ms_per_token.get(item[0], fallback), load)

            chosen = min(rotated, key=cost)
            load = cost(chosen)[1]
            self._inflight[chosen[0]] = self._inflight.get(chosen[0], 0) + 1
            return chosen, load

    def _observe_latency(self, key: str, response: ModelResponse, load: float) -> None:
        """Fold one success into the endpoint's ms-per-token estimate.

        The duration includes waiting behind the requests already in flight
        there, so it is divided by the dispatch load ((in-flight + 1) /
        max_inflight, at least 1): the estimate is the service rate, and
        ``cost`` multiplies the current load back in exactly once.
        """
        tokens = getattr(response, "tokens_out", None)
        duration = getattr(response, "duration_ms", None)
        if not isinstance(tokens, int) or tokens <= 0 or not isinstance(duration, (int, float)) or duration < 0:
            return
        sample = max(float(duration), 1.0) / tokens / max(1.0, load)
        with self._lock:
            previous = self._ms_per_token.get(key)
            self._ms_per_token[key] = sample if previous is None else (
                LATENCY_EWMA_ALPHA * sample + (1 - LATENCY_EWMA_ALPHA) * previous
            )

    def _release_endpoint(self, key: str) -> None:
        with self._lock:
            remaining = self._inflight.get(key, 0) - 1
            if remaining > 0:
                self._inflight[key] = remaining
            else:
                self._inflight.pop(key, None)

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        settings = self.settings()
        if settings.private_worker or not settings.workers:
            return self._generate_here(request, context)
        self._call.response_meta = {}
        usable = self._placement_candidates(request, settings, context)
        if not usable:
            # Nothing is known to be able to take it: let the primary produce
            # the precise refusal (not ready, unknown model, consent, ...).
            return self._generate_here(request, context)
        excluded: set[str] = set()
        last_error: SonderInferenceUnreachable | None = None
        while True:
            acquired = self._acquire_endpoint(usable, excluded)
            if acquired is None:
                assert last_error is not None
                raise last_error
            chosen, load = acquired
            key, gateway, endpoint = chosen
            try:
                logger.info(
                    "sonder-inference placed request on %s (%s)",
                    endpoint.display_base_url, "private worker" if endpoint.private_worker else "primary",
                )
                response = gateway._generate_here(request, context)
            except SonderInferenceUnreachable as exc:
                # Provably never executed there: another endpoint may take it.
                excluded.add(key)
                last_error = exc
                continue
            finally:
                self._release_endpoint(key)
                if gateway is not self:
                    self._call.response_meta = gateway.last_response_meta
            self._observe_latency(key, response, load)
            if isinstance(response, SonderInferenceResponse):
                response = replace(response, endpoint=endpoint.display_base_url)
            return response

    def _generate_here(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        self._call.response_meta = {}
        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        settings = self.settings()
        cfg = OpenAICompatibleConfig(
            base_url=settings.base_url, api_key=settings.api_key, model=settings.model,
        )
        self._enforce_consent(cfg, context)
        timeout = self._call_timeout(settings, context)
        options = dict(request.options or {})
        think = options.pop("think", None)  # decided after readiness (request_tuning)
        model = self.select_model(request, settings)
        live = stream_sink.call_stream()
        payload = {
            "model": model,
            "messages": self._build_messages(request),
            "stream": live is not None,
            **self._payload_options(options),
        }
        if live is not None:
            payload["stream_options"] = {"include_usage": True}
        cache_key = _conversation_key(context)
        if cache_key is not None:
            payload["prompt_cache_key"] = cache_key
        self._call.settings = settings
        snap = self._require_ready(settings, timeout)
        tune_request(payload, think, snap.document, self._env)
        started = time.monotonic()
        data = self._post_with_busy_retry(payload, cfg, settings, context, live)
        self._check_liveness(context, phase="during model call")

        def served_model(result: dict) -> str:
            extension = result.get("sonder")
            if isinstance(extension, dict) and "api_version" in extension:
                if extension["api_version"] != API_VERSION:
                    raise DependencyUnavailable(
                        "incompatible sonder-inference API version %r (this runtime speaks %d)"
                        % (extension["api_version"], API_VERSION)
                    )
            served = result.get("model")
            if not isinstance(served, str) or not served.strip() or served == DEFAULT_MODEL:
                raise DependencyUnavailable(
                    "incompatible sonder-inference API: the response did not name the served model"
                )
            return served

        served = served_model(data)
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        empty_length = (
            isinstance(choice, dict) and choice.get("finish_reason") == "length"
            and isinstance(message, dict)
            and (message.get("content") is None
                 or isinstance(message.get("content"), str)
                 and not message["content"].strip())
        )
        repaired = False
        if (empty_length and think is None
                and thinking_supported(snap.document, self._env)
                and (live is None or not live.generated and not live.cancelled)):
            # The model used its entire cap on private reasoning. No answer
            # reached the caller, so one non-thinking retry can recover it.
            # Keep this within the original transport and operation deadlines.
            remaining = min(self._call_timeout(settings, context),
                            timeout - (time.monotonic() - started))
            if remaining > 0:
                retry = dict(payload)
                retry["stream"] = False
                retry.pop("stream_options", None)
                template = dict(retry.get("chat_template_kwargs") or {})
                template["enable_thinking"] = False
                retry["chat_template_kwargs"] = template
                data = self._post("/v1/chat/completions", retry, cfg, remaining,
                                  context=context)
                self._check_liveness(context, phase="during model call")
                served = served_model(data)
                repaired = True
        text = self._extract_text(data)
        usage = data.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise DependencyUnavailable("sonder-inference returned an invalid usage object")
        timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
        extension = usage.get("sonder")
        nested = extension.get("timings") if isinstance(extension, dict) else None
        if isinstance(nested, dict):
            timings = {**timings, **nested}
        measured = _response_timings(timings, usage)
        telemetry = from_openai_compatible({**data, "timings": timings}, with_usage=True)
        prompt_count = usage.get("prompt_tokens")
        if prompt_count is None:
            prompt_count = timings.get("prompt_n")
        output_count = usage.get("completion_tokens")
        if output_count is None:
            output_count = timings.get("predicted_n")
        choices = data.get("choices")
        finish = choices[0].get("finish_reason") if (
            isinstance(choices, list) and choices and isinstance(choices[0], dict)
        ) else None
        finish = finish if finish in ("stop", "length", "content_filter", "tool_calls", "function_call") else None
        self._call.response_meta = {key: value for key, value in (
            ("finish_reason", finish), ("done_reason", finish), ("timings", measured),
            ("empty_length_recovered", True if repaired else None),
        ) if value is not None}
        # The public activity projection retains summary, while the owning
        # span also gets structured measurements. Never attach response text.
        activity_tracker.record_event(
            "inference_outcome", provider=PROVIDER_ID,
            summary=json.dumps(self._call.response_meta, sort_keys=True),
            **self._call.response_meta,
        )
        response = SonderInferenceResponse(
            text=require_model_text(text),
            model=served,
            tier=request.tier or PROVIDER_ID,
            duration_ms=int((time.monotonic() - started) * 1000),
            tokens_in=optional_token_count(prompt_count, "prompt token count"),
            tokens_out=optional_token_count(output_count, "completion token count"),
            telemetry=telemetry,
            timings=measured,
            finish_reason=finish,
        )
        default_registry().observe_inference(PROVIDER_ID, telemetry)
        return response

    # -- embed -------------------------------------------------------------

    def embed(self, texts: Sequence[str], context: OperationContext) -> Sequence[Embedding]:
        del texts, context
        raise DependencyUnavailable(
            "sonder-inference does not serve embeddings in API v1; set "
            "SONDER_EMBEDDING_PROVIDER=ollama"
        )


__all__ = [
    "API_VERSION",
    "CORRELATION_VALUE",
    "DEFAULT_BASE_URL",
    "ENV_PRIVATE_WORKERS",
    "HealthSnapshot",
    "IdentityObservation",
    "PROVIDER_ID",
    "PROVIDER_LABEL",
    "PrivateWorkerSpec",
    "Readiness",
    "STATUS_KEYS",
    "SonderInferenceConfig",
    "SonderInferenceGateway",
    "SonderInferenceUnreachable",
    "WORKLOAD_BY_SOURCE",
    "check_endpoint_policy",
    "config_from_env",
    "correlation_headers",
    "direct_get_transport",
    "direct_post_transport",
    "is_loopback_url",
    "normalize_base_url",
    "parse_private_workers",
    "read_ready_file",
    "trust_scope",
]
