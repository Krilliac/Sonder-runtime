"""OpenAI-compatible gateway adapter implementing the ModelGateway port.

A second ModelGateway backend that proves the port abstraction: it talks to any
OpenAI-compatible server — llama.cpp's ``server``, vLLM, LM Studio, or a hosted
API — over ``/v1/chat/completions`` and ``/v1/embeddings`` instead of Ollama.

Consent, mirroring the rest of Sonder's model:
- A **loopback** endpoint is treated as local (a self-hosted inference server,
  like local Ollama) and needs no cloud consent.
- Any **non-loopback** endpoint routes prompts off the machine and is refused
  unless the caller's OperationContext explicitly allows cloud — the consent
  gate cannot be bypassed by reaching this lane.

Calls are **single-attempt** (no silent retry of possibly-metered work), honor
the operation-context deadline and cancellation, and map transport/HTTP errors
into the domain error taxonomy — callers never see ``urllib`` or HTTP details.

Configuration (resolved lazily at call time, never at import/construction):
``SONDER_OPENAI_BASE_URL``, ``SONDER_OPENAI_API_KEY``, ``SONDER_OPENAI_MODEL``,
``SONDER_OPENAI_EMBED_MODEL``, ``SONDER_OPENAI_ALLOW_HTTP_NETWORKS``.

Transport: a non-loopback endpoint must use ``https`` -- prompts and the
bearer key never cross a network in plaintext -- unless its host is a private
IP literal inside a network the operator listed in
``SONDER_OPENAI_ALLOW_HTTP_NETWORKS`` (comma/semicolon separated CIDRs; only
private ranges are honoured, host names never are).  The default transports
never follow a redirect, reach a loopback endpoint directly (an ambient
``http_proxy`` would otherwise receive the request), and read a successful
body only up to a fixed byte ceiling.
"""
from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import format_datetime, parsedate_to_datetime
from types import SimpleNamespace
from typing import Sequence
from urllib.parse import urlsplit

import sonder_runtime.adapters.inference.gpu_admission as gpu_admission
from .exchange_transport import (
    DEFAULT_TIMEOUT_SECONDS as _EXCHANGE_DEFAULT_TIMEOUT,
    ERROR_BODY_LIMIT,
    RESPONSE_BODY_LIMIT as _EXCHANGE_RESPONSE_LIMIT,
    _BudgetedHTTPHandler,
    _BudgetedHTTPSHandler,
    _ExchangeBudget,
    _NoRedirect,
    _error_document_fields,
    _read_bounded,
)
from .log_safe import UNSENDABLE_URL_CHARS as _UNSENDABLE_URL_CHARS
from .log_safe import redact_url, urlsplit_or_none
from ...application.chat import stream_sink
from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    optional_token_count,
    require_embedding_vector,
    require_model_text,
)
from ...application.session.provider_attempts import dispatch_provider
from ...domain.chat_template_policy import (
    ChatTemplateOptionsError,
    normalize_chat_template_options,
)
from ...domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InternalFailure,
    InvalidInput,
    SonderError,
)
from ...domain.gpu_admission import RefusalReason
from ...domain.model_capabilities import (
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_EMBEDDINGS,
    GATEWAY_CAPABILITY_FIXED_ENDPOINT,
)
from ...platform.metrics import default_registry
from ..model_request_admission import (
    HostModelRequestAdmission,
    host_model_request_admission,
)
from ..model_transport import ModelCallError
from ..tls_contexts import default_https_context
from ..provider_bindings import provider_id_for_label
from .sse_stream import StreamTimeout, post_streaming, stall_limits
from .telemetry import from_openai_compatible

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_DEFAULT_TIMEOUT = 300
# Error bodies and GET responses are read with a hard bound: an error page is
# only ever inspected for a machine-readable code, never stored or relayed.
GET_BODY_LIMIT = 1_048_576
# A non-streaming completion or embedding batch is one JSON object; a larger
# successful body is not a response this runtime will buffer.
POST_BODY_LIMIT = 16 * 1024 * 1024
ENV_ALLOW_HTTP_NETWORKS = "SONDER_OPENAI_ALLOW_HTTP_NETWORKS"
ENV_STREAM_IDLE = "SONDER_OPENAI_STREAM_IDLE_TIMEOUT_SECONDS"
ENV_STREAM_FIRST_CONTENT = "SONDER_OPENAI_STREAM_FIRST_CONTENT_TIMEOUT_SECONDS"
# Streaming stall limits (0 disables, the default): see sse_stream.
MAX_STREAM_STALL_SECONDS = 86_400.0
_MALFORMED_BASE_URL = (
    "OpenAI-compatible base URL is not a valid http:// or https:// URL "
    "(check the host, the port, and for spaces or control characters)"
)
_PERCENT_IN_HOST = "OpenAI-compatible base URL host must not contain percent-encoding"

def shared_https_context() -> ssl.SSLContext:
    """The verifying TLS context for model-endpoint openers, shared in-process.

    See :mod:`sonder_runtime.adapters.tls_contexts`: an opener built per
    request otherwise reloads the platform trust store (~150-200 ms on
    Windows) on every request, plain-HTTP loopback included.
    """
    return default_https_context()


def _opener_for(url: str):
    """A non-redirecting opener; loopback URLs also bypass every proxy.

    urllib has no implicit loopback bypass: with ``http_proxy`` (or a system
    proxy) set and no matching ``no_proxy`` entry, a request to
    ``http://localhost`` -- prompt, bearer key and all -- goes to the proxy
    host.  Remote endpoints keep proxy support (they are https, so the proxy
    only sees a CONNECT tunnel).  Built per call so the proxy decision reflects
    the current environment rather than the one at import time.
    """
    handlers: list = [_NoRedirect(), urllib.request.HTTPSHandler(context=shared_https_context())]
    if (urlsplit(url).hostname or "").lower() in _LOOPBACK_HOSTS:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _stream_transport(url: str, payload: dict, headers: dict, timeout) -> dict:
    """``"stream": true`` POST: read the event stream into one completion.

    Reuses the Sonder Inference stream reader (:mod:`.sse_stream`) with this
    module's opener policy (no redirects; loopback bypasses every proxy).  The
    exchange helpers live in the leaf :mod:`.exchange_transport`, which both
    gateways import, so there is no import cycle; they are handed to
    ``post_streaming`` as a namespace.
    """
    def error_fields(document: object) -> tuple[str, str]:
        # A remote endpoint's message text is not relayed (the non-streaming
        # path emits only the HTTP status); a short token-like code is kept.
        code = _error_document_fields(document)[0]
        return (code if re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", code) else "", "")

    exchange = SimpleNamespace(
        DEFAULT_TIMEOUT_SECONDS=_EXCHANGE_DEFAULT_TIMEOUT,
        ERROR_BODY_LIMIT=ERROR_BODY_LIMIT,
        RESPONSE_BODY_LIMIT=_EXCHANGE_RESPONSE_LIMIT,
        _ExchangeBudget=_ExchangeBudget,
        _NoRedirect=_NoRedirect,
        _BudgetedHTTPHandler=_BudgetedHTTPHandler,
        _BudgetedHTTPSHandler=_BudgetedHTTPSHandler,
        _read_bounded=_read_bounded,
        _error_document_fields=error_fields,
    )

    def opener_factory(budget):
        handlers: list = [
            _NoRedirect(),
            _BudgetedHTTPHandler(budget), _BudgetedHTTPSHandler(budget),
        ]
        if (urlsplit(url).hostname or "").lower() in _LOOPBACK_HOSTS:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    return post_streaming(
        url, payload, headers, timeout or _DEFAULT_TIMEOUT, stream_sink.call_stream(),
        exchange=exchange, opener_factory=opener_factory, label="endpoint",
    )


def _split_networks(raw: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in str(raw or "").replace(";", ",").split(",")
        if item.strip()
    )

# Hook shapes (all optional, all additive; see OpenAICompatibleGateway).
ExtraHeaders = Callable[[OperationContext], Mapping[str, str]]
HttpErrorClassifier = Callable[[int, bytes], "SonderError | None"]
ConnectErrorClassifier = Callable[[BaseException], "SonderError | None"]
GetTransport = Callable[[str, dict, float], "tuple[int, bytes]"]

# Static, provider-shape facts — never a live probe result.  One configured
# endpoint and model serve every request; there is no per-request local/cloud
# tier resolution the way the Ollama gateway has.
CAPABILITIES = frozenset({
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_EMBEDDINGS,
    GATEWAY_CAPABILITY_FIXED_ENDPOINT,
})


class BackendAdmissionRefused(CapacityExceeded):
    """Local GPU/host-memory admission refused the request.

    ``reason`` is the ``RefusalReason`` value; ``retry_after`` is in seconds
    (or None)."""

    def __init__(self, message: str, *, reason: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_after = retry_after


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str = ""
    model: str = ""
    embed_model: str = ""
    # CIDRs whose private IP-literal hosts may be reached over plain http.
    plaintext_networks: tuple[str, ...] = ()
    # Streaming stall limits in seconds (0 disables); inside the call budget.
    stream_idle_seconds: float = 0.0
    stream_first_content_seconds: float = 0.0

    def __post_init__(self) -> None:
        for value, name in ((self.stream_idle_seconds, ENV_STREAM_IDLE),
                            (self.stream_first_content_seconds, ENV_STREAM_FIRST_CONTENT)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                0.0 <= float(value) <= MAX_STREAM_STALL_SECONDS
            ):
                raise InvalidInput("%s must be in [0, 86400] (0 disables it)" % name)


def _env_seconds(name: str) -> float:
    import os

    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise InvalidInput("%s must be a number, got %r" % (name, raw)) from exc
    if value != value:  # NaN
        raise InvalidInput("%s must be a number, got %r" % (name, raw))
    return value


def _config_from_env() -> OpenAICompatibleConfig:
    import os

    return OpenAICompatibleConfig(
        base_url=os.environ.get("SONDER_OPENAI_BASE_URL", "").strip(),
        api_key=os.environ.get("SONDER_OPENAI_API_KEY", "").strip(),
        model=os.environ.get("SONDER_OPENAI_MODEL", "").strip(),
        embed_model=os.environ.get("SONDER_OPENAI_EMBED_MODEL", "").strip(),
        plaintext_networks=_split_networks(os.environ.get(ENV_ALLOW_HTTP_NETWORKS, "")),
        stream_idle_seconds=_env_seconds(ENV_STREAM_IDLE),
        stream_first_content_seconds=_env_seconds(ENV_STREAM_FIRST_CONTENT),
    )


class OpenAICompatibleGateway:
    """ModelGateway over any OpenAI-compatible HTTP endpoint.

    ``transport`` is an injection seam ``(url, payload, headers, timeout) -> dict``
    so tests never touch the network; when absent the stdlib urllib transport is
    used. ``config`` overrides env resolution (mainly for tests).

    Additive hooks used by provider adapters that speak this wire format
    (defaults keep the historical behaviour exactly):

    * ``provider_label`` -- the ``dispatch_provider`` label recorded as capture
      evidence; one of ``provider_bindings.PROVIDER_LABEL_IDS``.
    * ``extra_headers(context)`` -- headers computed per call from the
      OperationContext.  They can never replace ``Content-Type`` or
      ``Authorization``.
    * ``http_error_classifier(status, body)`` -- sees the bounded error body
      and may return the domain error to raise instead of the default mapping.
    * ``connect_error_classifier(reason)`` -- same for transport failures that
      happened before any HTTP response (refused, unresolvable, ...).
    * ``get_transport(url, headers, timeout) -> (status, body)`` -- the GET
      seam behind :meth:`get_json`; it returns non-2xx statuses instead of
      raising so callers can read error bodies such as a 503 health document.
    """

    def __init__(
        self, config: OpenAICompatibleConfig | None = None, *,
        transport=None, request_admission: HostModelRequestAdmission | None = None,
        provider_label: str = "openai-compatible",
        extra_headers: ExtraHeaders | None = None,
        http_error_classifier: HttpErrorClassifier | None = None,
        connect_error_classifier: ConnectErrorClassifier | None = None,
        get_transport: GetTransport | None = None,
    ):
        self._provider_label = str(provider_label)
        # Fail at construction on a label that would not map to a provider id.
        self._provider_id = provider_id_for_label(self._provider_label)
        self._extra_headers = extra_headers
        self._http_error_classifier = http_error_classifier
        self._connect_error_classifier = connect_error_classifier
        self._get_transport = get_transport
        self._config = config
        self._transport = transport
        self._request_admission = (
            host_model_request_admission()
            if request_admission is None else request_admission
        )
        if not isinstance(self._request_admission, HostModelRequestAdmission):
            raise TypeError("request admission must be host-owned")
        if config is not None:
            loopback = self._is_loopback(config.base_url)
            logger.info(
                f"OpenAI-compatible gateway created, "
                f"base_url={'loopback' if loopback else 'remote'}, "
                f"model={config.model or 'from-env'}"
            )
        else:
            logger.info("OpenAI-compatible gateway created with deferred env config")

    @property
    def capabilities(self) -> frozenset[str]:
        """Typed capability metadata; shape matches ``ProviderHealth.capabilities``."""
        return CAPABILITIES

    @property
    def request_admission(self) -> HostModelRequestAdmission:
        return self._request_admission

    # -- configuration & consent ------------------------------------------

    def _resolved_config(self) -> OpenAICompatibleConfig:
        cfg = self._config or _config_from_env()
        if not cfg.base_url:
            raise InvalidInput(
                "no OpenAI-compatible endpoint configured "
                "(set SONDER_OPENAI_BASE_URL)"
            )
        return cfg

    @staticmethod
    def _is_loopback(base_url: str) -> bool:
        # Unparsable is never loopback; _require_secure_transport refuses it.
        parts = urlsplit_or_none(base_url)
        host = (parts.hostname or "").lower() if parts is not None else ""
        return host in _LOOPBACK_HOSTS

    def _enforce_consent(self, cfg: OpenAICompatibleConfig, context: OperationContext) -> None:
        if not self._is_loopback(cfg.base_url) and not context.cloud_allowed:
            raise Forbidden(
                "endpoint %r is non-loopback but this operation context does "
                "not allow cloud" % redact_url(cfg.base_url)
            )

    def _require_secure_transport(self, cfg: OpenAICompatibleConfig) -> None:
        """Refuse plaintext http to anything but loopback or a trusted private host.

        Applies to every request -- prompt-bearing or not -- because every
        request can carry the bearer key.  Cloud consent does not relax it:
        consent is about where prompts go, not about sending them in clear.
        """
        # Nothing malformed may reach the transport: urllib and http.client
        # quote the text they reject (a port, a netloc, a path and query) in
        # their errors. Every refusal here has fixed text and no chained error.
        parts = urlsplit_or_none(cfg.base_url)
        if parts is None:
            raise InvalidInput(_MALFORMED_BASE_URL)
        if "@" in parts.netloc:
            # urllib never sends URL userinfo as auth (the key goes in a header),
            # and http.client's error for it repeats the password, so refuse it
            # before any request is built.
            raise InvalidInput(
                "OpenAI-compatible base URL must not contain credentials "
                "(user:password@); set the API key instead"
            )
        if "%" in parts.netloc:
            # urllib unquotes the host, so "%3A" and "%40" become a port and
            # userinfo that http.client then quotes back in its InvalidURL; an
            # IPv6 zone id ("%25") is refused with them. A "%" in the path is fine.
            raise InvalidInput(_PERCENT_IN_HOST)
        if "@" in cfg.base_url.split("?", 1)[0].split("#", 1)[0]:
            # A password with an unescaped "/" parses as a path, so an "@" in
            # the path cannot be told apart from credentials.
            raise InvalidInput(
                "OpenAI-compatible base URL must not contain '@' before its "
                "query string: it cannot be told apart from credentials "
                "(user:password@). Write an '@' that belongs to the path as %40"
            )
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise InvalidInput("OpenAI-compatible endpoint must use http or https")
        if not parts.hostname or _UNSENDABLE_URL_CHARS.search(cfg.base_url):
            raise InvalidInput(_MALFORMED_BASE_URL)
        if scheme == "https" or self._is_loopback(cfg.base_url):
            return
        if self._trusted_plaintext_host(parts.hostname or "", cfg.plaintext_networks):
            return
        raise Forbidden(
            "OpenAI-compatible endpoint %s is not loopback and uses plaintext "
            "http; use an https:// base URL, or list its private network in %s "
            "to allow http to that private IP address"
            % (redact_url(cfg.base_url), ENV_ALLOW_HTTP_NETWORKS)
        )

    @staticmethod
    def _trusted_plaintext_host(host: str, networks: Sequence[str]) -> bool:
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return False  # a host name: DNS, not the operator, picks the peer
        if not address.is_private or address.is_global:
            return False
        for entry in networks:
            try:
                network = ipaddress.ip_network(str(entry).strip(), strict=False)
            except ValueError as exc:
                raise InvalidInput(
                    "%s entry is not a CIDR: %r" % (ENV_ALLOW_HTTP_NETWORKS, entry)
                ) from exc
            if not network.is_private:
                continue  # only private ranges can be trusted for plaintext
            if address.version == network.version and address in network:
                return True
        return False

    @staticmethod
    def _check_liveness(
        context: OperationContext, *, phase: str = "before model call"
    ) -> float | None:
        if context.expired:
            raise DeadlineExceeded(f"operation deadline exceeded {phase}")
        if context.cancellation is not None and context.cancellation.cancelled:
            raise Cancelled(f"operation cancelled {phase}")
        return context.remaining_seconds

    # -- generate ----------------------------------------------------------

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        cfg = self._resolved_config()
        self._enforce_consent(cfg, context)
        timeout = self._check_liveness(context)

        options = dict(request.options or {})
        live = stream_sink.call_stream()
        model = str(options.get("model") or cfg.model or request.tier or "default")
        logger.debug(f"OpenAICompatibleGateway.generate: model={model!r}, base_url={redact_url(cfg.base_url)!r}, timeout={timeout}")
        payload = {
            "model": model,
            "messages": self._build_messages(request),
            # The caller's live stream (claimed only for a streaming provider)
            # decides: a streaming call reads the event stream and returns the
            # same aggregated document, so everything below is unchanged.
            "stream": live is not None,
            "temperature": float(options.get("temperature", 0.2)),
        }
        if live is not None:
            payload["stream_options"] = {"include_usage": True}
        try:
            template_kwargs = normalize_chat_template_options(options)
        except ChatTemplateOptionsError as exc:
            raise InvalidInput(str(exc)) from exc
        if template_kwargs:
            payload["chat_template_kwargs"] = template_kwargs
        if "num_predict" in options:
            payload["max_tokens"] = int(options["num_predict"])

        started = time.monotonic()
        with stall_limits(
            cfg.stream_idle_seconds, cfg.stream_first_content_seconds,
            idle_setting=ENV_STREAM_IDLE, first_content_setting=ENV_STREAM_FIRST_CONTENT,
        ):
            data = self._post("/v1/chat/completions", payload, cfg, timeout, context=context)
        # urllib cannot observe a token while blocked.  A final liveness gate
        # prevents a response/cancellation race from publishing stale work.
        self._check_liveness(context, phase="during model call")
        text = self._extract_text(data)
        usage = data.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise DependencyUnavailable("endpoint returned an invalid usage object")
        timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
        telemetry = from_openai_compatible(data)
        prompt_count = usage.get("prompt_tokens")
        if prompt_count is None:
            prompt_count = timings.get("prompt_n")
        output_count = usage.get("completion_tokens")
        if output_count is None:
            output_count = timings.get("predicted_n")
        duration_ms = int((time.monotonic() - started) * 1000)
        response = ModelResponse(
            text=require_model_text(text),
            model=model,
            tier=request.tier or "openai",
            duration_ms=duration_ms,
            tokens_in=optional_token_count(prompt_count, "prompt token count"),
            tokens_out=optional_token_count(output_count, "completion token count"),
            telemetry=telemetry,
        )
        if duration_ms > 60_000:
            logger.warning(
                f"slow inference: model={model!r} took {duration_ms}ms "
                f"(>{60_000}ms threshold), tokens_out={response.tokens_out}"
            )
        logger.debug(
            f"OpenAICompatibleGateway.generate: completed in {duration_ms}ms, "
            f"tokens_in={response.tokens_in}, tokens_out={response.tokens_out}"
        )
        default_registry().observe_inference(self._provider_id, telemetry)
        return response

    # -- embed -------------------------------------------------------------

    def embed(
        self, texts: Sequence[str], context: OperationContext
    ) -> Sequence[Embedding]:
        items = list(texts)
        cfg = self._resolved_config()
        self._enforce_consent(cfg, context)
        timeout = self._check_liveness(context)
        model = cfg.embed_model or "text-embedding-3-small"
        logger.debug(f"OpenAICompatibleGateway.embed: text_count={len(items)}, model={model!r}")
        data = self._post(
            "/v1/embeddings", {"model": model, "input": items}, cfg, timeout,
            context=context,
        )
        self._check_liveness(context, phase="during embedding call")
        rows = data.get("data") or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DependencyUnavailable("endpoint returned invalid embedding data")
        if len(rows) != len(items):
            raise DependencyUnavailable(
                "endpoint returned %d embeddings for %d inputs"
                % (len(rows), len(items))
            )
        indexes = [row.get("index") for row in rows]
        if all(isinstance(index, int) for index in indexes):
            if set(indexes) != set(range(len(items))):
                raise DependencyUnavailable("endpoint returned invalid embedding indexes")
            # OpenAI-compatible servers identify input alignment by index; using
            # response order attached vectors to the wrong texts when rows arrived
            # out of order.
            rows = sorted(rows, key=lambda row: row["index"])
        results = []
        for row in rows:
            vector = require_embedding_vector(row.get("embedding"))
            results.append(Embedding(vector=vector, model=model))
        return results

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _build_messages(request: ModelRequest) -> list:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for entry in request.history or ():
            if isinstance(entry, dict) and entry.get("role") and "content" in entry:
                messages.append({"role": entry["role"], "content": entry["content"]})
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                messages.append({"role": entry[0], "content": entry[1]})
        messages.append({"role": "user", "content": request.prompt})
        return messages

    @staticmethod
    def _extract_text(data: dict) -> str:
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise DependencyUnavailable("endpoint returned no choices")
        if not isinstance(choices[0], dict):
            raise DependencyUnavailable("endpoint returned an invalid choice")
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            raise DependencyUnavailable("endpoint returned an invalid message")
        text = message.get("content")
        try:
            return require_model_text(text)
        except DependencyUnavailable as exc:
            # A thinking-only completion can legitimately contain no visible
            # text when the provider consumed its cap. Preserve the provider's
            # finish reason so the agent decision loop can perform its bounded
            # length repair instead of treating this as an opaque outage.
            if (choices[0].get("finish_reason") == "length"
                    and (text is None or isinstance(text, str) and not text.strip())):
                raise ModelCallError(
                    "empty_response",
                    '{"done_reason": "length"}: %s' % exc,
                ) from exc
            raise

    def _headers(
        self, cfg: OpenAICompatibleConfig,
        context: OperationContext | None = None,
    ) -> dict:
        headers: dict[str, str] = {}
        if context is not None and self._extra_headers is not None:
            for name, value in dict(self._extra_headers(context)).items():
                if str(name).lower() not in ("content-type", "authorization"):
                    headers[str(name)] = str(value)
        headers["Content-Type"] = "application/json"
        if cfg.api_key:
            headers["Authorization"] = "Bearer %s" % cfg.api_key
        return headers

    @staticmethod
    def _bounded_error_body(exc: urllib.error.HTTPError) -> bytes:
        try:
            body = exc.read(ERROR_BODY_LIMIT)
        except Exception:  # noqa: BLE001 - an unreadable error page has no code
            return b""
        return body if isinstance(body, bytes) else b""

    @staticmethod
    def _safe_http_cause(code: int, retry_after: object) -> urllib.error.HTTPError:
        """Retain retry timing without the response URL, reason or other headers."""
        header = None
        if isinstance(retry_after, (str, int, float)):
            try:
                header = str(float(retry_after))
            except (ValueError, TypeError, OverflowError):
                if isinstance(retry_after, str):
                    try:
                        header = format_datetime(parsedate_to_datetime(retry_after))
                    except (ValueError, TypeError, OverflowError):
                        pass
        headers = {} if header is None else {"Retry-After": header}
        return urllib.error.HTTPError("", code, "endpoint HTTP response", headers, None)

    def _classified_connect_error(self, reason: BaseException) -> SonderError | None:
        if self._connect_error_classifier is None:
            return None
        return self._connect_error_classifier(reason)

    @staticmethod
    def _backend_admission(
        path: str, payload: dict, cfg: OpenAICompatibleConfig,
        context: OperationContext | None,
    ):
        """GPU-slot admission for a chat request to a declared streamed model.

        It takes the shared GPU slot (queueing for it), and checks host memory
        only when a residency callback answers False; production passes none,
        so the gateway path queues but does not refuse on memory. Only a loopback endpoint whose model has a declaration in
        ``SONDER_GPU_STREAMED_MODELS`` is gated; everything else passes through.
        A remote endpoint never touches admission. A refusal raises
        ``GpuAdmissionRefused``, which ``_post`` maps to a domain error; an
        invalid declaration on a loopback endpoint raises ``InternalFailure``."""
        model = payload.get("model")
        if path != "/v1/chat/completions" or not isinstance(model, str) or not model:
            return contextlib.nullcontext()
        try:
            service = gpu_admission.backend_service_for(cfg.base_url)
        except ValueError as error:
            raise InternalFailure("GPU admission is misconfigured: %s" % error) from error
        if service is None:
            return contextlib.nullcontext()
        footprint = service.declared_backend_footprint(model)
        if footprint is None:
            return contextlib.nullcontext()
        deadline = context.deadline_monotonic if context is not None else None
        return service.admit_backend(
            model, origin=cfg.base_url, footprint=footprint, deadline=deadline)

    def _post(
        self, path: str, payload: dict, cfg: OpenAICompatibleConfig, timeout,
        *, context: OperationContext | None = None,
    ) -> dict:
        self._require_secure_transport(cfg)
        url = cfg.base_url.rstrip("/") + path
        safe_url = redact_url(url)
        logger.debug(f"OpenAICompatibleGateway._post: url={safe_url!r}, timeout={timeout}")
        transport = self._transport or self._default_transport
        if context is not None:
            self._check_liveness(context, phase="before provider send")
        admission = self._request_admission.try_acquire()
        if admission is not None and not admission.allowed:
            raise CapacityExceeded(
                "host model request rate admission refused; "
                f"retry after about {admission.retry_after:.3f}s"
            )
        headers = self._headers(cfg, context)
        failure: SonderError | None = None
        http_cause: urllib.error.HTTPError | None = None
        cause: BaseException | None = None
        gate = self._backend_admission(path, payload, cfg, context)
        try:
            with gate:
                if context is not None:
                    # The admission wait can be long; the request may have been
                    # cancelled or expired meanwhile, and the transport budget
                    # must be what is left, not what was left before the wait.
                    left = self._check_liveness(context, phase="after local admission")
                    if left is not None:
                        timeout = left if timeout is None else min(timeout, left)
                if path == "/v1/chat/completions":
                    data = dispatch_provider(
                        self._provider_label, path, payload,
                        lambda: transport(url, payload, headers, timeout),
                    )
                else:
                    data = transport(url, payload, headers, timeout)
        except gpu_admission.GpuAdmissionRefused as exc:
            # A typed refusal, not a transport failure; it must stay ahead of
            # the URLError clause because it is one.
            cause = exc
            refusal = exc.refusal
            if refusal.reason is RefusalReason.STORE_UNAVAILABLE:
                failure = DependencyUnavailable("GPU admission store is unavailable")
            else:
                failure = BackendAdmissionRefused(
                    "local model admission refused (%s): %s"
                    % (refusal.reason.value, refusal.detail),
                    reason=refusal.reason.value, retry_after=exc.retry_after_seconds)
        except urllib.error.HTTPError as exc:
            code = getattr(exc, "code", 0)
            retry_after = next((value for key, value in (exc.headers or {}).items()
                                if str(key).lower() == "retry-after"), None)
            http_cause = self._safe_http_cause(int(code or 0), retry_after)
            if self._http_error_classifier is not None:
                failure = self._http_error_classifier(
                    int(code or 0), self._bounded_error_body(exc),
                )
            if failure is None:
                if code in (401, 403):
                    logger.error(
                        f"authentication failed for OpenAI-compatible endpoint, "
                        f"http_status={code}, url={safe_url!r}"
                    )
                    failure = Forbidden("endpoint rejected credentials (HTTP %d)" % code)
                elif code in (400, 404, 422):
                    failure = InvalidInput("endpoint rejected request (HTTP %d)" % code)
                elif code == 429:
                    logger.warning(
                        f"rate limited by OpenAI-compatible endpoint (HTTP 429), "
                        f"url={safe_url!r}"
                    )
                    failure = CapacityExceeded(
                        "endpoint is rate limiting requests (HTTP 429)"
                    )
                else:
                    logger.error(
                        f"OpenAI-compatible endpoint returned server error, "
                        f"http_status={code}, url={safe_url!r}"
                    )
                    failure = DependencyUnavailable("endpoint returned HTTP %d" % code)
        except StreamTimeout as exc:
            logger.warning(
                f"OpenAI-compatible endpoint {exc.kind} timeout: url={safe_url!r}, "
                f"timeout={timeout}"
            )
            # The text names the limit and the elapsed numbers; it carries no URL.
            failure = DeadlineExceeded(str(exc))
        except (socket.timeout, TimeoutError):
            logger.warning(
                f"OpenAI-compatible endpoint timed out: url={safe_url!r}, "
                f"timeout={timeout}"
            )
            failure = DeadlineExceeded("endpoint timed out")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                logger.warning(
                    f"OpenAI-compatible endpoint timed out (URLError): "
                    f"url={safe_url!r}, timeout={timeout}"
                )
                failure = DeadlineExceeded("endpoint timed out")
            else:
                logger.warning(
                    f"OpenAI-compatible endpoint unreachable: url={safe_url!r}, "
                    f"error={type(reason).__name__}"
                )
                failure = self._classified_connect_error(
                    reason if isinstance(reason, BaseException) else exc
                )
                if failure is None:
                    failure = DependencyUnavailable("cannot reach endpoint")
        except http.client.HTTPException as exc:
            # The class name is safe; the transport text can contain the URL.
            malformed = type(exc).__name__
            logger.warning(
                f"OpenAI-compatible endpoint sent a malformed HTTP response: "
                f"url={safe_url!r}, error={malformed}"
            )
            failure = DependencyUnavailable(
                "endpoint sent a malformed HTTP response (%s)" % malformed
            )
        except OSError as exc:
            failure = self._classified_connect_error(exc)
            if failure is None:
                logger.error(
                    f"OpenAI-compatible endpoint unreachable (OSError), url={safe_url!r}, "
                    f"error={type(exc).__name__}"
                )
                failure = DependencyUnavailable("cannot reach endpoint")
        # Raise after the handlers so no raw transport error becomes context.
        if failure is not None:
            raise failure from (cause if cause is not None else http_cause)
        if not isinstance(data, dict):
            raise InternalFailure("endpoint transport returned a non-object response")
        return data

    def get_json(
        self, path: str, *, timeout: float,
        cfg: OpenAICompatibleConfig | None = None,
    ) -> tuple[int, dict | None]:
        """GET ``path`` on the configured endpoint; return ``(status, object)``.

        Shares the POST path's credentials and transport error mapping
        (including ``connect_error_classifier``).  Non-2xx statuses are
        returned, not raised, so a caller can read a structured error
        document.  ``object`` is ``None`` when the body is not a JSON object.
        Consent is the caller's responsibility: this helper sends no prompt
        content, but it does send credentials, so callers enforce their
        endpoint policy before calling it.
        """
        cfg = cfg or self._resolved_config()
        self._require_secure_transport(cfg)
        url = cfg.base_url.rstrip("/") + path
        headers = self._headers(cfg)
        headers.pop("Content-Type", None)
        headers["Accept"] = "application/json"
        transport = self._get_transport or self._default_get_transport
        failure: SonderError | None = None
        try:
            status, body = transport(url, headers, float(timeout))
        except (socket.timeout, TimeoutError):
            failure = DeadlineExceeded("endpoint timed out")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                failure = DeadlineExceeded("endpoint timed out")
            else:
                failure = self._classified_connect_error(
                    reason if isinstance(reason, BaseException) else exc
                )
                if failure is None:
                    failure = DependencyUnavailable("cannot reach endpoint")
        except http.client.HTTPException as exc:
            failure = DependencyUnavailable(
                "endpoint sent a malformed HTTP response (%s)" % type(exc).__name__
            )
        except OSError as exc:
            failure = self._classified_connect_error(exc)
            if failure is None:
                failure = DependencyUnavailable("cannot reach endpoint")
        if failure is not None:
            raise failure
        if not isinstance(body, (bytes, bytearray)) or len(body) > GET_BODY_LIMIT:
            raise DependencyUnavailable("endpoint returned an oversized or invalid body")
        try:
            value = json.loads(bytes(body).decode("utf-8")) if body else None
        except (UnicodeDecodeError, ValueError, RecursionError):
            value = None
        return int(status), value if isinstance(value, dict) else None

    @staticmethod
    def _default_get_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        # Never follow a redirect: it would re-send the Authorization header
        # to whatever host the Location names.  A 3xx is returned as-is.
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with _opener_for(url).open(req, timeout=timeout or _DEFAULT_TIMEOUT) as resp:
                return int(resp.status), resp.read(GET_BODY_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(GET_BODY_LIMIT + 1)
            except Exception:  # noqa: BLE001 - the status alone still answers
                body = b""
            return int(exc.code), body if isinstance(body, bytes) else b""

    @staticmethod
    def _default_transport(url: str, payload: dict, headers: dict, timeout) -> dict:
        if payload.get("stream") is True:
            return _stream_transport(url, payload, headers, timeout)
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        # Never follow a redirect: urllib would turn the POST into a GET and
        # copy the Authorization header to whatever origin Location names.  A
        # 3xx surfaces as an HTTPError and maps to DependencyUnavailable.
        limit = POST_BODY_LIMIT
        with _opener_for(url).open(req, timeout=timeout or _DEFAULT_TIMEOUT) as resp:
            raw = _read_bounded(resp, limit)
        if len(raw) > limit:
            raise DependencyUnavailable("endpoint response exceeds %d bytes" % limit)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise DependencyUnavailable("endpoint returned non-JSON") from exc
