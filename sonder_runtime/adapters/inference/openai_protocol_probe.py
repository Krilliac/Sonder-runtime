"""Opt-in OpenAI-compatible protocol diagnostics over the gateway transport.

Only fixed JSON-shape requests leave the host. The gateway currently exposes
textual model responses, and this probe has no granted production ToolGateway.
Provider text alone cannot establish tool dispatch, continuation or identity
of the backend's weights, tokenizer and hardware.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.routing.backend_conformance import run_protocol_probes
from sonder_runtime.domain.common.errors import DeadlineExceeded
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendConformanceRecord,
    BackendIdentity,
    ProbeResult,
)

from .openai_compat_gateway import OpenAICompatibleGateway

_CASES = (
    BackendCapability.STRUCTURED,
    BackendCapability.TOOL_NATIVE,
    BackendCapability.TOOL_FALLBACK,
    BackendCapability.TOOL_SEQUENTIAL,
    BackendCapability.TOOL_CONTINUATION,
)


class OpenAICompatibleProtocolProbe:
    """Report bounded, nonpromotable diagnostics for one configured endpoint."""

    def __init__(
        self, gateway: OpenAICompatibleGateway, *,
        identity_reader: Callable[[], BackendIdentity],
        cloud_allowed: bool = False,
    ) -> None:
        if type(gateway) is not OpenAICompatibleGateway or not callable(identity_reader):
            raise TypeError("concrete OpenAI gateway and host identity reader required")
        self._gateway = gateway
        self._read_identity = identity_reader
        self._cloud_allowed = cloud_allowed
        self._identity: BackendIdentity | None = None
        self._identity_changed = False
        self._deadline = 0.0
        self._transport_at_start = gateway._transport
        self._base_url = ""

    def _check_identity(self) -> BackendIdentity:
        current = self._read_identity()
        if (not isinstance(current, BackendIdentity) or current != self._identity
                or self._gateway._transport is not self._transport_at_start):
            self._identity_changed = True
            raise ValueError("host backend identity or transport changed during protocol probe")
        return current

    def _request(self, messages: list[dict[str, str]], *, timeout_seconds: float) -> str:
        identity = self._check_identity()
        config = self._gateway._resolved_config()
        if (config.model != identity.model or identity.backend != "openai-compatible"
                or config.base_url != self._base_url):
            raise ValueError("configured endpoint or model differs from host backend identity")
        remaining = min(timeout_seconds, self._deadline - time.monotonic())
        if remaining <= 0:
            raise DeadlineExceeded("protocol probe wall budget exhausted")
        context = local_owner_context(
            correlation_id="backend-protocol-conformance",
            timeout_seconds=remaining, cloud_allowed=self._cloud_allowed,
        )
        self._gateway._enforce_consent(config, context)
        timeout = self._gateway._check_liveness(context)
        response = self._gateway._post(
            "/v1/chat/completions",
            {"model": identity.model, "messages": messages, "stream": False,
             "temperature": 0.0, "max_tokens": 128},
            config, timeout, context=context,
        )
        self._gateway._check_liveness(context, phase="during protocol probe")
        self._check_identity()
        # The regular gateway's ModelResponse.model is request-derived. An
        # identity-bound conformance probe must inspect the provider's claim.
        if response.get("model") != identity.model:
            raise ValueError("provider response did not identify the probed model")
        text = self._gateway._extract_text(response)
        if len(text.encode("utf-8")) > 4096:
            raise ValueError("protocol probe response exceeds the text bound")
        return text

    @staticmethod
    def _tool_call(text: str, expected_value: str) -> bool:
        try:
            value = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            return False
        return type(value) is dict and value == {
            "tool": "echo", "arguments": {"value": expected_value},
        }

    @staticmethod
    def _ask(value: str) -> list[dict[str, str]]:
        return [{"role": "user", "content": (
            'Return only a JSON object with "tool":"echo" and '
            f'"arguments":{{"value":"{value}"}}. Do not return a final answer.'
        )}]

    def run_case(self, capability: BackendCapability, *, model: str,
                 timeout_seconds: float) -> dict[str, object] | None:
        if self._identity is None or model != self._identity.model:
            raise ValueError("probe model differs from host identity")
        if capability is BackendCapability.STRUCTURED:
            text = self._request(self._ask("alpha"), timeout_seconds=timeout_seconds)
            return {"model": model, "schema_valid": self._tool_call(text, "alpha"),
                    "response_kind": "object"}
        # The ordinary OpenAI-compatible gateway cannot publish a native
        # tool-call-only response. Text JSON without a real granted tool
        # dispatch and host receipt cannot establish the other tool protocols.
        return None

    def run(self, *, timeout_seconds: float = 30.0) -> BackendConformanceRecord:
        if not 0.0 < float(timeout_seconds) <= 300.0:
            raise ValueError("timeout_seconds must be between 0 and 300")
        self._identity = self._read_identity()
        if not isinstance(self._identity, BackendIdentity):
            raise TypeError("host identity reader must return a BackendIdentity")
        self._identity_changed = False
        self._transport_at_start = self._gateway._transport
        self._base_url = self._gateway._resolved_config().base_url
        self._deadline = time.monotonic() + float(timeout_seconds)
        try:
            chat = self._request(
                [{"role": "user", "content": "Reply with a nonempty greeting."}],
                timeout_seconds=timeout_seconds,
            )
            chat_result = ProbeResult(BackendCapability.CHAT, bool(chat.strip()),
                                      "plain_chat_passed" if chat.strip() else "plain_chat_empty")
        except Exception:  # noqa: BLE001 - provider errors become evidence, never leaked text
            chat_result = ProbeResult(BackendCapability.CHAT, False, "plain_chat_error")

        # A pre-cancelled gateway request proves only local admission behavior;
        # it cannot certify that an in-flight provider effect actually stopped.
        cancellation = ProbeResult(
            BackendCapability.CANCELLATION, None, "inflight_cancellation_unmeasured",
        )
        protocol = run_protocol_probes(
            self, identity=self._identity, timeout_seconds=timeout_seconds, cases=_CASES,
        )
        self._check_identity()
        if self._identity_changed:
            raise ValueError("host backend identity changed during protocol probe")
        return BackendConformanceRecord(
            protocol.backend, protocol.model, protocol.checked_at,
            (chat_result, cancellation, *protocol.results),
            probe_version=protocol.probe_version, synthetic=True,
            identity=self._identity,
        )


__all__ = ["OpenAICompatibleProtocolProbe"]
