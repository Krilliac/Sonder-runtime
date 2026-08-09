"""Chat use cases over the ModelGateway port (SPEC-3 application layer).

A minimal, typed application service: it translates a chat command into a
ModelRequest, invokes the gateway (which enforces the operation-context
consent gate and maps transport errors into the domain taxonomy), and
returns a typed result. Transport adapters translate protocol shapes at
the edge; this layer never touches HTTP, SQLite, or the model transport
directly.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..context import OperationContext
from ..ports.model_gateway import InferenceTelemetry, ModelGateway, ModelRequest


@dataclass(frozen=True)
class ChatCommand:
    content: str
    tier: str = "sonder"
    system: str = ""
    history: tuple = ()
    temperature: float | None = None
    num_predict: int | None = None
    num_ctx: int | None = None


@dataclass(frozen=True)
class ChatResult:
    response_text: str
    model: str
    tier: str
    duration_ms: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    telemetry: InferenceTelemetry | None = None


class ChatService:
    def __init__(self, model_gateway: ModelGateway) -> None:
        self._gateway = model_gateway

    def complete(
        self, command: ChatCommand, context: OperationContext
    ) -> ChatResult:
        options = {}
        if command.temperature is not None:
            options["temperature"] = command.temperature
        if command.num_predict is not None:
            options["num_predict"] = command.num_predict
        if command.num_ctx is not None:
            options["num_ctx"] = command.num_ctx
        request = ModelRequest(
            prompt=command.content,
            tier=command.tier,
            system=command.system,
            history=tuple(command.history),
            options=options,
        )
        response = self._gateway.generate(request, context)
        return ChatResult(
            response_text=response.text,
            model=response.model,
            tier=response.tier,
            duration_ms=response.duration_ms,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            telemetry=getattr(response, "telemetry", None),
        )
