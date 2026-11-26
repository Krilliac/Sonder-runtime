"""Opt-in Sonder Inference protocol diagnostics and identity reading.

Mirrors ``OpenAICompatibleProtocolProbe``: only fixed JSON-shape requests
leave the host, provider text is never persisted, and the record stays
diagnostic (``synthetic=True``) because bounded protocol probes without a
granted production ``ToolGateway`` cannot certify host tool execution or
in-flight cancellation.

Capabilities are discovered from the server's ``/v1/sonder/health`` document:

* When the server advertises native ``tool_calling`` capability, the probe
  runs bounded OpenAI ``tools`` / ``tool_choice`` conformance cases
  (``TOOL_NATIVE``, ``TOOLS_WITH_SCHEMA``, ``TOOL_SEQUENTIAL``,
  ``TOOL_CONTINUATION``, and optional ``TOOL_PARALLEL``) with strict
  fail-closed validation of returned ``tool_calls``.  When a server does not
  advertise ``tool_calling``, those cases stay ``unknown`` (``None``).
* ``STRUCTURED`` sends a strict ``response_format`` JSON Schema payload when
  ``structured_output`` is advertised, and falls back to the plain JSON
  prompt otherwise.
* ``/v1/embeddings``, ``structured_output``, and ``logprobs`` can be probed
  both via :meth:`SonderInferenceProtocolProbe.run` (recording
  ``BackendCapability.EMBEDDING`` and updating ``last_capability_probes``)
  and directly via :meth:`SonderInferenceProtocolProbe.probe_capabilities`
  and its per-capability helpers.

Identity comes from ``/v1/sonder/identity``: a *synthetic* identity -- the
mock backend -- is displayed by status surfaces but is refused here: it must
never be recorded as attestation evidence or satisfy identity-bound routing.
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from sonder_runtime.application.context import OperationContext, local_owner_context
from sonder_runtime.application.routing.backend_conformance import run_protocol_probes
from sonder_runtime.domain.common.errors import DeadlineExceeded
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendConformanceRecord,
    BackendIdentity,
    ProbeResult,
)

from .exchange_transport import trust_scope
from .request_tuning import advertised_features
from .sonder_inference_gateway import (
    IdentityObservation,
    SonderInferenceGateway,
)

_CASES = (
    BackendCapability.STRUCTURED,
    BackendCapability.TOOL_NATIVE,
    BackendCapability.TOOLS_WITH_SCHEMA,
    BackendCapability.TOOL_FALLBACK,
    BackendCapability.TOOL_SEQUENTIAL,
    BackendCapability.TOOL_CONTINUATION,
)
_TEXT_LIMIT = 4096
_MAX_PROBE_TOOL_CALLS = 8
_MAX_TOOL_ID_LENGTH = 128
_MAX_EMBEDDING_DIMS = 65_536
_MAX_LOGPROBS_TOKENS = 256
_MAX_TOP_LOGPROBS = 20
_MAX_TOKEN_BYTES = 256
_PROBE_TOP_LOGPROBS = 2
_EMBEDDING_PROBE_INPUT = "sonder conformance embedding"

_TOOL_CALLING_MARKERS = frozenset({"tool_calling", "tools", "tool_calls", "native_tools"})
_EMBEDDING_MARKERS = frozenset({"embeddings", "embedding"})
_STRUCTURED_OUTPUT_MARKERS = frozenset({"structured_output", "response_format", "json_schema"})
_LOGPROBS_MARKERS = frozenset({"logprobs", "token_logprobs"})


class SyntheticIdentityRefused(ValueError):
    """A mock/synthetic identity was offered where evidence is required."""


class IdentityUnavailable(ValueError):
    """Inference could not measure every identity field for the model."""


@dataclass(frozen=True)
class CapabilityProbeResult:
    """Bounded, content-free verdict for one Inference v1 capability probe."""

    capability: str
    advertised: bool
    supported: bool | None
    reason_code: str
    dimensions: int | None = None

    def __post_init__(self) -> None:
        if (type(self.capability) is not str or not 1 <= len(self.capability) <= 64
                or type(self.advertised) is not bool
                or (self.supported is not None and type(self.supported) is not bool)
                or type(self.reason_code) is not str
                or not 1 <= len(self.reason_code) <= 128
                or (self.dimensions is not None
                    and (type(self.dimensions) is not int or self.dimensions <= 0))):
            raise ValueError("invalid capability probe result")


@dataclass(frozen=True)
class _ParsedToolCall:
    call_id: str
    name: str
    arguments: dict[str, object]


def _reject_json_constant(token: str) -> object:
    raise ValueError("non-finite JSON constant %r is not allowed" % token)


def _strict_json_loads(raw: str) -> object:
    return json.loads(raw, parse_constant=_reject_json_constant)


def _is_finite_number(value: object) -> bool:
    return type(value) is not bool and isinstance(value, (int, float)) and math.isfinite(value)


def _valid_tool_call_id(value: object) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= _MAX_TOOL_ID_LENGTH:
        return None
    if value.strip() != value:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


def _echo_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo a fixed diagnostic value.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


def _structured_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "sonder_probe_echo",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
                "required": ["tool", "arguments"],
                "additionalProperties": False,
            },
        },
    }


def _echo_args_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "sonder_probe_echo_args",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


def _choice_message(
    response: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]] | None:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    return first, message


def _valid_utf8_bytes_list(raw: object) -> bool:
    if raw is None:
        return True
    if not isinstance(raw, list) or len(raw) > _MAX_TOKEN_BYTES:
        return False
    return all(type(item) is int and 0 <= item <= 255 for item in raw)


def _valid_logprob_entry(entry: object, *, require_top: bool) -> bool:
    if not isinstance(entry, Mapping):
        return False
    token = entry.get("token")
    if type(token) is not str or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        return False
    logprob = entry.get("logprob")
    if not _is_finite_number(logprob) or float(cast(int | float, logprob)) > 0.0:
        return False
    if not _valid_utf8_bytes_list(entry.get("bytes")):
        return False
    if not require_top:
        return True
    top_entries = entry.get("top_logprobs")
    if not isinstance(top_entries, list) or not 1 <= len(top_entries) <= _MAX_TOP_LOGPROBS:
        return False
    return all(_valid_logprob_entry(item, require_top=False) for item in top_entries)


def _extract_tool_calls(
    response: Mapping[str, object],
    *,
    allowed_names: frozenset[str],
) -> list[_ParsedToolCall] | None:
    pair = _choice_message(response)
    if pair is None:
        return None
    _choice, message = pair
    content = message.get("content")
    if isinstance(content, str) and len(content.encode("utf-8")) > _TEXT_LIMIT:
        return None
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and len(reasoning.encode("utf-8")) > _TEXT_LIMIT:
        return None
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list) or not 1 <= len(raw_calls) <= _MAX_PROBE_TOOL_CALLS:
        return None
    parsed_calls: list[_ParsedToolCall] = []
    seen_ids: set[str] = set()
    for entry in raw_calls:
        if not isinstance(entry, Mapping):
            return None
        call_id = _valid_tool_call_id(entry.get("id"))
        if call_id is None or call_id in seen_ids:
            return None
        if entry.get("type") != "function":
            return None
        func = entry.get("function")
        if not isinstance(func, Mapping):
            return None
        name = func.get("name")
        if type(name) is not str or name not in allowed_names:
            return None
        raw_args = func.get("arguments")
        if type(raw_args) is not str or len(raw_args.encode("utf-8")) > _TEXT_LIMIT:
            return None
        try:
            decoded = _strict_json_loads(raw_args)
        except (TypeError, ValueError, RecursionError):
            return None
        if type(decoded) is not dict:
            return None
        seen_ids.add(call_id)
        parsed_calls.append(_ParsedToolCall(call_id=call_id, name=name, arguments=decoded))
    return parsed_calls


def _single_echo_call(response: Mapping[str, object], expected_value: str) -> _ParsedToolCall | None:
    calls = _extract_tool_calls(response, allowed_names=frozenset({"echo"}))
    if calls is None or len(calls) != 1:
        return None
    call = calls[0]
    if call.name != "echo" or call.arguments != {"value": expected_value}:
        return None
    if type(call.arguments.get("value")) is not str:
        return None
    return call


def _validate_embeddings_response(
    response: Mapping[str, object],
    expected_model: str,
) -> tuple[bool, str, int | None]:
    if response.get("model") != expected_model:
        return False, "embeddings_model_mismatch", None
    rows = response.get("data")
    if not isinstance(rows, list) or len(rows) != 1:
        return False, "embeddings_invalid", None
    row = rows[0]
    if not isinstance(row, Mapping):
        return False, "embeddings_invalid", None
    index = row.get("index")
    if index is not None and (type(index) is not int or index != 0):
        return False, "embeddings_invalid", None
    vector = row.get("embedding")
    if not isinstance(vector, list) or not 1 <= len(vector) <= _MAX_EMBEDDING_DIMS:
        return False, "embeddings_invalid", None
    if not all(_is_finite_number(item) for item in vector):
        return False, "embeddings_invalid", None
    return True, "embeddings_passed", len(vector)


def _validate_logprobs_response(response: Mapping[str, object]) -> tuple[bool, str]:
    pair = _choice_message(response)
    if pair is None:
        return False, "logprobs_invalid"
    choice, message = pair
    content = message.get("content")
    if isinstance(content, str) and len(content.encode("utf-8")) > _TEXT_LIMIT:
        return False, "logprobs_invalid"
    raw_logprobs = choice.get("logprobs")
    if raw_logprobs is None:
        return False, "logprobs_missing"
    if not isinstance(raw_logprobs, Mapping):
        return False, "logprobs_invalid"
    entries = raw_logprobs.get("content")
    if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_LOGPROBS_TOKENS:
        return False, "logprobs_invalid"
    if not all(_valid_logprob_entry(item, require_top=True) for item in entries):
        return False, "logprobs_invalid"
    return True, "logprobs_passed"


def _assistant_echo_turn(call_id: str, value: str) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "echo",
                "arguments": json.dumps({"value": value}, separators=(",", ":"), allow_nan=False),
            },
        }],
    }


def _tool_result_turn(call_id: str, value: str) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "echo",
        "content": value,
    }


class SonderInferenceIdentityReader:
    """Callable returning the server-measured identity for one model.

    Raises :class:`SyntheticIdentityRefused` for synthetic identities and
    :class:`IdentityUnavailable` when Inference reported ``null`` (with its
    reason), so a caller can never mistake either for evidence.
    """

    def __init__(self, gateway: SonderInferenceGateway, *, model: str | None = None) -> None:
        if not isinstance(gateway, SonderInferenceGateway):
            raise TypeError("a SonderInferenceGateway is required")
        self._gateway = gateway
        self._model = model

    def observe(self) -> IdentityObservation:
        return self._gateway.observe_identity(self._model)

    def __call__(self) -> BackendIdentity:
        observation = self.observe()
        if observation.synthetic:
            raise SyntheticIdentityRefused(
                "sonder-inference reports a synthetic (mock) identity for %r; "
                "synthetic identities are never attestation evidence" % observation.model
            )
        if observation.identity is None:
            raise IdentityUnavailable(
                "sonder-inference cannot measure the identity of %r: %s"
                % (observation.model, observation.reason)
            )
        return observation.identity


class SonderInferenceProtocolProbe:
    """Report bounded, nonpromotable diagnostics for one Inference model."""

    def __init__(
        self, gateway: SonderInferenceGateway, *,
        identity_reader: Callable[[], BackendIdentity],
        cloud_allowed: bool = False,
    ) -> None:
        if type(gateway) is not SonderInferenceGateway or not callable(identity_reader):
            raise TypeError("concrete Sonder Inference gateway and identity reader required")
        self._gateway = gateway
        self._read_identity = identity_reader
        self._cloud_allowed = cloud_allowed
        self._identity: BackendIdentity | None = None
        self._identity_changed = False
        self._deadline = 0.0
        self._transport_at_start = gateway._transport
        self._base_url = ""
        self._advertised_caps: frozenset[str] | None = None
        self._last_capability_probes: dict[str, CapabilityProbeResult] = {}

    @property
    def last_capability_probes(self) -> Mapping[str, CapabilityProbeResult]:
        """Content-free results from the latest capability probe pass."""
        return MappingProxyType(dict(self._last_capability_probes))

    def _check_identity(self) -> BackendIdentity:
        current = self._read_identity()
        if (not isinstance(current, BackendIdentity) or current != self._identity
                or self._gateway._transport is not self._transport_at_start):
            self._identity_changed = True
            raise ValueError("backend identity or transport changed during protocol probe")
        return current

    def _prepare_context(self, timeout_seconds: float) -> tuple[BackendIdentity, OperationContext]:
        identity = self._check_identity()
        settings = self._gateway.settings()
        if settings.base_url != self._base_url:
            raise ValueError("configured endpoint changed during protocol probe")
        remaining = min(float(timeout_seconds), self._deadline - time.monotonic())
        if remaining <= 0:
            raise DeadlineExceeded("protocol probe wall budget exhausted")
        context = local_owner_context(
            correlation_id="backend-protocol-conformance", source="system",
            timeout_seconds=remaining, cloud_allowed=self._cloud_allowed,
        )
        config = self._gateway._resolved_config()
        self._gateway._enforce_consent(config, context)
        return identity, context

    def _discover_advertised_capabilities(self, timeout_seconds: float) -> frozenset[str]:
        if self._advertised_caps is not None:
            return self._advertised_caps
        _identity, context = self._prepare_context(timeout_seconds)
        remaining = self._gateway._check_liveness(context)
        budget = min(5.0, remaining if remaining is not None else float(timeout_seconds))
        snap = self._gateway.health(timeout=max(0.001, budget), refresh=True)
        self._gateway._check_liveness(context, phase="during protocol probe")
        self._check_identity()
        caps = (
            advertised_features(snap.document)
            if snap.state == "ready" and isinstance(snap.document, Mapping)
            else frozenset()
        )
        self._advertised_caps = caps
        return caps

    def _has_capability(self, markers: frozenset[str], *, timeout_seconds: float) -> bool:
        return bool(self._discover_advertised_capabilities(timeout_seconds) & markers)

    def _begin_session(self, timeout_seconds: float) -> BackendIdentity:
        if type(timeout_seconds) is bool or not 0.0 < float(timeout_seconds) <= 300.0:
            raise ValueError("timeout_seconds must be between 0 and 300")
        self._deadline = time.monotonic() + float(timeout_seconds)
        initial_context = local_owner_context(
            correlation_id="backend-protocol-conformance", source="system",
            timeout_seconds=float(timeout_seconds), cloud_allowed=self._cloud_allowed,
        )
        self._gateway._enforce_consent(self._gateway._resolved_config(), initial_context)
        self._identity = self._read_identity()
        if not isinstance(self._identity, BackendIdentity):
            raise TypeError("identity reader must return a BackendIdentity")
        self._identity_changed = False
        self._transport_at_start = self._gateway._transport
        self._base_url = self._gateway.settings().base_url
        self._advertised_caps = None
        self._discover_advertised_capabilities(timeout_seconds)
        return self._identity

    def _request(
        self,
        messages: list[dict[str, object]],
        *,
        timeout_seconds: float,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        identity, context = self._prepare_context(timeout_seconds)
        settings = self._gateway.settings()
        config = self._gateway._resolved_config()
        timeout = self._gateway._check_liveness(context)
        payload: dict[str, object] = {
            "model": identity.model,
            "messages": messages,
            "stream": False,
            "temperature": 0.0,
            "max_tokens": 128,
        }
        if extra:
            payload.update(extra)
        with trust_scope(settings.ca_bundle):
            response = self._gateway._post(
                "/v1/chat/completions",
                payload,
                config,
                timeout,
                context=context,
            )
        self._gateway._check_liveness(context, phase="during protocol probe")
        self._check_identity()
        if not isinstance(response, dict) or response.get("model") != identity.model:
            raise ValueError("provider response did not identify the probed model")
        return cast(dict[str, object], response)

    def _post_embeddings(self, *, timeout_seconds: float) -> dict[str, object]:
        identity, context = self._prepare_context(timeout_seconds)
        settings = self._gateway.settings()
        config = self._gateway._resolved_config()
        timeout = self._gateway._check_liveness(context)
        payload: dict[str, object] = {
            "model": identity.model,
            "input": [_EMBEDDING_PROBE_INPUT],
        }
        with trust_scope(settings.ca_bundle):
            response = self._gateway._post(
                "/v1/embeddings",
                payload,
                config,
                timeout,
                context=context,
            )
        self._gateway._check_liveness(context, phase="during protocol probe")
        self._check_identity()
        if not isinstance(response, dict):
            raise ValueError("provider embeddings response must be an object")
        return cast(dict[str, object], response)

    def _extract_bounded_text(self, response: Mapping[str, object]) -> str:
        text = self._gateway._extract_text(dict(response))
        if len(text.encode("utf-8")) > _TEXT_LIMIT:
            raise ValueError("protocol probe response exceeds the text bound")
        return text

    @staticmethod
    def _tool_call(text: str, expected_value: str) -> bool:
        try:
            value = _strict_json_loads(text)
        except (TypeError, ValueError, RecursionError):
            return False
        return type(value) is dict and value == {
            "tool": "echo", "arguments": {"value": expected_value},
        }

    @staticmethod
    def _ask(value: str) -> list[dict[str, object]]:
        return [{"role": "user", "content": (
            'Return only a JSON object with "tool":"echo" and '
            f'"arguments":{{"value":"{value}"}}. Do not return a final answer.'
        )}]

    def _run_tool_native_case(
        self,
        *,
        model: str,
        timeout_seconds: float,
        with_schema: bool = False,
    ) -> dict[str, object]:
        extra: dict[str, object] = {
            "tools": [_echo_tool()],
            "tool_choice": "auto",
        }
        if with_schema:
            extra["response_format"] = _echo_args_response_format()
        response = self._request(
            [{"role": "user", "content": 'Call the echo tool with value "alpha".'}],
            timeout_seconds=timeout_seconds,
            extra=extra,
        )
        valid = _single_echo_call(response, "alpha") is not None
        return {
            "model": model,
            "tool_calls": ["echo"] if valid else [],
            "schema_valid": valid,
        }

    def _run_tool_sequential_case(
        self,
        *,
        model: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        user_turn: dict[str, object] = {
            "role": "user",
            "content": (
                'Call echo twice in sequence: first with value "a", '
                'then after receiving the result call echo with value "b".'
            ),
        }
        tools_extra: dict[str, object] = {"tools": [_echo_tool()], "tool_choice": "auto"}
        first = self._request([user_turn], timeout_seconds=timeout_seconds, extra=tools_extra)
        call_a = _single_echo_call(first, "a")
        if call_a is None:
            return {"model": model, "events": []}
        events = ["call:a", "result:a"]
        second = self._request(
            [user_turn, _assistant_echo_turn(call_a.call_id, "a"), _tool_result_turn(call_a.call_id, "a")],
            timeout_seconds=timeout_seconds,
            extra=tools_extra,
        )
        if _single_echo_call(second, "b") is not None:
            events.extend(["call:b", "result:b"])
        return {"model": model, "events": events}

    def _run_tool_parallel_case(
        self,
        *,
        model: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        response = self._request(
            [{
                "role": "user",
                "content": (
                    'Call the echo tool twice in parallel in one turn: '
                    'once with value "a" and once with value "b".'
                ),
            }],
            timeout_seconds=timeout_seconds,
            extra={"tools": [_echo_tool()], "tool_choice": "auto"},
        )
        calls = _extract_tool_calls(response, allowed_names=frozenset({"echo"}))
        if calls is None or len(calls) != 2:
            return {"model": model, "events": [], "parallel_max": 0}
        values = {
            call.arguments.get("value")
            for call in calls
            if len(call.arguments) == 1 and type(call.arguments.get("value")) is str
        }
        if values != {"a", "b"}:
            return {"model": model, "events": [], "parallel_max": 0}
        return {
            "model": model,
            "events": ["call:a", "call:b", "result:a", "result:b"],
            "parallel_max": len(calls),
        }

    def _run_tool_continuation_case(
        self,
        *,
        model: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        user_turn: dict[str, object] = {
            "role": "user",
            "content": (
                'Call the echo tool with value "alpha", '
                "then reply with a short final answer once you receive the result."
            ),
        }
        tools_extra: dict[str, object] = {"tools": [_echo_tool()], "tool_choice": "auto"}
        first = self._request([user_turn], timeout_seconds=timeout_seconds, extra=tools_extra)
        call = _single_echo_call(first, "alpha")
        if call is None:
            return {"model": model, "events": []}
        events = ["call:echo", "result:echo"]
        final = self._request(
            [user_turn, _assistant_echo_turn(call.call_id, "alpha"), _tool_result_turn(call.call_id, "alpha")],
            timeout_seconds=timeout_seconds,
            extra=tools_extra,
        )
        pair = _choice_message(final)
        if pair is None:
            return {"model": model, "events": events}
        choice, message = pair
        if message.get("tool_calls") or choice.get("finish_reason") == "tool_calls":
            return {"model": model, "events": events}
        content = message.get("content")
        if type(content) is str and content.strip() and len(content.encode("utf-8")) <= _TEXT_LIMIT:
            events.append("assistant:done")
        return {"model": model, "events": events}

    def run_case(self, capability: BackendCapability, *, model: str,
                 timeout_seconds: float) -> dict[str, object] | None:
        if self._identity is None or model != self._identity.model:
            raise ValueError("probe model differs from the measured identity")
        if capability is BackendCapability.STRUCTURED:
            extra: Mapping[str, object] | None = (
                {"response_format": _structured_response_format()}
                if self._has_capability(_STRUCTURED_OUTPUT_MARKERS, timeout_seconds=timeout_seconds)
                else None
            )
            response = self._request(self._ask("alpha"), timeout_seconds=timeout_seconds, extra=extra)
            text = self._extract_bounded_text(response)
            return {
                "model": model,
                "schema_valid": self._tool_call(text, "alpha"),
                "response_kind": "object",
            }
        if capability is BackendCapability.TOOL_NATIVE:
            if not self._has_capability(_TOOL_CALLING_MARKERS, timeout_seconds=timeout_seconds):
                return None
            return self._run_tool_native_case(model=model, timeout_seconds=timeout_seconds)
        if capability is BackendCapability.TOOLS_WITH_SCHEMA:
            if not (self._has_capability(_TOOL_CALLING_MARKERS, timeout_seconds=timeout_seconds)
                    and self._has_capability(_STRUCTURED_OUTPUT_MARKERS, timeout_seconds=timeout_seconds)):
                return None
            return self._run_tool_native_case(
                model=model, timeout_seconds=timeout_seconds, with_schema=True,
            )
        if capability is BackendCapability.TOOL_SEQUENTIAL:
            if not self._has_capability(_TOOL_CALLING_MARKERS, timeout_seconds=timeout_seconds):
                return None
            return self._run_tool_sequential_case(model=model, timeout_seconds=timeout_seconds)
        if capability is BackendCapability.TOOL_PARALLEL:
            if not self._has_capability(_TOOL_CALLING_MARKERS, timeout_seconds=timeout_seconds):
                return None
            return self._run_tool_parallel_case(model=model, timeout_seconds=timeout_seconds)
        if capability is BackendCapability.TOOL_CONTINUATION:
            if not self._has_capability(_TOOL_CALLING_MARKERS, timeout_seconds=timeout_seconds):
                return None
            return self._run_tool_continuation_case(model=model, timeout_seconds=timeout_seconds)
        # TOOL_FALLBACK and other host-side dispatch protocols require a
        # granted production ToolGateway and remain unmeasured here.
        return None

    def _run_embeddings_probe(
        self, *, timeout_seconds: float, force: bool = False,
    ) -> CapabilityProbeResult:
        advertised = self._has_capability(_EMBEDDING_MARKERS, timeout_seconds=timeout_seconds)
        if not advertised and not force:
            return CapabilityProbeResult("embeddings", False, None, "embeddings_unadvertised")
        identity = self._identity
        if identity is None:
            raise ValueError("probe identity is not initialized")
        try:
            response = self._post_embeddings(timeout_seconds=timeout_seconds)
        except Exception:  # noqa: BLE001 - provider errors become content-free evidence
            if self._identity_changed:
                raise
            return CapabilityProbeResult("embeddings", advertised, False, "embeddings_error")
        ok, reason, dims = _validate_embeddings_response(response, identity.model)
        return CapabilityProbeResult("embeddings", advertised, ok, reason, dimensions=dims)

    def _run_structured_output_probe(
        self, *, timeout_seconds: float, force: bool = False,
    ) -> CapabilityProbeResult:
        advertised = self._has_capability(_STRUCTURED_OUTPUT_MARKERS, timeout_seconds=timeout_seconds)
        if not advertised and not force:
            return CapabilityProbeResult(
                "structured_output", False, None, "structured_output_unadvertised",
            )
        try:
            response = self._request(
                self._ask("alpha"),
                timeout_seconds=timeout_seconds,
                extra={"response_format": _structured_response_format()},
            )
            text = self._extract_bounded_text(response)
        except Exception:  # noqa: BLE001 - provider errors become content-free evidence
            if self._identity_changed:
                raise
            return CapabilityProbeResult(
                "structured_output", advertised, False, "structured_output_error",
            )
        valid = self._tool_call(text, "alpha")
        return CapabilityProbeResult(
            "structured_output",
            advertised,
            valid,
            "structured_output_passed" if valid else "structured_output_invalid",
        )

    def _run_logprobs_probe(
        self, *, timeout_seconds: float, force: bool = False,
    ) -> CapabilityProbeResult:
        advertised = self._has_capability(_LOGPROBS_MARKERS, timeout_seconds=timeout_seconds)
        if not advertised and not force:
            return CapabilityProbeResult("logprobs", False, None, "logprobs_unadvertised")
        try:
            response = self._request(
                [{"role": "user", "content": "Reply with one short word."}],
                timeout_seconds=timeout_seconds,
                extra={
                    "logprobs": True,
                    "top_logprobs": _PROBE_TOP_LOGPROBS,
                    "max_tokens": 16,
                },
            )
        except Exception:  # noqa: BLE001 - provider errors become content-free evidence
            if self._identity_changed:
                raise
            return CapabilityProbeResult("logprobs", advertised, False, "logprobs_error")
        ok, reason = _validate_logprobs_response(response)
        return CapabilityProbeResult("logprobs", advertised, ok, reason)

    def _run_tool_calling_probe(
        self, *, timeout_seconds: float, force: bool = False,
    ) -> CapabilityProbeResult:
        advertised = self._has_capability(_TOOL_CALLING_MARKERS, timeout_seconds=timeout_seconds)
        if not advertised and not force:
            return CapabilityProbeResult("tool_calling", False, None, "tool_calling_unadvertised")
        identity = self._identity
        if identity is None:
            raise ValueError("probe identity is not initialized")
        try:
            observed = self._run_tool_native_case(
                model=identity.model, timeout_seconds=timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - provider errors become content-free evidence
            if self._identity_changed:
                raise
            return CapabilityProbeResult("tool_calling", advertised, False, "tool_calling_error")
        passed = observed.get("tool_calls") == ["echo"] and observed.get("schema_valid") is True
        return CapabilityProbeResult(
            "tool_calling",
            advertised,
            passed,
            "tool_calling_passed" if passed else "tool_calling_invalid",
        )

    def probe_embeddings(
        self, *, timeout_seconds: float = 30.0, force: bool = False,
    ) -> CapabilityProbeResult:
        """Probe ``POST /v1/embeddings`` with bounded, identity-checked validation."""
        self._begin_session(timeout_seconds)
        result = self._run_embeddings_probe(timeout_seconds=timeout_seconds, force=force)
        self._check_identity()
        return result

    def probe_structured_output(
        self, *, timeout_seconds: float = 30.0, force: bool = False,
    ) -> CapabilityProbeResult:
        """Probe ``response_format`` JSON Schema enforcement on ``/v1/chat/completions``."""
        self._begin_session(timeout_seconds)
        result = self._run_structured_output_probe(timeout_seconds=timeout_seconds, force=force)
        self._check_identity()
        return result

    def probe_logprobs(
        self, *, timeout_seconds: float = 30.0, force: bool = False,
    ) -> CapabilityProbeResult:
        """Probe ``logprobs`` / ``top_logprobs`` on ``/v1/chat/completions``."""
        self._begin_session(timeout_seconds)
        result = self._run_logprobs_probe(timeout_seconds=timeout_seconds, force=force)
        self._check_identity()
        return result

    def probe_capabilities(
        self, *, timeout_seconds: float = 30.0, force: bool = False,
    ) -> dict[str, CapabilityProbeResult]:
        """Probe ``/v1/embeddings``, ``structured_output``, ``logprobs`` and ``tool_calling``."""
        self._begin_session(timeout_seconds)
        results = {
            "embeddings": self._run_embeddings_probe(timeout_seconds=timeout_seconds, force=force),
            "structured_output": self._run_structured_output_probe(
                timeout_seconds=timeout_seconds, force=force,
            ),
            "logprobs": self._run_logprobs_probe(timeout_seconds=timeout_seconds, force=force),
            "tool_calling": self._run_tool_calling_probe(
                timeout_seconds=timeout_seconds, force=force,
            ),
        }
        self._check_identity()
        if self._identity_changed:
            raise ValueError("backend identity changed during protocol probe")
        self._last_capability_probes = dict(results)
        return results

    def run(self, *, timeout_seconds: float = 30.0) -> BackendConformanceRecord:
        identity = self._begin_session(timeout_seconds)
        try:
            chat_response = self._request(
                [{"role": "user", "content": "Reply with a nonempty greeting."}],
                timeout_seconds=timeout_seconds,
            )
            chat = self._extract_bounded_text(chat_response)
            chat_result = ProbeResult(BackendCapability.CHAT, bool(chat.strip()),
                                      "plain_chat_passed" if chat.strip() else "plain_chat_empty")
        except Exception:  # noqa: BLE001 - provider errors become evidence, never leaked text
            if self._identity_changed:
                raise
            chat_result = ProbeResult(BackendCapability.CHAT, False, "plain_chat_error")
        cancellation = ProbeResult(
            BackendCapability.CANCELLATION, None, "inflight_cancellation_unmeasured",
        )
        protocol = run_protocol_probes(
            self, identity=identity, timeout_seconds=timeout_seconds, cases=_CASES,
        )
        if self._identity_changed:
            raise ValueError("backend identity changed during protocol probe")
        embedding_probe = self._run_embeddings_probe(timeout_seconds=timeout_seconds, force=False)
        embedding_result = ProbeResult(
            BackendCapability.EMBEDDING, embedding_probe.supported, embedding_probe.reason_code,
        )
        logprobs_probe = self._run_logprobs_probe(timeout_seconds=timeout_seconds, force=False)
        protocol_by_cap = {item.capability: item for item in protocol.results}
        structured_adv = self._has_capability(
            _STRUCTURED_OUTPUT_MARKERS, timeout_seconds=timeout_seconds,
        )
        structured_item = protocol_by_cap.get(BackendCapability.STRUCTURED)
        structured_reason = (
            "structured_output_passed"
            if structured_item is not None and structured_item.passed is True
            else (
                "structured_output_error"
                if structured_item is not None and structured_item.reason_code == "live_protocol_error"
                else "structured_output_invalid"
            )
        )
        structured_probe = (
            CapabilityProbeResult(
                "structured_output",
                True,
                structured_item.passed if structured_item is not None else False,
                structured_reason,
            )
            if structured_adv
            else CapabilityProbeResult(
                "structured_output", False, None, "structured_output_unadvertised",
            )
        )
        tools_adv = self._has_capability(_TOOL_CALLING_MARKERS, timeout_seconds=timeout_seconds)
        tool_item = protocol_by_cap.get(BackendCapability.TOOL_NATIVE)
        tool_reason = (
            "tool_calling_passed"
            if tool_item is not None and tool_item.passed is True
            else (
                "tool_calling_error"
                if tool_item is not None and tool_item.reason_code == "live_protocol_error"
                else "tool_calling_invalid"
            )
        )
        tool_probe = (
            CapabilityProbeResult(
                "tool_calling",
                True,
                tool_item.passed if tool_item is not None else False,
                tool_reason,
            )
            if tools_adv
            else CapabilityProbeResult("tool_calling", False, None, "tool_calling_unadvertised")
        )
        self._last_capability_probes = {
            "embeddings": embedding_probe,
            "structured_output": structured_probe,
            "logprobs": logprobs_probe,
            "tool_calling": tool_probe,
        }
        self._check_identity()
        if self._identity_changed:
            raise ValueError("backend identity changed during protocol probe")
        return BackendConformanceRecord(
            protocol.backend, protocol.model, protocol.checked_at,
            (chat_result, cancellation, *protocol.results, embedding_result),
            probe_version=protocol.probe_version, synthetic=True,
            identity=identity,
        )


__all__ = [
    "CapabilityProbeResult",
    "IdentityUnavailable",
    "SonderInferenceIdentityReader",
    "SonderInferenceProtocolProbe",
    "SyntheticIdentityRefused",
]
