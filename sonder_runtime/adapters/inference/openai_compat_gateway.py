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
``SONDER_OPENAI_EMBED_MODEL``.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

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
from ..provider_bindings import provider_id_for_label
from .telemetry import from_openai_compatible

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_DEFAULT_TIMEOUT = 300
# Error bodies and GET responses are read with a hard bound: an error page is
# only ever inspected for a machine-readable code, never stored or relayed.
ERROR_BODY_LIMIT = 16_384
GET_BODY_LIMIT = 1_048_576

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


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str = ""
    model: str = ""
    embed_model: str = ""


def _config_from_env() -> OpenAICompatibleConfig:
    import os

    return OpenAICompatibleConfig(
        base_url=os.environ.get("SONDER_OPENAI_BASE_URL", "").strip(),
        api_key=os.environ.get("SONDER_OPENAI_API_KEY", "").strip(),
        model=os.environ.get("SONDER_OPENAI_MODEL", "").strip(),
        embed_model=os.environ.get("SONDER_OPENAI_EMBED_MODEL", "").strip(),
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
        host = (urlsplit(base_url).hostname or "").lower()
        return host in _LOOPBACK_HOSTS

    def _enforce_consent(self, cfg: OpenAICompatibleConfig, context: OperationContext) -> None:
        if not self._is_loopback(cfg.base_url) and not context.cloud_allowed:
            raise Forbidden(
                "endpoint %r is non-loopback but this operation context does "
                "not allow cloud" % cfg.base_url
            )

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
        model = str(options.get("model") or cfg.model or request.tier or "default")
        logger.debug(f"OpenAICompatibleGateway.generate: model={model!r}, base_url={cfg.base_url!r}, timeout={timeout}")
        payload = {
            "model": model,
            "messages": self._build_messages(request),
            "stream": False,
            "temperature": float(options.get("temperature", 0.2)),
        }
        try:
            template_kwargs = normalize_chat_template_options(options)
        except ChatTemplateOptionsError as exc:
            raise InvalidInput(str(exc)) from exc
        if template_kwargs:
            payload["chat_template_kwargs"] = template_kwargs
        if "num_predict" in options:
            payload["max_tokens"] = int(options["num_predict"])

        started = time.monotonic()
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
        return require_model_text(text)

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

    def _classified_connect_error(self, reason: BaseException) -> SonderError | None:
        if self._connect_error_classifier is None:
            return None
        return self._connect_error_classifier(reason)

    def _post(
        self, path: str, payload: dict, cfg: OpenAICompatibleConfig, timeout,
        *, context: OperationContext | None = None,
    ) -> dict:
        url = cfg.base_url.rstrip("/") + path
        logger.debug(f"OpenAICompatibleGateway._post: url={url!r}, timeout={timeout}")
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
        try:
            if path == "/v1/chat/completions":
                data = dispatch_provider(
                    self._provider_label, path, payload,
                    lambda: transport(url, payload, headers, timeout),
                )
            else:
                data = transport(url, payload, headers, timeout)
        except urllib.error.HTTPError as exc:
            code = getattr(exc, "code", 0)
            if self._http_error_classifier is not None:
                classified = self._http_error_classifier(
                    int(code or 0), self._bounded_error_body(exc),
                )
                if classified is not None:
                    raise classified from exc
            if code in (401, 403):
                logger.error(
                    f"authentication failed for OpenAI-compatible endpoint, "
                    f"http_status={code}, url={url!r}"
                )
                raise Forbidden("endpoint rejected credentials (HTTP %d)" % code) from exc
            if code in (400, 404, 422):
                raise InvalidInput("endpoint rejected request (HTTP %d)" % code) from exc
            if code == 429:
                logger.warning(
                    f"rate limited by OpenAI-compatible endpoint (HTTP 429), "
                    f"url={url!r}"
                )
                raise CapacityExceeded(
                    "endpoint is rate limiting requests (HTTP 429)"
                ) from exc
            logger.error(
                f"OpenAI-compatible endpoint returned server error, "
                f"http_status={code}, url={url!r}",
                exc_info=True,
            )
            raise DependencyUnavailable("endpoint returned HTTP %d" % code) from exc
        except (socket.timeout, TimeoutError) as exc:
            logger.warning(
                f"OpenAI-compatible endpoint timed out: url={url!r}, "
                f"timeout={timeout}"
            )
            raise DeadlineExceeded("endpoint timed out") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                logger.warning(
                    f"OpenAI-compatible endpoint timed out (URLError): "
                    f"url={url!r}, timeout={timeout}"
                )
                raise DeadlineExceeded("endpoint timed out") from exc
            logger.warning(
                f"OpenAI-compatible endpoint unreachable: url={url!r}, "
                f"reason={reason}"
            )
            classified = self._classified_connect_error(
                reason if isinstance(reason, BaseException) else exc
            )
            if classified is not None:
                raise classified from exc
            raise DependencyUnavailable("cannot reach endpoint: %s" % reason) from exc
        except OSError as exc:
            classified = self._classified_connect_error(exc)
            if classified is not None:
                raise classified from exc
            logger.error(
                f"OpenAI-compatible endpoint unreachable (OSError), url={url!r}",
                exc_info=True,
            )
            raise DependencyUnavailable("cannot reach endpoint: %s" % exc) from exc
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
        url = cfg.base_url.rstrip("/") + path
        headers = self._headers(cfg)
        headers.pop("Content-Type", None)
        headers["Accept"] = "application/json"
        transport = self._get_transport or self._default_get_transport
        try:
            status, body = transport(url, headers, float(timeout))
        except (socket.timeout, TimeoutError) as exc:
            raise DeadlineExceeded("endpoint timed out") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise DeadlineExceeded("endpoint timed out") from exc
            classified = self._classified_connect_error(
                reason if isinstance(reason, BaseException) else exc
            )
            if classified is not None:
                raise classified from exc
            raise DependencyUnavailable("cannot reach endpoint: %s" % reason) from exc
        except OSError as exc:
            classified = self._classified_connect_error(exc)
            if classified is not None:
                raise classified from exc
            raise DependencyUnavailable("cannot reach endpoint: %s" % exc) from exc
        if not isinstance(body, (bytes, bytearray)) or len(body) > GET_BODY_LIMIT:
            raise DependencyUnavailable("endpoint returned an oversized or invalid body")
        try:
            value = json.loads(bytes(body).decode("utf-8")) if body else None
        except (UnicodeDecodeError, ValueError):
            value = None
        return int(status), value if isinstance(value, dict) else None

    @staticmethod
    def _default_get_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or _DEFAULT_TIMEOUT) as resp:
                return int(resp.status), resp.read(GET_BODY_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(GET_BODY_LIMIT + 1)
            except Exception:  # noqa: BLE001 - the status alone still answers
                body = b""
            return int(exc.code), body if isinstance(body, bytes) else b""

    @staticmethod
    def _default_transport(url: str, payload: dict, headers: dict, timeout) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout or _DEFAULT_TIMEOUT) as resp:
            raw = resp.read()
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise DependencyUnavailable("endpoint returned non-JSON") from exc
