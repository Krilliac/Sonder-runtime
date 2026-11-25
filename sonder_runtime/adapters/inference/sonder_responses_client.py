"""Strictly-typed client adapter for Sonder Inference's stateful ``/v1/responses`` API.

Targets ``sonder-infer serve`` (Inference HTTP API v1, PR #96) and OpenAI
Responses API semantics:

* **Stateful chaining.** ``previous_response_id`` chains multi-turn history and
  maps directly onto ``sonder-inference``'s backend ``session_key`` for KV
  prefix-cache reuse.  :class:`SonderResponsesSession` manages multi-turn and
  ``function_call_output`` continuations cleanly.
* **Full wire contract.** Supports ``instructions``, string or structured
  ``input`` items (messages with text and inline base64-only images,
  ``function_call``, ``function_call_output``), flat and nested function
  ``tools`` / ``tool_choice``, structured ``text.format`` (``text``,
  ``json_object``, ``json_schema``), ``reasoning.effort``, sampling controls,
  ``logprobs`` / ``top_logprobs``, ``prompt_cache_key``, ``store``, and named
  SSE streaming (``response.created``, ``response.output_text.delta``,
  ``response.function_call_arguments.delta``, ``response.completed``).
* **Correlation and priority headers.** Validates and sends
  ``X-Sonder-Priority`` (class name ``interactive`` / ``subagent`` /
  ``background`` or integer ``-16..16``), ``X-Sonder-Workload``,
  ``X-Sonder-Deadline-Ms``, ``X-Sonder-Reasoning-Budget``, and run/parent/
  agent/task correlation IDs.
* **Strict transport security.** Reuses :mod:`.exchange_transport` for
  direct, proxy-free, non-redirected, wall-clock-budgeted exchanges with
  cooperative cancellation and bounded response/SSE reads.  Loopback endpoints
  allow only ``127.0.0.1``, ``localhost`` or ``[::1]``; private workers require
  ``https://`` on a private/link-local IP literal with an explicit port, a
  pinned CA bundle (no fallback to the system trust store), and a bearer token;
  other non-loopback endpoints require ``allow_remote=True``, ``https://``, a
  bearer token, and ``OperationContext.cloud_allowed=True``.
"""
from __future__ import annotations

import base64
import binascii
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from ...application.context import OperationContext, current_operation_context
from ...domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    SonderError,
)
from .exchange_transport import (
    DEFAULT_TIMEOUT_SECONDS,
    ERROR_BODY_LIMIT,
    RESPONSE_BODY_LIMIT,
    _ACTIVE_CANCELLATION,
    _BudgetedHTTPHandler,
    _BudgetedHTTPSHandler,
    _CompositeCancellation,
    _ExchangeBudget,
    _NoRedirect,
    _bounded,
    _cancellation_scope,
    _cancelled_quietly,
    _error_document_fields,
    _read_bounded,
    trust_scope,
)
from .log_safe import UNSENDABLE_URL_CHARS, redact_url, urlsplit_or_none

API_VERSION = 1
DEFAULT_BASE_URL = "http://127.0.0.1:11437"
DEFAULT_MODEL = "default"
RESPONSES_PATH = "/v1/responses"

ACCEPTED_LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
_BIND_ADDRESS_REWRITES: Mapping[str, str] = MappingProxyType({
    "0.0.0.0": "127.0.0.1",
    "::": "::1",
})

VALID_PRIORITY_CLASSES: frozenset[str] = frozenset({
    "interactive",
    "subagent",
    "background",
})
MIN_PRIORITY_INT = -16
MAX_PRIORITY_INT = 16

VALID_WORKLOADS: frozenset[str] = frozenset({
    "interactive_user",
    "owner_orchestrator",
    "critic_verification",
    "implementation_worker",
    "research_worker",
    "background_indexing",
    "maintenance",
})
WORKLOAD_BY_SOURCE: Mapping[str, str] = MappingProxyType({
    "http": "interactive_user",
    "repl": "interactive_user",
    "mcp": "owner_orchestrator",
    "worker": "implementation_worker",
    "system": "maintenance",
})
PRIORITY_BY_SOURCE: Mapping[str, str] = MappingProxyType({
    "http": "interactive",
    "repl": "interactive",
    "mcp": "interactive",
    "worker": "subagent",
    "system": "background",
})

VALID_REASONING_EFFORTS: frozenset[str] = frozenset({
    "none",
    "off",
    "low",
    "medium",
    "high",
    "xhigh",
})
VALID_MESSAGE_ROLES: frozenset[str] = frozenset({
    "system",
    "developer",
    "user",
    "assistant",
    "tool",
})
VALID_TEXT_PART_TYPES: frozenset[str] = frozenset({
    "input_text",
    "output_text",
    "text",
})
VALID_IMAGE_PART_TYPES: frozenset[str] = frozenset({
    "input_image",
    "image_url",
})
SUPPORTED_IMAGE_MIMES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

MAX_IMAGE_DECODED_BYTES = 16 * 1024 * 1024
MAX_IMAGES_PER_REQUEST = 8
MAX_TOOLS_PER_REQUEST = 128
MAX_OUTPUT_TOKENS_LIMIT = 1 << 20
MAX_TOP_LOGPROBS = 20
MAX_DEADLINE_MS = 2_147_483_647
MAX_PREVIOUS_RESPONSE_ID_BYTES = 128
MAX_PROMPT_CACHE_KEY_BYTES = 256
MAX_SSE_LINE_BYTES = 1_048_576
MIN_RESPONSE_BYTES = 1024
MAX_TIMEOUT_SECONDS = 86_400.0
MAX_TEMPERATURE = 10.0

_CORRELATION_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_MODEL_ID = re.compile(r"[^\s]{1,128}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_FORBIDDEN_IMAGE_SCHEMES: tuple[str, ...] = ("http://", "https://", "file://", "ftp://")

PriorityValue = Literal["interactive", "subagent", "background"] | int
WorkloadValue = Literal[
    "interactive_user",
    "owner_orchestrator",
    "critic_verification",
    "implementation_worker",
    "research_worker",
    "background_indexing",
    "maintenance",
]
ReasoningEffortValue = Literal["none", "off", "low", "medium", "high", "xhigh"]
MessageRole = Literal["system", "developer", "user", "assistant", "tool"]
TextPartType = Literal["input_text", "output_text", "text"]
ImagePartType = Literal["input_image", "image_url"]
TextFormatType = Literal["text", "json_object", "json_schema"]
ToolChoiceKeyword = Literal["auto", "none", "required"]


class _ReadableBinaryStream(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def readline(self, size: int = -1) -> bytes: ...


class _ReadableHTTPResponse(_ReadableBinaryStream, Protocol):
    status: int
    headers: Mapping[str, str]

    def close(self) -> None: ...

    def __enter__(self) -> "_ReadableHTTPResponse": ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


class PreviousResponseNotFound(NotFound):
    """Server did not find ``previous_response_id`` (unknown or LRU-evicted)."""

    def __init__(self, message: str, *, previous_response_id: str | None = None) -> None:
        super().__init__(message)
        self.previous_response_id = previous_response_id


def _is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _has_header_control_chars(value: str) -> bool:
    return any(ch in ("\r", "\n", "\x00") for ch in value)


# -- URL & endpoint policy validation -----------------------------------------


def normalize_responses_base_url(value: str) -> str:
    """Validate and normalize a Sonder Inference base URL.

    Enforces ``http``/``https`` scheme, forbids credentials, queries,
    fragments, control characters, percent-encoding in netloc, and non-accepted
    loopback aliases (``sonder-infer serve``'s Host check accepts only
    ``127.0.0.1``, ``localhost``, and ``[::1]``).  Maps wildcard bind addresses
    ``0.0.0.0`` and ``::`` to their matching loopback addresses.
    """
    raw = str(value or "").strip()
    if not raw or UNSENDABLE_URL_CHARS.search(raw):
        raise InvalidInput(
            "sonder-inference base URL %r must not be empty or contain whitespace "
            "or control characters" % redact_url(raw)
        )
    parts = urlsplit_or_none(raw)
    if parts is None:
        raise InvalidInput(
            "sonder-inference base URL %r is not a valid URL" % redact_url(raw)
        )
    if "%" in parts.netloc:
        raise InvalidInput(
            "sonder-inference base URL host must not contain percent-encoding"
        )
    host = parts.hostname
    port = parts.port
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise InvalidInput(
            "sonder-inference base URL must be http(s)://host[:port][/path] "
            "without credentials, query or fragment"
        )
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in parts.netloc):
        raise InvalidInput(
            "sonder-inference base URL host must not contain whitespace or control characters"
        )
    if UNSENDABLE_URL_CHARS.search(parts.path):
        raise InvalidInput(
            "sonder-inference base URL path must not contain whitespace or control characters"
        )
    host = _BIND_ADDRESS_REWRITES.get(host.lower(), host.lower())
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        host = str(address)
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
    """Return True when *base_url* targets a loopback host."""
    parts = urlsplit_or_none(base_url)
    if parts is None:
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_private_worker_url(value: str, *, where: str = "private_worker") -> str:
    """Validate an operator-approved private worker URL.

    Requires ``https://``, a private or link-local IP literal (never DNS,
    public, loopback, unspecified, or multicast), and an explicit port.
    """
    if not isinstance(value, str):
        raise InvalidInput("%s.url must be a string" % where)
    url = normalize_responses_base_url(value)
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
    if (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or not (address.is_private or address.is_link_local)
    ):
        raise InvalidInput(
            "%s.url host %s is not a private-network address; private workers "
            "never reach public or loopback hosts" % (where, address)
        )
    if parts.port is None:
        raise InvalidInput("%s.url must name an explicit port" % where)
    return url


def _validate_ca_bundle_path(ca_bundle: str, *, required: bool) -> str:
    raw = str(ca_bundle or "").strip()
    if not raw:
        if required:
            raise InvalidInput("private worker requires an absolute ca_bundle PEM file path")
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise InvalidInput("ca_bundle must be an absolute path to an existing PEM file")
    return str(path)


@dataclass(frozen=True)
class SonderResponsesConfig:
    """Validated transport and endpoint configuration for :class:`SonderResponsesClient`."""

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = ""
    allow_remote: bool = False
    private_worker: bool = False
    ca_bundle: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = RESPONSE_BODY_LIMIT
    max_sse_line_bytes: int = MAX_SSE_LINE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str):
            raise InvalidInput("sonder-inference base_url must be a string")
        normalized = (
            validate_private_worker_url(self.base_url)
            if self.private_worker
            else normalize_responses_base_url(self.base_url)
        )
        object.__setattr__(self, "base_url", normalized)
        if not isinstance(self.model, str) or not _MODEL_ID.fullmatch(self.model):
            raise InvalidInput("sonder-inference model id must be 1-128 non-space characters")
        if not isinstance(self.api_key, str) or _has_header_control_chars(self.api_key):
            raise InvalidInput("sonder-inference api_key must be a single-line string without control characters")
        object.__setattr__(self, "api_key", self.api_key.strip())
        if not isinstance(self.allow_remote, bool) or not isinstance(self.private_worker, bool):
            raise InvalidInput("allow_remote and private_worker must be booleans")
        resolved_ca = _validate_ca_bundle_path(self.ca_bundle, required=self.private_worker)
        object.__setattr__(self, "ca_bundle", resolved_ca)
        if (
            not _is_strict_number(self.timeout_seconds)
            or not math.isfinite(float(self.timeout_seconds))
            or not (0.0 < float(self.timeout_seconds) <= MAX_TIMEOUT_SECONDS)
        ):
            raise InvalidInput("timeout_seconds must be a finite number in (0, %d]" % int(MAX_TIMEOUT_SECONDS))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if (
            not _is_strict_int(self.max_response_bytes)
            or not (MIN_RESPONSE_BYTES <= self.max_response_bytes <= RESPONSE_BODY_LIMIT)
        ):
            raise InvalidInput(
                "max_response_bytes must be an integer in [%d, %d]"
                % (MIN_RESPONSE_BYTES, RESPONSE_BODY_LIMIT)
            )
        if (
            not _is_strict_int(self.max_sse_line_bytes)
            or not (MIN_RESPONSE_BYTES <= self.max_sse_line_bytes <= MAX_SSE_LINE_BYTES)
        ):
            raise InvalidInput(
                "max_sse_line_bytes must be an integer in [%d, %d]"
                % (MIN_RESPONSE_BYTES, MAX_SSE_LINE_BYTES)
            )

    @property
    def loopback(self) -> bool:
        return is_loopback_url(self.base_url)

    @property
    def display_base_url(self) -> str:
        """Loopback URL, or scheme + host[:port] only for non-loopback targets."""
        if self.loopback:
            return self.base_url
        parts = urlsplit(self.base_url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    @property
    def endpoint_url(self) -> str:
        return "%s%s" % (self.base_url, RESPONSES_PATH)


def validate_endpoint_policy(
    config: SonderResponsesConfig,
    *,
    context: OperationContext | None = None,
) -> None:
    """Enforce fail-closed TLS, token, private-worker, and cloud-consent rules.

    Must pass before any socket is opened or any prompt byte leaves the host.
    """
    if config.ca_bundle:
        ca_path = Path(config.ca_bundle)
        if not ca_path.is_absolute() or not ca_path.is_file():
            raise Forbidden(
                "sonder-inference CA bundle for %s is missing or unreadable"
                % config.display_base_url
            )
    if config.loopback and not config.private_worker:
        return
    missing: list[str] = []
    if not config.allow_remote:
        missing.append("SONDER_ALLOW_REMOTE_INFERENCE=1 (allow_remote=True)")
    if urlsplit(config.base_url).scheme != "https":
        missing.append("an https:// base URL")
    if not config.api_key:
        missing.append("a worker token" if config.private_worker else "an API key")
    if config.private_worker and not config.ca_bundle:
        missing.append("a worker CA bundle")
    if missing:
        raise Forbidden(
            "sonder-inference endpoint %s is not loopback; remote inference requires %s"
            % (config.display_base_url, ", ".join(missing))
        )
    if not config.private_worker:
        if context is None or not context.cloud_allowed:
            raise Forbidden(
                "sonder-inference endpoint %s is not loopback and this operation context "
                "does not allow prompts to leave the machine"
                % config.display_base_url
            )


# -- Request schema types & validation ----------------------------------------


def validate_data_image_uri(uri: str, *, where: str = "image_url") -> str:
    """Validate an inline base64 ``data:image/...`` URI (strict zero-SSRF)."""
    if not isinstance(uri, str) or not uri:
        raise InvalidInput("%s must be a non-empty data:image/<mime>;base64,... URI" % where)
    lower_prefix = uri[:16].lower()
    if lower_prefix.startswith(_FORBIDDEN_IMAGE_SCHEMES):
        raise InvalidInput(
            "%s forbids remote or local file URLs (SSRF protection); only inline "
            "data:image/<png|jpeg|webp|gif>;base64,... URIs are accepted" % where
        )
    if not uri.startswith("data:"):
        raise InvalidInput(
            "%s must be an inline data:image/<png|jpeg|webp|gif>;base64,... URI" % where
        )
    semi = uri.find(";base64,", 5)
    if semi < 0:
        raise InvalidInput(
            "%s must use base64 encoding (data:image/<png|jpeg|webp|gif>;base64,...)" % where
        )
    mime = uri[5:semi]
    if mime not in SUPPORTED_IMAGE_MIMES:
        raise InvalidInput(
            "%s has unsupported image MIME type %r (allowed: %s)"
            % (where, mime, ", ".join(sorted(SUPPORTED_IMAGE_MIMES)))
        )
    payload = uri[semi + 8:]
    if not payload or (len(payload) % 4) != 0:
        raise InvalidInput("%s base64 image data must be non-empty with length a multiple of 4" % where)
    try:
        decoded = base64.b64decode(payload.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise InvalidInput("%s contains invalid base64 image data" % where) from None
    if not decoded:
        raise InvalidInput("%s base64 image data decoded to 0 bytes" % where)
    if len(decoded) > MAX_IMAGE_DECODED_BYTES:
        raise InvalidInput(
            "%s decoded image exceeds maximum size of %d bytes" % (where, MAX_IMAGE_DECODED_BYTES)
        )
    if base64.b64encode(decoded).decode("ascii") != payload:
        raise InvalidInput("%s contains non-canonical base64 padding bits" % where)
    return uri


@dataclass(frozen=True)
class ResponsesTextPart:
    """One text content part inside a message input item."""

    text: str
    part_type: TextPartType = "input_text"

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise InvalidInput("text part .text must be a string")
        if self.part_type not in VALID_TEXT_PART_TYPES:
            raise InvalidInput("text part .part_type must be input_text, output_text, or text")

    def to_payload(self) -> dict[str, object]:
        return {"type": self.part_type, "text": self.text}


@dataclass(frozen=True)
class ResponsesImagePart:
    """One inline base64 image content part (remote URLs strictly refused)."""

    image_url: str
    part_type: ImagePartType = "input_image"

    def __post_init__(self) -> None:
        if self.part_type not in VALID_IMAGE_PART_TYPES:
            raise InvalidInput("image part .part_type must be input_image or image_url")
        validate_data_image_uri(self.image_url, where="image_part.image_url")

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        mime_type: str,
        *,
        part_type: ImagePartType = "input_image",
    ) -> "ResponsesImagePart":
        if mime_type not in SUPPORTED_IMAGE_MIMES:
            raise InvalidInput(
                "unsupported image MIME type %r (allowed: %s)"
                % (mime_type, ", ".join(sorted(SUPPORTED_IMAGE_MIMES)))
            )
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise InvalidInput("image bytes must be non-empty")
        if len(data) > MAX_IMAGE_DECODED_BYTES:
            raise InvalidInput("image exceeds maximum size of %d bytes" % MAX_IMAGE_DECODED_BYTES)
        encoded = base64.b64encode(bytes(data)).decode("ascii")
        return cls(image_url="data:%s;base64,%s" % (mime_type, encoded), part_type=part_type)

    def to_payload(self) -> dict[str, object]:
        if self.part_type == "image_url":
            return {"type": "image_url", "image_url": {"url": self.image_url}}
        return {"type": "input_image", "image_url": self.image_url}


ResponsesContentPart = ResponsesTextPart | ResponsesImagePart


@dataclass(frozen=True)
class ResponsesMessageInput:
    """One message item in ``/v1/responses`` ``input``."""

    role: MessageRole
    content: str | Sequence[ResponsesContentPart]

    def __post_init__(self) -> None:
        if self.role not in VALID_MESSAGE_ROLES:
            raise InvalidInput(
                "message role must be system, developer, user, assistant, or tool"
            )
        if isinstance(self.content, str):
            return
        if isinstance(self.content, Sequence):
            parts = tuple(self.content)
            for part in parts:
                if not isinstance(part, (ResponsesTextPart, ResponsesImagePart)):
                    raise InvalidInput("message content parts must be ResponsesTextPart or ResponsesImagePart")
            object.__setattr__(self, "content", parts)
            return
        raise InvalidInput("message content must be a string or sequence of content parts")

    def to_payload(self) -> dict[str, object]:
        if isinstance(self.content, str):
            content_value: object = self.content
        else:
            content_value = [part.to_payload() for part in self.content]
        return {"type": "message", "role": self.role, "content": content_value}


@dataclass(frozen=True)
class ResponsesFunctionCallInput:
    """Replay an assistant function call item inside ``input``."""

    call_id: str
    name: str
    arguments: str = "{}"

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise InvalidInput("function_call.call_id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise InvalidInput("function_call.name must be a non-empty string")
        if not isinstance(self.arguments, str):
            raise InvalidInput("function_call.arguments must be a JSON string")

    def to_payload(self) -> dict[str, object]:
        return {
            "type": "function_call",
            "call_id": self.call_id,
            "name": self.name,
            "arguments": self.arguments,
        }


@dataclass(frozen=True)
class ResponsesFunctionCallOutputInput:
    """Submit a tool execution result back to ``/v1/responses``."""

    call_id: str
    output: str

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise InvalidInput("function_call_output.call_id must be a non-empty string")
        if not isinstance(self.output, str):
            raise InvalidInput("function_call_output.output must be a string")

    def to_payload(self) -> dict[str, object]:
        return {
            "type": "function_call_output",
            "call_id": self.call_id,
            "output": self.output,
        }


ResponsesInputItem = (
    ResponsesMessageInput
    | ResponsesFunctionCallInput
    | ResponsesFunctionCallOutputInput
    | Mapping[str, object]
)


@dataclass(frozen=True)
class ResponsesToolDefinition:
    """Function tool declaration serialized in flat OpenAI Responses API format."""

    name: str
    description: str = ""
    parameters: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
            raise InvalidInput("tool name must be 1 to 128 characters of [A-Za-z0-9_.-]")
        if not isinstance(self.description, str):
            raise InvalidInput("tool description must be a string")
        if self.parameters is not None:
            if not isinstance(self.parameters, Mapping):
                raise InvalidInput("tool parameters must be a JSON Schema mapping")
            object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"type": "function", "name": self.name}
        if self.description:
            payload["description"] = self.description
        if self.parameters is not None:
            payload["parameters"] = dict(self.parameters)
        return payload


@dataclass(frozen=True)
class ResponsesNamedToolChoice:
    """Force the model to invoke one named function tool."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
            raise InvalidInput("tool_choice function name must be 1 to 128 characters of [A-Za-z0-9_.-]")

    def to_payload(self) -> dict[str, object]:
        return {"type": "function", "name": self.name}


ResponsesToolChoice = ToolChoiceKeyword | ResponsesNamedToolChoice | Mapping[str, object]


@dataclass(frozen=True)
class ResponsesTextFormat:
    """Structured output constraint under ``text.format``."""

    format_type: TextFormatType = "text"
    name: str | None = None
    schema: Mapping[str, object] | None = None
    strict: bool | None = None

    def __post_init__(self) -> None:
        if self.format_type not in ("text", "json_object", "json_schema"):
            raise InvalidInput(
                "text.format.type must be 'text', 'json_object', or 'json_schema'"
            )
        if self.format_type == "json_schema":
            if self.schema is None or not isinstance(self.schema, Mapping):
                raise InvalidInput("text.format.schema must be a JSON Schema object for json_schema")
            object.__setattr__(self, "schema", MappingProxyType(dict(self.schema)))
        elif self.schema is not None:
            raise InvalidInput("text.format.schema is only valid when format_type is 'json_schema'")
        if self.name is not None and (not isinstance(self.name, str) or not self.name):
            raise InvalidInput("text.format.name must be a non-empty string")
        if self.strict is not None and not isinstance(self.strict, bool):
            raise InvalidInput("text.format.strict must be a boolean")

    def to_payload(self) -> dict[str, object]:
        fmt: dict[str, object] = {"type": self.format_type}
        if self.name is not None:
            fmt["name"] = self.name
        if self.schema is not None:
            fmt["schema"] = dict(self.schema)
        if self.strict is not None:
            fmt["strict"] = self.strict
        return {"format": fmt}


def validate_priority_value(value: object, *, where: str = "X-Sonder-Priority") -> str | int:
    """Validate ``X-Sonder-Priority``: class name or integer in ``[-16, 16]``."""
    if isinstance(value, str):
        if value in VALID_PRIORITY_CLASSES:
            return value
        raise InvalidInput(
            "%s must be an integer from %d to %d or interactive, subagent, or background"
            % (where, MIN_PRIORITY_INT, MAX_PRIORITY_INT)
        )
    if _is_strict_int(value):
        int_val = cast(int, value)
        if MIN_PRIORITY_INT <= int_val <= MAX_PRIORITY_INT:
            return int_val
        raise InvalidInput(
            "%s must be an integer from %d to %d" % (where, MIN_PRIORITY_INT, MAX_PRIORITY_INT)
        )
    raise InvalidInput(
        "%s must be an integer from %d to %d or interactive, subagent, or background"
        % (where, MIN_PRIORITY_INT, MAX_PRIORITY_INT)
    )


def validate_workload_value(value: object, *, where: str = "X-Sonder-Workload") -> str:
    """Validate ``X-Sonder-Workload`` against the 7 Inference workload classes."""
    if isinstance(value, str) and value in VALID_WORKLOADS:
        return value
    raise InvalidInput(
        "%s must be one of %s" % (where, ", ".join(sorted(VALID_WORKLOADS)))
    )


def validate_deadline_ms(value: object, *, where: str = "deadline_ms") -> int:
    """Validate positive 32-bit millisecond deadline."""
    if _is_strict_int(value):
        int_val = cast(int, value)
        if 1 <= int_val <= MAX_DEADLINE_MS:
            return int_val
    raise InvalidInput("%s must be a positive integer in [1, %d]" % (where, MAX_DEADLINE_MS))


def validate_correlation_id(value: object, *, where: str) -> str:
    if isinstance(value, str) and _CORRELATION_VALUE.fullmatch(value):
        return value
    raise InvalidInput("%s must match [A-Za-z0-9._:-]{1,128}" % where)


@dataclass(frozen=True)
class ResponsesCorrelationHeaders:
    """Validated Sonder Inference correlation and scheduling headers."""

    run_id: str | None = None
    parent_request_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    workload: str | None = None
    priority: str | int | None = None
    deadline_ms: int | None = None
    reasoning_budget: int | None = None

    def __post_init__(self) -> None:
        if self.run_id is not None:
            validate_correlation_id(self.run_id, where="X-Sonder-Run-Id")
        if self.parent_request_id is not None:
            validate_correlation_id(self.parent_request_id, where="X-Sonder-Parent-Request-Id")
        if self.agent_id is not None:
            validate_correlation_id(self.agent_id, where="X-Sonder-Agent-Id")
        if self.task_id is not None:
            validate_correlation_id(self.task_id, where="X-Sonder-Task-Id")
        if self.workload is not None:
            validate_workload_value(self.workload)
        if self.priority is not None:
            validate_priority_value(self.priority)
        if self.deadline_ms is not None:
            validate_deadline_ms(self.deadline_ms, where="X-Sonder-Deadline-Ms")
        if self.reasoning_budget is not None:
            if not _is_strict_int(self.reasoning_budget) or self.reasoning_budget < -1:
                raise InvalidInput("X-Sonder-Reasoning-Budget must be an integer >= -1")

    @classmethod
    def from_operation_context(cls, context: OperationContext) -> "ResponsesCorrelationHeaders":
        """Derive correlation, workload, and priority headers from *context*."""
        ambient = current_operation_context()
        operation = (
            ambient
            if (
                not context.session_id
                and ambient is not None
                and ambient.principal_id == context.principal_id
            )
            else context
        )
        run_id: str | None = None
        parent_id: str | None = None
        if isinstance(context.correlation_id, str) and _CORRELATION_VALUE.fullmatch(
            context.correlation_id
        ):
            run_id = context.correlation_id
            parent_id = context.correlation_id
        workload = WORKLOAD_BY_SOURCE.get(context.source)
        priority: str | None = PRIORITY_BY_SOURCE.get(operation.source)
        agent_id: str | None = None
        session = _conversation_key(operation)
        if session and (
            operation.source == "worker"
            or session.startswith(("standalone-", "repl-work-"))
        ):
            agent_id = session
            priority = "subagent"
        return cls(
            run_id=run_id,
            parent_request_id=parent_id,
            agent_id=agent_id,
            workload=workload,
            priority=priority,
        )

    def to_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.run_id is not None:
            headers["X-Sonder-Run-Id"] = self.run_id
        if self.parent_request_id is not None:
            headers["X-Sonder-Parent-Request-Id"] = self.parent_request_id
        if self.agent_id is not None:
            headers["X-Sonder-Agent-Id"] = self.agent_id
        if self.task_id is not None:
            headers["X-Sonder-Task-Id"] = self.task_id
        if self.workload is not None:
            headers["X-Sonder-Workload"] = self.workload
        if self.priority is not None:
            headers["X-Sonder-Priority"] = str(self.priority)
        if self.deadline_ms is not None:
            headers["X-Sonder-Deadline-Ms"] = str(self.deadline_ms)
        if self.reasoning_budget is not None:
            headers["X-Sonder-Reasoning-Budget"] = str(self.reasoning_budget)
        return headers


def _conversation_key(operation: OperationContext) -> str | None:
    session = operation.session_id
    if isinstance(session, str) and _CORRELATION_VALUE.fullmatch(session):
        return session
    correlation = operation.correlation_id
    if (
        isinstance(correlation, str)
        and correlation.startswith(("standalone-", "repl-work-"))
        and _CORRELATION_VALUE.fullmatch(correlation)
    ):
        return correlation
    return None


@dataclass(frozen=True)
class ResponsesRequest:
    """Typed request for ``POST /v1/responses``."""

    input: str | Sequence[ResponsesInputItem]
    model: str | None = None
    instructions: str | None = None
    previous_response_id: str | None = None
    tools: Sequence[ResponsesToolDefinition | Mapping[str, object]] = ()
    tool_choice: ResponsesToolChoice | None = None
    text_format: ResponsesTextFormat | None = None
    reasoning_effort: ReasoningEffortValue | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    seed: int | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    include: Sequence[str] = ()
    stream: bool = False
    store: bool = True
    prompt_cache_key: str | None = None
    priority: str | int | None = None
    workload: str | None = None
    deadline_ms: int | None = None
    reasoning_budget: int | None = None
    correlation: ResponsesCorrelationHeaders | None = None


def _serialize_raw_input_item(
    item: Mapping[str, object], index: int, image_count: list[int]
) -> dict[str, object]:
    where = "input[%d]" % index
    raw_type = item.get("type")
    if raw_type is None:
        item_type = "message"
    elif isinstance(raw_type, str):
        item_type = raw_type
    else:
        raise InvalidInput("%s.type must be a string" % where)

    if item_type == "function_call_output":
        call_id = item.get("call_id")
        output = item.get("output")
        if not isinstance(call_id, str) or not call_id:
            raise InvalidInput("%s.call_id must be a non-empty string" % where)
        if not isinstance(output, str):
            raise InvalidInput("%s.output must be a string" % where)
        return {"type": "function_call_output", "call_id": call_id, "output": output}

    if item_type == "function_call":
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments", "{}")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise InvalidInput("%s requires non-empty string call_id and name" % where)
        if arguments is None:
            arguments = "{}"
        if not isinstance(arguments, str):
            raise InvalidInput("%s.arguments must be a string" % where)
        return {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        }

    if item_type != "message":
        raise InvalidInput("%s.type %r is not supported" % (where, item_type))

    role = item.get("role")
    if not isinstance(role, str) or role not in VALID_MESSAGE_ROLES:
        raise InvalidInput("%s.role must be system, developer, user, assistant, or tool" % where)
    content = item.get("content")
    if isinstance(content, str):
        return {"type": "message", "role": role, "content": content}
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)):
        raise InvalidInput("%s.content must be a string or array of content parts" % where)
    parts_out: list[dict[str, object]] = []
    for part_idx, part in enumerate(content):
        pwhere = "%s.content[%d]" % (where, part_idx)
        if isinstance(part, ResponsesTextPart):
            parts_out.append(part.to_payload())
            continue
        if isinstance(part, ResponsesImagePart):
            image_count[0] += 1
            if image_count[0] > MAX_IMAGES_PER_REQUEST:
                raise InvalidInput("request exceeds maximum of %d images" % MAX_IMAGES_PER_REQUEST)
            parts_out.append(part.to_payload())
            continue
        if not isinstance(part, Mapping):
            raise InvalidInput("%s must be an object" % pwhere)
        ptype = part.get("type")
        if not isinstance(ptype, str):
            raise InvalidInput("%s.type must be a string" % pwhere)
        if ptype in VALID_TEXT_PART_TYPES:
            text_val = part.get("text")
            if not isinstance(text_val, str):
                raise InvalidInput("%s.text must be a string" % pwhere)
            parts_out.append({"type": ptype, "text": text_val})
        elif ptype in VALID_IMAGE_PART_TYPES:
            raw_iu = part.get("image_url")
            url_str = ""
            if isinstance(raw_iu, str):
                url_str = raw_iu
            elif isinstance(raw_iu, Mapping) and isinstance(raw_iu.get("url"), str):
                url_str = cast(str, raw_iu["url"])
            validate_data_image_uri(url_str, where="%s.image_url" % pwhere)
            image_count[0] += 1
            if image_count[0] > MAX_IMAGES_PER_REQUEST:
                raise InvalidInput("request exceeds maximum of %d images" % MAX_IMAGES_PER_REQUEST)
            if isinstance(raw_iu, Mapping):
                parts_out.append({"type": ptype, "image_url": {"url": url_str}})
            else:
                parts_out.append({"type": ptype, "image_url": url_str})
        else:
            raise InvalidInput("%s.type %r is not supported" % (pwhere, ptype))
    return {"type": "message", "role": role, "content": parts_out}


def _serialize_input(raw_input: str | Sequence[ResponsesInputItem]) -> str | list[dict[str, object]]:
    if isinstance(raw_input, str):
        return raw_input
    if not isinstance(raw_input, Sequence) or isinstance(raw_input, (bytes, bytearray)):
        raise InvalidInput("input must be a string or a non-empty sequence of input items")
    if len(raw_input) == 0:
        raise InvalidInput("input must be a string or a non-empty sequence of input items")
    image_count = [0]
    items_out: list[dict[str, object]] = []
    for index, item in enumerate(raw_input):
        if isinstance(item, ResponsesMessageInput):
            if not isinstance(item.content, str):
                for part in item.content:
                    if isinstance(part, ResponsesImagePart):
                        image_count[0] += 1
                        if image_count[0] > MAX_IMAGES_PER_REQUEST:
                            raise InvalidInput(
                                "request exceeds maximum of %d images" % MAX_IMAGES_PER_REQUEST
                            )
            items_out.append(item.to_payload())
        elif isinstance(item, (ResponsesFunctionCallInput, ResponsesFunctionCallOutputInput)):
            items_out.append(item.to_payload())
        elif isinstance(item, Mapping):
            items_out.append(_serialize_raw_input_item(item, index, image_count))
        else:
            raise InvalidInput("input[%d] must be a typed input item or mapping" % index)
    return items_out


def _serialize_tools(
    tools: Sequence[ResponsesToolDefinition | Mapping[str, object]],
) -> tuple[list[dict[str, object]], set[str]]:
    if len(tools) > MAX_TOOLS_PER_REQUEST:
        raise InvalidInput("tools exceeds maximum of %d definitions" % MAX_TOOLS_PER_REQUEST)
    out: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for index, tool in enumerate(tools):
        where = "tools[%d]" % index
        if isinstance(tool, ResponsesToolDefinition):
            if tool.name in seen_names:
                raise InvalidInput("duplicate tool name %r" % tool.name)
            seen_names.add(tool.name)
            out.append(tool.to_payload())
            continue
        if not isinstance(tool, Mapping):
            raise InvalidInput("%s must be a tool definition object" % where)
        if tool.get("type") != "function":
            raise InvalidInput("%s.type must be 'function'" % where)
        nested_fn = tool.get("function")
        if nested_fn is not None:
            if not isinstance(nested_fn, Mapping):
                raise InvalidInput("%s.function must be an object" % where)
            spec = nested_fn
            prefix = "%s.function" % where
        else:
            spec = tool
            prefix = where
        name = spec.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise InvalidInput("%s.name must be 1 to 128 characters of [A-Za-z0-9_.-]" % prefix)
        if name in seen_names:
            raise InvalidInput("duplicate tool name %r" % name)
        seen_names.add(name)
        entry: dict[str, object] = {"type": "function", "name": name}
        desc = spec.get("description")
        if desc is not None:
            if not isinstance(desc, str):
                raise InvalidInput("%s.description must be a string" % prefix)
            entry["description"] = desc
        params = spec.get("parameters")
        if params is not None:
            if not isinstance(params, Mapping):
                raise InvalidInput("%s.parameters must be a JSON Schema object" % prefix)
            entry["parameters"] = dict(params)
        out.append(entry)
    return out, seen_names


def _serialize_tool_choice(
    choice: ResponsesToolChoice,
    defined_names: set[str],
) -> str | dict[str, object]:
    if isinstance(choice, str):
        if choice in ("auto", "none"):
            return choice
        if choice == "required":
            if not defined_names:
                raise InvalidInput("tool_choice 'required' requires at least one tool")
            return choice
        raise InvalidInput(
            "tool_choice must be 'auto', 'none', 'required', or a function object"
        )
    if isinstance(choice, ResponsesNamedToolChoice):
        if choice.name not in defined_names:
            raise InvalidInput(
                "tool_choice function %r is not defined in tools" % choice.name
            )
        return choice.to_payload()
    if isinstance(choice, Mapping):
        if choice.get("type") != "function":
            raise InvalidInput("tool_choice.type must be 'function'")
        nested_fn = choice.get("function")
        if nested_fn is not None:
            if not isinstance(nested_fn, Mapping):
                raise InvalidInput("tool_choice.function must be an object")
            name = nested_fn.get("name")
        else:
            name = choice.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidInput("tool_choice function name must be a non-empty string")
        if name not in defined_names:
            raise InvalidInput("tool_choice function %r is not defined in tools" % name)
        return {"type": "function", "name": name}
    raise InvalidInput("tool_choice must be a string, ResponsesNamedToolChoice, or mapping")


def build_request_payload(
    request: ResponsesRequest,
    *,
    default_model: str = DEFAULT_MODEL,
) -> dict[str, object]:
    """Validate *request* and serialize it to the exact ``/v1/responses`` JSON payload."""
    model = default_model if request.model is None else request.model
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
        raise InvalidInput("model must be 1-128 non-space characters")

    payload: dict[str, object] = {
        "model": model,
        "input": _serialize_input(request.input),
    }

    if request.instructions is not None:
        if not isinstance(request.instructions, str):
            raise InvalidInput("instructions must be a string")
        payload["instructions"] = request.instructions

    if request.previous_response_id is not None:
        prev_id = request.previous_response_id
        if (
            not isinstance(prev_id, str)
            or not prev_id
            or len(prev_id.encode("utf-8")) > MAX_PREVIOUS_RESPONSE_ID_BYTES
        ):
            raise InvalidInput(
                "previous_response_id must be a non-empty string of at most %d bytes"
                % MAX_PREVIOUS_RESPONSE_ID_BYTES
            )
        payload["previous_response_id"] = prev_id

    if not isinstance(request.stream, bool):
        raise InvalidInput("stream must be a boolean")
    if request.stream:
        payload["stream"] = True

    if not isinstance(request.store, bool):
        raise InvalidInput("store must be a boolean")
    if not request.store:
        payload["store"] = False

    if request.prompt_cache_key is not None:
        cache_key = request.prompt_cache_key
        if (
            not isinstance(cache_key, str)
            or not cache_key
            or len(cache_key.encode("utf-8")) > MAX_PROMPT_CACHE_KEY_BYTES
        ):
            raise InvalidInput(
                "prompt_cache_key must be a string of 1 to %d bytes"
                % MAX_PROMPT_CACHE_KEY_BYTES
            )
        payload["prompt_cache_key"] = cache_key

    defined_tools: set[str] = set()
    if request.tools:
        serialized_tools, defined_tools = _serialize_tools(request.tools)
        payload["tools"] = serialized_tools

    if request.tool_choice is not None:
        payload["tool_choice"] = _serialize_tool_choice(request.tool_choice, defined_tools)

    if request.text_format is not None:
        if not isinstance(request.text_format, ResponsesTextFormat):
            raise InvalidInput("text_format must be a ResponsesTextFormat")
        payload["text"] = request.text_format.to_payload()

    if request.reasoning_effort is not None:
        if request.reasoning_effort not in VALID_REASONING_EFFORTS:
            raise InvalidInput(
                "reasoning_effort must be none, off, low, medium, high, or xhigh"
            )
        payload["reasoning"] = {"effort": request.reasoning_effort}

    if request.priority is not None:
        validated_priority = validate_priority_value(request.priority, where="priority")
        # Server body `priority` accepts class names only; numeric priorities go
        # exclusively in the X-Sonder-Priority header.
        if isinstance(validated_priority, str):
            payload["priority"] = validated_priority

    if request.deadline_ms is not None:
        payload["deadline_ms"] = validate_deadline_ms(request.deadline_ms)

    if request.temperature is not None:
        if (
            not _is_strict_number(request.temperature)
            or not math.isfinite(float(request.temperature))
            or not (0.0 <= float(request.temperature) <= MAX_TEMPERATURE)
        ):
            raise InvalidInput("temperature must be a finite number in [0, 10]")
        payload["temperature"] = float(request.temperature)

    if request.top_p is not None:
        if (
            not _is_strict_number(request.top_p)
            or not math.isfinite(float(request.top_p))
            or not (0.0 < float(request.top_p) <= 1.0)
        ):
            raise InvalidInput("top_p must be a finite number in (0, 1]")
        payload["top_p"] = float(request.top_p)

    if request.max_output_tokens is not None:
        if (
            not _is_strict_int(request.max_output_tokens)
            or not (1 <= request.max_output_tokens <= MAX_OUTPUT_TOKENS_LIMIT)
        ):
            raise InvalidInput(
                "max_output_tokens must be an integer in [1, %d]" % MAX_OUTPUT_TOKENS_LIMIT
            )
        payload["max_output_tokens"] = request.max_output_tokens

    if request.seed is not None:
        if not _is_strict_int(request.seed) or request.seed < 0:
            raise InvalidInput("seed must be a non-negative integer")
        payload["seed"] = request.seed

    include_list: list[str] = []
    if request.include:
        for entry in request.include:
            if not isinstance(entry, str) or not entry:
                raise InvalidInput("include entries must be non-empty strings")
            if entry not in include_list:
                include_list.append(entry)

    if request.logprobs is not None and not isinstance(request.logprobs, bool):
        raise InvalidInput("logprobs must be a boolean")
    if request.top_logprobs is not None:
        if not _is_strict_int(request.top_logprobs) or not (
            0 <= request.top_logprobs <= MAX_TOP_LOGPROBS
        ):
            raise InvalidInput("top_logprobs must be an integer in [0, %d]" % MAX_TOP_LOGPROBS)
        if request.logprobs is False:
            raise InvalidInput("top_logprobs requires logprobs to be True")
        if request.top_logprobs > 0 and request.logprobs is not True:
            raise InvalidInput("top_logprobs > 0 requires logprobs=True")

    if request.logprobs is True:
        payload["logprobs"] = True
        if "message.output_text.logprobs" not in include_list:
            include_list.append("message.output_text.logprobs")
    elif request.logprobs is False:
        payload["logprobs"] = False

    if request.top_logprobs is not None:
        payload["top_logprobs"] = request.top_logprobs

    if include_list:
        payload["include"] = include_list

    return payload


def build_request_headers(
    request: ResponsesRequest,
    *,
    config: SonderResponsesConfig,
    context: OperationContext | None = None,
) -> dict[str, str]:
    """Build HTTP headers including auth, correlation, workload, and ``X-Sonder-Priority``."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if request.stream:
        headers["Accept"] = "text/event-stream"
    if config.api_key:
        headers["Authorization"] = "Bearer %s" % config.api_key

    base_corr = (
        ResponsesCorrelationHeaders.from_operation_context(context)
        if context is not None
        else ResponsesCorrelationHeaders()
    )
    req_corr = request.correlation
    merged = ResponsesCorrelationHeaders(
        run_id=(
            req_corr.run_id
            if req_corr is not None and req_corr.run_id is not None
            else base_corr.run_id
        ),
        parent_request_id=(
            req_corr.parent_request_id
            if req_corr is not None and req_corr.parent_request_id is not None
            else base_corr.parent_request_id
        ),
        agent_id=(
            req_corr.agent_id
            if req_corr is not None and req_corr.agent_id is not None
            else base_corr.agent_id
        ),
        task_id=(
            req_corr.task_id
            if req_corr is not None and req_corr.task_id is not None
            else base_corr.task_id
        ),
        workload=(
            request.workload
            if request.workload is not None
            else (
                req_corr.workload
                if req_corr is not None and req_corr.workload is not None
                else base_corr.workload
            )
        ),
        priority=(
            request.priority
            if request.priority is not None
            else (
                req_corr.priority
                if req_corr is not None and req_corr.priority is not None
                else base_corr.priority
            )
        ),
        deadline_ms=(
            request.deadline_ms
            if request.deadline_ms is not None
            else (
                req_corr.deadline_ms
                if req_corr is not None and req_corr.deadline_ms is not None
                else base_corr.deadline_ms
            )
        ),
        reasoning_budget=(
            request.reasoning_budget
            if request.reasoning_budget is not None
            else (
                req_corr.reasoning_budget
                if req_corr is not None and req_corr.reasoning_budget is not None
                else base_corr.reasoning_budget
            )
        ),
    )
    headers.update(merged.to_headers())
    return headers


# -- Response & streaming event schema types ----------------------------------


@dataclass(frozen=True)
class TokenLogprobCandidate:
    """One candidate token in a ``top_logprobs`` list."""

    token: str
    logprob: float
    bytes: tuple[int, ...] = ()


@dataclass(frozen=True)
class TokenLogprob:
    """Per-token log-probability entry from response output or streaming deltas."""

    token: str
    logprob: float
    bytes: tuple[int, ...] = ()
    top_logprobs: tuple[TokenLogprobCandidate, ...] = ()


@dataclass(frozen=True)
class ResponsesFunctionCall:
    """One ``function_call`` item emitted in ``response.output``."""

    id: str
    call_id: str
    name: str
    arguments: str = "{}"
    status: str = "completed"

    def parsed_arguments(self) -> dict[str, object]:
        """Parse ``arguments`` as a JSON object, raising :class:`InvalidInput` otherwise."""
        try:
            parsed: object = json.loads(self.arguments)
        except (ValueError, RecursionError) as exc:
            raise InvalidInput(
                "function_call %r arguments are not valid JSON" % self.name
            ) from exc
        if not isinstance(parsed, dict):
            raise InvalidInput(
                "function_call %r arguments must decode to a JSON object" % self.name
            )
        return dict(parsed)


@dataclass(frozen=True)
class ResponsesUsage:
    """Token counts returned on a ``/v1/responses`` document."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None


@dataclass(frozen=True)
class ResponsesSonderMetadata:
    """Additive ``sonder`` telemetry/version metadata on every Inference response."""

    api_version: int | None = None
    request_id: str | None = None
    session_id: str | None = None
    session_key: str | None = None
    backend: str | None = None
    synthetic: bool | None = None
    token_counts_from_backend: bool | None = None
    warnings: tuple[str, ...] = ()


_JsonMapping = Mapping[str, object]


@dataclass(frozen=True)
class ResponsesResult:
    """Parsed immutable ``/v1/responses`` response object."""

    id: str
    object: str
    created_at: int | None
    status: str
    model: str
    previous_response_id: str | None
    output_text: str
    reasoning: str
    function_calls: tuple[ResponsesFunctionCall, ...]
    output: tuple[_JsonMapping, ...]
    usage: ResponsesUsage
    logprobs: tuple[TokenLogprob, ...]
    sonder: ResponsesSonderMetadata

    @property
    def has_function_calls(self) -> bool:
        return bool(self.function_calls)

    @property
    def is_incomplete(self) -> bool:
        return self.status == "incomplete"


@dataclass(frozen=True)
class ResponsesStreamEvent:
    """One typed SSE event from ``POST /v1/responses`` with ``stream=True``."""

    event_type: str
    delta: str = ""
    item_id: str | None = None
    output_index: int | None = None
    content_index: int | None = None
    call_id: str | None = None
    name: str | None = None
    logprobs: tuple[TokenLogprob, ...] = ()
    response: ResponsesResult | None = None
    raw: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


# -- Response & SSE parsing ---------------------------------------------------


def _parse_int_bytes(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[int] = []
    for item in raw:
        if _is_strict_int(item):
            int_val = cast(int, item)
            if 0 <= int_val <= 255:
                out.append(int_val)
    return tuple(out)


def _parse_token_logprob_candidate(raw: object) -> TokenLogprobCandidate | None:
    if not isinstance(raw, Mapping):
        return None
    token = raw.get("token")
    logprob = raw.get("logprob")
    if not isinstance(token, str) or not _is_strict_number(logprob):
        return None
    lp_float = float(cast(int | float, logprob))
    if not math.isfinite(lp_float):
        return None
    return TokenLogprobCandidate(
        token=token,
        logprob=lp_float,
        bytes=_parse_int_bytes(raw.get("bytes")),
    )


def _parse_token_logprobs(raw: object) -> tuple[TokenLogprob, ...]:
    items: object = raw
    if isinstance(raw, Mapping):
        items = raw.get("content")
    if not isinstance(items, list):
        return ()
    parsed: list[TokenLogprob] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        token = entry.get("token")
        logprob = entry.get("logprob")
        if not isinstance(token, str) or not _is_strict_number(logprob):
            continue
        lp_float = float(cast(int | float, logprob))
        if not math.isfinite(lp_float):
            continue
        raw_top = entry.get("top_logprobs")
        candidates: list[TokenLogprobCandidate] = []
        if isinstance(raw_top, list):
            for cand in raw_top:
                parsed_cand = _parse_token_logprob_candidate(cand)
                if parsed_cand is not None:
                    candidates.append(parsed_cand)
        parsed.append(
            TokenLogprob(
                token=token,
                logprob=lp_float,
                bytes=_parse_int_bytes(entry.get("bytes")),
                top_logprobs=tuple(candidates),
            )
        )
    return tuple(parsed)


def _parse_usage(raw: object) -> ResponsesUsage:
    if not isinstance(raw, Mapping):
        return ResponsesUsage()
    raw_in = raw.get("input_tokens")
    raw_out = raw.get("output_tokens")
    raw_tot = raw.get("total_tokens")
    in_tok = cast(int, raw_in) if _is_strict_int(raw_in) and cast(int, raw_in) >= 0 else 0
    out_tok = cast(int, raw_out) if _is_strict_int(raw_out) and cast(int, raw_out) >= 0 else 0
    tot_tok = (
        cast(int, raw_tot)
        if _is_strict_int(raw_tot) and cast(int, raw_tot) >= 0
        else in_tok + out_tok
    )
    cached: int | None = None
    details = raw.get("input_tokens_details")
    if isinstance(details, Mapping):
        raw_cached = details.get("cached_tokens")
        if _is_strict_int(raw_cached) and cast(int, raw_cached) >= 0:
            cached = cast(int, raw_cached)
    return ResponsesUsage(
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=tot_tok,
        cached_tokens=cached,
    )


def _parse_sonder_metadata(raw: object) -> ResponsesSonderMetadata:
    if not isinstance(raw, Mapping):
        return ResponsesSonderMetadata()
    api_version_raw = raw.get("api_version")
    api_version: int | None = None
    if api_version_raw is not None:
        if not _is_strict_int(api_version_raw):
            raise DependencyUnavailable(
                "sonder-inference returned a non-integer sonder.api_version (%r)"
                % (api_version_raw,)
            )
        api_version = cast(int, api_version_raw)
        if api_version != API_VERSION:
            raise DependencyUnavailable(
                "incompatible sonder-inference API version %d (this client speaks %d)"
                % (api_version, API_VERSION)
            )
    request_id = raw.get("request_id")
    session_id = raw.get("session_id")
    session_key = raw.get("session_key")
    backend = raw.get("backend")
    synthetic = raw.get("synthetic")
    counts_from_be = raw.get("token_counts_from_backend")
    raw_warnings = raw.get("warnings")
    warnings: tuple[str, ...] = ()
    if isinstance(raw_warnings, list):
        warnings = tuple(w for w in raw_warnings if isinstance(w, str))
    return ResponsesSonderMetadata(
        api_version=api_version,
        request_id=request_id if isinstance(request_id, str) else None,
        session_id=session_id if isinstance(session_id, str) else None,
        session_key=session_key if isinstance(session_key, str) else None,
        backend=backend if isinstance(backend, str) else None,
        synthetic=synthetic if isinstance(synthetic, bool) else None,
        token_counts_from_backend=counts_from_be if isinstance(counts_from_be, bool) else None,
        warnings=warnings,
    )


def parse_response_document(document: object) -> ResponsesResult:
    """Validate and parse a ``/v1/responses`` JSON object into :class:`ResponsesResult`."""
    if not isinstance(document, Mapping):
        raise DependencyUnavailable("sonder-inference /v1/responses returned a non-object JSON payload")

    resp_id = document.get("id")
    obj_kind = document.get("object")
    status = document.get("status")
    model = document.get("model")
    if not isinstance(resp_id, str) or not resp_id:
        raise DependencyUnavailable("sonder-inference /v1/responses payload missing string 'id'")
    if obj_kind is not None and obj_kind != "response":
        raise DependencyUnavailable(
            "sonder-inference /v1/responses returned unexpected object type %r" % (obj_kind,)
        )
    if not isinstance(status, str) or not status:
        raise DependencyUnavailable("sonder-inference /v1/responses payload missing string 'status'")
    if not isinstance(model, str) or not model:
        raise DependencyUnavailable("sonder-inference /v1/responses payload missing string 'model'")

    sonder = _parse_sonder_metadata(document.get("sonder"))

    raw_created = document.get("created_at")
    created_at = cast(int, raw_created) if _is_strict_int(raw_created) else None

    raw_prev = document.get("previous_response_id")
    if raw_prev is not None and not isinstance(raw_prev, str):
        raise DependencyUnavailable("sonder-inference /v1/responses has invalid previous_response_id")
    previous_response_id: str | None = raw_prev

    raw_output = document.get("output")
    output_items: list[Mapping[str, object]] = []
    reasoning_parts: list[str] = []
    message_text_parts: list[str] = []
    function_calls: list[ResponsesFunctionCall] = []
    collected_logprobs: list[TokenLogprob] = []

    if isinstance(raw_output, list):
        for item in raw_output:
            if not isinstance(item, Mapping):
                continue
            frozen_item = MappingProxyType(dict(item))
            output_items.append(frozen_item)
            item_type = item.get("type")
            if item_type == "reasoning":
                summary = item.get("summary")
                if isinstance(summary, list):
                    for part in summary:
                        if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                            reasoning_parts.append(cast(str, part["text"]))
            elif item_type == "message":
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, Mapping):
                            continue
                        if part.get("type") == "output_text":
                            text_val = part.get("text")
                            if isinstance(text_val, str):
                                message_text_parts.append(text_val)
                            part_lps = _parse_token_logprobs(part.get("logprobs"))
                            if part_lps:
                                collected_logprobs.extend(part_lps)
            elif item_type == "function_call":
                fc_id = item.get("id")
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments")
                fc_status = item.get("status")
                if isinstance(name, str) and name:
                    function_calls.append(
                        ResponsesFunctionCall(
                            id=fc_id if isinstance(fc_id, str) else "",
                            call_id=call_id if isinstance(call_id, str) else "",
                            name=name,
                            arguments=arguments if isinstance(arguments, str) else "{}",
                            status=fc_status if isinstance(fc_status, str) else "completed",
                        )
                    )
    elif raw_output is not None:
        raise DependencyUnavailable("sonder-inference /v1/responses 'output' must be a list")

    if not collected_logprobs:
        top_lps = _parse_token_logprobs(document.get("logprobs"))
        if top_lps:
            collected_logprobs.extend(top_lps)

    raw_output_text = document.get("output_text")
    if isinstance(raw_output_text, str):
        output_text = raw_output_text
    elif raw_output_text is None:
        output_text = "".join(message_text_parts)
    else:
        raise DependencyUnavailable("sonder-inference /v1/responses 'output_text' must be a string")

    return ResponsesResult(
        id=resp_id,
        object="response",
        created_at=created_at,
        status=status,
        model=model,
        previous_response_id=previous_response_id,
        output_text=output_text,
        reasoning="".join(reasoning_parts),
        function_calls=tuple(function_calls),
        output=tuple(output_items),
        usage=_parse_usage(document.get("usage")),
        logprobs=tuple(collected_logprobs),
        sonder=sonder,
    )


def _parse_sse_frame(event_name: str, data_text: str) -> ResponsesStreamEvent:
    try:
        parsed_obj: object = json.loads(data_text)
    except (ValueError, RecursionError) as exc:
        raise DependencyUnavailable(
            "sonder-inference /v1/responses sent malformed SSE JSON"
        ) from exc
    if not isinstance(parsed_obj, Mapping):
        raise DependencyUnavailable(
            "sonder-inference /v1/responses SSE event payload must be a JSON object"
        )

    effective_type = (
        cast(str, parsed_obj["type"])
        if isinstance(parsed_obj.get("type"), str) and parsed_obj["type"]
        else event_name
    )
    if effective_type == "error" or ("error" in parsed_obj and "response" not in parsed_obj):
        code, message = _error_document_fields(parsed_obj)
        raise DependencyUnavailable(
            "sonder-inference /v1/responses failed while streaming (%s)%s"
            % (code or "error", ": %s" % message if message else "")
        )

    if effective_type == "response.failed":
        resp_obj = parsed_obj.get("response")
        code, message = _error_document_fields(resp_obj if isinstance(resp_obj, Mapping) else parsed_obj)
        raise DependencyUnavailable(
            "sonder-inference /v1/responses reported response.failed (%s)%s"
            % (code or "failed", ": %s" % message if message else "")
        )

    raw_frozen = MappingProxyType(dict(parsed_obj))
    parsed_response: ResponsesResult | None = None
    if effective_type in ("response.created", "response.completed", "response.incomplete"):
        resp_obj = parsed_obj.get("response")
        if resp_obj is None:
            raise DependencyUnavailable(
                "sonder-inference /v1/responses %s event missing 'response'" % effective_type
            )
        parsed_response = parse_response_document(resp_obj)

    delta_raw = parsed_obj.get("delta")
    delta = delta_raw if isinstance(delta_raw, str) else ""
    item_id = parsed_obj.get("item_id")
    output_index = parsed_obj.get("output_index")
    content_index = parsed_obj.get("content_index")
    call_id = parsed_obj.get("call_id")
    name = parsed_obj.get("name")
    logprobs = _parse_token_logprobs(parsed_obj.get("logprobs"))

    return ResponsesStreamEvent(
        event_type=effective_type or "message",
        delta=delta,
        item_id=item_id if isinstance(item_id, str) else None,
        output_index=cast(int, output_index) if _is_strict_int(output_index) else None,
        content_index=cast(int, content_index) if _is_strict_int(content_index) else None,
        call_id=call_id if isinstance(call_id, str) else None,
        name=name if isinstance(name, str) else None,
        logprobs=logprobs,
        response=parsed_response,
        raw=raw_frozen,
    )


def iter_sse_events(
    stream: _ReadableBinaryStream,
    *,
    max_bytes: int = RESPONSE_BODY_LIMIT,
    max_line_bytes: int = MAX_SSE_LINE_BYTES,
    cancellation: object | None = None,
) -> Iterator[ResponsesStreamEvent]:
    """Incrementally parse bounded named SSE frames from ``POST /v1/responses``."""
    event_name = ""
    data_lines: list[str] = []
    total_bytes = 0

    while True:
        if _cancelled_quietly(cancellation):
            raise Cancelled("sonder-inference /v1/responses stream cancelled")
        raw = stream.readline(max_line_bytes + 1)
        if not raw:
            break
        total_bytes += len(raw)
        if total_bytes > max_bytes:
            raise DependencyUnavailable(
                "sonder-inference /v1/responses stream exceeds %d bytes" % max_bytes
            )
        if len(raw) > max_line_bytes:
            raise DependencyUnavailable(
                "sonder-inference /v1/responses SSE line exceeds %d bytes" % max_line_bytes
            )
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise DependencyUnavailable(
                "sonder-inference /v1/responses stream contains invalid UTF-8"
            ) from exc

        if not line:
            if data_lines:
                event = _parse_sse_frame(event_name, "\n".join(data_lines))
                event_name = ""
                data_lines = []
                yield event
                if event.event_type in ("response.completed", "response.incomplete"):
                    return
            else:
                event_name = ""
            continue
        if line.startswith(":"):
            continue  # SSE comment / keep-alive
        field_name, _sep, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            event_name = value
        elif field_name == "data":
            data_lines.append(value)

    if data_lines:
        event = _parse_sse_frame(event_name, "\n".join(data_lines))
        yield event


# -- HTTP error classification ------------------------------------------------


def _parse_error_envelope(body: bytes) -> tuple[str, str, str]:
    """Return ``(code, message, param)`` from a bounded Inference error body."""
    if not body:
        return "", "", ""
    try:
        document: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "", "", ""
    if not isinstance(document, Mapping):
        return "", "", ""
    code, message = _error_document_fields(document, bound_message=True)
    error_obj = document.get("error")
    param = ""
    if isinstance(error_obj, Mapping) and isinstance(error_obj.get("param"), str):
        param = cast(str, error_obj["param"])
    return code, message, param


def _classify_http_error(
    status: int,
    body: bytes,
    *,
    previous_response_id: str | None = None,
) -> SonderError:
    code, message, param = _parse_error_envelope(body)
    label = "HTTP %d%s" % (status, " %s" % code if code else "")
    suffix = ": %s" % message if message else ""

    if status in (401, 403):
        if code in ("forbidden_host", "forbidden_origin"):
            return Forbidden(
                "sonder-inference refused the request's %s (%s); the base URL host "
                "must be 127.0.0.1, localhost, or [::1]%s"
                % ("Host" if code == "forbidden_host" else "Origin", label, suffix)
            )
        return Forbidden("sonder-inference rejected credentials (%s)%s" % (label, suffix))

    if status == 404:
        if param == "previous_response_id" or (
            code == "not_found" and previous_response_id is not None
        ):
            return PreviousResponseNotFound(
                "sonder-inference did not find previous_response_id (%s)%s" % (label, suffix),
                previous_response_id=previous_response_id,
            )
        return NotFound("sonder-inference resource not found (%s)%s" % (label, suffix))

    if status in (408, 504):
        return DeadlineExceeded("sonder-inference deadline exceeded (%s)%s" % (label, suffix))

    if status == 429 or (status == 503 and code == "overloaded"):
        return CapacityExceeded("sonder-inference is overloaded (%s)%s" % (label, suffix))

    if status in (400, 405, 411, 413, 417, 431, 501):
        return InvalidInput("sonder-inference rejected the request (%s)%s" % (label, suffix))

    return DependencyUnavailable("sonder-inference failed the request (%s)%s" % (label, suffix))


def _classify_url_error(
    exc: urllib.error.URLError,
    *,
    display_url: str,
    budget_seconds: float,
) -> SonderError:
    reason: object = exc.reason
    if isinstance(reason, ssl.SSLError):
        return DependencyUnavailable(
            "sonder-inference TLS verification failed for %s: %s"
            % (display_url, _bounded(reason))
        )
    if isinstance(reason, ConnectionRefusedError):
        return DependencyUnavailable(
            "sonder-inference at %s is not reachable (connection refused)" % display_url
        )
    if isinstance(reason, socket.gaierror):
        return DependencyUnavailable(
            "sonder-inference at %s is not reachable (host name does not resolve)" % display_url
        )
    if isinstance(reason, TimeoutError):
        return DeadlineExceeded(
            "sonder-inference /v1/responses exchange exceeded its %.1fs budget"
            % budget_seconds
        )
    return DependencyUnavailable(
        "sonder-inference at %s transport error: %s" % (display_url, _bounded(reason))
    )


# -- Client & stateful chain session ------------------------------------------


OpenerFactory = Callable[[_ExchangeBudget], urllib.request.OpenerDirector]


def _default_opener(budget: _ExchangeBudget) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        _BudgetedHTTPHandler(budget),
        _BudgetedHTTPSHandler(budget),
    )


def _combine_cancellation(
    context: OperationContext | None,
    cancellation: object | None,
) -> object | None:
    parts: list[object] = []
    ambient = _ACTIVE_CANCELLATION.get()
    if ambient is not None:
        parts.append(ambient)
    if context is not None and context.cancellation is not None:
        parts.append(context.cancellation)
    if cancellation is not None:
        parts.append(cancellation)
    if not parts:
        return None
    combined: object = parts[0]
    for extra in parts[1:]:
        combined = _CompositeCancellation(combined, extra)
    return combined


def _compute_budget_seconds(
    config: SonderResponsesConfig,
    context: OperationContext | None,
) -> float:
    budget = config.timeout_seconds
    if context is not None:
        remaining = context.remaining_seconds
        if remaining is not None:
            if remaining <= 0.0:
                raise DeadlineExceeded(
                    "operation context deadline already expired before /v1/responses send"
                )
            budget = min(budget, remaining)
    return budget


class SonderResponsesClient:
    """Strictly-typed HTTP client for Sonder Inference's ``POST /v1/responses``."""

    def __init__(
        self,
        config: SonderResponsesConfig | None = None,
        *,
        opener_factory: OpenerFactory | None = None,
    ) -> None:
        self._config = config if config is not None else SonderResponsesConfig()
        self._opener_factory: OpenerFactory = (
            opener_factory if opener_factory is not None else _default_opener
        )

    @property
    def config(self) -> SonderResponsesConfig:
        return self._config

    def _prepare_exchange(
        self,
        request: ResponsesRequest,
        *,
        context: OperationContext | None,
        cancellation: object | None,
    ) -> tuple[urllib.request.Request, float, object | None]:
        validate_endpoint_policy(self._config, context=context)
        combined_cancel = _combine_cancellation(context, cancellation)
        if _cancelled_quietly(combined_cancel):
            raise Cancelled("sonder-inference /v1/responses exchange cancelled before send")
        budget_seconds = _compute_budget_seconds(self._config, context)
        payload = build_request_payload(request, default_model=self._config.model)
        headers = build_request_headers(request, config=self._config, context=context)
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        http_request = urllib.request.Request(
            self._config.endpoint_url,
            data=data,
            headers=headers,
            method="POST",
        )
        return http_request, budget_seconds, combined_cancel

    def _decode_json_response(self, body: bytes) -> ResponsesResult:
        if len(body) > self._config.max_response_bytes:
            raise DependencyUnavailable(
                "sonder-inference /v1/responses body exceeds %d bytes"
                % self._config.max_response_bytes
            )
        try:
            document: object = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise DependencyUnavailable(
                "sonder-inference /v1/responses returned a non-JSON response"
            ) from exc
        return parse_response_document(document)

    def _create_non_streaming(
        self,
        http_request: urllib.request.Request,
        *,
        budget_seconds: float,
        combined_cancel: object | None,
        previous_response_id: str | None,
    ) -> ResponsesResult:
        malformed: str | None = None
        expired_cause: Exception | None = None
        aborted_cause: Exception | None = None
        url_error: urllib.error.URLError | None = None
        http_error_pair: tuple[int, bytes] | None = None
        body = b""

        with (
            trust_scope(self._config.ca_bundle),
            _cancellation_scope(combined_cancel),
            _ExchangeBudget(budget_seconds) as budget,
        ):
            opener = self._opener_factory(budget)
            try:
                try:
                    response = cast(
                        _ReadableHTTPResponse,
                        opener.open(http_request, timeout=budget_seconds),
                    )
                    with response:
                        body = _read_bounded(response, self._config.max_response_bytes)
                except urllib.error.HTTPError as exc:
                    status = int(exc.code)
                    try:
                        err_body = _read_bounded(exc, ERROR_BODY_LIMIT)
                    finally:
                        exc.close()
                    http_error_pair = (status, err_body)
            except Exception as exc:  # noqa: BLE001 - mapped to SonderError below
                if budget.aborted:
                    aborted_cause = exc
                elif isinstance(exc, http.client.HTTPException):
                    malformed = type(exc).__name__
                elif budget.expired:
                    expired_cause = exc
                elif isinstance(exc, urllib.error.URLError):
                    url_error = exc
                elif isinstance(exc, TimeoutError):
                    expired_cause = exc
                elif isinstance(exc, OSError):
                    url_error = urllib.error.URLError(exc)
                else:
                    raise

            if budget.aborted:
                if isinstance(aborted_cause, http.client.HTTPException):
                    aborted_cause = None
                raise Cancelled(
                    "sonder-inference /v1/responses exchange cancelled"
                ) from aborted_cause
            if malformed is not None:
                if budget.expired:
                    raise DeadlineExceeded(
                        "sonder-inference /v1/responses exchange exceeded its %.1fs budget"
                        % budget_seconds
                    )
                raise DependencyUnavailable(
                    "sonder-inference /v1/responses sent a malformed HTTP response (%s)"
                    % malformed
                )
            if expired_cause is not None or budget.expired:
                raise DeadlineExceeded(
                    "sonder-inference /v1/responses exchange exceeded its %.1fs budget"
                    % budget_seconds
                ) from expired_cause
            if url_error is not None:
                raise _classify_url_error(
                    url_error,
                    display_url=self._config.display_base_url,
                    budget_seconds=budget_seconds,
                ) from None

        if http_error_pair is not None:
            raise _classify_http_error(
                http_error_pair[0],
                http_error_pair[1],
                previous_response_id=previous_response_id,
            )
        return self._decode_json_response(body)

    def stream(
        self,
        request: ResponsesRequest,
        *,
        context: OperationContext | None = None,
        cancellation: object | None = None,
    ) -> Generator[ResponsesStreamEvent, None, None]:
        """Stream typed SSE events from ``POST /v1/responses`` (forces ``stream=True``)."""
        stream_req = request if request.stream else replace(request, stream=True)
        http_request, budget_seconds, combined_cancel = self._prepare_exchange(
            stream_req, context=context, cancellation=cancellation,
        )
        malformed: str | None = None
        expired_cause: Exception | None = None
        aborted_cause: Exception | None = None
        url_error: urllib.error.URLError | None = None
        http_error_pair: tuple[int, bytes] | None = None
        saw_terminal = False
        started = time.monotonic()

        with (
            trust_scope(self._config.ca_bundle),
            _cancellation_scope(combined_cancel),
            _ExchangeBudget(budget_seconds) as budget,
        ):
            opener = self._opener_factory(budget)
            try:
                try:
                    response = cast(
                        _ReadableHTTPResponse,
                        opener.open(http_request, timeout=budget_seconds),
                    )
                except urllib.error.HTTPError as exc:
                    status = int(exc.code)
                    try:
                        err_body = _read_bounded(exc, ERROR_BODY_LIMIT)
                    finally:
                        exc.close()
                    http_error_pair = (status, err_body)
                else:
                    with response:
                        content_type = str(response.headers.get("Content-Type", "")).lower()
                        if "text/event-stream" not in content_type:
                            body = _read_bounded(response, self._config.max_response_bytes)
                            result = self._decode_json_response(body)
                            saw_terminal = True
                            yield ResponsesStreamEvent(
                                event_type="response.completed",
                                response=result,
                            )
                        else:
                            for event in iter_sse_events(
                                response,
                                max_bytes=self._config.max_response_bytes,
                                max_line_bytes=self._config.max_sse_line_bytes,
                                cancellation=combined_cancel,
                            ):
                                if event.event_type in ("response.completed", "response.incomplete"):
                                    saw_terminal = True
                                yield event
            except SonderError:
                if budget.aborted:
                    raise Cancelled(
                        "sonder-inference /v1/responses stream cancelled"
                    ) from None
                if budget.expired:
                    raise DeadlineExceeded(
                        "sonder-inference /v1/responses stream exceeded its %.1fs budget "
                        "(elapsed %.1fs)" % (budget_seconds, time.monotonic() - started)
                    ) from None
                raise
            except Exception as exc:  # noqa: BLE001 - mapped to SonderError below
                if budget.aborted:
                    aborted_cause = exc
                elif isinstance(exc, http.client.HTTPException):
                    malformed = type(exc).__name__
                elif budget.expired:
                    expired_cause = exc
                elif isinstance(exc, urllib.error.URLError):
                    url_error = exc
                elif isinstance(exc, TimeoutError):
                    expired_cause = exc
                elif isinstance(exc, OSError):
                    url_error = urllib.error.URLError(exc)
                else:
                    raise

            if budget.aborted:
                if isinstance(aborted_cause, http.client.HTTPException):
                    aborted_cause = None
                raise Cancelled(
                    "sonder-inference /v1/responses stream cancelled"
                ) from aborted_cause
            if malformed is not None:
                if budget.expired:
                    raise DeadlineExceeded(
                        "sonder-inference /v1/responses stream exceeded its %.1fs budget"
                        % budget_seconds
                    )
                raise DependencyUnavailable(
                    "sonder-inference /v1/responses sent a malformed HTTP response (%s)"
                    % malformed
                )
            if expired_cause is not None or budget.expired:
                raise DeadlineExceeded(
                    "sonder-inference /v1/responses stream exceeded its %.1fs budget"
                    % budget_seconds
                ) from expired_cause
            if url_error is not None:
                raise _classify_url_error(
                    url_error,
                    display_url=self._config.display_base_url,
                    budget_seconds=budget_seconds,
                ) from None

        if http_error_pair is not None:
            raise _classify_http_error(
                http_error_pair[0],
                http_error_pair[1],
                previous_response_id=stream_req.previous_response_id,
            )
        if not saw_terminal:
            raise DependencyUnavailable(
                "sonder-inference /v1/responses stream ended before response.completed"
            )

    def create(
        self,
        request: ResponsesRequest,
        *,
        context: OperationContext | None = None,
        cancellation: object | None = None,
        on_event: Callable[[ResponsesStreamEvent], object] | None = None,
        on_text_delta: Callable[[str], object] | None = None,
        on_function_call_delta: Callable[[ResponsesStreamEvent], object] | None = None,
    ) -> ResponsesResult:
        """Execute ``POST /v1/responses`` (non-streaming or streaming with callbacks).

        When ``request.stream`` is True, callbacks fire incrementally as deltas
        arrive.  Returning ``False`` from any callback cancels the stream
        immediately and raises :class:`Cancelled`.
        """
        if not request.stream:
            http_request, budget_seconds, combined_cancel = self._prepare_exchange(
                request, context=context, cancellation=cancellation,
            )
            return self._create_non_streaming(
                http_request,
                budget_seconds=budget_seconds,
                combined_cancel=combined_cancel,
                previous_response_id=request.previous_response_id,
            )

        callback_cancel = threading.Event()
        effective_cancel: object = (
            _CompositeCancellation(cancellation, callback_cancel)
            if cancellation is not None
            else callback_cancel
        )
        final_result: ResponsesResult | None = None
        streamed_logprobs: list[TokenLogprob] = []

        gen = self.stream(request, context=context, cancellation=effective_cancel)
        try:
            for event in gen:
                if event.logprobs:
                    streamed_logprobs.extend(event.logprobs)
                if on_event is not None and on_event(event) is False:
                    callback_cancel.set()
                    raise Cancelled("streaming callback cancelled /v1/responses request")
                if (
                    event.event_type == "response.output_text.delta"
                    and event.delta
                    and on_text_delta is not None
                    and on_text_delta(event.delta) is False
                ):
                    callback_cancel.set()
                    raise Cancelled("text-delta callback cancelled /v1/responses request")
                if (
                    event.event_type == "response.function_call_arguments.delta"
                    and on_function_call_delta is not None
                    and on_function_call_delta(event) is False
                ):
                    callback_cancel.set()
                    raise Cancelled("function-call callback cancelled /v1/responses request")
                if (
                    event.event_type in ("response.completed", "response.incomplete")
                    and event.response is not None
                ):
                    final_result = event.response
        finally:
            gen.close()

        if final_result is None:
            raise DependencyUnavailable(
                "sonder-inference /v1/responses stream ended without a completed response"
            )
        if not final_result.logprobs and streamed_logprobs:
            final_result = replace(final_result, logprobs=tuple(streamed_logprobs))
        return final_result

    def start_session(
        self,
        *,
        model: str | None = None,
        instructions: str | None = None,
        tools: Sequence[ResponsesToolDefinition | Mapping[str, object]] = (),
        tool_choice: ResponsesToolChoice | None = None,
        text_format: ResponsesTextFormat | None = None,
        reasoning_effort: ReasoningEffortValue | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        logprobs: bool | None = None,
        top_logprobs: int | None = None,
        prompt_cache_key: str | None = None,
        priority: str | int | None = None,
        workload: str | None = None,
        deadline_ms: int | None = None,
        reasoning_budget: int | None = None,
        correlation: ResponsesCorrelationHeaders | None = None,
    ) -> "SonderResponsesSession":
        """Create a stateful multi-turn ``previous_response_id`` session."""
        return SonderResponsesSession(
            self,
            model=model,
            instructions=instructions,
            tools=tools,
            tool_choice=tool_choice,
            text_format=text_format,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            logprobs=logprobs,
            top_logprobs=top_logprobs,
            prompt_cache_key=prompt_cache_key,
            priority=priority,
            workload=workload,
            deadline_ms=deadline_ms,
            reasoning_budget=reasoning_budget,
            correlation=correlation,
        )


class SonderResponsesSession:
    """Stateful ``previous_response_id`` conversation chain over :class:`SonderResponsesClient`."""

    def __init__(
        self,
        client: SonderResponsesClient,
        *,
        model: str | None = None,
        instructions: str | None = None,
        tools: Sequence[ResponsesToolDefinition | Mapping[str, object]] = (),
        tool_choice: ResponsesToolChoice | None = None,
        text_format: ResponsesTextFormat | None = None,
        reasoning_effort: ReasoningEffortValue | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        logprobs: bool | None = None,
        top_logprobs: int | None = None,
        prompt_cache_key: str | None = None,
        priority: str | int | None = None,
        workload: str | None = None,
        deadline_ms: int | None = None,
        reasoning_budget: int | None = None,
        correlation: ResponsesCorrelationHeaders | None = None,
        initial_response_id: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._instructions = instructions
        self._tools: tuple[ResponsesToolDefinition | Mapping[str, object], ...] = tuple(tools)
        self._tool_choice = tool_choice
        self._text_format = text_format
        self._reasoning_effort = reasoning_effort
        self._temperature = temperature
        self._top_p = top_p
        self._max_output_tokens = max_output_tokens
        self._logprobs = logprobs
        self._top_logprobs = top_logprobs
        self._prompt_cache_key = prompt_cache_key
        self._priority = priority
        self._workload = workload
        self._deadline_ms = deadline_ms
        self._reasoning_budget = reasoning_budget
        self._correlation = correlation
        self._lock = threading.Lock()
        self._last_response_id: str | None = initial_response_id
        self._session_key: str | None = prompt_cache_key
        self._history: list[ResponsesResult] = []

    @property
    def last_response_id(self) -> str | None:
        with self._lock:
            return self._last_response_id

    @property
    def session_key(self) -> str | None:
        with self._lock:
            return self._session_key

    @property
    def history(self) -> tuple[ResponsesResult, ...]:
        with self._lock:
            return tuple(self._history)

    def reset(self) -> None:
        """Clear retained chain state (for example after ``PreviousResponseNotFound``)."""
        with self._lock:
            self._last_response_id = None
            self._session_key = self._prompt_cache_key
            self._history.clear()

    def send(
        self,
        input: str | Sequence[ResponsesInputItem],
        *,
        instructions: str | None = None,
        tools: Sequence[ResponsesToolDefinition | Mapping[str, object]] | None = None,
        tool_choice: ResponsesToolChoice | None = None,
        stream: bool = False,
        store: bool = True,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        priority: str | int | None = None,
        workload: str | None = None,
        deadline_ms: int | None = None,
        context: OperationContext | None = None,
        cancellation: object | None = None,
        on_event: Callable[[ResponsesStreamEvent], object] | None = None,
        on_text_delta: Callable[[str], object] | None = None,
        on_function_call_delta: Callable[[ResponsesStreamEvent], object] | None = None,
    ) -> ResponsesResult:
        """Send the next turn chained to ``last_response_id``."""
        with self._lock:
            prev_id = self._last_response_id
        # Send default session instructions on the first turn, or explicit
        # per-turn instructions whenever supplied.
        effective_instructions = (
            instructions if instructions is not None else (self._instructions if prev_id is None else None)
        )
        effective_tools = self._tools if tools is None else tuple(tools)
        effective_tool_choice = self._tool_choice if tool_choice is None else tool_choice
        request = ResponsesRequest(
            input=input,
            model=self._model,
            instructions=effective_instructions,
            previous_response_id=prev_id,
            tools=effective_tools,
            tool_choice=effective_tool_choice,
            text_format=self._text_format,
            reasoning_effort=self._reasoning_effort,
            temperature=self._temperature if temperature is None else temperature,
            top_p=self._top_p,
            max_output_tokens=(
                self._max_output_tokens if max_output_tokens is None else max_output_tokens
            ),
            logprobs=self._logprobs,
            top_logprobs=self._top_logprobs,
            stream=stream,
            store=store,
            prompt_cache_key=self._prompt_cache_key if prev_id is None else None,
            priority=self._priority if priority is None else priority,
            workload=self._workload if workload is None else workload,
            deadline_ms=self._deadline_ms if deadline_ms is None else deadline_ms,
            reasoning_budget=self._reasoning_budget,
            correlation=self._correlation,
        )
        result = self._client.create(
            request,
            context=context,
            cancellation=cancellation,
            on_event=on_event,
            on_text_delta=on_text_delta,
            on_function_call_delta=on_function_call_delta,
        )
        with self._lock:
            self._history.append(result)
            if store and result.status == "completed":
                self._last_response_id = result.id
            if result.sonder.session_key:
                self._session_key = result.sonder.session_key
        return result

    def submit_tool_outputs(
        self,
        outputs: Sequence[ResponsesFunctionCallOutputInput | tuple[str, str]],
        *,
        stream: bool = False,
        store: bool = True,
        max_output_tokens: int | None = None,
        priority: str | int | None = None,
        workload: str | None = None,
        deadline_ms: int | None = None,
        context: OperationContext | None = None,
        cancellation: object | None = None,
        on_event: Callable[[ResponsesStreamEvent], object] | None = None,
        on_text_delta: Callable[[str], object] | None = None,
        on_function_call_delta: Callable[[ResponsesStreamEvent], object] | None = None,
    ) -> ResponsesResult:
        """Submit ``function_call_output`` items chained to the latest response."""
        items: list[ResponsesFunctionCallOutputInput] = []
        for entry in outputs:
            if isinstance(entry, ResponsesFunctionCallOutputInput):
                items.append(entry)
            elif isinstance(entry, tuple) and len(entry) == 2:
                items.append(ResponsesFunctionCallOutputInput(call_id=entry[0], output=entry[1]))
            else:
                raise InvalidInput(
                    "tool outputs must be ResponsesFunctionCallOutputInput or (call_id, output) pairs"
                )
        if not items:
            raise InvalidInput("submit_tool_outputs requires at least one tool output")
        return self.send(
            items,
            stream=stream,
            store=store,
            max_output_tokens=max_output_tokens,
            priority=priority,
            workload=workload,
            deadline_ms=deadline_ms,
            context=context,
            cancellation=cancellation,
            on_event=on_event,
            on_text_delta=on_text_delta,
            on_function_call_delta=on_function_call_delta,
        )


__all__ = [
    "ACCEPTED_LOOPBACK_HOSTS",
    "API_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "MAX_DEADLINE_MS",
    "MAX_IMAGES_PER_REQUEST",
    "MAX_IMAGE_DECODED_BYTES",
    "MAX_OUTPUT_TOKENS_LIMIT",
    "MAX_PREVIOUS_RESPONSE_ID_BYTES",
    "MAX_PRIORITY_INT",
    "MAX_PROMPT_CACHE_KEY_BYTES",
    "MAX_SSE_LINE_BYTES",
    "MAX_TOOLS_PER_REQUEST",
    "MAX_TOP_LOGPROBS",
    "MIN_PRIORITY_INT",
    "PRIORITY_BY_SOURCE",
    "PreviousResponseNotFound",
    "RESPONSES_PATH",
    "ResponsesContentPart",
    "ResponsesCorrelationHeaders",
    "ResponsesFunctionCall",
    "ResponsesFunctionCallInput",
    "ResponsesFunctionCallOutputInput",
    "ResponsesImagePart",
    "ResponsesInputItem",
    "ResponsesMessageInput",
    "ResponsesNamedToolChoice",
    "ResponsesRequest",
    "ResponsesResult",
    "ResponsesSonderMetadata",
    "ResponsesStreamEvent",
    "ResponsesTextFormat",
    "ResponsesTextPart",
    "ResponsesToolChoice",
    "ResponsesToolDefinition",
    "ResponsesUsage",
    "SUPPORTED_IMAGE_MIMES",
    "SonderResponsesClient",
    "SonderResponsesConfig",
    "SonderResponsesSession",
    "TokenLogprob",
    "TokenLogprobCandidate",
    "VALID_PRIORITY_CLASSES",
    "VALID_REASONING_EFFORTS",
    "VALID_WORKLOADS",
    "WORKLOAD_BY_SOURCE",
    "build_request_headers",
    "build_request_payload",
    "is_loopback_url",
    "iter_sse_events",
    "normalize_responses_base_url",
    "parse_response_document",
    "validate_correlation_id",
    "validate_data_image_uri",
    "validate_deadline_ms",
    "validate_endpoint_policy",
    "validate_priority_value",
    "validate_private_worker_url",
    "validate_workload_value",
]
