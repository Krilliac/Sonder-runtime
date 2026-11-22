"""Pure price rules for the metered OpenAI-compatible gateway.

I/O-free.  The operator states the prices (USD per 1M tokens) because they
differ per provider and drift; there is no built-in table.  ``"0,0"`` declares
a free endpoint.  Cost arithmetic matches
:func:`sonder_runtime.domain.anthropic_policy.usage_cost_usd`: tokens times the
rate over 1e6, rounded UP to a micro-dollar, so spend is never under-counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_PRICE_USD_PER_MTOK = Decimal(10_000)
_PER_MILLION = Decimal(1_000_000)
_MICRO = Decimal("0.000001")


class OpenAISpendPolicyError(ValueError):
    """A price setting or token count failed validation."""


@dataclass(frozen=True)
class ChatPrice:
    """USD per 1M input and output tokens."""

    input: Decimal
    output: Decimal


def _rate(text: str, label: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError) as exc:
        raise OpenAISpendPolicyError("%s must be a dollar amount per 1M tokens" % label) from exc
    if not value.is_finite() or not Decimal(0) <= value <= MAX_PRICE_USD_PER_MTOK:
        raise OpenAISpendPolicyError(
            "%s must be between 0 and %s USD per 1M tokens" % (label, MAX_PRICE_USD_PER_MTOK)
        )
    return value


def parse_chat_price(raw: str) -> ChatPrice:
    """Parse ``"<input>,<output>"``."""
    parts = str(raw).split(",")
    if len(parts) != 2:
        raise OpenAISpendPolicyError(
            "price must be '<input>,<output>' in USD per 1M tokens (for example 0.15,0.60)"
        )
    return ChatPrice(_rate(parts[0], "input price"), _rate(parts[1], "output price"))


def parse_embed_price(raw: str) -> Decimal:
    """Parse a single ``"<input>"`` price."""
    return _rate(str(raw), "input price")


def cost_usd(*, input_tokens: int, output_tokens: int, input_rate: Decimal,
             output_rate: Decimal = Decimal(0)) -> Decimal:
    """Cost of one call, rounded up to a micro-dollar."""
    for count in (input_tokens, output_tokens):
        if type(count) is not int or count < 0:
            raise OpenAISpendPolicyError("token counts must be non-negative integers")
    total = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / _PER_MILLION
    return total.quantize(_MICRO, rounding="ROUND_UP")


__all__ = [
    "MAX_PRICE_USD_PER_MTOK",
    "ChatPrice",
    "OpenAISpendPolicyError",
    "cost_usd",
    "parse_chat_price",
    "parse_embed_price",
]
