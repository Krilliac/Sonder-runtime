"""OpenRouter (https://openrouter.ai) as a hosted ModelGateway provider.

OpenRouter speaks the OpenAI chat-completions wire format at
``https://openrouter.ai/api/v1``, so this adapter reuses
:class:`OpenAICompatibleGateway` -- one HTTP client, one error taxonomy, one
evidence-capture path -- and adds what a generic peer cannot know:

* **Consent.** OpenRouter is always a cloud provider, whatever its base URL:
  every request (model listing and account reads included) needs the
  explicit ``SONDER_ALLOW_CLOUD`` opt-in, and prompt-bearing calls also need
  an OperationContext that allows cloud.  It is off by default and nothing
  routes to it unless a tier is bound with ``SONDER_<TIER>_PROVIDER=openrouter``
  (or ``SONDER_MODEL_BACKEND=openrouter``).
* **Credentials.** The key comes from ``OPENROUTER_API_KEY`` in the process
  environment, read per call.  It is sent only as the
  bearer header to the configured https endpoint and never appears in a log
  line, error, status document, telemetry label or export.
* **Model selection.** An explicit ``model`` request option, else
  ``SONDER_OPENROUTER_TIER_MODELS``, else the runtime policy's
  ``provider_models.openrouter`` map (``openrouter use <tier> <model>``), else
  ``SONDER_OPENROUTER_MODEL``.  Ids are validated as ``vendor/model[:variant]``.
* **Routing preferences.** OpenRouter's ``provider`` object is sent on every
  chat request: privacy-first defaults (``data_collection: "deny"``,
  ``zdr: true``, ``allow_fallbacks: true``), then ``SONDER_OPENROUTER_PROVIDER``
  (JSON) and ``SONDER_OPENROUTER_PROVIDER_ORDER`` (comma list), then
  ``SONDER_OPENROUTER_TIER_PROVIDER`` (JSON ``{tier: {...}}``).
* **Accounting.** OpenRouter returns ``usage.cost`` (USD) and token details on
  every response; they are recorded per call with ``backend=openrouter`` and
  the upstream host OpenRouter reports.
* **Errors.** 401 (key), 402 (insufficient credits), 429 (rate limited; the
  ``Retry-After`` delay is honoured as a local cool-down before the next
  send) and 5xx map onto the domain taxonomy with actionable text.  Calls
  stay single-attempt: metered work is never silently retried.

Discovery (:meth:`OpenRouterGateway.list_models`, :meth:`account`) backs the
``python -m sonder_runtime openrouter`` CLI and the read-only MCP tools.
Neither ever sends a prompt, and nothing probes the paid API automatically.
"""
from __future__ import annotations

import contextvars
import http.client
import ipaddress
import json
import logging
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit

from ...application.context import OperationContext
from ...application.model_batching import generate_batch as batch_generate
from ...application.ports.model_gateway import (
    Embedding,
    InferenceTelemetry,
    ModelBatchOutcome,
    ModelRequest,
    ModelResponse,
    optional_token_count,
    require_model_text,
)
from ...application.ports.model_gateway_contract import (
    Capability,
    CapabilityHealth,
    GenerationChunk,
)
from ...domain.cloud_access import cloud_allowed as cloud_opted_in
from ...domain.common.errors import (
    Cancelled,
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
from ...domain.openrouter_policy import (
    OpenRouterPolicyError,
    effective_provider_preferences,
    filter_models,
    model_summary,
    normalize_provider_preferences,
    validate_model_id,
)
from ...domain.security.redaction import redact_text
from ...platform.metrics import default_registry
from ...platform.runtime_threads import Thread as owned_runtime_thread
from ..model_request_admission import HostModelRequestAdmission
from ..provider_bindings import PROVIDER_TIERS
from .openai_compat_gateway import (
    ERROR_BODY_LIMIT,
    POST_BODY_LIMIT,
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
    _opener_for,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "openrouter"
PROVIDER_LABEL = "openrouter"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 300.0
DISCOVERY_TIMEOUT_SECONDS = 20.0
# The full public catalog is a few MB of JSON; bounded, never unbounded.
MODELS_BODY_LIMIT = 16 * 1024 * 1024
ACCOUNT_BODY_LIMIT = 65_536
_STREAM_LINE_LIMIT = 1024 * 1024
_STREAM_BUFFER_CHUNKS = 64
_STREAM_POLL_SECONDS = 0.025
MAX_COOLDOWN_SECONDS = 600.0
DETAIL_LIMIT = 240

ENV_API_KEY = "OPENROUTER_API_KEY"
ENV_BASE_URL = "SONDER_OPENROUTER_BASE_URL"
ENV_MODEL = "SONDER_OPENROUTER_MODEL"
ENV_TIER_MODELS = "SONDER_OPENROUTER_TIER_MODELS"
ENV_PROVIDER = "SONDER_OPENROUTER_PROVIDER"
ENV_PROVIDER_ORDER = "SONDER_OPENROUTER_PROVIDER_ORDER"
ENV_TIER_PROVIDER = "SONDER_OPENROUTER_TIER_PROVIDER"
ENV_TIMEOUT = "SONDER_OPENROUTER_TIMEOUT_SECONDS"
ENV_APP_URL = "SONDER_OPENROUTER_APP_URL"
ENV_APP_TITLE = "SONDER_OPENROUTER_APP_TITLE"
ENV_ALLOW_CLOUD = "SONDER_ALLOW_CLOUD"
CREDITS_URL = "https://openrouter.ai/settings/credits"

CAPABILITIES = frozenset({GATEWAY_CAPABILITY_CHAT, GATEWAY_CAPABILITY_FIXED_ENDPOINT})
STATUS_KEYS = (
    "provider", "state", "healthy", "detail", "capabilities", "base_url",
    "cloud_enabled", "api_key_configured", "tier_models", "provider_preferences",
)
# Ollama-style option -> OpenAI/OpenRouter request field.  Only options the
# caller set are forwarded; OpenRouter applies the model's own defaults.
_FORWARDED_OPTIONS = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "seed": "seed",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "repeat_penalty": "repetition_penalty",
    "num_predict": "max_tokens",
}
_INTEGER_OPTIONS = frozenset({"top_k", "seed", "num_predict"})
# Local-only hints with no OpenRouter meaning; dropping them changes nothing
# about what the hosted model does.
_LOCAL_ONLY_OPTIONS = frozenset({"num_ctx", "repeat_last_n", "keep_alive", "num_gpu"})
# This gateway returns text only, so native tool calls cannot be carried.
_UNSUPPORTED_OPTIONS = ("tools", "tool_choice", "functions")


class OpenRouterCreditsExhausted(Forbidden):
    """HTTP 402: the account or key has no credits left for this request."""


class OpenRouterRateLimited(CapacityExceeded):
    """HTTP 429 (or a live Retry-After cool-down); ``retry_after`` in seconds."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _bounded(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= DETAIL_LIMIT else value[: DETAIL_LIMIT - 3] + "..."


def is_loopback_url(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_base_url(value: str) -> str:
    """``https://host[:port]/…/v1``; plain http only for a loopback test server."""
    raw = str(value or "").strip().rstrip("/")
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        parts.port  # noqa: B018 - validates the port
    except ValueError as exc:
        raise InvalidInput("%s is not a valid URL" % ENV_BASE_URL) from exc
    if (parts.scheme not in ("http", "https") or not host or parts.username
            or parts.password or parts.query or parts.fragment):
        raise InvalidInput(
            "%s must be https://host[:port]/path/v1 without credentials, "
            "query or fragment" % ENV_BASE_URL
        )
    if parts.scheme != "https" and not is_loopback_url(raw):
        raise InvalidInput(
            "%s must use https:// (plain http is accepted only for a loopback "
            "test server)" % ENV_BASE_URL
        )
    if not parts.path.endswith("/v1"):
        raise InvalidInput("%s must end in /v1 (for example %s)" % (ENV_BASE_URL, DEFAULT_BASE_URL))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _parse_tier_models(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (part.strip() for part in str(raw or "").split(",")):
        if not item:
            continue
        tier, sep, model = item.partition("=")
        tier = tier.strip().lower()
        if not sep or not tier or not model.strip():
            raise InvalidInput("%s entries must look like tier=vendor/model" % ENV_TIER_MODELS)
        if tier not in PROVIDER_TIERS:
            raise InvalidInput(
                "%s names unknown tier %r (tiers: %s)"
                % (ENV_TIER_MODELS, tier, ", ".join(PROVIDER_TIERS))
            )
        if tier in result:
            raise InvalidInput("%s names tier %r twice" % (ENV_TIER_MODELS, tier))
        try:
            result[tier] = validate_model_id(model, "%s tier %r" % (ENV_TIER_MODELS, tier))
        except OpenRouterPolicyError as exc:
            raise InvalidInput(str(exc)) from exc
    return result


def _json_env(source: Mapping[str, str], name: str) -> object:
    raw = str(source.get(name, "") or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise InvalidInput("%s must be a JSON object" % name) from exc


def _provider_env(source: Mapping[str, str]) -> tuple[dict, dict]:
    try:
        default = normalize_provider_preferences(_json_env(source, ENV_PROVIDER), ENV_PROVIDER)
        order = str(source.get(ENV_PROVIDER_ORDER, "") or "").strip()
        if order:
            default["order"] = normalize_provider_preferences(
                {"order": [item for item in order.split(",") if item.strip()]},
                ENV_PROVIDER_ORDER,
            )["order"]
        raw_tiers = _json_env(source, ENV_TIER_PROVIDER)
        if raw_tiers is not None and not isinstance(raw_tiers, dict):
            raise InvalidInput("%s must be a JSON object {tier: {...}}" % ENV_TIER_PROVIDER)
        tiers: dict[str, dict] = {}
        for tier, prefs in (raw_tiers or {}).items():
            if tier not in PROVIDER_TIERS:
                raise InvalidInput(
                    "%s names unknown tier %r (tiers: %s)"
                    % (ENV_TIER_PROVIDER, tier, ", ".join(PROVIDER_TIERS))
                )
            tiers[tier] = normalize_provider_preferences(prefs, "%s.%s" % (ENV_TIER_PROVIDER, tier))
    except OpenRouterPolicyError as exc:
        raise InvalidInput(str(exc)) from exc
    return default, tiers


def _float_env(source: Mapping[str, str], name: str, default: float) -> float:
    raw = str(source.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise InvalidInput("%s must be a number" % name) from exc
    if not 0.0 < value <= 86_400.0:
        raise InvalidInput("%s must be in (0, 86400]" % name)
    return value


@dataclass(frozen=True)
class OpenRouterSettings:
    """Resolved provider settings; build with :func:`config_from_env`."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = field(default="", repr=False)
    model: str = ""
    tier_models: Mapping[str, str] = field(default_factory=dict)
    tier_model_sources: Mapping[str, str] = field(default_factory=dict)
    provider_default: Mapping[str, object] = field(default_factory=dict)
    provider_tiers: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    cloud_enabled: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    app_url: str = ""
    app_title: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        if self.model:
            try:
                validate_model_id(self.model, ENV_MODEL)
            except OpenRouterPolicyError as exc:
                raise InvalidInput(str(exc)) from exc
        object.__setattr__(self, "tier_models", MappingProxyType(dict(self.tier_models)))
        object.__setattr__(self, "tier_model_sources", MappingProxyType(dict(self.tier_model_sources)))
        object.__setattr__(self, "provider_default", MappingProxyType(dict(self.provider_default)))
        object.__setattr__(self, "provider_tiers", MappingProxyType(
            {k: MappingProxyType(dict(v)) for k, v in dict(self.provider_tiers).items()}
        ))
        for header, value in (("app url", self.app_url), ("app title", self.app_title)):
            if any(ch in str(value) for ch in "\r\n") or len(str(value)) > 200:
                raise InvalidInput("OpenRouter %s must be one short line" % header)

    @property
    def api_root(self) -> str:
        """The base without its trailing ``/v1`` (the shared transport adds it)."""
        return self.base_url[: -len("/v1")]

    @property
    def display_base_url(self) -> str:
        parts = urlsplit(self.base_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def model_for_tier(self, tier: str) -> str:
        return self.tier_models.get(str(tier or ""), self.model)

    def provider_preferences(self, tier: str) -> dict[str, object]:
        return effective_provider_preferences(
            self.provider_default, self.provider_tiers.get(str(tier or "")),
        )


def _policy_tier_models() -> dict[str, str]:
    """``provider_models.openrouter`` from the shared runtime policy file."""
    try:
        from ..runtime_policy import load as load_policy

        policy = load_policy(create=False)
        return dict((policy.get("provider_models") or {}).get(PROVIDER_ID) or {})
    except Exception as exc:  # noqa: BLE001 - a broken policy never breaks env config
        logger.warning("openrouter: runtime policy tier models unavailable: %s", type(exc).__name__)
        return {}


def config_from_env(
    env: Mapping[str, str] | None = None,
    *, policy_models: Callable[[], Mapping[str, str]] | None = _policy_tier_models,
) -> OpenRouterSettings:
    """Resolve settings lazily (never at import); env tier models win over policy."""
    source = os.environ if env is None else env
    env_tiers = _parse_tier_models(str(source.get(ENV_TIER_MODELS, "") or ""))
    policy_tiers: dict[str, str] = {}
    for tier, model in dict(policy_models() if policy_models else {}).items():
        try:
            if tier in PROVIDER_TIERS and str(model or "").strip():
                policy_tiers[tier] = validate_model_id(model, "runtime policy openrouter.%s" % tier)
        except OpenRouterPolicyError as exc:
            logger.warning("openrouter: ignoring invalid runtime policy model: %s", exc)
    tier_models = {**policy_tiers, **env_tiers}
    sources = {tier: "runtime_policy" for tier in policy_tiers}
    sources.update({tier: "env" for tier in env_tiers})
    default, tiers = _provider_env(source)
    return OpenRouterSettings(
        base_url=str(source.get(ENV_BASE_URL, "") or "").strip() or DEFAULT_BASE_URL,
        api_key=str(source.get(ENV_API_KEY, "") or "").strip(),
        model=str(source.get(ENV_MODEL, "") or "").strip(),
        tier_models=tier_models,
        tier_model_sources=sources,
        provider_default=default,
        provider_tiers=tiers,
        cloud_enabled=cloud_opted_in(source),
        timeout_seconds=_float_env(source, ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS),
        app_url=str(source.get(ENV_APP_URL, "") or "").strip(),
        app_title=str(source.get(ENV_APP_TITLE, "") or "").strip(),
    )


def check_cloud_consent(settings: OpenRouterSettings) -> None:
    if not settings.cloud_enabled:
        raise Forbidden(
            "OpenRouter is a hosted, metered provider: prompts and the API key "
            "leave this machine. It stays off until cloud is enabled: set "
            "%s=1 for the runtime (or run cloud_opt_in on)" % ENV_ALLOW_CLOUD
        )


def check_endpoint_policy(settings: OpenRouterSettings, *, require_key: bool = True) -> None:
    """Cloud opt-in, then a key; applies to every request, probes included."""
    check_cloud_consent(settings)
    if require_key and not settings.api_key:
        raise InvalidInput(
            "OpenRouter API key is not configured: set the %s environment "
            "variable (for example as a Windows user environment variable) and "
            "restart Sonder" % ENV_API_KEY
        )


def _retry_after_seconds(headers: object) -> float | None:
    try:
        raw = headers.get("Retry-After") if headers is not None else None  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - a header we cannot read is simply absent
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None  # an HTTP-date is not worth trusting a clock over
    if value != value or value < 0:
        return None
    return min(value, MAX_COOLDOWN_SECONDS)


def _error_fields(body: bytes) -> tuple[str, str]:
    """``(message, error_type)`` from ``{"error": {"message", "metadata"}}``."""
    try:
        document = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "", ""
    error = document.get("error") if isinstance(document, dict) else None
    if not isinstance(error, dict):
        return "", ""
    message = error.get("message")
    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    kind = metadata.get("error_type")
    return (
        _bounded(message) if isinstance(message, str) else "",
        kind if isinstance(kind, str) and len(kind) <= 64 else "",
    )


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000_000:
        return None
    return value


def usage_facts(data: Mapping[str, object], *, requested_model: str = "") -> dict[str, object]:
    """Content-free accounting facts from one OpenRouter response."""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost <= 1_000_000:
        cost = None
    upstream = data.get("provider")
    served = data.get("model")
    return {
        "model": served if isinstance(served, str) and served.strip() else requested_model,
        "upstream_provider": _bounded(upstream) if isinstance(upstream, str) and upstream.strip() else None,
        "cost_usd": cost,
        "prompt_tokens": _count(usage.get("prompt_tokens")),
        "completion_tokens": _count(usage.get("completion_tokens")),
        "cached_tokens": _count(prompt_details.get("cached_tokens")),
        "cache_write_tokens": _count(prompt_details.get("cache_write_tokens")),
        "reasoning_tokens": _count(completion_details.get("reasoning_tokens")),
    }


def _telemetry(facts: Mapping[str, object]) -> InferenceTelemetry | None:
    prompt = facts.get("prompt_tokens")
    cached = facts.get("cached_tokens")
    if not isinstance(prompt, int) or not isinstance(cached, int) or cached > prompt:
        cached, uncached = None, None
    else:
        uncached = prompt - cached
    telemetry = InferenceTelemetry(
        prompt_tokens=prompt if isinstance(prompt, int) else None,
        prompt_cached_tokens=cached,
        prompt_uncached_tokens=uncached,
        output_tokens=facts.get("completion_tokens"),  # type: ignore[arg-type]
    )
    return telemetry if any(value is not None for value in telemetry.__dict__.values()) else None


def record_usage(facts: Mapping[str, object]) -> None:
    """Export one call's usage: metrics (bounded labels) and one INFO line."""
    registry = default_registry()
    registry.observe_inference(PROVIDER_ID, _telemetry(facts))
    registry.observe_provider_usage(
        PROVIDER_ID, upstream=facts.get("upstream_provider"), cost_usd=facts.get("cost_usd"),
        tokens={
            "prompt": facts.get("prompt_tokens"),
            "completion": facts.get("completion_tokens"),
            "cached": facts.get("cached_tokens"),
            "cache_write": facts.get("cache_write_tokens"),
            "reasoning": facts.get("reasoning_tokens"),
        },
    )
    logger.info(
        "openrouter usage: model=%s upstream=%s cost_usd=%s prompt_tokens=%s "
        "completion_tokens=%s cached_tokens=%s",
        facts.get("model"), facts.get("upstream_provider") or "unknown",
        facts.get("cost_usd"), facts.get("prompt_tokens"),
        facts.get("completion_tokens"), facts.get("cached_tokens"),
    )


def _default_get(url: str, headers: dict, timeout: float, limit: int) -> tuple[int, bytes, Mapping[str, str]]:
    """Non-redirected, bounded GET; non-2xx is returned with its headers."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener_for(url).open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
            return int(response.status), body, dict(response.headers or {})
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(ERROR_BODY_LIMIT)
        except Exception:  # noqa: BLE001 - the status alone still answers
            body = b""
        finally:
            exc.close()
        return int(exc.code), body if isinstance(body, bytes) else b"", dict(exc.headers or {})


def _default_stream(url: str, payload: dict, headers: dict, timeout: float) -> Iterator[bytes]:
    """POST and yield raw SSE lines; non-2xx raises ``HTTPError``."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with _opener_for(url).open(request, timeout=timeout) as response:
        total = 0
        while True:
            line = response.readline(_STREAM_LINE_LIMIT + 1)
            if not line:
                return
            total += len(line)
            if len(line) > _STREAM_LINE_LIMIT or total > POST_BODY_LIMIT:
                raise DependencyUnavailable("OpenRouter stream exceeds its size bound")
            yield line


class OpenRouterGateway(OpenAICompatibleGateway):
    """ModelGateway over OpenRouter's OpenAI-compatible API."""

    def __init__(
        self, config: OpenRouterSettings | None = None, *,
        transport=None, get_transport=None, stream_transport=None,
        request_admission: HostModelRequestAdmission | None = None,
        env: Mapping[str, str] | None = None,
        policy_models: Callable[[], Mapping[str, str]] | None = _policy_tier_models,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._raw_post = transport or OpenAICompatibleGateway._default_transport
        self._raw_get = get_transport or _default_get
        self._raw_stream = stream_transport or _default_stream
        super().__init__(
            None,
            transport=self._post_transport,
            request_admission=request_admission,
            provider_label=PROVIDER_LABEL,
            extra_headers=self._attribution_headers,
            http_error_classifier=self._classify_http_error,
        )
        self._settings_override = config
        self._env = env
        self._policy_models = policy_models
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._call = threading.local()
        self.last_usage: Mapping[str, object] | None = None

    # -- configuration & consent ------------------------------------------

    @property
    def capabilities(self) -> frozenset[str]:
        return CAPABILITIES

    def settings(self) -> OpenRouterSettings:
        """Resolve the current settings (env and policy are read per call)."""
        return self._settings_override or config_from_env(self._env, policy_models=self._policy_models)

    def _cfg(self, settings: OpenRouterSettings, model: str = "") -> OpenAICompatibleConfig:
        return OpenAICompatibleConfig(base_url=settings.api_root, api_key=settings.api_key, model=model)

    def _resolved_config(self) -> OpenAICompatibleConfig:
        settings = self.settings()
        return self._cfg(settings, settings.model)

    @staticmethod
    def _is_loopback(base_url: str) -> bool:
        return is_loopback_url(base_url)

    def _enforce_consent(self, cfg: OpenAICompatibleConfig, context: OperationContext) -> None:
        """Always cloud: the opt-in, a key and a cloud-allowing context."""
        del cfg
        check_endpoint_policy(self.settings())
        if not context.cloud_allowed:
            raise Forbidden(
                "OpenRouter is a hosted provider and this operation context does "
                "not allow prompts to leave the machine"
            )

    def _attribution_headers(self, context: OperationContext) -> dict[str, str]:
        del context
        settings = getattr(self._call, "settings", None) or self.settings()
        headers: dict[str, str] = {}
        if settings.app_url:
            headers["HTTP-Referer"] = settings.app_url
        if settings.app_title:
            headers["X-Title"] = settings.app_title
        return headers

    def select_model(self, request: ModelRequest, settings: OpenRouterSettings) -> str:
        """Explicit ``model`` option, else the tier map, else the default."""
        explicit = (request.options or {}).get("model")
        if explicit is not None:
            try:
                return validate_model_id(explicit, "model option")
            except OpenRouterPolicyError as exc:
                raise InvalidInput(str(exc)) from exc
        model = settings.model_for_tier(request.tier)
        if not model:
            raise InvalidInput(
                "no OpenRouter model is configured for tier %r: run `python -m "
                "sonder_runtime openrouter use %s <vendor/model>`, or set %s or %s"
                % (request.tier, request.tier if request.tier in PROVIDER_TIERS else "<tier>",
                   ENV_TIER_MODELS, ENV_MODEL)
            )
        return model

    # -- errors and cool-down ----------------------------------------------

    def _post_transport(self, url: str, payload: dict, headers: dict, timeout) -> dict:
        self._call.retry_after = None
        try:
            if payload.get("stream") is True:
                data = self._stream_send(url, payload, headers, timeout)
            else:
                data = self._raw_post(url, payload, headers, timeout)
            # A completed physical response can be billable even when later
            # cancellation or evidence persistence prevents publishing it.
            # This is the sole accounting boundary, shared by stream/generate.
            if isinstance(data, dict):
                facts = self._sanitized_usage(data, str(payload.get("model", "")))
                self.last_usage = MappingProxyType(dict(facts))
                record_usage(facts)
            return data
        except urllib.error.HTTPError as exc:
            self._call.retry_after = _retry_after_seconds(getattr(exc, "headers", None))
            raise

    def _redacted(self, text: str) -> str:
        settings = getattr(self._call, "settings", None)
        secrets = (settings.api_key,) if settings is not None and settings.api_key else ()
        return _bounded(redact_text(text, secret_values=secrets))

    def _sanitized_usage(self, data: dict, model: str) -> dict[str, object]:
        facts = usage_facts(data, requested_model=model)
        for label in ("model", "upstream_provider"):
            value = data.get("provider") if label == "upstream_provider" else facts[label]
            if isinstance(value, str) and value.strip():
                # Redact before bounding, including keys crossing the limit.
                facts[label] = self._redacted(value)
        return facts

    def _classify_http_error(self, status: int, body: bytes) -> SonderError | None:
        message, kind = _error_fields(body)
        message = self._redacted(message) if message else ""
        suffix = ": %s" % message if message else ""
        retry_after = getattr(self._call, "retry_after", None)
        self._call.retry_after = None
        if status == 401:
            return Forbidden("OpenRouter rejected the API key (HTTP 401); check %s" % ENV_API_KEY)
        if status == 402:
            return OpenRouterCreditsExhausted(
                "insufficient OpenRouter credits (HTTP 402)%s; add credits at %s, "
                "lower the request's max tokens, or choose a cheaper or :free model"
                % (suffix, CREDITS_URL)
            )
        if status == 403:
            return Forbidden("OpenRouter refused the request (HTTP 403: permission, "
                             "guardrail or moderation)%s" % suffix)
        if status == 408:
            return DeadlineExceeded("OpenRouter timed out the request (HTTP 408)%s" % suffix)
        if status == 429:
            self._start_cooldown(retry_after)
            wait = (" retry after %.0fs" % retry_after) if retry_after is not None else " retry later"
            return OpenRouterRateLimited(
                "OpenRouter is rate limiting this key (HTTP 429)%s;%s" % (suffix, wait),
                retry_after=retry_after,
            )
        if status == 404:
            return InvalidInput(
                "OpenRouter found no endpoint for this request (HTTP 404)%s; check "
                "the model id, or relax the provider preferences (zdr / "
                "data_collection / only) for that tier" % suffix
            )
        if status in (400, 413, 422):
            return InvalidInput("OpenRouter rejected the request (HTTP %d)%s" % (status, suffix))
        if status == 503:
            return DependencyUnavailable(
                "no OpenRouter provider can serve this request (HTTP 503)%s; relax "
                "the provider preferences or choose another model" % suffix
            )
        return DependencyUnavailable("OpenRouter failed the request (HTTP %d)%s%s" % (
            status, suffix, " [%s]" % kind if kind else "",
        ))

    def _start_cooldown(self, retry_after: float | None) -> None:
        if retry_after is None or retry_after <= 0:
            return
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._monotonic() + retry_after)

    def _check_cooldown(self) -> None:
        with self._lock:
            remaining = self._cooldown_until - self._monotonic()
        if remaining > 0:
            raise OpenRouterRateLimited(
                "OpenRouter asked this key to wait (Retry-After); not sending for "
                "another %.0fs" % remaining, retry_after=remaining,
            )

    # -- request building --------------------------------------------------

    @staticmethod
    def _payload_options(options: Mapping[str, object]) -> dict[str, object]:
        for name in _UNSUPPORTED_OPTIONS:
            if options.get(name) not in (None, False, "", [], {}):
                raise InvalidInput("the OpenRouter gateway returns text only and does not carry %r" % name)
        if options.get("think") not in (None, False):
            raise InvalidInput("the OpenRouter gateway does not carry thinking mode")
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
        fmt = options.get("format")
        if fmt == "json":
            payload["response_format"] = {"type": "json_object"}
        elif isinstance(fmt, dict) and fmt:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": fmt},
            }
        elif fmt not in (None, "", False):
            raise InvalidInput("option 'format' must be \"json\" or a JSON schema object")
        if isinstance(options.get("response_format"), dict):
            payload["response_format"] = dict(options["response_format"])
        return payload

    def build_payload(
        self, request: ModelRequest, settings: OpenRouterSettings, model: str, *, stream: bool,
    ) -> dict[str, object]:
        options = dict(request.options or {})
        try:
            override = normalize_provider_preferences(options.get("provider"), "provider option")
        except OpenRouterPolicyError as exc:
            raise InvalidInput(str(exc)) from exc
        provider = settings.provider_preferences(request.tier)
        provider.update(override)
        payload: dict[str, object] = {
            "model": model,
            "messages": self._build_messages(request),
            "stream": stream,
            "provider": provider,
            **self._payload_options(options),
        }
        return payload

    def _call_timeout(self, settings: OpenRouterSettings, context: OperationContext) -> float:
        remaining = self._check_liveness(context)
        return settings.timeout_seconds if remaining is None else min(settings.timeout_seconds, remaining)

    def _prepare(self, request: ModelRequest, context: OperationContext, *, stream: bool):
        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        settings = self.settings()
        self._call.settings = settings
        self._enforce_consent(self._cfg(settings), context)
        model = self.select_model(request, settings)
        payload = self.build_payload(request, settings, model, stream=stream)
        self._check_cooldown()
        return settings, model, payload

    # -- generate ----------------------------------------------------------

    def _finish(self, data: dict, request: ModelRequest, model: str, started: float) -> ModelResponse:
        text = self._extract_text(data)
        usage = data.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise DependencyUnavailable("OpenRouter returned an invalid usage object")
        facts = self._sanitized_usage(data, model)
        return ModelResponse(
            text=require_model_text(text),
            model=str(facts["model"]),
            tier=request.tier or PROVIDER_ID,
            duration_ms=int((time.monotonic() - started) * 1000),
            tokens_in=optional_token_count((usage or {}).get("prompt_tokens"), "prompt token count"),
            tokens_out=optional_token_count((usage or {}).get("completion_tokens"), "completion token count"),
            telemetry=_telemetry(facts),
        )

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        settings, model, payload = self._prepare(request, context, stream=False)
        timeout = self._call_timeout(settings, context)
        started = time.monotonic()
        data = self._post("/v1/chat/completions", payload, self._cfg(settings, model), timeout, context=context)
        self._check_liveness(context, phase="during model call")
        return self._finish(data, request, model, started)

    def generate_batch(
        self, requests: Sequence[ModelRequest], context: OperationContext,
        *, max_workers: int = 2,
    ) -> tuple[ModelBatchOutcome, ...]:
        """Up to 64 independent completions, ordered by input; never retried.

        Validate model/options for every item before admitting the first send.
        Each admitted item still traverses generate's per-call consent,
        physical rate admission, deadline, error mapping and accounting.
        """
        def validate(request: ModelRequest) -> None:
            try:
                settings = self.settings()
                model = self.select_model(request, settings)
                payload = self.build_payload(request, settings, model, stream=False)
                # Exercise the real wire encoder before any sibling can bill;
                # additionally reject nonfinite JSON numbers in batch inputs.
                json.dumps(payload, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                raise InvalidInput("invalid OpenRouter batch request configuration") from exc

        return batch_generate(self, requests, context, max_workers=max_workers,
                              validate_request=validate)

    # -- stream ------------------------------------------------------------

    def _stream_send(self, url: str, payload: dict, headers: dict, timeout) -> dict:
        """Read SSE deltas into the caller's sink; return the assembled reply.

        Returning an ordinary chat-completion object keeps evidence capture
        and telemetry identical to a non-streamed call.
        """
        sink = getattr(self._call, "sink", None)
        stop = getattr(self._call, "stop", None)
        parts: list[str] = []
        final: dict[str, object] = {}
        finish_reason = None
        headers = {**headers, "Accept": "text/event-stream"}
        raw_stream = self._raw_stream(url, payload, headers, timeout)
        try:
            for raw in raw_stream:
                if stop is not None and stop.is_set():
                    raise Cancelled("OpenRouter stream abandoned by its consumer")
                line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue  # blank separators and ": OPENROUTER PROCESSING" keep-alives
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError as exc:
                    raise DependencyUnavailable("OpenRouter sent a malformed stream event") from exc
                if not isinstance(event, dict):
                    continue
                if isinstance(event.get("error"), dict):
                    message, _kind = _error_fields(json.dumps({"error": event["error"]}).encode())
                    raise DependencyUnavailable(
                        "OpenRouter stream failed mid-response%s"
                        % (": %s" % self._redacted(message) if message else "")
                    )
                for key in ("id", "model", "provider", "usage"):
                    if key in event:
                        final[key] = event[key]
                choices = event.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        parts.append(text)
                        if sink is not None:
                            sink(GenerationChunk(text=text))
                    if isinstance(choices[0].get("finish_reason"), str):
                        finish_reason = choices[0]["finish_reason"]
        finally:
            close = getattr(raw_stream, "close", None)
            if callable(close):
                close()
        final.update({
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": "".join(parts)}}],
        })
        return final

    def stream(self, request: ModelRequest, context: OperationContext) -> Iterator[GenerationChunk]:
        """Yield text deltas, then one final chunk with finish data and usage.

        The HTTP exchange runs on a worker thread through the same ``_post``
        path as :meth:`generate` (admission, consent, evidence capture, error
        mapping); the consumer abandoning the iterator stops the read.
        """
        settings, model, payload = self._prepare(request, context, stream=True)
        timeout = self._call_timeout(settings, context)
        events: queue.Queue = queue.Queue(maxsize=_STREAM_BUFFER_CHUNKS)
        stop = threading.Event()
        finished = threading.Event()
        terminal: list[tuple[str, object]] = []

        def publish(chunk: GenerationChunk) -> None:
            # A slow consumer must backpressure SSE reads without trapping a
            # worker forever when the full iterator is closed or cancelled.
            while True:
                if stop.is_set():
                    raise Cancelled("OpenRouter stream abandoned by its consumer")
                self._check_liveness(context, phase="during model stream")
                try:
                    events.put(chunk, timeout=_STREAM_POLL_SECONDS)
                    return
                except queue.Full:
                    continue

        def worker() -> None:
            self._call.settings = settings
            self._call.sink = publish
            self._call.stop = stop
            try:
                data = self._post("/v1/chat/completions", payload, self._cfg(settings, model),
                                  timeout, context=context)
                terminal.append(("done", data))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the consumer thread
                terminal.append(("error", exc))
            finally:
                self._call.sink = None
                self._call.stop = None
                # Terminal state must never compete with chunks for capacity.
                finished.set()
                try:
                    events.put_nowait(None)  # wake an idle consumer, never wait
                except queue.Full:
                    pass  # the consumer discovers terminal state after drain

        started = time.monotonic()
        thread = owned_runtime_thread(
            target=contextvars.copy_context().run, args=(worker,),
            name="openrouter-stream", daemon=True,
        )
        thread.start()
        try:
            while True:
                self._check_liveness(context, phase="during model stream")
                try:
                    chunk = events.get(timeout=0 if finished.is_set() else _STREAM_POLL_SECONDS)
                except queue.Empty:
                    if not finished.is_set():
                        continue
                    # A producer can publish between get() timing out and
                    # signalling completion. Drain that chunk before terminal.
                    try:
                        chunk = events.get_nowait()
                    except queue.Empty:
                        kind, value = terminal[0]
                    else:
                        if chunk is not None:
                            yield chunk
                            continue
                        kind, value = terminal[0]
                else:
                    if chunk is not None:
                        yield chunk
                        continue
                    kind, value = terminal[0]
                if kind == "error":
                    raise value
                else:
                    response = self._finish(value, request, model, started)
                    choices = value.get("choices") or [{}]
                    yield GenerationChunk(
                        text="", finish_reason=choices[0].get("finish_reason"),
                        input_tokens=response.tokens_in, output_tokens=response.tokens_out,
                    )
                    return
        finally:
            stop.set()
            # Queue-blocked workers drain promptly. A transport blocked in a
            # socket read remains subject to its existing per-call timeout;
            # closing an iterator must not wait that entire timeout itself.
            thread.join(timeout=0.25)

    # -- embed -------------------------------------------------------------

    def embed(self, texts: Sequence[str], context: OperationContext) -> Sequence[Embedding]:
        del texts, context
        raise DependencyUnavailable(
            "the OpenRouter gateway does not serve embeddings; set "
            "SONDER_EMBEDDING_PROVIDER=ollama"
        )

    # -- discovery (never sends a prompt) -----------------------------------

    def _get(self, settings: OpenRouterSettings, path: str, *, timeout: float,
             limit: int, auth: bool = True) -> tuple[int, object]:
        cfg = self._cfg(settings)
        self._require_secure_transport(cfg)
        self._call.settings = settings
        headers = self._headers(cfg if auth else OpenAICompatibleConfig(base_url=cfg.base_url))
        headers.pop("Content-Type", None)
        headers["Accept"] = "application/json"
        url = settings.base_url + path
        try:
            status, body, response_headers = self._raw_get(url, headers, float(timeout), limit)
        except (socket.timeout, TimeoutError) as exc:
            raise DeadlineExceeded("OpenRouter timed out") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise DeadlineExceeded("OpenRouter timed out") from exc
            raise DependencyUnavailable("cannot reach OpenRouter: %s" % type(reason).__name__) from exc
        except http.client.HTTPException as exc:
            raise DependencyUnavailable("OpenRouter sent a malformed HTTP response") from exc
        except OSError as exc:
            raise DependencyUnavailable("cannot reach OpenRouter: %s" % type(exc).__name__) from exc
        if not isinstance(body, (bytes, bytearray)) or len(body) > limit:
            raise DependencyUnavailable("OpenRouter returned an oversized or invalid body")
        if not 200 <= int(status) < 300:
            self._call.retry_after = _retry_after_seconds(response_headers)
            return int(status), bytes(body)
        try:
            return int(status), json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise DependencyUnavailable("OpenRouter returned non-JSON") from exc

    def _get_ok(self, settings: OpenRouterSettings, path: str, **kwargs) -> object:
        status, document = self._get(settings, path, **kwargs)
        if not 200 <= status < 300:
            raise self._classify_http_error(status, document if isinstance(document, bytes) else b"")
        return document

    def list_models(
        self, *, search: str = "", tools: bool = False,
        timeout: float = DISCOVERY_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        """Models this key may use (``/models/user``), else the public catalog."""
        settings = self.settings()
        check_endpoint_policy(settings, require_key=False)
        source = "public"
        document: object = None
        if settings.api_key:
            status, document = self._get(settings, "/models/user", timeout=timeout,
                                         limit=MODELS_BODY_LIMIT)
            if 200 <= status < 300:
                source = "account"
            elif status in (401, 402, 429) or status >= 500:
                raise self._classify_http_error(status, document if isinstance(document, bytes) else b"")
            else:  # 403/404: the filtered listing is unavailable to this key
                document = None
        if document is None:
            document = self._get_ok(settings, "/models", timeout=timeout,
                                    limit=MODELS_BODY_LIMIT, auth=False)
        rows = document.get("data") if isinstance(document, dict) else None
        if not isinstance(rows, list):
            raise DependencyUnavailable("OpenRouter model listing has no data array")
        summaries = [item for item in (model_summary(row) for row in rows) if item is not None]
        models = filter_models(summaries, search=search, tools=tools)
        return {"provider": PROVIDER_ID, "source": source, "total": len(summaries),
                "count": len(models), "models": models}

    def account(self, *, timeout: float = DISCOVERY_TIMEOUT_SECONDS) -> dict[str, object]:
        """Key limits and usage (``/key``) plus the credit balance (``/credits``)."""
        settings = self.settings()
        check_endpoint_policy(settings)
        document = self._get_ok(settings, "/key", timeout=timeout, limit=ACCOUNT_BODY_LIMIT)
        data = document.get("data") if isinstance(document, dict) else None
        if not isinstance(data, dict):
            raise DependencyUnavailable("OpenRouter key info has no data object")

        def number(value):
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        result: dict[str, object] = {
            "provider": PROVIDER_ID,
            "limit": number(data.get("limit")),
            "limit_remaining": number(data.get("limit_remaining")),
            "usage": number(data.get("usage")),
            "usage_daily": number(data.get("usage_daily")),
            "usage_weekly": number(data.get("usage_weekly")),
            "usage_monthly": number(data.get("usage_monthly")),
            "is_free_tier": data.get("is_free_tier") if isinstance(data.get("is_free_tier"), bool) else None,
            "total_credits": None, "total_usage": None, "credits_remaining": None,
            "credits_note": "",
        }
        status, credits = self._get(settings, "/credits", timeout=timeout, limit=ACCOUNT_BODY_LIMIT)
        payload = credits.get("data") if 200 <= status < 300 and isinstance(credits, dict) else None
        if isinstance(payload, dict):
            total, used = number(payload.get("total_credits")), number(payload.get("total_usage"))
            result.update(total_credits=total, total_usage=used)
            if total is not None and used is not None:
                result["credits_remaining"] = round(total - used, 6)
        else:
            result["credits_note"] = (
                "account credit balance not readable with this key (HTTP %d); "
                "limit_remaining is this key's own spending cap" % status
            )
        return result

    # -- health and status (configuration only; never a network call) -------

    def served_tier_models(self) -> Mapping[str, Mapping[str, str]]:
        settings = self.settings()
        return {PROVIDER_ID: {
            tier: model for tier in PROVIDER_TIERS
            if (model := settings.model_for_tier(tier))
        }}

    def capability_health(self) -> CapabilityHealth:
        try:
            check_endpoint_policy(self.settings())
            healthy, detail = True, "configured (hosted; not probed)"
        except Exception as exc:  # noqa: BLE001 - health reports, never raises
            healthy, detail = False, _bounded(exc if isinstance(exc, SonderError) else type(exc).__name__)
        return CapabilityHealth(
            provider=PROVIDER_ID,
            capabilities=frozenset({Capability.GENERATION, Capability.STREAMING}),
            healthy=healthy, checked_at=datetime.now(timezone.utc), detail=detail,
        )

    def provider_status(self) -> Mapping[str, Mapping[str, object]]:
        """Content-free, key-free status from configuration alone."""
        entry: dict[str, object] = {key: None for key in STATUS_KEYS}
        entry.update(provider=PROVIDER_ID, state="unavailable", healthy=False,
                     capabilities=sorted(CAPABILITIES))
        try:
            settings = self.settings()
            entry.update(
                base_url=settings.display_base_url,
                cloud_enabled=settings.cloud_enabled,
                api_key_configured=bool(settings.api_key),
                tier_models={tier: settings.model_for_tier(tier) or None for tier in PROVIDER_TIERS},
                provider_preferences={tier: settings.provider_preferences(tier) for tier in PROVIDER_TIERS},
            )
            check_endpoint_policy(settings)
            entry.update(state="configured", healthy=True, detail="hosted provider; not probed")
        except Exception as exc:  # noqa: BLE001 - status reports, never raises
            entry["detail"] = _bounded(exc if isinstance(exc, SonderError) else type(exc).__name__)
        return {PROVIDER_ID: entry}


def protocol_probe_gateway(
    model: str, *, env: Mapping[str, str] | None = None, transport=None,
) -> OpenAICompatibleGateway:
    """A concrete OpenAI-compatible gateway on the OpenRouter route.

    ``OpenAICompatibleProtocolProbe`` accepts only that concrete type.  This
    only builds it; nothing here runs a probe, and the probe itself still
    needs an explicit ``cloud_allowed=True`` (``backend_attest --allow-cloud``).
    """
    settings = config_from_env(env, policy_models=None)
    check_endpoint_policy(settings)
    try:
        model = validate_model_id(model, "probe model")
    except OpenRouterPolicyError as exc:
        raise InvalidInput(str(exc)) from exc
    preferences = settings.provider_preferences("")
    send = transport or OpenAICompatibleGateway._default_transport

    def with_preferences(url: str, payload: dict, headers: dict, timeout) -> dict:
        # Probe payloads carry the same privacy-first routing preferences as
        # every other OpenRouter request.
        return send(url, {**payload, "provider": dict(preferences)}, headers, timeout)

    return OpenAICompatibleGateway(OpenAICompatibleConfig(
        base_url=settings.api_root, api_key=settings.api_key, model=model,
    ), transport=with_preferences)


__all__ = [
    "CAPABILITIES",
    "DEFAULT_BASE_URL",
    "ENV_API_KEY",
    "OpenRouterCreditsExhausted",
    "OpenRouterGateway",
    "OpenRouterRateLimited",
    "OpenRouterSettings",
    "PROVIDER_ID",
    "PROVIDER_LABEL",
    "STATUS_KEYS",
    "check_endpoint_policy",
    "config_from_env",
    "is_loopback_url",
    "normalize_base_url",
    "protocol_probe_gateway",
    "record_usage",
    "usage_facts",
]
