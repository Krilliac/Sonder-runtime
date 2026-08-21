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
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlsplit

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    optional_token_count,
    require_embedding_vector,
    require_model_text,
)
from ...platform.metrics import default_registry
from .telemetry import from_openai_compatible
from ...domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InternalFailure,
    InvalidInput,
)
from ...domain.chat_template_policy import (
    ChatTemplateOptionsError,
    normalize_chat_template_options,
)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_DEFAULT_TIMEOUT = 300


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
    """

    def __init__(self, config: OpenAICompatibleConfig | None = None, *, transport=None):
        self._config = config
        self._transport = transport

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
    def _check_liveness(context: OperationContext) -> float | None:
        if context.expired:
            raise DeadlineExceeded("operation deadline exceeded before model call")
        if context.cancellation is not None and context.cancellation.cancelled:
            raise Cancelled("operation cancelled before model call")
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
        data = self._post("/v1/chat/completions", payload, cfg, timeout)
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
        response = ModelResponse(
            text=require_model_text(text),
            model=model,
            tier=request.tier or "openai",
            duration_ms=int((time.monotonic() - started) * 1000),
            tokens_in=optional_token_count(prompt_count, "prompt token count"),
            tokens_out=optional_token_count(output_count, "completion token count"),
            telemetry=telemetry,
        )
        default_registry().observe_inference("openai_compatible", telemetry)
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
        data = self._post(
            "/v1/embeddings", {"model": model, "input": items}, cfg, timeout
        )
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

    def _headers(self, cfg: OpenAICompatibleConfig) -> dict:
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = "Bearer %s" % cfg.api_key
        return headers

    def _post(
        self, path: str, payload: dict, cfg: OpenAICompatibleConfig, timeout
    ) -> dict:
        url = cfg.base_url.rstrip("/") + path
        transport = self._transport or self._default_transport
        try:
            data = transport(url, payload, self._headers(cfg), timeout)
        except urllib.error.HTTPError as exc:
            code = getattr(exc, "code", 0)
            if code in (401, 403):
                raise Forbidden("endpoint rejected credentials (HTTP %d)" % code) from exc
            if code in (400, 404, 422):
                raise InvalidInput("endpoint rejected request (HTTP %d)" % code) from exc
            raise DependencyUnavailable("endpoint returned HTTP %d" % code) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise DeadlineExceeded("endpoint timed out") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise DeadlineExceeded("endpoint timed out") from exc
            raise DependencyUnavailable("cannot reach endpoint: %s" % reason) from exc
        except OSError as exc:
            raise DependencyUnavailable("cannot reach endpoint: %s" % exc) from exc
        if not isinstance(data, dict):
            raise InternalFailure("endpoint transport returned a non-object response")
        return data

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
