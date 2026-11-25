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
``SONDER_OPENAI_EMBED_MODEL``, ``SONDER_OPENAI_ALLOW_HTTP_NETWORKS``,
``SONDER_OPENAI_PRICE_USD_PER_MTOK`` (``<input>,<output>``),
``SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK`` (``<input>``),
``SONDER_OPENAI_DAILY_USD`` (default 10) and ``SONDER_OPENAI_MAX_TOKENS``
(default 16000).

Transport: a non-loopback endpoint must use ``https`` -- prompts and the
bearer key never cross a network in plaintext -- unless its host is a private
IP literal inside a network the operator listed in
``SONDER_OPENAI_ALLOW_HTTP_NETWORKS`` (comma/semicolon separated CIDRs; only
private ranges are honoured, host names never are).  The default transports
never follow a redirect, reach a loopback endpoint directly (an ambient
``http_proxy`` would otherwise receive the request), and read a successful
body only up to a fixed byte ceiling.

Spend cap: a **non-loopback** endpoint is metered unless the gateway was built
with ``spend_ledger=None`` (the Sonder-Inference LAN host and OpenRouter, which
has its own policy, are).  There is no built-in price table -- prices differ
per provider and drift -- so the operator states them; an unpriced metered
call is refused before it is sent, and ``0,0`` declares a free endpoint.  A
persistent per-day total (``openai-spend.json`` in the state home, the same
ledger as the Anthropic gateway) backs ``SONDER_OPENAI_DAILY_USD``: before each
send the call's worst case (the request's byte length as input tokens plus
``max_tokens`` of output, which is capped by ``SONDER_OPENAI_MAX_TOKENS``) is
reserved, and the call is refused with :class:`OpenAIDailyCapReached` if that
would take the day over the cap.  The real cost comes from the response's
``usage``; a response with no usage, and any failure after the request was
sent other than an HTTP error answer (timeout, reset, stall, cancellation,
unusable body), is charged the worst case, so spend may be over-counted but
never under-counted.  Loopback endpoints are never metered.  The ledger holds
dollar totals and counts only -- never a key, a prompt or a reply.
"""
from __future__ import annotations

import contextlib
import contextvars
import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from email.utils import format_datetime, parsedate_to_datetime
from types import MappingProxyType, SimpleNamespace
from typing import Any, Sequence

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
from ...domain.openai_spend_policy import (
    MAX_PRICE_USD_PER_MTOK,
    ChatPrice,
    OpenAISpendPolicyError,
    cost_usd,
    parse_chat_price,
    parse_embed_price,
)
from ...domain.model_capabilities import (
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_EMBEDDINGS,
    GATEWAY_CAPABILITY_FIXED_ENDPOINT,
)
from ...domain.native_tool_policy import (
    MAX_ARGUMENT_CHARS,
    MAX_ARGUMENT_DEPTH,
    TOOL_NAME_RE,
)
from ...domain.sampling_temperature import check_temperature
from ...platform.metrics import default_registry
from ..model_request_admission import (
    HostModelRequestAdmission,
    host_model_request_admission,
)
from ..model_transport import ModelCallError
from ..observability import activity_tracker
from ..tls_contexts import default_https_context
from ..provider_bindings import PROVIDER_TIERS, provider_id_for_label
from .anthropic_spend import SpendLedger
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
ENV_PRICE = "SONDER_OPENAI_PRICE_USD_PER_MTOK"
ENV_EMBED_PRICE = "SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK"
ENV_DAILY_USD = "SONDER_OPENAI_DAILY_USD"
ENV_MAX_TOKENS = "SONDER_OPENAI_MAX_TOKENS"
DEFAULT_DAILY_USD = Decimal(10)
MAX_DAILY_USD = Decimal(1_000_000)
DEFAULT_MAX_TOKENS_CEILING = 16_000
MAX_TOKENS_LIMIT = 1_000_000
SPEND_LEDGER_FILENAME = "openai-spend.json"
SPEND_LEDGER_SCHEMA = "sonder.openai_spend.v1"
_MALFORMED_BASE_URL = (
    "OpenAI-compatible base URL is not a valid http:// or https:// URL "
    "(check the host, the port, and for spaces or control characters)"
)
_PERCENT_IN_HOST = "OpenAI-compatible base URL host must not contain percent-encoding"

# Native OpenAI function-tool request/response bounds.
MAX_TOOLS = 128
MAX_TOOL_DESCRIPTION_CHARS = 16_384
MAX_TOOL_SCHEMA_CHARS = 65_536
MAX_TOOLS_PAYLOAD_BYTES = 512 * 1024
MAX_RESPONSE_TOOL_CALLS = 64
MAX_TOOL_CALL_ID_CHARS = 128
MAX_TOOL_NAME_CHARS = 128
MAX_TOOL_CALL_ARGUMENTS_CHARS = MAX_ARGUMENT_CHARS
MAX_TOOL_CALL_ARGUMENTS_DEPTH = MAX_ARGUMENT_DEPTH
_VALID_TOOL_CHOICE_STRINGS = frozenset({"auto", "none", "required"})
_VALID_FINISH_REASONS = frozenset({
    "stop", "length", "content_filter", "tool_calls", "function_call",
})
_ALLOWED_TOOL_KEYS = frozenset({"type", "function"})
_ALLOWED_TOOL_FUNCTION_KEYS = frozenset({"name", "description", "parameters", "strict"})
_ALLOWED_TOOL_CHOICE_KEYS = frozenset({"type", "function"})
_ALLOWED_TOOL_CHOICE_FN_KEYS = frozenset({"name"})

_STREAM_TOOL_CAPTURE: contextvars.ContextVar["dict[str, int] | None"] = (
    contextvars.ContextVar("sonder_openai_compat_stream_tool_capture", default=None)
)

def shared_https_context() -> ssl.SSLContext:
    """The verifying TLS context for model-endpoint openers, shared in-process.

    See :mod:`sonder_runtime.adapters.tls_contexts`: an opener built per
    request otherwise reloads the platform trust store (~150-200 ms on
    Windows) on every request, plain-HTTP loopback included.
    """
    return default_https_context()


def _host_is_loopback(host: str) -> bool:
    """True for ``localhost`` (allowing one trailing dot) or a loopback IP.

    Unspecified addresses such as ``0.0.0.0`` and ``::`` are excluded.
    """
    text = (host or "").strip().lower()
    if text.endswith("."):
        text = text[:-1]
    if not text:
        return False
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _bypasses_proxy(url: str) -> bool:
    """True when ``url`` must never be sent through an ambient proxy.

    That is every plaintext ``http://`` URL (loopback or an operator-trusted
    private IP) and every non-credentialed ``https://`` loopback URL.  An http
    request through a proxy is sent to the proxy in clear -- prompt, bearer key
    and all -- and urllib has no implicit loopback bypass.  Only remote https
    keeps proxy support: the proxy then sees just a CONNECT tunnel.  Redirects
    are never followed, so the URL's own scheme decides.
    """
    parts = urlsplit_or_none(url)
    if parts is None:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme == "http":
        return True
    if scheme == "https" and "@" not in parts.netloc:
        return _host_is_loopback(parts.hostname or "")
    return False


def _opener_for(url: str):
    """A non-redirecting opener; plaintext and loopback URLs bypass every proxy.

    See :func:`_bypasses_proxy`.  Built per call so the proxy decision reflects
    the current environment rather than the one at import time.
    """
    handlers: list = [_NoRedirect(), urllib.request.HTTPSHandler(context=shared_https_context())]
    if _bypasses_proxy(url):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


class _ToolValidationError(DependencyUnavailable):
    """Internal ``DependencyUnavailable`` carrying a bounded telemetry reason."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class OpenAIToolCall:
    """One strictly validated native OpenAI function tool call."""

    id: str
    name: str
    arguments: Mapping[str, Any]
    arguments_json: str
    type: str = "function"

    def to_openai_dict(self) -> dict[str, Any]:
        """Serialize back into standard OpenAI ``message.tool_calls[]`` wire shape."""
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.name,
                "arguments": self.arguments_json,
            },
        }


@dataclass(frozen=True)
class OpenAIToolTelemetry:
    """Content-free per-call native tool telemetry (no arguments or text)."""

    tools_requested: int = 0
    tool_choice: str | None = None
    tool_calls_returned: int = 0
    tool_names: tuple[str, ...] = ()
    streamed: bool = False
    stream_delta_count: int = 0


@dataclass(frozen=True)
class OpenAICompatibleResponse(ModelResponse):
    """Additive OpenAI-compatible response fields without changing ``ModelResponse``."""

    tool_calls: tuple[OpenAIToolCall, ...] = ()
    finish_reason: str | None = None
    reasoning_content: str | None = None
    tool_telemetry: OpenAIToolTelemetry | None = None


def _reject_nonfinite_json(constant: str) -> Any:
    raise ValueError("non-finite JSON constant %r is not allowed" % constant)


def _json_text_depth_within(text: str, limit: int) -> bool:
    """Return False once bracket nesting outside JSON string literals exceeds ``limit``."""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > limit:
                return False
        elif char in "}]":
            depth -= 1
    return True


def _value_depth_within(value: object, limit: int) -> bool:
    """Iteratively verify container nesting depth of a parsed Python structure."""
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, Mapping):
            children = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        if depth > limit:
            return False
        stack.extend((child, depth + 1) for child in children)
    return True


def _plain_json_structure(value: object) -> Any:
    """Convert nested ``Mapping``/``tuple`` containers (depth already bounded) to JSON-native types."""
    if isinstance(value, Mapping):
        return {k: _plain_json_structure(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_structure(v) for v in value]
    return value


def _validate_tool_call_id(
    raw_id: object, *, where: str, error_cls: type[SonderError], reason: str | None = None,
) -> str:
    def _fail(msg: str) -> SonderError:
        if error_cls is _ToolValidationError and reason is not None:
            return _ToolValidationError(msg, reason=reason)
        return error_cls(msg)

    if raw_id is None:
        return ""
    if not isinstance(raw_id, str):
        raise _fail("%s id must be a string" % where)
    if _UNSAFE_HEADER_VALUE_RE.search(raw_id) or len(raw_id) > MAX_TOOL_CALL_ID_CHARS:
        raise _fail("%s id must be at most %d printable characters" % (where, MAX_TOOL_CALL_ID_CHARS))
    return raw_id.strip()


def _parse_json_object_arguments(
    raw_arguments: object, *, where: str, error_cls: type[SonderError],
) -> tuple[dict[str, Any], str]:
    def _fail(msg: str, reason: str) -> SonderError:
        if error_cls is _ToolValidationError:
            return _ToolValidationError(msg, reason=reason)
        return error_cls(msg)

    if isinstance(raw_arguments, str):
        if len(raw_arguments) > MAX_TOOL_CALL_ARGUMENTS_CHARS:
            raise _fail(
                "%s arguments exceed %d characters" % (where, MAX_TOOL_CALL_ARGUMENTS_CHARS),
                "oversized_arguments",
            )
        if not _json_text_depth_within(raw_arguments, MAX_TOOL_CALL_ARGUMENTS_DEPTH):
            raise _fail(
                "%s arguments exceed nesting depth %d" % (where, MAX_TOOL_CALL_ARGUMENTS_DEPTH),
                "deeply_nested_arguments",
            )
        try:
            parsed = json.loads(raw_arguments, parse_constant=_reject_nonfinite_json)
        except (TypeError, ValueError, RecursionError) as exc:
            raise _fail("%s arguments are not valid JSON" % where, "malformed_arguments") from exc
        if not isinstance(parsed, dict):
            raise _fail("%s arguments must decode to a JSON object" % where, "non_object_arguments")
        if not _value_depth_within(parsed, MAX_TOOL_CALL_ARGUMENTS_DEPTH):
            raise _fail(
                "%s arguments exceed nesting depth %d" % (where, MAX_TOOL_CALL_ARGUMENTS_DEPTH),
                "deeply_nested_arguments",
            )
        try:
            canonical = json.dumps(
                parsed, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise _fail("%s arguments are not JSON-serializable" % where, "malformed_arguments") from exc
        if len(canonical) > MAX_TOOL_CALL_ARGUMENTS_CHARS:
            raise _fail(
                "%s arguments exceed %d characters" % (where, MAX_TOOL_CALL_ARGUMENTS_CHARS),
                "oversized_arguments",
            )
        return parsed, canonical
    if isinstance(raw_arguments, Mapping):
        if not _value_depth_within(raw_arguments, MAX_TOOL_CALL_ARGUMENTS_DEPTH):
            raise _fail(
                "%s arguments exceed nesting depth %d" % (where, MAX_TOOL_CALL_ARGUMENTS_DEPTH),
                "deeply_nested_arguments",
            )
        try:
            canonical = json.dumps(
                _plain_json_structure(raw_arguments), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise _fail("%s arguments are not valid JSON object data" % where, "malformed_arguments") from exc
        if len(canonical) > MAX_TOOL_CALL_ARGUMENTS_CHARS:
            raise _fail(
                "%s arguments exceed %d characters" % (where, MAX_TOOL_CALL_ARGUMENTS_CHARS),
                "oversized_arguments",
            )
        parsed = json.loads(canonical, parse_constant=_reject_nonfinite_json)
        return parsed, canonical
    raise _fail("%s arguments must be a JSON string or object" % where, "invalid_arguments_type")


def normalize_openai_tools(raw_tools: object) -> tuple[list[dict[str, Any]], frozenset[str]]:
    """Validate and normalize an OpenAI ``tools`` list into detached wire dicts."""
    if not isinstance(raw_tools, (list, tuple)):
        raise InvalidInput("option 'tools' must be a list of tool definitions")
    if not raw_tools:
        raise InvalidInput("option 'tools' must be a non-empty list of tool definitions")
    if len(raw_tools) > MAX_TOOLS:
        raise InvalidInput("option 'tools' exceeds the maximum of %d tools" % MAX_TOOLS)
    normalized: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, raw_tool in enumerate(raw_tools):
        where = "tools[%d]" % index
        if not isinstance(raw_tool, Mapping):
            raise InvalidInput("%s must be an object" % where)
        extra_keys = set(raw_tool) - _ALLOWED_TOOL_KEYS
        if extra_keys:
            raise InvalidInput(
                "%s contains unsupported keys: %s" % (where, ", ".join(sorted(str(k) for k in extra_keys)))
            )
        if raw_tool.get("type") != "function":
            raise InvalidInput("%s.type must be 'function'" % where)
        raw_fn = raw_tool.get("function")
        if not isinstance(raw_fn, Mapping):
            raise InvalidInput("%s.function must be an object" % where)
        extra_fn_keys = set(raw_fn) - _ALLOWED_TOOL_FUNCTION_KEYS
        if extra_fn_keys:
            raise InvalidInput(
                "%s.function contains unsupported keys: %s"
                % (where, ", ".join(sorted(str(k) for k in extra_fn_keys)))
            )
        raw_name = raw_fn.get("name")
        if not isinstance(raw_name, str) or not TOOL_NAME_RE.fullmatch(raw_name.strip()):
            raise InvalidInput("%s.function.name must be a valid tool identifier" % where)
        name = raw_name.strip()
        if name in seen_names:
            raise InvalidInput("option 'tools' contains duplicate tool name %r" % name)
        seen_names.add(name)
        fn_obj: dict[str, Any] = {"name": name}
        if "description" in raw_fn and raw_fn["description"] is not None:
            desc = raw_fn["description"]
            if not isinstance(desc, str) or "\x00" in desc or len(desc) > MAX_TOOL_DESCRIPTION_CHARS:
                raise InvalidInput(
                    "%s.function.description must be text of at most %d characters"
                    % (where, MAX_TOOL_DESCRIPTION_CHARS)
                )
            fn_obj["description"] = desc
        if "parameters" in raw_fn and raw_fn["parameters"] is not None:
            params = raw_fn["parameters"]
            if not isinstance(params, Mapping):
                raise InvalidInput("%s.function.parameters must be a JSON Schema object" % where)
            if "type" in params and params["type"] != "object":
                raise InvalidInput("%s.function.parameters.type must be 'object'" % where)
            if not _value_depth_within(params, MAX_TOOL_CALL_ARGUMENTS_DEPTH):
                raise InvalidInput(
                    "%s.function.parameters exceeds nesting depth %d"
                    % (where, MAX_TOOL_CALL_ARGUMENTS_DEPTH)
                )
            try:
                serialized_params = json.dumps(
                    _plain_json_structure(params), ensure_ascii=False, allow_nan=False,
                )
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                raise InvalidInput("%s.function.parameters must be JSON-serializable" % where) from exc
            if len(serialized_params) > MAX_TOOL_SCHEMA_CHARS:
                raise InvalidInput(
                    "%s.function.parameters exceeds %d characters" % (where, MAX_TOOL_SCHEMA_CHARS)
                )
            fn_obj["parameters"] = json.loads(serialized_params, parse_constant=_reject_nonfinite_json)
        if "strict" in raw_fn and raw_fn["strict"] is not None:
            strict = raw_fn["strict"]
            if type(strict) is not bool:
                raise InvalidInput("%s.function.strict must be a boolean" % where)
            fn_obj["strict"] = strict
        normalized.append({"type": "function", "function": fn_obj})
    encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_TOOLS_PAYLOAD_BYTES:
        raise InvalidInput(
            "option 'tools' serialized size exceeds %d bytes" % MAX_TOOLS_PAYLOAD_BYTES
        )
    return normalized, frozenset(seen_names)


def normalize_openai_tool_choice(
    raw_choice: object, *, declared_tools: frozenset[str],
) -> str | dict[str, Any]:
    """Validate and normalize an OpenAI ``tool_choice`` value against declared tools."""
    if not declared_tools:
        raise InvalidInput("option 'tool_choice' requires non-empty 'tools'")
    if isinstance(raw_choice, str):
        if raw_choice not in _VALID_TOOL_CHOICE_STRINGS:
            raise InvalidInput(
                "option 'tool_choice' string must be one of: auto, none, required"
            )
        return raw_choice
    if isinstance(raw_choice, Mapping):
        if set(raw_choice) != _ALLOWED_TOOL_CHOICE_KEYS or raw_choice.get("type") != "function":
            raise InvalidInput(
                "option 'tool_choice' object must be {'type': 'function', 'function': {'name': ...}}"
            )
        raw_fn = raw_choice.get("function")
        if not isinstance(raw_fn, Mapping) or set(raw_fn) != _ALLOWED_TOOL_CHOICE_FN_KEYS:
            raise InvalidInput("option 'tool_choice'.function must contain only 'name'")
        raw_name = raw_fn.get("name")
        if not isinstance(raw_name, str) or not TOOL_NAME_RE.fullmatch(raw_name.strip()):
            raise InvalidInput("option 'tool_choice'.function.name must be a valid tool identifier")
        name = raw_name.strip()
        if name not in declared_tools:
            raise InvalidInput("option 'tool_choice' names undeclared tool %r" % name)
        return {"type": "function", "function": {"name": name}}
    raise InvalidInput(
        "option 'tool_choice' must be 'auto', 'none', 'required', or a function selector object"
    )


def _tool_choice_label(tool_choice: str | dict[str, Any] | None) -> str | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    fn = tool_choice.get("function")
    if isinstance(fn, Mapping) and isinstance(fn.get("name"), str):
        return "function:%s" % fn["name"]
    return "function"


def _history_ends_with_tool_result(history: object) -> bool:
    if not isinstance(history, (list, tuple)) or not history:
        return False
    last = history[-1]
    return isinstance(last, Mapping) and last.get("role") == "tool"


def parse_openai_tool_calls(
    raw_tool_calls: object,
    *,
    allowed_names: frozenset[str] | None = None,
    forced_name: str | None = None,
) -> tuple[OpenAIToolCall, ...]:
    """Parse and strictly validate OpenAI ``message.tool_calls`` into typed DTOs."""
    if raw_tool_calls is None:
        return ()
    if not isinstance(raw_tool_calls, list):
        raise _ToolValidationError(
            "endpoint returned invalid tool_calls", reason="invalid_tool_calls_shape",
        )
    if len(raw_tool_calls) > MAX_RESPONSE_TOOL_CALLS:
        raise _ToolValidationError(
            "endpoint returned too many tool calls (%d > %d)"
            % (len(raw_tool_calls), MAX_RESPONSE_TOOL_CALLS),
            reason="too_many_tool_calls",
        )
    parsed_calls: list[OpenAIToolCall] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_tool_calls):
        where = "tool_calls[%d]" % index
        if not isinstance(raw_item, Mapping):
            raise _ToolValidationError(
                "endpoint returned non-object %s" % where, reason="invalid_tool_call_shape",
            )
        raw_type = raw_item.get("type", "function")
        if raw_type is not None and raw_type != "function":
            raise _ToolValidationError(
                "endpoint returned unsupported type in %s" % where,
                reason="unsupported_tool_call_type",
            )
        call_id = _validate_tool_call_id(
            raw_item.get("id"), where="endpoint %s" % where,
            error_cls=_ToolValidationError, reason="invalid_tool_call_id",
        )
        if call_id:
            if call_id in seen_ids:
                raise _ToolValidationError(
                    "endpoint returned duplicate tool_call id %r" % call_id,
                    reason="duplicate_tool_call_id",
                )
        else:
            suffix = index
            candidate = "call_%d" % suffix
            while candidate in seen_ids:
                suffix += 1
                candidate = "call_%d" % suffix
            call_id = candidate
        seen_ids.add(call_id)
        raw_fn = raw_item.get("function")
        if not isinstance(raw_fn, Mapping):
            raise _ToolValidationError(
                "endpoint returned invalid function object in %s" % where,
                reason="invalid_tool_call_shape",
            )
        raw_name = raw_fn.get("name")
        if not isinstance(raw_name, str) or not TOOL_NAME_RE.fullmatch(raw_name.strip()):
            raise _ToolValidationError(
                "endpoint returned invalid tool name in %s" % where,
                reason="invalid_tool_name",
            )
        name = raw_name.strip()
        if allowed_names is not None and name not in allowed_names:
            raise _ToolValidationError(
                "endpoint returned undeclared tool call %r" % name,
                reason="undeclared_tool",
            )
        if forced_name is not None and name != forced_name:
            raise _ToolValidationError(
                "endpoint returned tool call %r instead of forced %r" % (name, forced_name),
                reason="tool_choice_violation",
            )
        if "arguments" not in raw_fn:
            raise _ToolValidationError(
                "endpoint returned %s without arguments" % where,
                reason="missing_arguments",
            )
        parsed_args, canonical_json = _parse_json_object_arguments(
            raw_fn["arguments"], where="endpoint %s" % where, error_cls=_ToolValidationError,
        )
        parsed_calls.append(OpenAIToolCall(
            id=call_id,
            name=name,
            arguments=MappingProxyType(parsed_args),
            arguments_json=canonical_json,
            type="function",
        ))
    return tuple(parsed_calls)


class ToolCallDeltaAssembler:
    """Strictly assemble OpenAI streaming ``delta.tool_calls`` fragments by index.

    Handles interleaved parallel tool calls, incremental argument fragments,
    split function names across chunks, idempotent repeated headers, and fails
    closed on malformed deltas, conflicting/duplicate IDs, or oversized payloads.
    """

    def __init__(
        self,
        *,
        max_calls: int = MAX_RESPONSE_TOOL_CALLS,
        max_argument_chars: int = MAX_TOOL_CALL_ARGUMENTS_CHARS,
    ) -> None:
        if type(max_calls) is not int or not 1 <= max_calls <= MAX_RESPONSE_TOOL_CALLS:
            raise ValueError("max_calls must be an integer in [1, %d]" % MAX_RESPONSE_TOOL_CALLS)
        if (type(max_argument_chars) is not int
                or not 1 <= max_argument_chars <= MAX_TOOL_CALL_ARGUMENTS_CHARS):
            raise ValueError(
                "max_argument_chars must be an integer in [1, %d]" % MAX_TOOL_CALL_ARGUMENTS_CHARS
            )
        self._max_calls = max_calls
        self._max_argument_chars = max_argument_chars
        self._slots: dict[int, dict[str, str]] = {}
        self._id_to_index: dict[str, int] = {}
        self._delta_count = 0

    @property
    def delta_count(self) -> int:
        return self._delta_count

    @property
    def call_count(self) -> int:
        return len(self._slots)

    def add_delta(self, delta: object) -> dict[str, Any]:
        """Validate and merge one ``delta.tool_calls[]`` item; return normalized delta."""
        if not isinstance(delta, Mapping):
            raise _ToolValidationError(
                "endpoint streamed non-object tool_call delta",
                reason="invalid_stream_delta",
            )
        raw_idx = delta["index"] if "index" in delta else 0
        if (type(raw_idx) is not int or raw_idx < 0 or raw_idx >= self._max_calls):
            raise _ToolValidationError(
                "endpoint streamed invalid tool_call index",
                reason="invalid_stream_index",
            )
        if raw_idx not in self._slots and len(self._slots) >= self._max_calls:
            raise _ToolValidationError(
                "endpoint streamed too many distinct tool calls",
                reason="too_many_tool_calls",
            )
        slot = self._slots.setdefault(
            raw_idx, {"id": "", "type": "function", "name": "", "arguments": ""},
        )
        raw_id = delta.get("id")
        clean_id = ""
        same_id_repeated = False
        if raw_id is not None and raw_id != "":
            clean_id = _validate_tool_call_id(
                raw_id, where="streamed tool_calls[%d]" % raw_idx,
                error_cls=_ToolValidationError, reason="invalid_tool_call_id",
            )
            if clean_id:
                existing_owner = self._id_to_index.get(clean_id)
                if existing_owner is not None and existing_owner != raw_idx:
                    raise _ToolValidationError(
                        "endpoint streamed duplicate tool_call id %r across indexes" % clean_id,
                        reason="duplicate_tool_call_id",
                    )
                if slot["id"] and slot["id"] != clean_id:
                    raise _ToolValidationError(
                        "endpoint streamed conflicting tool_call id on index %d" % raw_idx,
                        reason="conflicting_tool_call_id",
                    )
                same_id_repeated = bool(slot["id"] and slot["id"] == clean_id)
                slot["id"] = clean_id
                self._id_to_index[clean_id] = raw_idx
        raw_type = delta.get("type")
        if raw_type is not None and raw_type != "":
            if raw_type != "function":
                raise _ToolValidationError(
                    "endpoint streamed unsupported tool_call type on index %d" % raw_idx,
                    reason="unsupported_tool_call_type",
                )
            slot["type"] = "function"
        raw_fn = delta.get("function")
        emitted_name: str | None = None
        emitted_args: str | None = None
        if raw_fn is not None:
            if not isinstance(raw_fn, Mapping):
                raise _ToolValidationError(
                    "endpoint streamed invalid function delta on index %d" % raw_idx,
                    reason="invalid_stream_delta",
                )
            raw_name = raw_fn.get("name")
            if raw_name is not None:
                if not isinstance(raw_name, str):
                    raise _ToolValidationError(
                        "endpoint streamed non-string function name on index %d" % raw_idx,
                        reason="invalid_tool_name",
                    )
                if raw_name:
                    if _UNSAFE_HEADER_VALUE_RE.search(raw_name):
                        raise _ToolValidationError(
                            "endpoint streamed invalid control characters in function name on index %d" % raw_idx,
                            reason="invalid_tool_name",
                        )
                    if same_id_repeated and slot["name"] == raw_name:
                        pass  # idempotent proxy re-send of full header
                    else:
                        if len(slot["name"]) + len(raw_name) > MAX_TOOL_NAME_CHARS:
                            raise _ToolValidationError(
                                "endpoint streamed oversized function name on index %d" % raw_idx,
                                reason="invalid_tool_name",
                            )
                        slot["name"] += raw_name
                        emitted_name = raw_name
            raw_args = raw_fn.get("arguments")
            if raw_args is not None:
                if not isinstance(raw_args, str):
                    raise _ToolValidationError(
                        "endpoint streamed non-string arguments on index %d" % raw_idx,
                        reason="invalid_arguments_type",
                    )
                if raw_args:
                    if len(slot["arguments"]) + len(raw_args) > self._max_argument_chars:
                        raise _ToolValidationError(
                            "endpoint streamed oversized arguments on index %d" % raw_idx,
                            reason="oversized_arguments",
                        )
                    slot["arguments"] += raw_args
                    emitted_args = raw_args
                elif emitted_name is not None:
                    emitted_args = ""
        self._delta_count += 1
        normalized: dict[str, Any] = {"index": raw_idx}
        if clean_id and not same_id_repeated:
            normalized["id"] = clean_id
        if raw_type == "function" and not same_id_repeated:
            normalized["type"] = "function"
        fn_delta: dict[str, str] = {}
        if emitted_name is not None:
            fn_delta["name"] = emitted_name
        if emitted_args is not None:
            fn_delta["arguments"] = emitted_args
        if fn_delta:
            normalized["function"] = fn_delta
        return normalized

    def feed_chunk(self, chunk: object) -> list[dict[str, Any]]:
        """Feed one OpenAI ``chat.completion.chunk`` object; return normalized tool deltas."""
        if not isinstance(chunk, Mapping):
            raise _ToolValidationError(
                "endpoint streamed non-object chunk", reason="invalid_stream_delta",
            )
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise _ToolValidationError(
                "endpoint streamed non-object choice", reason="invalid_stream_delta",
            )
        delta = choice.get("delta")
        if not isinstance(delta, Mapping) or "tool_calls" not in delta or delta["tool_calls"] is None:
            return []
        raw_tool_calls = delta["tool_calls"]
        if not isinstance(raw_tool_calls, list):
            raise _ToolValidationError(
                "endpoint streamed non-list delta.tool_calls", reason="invalid_stream_delta",
            )
        return [self.add_delta(item) for item in raw_tool_calls]

    def to_openai_list(self) -> list[dict[str, Any]]:
        """Return assembled ``message.tool_calls`` entries ordered by delta index."""
        return [
            {
                "id": self._slots[idx]["id"],
                "type": self._slots[idx]["type"],
                "function": {
                    "name": self._slots[idx]["name"],
                    "arguments": self._slots[idx]["arguments"],
                },
            }
            for idx in sorted(self._slots)
        ]

    def finalize(
        self,
        *,
        allowed_names: frozenset[str] | None = None,
        forced_name: str | None = None,
    ) -> tuple[OpenAIToolCall, ...]:
        """Validate and materialize all assembled streaming tool calls."""
        if not self._slots:
            return ()
        return parse_openai_tool_calls(
            self.to_openai_list(), allowed_names=allowed_names, forced_name=forced_name,
        )


class _ToolCallStreamProxy:
    """Validate and assemble streaming ``tool_calls`` deltas while forwarding live events."""

    def __init__(
        self,
        live: Any,
        assembler: ToolCallDeltaAssembler,
        *,
        allow_tool_calls: bool,
    ) -> None:
        self._live = live
        self._assembler = assembler
        self._allow_tool_calls = allow_tool_calls

    @property
    def cancelled(self) -> bool:
        return bool(self._live.cancelled) if self._live is not None else False

    def emit(self, text: str) -> bool:
        if self._live is None:
            return True
        return bool(self._live.emit(text))

    def emit_reasoning(self, text: str) -> bool:
        if self._live is None:
            return True
        emit_fn = getattr(self._live, "emit_reasoning", None)
        return bool(emit_fn(text)) if callable(emit_fn) else True

    def emit_tool_call_delta(self, delta: dict[str, object]) -> bool:
        if not self._allow_tool_calls:
            raise _ToolValidationError(
                "endpoint streamed tool calls when tool calls were not allowed",
                reason="unrequested_tool_calls",
            )
        normalized = self._assembler.add_delta(delta)
        if self._live is None:
            return True
        emit_fn = getattr(self._live, "emit_tool_call_delta", None)
        return bool(emit_fn(normalized)) if callable(emit_fn) else True


def _stream_transport(url: str, payload: dict, headers: dict, timeout) -> dict:
    """``"stream": true`` POST: read the event stream into one completion.

    Reuses the Sonder Inference stream reader (:mod:`.sse_stream`) with this
    module's opener policy (no redirects; plaintext and loopback bypass every proxy).  The
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
        if _bypasses_proxy(url):
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    allow_tool_calls = bool(payload.get("tools")) and payload.get("tool_choice") != "none"
    assembler = ToolCallDeltaAssembler()
    proxy = _ToolCallStreamProxy(
        stream_sink.call_stream(), assembler, allow_tool_calls=allow_tool_calls,
    )
    document = post_streaming(
        url, payload, headers, timeout or _DEFAULT_TIMEOUT, proxy,
        exchange=exchange, opener_factory=opener_factory, label="endpoint",
    )
    capture = _STREAM_TOOL_CAPTURE.get()
    if capture is not None:
        capture["delta_count"] = assembler.delta_count
    if isinstance(document, dict) and assembler.delta_count > 0:
        choices = document.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                message["tool_calls"] = assembler.to_openai_list()
    return document


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


class OpenAISpendUnpriced(Forbidden):
    """A metered call was refused because no price was declared for it."""


class OpenAIDailyCapReached(Forbidden):
    """Today's local spend reached ``SONDER_OPENAI_DAILY_USD``."""

    def __init__(self, message: str, *, cap_usd: Decimal, spent_usd: Decimal) -> None:
        super().__init__(message)
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd


def _usd(value: Decimal) -> str:
    return "$%s" % value.quantize(Decimal("0.0001"), rounding="ROUND_UP")


class _DefaultLedger:
    """Sentinel type: build the metered default ledger."""

    def __repr__(self) -> str:
        return "<default OpenAI spend ledger>"


_DEFAULT_LEDGER = _DefaultLedger()
_UNSAFE_HEADER_VALUE_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_api_key(api_key: object) -> None:
    if not isinstance(api_key, str):
        raise InvalidInput("OpenAI-compatible API key must be a string")
    if _UNSAFE_HEADER_VALUE_RE.search(api_key):
        raise InvalidInput(
            "OpenAI-compatible API key must not contain control or newline characters"
        )
    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError:
        raise InvalidInput(
            "OpenAI-compatible API key must contain only HTTP header-safe characters"
        ) from None


ENV_TIER_MODELS = "SONDER_OPENAI_TIER_MODELS"


def parse_tier_models(raw: str | None) -> dict[str, str]:
    """Parse ``tier=model,tier=model`` pairs from ``SONDER_OPENAI_TIER_MODELS``."""
    result: dict[str, str] = {}
    for entry in str(raw or "").split(","):
        text = entry.strip()
        if not text or "=" not in text:
            continue
        tier, _, model_id = text.partition("=")
        tier_key = tier.strip().lower()
        model_val = model_id.strip()
        if tier_key and model_val:
            result[tier_key] = model_val
    return result


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str = field(default="", repr=False)
    model: str = ""
    embed_model: str = ""
    tier_models: Mapping[str, str] = field(default_factory=dict)
    # CIDRs whose private IP-literal hosts may be reached over plain http.
    plaintext_networks: tuple[str, ...] = ()
    # Streaming stall limits in seconds (0 disables); inside the call budget.
    stream_idle_seconds: float = 0.0
    stream_first_content_seconds: float = 0.0
    # Spend metering (used only for a metered gateway on a non-loopback
    # endpoint).  ``price``/``embed_price`` None means unpriced: refused.
    price: ChatPrice | None = None
    embed_price: Decimal | None = None
    daily_usd: Decimal = DEFAULT_DAILY_USD
    max_tokens_ceiling: int = DEFAULT_MAX_TOKENS_CEILING

    def model_for_tier(self, tier: str | None) -> str:
        key = str(tier or "").strip().lower()
        if key and self.tier_models:
            mapped = str(self.tier_models.get(key, "") or "").strip()
            if mapped:
                return mapped
        return self.model

    def __post_init__(self) -> None:
        _validate_api_key(self.api_key)
        for value, name in ((self.stream_idle_seconds, ENV_STREAM_IDLE),
                            (self.stream_first_content_seconds, ENV_STREAM_FIRST_CONTENT)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                0.0 <= float(value) <= MAX_STREAM_STALL_SECONDS
            ):
                raise InvalidInput("%s must be in [0, 86400] (0 disables it)" % name)
        if (not isinstance(self.daily_usd, Decimal)
                or not self.daily_usd.is_finite()
                or not Decimal(0) <= self.daily_usd <= MAX_DAILY_USD):
            raise InvalidInput("%s must be a finite Decimal between 0 and %s" % (ENV_DAILY_USD, MAX_DAILY_USD))
        if (isinstance(self.max_tokens_ceiling, bool)
                or not isinstance(self.max_tokens_ceiling, int)
                or not 1 <= self.max_tokens_ceiling <= MAX_TOKENS_LIMIT):
            raise InvalidInput("%s must be an integer in [1, %d]" % (ENV_MAX_TOKENS, MAX_TOKENS_LIMIT))
        if self.price is not None:
            if not isinstance(self.price, ChatPrice):
                raise InvalidInput("%s must be a ChatPrice or None" % ENV_PRICE)
            for rate, label in ((self.price.input, "input price"), (self.price.output, "output price")):
                if (not isinstance(rate, Decimal)
                        or not rate.is_finite()
                        or not Decimal(0) <= rate <= MAX_PRICE_USD_PER_MTOK):
                    raise InvalidInput(
                        "%s %s must be a finite Decimal between 0 and %s"
                        % (ENV_PRICE, label, MAX_PRICE_USD_PER_MTOK)
                    )
        if self.embed_price is not None:
            if (not isinstance(self.embed_price, Decimal)
                    or not self.embed_price.is_finite()
                    or not Decimal(0) <= self.embed_price <= MAX_PRICE_USD_PER_MTOK):
                raise InvalidInput(
                    "%s must be a finite Decimal between 0 and %s"
                    % (ENV_EMBED_PRICE, MAX_PRICE_USD_PER_MTOK)
                )


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


def _env_base_url() -> str:
    import os

    return os.environ.get("SONDER_OPENAI_BASE_URL", "").strip()


def configured_endpoint_is_loopback() -> bool:
    """``endpoint_is_loopback`` for a gateway built without a config (env-resolved)."""
    base_url = _env_base_url()
    return bool(base_url) and OpenAICompatibleGateway._is_loopback(base_url)


def _env_price(name: str, parse: Callable[[str], Any]) -> Any:
    import os

    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return parse(raw)
    except OpenAISpendPolicyError as exc:
        raise InvalidInput("%s: %s" % (name, exc)) from exc


def _env_daily_usd() -> Decimal:
    import os

    raw = os.environ.get(ENV_DAILY_USD, "").strip()
    if not raw:
        return DEFAULT_DAILY_USD
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise InvalidInput("%s must be a dollar amount" % ENV_DAILY_USD) from exc
    if not value.is_finite() or not 0 <= value <= MAX_DAILY_USD:
        raise InvalidInput("%s must be between 0 and %s" % (ENV_DAILY_USD, MAX_DAILY_USD))
    return value


def _env_max_tokens() -> int:
    import os

    raw = os.environ.get(ENV_MAX_TOKENS, "").strip()
    if not raw:
        return DEFAULT_MAX_TOKENS_CEILING
    if not (raw.isascii() and raw.isdecimal()) or not 1 <= int(raw) <= MAX_TOKENS_LIMIT:
        raise InvalidInput("%s must be an integer in [1, %d]" % (ENV_MAX_TOKENS, MAX_TOKENS_LIMIT))
    return int(raw)


def _config_from_env(*, metered: bool = True) -> OpenAICompatibleConfig:
    """Resolve settings from the environment.

    The spend settings are parsed only for a ``metered`` gateway on a
    non-loopback endpoint, so a malformed price never breaks an unmetered one.
    """
    import os

    base_url = _env_base_url()
    spend: dict[str, object] = {}
    if metered and not OpenAICompatibleGateway._is_loopback(base_url):
        spend = {
            "price": _env_price(ENV_PRICE, parse_chat_price),
            "embed_price": _env_price(ENV_EMBED_PRICE, parse_embed_price),
            "daily_usd": _env_daily_usd(),
            "max_tokens_ceiling": _env_max_tokens(),
        }
    return OpenAICompatibleConfig(
        base_url=base_url,
        api_key=os.environ.get("SONDER_OPENAI_API_KEY", "").strip(),
        model=os.environ.get("SONDER_OPENAI_MODEL", "").strip(),
        embed_model=os.environ.get("SONDER_OPENAI_EMBED_MODEL", "").strip(),
        tier_models=parse_tier_models(os.environ.get(ENV_TIER_MODELS, "")),
        plaintext_networks=_split_networks(os.environ.get(ENV_ALLOW_HTTP_NETWORKS, "")),
        stream_idle_seconds=_env_seconds(ENV_STREAM_IDLE),
        stream_first_content_seconds=_env_seconds(ENV_STREAM_FIRST_CONTENT),
        **spend,
    )


class _SendTrace:
    """What ``_post`` got as far as doing; read by the spend accounting."""

    __slots__ = ("sent", "answered")

    def __init__(self) -> None:
        self.sent = False       # the transport was invoked: the request may be billed
        self.answered = False   # the provider replied with an HTTP error status: not billed


class _SpendCall:
    """One metered call's reservation and its single accounting.

    Owned by the thread running the call.  ``charge`` and ``charge_worst`` record
    at most once; recording never raises (``SpendLedger.record`` guarantees it).
    """

    def __init__(self, ledger: SpendLedger, token: int, *, worst: Decimal,
                 prompt_estimate: int, output_estimate: int,
                 input_rate: Decimal, output_rate: Decimal) -> None:
        self._ledger = ledger
        self._token = token
        self.worst = worst
        self.prompt_estimate = prompt_estimate
        self.output_estimate = output_estimate
        self._input_rate = input_rate
        self._output_rate = output_rate
        self.charged = False

    def charge(self, prompt_tokens: int | None, output_tokens: int | None) -> None:
        """Record the real cost; a count the response did not report is charged at its worst case."""
        if self.charged:
            return
        if prompt_tokens is None and output_tokens is None:
            self.charge_worst("the response carried no token usage")
            return
        prompt = self.prompt_estimate if prompt_tokens is None else prompt_tokens
        output = self.output_estimate if output_tokens is None else output_tokens
        if prompt_tokens is None or output_tokens is None:
            logger.warning(
                "openai-compatible: response reported only part of its token usage; "
                "the missing part was charged at its worst case"
            )
        usd = cost_usd(input_tokens=prompt, output_tokens=output,
                       input_rate=self._input_rate, output_rate=self._output_rate)
        self.charged = True
        self._ledger.record(usd=usd, slot=1, input_tokens=prompt, output_tokens=output)

    def charge_worst(self, reason: str) -> None:
        if self.charged:
            return
        self.charged = True
        logger.warning(
            "openai-compatible: %s; the call's worst-case cost %s was charged against the cap",
            reason, self.worst,
        )
        self._ledger.record(
            usd=self.worst, slot=1,
            input_tokens=self.prompt_estimate, output_tokens=self.output_estimate,
        )

    def release(self) -> None:
        self._ledger.release(self._token)


def _reported_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000_000:
        return None
    return value


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
        spend_ledger: SpendLedger | None | _DefaultLedger = _DEFAULT_LEDGER,
    ):
        # Sentinel: metered with the OpenAI ledger in the state home.  None:
        # unmetered.  A SpendLedger: metered with that ledger.  Metering still
        # applies only to a non-loopback endpoint.
        if isinstance(spend_ledger, _DefaultLedger):
            spend_ledger = SpendLedger(
                filename=SPEND_LEDGER_FILENAME, schema=SPEND_LEDGER_SCHEMA,
                label="OpenAI-compatible",
            )
        elif spend_ledger is not None and not isinstance(spend_ledger, SpendLedger):
            raise TypeError("spend_ledger must be a SpendLedger or None")
        self._spend_ledger: SpendLedger | None = spend_ledger
        self._provider_label = str(provider_label)
        # Fail at construction on a label that would not map to a provider id.
        self._provider_id = provider_id_for_label(self._provider_label)
        self._extra_headers = extra_headers
        self._http_error_classifier = http_error_classifier
        self._connect_error_classifier = connect_error_classifier
        self._get_transport = get_transport
        self._config = config
        self._transport = transport
        self._openai_call = threading.local()
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

    @property
    def last_response_meta(self) -> dict[str, object]:
        """Content-free measurements for this thread's last call; failures clear old data."""
        return dict(getattr(self._openai_call, "response_meta", {}))

    # -- status ------------------------------------------------------------

    @staticmethod
    def _resolved_tier_models(cfg: OpenAICompatibleConfig) -> dict[str, str]:
        """Resolve per-tier models from ``tier_models`` with ``model`` fallback."""
        resolved: dict[str, str] = {}
        for tier in PROVIDER_TIERS:
            model_id = cfg.model_for_tier(tier)
            if model_id:
                resolved[tier] = model_id
        for raw_tier, raw_model in (cfg.tier_models or {}).items():
            key = str(raw_tier or "").strip().lower()
            val = str(raw_model or "").strip()
            if key and val and key not in resolved:
                resolved[key] = val
        return resolved

    @staticmethod
    def _bounded_status_detail(value: object, limit: int = 240) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def served_tier_models(self) -> Mapping[str, Mapping[str, str]]:
        """``{provider: {tier: model}}`` from configuration alone (no I/O)."""
        try:
            cfg = self._config or _config_from_env(metered=self._spend_ledger is not None)
        except Exception:  # noqa: BLE001 - display metadata never fails a caller
            return {}
        if not cfg.base_url:
            return {}
        resolved = self._resolved_tier_models(cfg)
        return {self._provider_id: resolved} if resolved else {}

    def provider_status(self) -> Mapping[str, Mapping[str, object]]:
        """Content-free, key-free status from configuration alone (no network probe)."""
        entry: dict[str, object] = {
            "provider": self._provider_id,
            "state": "unavailable",
            "healthy": False,
            "configured": False,
            "capabilities": sorted(CAPABILITIES),
            "base_url": None,
            "loopback": None,
            "model": None,
            "embed_model": None,
            "tier_models": {},
            "models": [],
            "api_key_configured": False,
            "detail": "",
        }
        try:
            cfg = self._config or _config_from_env(metered=self._spend_ledger is not None)
        except Exception as exc:  # noqa: BLE001 - status reports, never raises
            entry["detail"] = self._bounded_status_detail(
                exc if isinstance(exc, SonderError) else type(exc).__name__
            )
            return {self._provider_id: entry}
        tier_models = self._resolved_tier_models(cfg)
        models: list[str] = []
        for candidate in (cfg.model, *tier_models.values()):
            if candidate and candidate not in models:
                models.append(candidate)
        entry.update(
            model=cfg.model or None,
            embed_model=cfg.embed_model or None,
            tier_models=tier_models,
            models=models,
            api_key_configured=bool(cfg.api_key),
        )
        if not cfg.base_url:
            entry["detail"] = "no OpenAI-compatible endpoint configured (set SONDER_OPENAI_BASE_URL)"
            return {self._provider_id: entry}
        safe_base = redact_url(cfg.base_url)
        has_userinfo = "@" in cfg.base_url.split("?", 1)[0].split("#", 1)[0]
        if (
            not has_userinfo
            and safe_base != "<invalid url>"
            and not safe_base.endswith("://<redacted>")
        ):
            entry["base_url"] = safe_base
        loopback = self._is_loopback(cfg.base_url)
        entry["loopback"] = loopback
        try:
            self._require_secure_transport(cfg)
        except Exception as exc:  # noqa: BLE001 - status reports, never raises
            entry["detail"] = self._bounded_status_detail(
                exc if isinstance(exc, SonderError) else type(exc).__name__
            )
            return {self._provider_id: entry}
        entry.update(
            configured=True,
            state="unknown",
            healthy=None,
            detail="configured (%s; not probed)" % ("loopback" if loopback else "remote"),
        )
        return {self._provider_id: entry}

    # -- configuration & consent ------------------------------------------

    def _resolved_config(self) -> OpenAICompatibleConfig:
        cfg = self._config or _config_from_env(metered=self._spend_ledger is not None)
        if not cfg.base_url:
            raise InvalidInput(
                "no OpenAI-compatible endpoint configured "
                "(set SONDER_OPENAI_BASE_URL)"
            )
        return cfg

    @staticmethod
    def _is_loopback(base_url: str) -> bool:
        # Unparsable is never loopback; _require_secure_transport refuses it.
        # Shared by the consent gate, the transport check and the hostedness
        # probe: only a plain http(s) URL with no userinfo and a loopback host
        # counts (0.0.0.0 is the unspecified address, not loopback).
        parts = urlsplit_or_none(base_url)
        if parts is None or parts.scheme.lower() not in ("http", "https"):
            return False
        if "@" in parts.netloc:
            return False
        return _host_is_loopback(parts.hostname or "")

    def endpoint_is_loopback(self) -> bool:
        """Whether a call made now would send its prompt to a loopback endpoint.

        The same test the consent gate applies, on the base URL a call would
        resolve (the injected config, else ``SONDER_OPENAI_BASE_URL``).  No I/O;
        an unset or unparsable endpoint is not loopback.
        """
        try:
            # The hook subclasses override, so a subclass never reads this
            # class's SONDER_OPENAI_BASE_URL for its own endpoint.
            base_url = self._resolved_config().base_url
        except InvalidInput:
            return False
        return self._is_loopback(base_url)

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
        if parts.query or parts.fragment:
            raise InvalidInput(
                "OpenAI-compatible base URL must not contain a query string or fragment"
            )
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

    # -- spend cap -----------------------------------------------------------

    def _metered(self, cfg: OpenAICompatibleConfig) -> bool:
        return self._spend_ledger is not None and not self._is_loopback(cfg.base_url)

    def spend_snapshot(self) -> dict[str, object] | None:
        """Content-free totals for status surfaces; None when this gateway is not metered."""
        if self._spend_ledger is None:
            return None
        return self._spend_ledger.snapshot()

    def _reserve(
        self, cfg: OpenAICompatibleConfig, *, prompt_estimate: int, output_estimate: int,
        input_rate: Decimal, output_rate: Decimal,
    ) -> _SpendCall:
        """Refuse a call whose worst case would pass the cap; else reserve that cost."""
        ledger = self._spend_ledger
        if ledger is None:
            raise InternalFailure("spend reservation requested on an unmetered gateway")
        worst = cost_usd(input_tokens=prompt_estimate, output_tokens=output_estimate,
                         input_rate=input_rate, output_rate=output_rate)
        cap = cfg.daily_usd
        if cap <= 0:
            raise OpenAIDailyCapReached(
                "OpenAI-compatible calls are disabled: the daily cap %s is 0, which "
                "refuses every metered call; raise it to use this endpoint" % ENV_DAILY_USD,
                cap_usd=cap, spent_usd=ledger.today_usd(),
            )
        token, spent, inflight = ledger.try_reserve(worst, cap)
        if token is None:
            if worst > cap:
                raise OpenAIDailyCapReached(
                    "OpenAI-compatible call refused: its worst-case cost %s (max_tokens of "
                    "output plus the request size) is larger than the whole %s daily cap "
                    "(%s); lower max_tokens, shorten the prompt, or raise the cap"
                    % (_usd(worst), _usd(cap), ENV_DAILY_USD),
                    cap_usd=cap, spent_usd=spent,
                )
            raise OpenAIDailyCapReached(
                "OpenAI-compatible daily spend cap reached: %s spent today (UTC)%s, and "
                "this call's worst-case cost %s would take the day over the %s cap (%s); "
                "new calls resume after 00:00 UTC, or raise the cap"
                % (_usd(spent), (" plus up to %s in flight" % _usd(inflight)) if inflight else "",
                   _usd(worst), _usd(cap), ENV_DAILY_USD),
                cap_usd=cap, spent_usd=spent,
            )
        return _SpendCall(
            ledger, token, worst=worst, prompt_estimate=prompt_estimate,
            output_estimate=output_estimate, input_rate=input_rate, output_rate=output_rate,
        )

    @staticmethod
    def _unpriced(env_name: str, what: str, free: str) -> OpenAISpendUnpriced:
        return OpenAISpendUnpriced(
            "this OpenAI-compatible endpoint is metered but has no declared %s price, so "
            "its spend could not be counted against %s: set %s (USD per 1M tokens; "
            "\"%s\" declares a free endpoint)" % (what, ENV_DAILY_USD, env_name, free)
        )

    def _meter_chat_payload(
        self, cfg: OpenAICompatibleConfig, payload: dict, num_predict: object,
    ) -> ChatPrice | None:
        """Set the payload's output limit; return the price when this call is metered.

        Unmetered calls keep the caller's limit exactly.  A metered call is
        refused when unpriced and is clamped to [1, ceiling] output tokens.
        """
        if not self._metered(cfg):
            if num_predict is not None:
                payload["max_tokens"] = int(num_predict)  # type: ignore[call-overload]
            return None
        # Transport policy refuses first: an unsafe endpoint is never a pricing question.
        self._require_secure_transport(cfg)
        if cfg.price is None:
            raise self._unpriced(ENV_PRICE, "chat", "0,0")
        ceiling = cfg.max_tokens_ceiling
        payload["max_tokens"] = (
            ceiling if num_predict is None
            else max(1, min(int(num_predict), ceiling))  # type: ignore[call-overload]
        )
        return cfg.price

    def _reserve_chat(
        self, cfg: OpenAICompatibleConfig, price: ChatPrice, payload: dict,
    ) -> _SpendCall:
        # A token is at least a byte, so the UTF-8 byte length bounds input tokens.
        prompt_estimate = len(json.dumps(payload["messages"]).encode("utf-8"))
        if "tools" in payload:
            prompt_estimate += len(json.dumps(payload["tools"]).encode("utf-8"))
        if "tool_choice" in payload:
            prompt_estimate += len(json.dumps(payload["tool_choice"]).encode("utf-8"))
        return self._reserve(
            cfg,
            prompt_estimate=prompt_estimate,
            output_estimate=int(payload["max_tokens"]),
            input_rate=price.input, output_rate=price.output,
        )

    def _post_chat_accounted(
        self, cfg: OpenAICompatibleConfig, payload: dict, timeout,
        *, context: OperationContext, num_predict: int,
    ) -> dict:
        """POST one chat request with the same spend metering as ``generate``.

        For callers that send chat requests without ``generate`` (the protocol
        probe): price check, reservation, accounting and release.
        """
        price = self._meter_chat_payload(cfg, payload, num_predict)
        if price is None:
            return self._post("/v1/chat/completions", payload, cfg, timeout, context=context)
        spend = self._reserve_chat(cfg, price, payload)
        try:
            data = self._post_metered(lambda trace: self._post(
                "/v1/chat/completions", payload, cfg, timeout, context=context, trace=trace,
            ), spend)
            self._account_chat(data, spend)
            return data
        finally:
            spend.release()

    @staticmethod
    def _post_metered(post: Callable[[_SendTrace], dict], spend: _SpendCall) -> dict:
        """Run ``post``; a failure after the send other than an HTTP answer is charged."""
        trace = _SendTrace()
        try:
            return post(trace)
        except BaseException:
            if trace.sent and not trace.answered:
                spend.charge_worst("the request was sent but did not complete normally")
            raise

    # -- generate ----------------------------------------------------------

    def _record_tool_validation_failure(
        self, *, reason: str, streamed: bool, tools_requested: int,
    ) -> None:
        """Emit content-free warning & activity telemetry when tool validation rejects a reply."""
        payload = {
            "reason": reason,
            "streamed": streamed,
            "tools_requested": tools_requested,
        }
        logger.warning(
            "openai-compatible: tool call validation failed (reason=%s, streamed=%s, tools_requested=%d)",
            reason, streamed, tools_requested,
        )
        activity_tracker.record_event(
            "openai_tool_validation_failed",
            provider=self._provider_id,
            summary=json.dumps(payload, sort_keys=True),
            **payload,
        )

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        self._openai_call.response_meta = {}
        has_prompt = bool((request.prompt or "").strip())
        if not has_prompt and not _history_ends_with_tool_result(
            getattr(request, "history", ())
        ):
            raise InvalidInput("model request prompt is empty")
        cfg = self._resolved_config()
        self._enforce_consent(cfg, context)
        timeout = self._check_liveness(context)

        options = dict(request.options or {})
        live = stream_sink.call_stream()
        use_stream = live is not None or bool(getattr(request, "stream", False))
        model = str(options.get("model") or cfg.model_for_tier(request.tier) or request.tier or "default")
        logger.debug(f"OpenAICompatibleGateway.generate: model={model!r}, base_url={redact_url(cfg.base_url)!r}, timeout={timeout}")
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(request),
            # The caller's live stream (or explicit request.stream) decides:
            # a streaming call reads the event stream and returns the same
            # aggregated document, so everything below is unchanged.
            "stream": use_stream,
        }
        temperature = check_temperature(options.get("temperature"))
        if temperature is not None:  # unset: the server's preset applies
            payload["temperature"] = temperature
        if use_stream:
            payload["stream_options"] = {"include_usage": True}
        declared_tools: frozenset[str] = frozenset()
        tool_choice: str | dict[str, Any] | None = None
        if "tools" in options and options["tools"] is not None:
            normalized_tools, declared_tools = normalize_openai_tools(options["tools"])
            payload["tools"] = normalized_tools
        if "tool_choice" in options and options["tool_choice"] is not None:
            tool_choice = normalize_openai_tool_choice(
                options["tool_choice"], declared_tools=declared_tools,
            )
            payload["tool_choice"] = tool_choice
        if "parallel_tool_calls" in options and options["parallel_tool_calls"] is not None:
            if not declared_tools:
                raise InvalidInput("option 'parallel_tool_calls' requires non-empty 'tools'")
            parallel = options["parallel_tool_calls"]
            if type(parallel) is not bool:
                raise InvalidInput("option 'parallel_tool_calls' must be a boolean")
            payload["parallel_tool_calls"] = parallel
        try:
            template_kwargs = normalize_chat_template_options(options)
        except ChatTemplateOptionsError as exc:
            raise InvalidInput(str(exc)) from exc
        if template_kwargs:
            effort = template_kwargs.get("reasoning_effort")
            if effort is not None and "enable_thinking" not in template_kwargs:
                template_kwargs = dict(template_kwargs)
                template_kwargs["enable_thinking"] = effort != "off"
            payload["chat_template_kwargs"] = template_kwargs
        price = self._meter_chat_payload(cfg, payload, options.get("num_predict"))

        spend: _SpendCall | None = None
        try:
            if price is not None:
                spend = self._reserve_chat(cfg, price, payload)
            return self._generate_sent(
                request, context, cfg, payload, model, timeout, spend,
                declared_tools=declared_tools, tool_choice=tool_choice,
            )
        finally:
            if spend is not None:
                spend.release()

    def _generate_sent(
        self, request: ModelRequest, context: OperationContext, cfg: OpenAICompatibleConfig,
        payload: dict, model: str, timeout, spend: _SpendCall | None,
        *,
        declared_tools: frozenset[str] = frozenset(),
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ModelResponse:
        started = time.monotonic()
        streamed = bool(payload.get("stream"))
        stream_capture: dict[str, int] = {"delta_count": 0}

        def post(trace: _SendTrace | None) -> dict:
            token = _STREAM_TOOL_CAPTURE.set(stream_capture)
            try:
                with stall_limits(
                    cfg.stream_idle_seconds, cfg.stream_first_content_seconds,
                    idle_setting=ENV_STREAM_IDLE, first_content_setting=ENV_STREAM_FIRST_CONTENT,
                ):
                    return self._post(
                        "/v1/chat/completions", payload, cfg, timeout, context=context, trace=trace,
                    )
            finally:
                _STREAM_TOOL_CAPTURE.reset(token)

        try:
            data = post(None) if spend is None else self._post_metered(post, spend)
        except _ToolValidationError as exc:
            self._record_tool_validation_failure(
                reason=exc.reason, streamed=streamed, tools_requested=len(declared_tools),
            )
            raise
        if spend is not None:
            # A completed response is billable even if the caller was cancelled:
            # account before the liveness gate below, and before parsing it.
            self._account_chat(data, spend)
        # urllib cannot observe a token while blocked.  A final liveness gate
        # prevents a response/cancellation race from publishing stale work.
        self._check_liveness(context, phase="during model call")

        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise DependencyUnavailable("endpoint returned no choices")
        if not isinstance(choices[0], dict):
            raise DependencyUnavailable("endpoint returned an invalid choice")
        raw_message = choices[0].get("message")
        if raw_message is None:
            message: dict[str, Any] = {}
        elif isinstance(raw_message, dict):
            message = raw_message
        else:
            raise DependencyUnavailable("endpoint returned an invalid message")
        raw_finish = choices[0].get("finish_reason")
        finish_reason = (
            raw_finish
            if isinstance(raw_finish, str) and raw_finish in _VALID_FINISH_REASONS
            else None
        )
        raw_reasoning = message.get("reasoning_content")
        if not isinstance(raw_reasoning, str) or not raw_reasoning:
            raw_reasoning = message.get("reasoning")
        reasoning_content = (
            raw_reasoning if isinstance(raw_reasoning, str) and raw_reasoning else None
        )

        forced_name: str | None = None
        if isinstance(tool_choice, Mapping):
            fn_obj = tool_choice.get("function")
            if isinstance(fn_obj, Mapping) and isinstance(fn_obj.get("name"), str):
                forced_name = fn_obj["name"]

        raw_tool_calls = message.get("tool_calls")
        tool_calls: tuple[OpenAIToolCall, ...] = ()
        try:
            if raw_tool_calls is not None and not isinstance(raw_tool_calls, list):
                raise _ToolValidationError(
                    "endpoint returned invalid tool_calls", reason="invalid_tool_calls_shape",
                )
            if raw_tool_calls:
                if not declared_tools:
                    raise _ToolValidationError(
                        "endpoint returned tool calls when no tools were requested",
                        reason="unrequested_tool_calls",
                    )
                if tool_choice == "none":
                    raise _ToolValidationError(
                        "endpoint returned tool calls despite tool_choice='none'",
                        reason="tool_choice_none_violation",
                    )
                tool_calls = parse_openai_tool_calls(
                    raw_tool_calls, allowed_names=declared_tools, forced_name=forced_name,
                )
            elif tool_choice == "required" or forced_name is not None:
                raw_content = message.get("content")
                if not (
                    finish_reason == "length"
                    and (raw_content is None or isinstance(raw_content, str) and not raw_content.strip())
                ):
                    raise _ToolValidationError(
                        "endpoint returned no tool calls despite required tool_choice",
                        reason="missing_required_tool_call",
                    )
        except _ToolValidationError as exc:
            self._record_tool_validation_failure(
                reason=exc.reason, streamed=streamed, tools_requested=len(declared_tools),
            )
            raise

        if tool_calls:
            raw_content = message.get("content")
            if raw_content is None:
                text = ""
            elif isinstance(raw_content, str):
                text = raw_content
            else:
                raise DependencyUnavailable("endpoint returned invalid message content")
        else:
            text = require_model_text(self._extract_text(data))

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

        tool_names = tuple(call.name for call in tool_calls)
        stream_delta_count = int(stream_capture.get("delta_count", 0))
        choice_label = _tool_choice_label(tool_choice)
        tool_telemetry: OpenAIToolTelemetry | None = None
        if declared_tools or tool_calls:
            tool_telemetry = OpenAIToolTelemetry(
                tools_requested=len(declared_tools),
                tool_choice=choice_label,
                tool_calls_returned=len(tool_calls),
                tool_names=tool_names,
                streamed=streamed,
                stream_delta_count=stream_delta_count,
            )

        response_meta: dict[str, object] = {
            key: value for key, value in (
                ("finish_reason", finish_reason),
                ("done_reason", finish_reason),
                ("streamed", True if streamed else None),
                ("tools_requested", len(declared_tools) if declared_tools else None),
                ("tool_choice", choice_label),
                ("tool_calls_returned", len(tool_calls) if (declared_tools or tool_calls) else None),
                ("tool_names", list(tool_names) if tool_names else None),
                ("stream_delta_count", stream_delta_count if stream_delta_count else None),
            ) if value is not None
        }
        self._openai_call.response_meta = response_meta
        if response_meta:
            activity_tracker.record_event(
                "inference_outcome",
                provider=self._provider_id,
                summary=json.dumps(response_meta, sort_keys=True),
                **response_meta,
            )

        response = OpenAICompatibleResponse(
            text=text,
            model=model,
            tier=request.tier or "openai",
            duration_ms=duration_ms,
            tokens_in=optional_token_count(prompt_count, "prompt token count"),
            tokens_out=optional_token_count(output_count, "completion token count"),
            telemetry=telemetry,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
            tool_telemetry=tool_telemetry,
        )
        if duration_ms > 60_000:
            logger.warning(
                f"slow inference: model={model!r} took {duration_ms}ms "
                f"(>{60_000}ms threshold), tokens_out={response.tokens_out}"
            )
        logger.debug(
            f"OpenAICompatibleGateway.generate: completed in {duration_ms}ms, "
            f"tokens_in={response.tokens_in}, tokens_out={response.tokens_out}, "
            f"tools_requested={len(declared_tools)}, tool_calls={len(tool_calls)}, "
            f"finish_reason={finish_reason!r}"
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
        if not self._metered(cfg):
            data = self._post(
                "/v1/embeddings", {"model": model, "input": items}, cfg, timeout,
                context=context,
            )
            self._check_liveness(context, phase="during embedding call")
            return self._embedding_results(data, items, model)
        self._require_secure_transport(cfg)
        if cfg.embed_price is None:
            raise self._unpriced(ENV_EMBED_PRICE, "embedding", "0")
        spend = self._reserve(
            cfg, prompt_estimate=sum(len(str(item).encode("utf-8", "replace")) for item in items),
            output_estimate=0, input_rate=cfg.embed_price, output_rate=Decimal(0),
        )
        try:
            data = self._post_metered(lambda trace: self._post(
                "/v1/embeddings", {"model": model, "input": items}, cfg, timeout,
                context=context, trace=trace,
            ), spend)
            self._account_embed(data, spend)
            self._check_liveness(context, phase="during embedding call")
        finally:
            spend.release()
        return self._embedding_results(data, items, model)

    def _account_chat(self, data: dict, spend: _SpendCall) -> None:
        usage = data.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            spend.charge_worst("the response carried an invalid usage object")
            return
        timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
        prompt = usage.get("prompt_tokens")
        if prompt is None:
            prompt = timings.get("prompt_n")
        output = usage.get("completion_tokens")
        if output is None:
            output = timings.get("predicted_n")
        spend.charge(_reported_count(prompt), _reported_count(output))

    def _account_embed(self, data: dict, spend: _SpendCall) -> None:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            spend.charge_worst("the embedding response carried no usable usage object")
            return
        prompt = _reported_count(usage.get("prompt_tokens"))
        if prompt is None:
            spend.charge_worst("the embedding response reported no prompt tokens")
            return
        spend.charge(prompt, 0)

    def _embedding_results(self, data: dict, items: list, model: str) -> Sequence[Embedding]:
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
    def _normalize_history_tool_calls(raw_tool_calls: object) -> list[dict[str, Any]]:
        if not isinstance(raw_tool_calls, (list, tuple)) or not raw_tool_calls:
            raise InvalidInput("assistant history tool_calls must be a non-empty list")
        if len(raw_tool_calls) > MAX_RESPONSE_TOOL_CALLS:
            raise InvalidInput(
                "assistant history tool_calls exceeds maximum of %d" % MAX_RESPONSE_TOOL_CALLS
            )
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(raw_tool_calls):
            where = "history tool_calls[%d]" % index
            if isinstance(item, OpenAIToolCall):
                item = item.to_openai_dict()
            if not isinstance(item, Mapping):
                raise InvalidInput("%s must be an object" % where)
            raw_type = item.get("type", "function")
            if raw_type is not None and raw_type != "function":
                raise InvalidInput("%s.type must be 'function'" % where)
            call_id = _validate_tool_call_id(
                item.get("id"), where=where, error_cls=InvalidInput,
            )
            if call_id:
                if call_id in seen_ids:
                    raise InvalidInput("duplicate tool_call id %r in assistant history" % call_id)
            else:
                suffix = index
                candidate = "call_%d" % suffix
                while candidate in seen_ids:
                    suffix += 1
                    candidate = "call_%d" % suffix
                call_id = candidate
            seen_ids.add(call_id)
            raw_fn = item.get("function")
            if not isinstance(raw_fn, Mapping):
                raise InvalidInput("%s.function must be an object" % where)
            raw_name = raw_fn.get("name")
            if not isinstance(raw_name, str) or not TOOL_NAME_RE.fullmatch(raw_name.strip()):
                raise InvalidInput("%s.function.name must be a valid tool identifier" % where)
            if "arguments" not in raw_fn:
                raise InvalidInput("%s.function.arguments is required" % where)
            _parsed, canonical = _parse_json_object_arguments(
                raw_fn["arguments"], where=where, error_cls=InvalidInput,
            )
            normalized.append({
                "id": call_id,
                "type": "function",
                "function": {"name": raw_name.strip(), "arguments": canonical},
            })
        return normalized

    @staticmethod
    def _build_messages(request: ModelRequest) -> list:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        history = getattr(request, "history", None) or ()
        for entry in history:
            if isinstance(entry, Mapping) and entry.get("role"):
                role = entry["role"]
                if role == "assistant" and entry.get("tool_calls") is not None:
                    raw_content = entry.get("content")
                    if raw_content is not None and not isinstance(raw_content, str):
                        raise InvalidInput("assistant history message content must be a string or null")
                    messages.append({
                        "role": "assistant",
                        "content": "" if raw_content is None else raw_content,
                        "tool_calls": OpenAICompatibleGateway._normalize_history_tool_calls(
                            entry["tool_calls"]
                        ),
                    })
                elif role == "tool":
                    raw_content = entry.get("content")
                    if not isinstance(raw_content, str):
                        raise InvalidInput("tool history message must include string content")
                    tool_msg: dict[str, Any] = {"role": "tool", "content": raw_content}
                    if "tool_call_id" in entry and entry["tool_call_id"] is not None:
                        tool_call_id = _validate_tool_call_id(
                            entry["tool_call_id"],
                            where="tool history message tool_call_id",
                            error_cls=InvalidInput,
                        )
                        if not tool_call_id:
                            raise InvalidInput("tool history message tool_call_id must not be empty")
                        tool_msg["tool_call_id"] = tool_call_id
                    if "name" in entry and entry["name"] is not None:
                        raw_name = entry["name"]
                        if not isinstance(raw_name, str) or not TOOL_NAME_RE.fullmatch(raw_name.strip()):
                            raise InvalidInput("tool history message name must be a valid tool identifier")
                        tool_msg["name"] = raw_name.strip()
                    messages.append(tool_msg)
                elif "content" in entry:
                    messages.append({"role": role, "content": entry["content"]})
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                messages.append({"role": entry[0], "content": entry[1]})
        if (request.prompt or "").strip() or not _history_ends_with_tool_result(history):
            messages.append({"role": "user", "content": request.prompt})
        return messages

    @staticmethod
    def _extract_text(data: dict) -> str:
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise DependencyUnavailable("endpoint returned no choices")
        if not isinstance(choices[0], dict):
            raise DependencyUnavailable("endpoint returned an invalid choice")
        raw_message = choices[0].get("message")
        if raw_message is None:
            message: dict[str, Any] = {}
        elif isinstance(raw_message, dict):
            message = raw_message
        else:
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
            _validate_api_key(cfg.api_key)
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
        *, context: OperationContext | None = None, trace: _SendTrace | None = None,
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

        def send() -> dict:
            if trace is not None:
                trace.sent = True  # from here on the provider may bill this request
            return transport(url, payload, headers, timeout)

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
                        send,
                    )
                else:
                    data = send()
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
            try:
                if trace is not None:
                    trace.answered = True  # the provider answered with an error status: not billed
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
            finally:
                with contextlib.suppress(Exception):
                    exc.close()
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
                try:
                    body = exc.read(GET_BODY_LIMIT + 1)
                except Exception:  # noqa: BLE001 - the status alone still answers
                    body = b""
                return int(exc.code), body if isinstance(body, bytes) else b""
            finally:
                with contextlib.suppress(Exception):
                    exc.close()

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
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise DependencyUnavailable("endpoint returned non-JSON") from exc
