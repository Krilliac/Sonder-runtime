"""Measured, loopback-only Ollama conformance probes.

This adapter is the concrete host attestation boundary.  The generic protocol
probe validates traces, but only this class binds them to Ollama's observed
version, model digest and ``/api/show`` metadata.
"""
from __future__ import annotations

import hashlib
import json
import platform
import time
import urllib.request
from collections.abc import Mapping
from urllib.parse import urlsplit

import sonder_runtime.adapters.inference.gpu_admission as gpu_admission
from sonder_runtime.adapters.inference.ollama_endpoint import open_url
from sonder_runtime.domain.gpu_admission import CtxPolicy
from sonder_runtime.application.routing.backend_conformance import (
    PROTOCOL_CASES,
    run_protocol_probes,
)
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendConformanceRecord,
    BackendIdentity,
    ProbeResult,
)
from sonder_runtime.domain.routing.model_names import tagged_ollama_model as _tagged

_MAX_RESPONSE_BYTES = 1_048_576
_MAX_TEXT_BYTES = 4096
_SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}},
           "required": ["value"], "additionalProperties": False}


def _digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class OllamaConformanceProbe:
    """Run bounded probes against one local Ollama model."""

    def __init__(self, base_url: str, model: str, context_tokens: int = 8192) -> None:
        parsed = urlsplit(str(base_url).rstrip("/"))
        if parsed.scheme != "http" or parsed.username or parsed.password:
            raise ValueError("Ollama conformance requires a plain loopback HTTP URL")
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Ollama conformance is loopback-only")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.port is None:
            raise ValueError("base_url must be a loopback origin with an explicit port")
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("model is required and bounded")
        if type(context_tokens) is not int or context_tokens <= 0:
            raise ValueError("context_tokens must be positive")
        self.base_url = str(base_url).rstrip("/")
        self.model = model.strip()
        self.context_tokens = context_tokens
        self._identity: BackendIdentity | None = None
        self._deadline = 0.0
        self._running = False
        self._identity_changed = False

    def _request(self, path: str, payload: Mapping[str, object] | None, *, timeout: float) -> dict:
        if self._deadline - time.monotonic() <= 0:
            raise TimeoutError("Ollama conformance wall budget exhausted")
        # The evidence is bound to the window sent, so this battery never accepts
        # a rewritten num_ctx: a loaded model at another window makes it wait.
        # A refusal (GpuAdmissionRefused) propagates: it is not a measurement.
        with gpu_admission.process_service().admit(
            path, payload, origin=self.base_url, deadline=self._deadline,
            ctx_policy=CtxPolicy.EXACT,
        ) as ticket:
            remaining = self._deadline - time.monotonic()  # admission may have queued
            if remaining <= 0:
                raise TimeoutError("Ollama conformance wall budget exhausted")
            sent = ticket.payload  # the decided body (INHERIT_PINNED names the window)
            is_chat = path == "/v1/chat/completions"
            if sent is not None and is_chat:
                from sonder_runtime.adapters.inference.si_chat_codec import to_openai_chat_payload

                wire_payload: Mapping[str, object] = to_openai_chat_payload(sent)
            else:
                wire_payload = sent  # type: ignore[assignment]
            from sonder_runtime.adapters.inference.sonder_inference_gateway import si_auth_headers

            body = None if wire_payload is None else json.dumps(wire_payload, separators=(",", ":")).encode()
            headers: dict[str, str] = {"Content-Type": "application/json"} if body is not None else {}
            headers.update(si_auth_headers(self.base_url))
            if is_chat:
                headers["X-Sonder-Local-Only"] = "1"
            request = urllib.request.Request(
                self.base_url + path, data=body,
                headers=headers,
                method="POST" if body is not None else "GET",
            )
            with open_url(request, timeout=min(timeout, remaining), allow_remote=False) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("Ollama response exceeds size bound")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Ollama response must be an object")
        if is_chat:
            from sonder_runtime.adapters.inference.si_chat_codec import from_openai_chat_response

            value = from_openai_chat_response(value)
        return value

    def read_identity(self) -> BackendIdentity:
        """Read and validate Sonder Inference host identity via ``/v1/sonder/identity``."""
        import urllib.parse

        if not self._running:
            self._deadline = time.monotonic() + 5.0
        query = "/v1/sonder/identity?model=" + urllib.parse.quote(self.model, safe="")
        payload = self._request(query, None, timeout=5)
        raw = payload.get("backend_identity")
        if not isinstance(raw, dict):
            raise ValueError("Sonder Inference identity is unavailable for model %r" % self.model)
        return BackendIdentity(
            backend=str(raw.get("backend") or "llamacpp"),
            model=self.model,
            model_digest=str(raw.get("model_digest") or "").lower(),
            quantization=str(raw.get("quantization") or ""),
            backend_version=str(raw.get("backend_version") or ""),
            tokenizer_digest=str(raw.get("tokenizer_digest") or ""),
            template_digest=str(raw.get("template_digest") or ""),
            context_tokens=int(raw.get("context_tokens") or self.context_tokens),
            hardware=str(raw.get("hardware") or ("host-origin:" + _digest((platform.node(), platform.machine(), self.base_url)))),
        )

    @staticmethod
    def _message(response: Mapping[str, object]) -> Mapping[str, object]:
        message = response.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("Ollama response has no message")
        return message

    @staticmethod
    def _valid_echo_call(calls: object, expected: str = "alpha") -> bool:
        if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], Mapping):
            return False
        function = calls[0].get("function")
        if not isinstance(function, Mapping) or function.get("name") != "echo":
            return False
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                return False
        return arguments == {"value": expected}

    def _chat(self, *, messages: list[dict], **options: object) -> dict:
        self._check_identity()
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "options": {"temperature": 0, "num_ctx": self.context_tokens,
                               "num_predict": 128}}
        payload.update(options)
        response = self._request("/v1/chat/completions", payload, timeout=max(0.1, self._deadline - time.monotonic()))
        if not isinstance(response.get("model"), str) or _tagged(response["model"]) != _tagged(self.model):
            raise ValueError("Ollama response model mismatch")
        self._check_identity()
        return response

    def _check_identity(self):
        if self._identity_changed or self.read_identity() != self._identity:
            self._identity_changed = True
            raise ValueError("Ollama host identity changed during conformance probe")

    def run_case(self, capability: BackendCapability, *, model: str,
                 timeout_seconds: float) -> Mapping[str, object] | None:
        if self._identity is None or model != self._identity.model:
            raise ValueError("probe model differs from host identity")
        if capability is BackendCapability.STRUCTURED:
            response = self._chat(messages=[{"role": "user", "content": "Return a JSON object with value alpha."}], format=_SCHEMA)
            text = str(self._message(response).get("content") or "")
            try:
                value = json.loads(text)
            except (TypeError, ValueError):
                value = None
            return {"model": model, "schema_valid": value == {"value": "alpha"}, "response_kind": "object"}
        tool = {"type": "function", "function": {"name": "echo", "description": "Echo a value", "parameters": _SCHEMA}}
        if capability in {BackendCapability.TOOL_NATIVE, BackendCapability.TOOLS_WITH_SCHEMA}:
            kwargs = {"tools": [tool]}
            if capability is BackendCapability.TOOLS_WITH_SCHEMA:
                kwargs["format"] = _SCHEMA
            response = self._chat(messages=[{"role": "user", "content": "Call echo with value alpha."}], **kwargs)
            calls = self._message(response).get("tool_calls") or []
            valid = self._valid_echo_call(calls)
            return {"model": model, "tool_calls": ["echo"] if valid else [], "schema_valid": valid}
        if capability is BackendCapability.TOOL_SEQUENTIAL:
            first = self._chat(messages=[{"role": "user", "content": "Call echo twice, first with a then b."}], tools=[tool])
            calls = self._message(first).get("tool_calls") or []
            if not self._valid_echo_call(calls, "a"):
                return {"model": model, "events": []}
            events = ["call:a", "result:a"]
            second = self._chat(messages=[{"role": "user", "content": "Call echo twice, first with a then b."}, self._message(first), {"role": "tool", "tool_name": "echo", "content": "a"}], tools=[tool])
            calls2 = self._message(second).get("tool_calls") or []
            if self._valid_echo_call(calls2, "b"):
                events.extend(["call:b", "result:b"])
            return {"model": model, "events": events}
        if capability is BackendCapability.TOOL_CONTINUATION:
            first = self._chat(messages=[{"role": "user", "content": "Call echo with value alpha."}], tools=[tool])
            calls = self._message(first).get("tool_calls") or []
            if not self._valid_echo_call(calls, "alpha"):
                return {"model": model, "events": []}
            final = self._chat(messages=[{"role": "user", "content": "Call echo with value alpha."}, self._message(first), {"role": "tool", "tool_name": "echo", "content": "alpha"}], tools=[tool])
            final_message = self._message(final)
            return {"model": model, "events": ["call:echo", "result:echo", "assistant:done"]
                    if str(final_message.get("content") or "").strip() and not final_message.get("tool_calls") else []}
        return None

    def run(self, *, timeout_seconds: float = 30.0) -> BackendConformanceRecord:
        if not 0.0 < float(timeout_seconds) <= 300.0:
            raise ValueError("timeout_seconds must be between 0 and 300")
        self._deadline = time.monotonic() + float(timeout_seconds)
        self._running = True
        self._identity_changed = False
        try:
            return self._run(timeout_seconds=timeout_seconds)
        finally:
            self._running = False

    def _run(self, *, timeout_seconds: float) -> BackendConformanceRecord:
        self._identity = self.read_identity()
        try:
            response = self._chat(messages=[{"role": "user", "content": "Reply with a nonempty greeting."}])
            text = str(self._message(response).get("content") or "")
            if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
                chat = ProbeResult(BackendCapability.CHAT, False, "plain_chat_response_too_large")
            else:
                chat = ProbeResult(BackendCapability.CHAT, bool(text.strip()), "plain_chat_passed" if text.strip() else "plain_chat_empty")
        except gpu_admission.GpuAdmissionRefused:
            raise  # never measured: the request did not run
        except ValueError as error:
            if "model mismatch" in str(error):
                raise
            chat = ProbeResult(BackendCapability.CHAT, False, "plain_chat_error")
        except Exception:  # noqa: BLE001 - probe failures become measured failures
            chat = ProbeResult(BackendCapability.CHAT, False, "plain_chat_error")
        protocol = run_protocol_probes(self, identity=self._identity, timeout_seconds=timeout_seconds,
                                        cases=PROTOCOL_CASES,
                                        propagate=(gpu_admission.GpuAdmissionRefused,))
        self._check_identity()
        return BackendConformanceRecord(self._identity.backend, self.model, protocol.checked_at,
                                        (chat, *protocol.results), probe_version=3,
                                        synthetic=False, identity=self._identity)


__all__ = ["OllamaConformanceProbe"]
