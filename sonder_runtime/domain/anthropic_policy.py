"""Pure rules for the Anthropic (Claude API) model provider.

I/O-free: model id grammar, the USD price table and cost arithmetic, and the
error-text rule that tells a credit-exhausted account apart from a malformed
request.  The gateway (``adapters/inference/anthropic_gateway.py``) owns every
network, environment and storage concern.

Prices below are USD per 1M tokens, copied from the Claude API skill's shared
documentation (``claude-api/shared/models.md`` model descriptions, and the
cache multipliers in ``claude-api/shared/prompt-caching.md``: reads are quoted
per model, 5-minute cache writes cost 1.25x the input price).  A model that
is not in this table is refused by the gateway rather than sent unpriced: the
daily spend cap could not count it.  Where the documentation quotes no cache
rate for a model (Haiku 5.5), the table charges the full input price, which
can only over-count spend against the cap, never under-count it.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

PROVIDER_ID = "anthropic"
# "claude-opus-5-5" is the current Opus in claude-api/shared/models.md.
DEFAULT_MODEL = "claude-opus-5-5"
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_DATED_SUFFIX = re.compile(r"-\d{8}\Z")
_PER_MILLION = Decimal(1_000_000)
_MICRO = Decimal("0.000001")


class AnthropicPolicyError(ValueError):
    """An Anthropic model id or usage record failed validation."""


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens; ``long_*`` apply when the prompt exceeds ``long_over``."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal
    long_over: int = 0
    long_input: Decimal = Decimal(0)
    long_output: Decimal = Decimal(0)
    long_cache_read: Decimal = Decimal(0)
    long_cache_write: Decimal = Decimal(0)


def _price(input_: str, output: str, cache_read: str, cache_write: str) -> ModelPrice:
    return ModelPrice(Decimal(input_), Decimal(output), Decimal(cache_read), Decimal(cache_write))


_FABLE_5_2 = _price("8", "40", "0.20", "10")
_FABLE_5_1 = _price("10", "50", "0.25", "12.5")
PRICES_USD_PER_MILLION: Mapping[str, ModelPrice] = MappingProxyType({
    "claude-fable-5-2": _FABLE_5_2,
    "claude-mythos-5-2": _FABLE_5_2,
    "claude-fable-5-1": _FABLE_5_1,
    "claude-mythos-5-1": _FABLE_5_1,
    "claude-opus-5-5": _price("4", "20", "0.20", "5"),
    "claude-sonnet-5-5": _price("2", "10", "0.10", "2.5"),
    # $0.10/$0.50 up to a 100K-token prompt, $0.50/$2.50 above it.
    "claude-haiku-5-5": ModelPrice(
        Decimal("0.10"), Decimal("0.50"), Decimal("0.10"), Decimal("0.125"),
        long_over=100_000, long_input=Decimal("0.50"), long_output=Decimal("2.50"),
        long_cache_read=Decimal("0.50"), long_cache_write=Decimal("0.625"),
    ),
})


def validate_model_id(value: object, label: str = "model") -> str:
    text = str(value or "").strip()
    if MODEL_ID.match(text) is None:
        raise AnthropicPolicyError(
            "%s must be a Claude model id such as %s, got %r" % (label, DEFAULT_MODEL, text[:80])
        )
    return text


def price_for(model: str) -> ModelPrice | None:
    """The price row for ``model`` (a dated snapshot id maps to its base id)."""
    key = str(model or "").strip()
    return PRICES_USD_PER_MILLION.get(key) or PRICES_USD_PER_MILLION.get(_DATED_SUFFIX.sub("", key))


def usage_cost_usd(
    model: str, *, input_tokens: int, output_tokens: int,
    cache_read_tokens: int = 0, cache_write_tokens: int = 0,
) -> Decimal:
    """Cost of one response from its token usage, rounded up to a micro-dollar.

    ``input_tokens`` is the uncached remainder, as the API reports it; the
    cache counts are separate fields.  Raises for an unpriced model.
    """
    price = price_for(model)
    if price is None:
        raise AnthropicPolicyError("no price for model %r" % str(model)[:80])
    counts = (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    if any(type(count) is not int or count < 0 for count in counts):
        raise AnthropicPolicyError("token counts must be non-negative integers")
    prompt = input_tokens + cache_read_tokens + cache_write_tokens
    long_prompt = price.long_over > 0 and prompt > price.long_over
    rates = (
        (price.long_input, price.long_output, price.long_cache_read, price.long_cache_write)
        if long_prompt else
        (price.input, price.output, price.cache_read, price.cache_write)
    )
    total = (
        Decimal(input_tokens) * rates[0] + Decimal(output_tokens) * rates[1]
        + Decimal(cache_read_tokens) * rates[2] + Decimal(cache_write_tokens) * rates[3]
    ) / _PER_MILLION
    return total.quantize(_MICRO, rounding="ROUND_UP")


def is_credit_exhausted(status: object, message: object) -> bool:
    """Whether an error response means the account has run out of credit.

    The API reports it as HTTP 400 ``invalid_request_error`` whose message is
    "Your credit balance is too low to access the Anthropic API"
    (claude-api/shared/api-sign-in.md); a 402 ``billing_error`` is the same
    class of problem.  A 400 about anything else is a malformed request.
    """
    if status == 402:
        return True
    if status != 400:
        return False
    text = " ".join(str(message or "").lower().split())
    return "credit balance" in text and "too low" in text


__all__ = [
    "DEFAULT_MODEL",
    "PRICES_USD_PER_MILLION",
    "PROVIDER_ID",
    "AnthropicPolicyError",
    "ModelPrice",
    "is_credit_exhausted",
    "price_for",
    "usage_cost_usd",
    "validate_model_id",
]
