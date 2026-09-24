"""Chat use cases over the ModelGateway port (SPEC-3 application layer).

A minimal, typed application service: it translates a chat command into a
ModelRequest, invokes the gateway (which enforces the operation-context
consent gate and maps transport errors into the domain taxonomy), and
returns a typed result. Transport adapters translate protocol shapes at
the edge; this layer never touches HTTP, SQLite, or the model transport
directly.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..context import OperationContext

logger = logging.getLogger(__name__)
from ..ports.model_gateway import InferenceTelemetry, ModelGateway, ModelRequest
from ..session.capture import CapturedTurn, SessionCaptureService
from ..session.provider_attempts import ProviderCaptureFailure, provider_attempt_scope
from ...domain.common.errors import IntegrityFailure, InternalFailure, InvalidInput, SonderError
from ...domain.common.ids import SessionId, TurnId, new_id


@dataclass(frozen=True)
class ChatCommand:
    content: str
    # ``sonder`` resolves the chat lane's current policy tier at dispatch.
    # An explicitly requested tier remains pinned to the caller's choice.
    tier: str = "sonder"
    lane: str = "chat"
    routing_reason: str = "ordinary conversation"
    system: str = ""
    history: tuple = ()
    temperature: float | None = None
    num_predict: int | None = None
    num_ctx: int | None = None
    session_id: SessionId | None = None
    turn_id: TurnId | None = None


@dataclass(frozen=True)
class ChatResult:
    response_text: str
    model: str
    tier: str
    duration_ms: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    telemetry: InferenceTelemetry | None = None
    capture: CapturedTurn | None = None
    lane: str = "chat"
    routing_reason: str = "ordinary conversation"


class ChatService:
    def __init__(
        self,
        model_gateway: ModelGateway,
        session_capture_service: SessionCaptureService | None = None,
        *,
        session_capture_factory: Callable[[], SessionCaptureService] | None = None,
        chat_default_tier: Callable[[], str] | None = None,
        strict_alias_provider_available: bool = True,
    ) -> None:
        if session_capture_service is not None and session_capture_factory is not None:
            raise ValueError("provide a session capture service or factory, not both")
        self._gateway = model_gateway
        self._capture = session_capture_service
        self._capture_factory = session_capture_factory
        self._chat_default_tier = chat_default_tier
        self._strict_alias_provider_available = strict_alias_provider_available

    def complete(
        self, command: ChatCommand, context: OperationContext
    ) -> ChatResult:
        logger.debug(f"ChatService.complete: tier={command.tier!r}, system_len={len(command.system)}, history_len={len(command.history)}, session_id={command.session_id!r}")
        options = {}
        if command.temperature is not None:
            options["temperature"] = command.temperature
        if command.num_predict is not None:
            options["num_predict"] = command.num_predict
        if command.num_ctx is not None:
            options["num_ctx"] = command.num_ctx
        if command.lane != "chat":
            raise ValueError("ChatService accepts only the chat routing lane")
        if not isinstance(command.routing_reason, str) or not command.routing_reason.strip():
            raise ValueError("chat routing reason must be non-empty text")
        # The logical default must resolve before mixed-provider dispatch;
        # otherwise the dispatcher picks its backend default instead of the
        # provider bound to the operator-selected chat tier.
        tier = (
            self._chat_default_tier()
            if command.tier == "sonder" and self._chat_default_tier is not None
            else command.tier
        )
        strict_alias = command.tier == "sonder" and tier == "sonder" and self._chat_default_tier is not None
        if strict_alias and not self._strict_alias_provider_available:
            raise InvalidInput("strict chat alias requires a configured local Ollama provider")
        request = ModelRequest(
            prompt=command.content,
            tier=tier,
            system=command.system,
            history=tuple(command.history),
            options=options,
            routing_metadata={
                "lane": command.lane,
                "reason": command.routing_reason.strip()[:240],
            },
        )
        capture_service = self._capture
        if capture_service is None and self._capture_factory is not None:
            capture_service = self._capture_factory()
        pending = None
        if capture_service is not None:
            session_id = command.session_id or SessionId.new()
            turn_id = command.turn_id or TurnId.new()
            pending = capture_service.begin_request(
                session_id.serialize(),
                turn_id.serialize(),
                request,
                request_id=new_id("request"),
                user_message=command.content,
            )
        logger.debug(f"ChatService.complete: sending request to gateway, tier={command.tier!r}")
        try:
            with provider_attempt_scope(capture_service, pending):
                local_alias = getattr(self._gateway, "generate_strict_alias", None)
                response = (
                    local_alias(request, context)
                    if strict_alias and callable(local_alias)
                    else self._gateway.generate(request, context)
                )
        except Exception as model_error:
            if isinstance(model_error, ProviderCaptureFailure):
                raise
            if pending is not None:
                code = (
                    model_error.code
                    if isinstance(model_error, SonderError)
                    else InternalFailure.code
                )
                try:
                    capture_service.fail_request(pending, error_code=code)
                except Exception as capture_error:
                    raise IntegrityFailure("could not persist model failure") from capture_error
            raise

        capture = None
        if pending is not None:
            capture = capture_service.complete_request(pending, model_response=response.text)
        if response.duration_ms and response.duration_ms > 30_000:
            logger.warning(f"slow inference response: model={response.model!r}, duration_ms={response.duration_ms}, tier={command.tier!r}")
        logger.debug(f"ChatService.complete: response model={response.model!r}, duration_ms={response.duration_ms}, tokens_in={response.tokens_in}, tokens_out={response.tokens_out}, captured={capture is not None}")
        return ChatResult(
            response_text=response.text,
            model=response.model,
            tier=response.tier,
            duration_ms=response.duration_ms,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            telemetry=getattr(response, "telemetry", None),
            capture=capture,
            lane=command.lane,
            routing_reason=command.routing_reason.strip()[:240],
        )
