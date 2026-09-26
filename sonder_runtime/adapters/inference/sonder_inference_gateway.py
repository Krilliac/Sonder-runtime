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

import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Sequence
from urllib.parse import quote, urlsplit, urlunsplit

from ...application.context import OperationContext
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
from ..provider_bindings import PROVIDER_TIERS
from .openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
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
HEALTH_PROBE_TIMEOUT_SECONDS = 2.0
READY_FILE_LIMIT = 65_536
DETAIL_LIMIT = 240

ENV_BASE_URL = "SONDER_INFERENCE_BASE_URL"
ENV_MODEL = "SONDER_INFERENCE_MODEL"
ENV_TIER_MODELS = "SONDER_INFERENCE_TIER_MODELS"
ENV_API_KEY = "SONDER_INFERENCE_API_KEY"
ENV_ALLOW_REMOTE = "SONDER_ALLOW_REMOTE_INFERENCE"
ENV_READY_FILE = "SONDER_INFERENCE_READY_FILE"
ENV_TIMEOUT = "SONDER_INFERENCE_TIMEOUT_SECONDS"
ENV_HEALTH_TTL = "SONDER_INFERENCE_HEALTH_TTL_SECONDS"
ENV_FALLBACK = "SONDER_INFERENCE_FALLBACK"

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
    "telemetry", "fallback", "fallback_count",
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
    """


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
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
        raise SonderInferenceUnreachable(
            "Sonder Inference ready file %s does not exist; start "
            "`sonder-infer serve --ready-file %s`" % (target, target)
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
    return SonderInferenceConfig(
        base_url=base_url,
        model=str(source.get(ENV_MODEL, "") or "").strip() or DEFAULT_MODEL,
        tier_models=_parse_tier_models(str(source.get(ENV_TIER_MODELS, "") or "")),
        api_key=str(source.get(ENV_API_KEY, "") or "").strip(),
        allow_remote=allow_remote_raw == "1",
        timeout_seconds=_parse_float(source, ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS),
        health_ttl_seconds=_parse_float(source, ENV_HEALTH_TTL, DEFAULT_HEALTH_TTL_SECONDS),
        base_url_source=origin,
    )


def check_endpoint_policy(settings: SonderInferenceConfig) -> None:
    """Refuse a remote endpoint without explicit opt-in, TLS and a key.

    This is the part of consent that applies to every request, probes
    included, because every request carries the API key.  Prompt-bearing
    calls additionally require ``OperationContext.cloud_allowed``.
    """
    if settings.loopback:
        return
    missing = []
    if not settings.allow_remote:
        missing.append("%s=1" % ENV_ALLOW_REMOTE)
    if urlsplit(settings.base_url).scheme != "https":
        missing.append("an https:// base URL")
    if not settings.api_key:
        missing.append(ENV_API_KEY)
    if missing:
        raise Forbidden(
            "sonder-inference endpoint %s is not loopback; remote inference "
            "requires %s" % (settings.display_base_url, ", ".join(missing))
        )


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
    except (UnicodeDecodeError, ValueError):
        return "", ""
    error = document.get("error") if isinstance(document, dict) else None
    if not isinstance(error, dict):
        return "", ""
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else "",
        _bounded(message) if isinstance(message, str) else "",
    )


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

    @property
    def api_version(self) -> object:
        return None if self.document is None else self.document.get("api_version")


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
            transport=transport,
            request_admission=request_admission,
            provider_label=PROVIDER_LABEL,
            extra_headers=correlation_headers,
            http_error_classifier=self._classify_http_error,
            connect_error_classifier=self._classify_connect_error,
            get_transport=get_transport,
        )
        self._settings_override = config
        self._env = env
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._health_cache: HealthSnapshot | None = None
        self._identity_cache: dict[tuple[str, str], tuple[float, IdentityObservation]] = {}
        # Settings of the call in flight on this thread, so the transport's
        # error classifiers can name the endpoint and update its health.
        self._call = threading.local()

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
        if not settings.loopback and not context.cloud_allowed:
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
    def _unreachable_message(settings: SonderInferenceConfig, detail: str) -> str:
        return (
            "Sonder Inference at %s is not reachable or not ready (%s); start it "
            "with `sonder-infer serve`, or set %s=ollama to send requests it "
            "never received to local Ollama"
            % (settings.display_base_url, detail, ENV_FALLBACK)
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
                "`sonder-infer serve`" % detail
            )
        return SonderInferenceUnreachable(self._unreachable_message(settings, detail))

    def _classify_http_error(self, status: int, body: bytes) -> SonderError | None:
        code, message = _error_fields(body)
        label = "HTTP %d%s" % (status, " %s" % code if code else "")
        suffix = ": %s" % message if message else ""
        if status == 503 and code == "not_ready":
            self._mark_unavailable("server not ready (HTTP 503 not_ready)", state="degraded")
            settings = getattr(self._call, "settings", None)
            detail = "HTTP 503 not_ready"
            if settings is None:
                return SonderInferenceUnreachable("Sonder Inference is not ready (%s)" % detail)
            return SonderInferenceUnreachable(self._unreachable_message(settings, detail))
        if status == 503 and code == "overloaded":
            return CapacityExceeded("sonder-inference is at its connection limit (%s)" % label)
        if status in (401, 403):
            return Forbidden(
                "sonder-inference rejected the request (%s); check %s%s"
                % (label, ENV_API_KEY, suffix)
            )
        if status == 429:
            return CapacityExceeded("sonder-inference scheduler rejected the request (%s)%s" % (label, suffix))
        if status in (400, 404, 405, 411, 413, 501):
            return InvalidInput("sonder-inference rejected the request (%s)%s" % (label, suffix))
        # 408, 500, 503 backend_unavailable and anything unexpected: the
        # request may have reached the backend, so it is never "unreachable".
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

    def health(
        self, *, settings: SonderInferenceConfig | None = None,
        timeout: float = HEALTH_PROBE_TIMEOUT_SECONDS, refresh: bool = False,
    ) -> HealthSnapshot:
        """Return cached health, probing ``/v1/sonder/health`` when stale."""
        settings = settings or self.settings()
        check_endpoint_policy(settings)
        now = self._monotonic()
        with self._lock:
            cached = self._health_cache
        if (not refresh and cached is not None and cached.base_url == settings.base_url
                and now - cached.checked_monotonic < settings.health_ttl_seconds):
            return cached
        snapshot = self._probe_health(settings, max(0.05, min(HEALTH_PROBE_TIMEOUT_SECONDS, timeout)))
        with self._lock:
            self._health_cache = snapshot
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
            status, document = self.get_json("/v1/sonder/health", timeout=timeout, cfg=cfg)
        except SonderInferenceUnreachable as exc:
            return snapshot("unavailable", str(exc))
        except DeadlineExceeded:
            return snapshot("unavailable", "health probe timed out after %.1fs" % timeout)
        except DependencyUnavailable as exc:
            return snapshot("unavailable", "health probe failed: %s" % exc)
        if status in (401, 403):
            return snapshot(
                "unavailable", "credentials rejected (HTTP %d); check %s" % (status, ENV_API_KEY),
                auth_rejected=True,
            )
        if document is None:
            return snapshot("unavailable", "health returned HTTP %d without a JSON document" % status)
        api = document.get("api_version")
        if api != API_VERSION:
            return snapshot(
                "unavailable",
                "incompatible sonder-inference API version %r (this runtime speaks %d)"
                % (api, API_VERSION),
                document=document, api_mismatch=True,
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
        snap = self.health(settings=settings, timeout=timeout)
        if snap.api_mismatch:
            raise DependencyUnavailable(snap.detail)
        if snap.auth_rejected:
            raise Forbidden("sonder-inference %s" % snap.detail)
        if snap.state != "ready":
            raise SonderInferenceUnreachable(self._unreachable_message(settings, snap.detail))
        return snap

    def capability_health(self) -> CapabilityHealth:
        """Provider-reported health from the cached, bounded probe."""
        try:
            snap = self.health()
        except SonderError as exc:
            return CapabilityHealth(
                provider=PROVIDER_ID, capabilities=frozenset({Capability.GENERATION}),
                healthy=False, checked_at=self._wall_clock(), detail=_bounded(exc),
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

    def provider_status(self) -> Mapping[str, Mapping[str, object]]:
        """Content-free status for doctor, the ecosystem route and the app."""
        entry: dict[str, object] = {key: None for key in STATUS_KEYS}
        entry.update(
            provider=PROVIDER_ID, state="unavailable", healthy=False,
            capabilities=sorted(CAPABILITIES), models=[], fallback=None,
            fallback_count=0,
        )
        try:
            settings = self.settings()
            entry["base_url"] = settings.display_base_url
            snap = self.health(settings=settings)
        except SonderError as exc:
            entry["detail"] = _bounded(exc)
            return {PROVIDER_ID: entry}
        document = snap.document or {}
        models = document.get("models") if isinstance(document.get("models"), list) else []
        synthetic = document.get("synthetic")
        entry.update(
            state=snap.state,
            healthy=snap.state == "ready",
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
        if snap.state == "ready":
            try:
                identity = self.backend_identity()
            except SonderError as exc:
                logger.info("sonder-inference identity unavailable: %s", _bounded(exc))
                identity = None
            entry["identity"] = identity.to_dict() if identity is not None else None
        return {PROVIDER_ID: entry}

    # -- generate ----------------------------------------------------------

    @staticmethod
    def _payload_options(options: Mapping[str, object]) -> dict[str, object]:
        for name in _UNSUPPORTED_OPTIONS:
            if options.get(name) not in (None, False, "", [], {}):
                raise InvalidInput("sonder-inference v1 does not support the %r option" % name)
        if options.get("think") not in (None, False):
            raise InvalidInput("sonder-inference v1 does not support thinking mode")
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
        return payload

    def _call_timeout(self, settings: SonderInferenceConfig, context: OperationContext) -> float:
        remaining = self._check_liveness(context)
        timeout = settings.timeout_seconds
        return timeout if remaining is None else min(timeout, remaining)

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        settings = self.settings()
        cfg = OpenAICompatibleConfig(
            base_url=settings.base_url, api_key=settings.api_key, model=settings.model,
        )
        self._enforce_consent(cfg, context)
        timeout = self._call_timeout(settings, context)
        options = dict(request.options or {})
        model = self.select_model(request, settings)
        payload = {
            "model": model,
            "messages": self._build_messages(request),
            "stream": False,
            **self._payload_options(options),
        }
        self._call.settings = settings
        self._require_ready(settings, timeout)
        timeout = self._call_timeout(settings, context)
        started = time.monotonic()
        data = self._post("/v1/chat/completions", payload, cfg, timeout, context=context)
        self._check_liveness(context, phase="during model call")
        extension = data.get("sonder")
        if isinstance(extension, dict) and "api_version" in extension:
            if extension["api_version"] != API_VERSION:
                raise DependencyUnavailable(
                    "incompatible sonder-inference API version %r (this runtime speaks %d)"
                    % (extension["api_version"], API_VERSION)
                )
        served = data.get("model")
        if not isinstance(served, str) or not served.strip() or served == DEFAULT_MODEL:
            raise DependencyUnavailable(
                "incompatible sonder-inference API: the response did not name the served model"
            )
        text = self._extract_text(data)
        usage = data.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise DependencyUnavailable("sonder-inference returned an invalid usage object")
        timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
        telemetry = from_openai_compatible(data)
        prompt_count = usage.get("prompt_tokens")
        if prompt_count is None:
            prompt_count = timings.get("prompt_n")
        output_count = usage.get("completion_tokens")
        if output_count is None:
            output_count = timings.get("predicted_n")
        response = ModelResponse(
            text=require_model_text(text),
            model=served,
            tier=request.tier or PROVIDER_ID,
            duration_ms=int((time.monotonic() - started) * 1000),
            tokens_in=optional_token_count(prompt_count, "prompt token count"),
            tokens_out=optional_token_count(output_count, "completion token count"),
            telemetry=telemetry,
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
    "HealthSnapshot",
    "IdentityObservation",
    "PROVIDER_ID",
    "PROVIDER_LABEL",
    "STATUS_KEYS",
    "SonderInferenceConfig",
    "SonderInferenceGateway",
    "SonderInferenceUnreachable",
    "WORKLOAD_BY_SOURCE",
    "check_endpoint_policy",
    "config_from_env",
    "correlation_headers",
    "is_loopback_url",
    "normalize_base_url",
    "read_ready_file",
]
