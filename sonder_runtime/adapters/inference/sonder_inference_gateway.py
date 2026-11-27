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
* **Embeddings & reranking.** Capability-gated ``/v1/embeddings`` and
  ``/v1/rerank`` execution with explicit model selection, index preservation,
  bounded batching, and host-admission integration.
"""
from __future__ import annotations

import base64
import http.client
import io
import ipaddress
import json
import logging
import math
import numbers
import os
import re
import socket
import ssl  # noqa: F401  (tests patch ssl through this module)
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
from typing import Any, Sequence, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit

from ...application.chat import stream_sink
from ...application.chat.provider_bridge import UnsupportedProviderFeature
from ...application.context import OperationContext, current_operation_context, local_owner_context
from ...application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    RerankScore,
    optional_token_count,
    require_model_text,
)
from ...application.ports.model_gateway_contract import Capability, CapabilityHealth
from .log_safe import redact_url, urlsplit_or_none
from ...domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    SonderError,
)
from ...domain.inference_profiles import (
    CATALOG_ABSENT,
    CATALOG_ERROR,
    CATALOG_OK,
    LaunchProfileCatalog,
    parse_launch_profile_models,
)
from ...domain.model_capabilities import (
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_EMBEDDINGS,
    GATEWAY_CAPABILITY_FIXED_ENDPOINT,
)
from ...domain.routing.backend_conformance import BackendIdentity
from ...domain.routing.prefix_locality import (
    DEFAULT_MAX_LOCALITY_ENTRIES,
    LocalityMatch,
    PrefixLocalityTable,
    digest_key_parts,
    score_endpoint_completion,
)
from ...platform.metrics import default_registry
from ..model_request_admission import HostModelRequestAdmission
from ..observability import activity_tracker
from ..provider_bindings import PROVIDER_TIERS
import sonder_runtime.adapters.inference.launch_profile_cache as launch_profile_cache
from .exchange_transport import (
    DEFAULT_TIMEOUT_SECONDS,
    DETAIL_LIMIT,  # noqa: F401  (re-exported)
    ERROR_BODY_LIMIT,
    RESPONSE_BODY_LIMIT,
    _ACTIVE_CANCELLATION,
    _TRUST,  # noqa: F401  (re-exported)
    _BudgetedHTTPHandler,
    _BudgetedHTTPSHandler,
    _CompositeCancellation,
    _ExchangeBudget,
    _NoRedirect,
    _bounded,
    _cancellation_scope,
    _cancelled_quietly,
    _error_document_fields,
    _is_cancelled,  # noqa: F401  (re-exported)
    _read_bounded,
    _tls_context,  # noqa: F401  (re-exported)
    trust_scope,
)
from .openai_compat_gateway import (
    GET_BODY_LIMIT,
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
    ResidencyProbe,
    shared_https_context,  # noqa: F401  (re-exported)
)
from .request_tuning import thinking_supported, tune_request
from .sse_stream import post_streaming, stall_limits
from .telemetry import from_openai_compatible

logger = logging.getLogger(__name__)

PROVIDER_ID = "sonder_inference"
PROVIDER_LABEL = "sonder-inference"
API_VERSION = 1
IDENTITY_SCHEMA = "sonder.inference.identity/1"
DEFAULT_BASE_URL = "http://127.0.0.1:11437"
DEFAULT_MODEL = "default"
# Streaming stall limits sit inside the total budget above.  0 disables a
# limit, and both default to disabled: Sonder Inference may legitimately send
# no bytes while it prefills a very long prompt on a slow model (nothing
# documents a keep-alive cadence), so no idle value is safe to impose.
DEFAULT_STREAM_IDLE_SECONDS = 0.0
DEFAULT_STREAM_FIRST_CONTENT_SECONDS = 0.0
MAX_STREAM_STALL_SECONDS = 86_400.0
DEFAULT_HEALTH_TTL_SECONDS = 5.0
HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
# ``/v1/models`` launch-profile metadata is optional display and routing
# data; a slow endpoint must not hold a status read or a chat turn long.
MODELS_PROBE_TIMEOUT_SECONDS = 2.0
DEFAULT_HEALTH_STALE_SECONDS = 120.0
BUSY_RETRY_SECONDS = 5.0
READY_FILE_LIMIT = 65_536
# The only host names Inference's Host check accepts on a loopback bind
# (contract 2.3); any other loopback alias would be refused with 403.
ACCEPTED_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

ENV_BASE_URL = "SONDER_INFERENCE_BASE_URL"
ENV_MODEL = "SONDER_INFERENCE_MODEL"
ENV_EMBED_MODEL = "SONDER_INFERENCE_EMBED_MODEL"
ENV_RERANK_MODEL = "SONDER_INFERENCE_RERANK_MODEL"
ENV_TIER_MODELS = "SONDER_INFERENCE_TIER_MODELS"
ENV_API_KEY = "SONDER_INFERENCE_API_KEY"
ENV_ALLOW_REMOTE = "SONDER_ALLOW_REMOTE_INFERENCE"
ENV_READY_FILE = "SONDER_INFERENCE_READY_FILE"
ENV_TIMEOUT = "SONDER_INFERENCE_TIMEOUT_SECONDS"
ENV_STREAM_IDLE = "SONDER_INFERENCE_STREAM_IDLE_TIMEOUT_SECONDS"
ENV_STREAM_FIRST_CONTENT = "SONDER_INFERENCE_STREAM_FIRST_CONTENT_TIMEOUT_SECONDS"
ENV_HEALTH_TTL = "SONDER_INFERENCE_HEALTH_TTL_SECONDS"
ENV_HEALTH_TIMEOUT = "SONDER_INFERENCE_HEALTH_TIMEOUT_SECONDS"
ENV_HEALTH_STALE = "SONDER_INFERENCE_HEALTH_STALE_SECONDS"
ENV_FALLBACK = "SONDER_INFERENCE_FALLBACK"
ENV_PRIVATE_WORKERS = "SONDER_INFERENCE_PRIVATE_WORKERS"
ENV_MAX_INFLIGHT = "SONDER_INFERENCE_MAX_INFLIGHT"
ENV_NATIVE_TOOLS = "SONDER_INFERENCE_NATIVE_TOOLS"
LATENCY_EWMA_ALPHA = 0.3
MAX_PRIVATE_WORKERS = 8
MAX_WORKER_INFLIGHT = 16
MAX_TOOLS_PER_REQUEST = 128
MAX_RESPONSE_TOOL_CALLS = 64
MIN_WORKER_TOKEN_LENGTH = 16
PRIVATE_WORKERS_LIMIT = 16_384
NATIVE_TOOL_MARKERS = frozenset({"tool_calling", "tools", "native_tools"})
STRUCTURED_OUTPUT_MARKERS = frozenset({"structured_output", "response_format", "json_schema"})
LOGPROBS_MARKERS = frozenset({"logprobs", "token_logprobs"})
EMBEDDING_MARKERS = frozenset({"embeddings", "embedding"})
RERANK_MARKERS = frozenset({"rerank", "reranking", "cross_encoder"})
MAX_TOP_LOGPROBS = 20
MAX_EMBED_INPUTS = 256
DEFAULT_RERANK_BATCH_SIZE = 32
MAX_RERANK_BATCH_SIZE = 128
MAX_RERANK_DOCUMENTS = 1024
_CHAR_PER_TOKEN_ESTIMATE = 4
_DEFAULT_EXPECTED_DECODE_TOKENS = 128
_MAX_EXPECTED_DECODE_TOKENS = 2048
_EMPTY_LOCALITY_MATCH = LocalityMatch(0.0, 0.0, 0.0, 0.0, False, 0.0, 0.0)
NATIVE_TOOLS_REFUSAL = (
    "native tool calls are not supported by this sonder-inference endpoint: "
    "the worker does not advertise 'tool_calling' or 'tools' "
    "(upgrade sonder-inference or set %s=on if it honours OpenAI tools)"
    % ENV_NATIVE_TOOLS
)
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_TOOL_FLAG_ON = frozenset({"1", "on", "true", "yes"})
_TOOL_FLAG_OFF = frozenset({"0", "off", "false", "no"})
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
    "busy", "workers", "tier_context",
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
# Requests this gateway always refuses locally (legacy functions and Ollama schema
# format).  OpenAI ``tools``, ``tool_choice``, ``response_format``, ``logprobs``
# and ``embeddings`` are validated locally and gated on advertised server capability.
_UNSUPPORTED_OPTIONS = ("format", "functions")
_ALLOWED_IMAGE_MEDIA_TYPES = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif",
})
_MAX_IMAGES_PER_MESSAGE = 16
_MAX_IMAGE_BASE64_CHARS = 24 * 1024 * 1024
_BASE64_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


def _normalize_image_url(raw: object) -> str:
    if not isinstance(raw, str):
        raise InvalidInput("chat message image must be a base64 string or data URI")
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > _MAX_IMAGE_BASE64_CHARS:
        raise InvalidInput("chat message image must be non-empty and within 24 MiB base64 limit")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise InvalidInput("chat message image must not contain control characters")
    if cleaned.lower().startswith("data:"):
        header, sep, b64_part = cleaned.partition(",")
        if not sep or not header.lower().endswith(";base64"):
            raise InvalidInput("chat message image data URI must use ;base64 encoding")
        media_type = header[5:-7].strip().lower()
        if media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
            raise InvalidInput("unsupported chat message image media type %r" % media_type)
        b64_compact = "".join(b64_part.split())
        if not b64_compact or not _BASE64_TOKEN_RE.fullmatch(b64_compact):
            raise InvalidInput("chat message image data URI contains invalid base64")
        return f"data:{media_type};base64,{b64_compact}"
    b64_compact = "".join(cleaned.split())
    if not b64_compact or not _BASE64_TOKEN_RE.fullmatch(b64_compact):
        raise InvalidInput("chat message image contains invalid base64")
    mime = "image/png"
    try:
        prefix = base64.b64decode(b64_compact[:24], validate=False)
        if prefix.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif prefix.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
            mime = "image/webp"
    except Exception:
        mime = "image/png"
    return f"data:{mime};base64,{b64_compact}"


def _encode_image_content_parts(text: str, raw_images: object) -> list[dict[str, object]]:
    if (
        not isinstance(raw_images, (list, tuple))
        or not raw_images
        or len(raw_images) > _MAX_IMAGES_PER_MESSAGE
    ):
        raise InvalidInput(
            "chat message images must be a non-empty list of at most %d images"
            % _MAX_IMAGES_PER_MESSAGE
        )
    parts: list[dict[str, object]] = [{"type": "text", "text": text}]
    for img in raw_images:
        parts.append({"type": "image_url", "image_url": {"url": _normalize_image_url(img)}})
    return parts


class SonderInferenceUnreachable(DependencyUnavailable):
    """Sonder Inference provably did not execute the request.

    Raised only for: connection refused, an unresolvable host, cached health
    that is not ready, HTTP 503 ``not_ready``, or a proxy 503
    ``upstream_unreachable`` carrying ``X-Sonder-Proxy-Upstream: unreachable``
    (the proxy could not dial the upstream).  It is the sole trigger for the
    fail-closed Ollama fallback (a proxy dial failure on the primary included),
    and keeps ``DEPENDENCY_UNAVAILABLE`` as
    its code so evidence capture treats it as an ordinary provider outage.

    ``reason`` is the short cause ("connection refused") and ``summary`` the
    one-line operator statement without remediation; ``str()`` is the full
    message with the fix.
    """

    kind = "provider_unavailable"

    def __init__(self, message: str, *, reason: str | None = None,
                 summary: str | None = None, timed_out: bool = False) -> None:
        super().__init__(message)
        self.reason = reason or message
        self.summary = summary or message
        # The readiness probe timed out: the endpoint may just be saturated,
        # so placement never cascades this onto an endpoint with no free slot.
        self.timed_out = bool(timed_out)


class SonderInferenceBusyTimeout(DependencyUnavailable):
    """A backend read timeout; retry here once, never on another provider."""

    kind = "busy_timeout"


class SonderInferenceOverloaded(CapacityExceeded):
    """The endpoint reported ``overloaded`` (HTTP 503, or HTTP 429).

    A 429 ``overloaded`` is either the queue-full rejection (nothing executed)
    or the engine-scheduler rejection, which may follow prefill but delivered
    no output; both are retried elsewhere on purpose.

    Still a ``CapacityExceeded`` (never the Ollama fallback); with private
    workers, placement may move it to an endpoint that has a free slot.
    """

    kind = "provider_unavailable"


@dataclass(frozen=True)
class SonderInferenceResponse(ModelResponse):
    """Additive provider measurements without changing other gateways' DTOs."""

    timings: Mapping[str, int | float] | None = None
    finish_reason: str | None = None
    # Scheme and host of the endpoint that served it, when private workers
    # are configured (never a path, query or credential).
    endpoint: str | None = None
    # Normalized OpenAI tool calls returned by a tool-capable worker, or None.
    tool_calls: tuple[Mapping[str, object], ...] | None = None
    # OpenAI-format token logprobs returned when requested, or None.
    logprobs: Mapping[str, object] | None = None


def _native_tools_flag(env: Mapping[str, str] | None) -> str:
    source = os.environ if env is None else env
    raw = str(source.get(ENV_NATIVE_TOOLS, "") or "").strip().lower() or "auto"
    if raw in _TOOL_FLAG_ON:
        return "on"
    if raw in _TOOL_FLAG_OFF:
        return "off"
    if raw == "auto":
        return "auto"
    raise InvalidInput("%s must be auto, on or off, got %r" % (ENV_NATIVE_TOOLS, raw))


def _capability_names(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {str(item).strip().lower() for item, enabled in value.items() if enabled}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {item.strip().lower() for item in value if isinstance(item, str)}
    return set()


def _document_advertises_markers(
    document: Mapping[str, object] | None,
    markers: frozenset[str],
    *,
    model: str | None = None,
) -> bool:
    """Whether a ``sonder-inference`` health document advertises any marker in *markers*."""
    if not isinstance(document, Mapping):
        return False
    containers: list[Mapping[str, object]] = [document]
    extension = document.get("sonder")
    if isinstance(extension, Mapping):
        containers.append(extension)
    for container in containers:
        if (_capability_names(container.get("features")) & markers
                or _capability_names(container.get("capabilities")) & markers):
            return True
    backends_raw = document.get("backends")
    backends = [entry for entry in backends_raw if isinstance(entry, Mapping)] if isinstance(backends_raw, list) else []
    models_raw = document.get("models")
    models = [entry for entry in models_raw if isinstance(entry, Mapping)] if isinstance(models_raw, list) else []
    if model is not None:
        matched_model: Mapping[str, object] | None = None
        if model == DEFAULT_MODEL:
            matched_model = next((entry for entry in models if entry.get("default") is True), None)
        else:
            matched_model = next((entry for entry in models if entry.get("id") == model), None)
        if matched_model is not None:
            model_caps = _capability_names(matched_model.get("capabilities"))
            if model_caps:
                return bool(model_caps & markers)
            backend_name = matched_model.get("backend")
            if isinstance(backend_name, str) and backend_name:
                matching_backends = [b for b in backends if b.get("name") == backend_name]
                if matching_backends:
                    return any(
                        b.get("available") is not False
                        and bool(_capability_names(b.get("capabilities")) & markers)
                        for b in matching_backends
                    )
    for backend in backends:
        if (backend.get("available") is not False
                and _capability_names(backend.get("capabilities")) & markers):
            return True
    if model is None:
        for entry in models:
            if _capability_names(entry.get("capabilities")) & markers:
                return True
    return False


def native_tools_supported(
    document: Mapping[str, object] | None,
    *,
    model: str | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Whether a ``sonder-inference`` health document advertises native tool calling.

    Honours ``SONDER_INFERENCE_NATIVE_TOOLS`` (``auto`` | ``on`` | ``off``).
    Under ``auto`` (the default), fails closed unless the server advertises one
    of :data:`NATIVE_TOOL_MARKERS` (e.g. ``tool_calling`` in
    ``backends[].capabilities`` from Sonder-Inference PR #96, or in top-level /
    ``sonder`` ``features``/``capabilities`` or ``models[].capabilities``).
    """
    mode = _native_tools_flag(env)
    if mode != "auto":
        return mode == "on"
    return _document_advertises_markers(document, NATIVE_TOOL_MARKERS, model=model)


def structured_output_supported(
    document: Mapping[str, object] | None,
    *,
    model: str | None = None,
) -> bool:
    """Whether a ``sonder-inference`` health document advertises ``response_format`` support."""
    return _document_advertises_markers(document, STRUCTURED_OUTPUT_MARKERS, model=model)


def logprobs_supported(
    document: Mapping[str, object] | None,
    *,
    model: str | None = None,
) -> bool:
    """Whether a ``sonder-inference`` health document advertises token ``logprobs``."""
    return _document_advertises_markers(document, LOGPROBS_MARKERS, model=model)


def embeddings_supported(
    document: Mapping[str, object] | None,
    *,
    model: str | None = None,
) -> bool:
    """Whether a ``sonder-inference`` health document advertises ``/v1/embeddings``."""
    return _document_advertises_markers(document, EMBEDDING_MARKERS, model=model)


def rerank_supported(
    document: Mapping[str, object] | None,
    *,
    model: str | None = None,
) -> bool:
    """Whether a ``sonder-inference`` health document advertises ``/v1/rerank``."""
    return _document_advertises_markers(document, RERANK_MARKERS, model=model)


def _normalize_tools(raw: object) -> list[dict[str, object]] | None:
    """Validate and clone OpenAI ``tools`` definitions before any network I/O."""
    if raw in (None, False, "", [], {}):
        return None
    if not isinstance(raw, (list, tuple)):
        raise InvalidInput("option 'tools' must be a list of OpenAI function tool definitions")
    if len(raw) > MAX_TOOLS_PER_REQUEST:
        raise InvalidInput(
            "option 'tools' exceeds maximum of %d definitions" % MAX_TOOLS_PER_REQUEST
        )
    normalized: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for idx, entry in enumerate(raw):
        where = "option 'tools[%d]'" % idx
        if not isinstance(entry, Mapping):
            raise InvalidInput("%s must be an object" % where)
        if entry.get("type") != "function":
            raise InvalidInput("%s.type must be 'function'" % where)
        fn = entry.get("function")
        if not isinstance(fn, Mapping):
            raise InvalidInput("%s.function must be an object" % where)
        name = fn.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise InvalidInput(
                "%s.function.name must be 1-128 characters of [A-Za-z0-9_.-]" % where
            )
        if name in seen_names:
            raise InvalidInput("%s duplicates tool name %r" % (where, name))
        seen_names.add(name)
        clean_fn: dict[str, object] = {"name": name}
        desc = fn.get("description")
        if desc is not None:
            if not isinstance(desc, str):
                raise InvalidInput("%s.function.description must be a string" % where)
            clean_fn["description"] = desc
        params = fn.get("parameters")
        if params is not None:
            if not isinstance(params, Mapping):
                raise InvalidInput("%s.function.parameters must be a JSON Schema object" % where)
            try:
                clean_fn["parameters"] = json.loads(json.dumps(dict(params), allow_nan=False))
            except (TypeError, ValueError, RecursionError) as exc:
                raise InvalidInput("%s.function.parameters must be JSON-serializable" % where) from exc
        strict = fn.get("strict")
        if strict is not None:
            if not isinstance(strict, bool):
                raise InvalidInput("%s.function.strict must be a boolean" % where)
            clean_fn["strict"] = strict
        normalized.append({"type": "function", "function": clean_fn})
    return normalized


def _normalize_tool_choice(
    raw: object, tools: list[dict[str, object]] | None,
) -> str | dict[str, object] | None:
    """Validate and clone OpenAI ``tool_choice`` against ``tools``."""
    if raw in (None, False, "", {}):
        return None
    tool_names = {
        str(fn["name"])
        for item in (tools or ())
        if isinstance(fn := item.get("function"), Mapping) and isinstance(fn.get("name"), str)
    }
    if isinstance(raw, str):
        if raw not in ("auto", "none", "required"):
            raise InvalidInput(
                "option 'tool_choice' must be 'auto', 'none', 'required', or a function object"
            )
        if raw == "required" and not tool_names:
            raise InvalidInput("option 'tool_choice' value 'required' requires at least one tool")
        return raw
    if isinstance(raw, Mapping):
        if raw.get("type") != "function":
            raise InvalidInput("option 'tool_choice'.type must be 'function'")
        fn = raw.get("function")
        if not isinstance(fn, Mapping):
            raise InvalidInput("option 'tool_choice'.function must be an object")
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidInput("option 'tool_choice'.function.name must be a non-empty string")
        if name not in tool_names:
            raise InvalidInput(
                "option 'tool_choice' function %r is not defined in tools" % name
            )
        return {"type": "function", "function": {"name": name}}
    raise InvalidInput(
        "option 'tool_choice' must be 'auto', 'none', 'required', or a function object"
    )


def _normalize_message_tool_calls(raw: object, *, msg_index: int) -> list[dict[str, object]]:
    """Normalize assistant history ``tool_calls`` to strict OpenAI wire shape."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise InvalidInput("history message %d tool_calls must be a non-empty list" % msg_index)
    normalized: list[dict[str, object]] = []
    for tc_idx, item in enumerate(raw):
        where = "history message %d tool_calls[%d]" % (msg_index, tc_idx)
        if not isinstance(item, Mapping):
            raise InvalidInput("%s must be an object" % where)
        tc_type = item.get("type", "function")
        if tc_type is not None and tc_type != "function":
            raise InvalidInput("%s.type must be 'function'" % where)
        raw_id = item.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            raise InvalidInput("%s.id must be a string" % where)
        tc_id = (
            raw_id.strip()
            if isinstance(raw_id, str) and raw_id.strip()
            else "call_hist_%d_%d" % (msg_index, tc_idx)
        )
        fn = item.get("function")
        if not isinstance(fn, Mapping):
            raise InvalidInput("%s.function must be an object" % where)
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidInput("%s.function.name must be a non-empty string" % where)
        raw_args = fn.get("arguments")
        if raw_args is None:
            args_str = "{}"
        elif isinstance(raw_args, str):
            args_str = raw_args
        elif isinstance(raw_args, Mapping):
            try:
                args_str = json.dumps(
                    dict(raw_args), ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise InvalidInput("%s.function.arguments must be JSON-serializable" % where) from exc
        else:
            raise InvalidInput("%s.function.arguments must be a JSON string or object" % where)
        normalized.append({
            "id": tc_id,
            "type": "function",
            "function": {"name": name.strip(), "arguments": args_str},
        })
    return normalized


def _extract_response_tool_calls(
    data: dict,
    *,
    tools: list[dict[str, object]] | None = None,
    tool_choice: str | dict[str, object] | None = None,
) -> tuple[dict[str, object], ...] | None:
    """Validate and extract OpenAI ``choices[0].message.tool_calls``.

    Fails closed with :class:`DependencyUnavailable` if the server returns
    unsolicited tool calls (``tools`` absent or ``tool_choice == "none"``),
    exceeds :data:`MAX_RESPONSE_TOOL_CALLS`, names an undeclared tool,
    violates a forced function ``tool_choice``, or emits malformed/NaN
    arguments.
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict) or "tool_calls" not in message or message["tool_calls"] is None:
        return None
    raw = message["tool_calls"]
    if not isinstance(raw, list):
        raise DependencyUnavailable("sonder-inference returned invalid tool_calls")
    if not raw:
        return None
    if not tools or tool_choice == "none":
        raise DependencyUnavailable(
            "sonder-inference returned unsolicited tool_calls when tools were not permitted"
        )
    if len(raw) > MAX_RESPONSE_TOOL_CALLS:
        raise DependencyUnavailable(
            "sonder-inference returned more than %d tool_calls" % MAX_RESPONSE_TOOL_CALLS
        )
    allowed_names = {
        str(fn["name"])
        for item in tools
        if isinstance(fn := item.get("function"), Mapping) and isinstance(fn.get("name"), str)
    }
    forced_name: str | None = None
    if isinstance(tool_choice, Mapping):
        tc_fn = tool_choice.get("function")
        if isinstance(tc_fn, Mapping) and isinstance(tc_fn.get("name"), str):
            forced_name = tc_fn["name"]
    calls: list[dict[str, object]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DependencyUnavailable("sonder-inference returned an invalid tool call entry")
        tc_type = item.get("type", "function")
        if tc_type is not None and tc_type != "function":
            raise DependencyUnavailable("sonder-inference returned an unsupported tool call type")
        raw_id = item.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            raise DependencyUnavailable("sonder-inference returned an invalid tool call id")
        tc_id = (
            raw_id.strip()
            if isinstance(raw_id, str) and raw_id.strip()
            else "call_%d" % idx
        )
        fn = item.get("function")
        if not isinstance(fn, dict):
            raise DependencyUnavailable("sonder-inference returned a tool call without a function")
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            raise DependencyUnavailable("sonder-inference returned a tool call without a name")
        clean_name = name.strip()
        if clean_name not in allowed_names:
            raise DependencyUnavailable(
                "sonder-inference returned tool call for undeclared tool %r" % clean_name
            )
        if forced_name is not None and clean_name != forced_name:
            raise DependencyUnavailable(
                "sonder-inference returned tool call %r instead of forced tool %r"
                % (clean_name, forced_name)
            )
        raw_args = fn.get("arguments")
        if raw_args is None:
            args_str = "{}"
        elif isinstance(raw_args, str):
            args_str = raw_args
        elif isinstance(raw_args, Mapping):
            try:
                args_str = json.dumps(
                    dict(raw_args), ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise DependencyUnavailable(
                    "sonder-inference returned non-serializable tool call arguments"
                ) from exc
        else:
            raise DependencyUnavailable("sonder-inference returned invalid tool call arguments")
        calls.append({
            "id": tc_id,
            "type": "function",
            "function": {"name": clean_name, "arguments": args_str},
        })
    return tuple(calls)


def _normalize_response_format(raw: object) -> dict[str, object] | None:
    """Validate and normalize OpenAI ``response_format`` before any network I/O."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise InvalidInput("option 'response_format' must be an object")
    fmt_type = raw.get("type")
    if fmt_type not in ("text", "json_object", "json_schema"):
        raise InvalidInput(
            "option 'response_format.type' must be 'text', 'json_object', or 'json_schema'"
        )
    if fmt_type in ("text", "json_object"):
        return {"type": str(fmt_type)}
    js = raw.get("json_schema")
    if not isinstance(js, Mapping):
        raise InvalidInput("option 'response_format.json_schema' must be an object")
    clean_js: dict[str, object] = {}
    if "name" in js and js["name"] is not None:
        if not isinstance(js["name"], str) or not js["name"].strip():
            raise InvalidInput("option 'response_format.json_schema.name' must be a non-empty string")
        clean_js["name"] = js["name"].strip()
    if "strict" in js and js["strict"] is not None:
        if type(js["strict"]) is not bool:
            raise InvalidInput("option 'response_format.json_schema.strict' must be a boolean")
        clean_js["strict"] = js["strict"]
    schema = js.get("schema")
    if not isinstance(schema, Mapping):
        raise InvalidInput("option 'response_format.json_schema.schema' must be a JSON Schema object")
    try:
        clean_js["schema"] = json.loads(json.dumps(dict(schema), allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidInput("option 'response_format.json_schema.schema' must be valid JSON") from exc
    return {"type": "json_schema", "json_schema": clean_js}


def _normalize_logprobs_options(
    raw_logprobs: object,
    raw_top_logprobs: object,
) -> tuple[bool | None, int | None]:
    """Validate OpenAI ``logprobs`` and ``top_logprobs`` options before network I/O."""
    if raw_logprobs is None:
        logprobs: bool | None = None
    elif type(raw_logprobs) is not bool:
        raise InvalidInput("option 'logprobs' must be a boolean")
    else:
        logprobs = raw_logprobs

    if raw_top_logprobs is None:
        top_logprobs: int | None = None
    else:
        if type(raw_top_logprobs) is not int or not (0 <= raw_top_logprobs <= MAX_TOP_LOGPROBS):
            raise InvalidInput(
                "option 'top_logprobs' must be an integer in [0, %d]" % MAX_TOP_LOGPROBS
            )
        if logprobs is not True:
            raise InvalidInput("option 'top_logprobs' requires option 'logprobs=True'")
        top_logprobs = raw_top_logprobs
    return logprobs, top_logprobs


def _valid_logprob_entry(entry: object) -> bool:
    if not isinstance(entry, Mapping):
        return False
    token = entry.get("token")
    logprob = entry.get("logprob")
    if not isinstance(token, str):
        return False
    if (
        isinstance(logprob, bool)
        or not isinstance(logprob, (int, float))
        or not math.isfinite(float(logprob))
    ):
        return False
    top_entries = entry.get("top_logprobs")
    if top_entries is not None:
        if not isinstance(top_entries, list):
            return False
        for candidate in top_entries:
            if not isinstance(candidate, Mapping) or not isinstance(candidate.get("token"), str):
                return False
            cand_lp = candidate.get("logprob")
            if (
                isinstance(cand_lp, bool)
                or not isinstance(cand_lp, (int, float))
                or not math.isfinite(float(cand_lp))
            ):
                return False
    return True


def _extract_response_logprobs(data: Mapping[str, object]) -> dict[str, object] | None:
    """Validate and extract OpenAI ``choices[0].logprobs`` when present."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    raw_lp = choices[0].get("logprobs")
    if raw_lp is None:
        return None
    if not isinstance(raw_lp, Mapping):
        raise DependencyUnavailable("sonder-inference returned an invalid logprobs object")
    content = raw_lp.get("content")
    if content is not None:
        if not isinstance(content, list) or not all(_valid_logprob_entry(item) for item in content):
            raise DependencyUnavailable("sonder-inference returned invalid logprobs entries")
    return dict(raw_lp)


# -- transport ---------------------------------------------------------------
#
# One exchange = one request and its bounded response.  The stdlib urllib
# stack is kept (so HTTP errors keep the shape the shared gateway maps), but
# built per exchange without ProxyHandler input from the environment, without
# redirect following, and with a watchdog that shuts the socket down when the
# exchange's wall-clock budget is spent.


def _exchange(
    method: str, url: str, headers: Mapping[str, str], data: bytes | None,
    timeout: float, limit: int,
) -> tuple[int, bytes, Mapping[str, str] | None]:
    """Run one direct, non-redirected, wall-clock-bounded HTTP exchange.

    Returns ``(status, body, None)`` for a 2xx response and
    ``(status, error body, headers)`` otherwise.  Raises ``Cancelled`` when the
    enclosing cancellation scope's token is cancelled (before the send, or
    mid-exchange: the watcher closes the socket), ``TimeoutError`` when the
    budget is spent, ``DependencyUnavailable`` for a malformed or oversized
    HTTP response, and the usual ``URLError``/``OSError`` for transport
    failures before a response (refused, unresolvable, ...).
    """
    budget_seconds = float(timeout) if timeout else DEFAULT_TIMEOUT_SECONDS
    if _cancelled_quietly(_ACTIVE_CANCELLATION.get()):
        raise Cancelled("sonder-inference exchange cancelled before send")
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    with _ExchangeBudget(budget_seconds) as budget:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            _BudgetedHTTPHandler(budget), _BudgetedHTTPSHandler(budget),
        )
        # http.client's InvalidURL quotes the URL text it rejected, so an
        # HTTPException is never chained: the class name is kept in this
        # variable and the error is raised after the except block.
        malformed: str | None = None
        expired_cause: Exception | None = None
        aborted_cause: Exception | None = None
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
            if budget.aborted:
                aborted_cause = exc
            elif isinstance(exc, http.client.HTTPException):
                malformed = type(exc).__name__
            elif budget.expired:
                expired_cause = exc
            else:
                raise
        if budget.aborted:
            # A response that raced the abort is discarded: the caller cancelled.
            # An HTTPException is not chained, for the reason given above.
            if isinstance(aborted_cause, http.client.HTTPException):
                aborted_cause = None
            raise Cancelled("sonder-inference exchange cancelled") from aborted_cause
        if malformed is not None:
            if budget.expired:
                raise TimeoutError(
                    "sonder-inference exchange exceeded its %.1fs budget" % budget_seconds
                )
            raise DependencyUnavailable(
                "sonder-inference sent a malformed HTTP response (%s)" % malformed
            )
        if expired_cause is not None:
            raise TimeoutError(
                "sonder-inference exchange exceeded its %.1fs budget" % budget_seconds
            ) from expired_cause
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
    stream_idle_seconds: float = DEFAULT_STREAM_IDLE_SECONDS
    stream_first_content_seconds: float = DEFAULT_STREAM_FIRST_CONTENT_SECONDS
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
    embed_model: str = ""
    rerank_model: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        object.__setattr__(self, "workers", tuple(self.workers or ()))
        if type(self.max_inflight) is not int or not 1 <= self.max_inflight <= MAX_WORKER_INFLIGHT:
            raise InvalidInput("sonder-inference max_inflight must be an integer in [1, %d]"
                               % MAX_WORKER_INFLIGHT)
        if not isinstance(self.model, str) or not _MODEL_ID.fullmatch(self.model):
            raise InvalidInput("sonder-inference model id must be 1-128 non-space characters")
        if self.embed_model:
            if not isinstance(self.embed_model, str) or not _MODEL_ID.fullmatch(self.embed_model.strip()):
                raise InvalidInput("sonder-inference embed_model id must be 1-128 non-space characters")
            object.__setattr__(self, "embed_model", self.embed_model.strip())
        if self.rerank_model:
            if not isinstance(self.rerank_model, str) or not _MODEL_ID.fullmatch(self.rerank_model.strip()):
                raise InvalidInput("sonder-inference rerank_model id must be 1-128 non-space characters")
            object.__setattr__(self, "rerank_model", self.rerank_model.strip())
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
        for value, name in ((self.stream_idle_seconds, ENV_STREAM_IDLE),
                            (self.stream_first_content_seconds, ENV_STREAM_FIRST_CONTENT)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                0.0 <= float(value) <= MAX_STREAM_STALL_SECONDS
            ):
                raise InvalidInput("%s must be in [0, 86400] (0 disables it)" % name)
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
    parts = urlsplit_or_none(raw)
    if parts is None:
        raise InvalidInput(
            "sonder-inference base URL %r is not a valid URL" % redact_url(raw)
        )
    if "%" in parts.netloc:
        # urllib unquotes the host, so an encoded ":" or "@" becomes a port or
        # userinfo that http.client later quotes in its own error; an IPv6 zone
        # id is refused with them. A "%" in the path is fine.
        raise InvalidInput("sonder-inference base URL host must not contain percent-encoding")
    host = parts.hostname
    port = parts.port
    if (parts.scheme not in ("http", "https") or not host
            or parts.username or parts.password or parts.query or parts.fragment):
        raise InvalidInput(
            "sonder-inference base URL must be http(s)://host[:port][/path] "
            "without credentials, query or fragment"
        )
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in parts.netloc):
        raise InvalidInput(
            "sonder-inference base URL host must not contain whitespace or control characters"
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
        embed_model=str(source.get(ENV_EMBED_MODEL, "") or "").strip(),
        rerank_model=str(source.get(ENV_RERANK_MODEL, "") or "").strip(),
        tier_models=_parse_tier_models(str(source.get(ENV_TIER_MODELS, "") or "")),
        api_key=str(source.get(ENV_API_KEY, "") or "").strip(),
        allow_remote=allow_remote_raw == "1",
        timeout_seconds=_parse_float(source, ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS),
        stream_idle_seconds=_parse_float(source, ENV_STREAM_IDLE, DEFAULT_STREAM_IDLE_SECONDS),
        stream_first_content_seconds=_parse_float(
            source, ENV_STREAM_FIRST_CONTENT, DEFAULT_STREAM_FIRST_CONTENT_SECONDS),
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
    if not context.cloud_allowed:
        headers["X-Sonder-Local-Only"] = "1"
    return headers


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (moment.microsecond // 1000)
    )


def _proxy_upstream_unreachable(headers: Any) -> bool:
    """True only for ``X-Sonder-Proxy-Upstream: unreachable`` (Caddy dial failure)."""
    if headers is None:
        return False
    try:
        value = headers.get("X-Sonder-Proxy-Upstream")
    except Exception:
        return False
    return isinstance(value, str) and value.strip().lower() == "unreachable"


def _error_fields(body: bytes) -> tuple[str, str]:
    """Return ``(code, message)`` from an Inference error document."""
    try:
        document = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "", ""
    return _error_document_fields(document, bound_message=False)


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


@dataclass(frozen=True)
class _RoutingHints:
    """Compact, prompt-free routing signals for cache-aware endpoint placement."""

    model: str
    prefix_key: str
    adapter_key: str
    estimated_prompt_tokens: int
    expected_decode_tokens: int


class SonderInferenceGateway(OpenAICompatibleGateway):
    """ModelGateway over ``sonder-infer serve`` (Inference HTTP API v1)."""

    def __init__(
        self, config: SonderInferenceConfig | None = None, *,
        transport=None, get_transport=None,
        request_admission: HostModelRequestAdmission | None = None,
        env: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        ollama_pool=None,
        profile_cache: launch_profile_cache.ProfileCache | None = None,
        residency_probe: ResidencyProbe | None = None,
        is_model_resident: ResidencyProbe | None = None,
        locality_max_entries: int = DEFAULT_MAX_LOCALITY_ENTRIES,
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
            residency_probe=residency_probe,
            is_model_resident=is_model_resident,
            spend_ledger=None,  # the owner's own LAN host: not metered
        )
        self._settings_override = config
        self._env = env
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._ollama_pool = ollama_pool
        self._lock = threading.Lock()
        # Signalled whenever an inflight slot is released (shares ``_lock``).
        self._cond = threading.Condition(self._lock)
        self._refresh_lock = threading.Lock()
        self._health_cache: HealthSnapshot | None = None
        self._health_probe_monotonic = float("-inf")
        self._identity_cache: dict[tuple[str, str], tuple[float, IdentityObservation]] = {}
        # ``/v1/models`` launch-profile metadata, shared process-wide by
        # default so every gateway instance (status, doctor, the chat loop's
        # context-fit check) costs one GET per endpoint per TTL.
        self._profile_cache = (
            profile_cache if profile_cache is not None else launch_profile_cache.shared_cache()
        )
        # Settings of the call in flight on this thread, so the transport's
        # error classifiers can name the endpoint and update its health.
        self._call = threading.local()
        # Private-worker placement (primary only): one child gateway per
        # approved worker, rebuilt when its resolved settings change, plus the
        # in-flight count per endpoint and a rotation cursor for ties.
        self._transport_seams = (transport, get_transport)
        self._workers: dict[str, tuple[tuple, SonderInferenceGateway]] = {}
        self._inflight: dict[str, int] = {}
        # Calls in flight on this endpoint when it serves alone (no placement).
        self._local_inflight = 0
        self._cursor = 0
        # Observed milliseconds per output token per endpoint (EWMA of
        # successful calls), so a much slower endpoint only gets overflow.
        self._ms_per_token: dict[str, float] = {}
        # Observed prompt prefill ms/token, overall request latency EWMA, and
        # bounded prefix-locality cache keyed by (endpoint, model, adapter, prefix).
        self._prompt_ms_per_token: dict[str, float] = {}
        self._request_ewma_ms: dict[str, float] = {}
        self._locality_table = PrefixLocalityTable(max_entries=locality_max_entries)
        self._last_routing_decision: dict[str, object] | None = None

    def _probe_model_residency(
        self, model: str, cfg: OpenAICompatibleConfig,
    ) -> bool | None:
        if self._residency_probe is not None:
            return super()._probe_model_residency(model, cfg)
        with self._lock:
            cached = self._health_cache
        if cached is None or cached.base_url != cfg.base_url or cached.document is None:
            return None
        models_raw = cached.document.get("models")
        if not isinstance(models_raw, list):
            return None
        matched: Mapping[str, object] | None = None
        for entry in models_raw:
            if not isinstance(entry, Mapping):
                continue
            if model == DEFAULT_MODEL:
                if entry.get("default") is True:
                    matched = entry
                    break
            elif entry.get("id") == model:
                matched = entry
                break
        if matched is None:
            return None
        resident = matched.get("resident")
        if type(resident) is bool:
            return resident
        state = matched.get("state")
        if isinstance(state, str):
            norm = state.strip().lower()
            if norm == "resident":
                return True
            if norm in ("unloaded", "unloading", "loading"):
                return False
        return None

    @property
    def last_response_meta(self) -> dict[str, object]:
        """Measurements for this thread's last call; failures clear old data."""
        return dict(getattr(self._call, "response_meta", {}))

    # -- configuration & consent ------------------------------------------

    def _advertised_capabilities(
        self,
        *,
        snap: HealthSnapshot | None = None,
    ) -> frozenset[str]:
        if snap is None:
            with self._lock:
                snap = self._health_cache
        if snap is not None and snap.state == "ready" and embeddings_supported(snap.document):
            return CAPABILITIES | frozenset({GATEWAY_CAPABILITY_EMBEDDINGS})
        return CAPABILITIES

    @property
    def capabilities(self) -> frozenset[str]:
        return self._advertised_capabilities()

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
        if settings.private_worker and context.private_node_allowed is False:
            raise Forbidden(
                "sonder-inference private worker %s skipped: consent (this "
                "operation context does not allow private nodes)"
                % settings.display_base_url
            )
        # An approved private worker is its own consent lane: it neither needs
        # nor grants cloud consent.  Every other non-loopback endpoint does.
        if not settings.loopback and not settings.private_worker and not context.cloud_allowed:
            raise Forbidden(
                "sonder-inference endpoint %s is not loopback and this "
                "operation context does not allow prompts to leave the machine"
                % settings.display_base_url
            )

    def _is_remote_cloud_model(self, document: Mapping[str, Any] | None, model: str) -> bool:
        """Return True if SI health/model metadata marks ``model`` or backend as remote/cloud."""
        from ...domain.model_routing import is_cloud_model_name

        if is_cloud_model_name(model):
            return True
        if not isinstance(document, Mapping):
            return False
        backend = str(document.get("backend") or "").strip().lower()
        if backend == "ollama_cloud":
            return True
        for row in document.get("models") or ():
            if not isinstance(row, Mapping):
                continue
            row_id = str(row.get("id") or row.get("model") or "").strip()
            if row_id and row_id != model:
                continue
            if row.get("remote") is True:
                return True
            row_backend = str(row.get("backend") or row.get("provider") or "").strip().lower()
            if row_backend == "ollama_cloud":
                return True
            sonder_meta = row.get("sonder")
            if isinstance(sonder_meta, Mapping) and (
                sonder_meta.get("remote") is True
                or str(sonder_meta.get("provider") or "").strip().lower() == "ollama_cloud"
            ):
                return True
        return False

    def _enforce_remote_model_consent(
        self, document: Mapping[str, Any] | None, model: str, context: OperationContext,
    ) -> None:
        """Fail closed before send when a loopback SI forwards to a remote cloud provider (RET-02)."""
        if not self._is_remote_cloud_model(document, model):
            return
        if not context.cloud_allowed:
            raise Forbidden(
                "model %r is served by a remote cloud provider on Sonder Inference and "
                "this operation context does not allow cloud prompts" % model
            )
        env = os.environ if self._env is None else self._env
        allow_cloud = str(env.get("SONDER_ALLOW_CLOUD", "") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not allow_cloud:
            raise Forbidden(
                "model %r is served by a remote cloud provider on Sonder Inference; "
                "set SONDER_ALLOW_CLOUD=1 to opt in" % model
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
    def unreachable_error(
        settings: SonderInferenceConfig, reason: str, *, timed_out: bool = False,
    ) -> SonderInferenceUnreachable:
        """Build the one operator message for "Inference did not get it"."""
        summary = "Sonder Inference at %s is not reachable or not ready (%s)" % (
            settings.display_base_url, _bounded(reason),
        )
        return SonderInferenceUnreachable(
            "%s; start it with `sonder-infer serve`" % summary,
            reason=_bounded(reason), summary=summary, timed_out=timed_out,
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

    def _classify_http_error(
        self, status: int, body: bytes, headers: Any = None,
    ) -> SonderError | None:
        code, message = _error_fields(body)
        if status == 503 and code == "upstream_unreachable" and _proxy_upstream_unreachable(headers):
            # The fronting proxy (Caddy handle_errors) failed to dial the
            # upstream: nothing reached Sonder Inference, so it is retryable.
            detail = "HTTP 503 upstream_unreachable"
            return SonderInferenceUnreachable(
                "the proxy in front of sonder-inference could not reach it (%s)" % detail,
                reason=detail,
            )
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
            return SonderInferenceOverloaded("sonder-inference is at its connection limit (%s)" % label)
        if busy_timeout:
            return SonderInferenceBusyTimeout(
                "sonder-inference busy_timeout (%s)%s" % (label, suffix)
            )
        if status in (401, 403):
            return self._forbidden(status, code, message)
        if status == 429 and code == "overloaded":
            # SI uses 429 overloaded for both the queue-full rejection (nothing
            # executed) and the engine-scheduler rejection (openai.cpp:1322,
            # session.cpp:2464), which can follow prefill.  The two cannot be
            # told apart on the wire.  Retrying both is accepted on purpose:
            # in either case no output was delivered to the caller, and
            # generation is not a side effect.  Any other 429 stays final.
            return SonderInferenceOverloaded(
                "sonder-inference rejected the request before executing it (%s)" % label)
        if status == 429:
            return CapacityExceeded("sonder-inference scheduler rejected the request (%s)%s" % (label, suffix))
        if status in (400, 404, 405, 411, 413, 501):
            return InvalidInput("sonder-inference rejected the request (%s)%s" % (label, suffix))
        # 3xx (never followed), 408, 500, 503 backend_unavailable and anything
        # unexpected: the request may have reached the backend, so it is
        # never "unreachable".
        return DependencyUnavailable("sonder-inference failed the request (%s)%s" % (label, suffix))

    _classify_http_error.accepts_headers = True

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
        except Cancelled:
            raise  # the caller went away; not evidence about the endpoint
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
            raise SonderInferenceOverloaded("sonder-inference at %s %s; retry shortly"
                                            % (settings.display_base_url, snap.detail))
        if snap.state != "ready":
            raise self.unreachable_error(settings, snap.detail, timed_out=snap.timed_out)
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

    def _supports_capability(
        self,
        matcher: Callable[..., bool],
        *,
        tier: str | None = None,
        model: str | None = None,
        refresh: bool = False,
    ) -> bool:
        try:
            settings = self.settings()
        except Exception:  # noqa: BLE001 - capability probe fails closed
            return False
        resolved_model: str | None = model
        if resolved_model is None and tier:
            resolved_model = settings.tier_models.get(tier, settings.model)
        try:
            check_endpoint_policy(settings)
            snap = self.health(settings=settings, refresh=refresh)
            if (snap.state == "ready"
                    and (resolved_model is None or self._serves(snap, resolved_model))
                    and matcher(snap.document, model=resolved_model)):
                return True
        except Exception:  # noqa: BLE001 - primary unavailable; private workers may still serve
            pass
        if not settings.private_worker and settings.workers:
            for spec in settings.workers:
                try:
                    worker_gw = self._worker_gateway(settings, spec)
                    worker_settings = worker_gw.settings()
                    worker_snap = worker_gw.health(settings=worker_settings, refresh=refresh)
                    if worker_snap.document is None or (
                        worker_snap.state != "ready" and not worker_snap.busy
                    ):
                        continue
                    if resolved_model is not None:
                        if resolved_model == DEFAULT_MODEL:
                            continue
                        if not self._worker_serves(worker_gw, worker_settings, worker_snap, resolved_model):
                            continue
                    if matcher(worker_snap.document, model=resolved_model):
                        return True
                except Exception:  # noqa: BLE001 - unusable worker fails closed
                    continue
        return False

    def supports_native_tools(
        self,
        *,
        tier: str | None = None,
        model: str | None = None,
        refresh: bool = False,
    ) -> bool:
        """Return True only when a ready endpoint advertises native tool calling.

        Uses the bounded, cached health probe (no generation, never raises) and
        fails closed whenever the endpoint is unreachable, misconfigured, or
        does not advertise tool capability.
        """
        mode = _native_tools_flag(self._env)
        if mode != "auto":
            return mode == "on"
        return self._supports_capability(
            lambda doc, *, model=None: native_tools_supported(doc, model=model, env=self._env),
            tier=tier,
            model=model,
            refresh=refresh,
        )

    def supports_structured_output(
        self,
        *,
        tier: str | None = None,
        model: str | None = None,
        refresh: bool = False,
    ) -> bool:
        """Return True only when a ready endpoint advertises ``response_format`` support."""
        return self._supports_capability(
            structured_output_supported, tier=tier, model=model, refresh=refresh,
        )

    def supports_logprobs(
        self,
        *,
        tier: str | None = None,
        model: str | None = None,
        refresh: bool = False,
    ) -> bool:
        """Return True only when a ready endpoint advertises token ``logprobs``."""
        return self._supports_capability(
            logprobs_supported, tier=tier, model=model, refresh=refresh,
        )

    def supports_embeddings(
        self,
        *,
        tier: str | None = None,
        model: str | None = None,
        refresh: bool = False,
    ) -> bool:
        """Return True only when a ready endpoint advertises ``/v1/embeddings``."""
        return self._supports_capability(
            embeddings_supported, tier=tier, model=model, refresh=refresh,
        )

    def supports_rerank(
        self,
        *,
        tier: str | None = None,
        model: str | None = None,
        refresh: bool = False,
    ) -> bool:
        """Return True only when a ready endpoint advertises ``/v1/rerank``."""
        return self._supports_capability(
            rerank_supported, tier=tier, model=model, refresh=refresh,
        )

    def prefix_locality_status(self) -> dict[str, object]:
        """Bounded cache-locality counters and last routing decision (content-free)."""
        with self._lock:
            last = dict(self._last_routing_decision) if self._last_routing_decision else None
        return {
            "summary": self._locality_table.summary(),
            "last_decision": last,
        }

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

    # -- launch-profile metadata (/v1/models) --------------------------------

    def _fetch_profile_catalog(self, settings: SonderInferenceConfig) -> LaunchProfileCatalog:
        """GET ``/v1/models`` on the bounded, direct transport.  Never raises."""
        base = settings.base_url
        self._call.settings = settings
        cfg = OpenAICompatibleConfig(base_url=base, api_key=settings.api_key)
        try:
            with trust_scope(settings.ca_bundle):
                status, document = self.get_json(
                    "/v1/models", timeout=MODELS_PROBE_TIMEOUT_SECONDS, cfg=cfg,
                )
        except SonderInferenceUnreachable as exc:
            return LaunchProfileCatalog(
                base, CATALOG_ERROR, _bounded("models probe failed: %s" % exc.reason),
            )
        except SonderError as exc:
            return LaunchProfileCatalog(base, CATALOG_ERROR, _bounded("models probe failed: %s" % exc))
        except Exception as exc:  # noqa: BLE001 - metadata is optional, never fatal
            return LaunchProfileCatalog(
                base, CATALOG_ERROR, "models probe failed: %s" % type(exc).__name__,
            )
        if status in (404, 405, 501):
            return LaunchProfileCatalog(base, CATALOG_ABSENT, "no /v1/models (HTTP %d)" % status)
        if status != 200:
            return LaunchProfileCatalog(base, CATALOG_ERROR, "/v1/models returned HTTP %d" % status)
        models = parse_launch_profile_models(document)
        if models is None:
            return LaunchProfileCatalog(base, CATALOG_ABSENT, "/v1/models has no model list")
        return LaunchProfileCatalog(
            base, CATALOG_OK, "%d model(s)" % len(models), MappingProxyType(models),
        )

    def model_profiles(self, *, refresh: bool = False) -> LaunchProfileCatalog:
        """``/v1/models`` launch-profile metadata, TTL-cached per endpoint; never raises.

        An Inference without ``/v1/models`` or without profile fields gives a
        catalog whose entries report ``None`` (unknown), which never changes a
        route.  A remote endpoint is contacted only under the same consent
        rules as every other request.  Only provider status, doctor and the
        chat loop's context-fit check call this; ``generate`` never does.
        """
        try:
            settings = self.settings()
            check_endpoint_policy(settings)
        except Exception as exc:  # noqa: BLE001 - metadata is optional
            return LaunchProfileCatalog("", CATALOG_ERROR, _bounded(
                exc.summary if isinstance(exc, SonderInferenceUnreachable)
                else exc if isinstance(exc, SonderError) else type(exc).__name__
            ))
        return self._profile_cache.get(
            settings.base_url,
            lambda: self._fetch_profile_catalog(settings),
            ttl=launch_profile_cache.profile_ttl_seconds(self._env),
            refresh=refresh,
        )

    def tier_context(self, catalog: LaunchProfileCatalog | None = None) -> dict[str, object]:
        """Per-tier context length and capabilities (the additive status field).

        ``catalog`` defaults to :meth:`model_profiles`.  A tier whose model has
        no reported ``context_length`` says ``None``: unknown, not unlimited.
        """
        settings = self.settings()
        catalog = catalog if catalog is not None else self.model_profiles()
        tiers: dict[str, object] = {}
        for tier, model in self._tier_models(settings).items():
            found = catalog.lookup(model)
            tiers[tier] = {
                "model": model,
                "profile": found.profile if found is not None else None,
                "context_length": found.context_length if found is not None else None,
                "capabilities": (
                    list(found.capabilities)
                    if found is not None and found.capabilities is not None else None
                ),
                "source": (
                    "inference_profile"
                    if found is not None and found.context_length is not None else "unknown"
                ),
            }
        return {"state": catalog.state, "detail": catalog.detail, "tiers": tiers}

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
            capabilities=sorted(self._advertised_capabilities(snap=snap)),
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
            try:
                entry["tier_context"] = self.tier_context()
            except Exception as exc:  # noqa: BLE001 - profile metadata is optional display data
                logger.info("sonder-inference profile metadata unavailable: %s", _bounded(
                    exc if isinstance(exc, SonderError) else type(exc).__name__,
                ))
        pool = self._ollama_pool
        if pool is not None:
            # Reuse the worker rows read above: one health call per worker.
            rows = entry.get("workers")
            try:
                unified = self.unified_worker_status(
                    ollama_pool=pool,
                    gateway_rows=rows if isinstance(rows, list) else [],
                )
            except Exception as exc:  # noqa: BLE001 - status reports, never raises
                logger.info("ollama pool status unavailable: %s", _bounded(
                    exc if isinstance(exc, SonderError) else type(exc).__name__,
                ))
            else:
                entry["ollama_pool"] = unified["pool"]
                entry["pool_workers"] = unified["workers"]
        return {PROVIDER_ID: entry}

    def attach_ollama_pool(self, ollama_pool) -> None:
        """Bind or replace the cached OllamaWorkerPool used for unified discovery."""
        with self._lock:
            self._ollama_pool = ollama_pool

    def register_ollama_worker(
        self,
        origin: str,
        *,
        allow_remote: bool | None = None,
        ollama_pool=None,
        refresh: bool = True,
    ):
        """Register/refresh a peer worker on the bound OllamaWorkerPool."""
        pool = ollama_pool if ollama_pool is not None else self._ollama_pool
        if pool is None:
            raise InvalidInput("no OllamaWorkerPool is attached to SonderInferenceGateway")
        return pool.register_worker(origin, allow_remote=allow_remote, refresh=refresh)

    def unified_worker_status(
        self,
        *,
        ollama_pool=None,
        gateway_rows: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Unify private gateway worker status with cached ``OLLAMA_POOL`` snapshots.

        Pool rows come from the pool's cached snapshots (no probe).  Gateway
        worker rows are read with one health call per worker unless the
        caller already holds them (``gateway_rows``).
        """
        pool = ollama_pool if ollama_pool is not None else self._ollama_pool
        if gateway_rows is None:
            settings = self.settings()
            gateway_rows = self._workers_status(settings) if settings.workers else []
        pool_summary = (
            pool.pool_capacity_summary()
            if pool is not None and callable(getattr(pool, "pool_capacity_summary", None))
            else None
        )
        unified_workers: list[dict[str, object]] = []
        available_models: set[str] = set()
        resident_models: set[str] = set()
        model_contexts: dict[str, int] = {}
        tool_capable_models: set[str] = set()
        healthy_count = 0

        if pool is not None and callable(getattr(pool, "snapshots", None)):
            for snap in pool.snapshots():
                snap_dict = snap.to_dict() if callable(getattr(snap, "to_dict", None)) else {}
                row = {
                    "source": "ollama_pool",
                    "worker_id": getattr(snap, "worker_id", snap_dict.get("worker_id")),
                    "base_url": getattr(snap, "origin", snap_dict.get("origin")),
                    "origin": getattr(snap, "origin", snap_dict.get("origin")),
                    "state": getattr(snap, "state", snap_dict.get("state")),
                    "healthy": bool(getattr(snap, "healthy", False)),
                    "inflight": getattr(snap, "inflight", 0),
                    "capacity": getattr(snap, "capacity", 0),
                    "latency_ewma_ms": getattr(snap, "latency_ewma_ms", None),
                    "models": list(getattr(snap, "models", ()) or ()),
                    "loaded_models": list(getattr(snap, "loaded_models", ()) or ()),
                    "model_contexts": dict(getattr(snap, "model_contexts", {}) or {}),
                    "vram_bytes": getattr(snap, "vram_bytes", None),
                    "ram_bytes": getattr(snap, "ram_bytes", None),
                    "kv_cache_type": getattr(snap, "kv_cache_type", None),
                    "supports_tools": dict(getattr(snap, "supports_tools", {}) or {}),
                    "model_active_billions": dict(getattr(snap, "model_active_billions", {}) or {}),
                }
                unified_workers.append(row)
                if row["healthy"]:
                    healthy_count += 1
                    available_models.update(row["models"])
                    resident_models.update(row["loaded_models"])
                    for m_name, ctx in row["model_contexts"].items():
                        prev = model_contexts.get(m_name)
                        if prev is None or ctx > prev:
                            model_contexts[m_name] = ctx
                    for m_name, supported in row["supports_tools"].items():
                        if supported:
                            tool_capable_models.add(m_name)

        for gw_row in gateway_rows:
            tagged = {"source": "sonder_inference_worker", **gw_row}
            unified_workers.append(tagged)
            if gw_row.get("healthy"):
                healthy_count += 1
                raw_models = gw_row.get("models")
                if isinstance(raw_models, list):
                    available_models.update(str(m) for m in raw_models if m)

        return {
            "worker_count": len(unified_workers),
            "healthy_worker_count": healthy_count,
            "workers": unified_workers,
            "pool": pool_summary,
            "gateway_workers": gateway_rows,
            "available_models": sorted(available_models),
            "resident_models": sorted(resident_models),
            "model_contexts": dict(sorted(model_contexts.items())),
            "tool_capable_models": sorted(tool_capable_models),
            "total_vram_bytes": pool_summary.get("total_vram_bytes") if pool_summary else None,
        }

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
                with trust_scope(settings.ca_bundle), stall_limits(
                    settings.stream_idle_seconds, settings.stream_first_content_seconds,
                    idle_setting=ENV_STREAM_IDLE, first_content_setting=ENV_STREAM_FIRST_CONTENT,
                ):
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
    # among those not known to be down that serve the model and have a free
    # slot (in-flight < max_inflight), ties rotated; when every such endpoint
    # is full the caller queues for a slot (cancel and deadline still apply).
    # A request moves to another endpoint only after SonderInferenceUnreachable
    # or SonderInferenceOverloaded, i.e. when it provably never executed, and
    # a saturated one (probe timeout, 503 overloaded) only to an endpoint with
    # a free slot; any other failure is final, exactly as on a single endpoint.
    # Without workers, the endpoint's own max_inflight queues callers the same
    # way (``_local_slot``).

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
            embed_model=settings.embed_model, rerank_model=settings.rerank_model,
            api_key=token, allow_remote=True, timeout_seconds=settings.timeout_seconds,
            stream_idle_seconds=settings.stream_idle_seconds,
            stream_first_content_seconds=settings.stream_first_content_seconds,
            health_ttl_seconds=settings.health_ttl_seconds, base_url_source="private_worker",
            health_timeout_seconds=settings.health_timeout_seconds,
            health_stale_seconds=settings.health_stale_seconds,
            private_worker=True, ca_bundle=spec.ca_bundle, max_inflight=spec.max_inflight,
        )

    def _worker_gateway(self, settings: SonderInferenceConfig,
                        spec: PrivateWorkerSpec) -> "SonderInferenceGateway":
        worker = self._worker_settings(settings, spec)
        key = (worker.base_url, worker.api_key, worker.ca_bundle, worker.max_inflight, worker.model,
               worker.embed_model, worker.rerank_model,
               tuple(sorted(worker.tier_models.items())), worker.timeout_seconds,
               worker.stream_idle_seconds, worker.stream_first_content_seconds,
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
            residency_probe=self._residency_probe,
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

    @staticmethod
    def _history_ends_with_tool(request: ModelRequest) -> bool:
        history = request.history or ()
        if not history:
            return False
        last = history[-1]
        return isinstance(last, Mapping) and last.get("role") == "tool"

    @staticmethod
    def _request_uses_native_tools(request: ModelRequest) -> bool:
        options = request.options or {}
        if options.get("tools") not in (None, False, "", [], {}):
            return True
        if options.get("tool_choice") not in (None, False, "", {}):
            return True
        for entry in request.history or ():
            if isinstance(entry, Mapping) and (
                entry.get("tool_calls") or entry.get("role") == "tool"
            ):
                return True
        return False

    @staticmethod
    def _build_messages(request: ModelRequest) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for idx, entry in enumerate(request.history or ()):
            if isinstance(entry, Mapping) and entry.get("role"):
                role = str(entry["role"])
                raw_tool_calls = entry.get("tool_calls") if role == "assistant" else None
                if raw_tool_calls:
                    raw_content = entry.get("content", "")
                    content = "" if raw_content is None else raw_content
                    if not isinstance(content, str):
                        raise InvalidInput("history message %d content must be text" % idx)
                    messages.append({
                        "role": "assistant",
                        "content": content,
                        "tool_calls": _normalize_message_tool_calls(raw_tool_calls, msg_index=idx),
                    })
                elif role == "tool":
                    if "content" not in entry or not isinstance(entry["content"], str):
                        raise InvalidInput("history tool message %d content must be text" % idx)
                    tool_msg: dict[str, object] = {
                        "role": "tool",
                        "content": entry["content"],
                    }
                    raw_tc_id = entry.get("tool_call_id")
                    if raw_tc_id is not None:
                        if not isinstance(raw_tc_id, str):
                            raise InvalidInput("history tool message %d tool_call_id must be a string" % idx)
                        if raw_tc_id.strip():
                            tool_msg["tool_call_id"] = raw_tc_id.strip()
                    raw_name = entry.get("name")
                    if raw_name is None:
                        raw_name = entry.get("tool_name")
                    if raw_name is not None:
                        if not isinstance(raw_name, str):
                            raise InvalidInput("history tool message %d name must be a string" % idx)
                        if raw_name.strip():
                            tool_msg["name"] = raw_name.strip()
                    messages.append(tool_msg)
                elif "content" in entry:
                    raw_imgs = entry.get("images")
                    if raw_imgs:
                        messages.append({
                            "role": role,
                            "content": _encode_image_content_parts(
                                str(entry["content"]), raw_imgs,
                            ),
                        })
                    else:
                        messages.append({"role": role, "content": entry["content"]})
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                messages.append({"role": entry[0], "content": entry[1]})
        prompt_images = (request.options or {}).get("images")
        if (request.prompt or "") or prompt_images or not (messages and messages[-1].get("role") == "tool"):
            if prompt_images:
                messages.append({
                    "role": "user",
                    "content": _encode_image_content_parts(request.prompt or "", prompt_images),
                })
            else:
                messages.append({"role": "user", "content": request.prompt})
        return messages

    def _operation_placement_candidates(
        self,
        settings: SonderInferenceConfig,
        context: OperationContext,
        *,
        model: str,
        predicate: Callable[[Mapping[str, object] | None], bool],
    ):
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
            if context.private_node_allowed is False:
                # Explicit per-operation refusal (None is legacy: allowed).
                logger.info(
                    "private sonder-inference worker %s skipped: consent",
                    spec.url,
                )
                continue
            try:
                gateway = self._worker_gateway(settings, spec)
            except SonderError as exc:
                logger.warning("private sonder-inference worker skipped: %s", _bounded(exc))
                continue
            candidates.append((spec.url, gateway, gateway.settings()))

        usable = []
        for key, gateway, endpoint in candidates:
            snapshot = gateway._peek_health(endpoint)
            if endpoint.private_worker:
                # A worker is chosen only on positive evidence that it serves
                # this exact model: a missing model would be a 404, which is
                # final and never moves to another endpoint.
                worker_snap = self._worker_snapshot(gateway, endpoint, snapshot, model)
                if worker_snap is None:
                    continue
                if not predicate(worker_snap.document):
                    continue
                usable.append((key, gateway, endpoint))
                continue
            if snapshot is None:
                # Learn the primary's model list too, so a model only a worker
                # serves is not sent here first (a 404 would be final).
                try:
                    snapshot = self.health(settings=endpoint)
                except Cancelled:
                    raise
                except Exception:  # noqa: BLE001 - the primary path reports it
                    snapshot = None
            if snapshot is not None and snapshot.state != "ready" and not snapshot.busy:
                continue
            if not self._serves(snapshot, model):
                continue
            if not predicate(snapshot.document if snapshot is not None else None):
                continue
            usable.append((key, gateway, endpoint))
        return usable

    def _placement_candidates(self, request: ModelRequest, settings: SonderInferenceConfig,
                              context: OperationContext):
        """``[(key, gateway, settings)]`` for the primary and each usable worker."""
        model = self.select_model(request, settings)
        needs_tools = self._request_uses_native_tools(request)
        opts = request.options or {}
        needs_structured_output = _normalize_response_format(opts.get("response_format")) is not None
        parsed_logprobs, _ = _normalize_logprobs_options(opts.get("logprobs"), opts.get("top_logprobs"))
        needs_logprobs = parsed_logprobs is True

        def _supports_all(doc: Mapping[str, object] | None) -> bool:
            if needs_tools and not native_tools_supported(doc, model=model, env=self._env):
                return False
            if needs_structured_output and not structured_output_supported(doc, model=model):
                return False
            if needs_logprobs and not logprobs_supported(doc, model=model):
                return False
            return True

        return self._operation_placement_candidates(
            settings, context, model=model, predicate=_supports_all,
        )

    @staticmethod
    def _worker_snapshot(
        gateway: "SonderInferenceGateway",
        endpoint: SonderInferenceConfig,
        snapshot: HealthSnapshot | None,
        model: str,
    ) -> HealthSnapshot | None:
        """The worker's ready health snapshot when it serves ``model``, else None."""
        if model == DEFAULT_MODEL:
            return None
        if snapshot is None:
            try:
                snapshot = gateway.health(settings=endpoint)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - an unusable worker is skipped
                logger.info("private sonder-inference worker %s skipped: %s",
                            endpoint.display_base_url,
                            _bounded(exc if isinstance(exc, SonderError) else type(exc).__name__))
                return None
        if snapshot.document is None or (snapshot.state != "ready" and not snapshot.busy):
            return None
        models = snapshot.document.get("models")
        if not isinstance(models, list):
            return None
        if model not in {item.get("id") for item in models if isinstance(item, dict)}:
            return None
        return snapshot

    @staticmethod
    def _worker_serves(gateway: "SonderInferenceGateway", endpoint: SonderInferenceConfig,
                       snapshot: HealthSnapshot | None, model: str) -> bool:
        """True only when the worker's health (probed if stale) lists ``model``.

        The ``default`` alias names whatever each server was started with, so
        it never selects a worker.  The probe is the cached, single-flight,
        bounded health check (at most one per worker per health TTL).
        """
        return SonderInferenceGateway._worker_snapshot(gateway, endpoint, snapshot, model) is not None

    @staticmethod
    def _with_request_cancel_token(
        request: ModelRequest, context: OperationContext,
    ) -> OperationContext:
        """Fold a per-request ``cancel_token``/``cancel_event`` option into the context.

        The option is any token :func:`_is_cancelled` reads (an Event, a
        ``cancelled`` property or method, or a callable); it is stripped from
        the wire payload in :meth:`_generate_here`.
        """
        options = request.options or {}
        token = options.get("cancel_token")
        if token is None:
            token = options.get("cancel_event")
        if token is None:
            return context
        current = context.cancellation
        if isinstance(current, _CompositeCancellation) and token in current.parts:
            return context  # already folded (generate -> _generate_here)
        return replace(context, cancellation=_CompositeCancellation(current, token))

    def _post(
        self, path: str, payload: dict, cfg: OpenAICompatibleConfig, timeout,
        *, context: OperationContext | None = None,
    ) -> dict:
        """The shared POST, with this call's token watched by every exchange it makes.

        A cancel that lands mid-call closes the socket; whatever the transport
        then reports (a reset, a mapped "unreachable") is reported as
        ``Cancelled``, so it is never mistaken for an endpoint failure and
        never fails over to another endpoint or provider.
        """
        token = context.cancellation if context is not None else _ACTIVE_CANCELLATION.get()
        with _cancellation_scope(token):
            try:
                return super()._post(path, payload, cfg, timeout, context=context)
            except Cancelled:
                raise
            except Exception:
                if _cancelled_quietly(token):
                    raise Cancelled("operation cancelled during model call") from None
                raise

    def _wait_for_slot(self, context: OperationContext | None) -> None:
        """One bounded wait on ``_cond`` (held by the caller); raises on cancel or deadline."""
        wait_for = 0.05
        if context is not None:
            remaining = self._check_liveness(context, phase="while waiting for an inference slot")
            if remaining is not None:
                wait_for = min(0.05, max(0.001, remaining))
        self._cond.wait(timeout=wait_for)

    @contextmanager
    def _local_slot(self, settings: SonderInferenceConfig, context: OperationContext):
        """Queue for one of ``settings.max_inflight`` slots on this endpoint alone.

        A caller past the limit waits here (cancel and deadline still apply)
        instead of opening one more connection the server would refuse with
        503 overloaded.
        """
        with self._cond:
            while self._local_inflight >= settings.max_inflight:
                self._wait_for_slot(context)
            self._local_inflight += 1
        try:
            yield
        finally:
            with self._cond:
                self._local_inflight = max(0, self._local_inflight - 1)
                self._cond.notify_all()

    def _has_free_endpoint(self, usable, excluded: set[str]) -> bool:
        with self._lock:
            return any(
                item[0] not in excluded and self._inflight.get(item[0], 0) < item[2].max_inflight
                for item in usable
            )

    @staticmethod
    def _extract_routing_hints(
        request: ModelRequest,
        context: OperationContext,
        model: str,
    ) -> _RoutingHints:
        options = request.options or {}
        manifest = getattr(request, "prefix_manifest", None)
        manifest_key = (
            manifest.get("cache_key")
            if isinstance(manifest, Mapping)
            else getattr(manifest, "cache_key", None)
        )
        raw_cache_key = options.get("prompt_cache_key")
        conv_key = _conversation_key(context)
        if isinstance(manifest_key, str) and manifest_key.strip():
            prefix_key = "pfx:" + manifest_key.strip()
        elif isinstance(raw_cache_key, str) and raw_cache_key.strip():
            prefix_key = "key:" + raw_cache_key.strip()
        elif conv_key:
            prefix_key = "conv:" + conv_key
        elif (request.system or "").strip() or request.history:
            prefix_key = digest_key_parts(
                "hist",
                {
                    "system": request.system or "",
                    "history": list(request.history or ()),
                },
            )
        else:
            prefix_key = ""

        raw_rf = options.get("response_format")
        rf_type = str(raw_rf.get("type") or "") if isinstance(raw_rf, Mapping) else ""
        raw_tools = options.get("tools")
        tool_names = tuple(
            sorted(
                str(fn.get("name") or "")
                for item in raw_tools
                if isinstance(item, Mapping)
                and isinstance(fn := item.get("function"), Mapping)
                and fn.get("name")
            )
        ) if isinstance(raw_tools, (list, tuple)) else ()
        adapter_key = digest_key_parts(
            "adapt",
            {
                "adapter": str(options.get("adapter") or options.get("lora") or ""),
                "num_ctx": options.get("num_ctx"),
                "think": options.get("think"),
                "reasoning_effort": options.get("reasoning_effort"),
                "response_format": rf_type,
                "tools": tool_names,
            },
        )

        approx = options.get("approx_tokens")
        if type(approx) is int and approx > 0:
            est_prompt = approx
        else:
            total_chars = len(request.system or "") + len(request.prompt or "")
            for entry in request.history or ():
                if isinstance(entry, Mapping):
                    content = entry.get("content")
                    if isinstance(content, str):
                        total_chars += len(content)
                    if entry.get("tool_calls"):
                        total_chars += len(str(entry.get("tool_calls")))
                elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                    total_chars += len(str(entry[1] or ""))
            est_prompt = max(1, total_chars // _CHAR_PER_TOKEN_ESTIMATE) if total_chars > 0 else 0

        raw_predict = options.get("num_predict", options.get("max_tokens"))
        if type(raw_predict) is int and raw_predict > 0:
            expected_decode = min(raw_predict, _MAX_EXPECTED_DECODE_TOKENS)
        else:
            expected_decode = _DEFAULT_EXPECTED_DECODE_TOKENS

        return _RoutingHints(
            model=model,
            prefix_key=prefix_key,
            adapter_key=adapter_key,
            estimated_prompt_tokens=est_prompt,
            expected_decode_tokens=expected_decode,
        )

    def _acquire_endpoint(
        self,
        usable,
        excluded: set[str],
        context: OperationContext | None = None,
        *,
        hints: _RoutingHints | None = None,
    ):
        """Pick the lowest estimated completion-cost endpoint with a free slot.

        Only endpoints below their ``max_inflight`` are eligible, so no
        endpoint is ever sent more concurrent requests than it allows; when
        every open endpoint is full the caller queues (cancel and deadline
        still apply) until a slot is released.

        When no warm prefix locality exists across ready endpoints, an idle
        unmeasured endpoint still gets an initial probe cost of ``0.0`` so each
        worker's service rate is measured rather than starved.  Once warm
        prefix locality or latency measurements apply, candidates are ranked by
        predicted completion cost (projected queue delay + uncached prompt
        work + expected decode work + restore cost minus locality saving).
        """
        with self._cond:
            while True:
                open_ = [item for item in usable if item[0] not in excluded]
                if not open_:
                    return None
                ready = [item for item in open_
                         if self._inflight.get(item[0], 0) < item[2].max_inflight]
                if not ready:
                    self._wait_for_slot(context)
                    continue
                known = [self._ms_per_token[item[0]] for item in ready if item[0] in self._ms_per_token]
                fallback = min(known) if known else 1.0
                start = self._cursor % len(ready)
                self._cursor += 1
                rotated = ready[start:] + ready[:start]
                now = self._monotonic()
                matches: dict[str, LocalityMatch] = {}
                if hints is not None and hints.prefix_key:
                    for ep_key, _gw, _ep in rotated:
                        matches[ep_key] = self._locality_table.lookup(
                            endpoint=ep_key,
                            model_identity=hints.model,
                            adapter_key=hints.adapter_key,
                            prefix_key=hints.prefix_key,
                            now=now,
                            estimated_prompt_tokens=hints.estimated_prompt_tokens,
                        )
                any_warm = any(m.effective_cached_tokens > 0.0 for m in matches.values())
                estimates: dict[str, tuple[float, float, object]] = {}

                for item in rotated:
                    ep_key, _gw, ep_cfg = item
                    inflight = self._inflight.get(ep_key, 0)
                    load = (inflight + 1) / ep_cfg.max_inflight
                    decode_rate = self._ms_per_token.get(ep_key, fallback)
                    prompt_rate = self._prompt_ms_per_token.get(
                        ep_key, max(0.05, decode_rate * 0.25),
                    )
                    est = score_endpoint_completion(
                        endpoint=ep_key,
                        inflight=inflight,
                        max_inflight=ep_cfg.max_inflight,
                        decode_ms_per_token=decode_rate,
                        prompt_ms_per_token=prompt_rate,
                        estimated_prompt_tokens=(
                            hints.estimated_prompt_tokens if hints is not None else 0
                        ),
                        expected_decode_tokens=(
                            hints.expected_decode_tokens
                            if hints is not None
                            else _DEFAULT_EXPECTED_DECODE_TOKENS
                        ),
                        match=matches.get(ep_key, _EMPTY_LOCALITY_MATCH),
                        observed_request_ms=self._request_ewma_ms.get(ep_key),
                    )
                    unmeasured_idle = (
                        not any_warm
                        and ep_key not in self._ms_per_token
                        and inflight == 0
                    )
                    rank = 0.0 if unmeasured_idle else est.total_cost_ms
                    estimates[ep_key] = (rank, load, est)

                chosen = min(rotated, key=lambda item: (estimates[item[0]][0], estimates[item[0]][1]))
                chosen_key, _chosen_gw, chosen_ep = chosen
                _rank, load, chosen_est = estimates[chosen_key]
                self._inflight[chosen_key] = self._inflight.get(chosen_key, 0) + 1
                decision = {
                    "selected_endpoint": chosen_ep.display_base_url,
                    "candidate_count": len(ready),
                    "warm_hit": chosen_est.warm_hit,
                    "effective_cached_tokens": chosen_est.effective_cached_tokens,
                    "projected_queue_delay_ms": chosen_est.projected_queue_delay_ms,
                    "uncached_prompt_work_ms": chosen_est.uncached_prompt_work_ms,
                    "expected_decode_work_ms": chosen_est.expected_decode_work_ms,
                    "locality_bonus_ms": chosen_est.locality_bonus_ms,
                    "total_cost_ms": chosen_est.total_cost_ms,
                }
                self._last_routing_decision = decision
                self._call.routing_decision = decision
                return chosen, load

    def _observe_latency(self, key: str, response: ModelResponse, load: float) -> None:
        """Fold one success into the endpoint's decode, request, and prompt rates."""
        duration = getattr(response, "duration_ms", None)
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            req_ms = max(1.0, float(duration))
            with self._lock:
                prev_req = self._request_ewma_ms.get(key)
                self._request_ewma_ms[key] = req_ms if prev_req is None else (
                    LATENCY_EWMA_ALPHA * req_ms + (1 - LATENCY_EWMA_ALPHA) * prev_req
                )
        tokens = getattr(response, "tokens_out", None)
        if isinstance(tokens, int) and tokens > 0 and isinstance(duration, (int, float)) and duration >= 0:
            sample = max(float(duration), 1.0) / tokens / max(1.0, load)
            with self._lock:
                previous = self._ms_per_token.get(key)
                self._ms_per_token[key] = sample if previous is None else (
                    LATENCY_EWMA_ALPHA * sample + (1 - LATENCY_EWMA_ALPHA) * previous
                )
        telemetry = getattr(response, "telemetry", None)
        prompt_ms = getattr(telemetry, "prompt_eval_ms", None) if telemetry is not None else None
        uncached = getattr(telemetry, "prompt_uncached_tokens", None) if telemetry is not None else None
        if uncached is None and telemetry is not None:
            uncached = getattr(telemetry, "prompt_tokens", None)
        if (
            isinstance(prompt_ms, (int, float))
            and not isinstance(prompt_ms, bool)
            and prompt_ms > 0
            and isinstance(uncached, int)
            and not isinstance(uncached, bool)
            and uncached > 0
        ):
            prompt_sample = max(0.01, float(prompt_ms) / float(uncached))
            with self._lock:
                prev_prompt = self._prompt_ms_per_token.get(key)
                self._prompt_ms_per_token[key] = (
                    prompt_sample if prev_prompt is None else (
                        LATENCY_EWMA_ALPHA * prompt_sample + (1 - LATENCY_EWMA_ALPHA) * prev_prompt
                    )
                )

    def _observe_prefix_locality(
        self,
        key: str,
        hints: _RoutingHints,
        response: ModelResponse,
    ) -> None:
        if not hints.prefix_key:
            return
        telemetry = getattr(response, "telemetry", None)
        prompt_tokens = getattr(telemetry, "prompt_tokens", None) if telemetry is not None else None
        if prompt_tokens is None:
            prompt_tokens = getattr(response, "tokens_in", None)
        cached_tokens = getattr(telemetry, "prompt_cached_tokens", None) if telemetry is not None else None
        if cached_tokens is None:
            timings = getattr(response, "timings", None)
            if isinstance(timings, Mapping):
                raw_cache_n = timings.get("cache_n")
                if type(raw_cache_n) is int and raw_cache_n >= 0:
                    cached_tokens = raw_cache_n
        served_model = getattr(response, "model", None) or hints.model
        model_id = hints.model if hints.model != DEFAULT_MODEL else str(served_model)
        self._locality_table.observe(
            endpoint=key,
            model_identity=model_id,
            adapter_key=hints.adapter_key,
            prefix_key=hints.prefix_key,
            now=self._monotonic(),
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            estimated_prompt_tokens=hints.estimated_prompt_tokens,
        )

    def _release_endpoint(self, key: str) -> None:
        with self._cond:
            remaining = self._inflight.get(key, 0) - 1
            if remaining > 0:
                self._inflight[key] = remaining
            else:
                self._inflight.pop(key, None)
            self._cond.notify_all()

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        context = self._with_request_cancel_token(request, context)
        settings = self.settings()
        target_model = self.select_model(request, settings)
        hints = self._extract_routing_hints(request, context, target_model)
        if settings.private_worker or not settings.workers:
            with self._local_slot(settings, context):
                response = self._generate_here(request, context)
            self._observe_prefix_locality(settings.base_url, hints, response)
            return response
        self._call.response_meta = {}
        self._call.routing_decision = None
        with _cancellation_scope(context.cancellation):
            usable = self._placement_candidates(request, settings, context)
        if not usable:
            # Nothing is known to be able to take it: let the primary produce
            # the precise refusal (not ready, unknown model, consent, ...).
            with self._local_slot(settings, context):
                response = self._generate_here(request, context)
            self._observe_prefix_locality(settings.base_url, hints, response)
            return response
        excluded: set[str] = set()
        last_error: SonderError | None = None
        while True:
            acquired = self._acquire_endpoint(usable, excluded, context, hints=hints)
            if acquired is None:
                assert last_error is not None
                raise last_error
            chosen, load = acquired
            key, gateway, endpoint = chosen
            decision = getattr(self._call, "routing_decision", None)
            try:
                logger.info(
                    "sonder-inference placed request on %s (%s)",
                    endpoint.display_base_url, "private worker" if endpoint.private_worker else "primary",
                )
                response = gateway._generate_here(request, context)
            except (SonderInferenceUnreachable, SonderInferenceOverloaded) as exc:
                # Provably never executed there: another endpoint may take it.
                # A timed-out readiness probe or a 503 overloaded says the
                # endpoint is saturated, so the request moves only to an
                # endpoint with a free slot now, never into a queue behind a
                # full one.
                excluded.add(key)
                last_error = exc
                saturated = isinstance(exc, SonderInferenceOverloaded) or exc.timed_out
                if saturated and not self._has_free_endpoint(usable, excluded):
                    raise
                continue
            finally:
                self._release_endpoint(key)
                if gateway is not self:
                    self._call.response_meta = gateway.last_response_meta
            if isinstance(decision, dict):
                meta = dict(self._call.response_meta)
                meta["routing_locality"] = dict(decision)
                self._call.response_meta = meta
            self._observe_latency(key, response, load)
            self._observe_prefix_locality(key, hints, response)
            if isinstance(response, SonderInferenceResponse):
                response = replace(response, endpoint=endpoint.display_base_url)
            return response

    def _generate_here(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        context = self._with_request_cancel_token(request, context)
        self._call.response_meta = {}
        if (
            not (request.prompt or "").strip()
            and not self._history_ends_with_tool(request)
            and not (request.options or {}).get("images")
        ):
            raise InvalidInput("model request prompt is empty")
        settings = self.settings()
        cfg = OpenAICompatibleConfig(
            base_url=settings.base_url, api_key=settings.api_key, model=settings.model,
        )
        self._enforce_consent(cfg, context)
        timeout = self._call_timeout(settings, context)
        options = dict(request.options or {})
        options.pop("images", None)  # encoded into OpenAI image_url content parts in _build_messages
        think = options.pop("think", None)  # decided after readiness (request_tuning)
        raw_tools = options.pop("tools", None)
        raw_tool_choice = options.pop("tool_choice", None)
        raw_response_format = options.pop("response_format", None)
        raw_logprobs = options.pop("logprobs", None)
        raw_top_logprobs = options.pop("top_logprobs", None)
        raw_effort = options.pop("reasoning_effort", None)
        from ...domain.reasoning_effort import check_effort as _check_effort
        reasoning_effort = _check_effort(raw_effort) if raw_effort is not None else None
        options.pop("cancel_token", None)  # folded into ``context`` above, never sent
        options.pop("cancel_event", None)
        tools = _normalize_tools(raw_tools)
        tool_choice = _normalize_tool_choice(raw_tool_choice, tools)
        response_format = _normalize_response_format(raw_response_format)
        logprobs, top_logprobs = _normalize_logprobs_options(raw_logprobs, raw_top_logprobs)
        model = self.select_model(request, settings)
        messages = self._build_messages(request)
        uses_native_tools = bool(
            tools is not None
            or tool_choice is not None
            or any(
                isinstance(msg, Mapping)
                and (msg.get("tool_calls") or msg.get("role") == "tool")
                for msg in messages
            )
        )
        live = stream_sink.call_stream()
        payload: dict[str, object] = {
            "model": model,
            "messages": messages,
            "stream": live is not None,
            **self._payload_options(options),
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format is not None:
            payload["response_format"] = response_format
        if logprobs is not None:
            payload["logprobs"] = logprobs
        if top_logprobs is not None:
            payload["top_logprobs"] = top_logprobs
        if live is not None:
            payload["stream_options"] = {"include_usage": True}
        cache_key = _conversation_key(context)
        if cache_key is not None:
            payload["prompt_cache_key"] = cache_key
        self._call.settings = settings
        with _cancellation_scope(context.cancellation):
            snap = self._require_ready(settings, timeout)
        self._check_liveness(context, phase="before model call")
        self._enforce_remote_model_consent(snap.document, model, context)
        if uses_native_tools and not native_tools_supported(
            snap.document, model=model, env=self._env,
        ):
            raise UnsupportedProviderFeature(NATIVE_TOOLS_REFUSAL)
        if response_format is not None and not structured_output_supported(
            snap.document, model=model,
        ):
            raise UnsupportedProviderFeature(
                "response_format is not supported by sonder-inference at %s "
                "(endpoint does not advertise structured_output capability for %r)"
                % (settings.display_base_url, model)
            )
        if logprobs is True and not logprobs_supported(
            snap.document, model=model,
        ):
            raise UnsupportedProviderFeature(
                "logprobs is not supported by sonder-inference at %s "
                "(endpoint does not advertise logprobs capability for %r)"
                % (settings.display_base_url, model)
            )
        if reasoning_effort is not None and think is None:
            from .request_tuning import thinking_supported as _thinking_supported
            if reasoning_effort == "off":
                think = False
            elif _thinking_supported(snap.document, self._env):
                think = True
        tune_request(payload, think, snap.document, self._env)
        if reasoning_effort is not None:
            from .request_tuning import thinking_supported as _thinking_supported
            if _thinking_supported(snap.document, self._env):
                kwargs = payload.get("chat_template_kwargs")
                merged = dict(kwargs) if isinstance(kwargs, Mapping) else {}
                merged["reasoning_effort"] = reasoning_effort
                payload["chat_template_kwargs"] = merged
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
        tool_calls = _extract_response_tool_calls(
            data, tools=tools, tool_choice=tool_choice,
        )
        response_logprobs = _extract_response_logprobs(data)
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        empty_length = (
            not tool_calls
            and isinstance(choice, dict) and choice.get("finish_reason") == "length"
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
                # Same trust anchor and stall limits as the first attempt: a
                # private worker's pinned CA must govern every request to it.
                with trust_scope(settings.ca_bundle), stall_limits(
                    settings.stream_idle_seconds, settings.stream_first_content_seconds,
                    idle_setting=ENV_STREAM_IDLE, first_content_setting=ENV_STREAM_FIRST_CONTENT,
                ):
                    data = self._post("/v1/chat/completions", retry, cfg, remaining,
                                      context=context)
                self._check_liveness(context, phase="during model call")
                served = served_model(data)
                tool_calls = _extract_response_tool_calls(
                    data, tools=tools, tool_choice=tool_choice,
                )
                response_logprobs = _extract_response_logprobs(data)
                repaired = True
        if tool_calls:
            choices = data.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else None
            message = choice.get("message") if isinstance(choice, dict) else None
            raw_content = message.get("content") if isinstance(message, dict) else None
            if raw_content is None:
                text = ""
            elif isinstance(raw_content, str):
                text = raw_content
            else:
                raise DependencyUnavailable("sonder-inference returned invalid message content")
        else:
            text = require_model_text(self._extract_text(data))
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
        lp_content = response_logprobs.get("content") if isinstance(response_logprobs, Mapping) else None
        self._call.response_meta = {key: value for key, value in (
            ("finish_reason", finish), ("done_reason", finish), ("timings", measured),
            ("tool_call_count", len(tool_calls) if tool_calls else None),
            ("logprobs_tokens", len(lp_content) if isinstance(lp_content, list) else None),
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
            text=text,
            model=served,
            tier=request.tier or PROVIDER_ID,
            duration_ms=int((time.monotonic() - started) * 1000),
            tokens_in=optional_token_count(prompt_count, "prompt token count"),
            tokens_out=optional_token_count(output_count, "completion token count"),
            telemetry=telemetry,
            timings=measured,
            finish_reason=finish,
            tool_calls=tool_calls,
            logprobs=response_logprobs,
        )
        default_registry().observe_inference(PROVIDER_ID, telemetry)
        return response

    # -- embed & rerank ----------------------------------------------------

    @staticmethod
    def _resolve_operation_model(
        settings: SonderInferenceConfig,
        explicit_model: str | None,
        *,
        tier: str,
        configured: str,
        operation: str,
    ) -> str:
        if explicit_model is not None:
            if not isinstance(explicit_model, str) or not explicit_model.strip():
                raise InvalidInput(
                    "sonder-inference %s model must be a non-empty string" % operation
                )
            return explicit_model.strip()
        tier_candidate = str(settings.tier_models.get(tier, "") or "").strip()
        if tier_candidate:
            return tier_candidate
        configured_candidate = str(configured or "").strip()
        if configured_candidate:
            return configured_candidate
        return settings.model

    def _require_operation_snapshot(
        self,
        settings: SonderInferenceConfig,
        timeout: float,
        context: OperationContext,
        *,
        model: str,
        explicit_model: str | None,
    ) -> HealthSnapshot:
        had_cached = self._peek_health(settings) is not None
        with _cancellation_scope(context.cancellation):
            snap = self._require_ready(settings, timeout)
        # If cached health came from before a server model switch and does not
        # list the explicitly requested model yet, refresh once before failing.
        if (
            had_cached
            and explicit_model is not None
            and model != DEFAULT_MODEL
            and not self._serves(snap, model)
        ):
            with _cancellation_scope(context.cancellation):
                refreshed = self.health(settings=settings, refresh=True)
                if refreshed.state != "ready":
                    snap = self._require_ready(settings, timeout)
                else:
                    snap = refreshed
        return snap

    @staticmethod
    def _check_served_model(
        data: Mapping[str, object],
        *,
        explicit_model: str | None,
        operation: str,
    ) -> str:
        extension = data.get("sonder")
        if isinstance(extension, dict) and "api_version" in extension:
            if extension["api_version"] != API_VERSION:
                raise DependencyUnavailable(
                    "incompatible sonder-inference API version %r (this runtime speaks %d)"
                    % (extension["api_version"], API_VERSION)
                )
        raw_served = data.get("model")
        if not isinstance(raw_served, str) or not raw_served.strip() or raw_served.strip() == DEFAULT_MODEL:
            raise DependencyUnavailable(
                "incompatible sonder-inference API: the %s response did not name the served model"
                % operation
            )
        served = raw_served.strip()
        if (
            explicit_model is not None
            and explicit_model.strip() != DEFAULT_MODEL
            and served != explicit_model.strip()
        ):
            raise DependencyUnavailable(
                "sonder-inference served %s model %r instead of requested %r"
                % (operation, served, explicit_model.strip())
            )
        return served

    _OpResult = TypeVar("_OpResult")

    def _dispatch_capability_operation(
        self,
        settings: SonderInferenceConfig,
        context: OperationContext,
        *,
        model: str,
        predicate: Callable[[Mapping[str, object] | None], bool],
        runner: Callable[["SonderInferenceGateway"], _OpResult],
    ) -> _OpResult:
        if settings.private_worker or not settings.workers:
            with self._local_slot(settings, context):
                return runner(self)
        with _cancellation_scope(context.cancellation):
            usable = self._operation_placement_candidates(
                settings, context, model=model, predicate=predicate,
            )
        if not usable:
            with self._local_slot(settings, context):
                return runner(self)
        excluded: set[str] = set()
        last_error: SonderError | None = None
        while True:
            acquired = self._acquire_endpoint(usable, excluded, context)
            if acquired is None:
                assert last_error is not None
                raise last_error
            chosen, _load = acquired
            key, gateway, _endpoint = chosen
            try:
                return runner(gateway)
            except (SonderInferenceUnreachable, SonderInferenceOverloaded) as exc:
                excluded.add(key)
                last_error = exc
                saturated = isinstance(exc, SonderInferenceOverloaded) or exc.timed_out
                if saturated and not self._has_free_endpoint(usable, excluded):
                    raise
                continue
            finally:
                self._release_endpoint(key)

    def _embed_here(
        self,
        items: list[str],
        context: OperationContext,
        *,
        settings: SonderInferenceConfig,
        model: str,
        explicit_model: str | None,
    ) -> Sequence[Embedding]:
        cfg = OpenAICompatibleConfig(
            base_url=settings.base_url, api_key=settings.api_key, model=model,
        )
        self._enforce_consent(cfg, context)
        timeout = self._call_timeout(settings, context)
        self._call.settings = settings
        snap = self._require_operation_snapshot(
            settings, timeout, context, model=model, explicit_model=explicit_model,
        )
        self._check_liveness(context, phase="before embedding call")
        if not embeddings_supported(snap.document, model=model):
            raise DependencyUnavailable(
                "sonder-inference at %s does not advertise embeddings capability for %r; "
                "configure an embedding model on Sonder Inference" % (settings.display_base_url, model)
            )
        if explicit_model is not None and model != DEFAULT_MODEL and not self._serves(snap, model):
            raise DependencyUnavailable(
                "sonder-inference at %s does not serve requested embedding model %r"
                % (settings.display_base_url, model)
            )
        with trust_scope(settings.ca_bundle):
            data = self._post(
                "/v1/embeddings",
                {"model": model, "input": items},
                cfg,
                timeout,
                context=context,
            )
        self._check_liveness(context, phase="during embedding call")
        served = self._check_served_model(
            data, explicit_model=explicit_model, operation="embeddings",
        )
        embeddings = self._embedding_results(data, items, served)
        activity_tracker.record_event(
            "embedding_outcome",
            provider=PROVIDER_ID,
            summary=json.dumps({"model": served, "count": len(embeddings)}, sort_keys=True),
            model=served,
            count=len(embeddings),
        )
        return embeddings

    def embed(
        self,
        texts: Sequence[str],
        context: OperationContext,
        *,
        model: str | None = None,
    ) -> Sequence[Embedding]:
        if isinstance(texts, (str, bytes)):
            raise InvalidInput("sonder-inference embed expects a sequence of strings")
        items = list(texts)
        if not items:
            raise InvalidInput("sonder-inference embed requires at least one input text")
        if len(items) > MAX_EMBED_INPUTS:
            raise InvalidInput(
                "sonder-inference embed accepts at most %d inputs per request" % MAX_EMBED_INPUTS
            )
        if any(not isinstance(item, str) for item in items):
            raise InvalidInput("sonder-inference embed inputs must all be strings")
        settings = self.settings()
        target_model = self._resolve_operation_model(
            settings, model, tier="embedding", configured=settings.embed_model, operation="embed",
        )
        return self._dispatch_capability_operation(
            settings,
            context,
            model=target_model,
            predicate=lambda doc: embeddings_supported(doc, model=target_model),
            runner=lambda gateway: gateway._embed_here(
                items, context, settings=gateway.settings(), model=target_model, explicit_model=model,
            ),
        )

    @staticmethod
    def _parse_rerank_chunk(
        data: Mapping[str, object],
        chunk: list[str],
        *,
        offset: int,
        explicit_model: str | None,
        return_documents: bool,
    ) -> tuple[str, list[RerankScore]]:
        served = SonderInferenceGateway._check_served_model(
            data, explicit_model=explicit_model, operation="rerank",
        )
        raw_rows = data.get("results") if "results" in data else data.get("data")
        if not isinstance(raw_rows, list) or any(not isinstance(row, Mapping) for row in raw_rows):
            raise DependencyUnavailable("sonder-inference returned invalid rerank results")
        if len(raw_rows) != len(chunk):
            raise DependencyUnavailable(
                "sonder-inference returned %d rerank results for %d documents"
                % (len(raw_rows), len(chunk))
            )
        seen_indices: set[int] = set()
        chunk_scores: list[RerankScore] = []
        for row in raw_rows:
            idx = row.get("index")
            if type(idx) is not int or not (0 <= idx < len(chunk)) or idx in seen_indices:
                raise DependencyUnavailable("sonder-inference returned invalid rerank indexes")
            seen_indices.add(idx)
            raw_score = row.get("relevance_score")
            if (
                isinstance(raw_score, bool)
                or not isinstance(raw_score, numbers.Real)
                or not math.isfinite(float(raw_score))
            ):
                raise DependencyUnavailable("sonder-inference returned invalid rerank relevance_score")
            if "document" in row and row["document"] is not None:
                raw_doc = row["document"]
                valid_doc = isinstance(raw_doc, str) or (
                    isinstance(raw_doc, Mapping) and isinstance(raw_doc.get("text"), str)
                )
                if not valid_doc:
                    raise DependencyUnavailable("sonder-inference returned invalid rerank document payload")
            doc_text = chunk[idx] if return_documents else None
            chunk_scores.append(
                RerankScore(
                    index=offset + idx,
                    relevance_score=float(raw_score),
                    model=served,
                    document=doc_text,
                )
            )
        if seen_indices != set(range(len(chunk))):
            raise DependencyUnavailable("sonder-inference returned incomplete rerank indexes")
        return served, chunk_scores

    def _rerank_here(
        self,
        query: str,
        items: list[str],
        context: OperationContext,
        *,
        settings: SonderInferenceConfig,
        model: str,
        explicit_model: str | None,
        top_n: int | None,
        chunk_size: int,
        return_documents: bool,
    ) -> tuple[RerankScore, ...]:
        cfg = OpenAICompatibleConfig(
            base_url=settings.base_url, api_key=settings.api_key, model=model,
        )
        self._enforce_consent(cfg, context)
        timeout = self._call_timeout(settings, context)
        self._call.settings = settings
        snap = self._require_operation_snapshot(
            settings, timeout, context, model=model, explicit_model=explicit_model,
        )
        self._check_liveness(context, phase="before rerank call")
        if not rerank_supported(snap.document, model=model):
            raise UnsupportedProviderFeature(
                "rerank is not supported by sonder-inference at %s "
                "(endpoint does not advertise rerank capability for %r)"
                % (settings.display_base_url, model)
            )
        if explicit_model is not None and model != DEFAULT_MODEL and not self._serves(snap, model):
            raise DependencyUnavailable(
                "sonder-inference at %s does not serve requested rerank model %r"
                % (settings.display_base_url, model)
            )
        all_scores: list[RerankScore] = []
        batch_served: str | None = None
        batch_count = 0
        with trust_scope(settings.ca_bundle):
            for offset in range(0, len(items), chunk_size):
                self._check_liveness(context, phase="before rerank batch")
                chunk = items[offset:offset + chunk_size]
                payload = {
                    "model": model,
                    "query": query,
                    "documents": chunk,
                    "top_n": len(chunk),
                    "return_documents": return_documents,
                }
                data = self._post(
                    "/v1/rerank", payload, cfg, timeout, context=context,
                )
                self._check_liveness(context, phase="during rerank call")
                served, chunk_scores = self._parse_rerank_chunk(
                    data,
                    chunk,
                    offset=offset,
                    explicit_model=explicit_model,
                    return_documents=return_documents,
                )
                if batch_served is None:
                    batch_served = served
                elif served != batch_served:
                    raise DependencyUnavailable(
                        "sonder-inference switched rerank served model mid-request (%r -> %r)"
                        % (batch_served, served)
                    )
                all_scores.extend(chunk_scores)
                batch_count += 1
        all_scores.sort(key=lambda item: (-item.relevance_score, item.index))
        selected = all_scores[:top_n] if top_n is not None else all_scores
        assert batch_served is not None
        activity_tracker.record_event(
            "rerank_outcome",
            provider=PROVIDER_ID,
            summary=json.dumps(
                {"model": batch_served, "documents": len(items), "batches": batch_count, "returned": len(selected)},
                sort_keys=True,
            ),
            model=batch_served,
            documents=len(items),
            batches=batch_count,
            returned=len(selected),
        )
        return tuple(selected)

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        context: OperationContext | None = None,
        *,
        model: str | None = None,
        top_n: int | None = None,
        batch_size: int | None = None,
        return_documents: bool = False,
    ) -> tuple[RerankScore, ...]:
        """Score ``documents`` against ``query`` via capability-gated ``/v1/rerank``."""
        if not isinstance(query, str) or not query.strip():
            raise InvalidInput("sonder-inference rerank query must be a non-empty string")
        if isinstance(documents, (str, bytes)):
            raise InvalidInput("sonder-inference rerank expects a sequence of document strings")
        items = list(documents)
        if not items:
            raise InvalidInput("sonder-inference rerank requires at least one document")
        if len(items) > MAX_RERANK_DOCUMENTS:
            raise InvalidInput(
                "sonder-inference rerank accepts at most %d documents per request" % MAX_RERANK_DOCUMENTS
            )
        if any(not isinstance(doc, str) for doc in items):
            raise InvalidInput("sonder-inference rerank documents must all be strings")
        if top_n is not None and (type(top_n) is not int or top_n <= 0):
            raise InvalidInput("sonder-inference rerank top_n must be a positive integer")
        if batch_size is not None and (
            type(batch_size) is not int or not (1 <= batch_size <= MAX_RERANK_BATCH_SIZE)
        ):
            raise InvalidInput(
                "sonder-inference rerank batch_size must be in 1..%d" % MAX_RERANK_BATCH_SIZE
            )
        if not isinstance(return_documents, bool):
            raise InvalidInput("sonder-inference rerank return_documents must be a boolean")
        chunk_size = batch_size if batch_size is not None else DEFAULT_RERANK_BATCH_SIZE
        resolved_context = (
            context
            if context is not None
            else (current_operation_context() or local_owner_context(correlation_id="sonder-rerank"))
        )
        settings = self.settings()
        target_model = self._resolve_operation_model(
            settings, model, tier="rerank", configured=settings.rerank_model, operation="rerank",
        )
        return self._dispatch_capability_operation(
            settings,
            resolved_context,
            model=target_model,
            predicate=lambda doc: rerank_supported(doc, model=target_model),
            runner=lambda gateway: gateway._rerank_here(
                query,
                items,
                resolved_context,
                settings=gateway.settings(),
                model=target_model,
                explicit_model=model,
                top_n=top_n,
                chunk_size=chunk_size,
                return_documents=return_documents,
            ),
        )


__all__ = [
    "API_VERSION",
    "CORRELATION_VALUE",
    "DEFAULT_BASE_URL",
    "DEFAULT_RERANK_BATCH_SIZE",
    "EMBEDDING_MARKERS",
    "ENV_EMBED_MODEL",
    "ENV_NATIVE_TOOLS",
    "ENV_PRIVATE_WORKERS",
    "ENV_RERANK_MODEL",
    "HealthSnapshot",
    "IdentityObservation",
    "LOGPROBS_MARKERS",
    "MAX_RERANK_BATCH_SIZE",
    "MAX_RERANK_DOCUMENTS",
    "MAX_RESPONSE_TOOL_CALLS",
    "MAX_TOP_LOGPROBS",
    "NATIVE_TOOLS_REFUSAL",
    "NATIVE_TOOL_MARKERS",
    "PROVIDER_ID",
    "PROVIDER_LABEL",
    "PrivateWorkerSpec",
    "Readiness",
    "RERANK_MARKERS",
    "RerankScore",
    "STATUS_KEYS",
    "STRUCTURED_OUTPUT_MARKERS",
    "SonderInferenceConfig",
    "SonderInferenceGateway",
    "SonderInferenceOverloaded",
    "SonderInferenceResponse",
    "SonderInferenceUnreachable",
    "WORKLOAD_BY_SOURCE",
    "check_endpoint_policy",
    "config_from_env",
    "correlation_headers",
    "direct_get_transport",
    "direct_post_transport",
    "embeddings_supported",
    "is_loopback_url",
    "logprobs_supported",
    "native_tools_supported",
    "normalize_base_url",
    "parse_private_workers",
    "read_ready_file",
    "rerank_supported",
    "structured_output_supported",
    "trust_scope",
]
