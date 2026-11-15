"""Anthropic (the Claude API) as a hosted ModelGateway provider.

Modelled on :mod:`openrouter_gateway`, but over the official ``anthropic``
Python SDK instead of an HTTP wire format.  The SDK is optional: it is
imported lazily on the first call, and a missing package is a typed,
actionable :class:`AnthropicSdkMissing` ("pip install anthropic"), never an
import-time failure of the runtime.

* **Consent.** Anthropic is always a cloud provider: every request needs the
  explicit ``SONDER_ALLOW_CLOUD`` opt-in and an OperationContext that allows
  cloud.  It is off by default and nothing routes to it unless a tier is bound
  with ``SONDER_<TIER>_PROVIDER=anthropic`` (or ``SONDER_MODEL_BACKEND=anthropic``).
* **Credentials.** Keys come from ``SONDER_ANTHROPIC_API_KEY`` and an optional
  ``SONDER_ANTHROPIC_API_KEY_2``, read per call from the process environment.
  Plain ``ANTHROPIC_API_KEY`` is deliberately NOT read: Claude Code and the
  SDK's own client use that name, and an operator's coding-assistant key must
  never be spent by this runtime by accident.  The SDK client is built with an
  explicit ``api_key`` and ``base_url`` (``https://api.anthropic.com``, or the
  validated https ``SONDER_ANTHROPIC_BASE_URL``).  An explicit key makes the SDK
  ignore ``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` and the profile and
  credential-file discovery; an explicit base URL makes it ignore
  ``ANTHROPIC_BASE_URL``; the headers the SDK constructor would merge from
  ``ANTHROPIC_CUSTOM_HEADERS`` (which could even replace ``x-api-key``) are
  cleared after construction, and the client is verified (base URL, key, no
  auth token, no extra headers) before first use.  A key is therefore sent only
  to that endpoint.  It never appears in a log line, error, status document,
  telemetry label, evidence record or export (only the key *slot*, 1 or 2, is
  ever recorded), and a mapped error keeps no reference to the SDK exception,
  whose request headers hold the key.
* **Key failover.** Key 1 is used first.  The call fails over to key 2 once,
  and only when key 1 was refused with an authentication error (HTTP 401) or a
  credit-exhausted error (HTTP 400 whose message says the credit balance is too
  low).  A refused call was not charged, so this is not a retry of billed work.
  Every other error -- including 429, 5xx and 529 -- stays single-attempt, and
  the SDK's own automatic retries are switched off (``max_retries=0``).
* **Model selection.** An explicit ``model`` request option, else
  ``SONDER_ANTHROPIC_TIER_MODELS`` (``tier=model,...``), else
  ``SONDER_ANTHROPIC_MODEL``, else the documented current Opus id.  A model
  with no entry in the price table is refused: its spend could not be counted
  against the cap.
* **Request mapping.** The system prompt is the ``system`` parameter; history
  and the prompt are ``user``/``assistant`` turns; ``num_predict`` is
  ``max_tokens`` bounded by ``SONDER_ANTHROPIC_MAX_TOKENS``; ``stop`` is
  ``stop_sequences``.  Sampling options (temperature, top_p, top_k and the
  like) are not forwarded: current Claude models reject them.  Like the
  OpenRouter gateway this one returns text only, so tools, tool choice,
  thinking and structured-output options are refused rather than dropped.
* **Accounting and spend cap.** The token usage the API returns is recorded
  per call with ``backend=anthropic`` and the model id; the USD cost comes from
  the price table in :mod:`...domain.anthropic_policy`.  A daily total persists
  in the state home (:mod:`anthropic_spend`), and once it reaches
  ``SONDER_ANTHROPIC_DAILY_USD`` (default 10) new calls are refused with
  :class:`AnthropicDailyCapReached`.  A call already in flight is never cut off.
* **Errors.** 401, credit exhaustion (400/402), 429 (``retry-after`` becomes a
  local cool-down for that key slot only), 529 overloaded and 5xx map onto the
  domain taxonomy with actionable text.
* **Unfinished streams are billed.**  A stream that is abandoned, times out or
  fails after ``message_start`` records the usage in the SDK's message snapshot.
* **Concurrency.**  Before each send the gateway reserves a worst-case cost
  (``max_tokens`` of output plus the prompt's byte length as input tokens) in
  the ledger, so calls in flight count against the cap; the reservation is
  released when the call ends.  Reservations are per process.

Thread affinity: any thread; per-call state lives on the stack.  The cool-down
is guarded by a lock.  Nothing here probes the paid API automatically.
"""
from __future__ import annotations

import contextvars
import importlib
import importlib.util
import json
import logging
import os
import queue
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    InferenceTelemetry,
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
from ...application.session.provider_attempts import dispatch_provider
from ...domain.anthropic_policy import (
    DEFAULT_MODEL,
    MODEL_ID,
    PRICES_USD_PER_MILLION,
    PROVIDER_ID,
    AnthropicPolicyError,
    is_credit_exhausted,
    price_for,
    usage_cost_usd,
    validate_model_id,
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
from ...domain.security.redaction import redact_text
from ...platform.metrics import default_registry
from ...platform.runtime_threads import Thread as owned_runtime_thread
from ..model_request_admission import HostModelRequestAdmission, host_model_request_admission
from ..provider_bindings import PROVIDER_TIERS
from .anthropic_spend import SpendLedger

logger = logging.getLogger(__name__)

PROVIDER_LABEL = "anthropic"
OPERATION = "/v1/messages"
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_TOKENS_CEILING = 16_000
MAX_TOKENS_LIMIT = 128_000
DEFAULT_DAILY_USD = Decimal(10)
MAX_DAILY_USD = Decimal(1_000_000)
MAX_COOLDOWN_SECONDS = 600.0
DETAIL_LIMIT = 240
_STREAM_BUFFER_CHUNKS = 64
_STREAM_POLL_SECONDS = 0.025

ENV_API_KEY = "SONDER_ANTHROPIC_API_KEY"
ENV_API_KEY_2 = "SONDER_ANTHROPIC_API_KEY_2"
ENV_MODEL = "SONDER_ANTHROPIC_MODEL"
ENV_TIER_MODELS = "SONDER_ANTHROPIC_TIER_MODELS"
ENV_MAX_TOKENS = "SONDER_ANTHROPIC_MAX_TOKENS"
ENV_DAILY_USD = "SONDER_ANTHROPIC_DAILY_USD"
ENV_TIMEOUT = "SONDER_ANTHROPIC_TIMEOUT_SECONDS"
ENV_BASE_URL = "SONDER_ANTHROPIC_BASE_URL"
DEFAULT_BASE_URL = "https://api.anthropic.com"
ENV_ALLOW_CLOUD = "SONDER_ALLOW_CLOUD"
_KEY_ENV_BY_SLOT = {1: ENV_API_KEY, 2: ENV_API_KEY_2}
CREDITS_URL = "https://platform.claude.com/settings/billing"

CAPABILITIES = frozenset({GATEWAY_CAPABILITY_CHAT, GATEWAY_CAPABILITY_FIXED_ENDPOINT})
STATUS_KEYS = (
    "provider", "state", "healthy", "detail", "capabilities", "cloud_enabled",
    "configured", "key_slots", "sdk_installed", "default_model", "tier_models",
    "base_url", "max_tokens_ceiling", "daily_cap_usd", "spent_today_usd", "day_utc",
    "calls_today", "key_slot_calls_today", "cap_reached",
)
# Options a Claude model either rejects or has no wire field for; dropping them
# changes nothing about what the hosted model does.
_DROPPED_SAMPLING_OPTIONS = frozenset({
    "temperature", "top_p", "top_k", "min_p", "seed", "presence_penalty",
    "frequency_penalty", "repeat_penalty",
})
_LOCAL_ONLY_OPTIONS = frozenset({"num_ctx", "repeat_last_n", "keep_alive", "num_gpu"})
_UNSUPPORTED_OPTIONS = ("tools", "tool_choice", "functions", "format", "response_format")
_FAILOVER_FLAG = "anthropic_failover"


class AnthropicSdkMissing(DependencyUnavailable):
    """The optional ``anthropic`` package is not installed."""


class AnthropicCreditsExhausted(Forbidden):
    """The key's account has no credit left (HTTP 400 credit balance / 402)."""


class AnthropicRateLimited(CapacityExceeded):
    """HTTP 429 (or a live ``retry-after`` cool-down); ``retry_after`` in seconds."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AnthropicDailyCapReached(Forbidden):
    """Today's local spend reached ``SONDER_ANTHROPIC_DAILY_USD``."""

    def __init__(self, message: str, *, cap_usd: Decimal, spent_usd: Decimal) -> None:
        super().__init__(message)
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd


def _bounded(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= DETAIL_LIMIT else value[: DETAIL_LIMIT - 3] + "..."


def _usd(value: Decimal) -> str:
    return "$%s" % value.quantize(Decimal("0.0001"), rounding="ROUND_UP")


def normalize_base_url(value: object) -> str:
    """``https://host[:port][/path]``: no plain http, userinfo, query or fragment."""
    raw = str(value or "").strip().rstrip("/")
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        parts.port  # noqa: B018 - validates the port
    except ValueError as exc:
        raise InvalidInput("%s is not a valid URL" % ENV_BASE_URL) from exc
    if (parts.scheme != "https" or not host or parts.username or parts.password
            or parts.query or parts.fragment):
        raise InvalidInput(
            "%s must be https://host[:port][/path] without credentials, query or fragment"
            % ENV_BASE_URL
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _parse_tier_models(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (part.strip() for part in str(raw or "").split(",")):
        if not item:
            continue
        tier, sep, model = item.partition("=")
        tier = tier.strip().lower()
        if not sep or not tier or not model.strip():
            raise InvalidInput("%s entries must look like tier=model" % ENV_TIER_MODELS)
        if tier not in PROVIDER_TIERS:
            raise InvalidInput(
                "%s names unknown tier %r (tiers: %s)"
                % (ENV_TIER_MODELS, tier, ", ".join(PROVIDER_TIERS))
            )
        if tier in result:
            raise InvalidInput("%s names tier %r twice" % (ENV_TIER_MODELS, tier))
        try:
            result[tier] = validate_model_id(model, "%s tier %r" % (ENV_TIER_MODELS, tier))
        except AnthropicPolicyError as exc:
            raise InvalidInput(str(exc)) from exc
    return result


def _int_env(source: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = str(source.get(name, "") or "").strip()
    if not raw:
        return default
    if not raw.isdecimal() or not low <= int(raw) <= high:
        raise InvalidInput("%s must be an integer in [%d, %d]" % (name, low, high))
    return int(raw)


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


def _daily_cap(source: Mapping[str, str]) -> Decimal:
    raw = str(source.get(ENV_DAILY_USD, "") or "").strip()
    if not raw:
        return DEFAULT_DAILY_USD
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise InvalidInput("%s must be a dollar amount" % ENV_DAILY_USD) from exc
    if not value.is_finite() or not 0 <= value <= MAX_DAILY_USD:
        raise InvalidInput("%s must be between 0 and %s" % (ENV_DAILY_USD, MAX_DAILY_USD))
    return value


@dataclass(frozen=True)
class AnthropicSettings:
    """Resolved provider settings; build with :func:`config_from_env`."""

    api_key_1: str = field(default="", repr=False)
    api_key_2: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    tier_models: Mapping[str, str] = field(default_factory=dict)
    max_tokens_ceiling: int = DEFAULT_MAX_TOKENS_CEILING
    daily_usd: Decimal = DEFAULT_DAILY_USD
    cloud_enabled: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    base_url: str = DEFAULT_BASE_URL

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        try:
            validate_model_id(self.model, ENV_MODEL)
        except AnthropicPolicyError as exc:
            raise InvalidInput(str(exc)) from exc
        object.__setattr__(self, "tier_models", MappingProxyType(dict(self.tier_models)))

    @property
    def key_slots(self) -> tuple[tuple[int, str], ...]:
        """``(slot, key)`` pairs in failover order; unset slots are absent."""
        return tuple(
            (slot, key) for slot, key in ((1, self.api_key_1), (2, self.api_key_2)) if key
        )

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(key for _slot, key in self.key_slots)

    def model_for_tier(self, tier: str) -> str:
        return self.tier_models.get(str(tier or ""), self.model)


def config_from_env(env: Mapping[str, str] | None = None) -> AnthropicSettings:
    """Resolve settings lazily (never at import); ``ANTHROPIC_API_KEY`` is never read."""
    source = os.environ if env is None else env
    return AnthropicSettings(
        api_key_1=str(source.get(ENV_API_KEY, "") or "").strip(),
        api_key_2=str(source.get(ENV_API_KEY_2, "") or "").strip(),
        model=str(source.get(ENV_MODEL, "") or "").strip() or DEFAULT_MODEL,
        tier_models=_parse_tier_models(str(source.get(ENV_TIER_MODELS, "") or "")),
        max_tokens_ceiling=_int_env(
            source, ENV_MAX_TOKENS, DEFAULT_MAX_TOKENS_CEILING, 1, MAX_TOKENS_LIMIT,
        ),
        daily_usd=_daily_cap(source),
        cloud_enabled=cloud_opted_in(source),
        timeout_seconds=_float_env(source, ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS),
        base_url=str(source.get(ENV_BASE_URL, "") or "").strip() or DEFAULT_BASE_URL,
    )


def check_cloud_consent(settings: AnthropicSettings) -> None:
    if not settings.cloud_enabled:
        raise Forbidden(
            "Anthropic is a hosted, metered provider: prompts and the API key "
            "leave this machine. It stays off until cloud is enabled: set "
            "%s=1 for the runtime (or run cloud_opt_in on)" % ENV_ALLOW_CLOUD
        )


def check_endpoint_policy(settings: AnthropicSettings) -> None:
    """Cloud opt-in, then at least one key."""
    check_cloud_consent(settings)
    if not settings.key_slots:
        raise InvalidInput(
            "no Anthropic API key is configured: set the %s environment variable "
            "(for example as a Windows user environment variable) and restart "
            "Sonder; %s is an optional second key. ANTHROPIC_API_KEY is not read"
            % (ENV_API_KEY, ENV_API_KEY_2)
        )


def load_sdk():
    """Import the optional SDK, or raise the typed, actionable error."""
    try:
        return importlib.import_module("anthropic")
    except ImportError:
        raise AnthropicSdkMissing(
            "the Anthropic provider needs the official 'anthropic' Python package, "
            "which is not installed in this environment: run `pip install anthropic` "
            "(or `pip install -r requirements-anthropic.txt`) and restart Sonder"
        ) from None


def sdk_installed() -> bool:
    """Whether the SDK can be found, without importing it."""
    try:
        return importlib.util.find_spec("anthropic") is not None
    except (ImportError, ValueError):
        return False


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000_000:
        return None
    return value


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if type(status) is int else None


def _header(exc: BaseException, name: str) -> object:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
        return value if value is not None else headers.get(name.title())
    except Exception:  # noqa: BLE001 - a header we cannot read is simply absent
        return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    raw = _header(exc, "retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None  # an HTTP-date is not worth trusting a clock over
    if value != value or value < 0:
        return None
    return min(value, MAX_COOLDOWN_SECONDS)


def _type_names(exc: BaseException) -> frozenset[str]:
    return frozenset(cls.__name__ for cls in type(exc).__mro__)


@dataclass(frozen=True)
class _Prepared:
    settings: AnthropicSettings
    model: str
    params: dict
    reservation: int


class AnthropicGateway:
    """ModelGateway over the Claude Messages API."""

    def __init__(
        self, config: AnthropicSettings | None = None, *,
        client_factory: Callable[[str, float], object] | None = None,
        spend_ledger: SpendLedger | None = None,
        request_admission: HostModelRequestAdmission | None = None,
        env: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings_override = config
        self._env = env
        self._client_factory = client_factory
        self._ledger = spend_ledger or SpendLedger()
        self._request_admission = (
            host_model_request_admission() if request_admission is None else request_admission
        )
        if not isinstance(self._request_admission, HostModelRequestAdmission):
            raise TypeError("request admission must be host-owned")
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cooldown_until: dict[int, float] = {}
        self._clients: dict[int, tuple[str, str, object]] = {}
        self.last_usage: Mapping[str, object] | None = None

    # -- configuration & consent ------------------------------------------

    @property
    def capabilities(self) -> frozenset[str]:
        return CAPABILITIES

    @property
    def request_admission(self) -> HostModelRequestAdmission:
        return self._request_admission

    def settings(self) -> AnthropicSettings:
        """Resolve the current settings (the environment is read per call)."""
        return self._settings_override or config_from_env(self._env)

    @staticmethod
    def _check_liveness(
        context: OperationContext, *, phase: str = "before model call"
    ) -> float | None:
        if context.expired:
            raise DeadlineExceeded(f"operation deadline exceeded {phase}")
        if context.cancellation is not None and context.cancellation.cancelled:
            raise Cancelled(f"operation cancelled {phase}")
        return context.remaining_seconds

    def _call_timeout(self, settings: AnthropicSettings, context: OperationContext) -> float:
        remaining = self._check_liveness(context)
        return settings.timeout_seconds if remaining is None else min(settings.timeout_seconds, remaining)

    def _enforce_consent(self, settings: AnthropicSettings, context: OperationContext) -> None:
        """Always cloud: the opt-in, a key and a cloud-allowing context."""
        check_endpoint_policy(settings)
        if not context.cloud_allowed:
            raise Forbidden(
                "Anthropic is a hosted provider and this operation context does "
                "not allow prompts to leave the machine"
            )

    def select_model(self, request: ModelRequest, settings: AnthropicSettings) -> str:
        """Explicit ``model`` option, else the tier map, else the default."""
        explicit = (request.options or {}).get("model")
        try:
            model = (
                validate_model_id(explicit, "model option") if explicit is not None
                else settings.model_for_tier(request.tier)
            )
        except AnthropicPolicyError as exc:
            raise InvalidInput(str(exc)) from exc
        if price_for(model) is None:
            raise InvalidInput(
                "Anthropic model %r has no entry in the price table, so its spend "
                "could not be counted against %s; priced models: %s"
                % (model, ENV_DAILY_USD, ", ".join(sorted(PRICES_USD_PER_MILLION)))
            )
        return model

    # -- cool-down and spend cap --------------------------------------------

    def _start_cooldown(self, slot: int, retry_after: float | None) -> None:
        if retry_after is None or retry_after <= 0:
            return
        with self._lock:
            self._cooldown_until[slot] = max(
                self._cooldown_until.get(slot, 0.0), self._monotonic() + retry_after,
            )

    def _cooldown_remaining(self, slot: int) -> float:
        with self._lock:
            return self._cooldown_until.get(slot, 0.0) - self._monotonic()

    def _check_cooldown(self, slot: int) -> None:
        remaining = self._cooldown_remaining(slot)
        if remaining > 0:
            raise AnthropicRateLimited(
                "Anthropic asked API key slot %d to wait (retry-after); not sending "
                "for another %.0fs" % (slot, remaining), retry_after=remaining,
            )

    def _reserve(self, settings: AnthropicSettings, model: str, params: dict) -> int:
        """Refuse at the cap; else reserve this call's worst-case cost in the ledger."""
        sized = {name: params[name] for name in ("system", "messages") if name in params}
        prompt_bytes = len(json.dumps(sized).encode("utf-8"))  # a token is at least a byte
        worst = usage_cost_usd(
            model, input_tokens=prompt_bytes, output_tokens=int(params["max_tokens"]), us_only=True,
        )
        token, spent, inflight = self._ledger.try_reserve(worst, settings.daily_usd)
        if token is None:
            raise AnthropicDailyCapReached(
                "Anthropic daily spend cap reached: %s spent today (UTC)%s of the %s cap "
                "(%s); new calls resume after 00:00 UTC, or raise the cap"
                % (_usd(spent), (" plus up to %s in flight" % _usd(inflight)) if inflight else "",
                   _usd(settings.daily_usd), ENV_DAILY_USD),
                cap_usd=settings.daily_usd, spent_usd=spent,
            )
        return token

    # -- request building --------------------------------------------------

    @staticmethod
    def _build_messages(request: ModelRequest) -> tuple[str, list[dict[str, str]]]:
        """``(system, messages)``; history system turns are folded into ``system``."""
        system = [request.system] if request.system else []
        messages: list[dict[str, str]] = []
        for entry in request.history or ():
            if isinstance(entry, dict) and entry.get("role") and "content" in entry:
                role, content = entry["role"], entry["content"]
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                role, content = entry
            else:
                continue
            if not isinstance(content, str):
                raise InvalidInput("the Anthropic gateway carries text history turns only")
            if role == "system":
                system.append(content)
            elif role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
            else:
                raise InvalidInput("history role %r is not user, assistant or system" % str(role)[:40])
        messages.append({"role": "user", "content": request.prompt})
        return "\n\n".join(part for part in system if part), messages

    def build_params(
        self, request: ModelRequest, settings: AnthropicSettings, model: str,
    ) -> dict[str, object]:
        options = dict(request.options or {})
        for name in _UNSUPPORTED_OPTIONS:
            if options.get(name) not in (None, False, "", [], {}):
                raise InvalidInput(
                    "the Anthropic gateway returns text only and does not carry %r" % name
                )
        if options.get("think") not in (None, False):
            raise InvalidInput("the Anthropic gateway does not carry thinking mode")
        ignored = sorted(
            name for name in options
            if name in _DROPPED_SAMPLING_OPTIONS or name in _LOCAL_ONLY_OPTIONS
        )
        if ignored:
            logger.debug("anthropic: not forwarding options %s", ", ".join(ignored))
        max_tokens = settings.max_tokens_ceiling
        wanted = options.get("num_predict")
        if wanted is not None:
            if type(wanted) is bool or not isinstance(wanted, (int, float)) or float(wanted) != int(wanted) or wanted < 1:
                raise InvalidInput("option 'num_predict' must be a positive integer")
            max_tokens = min(int(wanted), settings.max_tokens_ceiling)
        system, messages = self._build_messages(request)
        params: dict[str, object] = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            params["system"] = system
        stop = options.get("stop")
        if stop is not None:
            if isinstance(stop, str):
                stop = [stop]
            if (not isinstance(stop, (list, tuple)) or not 1 <= len(stop) <= 4
                    or any(not isinstance(item, str) or not item for item in stop)):
                raise InvalidInput("option 'stop' must be a string or at most 4 strings")
            params["stop_sequences"] = list(stop)
        return params

    def _prepare(self, request: ModelRequest, context: OperationContext) -> _Prepared:
        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        settings = self.settings()
        self._enforce_consent(settings, context)
        model = self.select_model(request, settings)
        params = self.build_params(request, settings, model)
        if self._client_factory is None:
            load_sdk()  # a missing SDK fails here, before any admission or spend check
        self._check_cooldown(settings.key_slots[0][0])
        return _Prepared(settings, model, params, self._reserve(settings, model, params))

    # -- errors --------------------------------------------------------------

    def _redacted(self, text: object, settings: AnthropicSettings) -> str:
        return _bounded(redact_text(str(text or ""), secret_values=settings.secrets))

    def _map_error(
        self, exc: BaseException, slot: int, settings: AnthropicSettings,
    ) -> SonderError:
        """Domain error for one SDK failure; no key, no raw SDK text is kept."""
        if isinstance(exc, SonderError):
            return exc
        status = _status_of(exc)
        raw_message = getattr(exc, "message", None)
        message = self._redacted(raw_message, settings) if isinstance(raw_message, str) else ""
        suffix = ": %s" % message if message else ""
        names = _type_names(exc)
        logger.warning(
            "anthropic: call failed: key_slot=%d http_status=%s error_type=%s",
            slot, status, type(exc).__name__,
        )
        failover = False
        if status is None:
            if isinstance(exc, (socket.timeout, TimeoutError)) or "APITimeoutError" in names:
                error: SonderError = DeadlineExceeded("Anthropic timed out the request")
            elif "APIConnectionError" in names or isinstance(exc, OSError):
                error = DependencyUnavailable("cannot reach Anthropic: %s" % type(exc).__name__)
            else:
                error = DependencyUnavailable("Anthropic call failed: %s" % type(exc).__name__)
        elif status == 401:
            failover = True
            error = Forbidden(
                "Anthropic rejected API key slot %d (HTTP 401); check %s"
                % (slot, _KEY_ENV_BY_SLOT.get(slot, ENV_API_KEY))
            )
        elif is_credit_exhausted(status, raw_message):
            failover = True
            error = AnthropicCreditsExhausted(
                "the Anthropic account for key slot %d has no credit left (HTTP %d)%s; "
                "add credit at %s or set %s to another key"
                % (slot, status, suffix, CREDITS_URL, ENV_API_KEY_2)
            )
        elif status == 403:
            error = Forbidden("Anthropic refused the request (HTTP 403: permission)%s" % suffix)
        elif status == 404:
            error = InvalidInput(
                "Anthropic found no such model or endpoint (HTTP 404)%s; check the model id "
                "(%s) and that this organization can use it" % (suffix, ENV_MODEL)
            )
        elif status == 408:
            error = DeadlineExceeded("Anthropic timed out the request (HTTP 408)%s" % suffix)
        elif status == 429:
            retry_after = _retry_after_seconds(exc)
            self._start_cooldown(slot, retry_after)
            wait = (" retry after %.0fs" % retry_after) if retry_after is not None else " retry later"
            error = AnthropicRateLimited(
                "Anthropic is rate limiting this key (HTTP 429)%s;%s" % (suffix, wait),
                retry_after=retry_after,
            )
        elif status in (400, 413, 422):
            error = InvalidInput("Anthropic rejected the request (HTTP %d)%s" % (status, suffix))
        elif status == 529:
            error = DependencyUnavailable(
                "Anthropic is overloaded (HTTP 529)%s; try again later" % suffix
            )
        elif status >= 500:
            error = DependencyUnavailable("Anthropic failed the request (HTTP %d)%s" % (status, suffix))
        else:
            error = DependencyUnavailable("Anthropic returned HTTP %d%s" % (status, suffix))
        setattr(error, _FAILOVER_FLAG, failover)
        return error

    # -- one physical call ----------------------------------------------------

    def _build_client(self, key: str, settings: AnthropicSettings):
        if self._client_factory is not None:
            return self._client_factory(key, settings.timeout_seconds)
        # Explicit api_key and base_url make the SDK ignore ANTHROPIC_API_KEY,
        # ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL and credential discovery.
        # max_retries=0: the SDK's own retry loop would silently re-send billed work.
        client = load_sdk().Anthropic(
            api_key=key, base_url=settings.base_url, timeout=settings.timeout_seconds,
            max_retries=0,
        )
        # The constructor also merges ANTHROPIC_CUSTOM_HEADERS, and those headers
        # override the auth headers; no environment-supplied header may be sent.
        if not hasattr(client, "_custom_headers"):
            raise AnthropicSdkMissing(
                "the installed 'anthropic' package is not a supported version "
                "(pip install -r requirements-anthropic.txt)"
            )
        client._custom_headers = {}
        if (str(client.base_url).rstrip("/") != settings.base_url.rstrip("/")
                or client.api_key != key or getattr(client, "auth_token", None) is not None):
            raise DependencyUnavailable(
                "the Anthropic SDK client did not take the configured endpoint and key; "
                "refusing to send"
            )
        return client

    def _client(self, slot: int, key: str, settings: AnthropicSettings):
        """One SDK client per key slot, rebuilt only if its key or endpoint changed."""
        with self._lock:
            cached = self._clients.get(slot)
            if cached is not None and cached[0] == key and cached[1] == settings.base_url:
                return cached[2]
        client = self._build_client(key, settings)
        with self._lock:
            stale = self._clients.get(slot)
            self._clients[slot] = (key, settings.base_url, client)
        self._close_client(stale[2] if stale else None)
        return client

    @staticmethod
    def _close_client(client: object) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - closing is best effort
                logger.debug("anthropic: closing an SDK client failed", exc_info=False)

    def close(self) -> None:
        """Close the cached SDK clients (idempotent; the gateway stays usable)."""
        with self._lock:
            clients, self._clients = list(self._clients.values()), {}
        for _key, _url, client in clients:
            self._close_client(client)

    def _admit(self) -> None:
        admission = self._request_admission.try_acquire()
        if admission is not None and not admission.allowed:
            raise CapacityExceeded(
                "host model request rate admission refused; "
                f"retry after about {admission.retry_after:.3f}s"
            )

    @staticmethod
    def _normalize(final: object, model: str) -> dict[str, object]:
        """A plain, JSON-safe record of one SDK ``Message``."""
        text = "".join(
            block.text for block in (getattr(final, "content", None) or ())
            if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
        )
        usage = getattr(final, "usage", None)
        served = getattr(final, "model", None)
        stop_reason = getattr(final, "stop_reason", None)
        return {
            "model": served if isinstance(served, str) and MODEL_ID.match(served) else model,
            "text": text,
            "stop_reason": stop_reason if isinstance(stop_reason, str) else None,
            "inference_geo": getattr(usage, "inference_geo", None) if isinstance(
                getattr(usage, "inference_geo", None), str) else None,
            "usage": None if usage is None else {
                "input_tokens": _count(getattr(usage, "input_tokens", None)),
                "output_tokens": _count(getattr(usage, "output_tokens", None)),
                "cache_read_input_tokens": _count(getattr(usage, "cache_read_input_tokens", None)),
                "cache_creation_input_tokens": _count(getattr(usage, "cache_creation_input_tokens", None)),
            },
        }

    def _account(self, data: dict, model: str, slot: int) -> None:
        """The sole accounting boundary: runs for every completed physical response.

        A completed response can be billable even when a later cancellation
        prevents publishing it, so spend is recorded here, not in ``_finish``.
        """
        usage = data.get("usage")
        counts = {name: int((usage or {}).get(name) or 0) for name in (
            "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
        )}
        cost = usage_cost_usd(
            model, input_tokens=counts["input_tokens"], output_tokens=counts["output_tokens"],
            cache_read_tokens=counts["cache_read_input_tokens"],
            cache_write_tokens=counts["cache_creation_input_tokens"],
            us_only=data.get("inference_geo") == "us",
        )
        if usage is None:
            logger.warning("anthropic: response carried no usage; spend recorded as zero")
        self._ledger.record(
            usd=cost, slot=slot, input_tokens=counts["input_tokens"] + counts["cache_read_input_tokens"]
            + counts["cache_creation_input_tokens"], output_tokens=counts["output_tokens"],
        )
        prompt = (counts["input_tokens"] + counts["cache_read_input_tokens"]
                  + counts["cache_creation_input_tokens"])
        facts = {
            "model": model, "key_slot": slot, "cost_usd": cost, "prompt_tokens": prompt,
            "completion_tokens": counts["output_tokens"],
            "cached_tokens": counts["cache_read_input_tokens"],
            "cache_write_tokens": counts["cache_creation_input_tokens"],
        }
        self.last_usage = MappingProxyType(dict(facts))
        data["key_slot"] = slot
        data["prompt_tokens"] = prompt
        data["cached_tokens"] = counts["cache_read_input_tokens"]
        record_usage(facts)

    def _account_partial(self, stream: object, model: str, slot: int) -> None:
        """Bill what an unfinished stream already consumed; never raises.

        After ``message_start`` the SDK's message snapshot carries the input
        tokens and the output produced so far, which Anthropic bills even when
        this side abandons the stream, times out or loses the connection.
        """
        try:
            snapshot = stream.current_message_snapshot  # raises before message_start
        except Exception:  # noqa: BLE001 - nothing was started, so nothing was billed
            return
        try:
            data = self._normalize(snapshot, model)
            data["partial"] = True
            self._account(data, model, slot)
        except Exception as exc:  # noqa: BLE001 - never mask the stream's own failure
            logger.error("anthropic: could not account an unfinished stream (%s)", type(exc).__name__)

    def _attempt(
        self, settings: AnthropicSettings, slot: int, key: str, model: str, params: dict,
        context: OperationContext, timeout: float, sink, stop,
    ) -> dict:
        self._check_liveness(context, phase="before provider send")
        self._admit()

        def send() -> dict:
            failure: Exception | None = None
            final = None
            try:
                client = self._client(slot, key, settings)
                if sink is None:
                    final = client.messages.create(timeout=timeout, **params)
                else:
                    with client.messages.stream(timeout=timeout, **params) as stream:
                        try:
                            for piece in stream.text_stream:
                                if stop is not None and stop.is_set():
                                    raise Cancelled("Anthropic stream abandoned by its consumer")
                                if piece:
                                    sink(GenerationChunk(text=piece))
                            final = stream.get_final_message()
                        except BaseException:
                            self._account_partial(stream, model, slot)
                            raise
            except Exception as exc:  # noqa: BLE001 - mapped below, outside this handler
                failure = exc
            if failure is not None:
                # Mapped outside the except block so the domain error has neither
                # __cause__ nor __context__: the SDK exception references the
                # request, whose headers hold the key.
                error = self._map_error(failure, slot, settings)
                failure = None
                raise error
            data = self._normalize(final, model)
            self._account(data, model, slot)
            return data

        # Evidence capture commits the effective body (never the key) before send.
        return dispatch_provider(PROVIDER_LABEL, OPERATION, params, send)

    def _invoke(
        self, settings: AnthropicSettings, model: str, params: dict, context: OperationContext,
        timeout: float, *, sink=None, stop=None,
    ) -> dict:
        """Key 1, then key 2 once, only after a 401 or credit-exhausted refusal."""
        slots = settings.key_slots
        for position, (slot, key) in enumerate(slots):
            delivered = False

            def mark(chunk: GenerationChunk) -> None:
                nonlocal delivered
                delivered = True
                sink(chunk)

            try:
                return self._attempt(
                    settings, slot, key, model, params, context, timeout,
                    mark if sink is not None else None, stop,
                )
            except SonderError as error:
                if (getattr(error, _FAILOVER_FLAG, False) and not delivered
                        and position + 1 < len(slots)
                        and self._cooldown_remaining(slots[position + 1][0]) <= 0):
                    logger.warning(
                        "anthropic: key slot %d refused (%s); failing over to slot %d",
                        slot, type(error).__name__, slots[position + 1][0],
                    )
                    continue
                raise
        raise InvalidInput("no Anthropic API key is configured")  # unreachable past consent

    # -- generate ----------------------------------------------------------

    def _finish(self, data: dict, request: ModelRequest, model: str, started: float) -> ModelResponse:
        if data.get("stop_reason") == "refusal":
            raise Forbidden("Claude declined this request (stop_reason=refusal)")
        usage = data.get("usage") or {}
        return ModelResponse(
            text=require_model_text(data.get("text")),
            model=str(data.get("model") or model),
            tier=request.tier or PROVIDER_ID,
            duration_ms=int((time.monotonic() - started) * 1000),
            tokens_in=optional_token_count(data.get("prompt_tokens") if usage else None, "prompt token count"),
            tokens_out=optional_token_count(usage.get("output_tokens"), "output token count"),
            telemetry=_telemetry(data),
        )

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        prepared = self._prepare(request, context)
        try:
            timeout = self._call_timeout(prepared.settings, context)
            started = time.monotonic()
            data = self._invoke(prepared.settings, prepared.model, prepared.params, context, timeout)
        finally:
            self._ledger.release(prepared.reservation)
        self._check_liveness(context, phase="during model call")
        return self._finish(data, request, prepared.model, started)

    # -- stream ------------------------------------------------------------

    def stream(self, request: ModelRequest, context: OperationContext) -> Iterator[GenerationChunk]:
        """Yield text deltas, then one final chunk with finish data and usage.

        The SDK exchange runs on a worker thread through the same ``_invoke``
        path as :meth:`generate` (consent, failover, evidence capture, error
        mapping, accounting); the consumer abandoning the iterator stops it.
        """
        prepared = self._prepare(request, context)
        try:
            timeout = self._call_timeout(prepared.settings, context)
        except BaseException:
            self._ledger.release(prepared.reservation)
            raise
        events: queue.Queue = queue.Queue(maxsize=_STREAM_BUFFER_CHUNKS)
        stop = threading.Event()
        finished = threading.Event()
        terminal: list[tuple[str, object]] = []

        def publish(chunk: GenerationChunk) -> None:
            # A slow consumer backpressures the SDK read without trapping the
            # worker forever when the iterator is closed or cancelled.
            while True:
                if stop.is_set():
                    raise Cancelled("Anthropic stream abandoned by its consumer")
                self._check_liveness(context, phase="during model stream")
                try:
                    events.put(chunk, timeout=_STREAM_POLL_SECONDS)
                    return
                except queue.Full:
                    continue

        def worker() -> None:
            try:
                terminal.append(("done", self._invoke(
                    prepared.settings, prepared.model, prepared.params, context, timeout,
                    sink=publish, stop=stop,
                )))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the consumer thread
                terminal.append(("error", exc))
            finally:
                # Released once the worker has accounted the call (or its partial usage).
                self._ledger.release(prepared.reservation)
                finished.set()
                try:
                    events.put_nowait(None)  # wake an idle consumer, never wait
                except queue.Full:
                    pass  # the consumer discovers terminal state after drain

        started = time.monotonic()
        thread = owned_runtime_thread(
            target=contextvars.copy_context().run, args=(worker,),
            name="anthropic-stream", daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            self._ledger.release(prepared.reservation)
            raise
        try:
            while True:
                self._check_liveness(context, phase="during model stream")
                try:
                    item = events.get(timeout=_STREAM_POLL_SECONDS)
                except queue.Empty:
                    if not finished.is_set():
                        continue
                    try:
                        item = events.get_nowait()
                    except queue.Empty:
                        item = None
                if item is not None:
                    yield item
                    continue
                kind, value = terminal[0]
                if kind == "error":
                    raise value  # type: ignore[misc]
                response = self._finish(value, request, prepared.model, started)  # type: ignore[arg-type]
                yield GenerationChunk(
                    text="", finish_reason=value.get("stop_reason"),  # type: ignore[union-attr]
                    input_tokens=response.tokens_in, output_tokens=response.tokens_out,
                )
                return
        finally:
            stop.set()
            thread.join(timeout=0.25)

    # -- embed -------------------------------------------------------------

    def embed(self, texts: Sequence[str], context: OperationContext) -> Sequence[Embedding]:
        del texts, context
        raise DependencyUnavailable(
            "the Anthropic gateway does not serve embeddings; set "
            "SONDER_EMBEDDING_PROVIDER=ollama"
        )

    # -- health and status (configuration and local ledger; never a network call) --

    def served_tier_models(self) -> Mapping[str, Mapping[str, str]]:
        settings = self.settings()
        return {PROVIDER_ID: {tier: settings.model_for_tier(tier) for tier in PROVIDER_TIERS}}

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
        """Content-free, key-free status: booleans for the key slots, never a value."""
        entry: dict[str, object] = {key: None for key in STATUS_KEYS}
        entry.update(provider=PROVIDER_ID, state="unavailable", healthy=False,
                     capabilities=sorted(CAPABILITIES))
        try:
            settings = self.settings()
            entry.update(
                cloud_enabled=settings.cloud_enabled,
                configured=bool(settings.key_slots),
                key_slots={"1": bool(settings.api_key_1), "2": bool(settings.api_key_2)},
                sdk_installed=sdk_installed() or self._client_factory is not None,
                default_model=settings.model,
                tier_models={tier: settings.model_for_tier(tier) for tier in PROVIDER_TIERS},
                base_url=settings.base_url,
                max_tokens_ceiling=settings.max_tokens_ceiling,
                daily_cap_usd=str(settings.daily_usd),
            )
            try:
                snapshot = self._ledger.snapshot()
                spent = Decimal(str(snapshot["usd"]))
                entry.update(
                    spent_today_usd=str(spent), day_utc=snapshot["day_utc"],
                    calls_today=snapshot["calls"], key_slot_calls_today=snapshot["key_slot_calls"],
                    cap_reached=spent >= settings.daily_usd,
                )
            except SonderError as exc:
                entry["detail"] = _bounded(exc)
            check_endpoint_policy(settings)
            entry.update(state="configured", healthy=True, detail="hosted provider; not probed")
        except Exception as exc:  # noqa: BLE001 - status reports, never raises
            entry["detail"] = _bounded(exc if isinstance(exc, SonderError) else type(exc).__name__)
        return {PROVIDER_ID: entry}


def _telemetry(data: Mapping[str, object]) -> InferenceTelemetry | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt, cached = data.get("prompt_tokens"), data.get("cached_tokens")
    output = usage.get("output_tokens")
    if not isinstance(prompt, int) or not isinstance(cached, int) or cached > prompt:
        cached, uncached = None, None
    else:
        uncached = prompt - cached
    telemetry = InferenceTelemetry(
        prompt_tokens=prompt if isinstance(prompt, int) else None,
        prompt_cached_tokens=cached, prompt_uncached_tokens=uncached,
        output_tokens=output if isinstance(output, int) else None,
    )
    return telemetry if any(value is not None for value in telemetry.__dict__.values()) else None


def record_usage(facts: Mapping[str, object]) -> None:
    """Export one call's usage: metrics (bounded labels) and one INFO line."""
    cost = facts.get("cost_usd")
    cost_float = float(cost) if isinstance(cost, Decimal) else None
    registry = default_registry()
    telemetry = _telemetry({
        "usage": {"output_tokens": facts.get("completion_tokens")},
        "prompt_tokens": facts.get("prompt_tokens"), "cached_tokens": facts.get("cached_tokens"),
    })
    registry.observe_inference(PROVIDER_ID, telemetry)
    registry.observe_provider_usage(
        PROVIDER_ID, upstream="anthropic", cost_usd=cost_float,
        tokens={
            "prompt": facts.get("prompt_tokens"),
            "completion": facts.get("completion_tokens"),
            "cached": facts.get("cached_tokens"),
            "cache_write": facts.get("cache_write_tokens"),
        },
    )
    logger.info(
        "anthropic usage: model=%s key_slot=%s cost_usd=%s prompt_tokens=%s "
        "completion_tokens=%s cached_tokens=%s",
        facts.get("model"), facts.get("key_slot"), cost, facts.get("prompt_tokens"),
        facts.get("completion_tokens"), facts.get("cached_tokens"),
    )


__all__ = [
    "CAPABILITIES",
    "ENV_API_KEY",
    "ENV_API_KEY_2",
    "ENV_DAILY_USD",
    "ENV_MAX_TOKENS",
    "ENV_MODEL",
    "ENV_TIER_MODELS",
    "PROVIDER_ID",
    "PROVIDER_LABEL",
    "STATUS_KEYS",
    "AnthropicCreditsExhausted",
    "AnthropicDailyCapReached",
    "AnthropicGateway",
    "AnthropicRateLimited",
    "AnthropicSdkMissing",
    "AnthropicSettings",
    "check_endpoint_policy",
    "config_from_env",
    "load_sdk",
    "record_usage",
]
