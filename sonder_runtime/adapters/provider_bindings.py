"""Validated, content-free provider bindings for model-gateway composition."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

PROVIDER_TIERS = ("fast", "general", "code", "reasoning", "vision")
TIER_PROVIDER_ENV = {
    "fast": "SONDER_FAST_PROVIDER",
    "general": "SONDER_GENERAL_PROVIDER",
    "code": "SONDER_CODE_PROVIDER",
    "reasoning": "SONDER_REASONING_PROVIDER",
    "vision": "SONDER_VISION_PROVIDER",
}
_ALIASES = {
    "ollama": "ollama",
    "openai": "openai_compatible",
    "openai-compatible": "openai_compatible",
    "openai_compatible": "openai_compatible",
    "llamacpp": "openai_compatible",
    "vllm": "openai_compatible",
}


def normalize_provider(value: str) -> str:
    normalized = str(value or "").strip().lower()
    try:
        return _ALIASES[normalized]
    except KeyError as exc:
        raise ValueError("unknown model provider %r" % value) from exc


@dataclass(frozen=True)
class ProviderBindings:
    default_generation_provider: str
    tier_providers: Mapping[str, str]
    embedding_provider: str

    def __post_init__(self) -> None:
        default = normalize_provider(self.default_generation_provider)
        embedding = normalize_provider(self.embedding_provider)
        tiers = {str(k): normalize_provider(v) for k, v in self.tier_providers.items()}
        if set(tiers) != set(PROVIDER_TIERS):
            raise ValueError("provider bindings must define exactly %r" % (PROVIDER_TIERS,))
        object.__setattr__(self, "default_generation_provider", default)
        object.__setattr__(self, "tier_providers", MappingProxyType(tiers))
        object.__setattr__(self, "embedding_provider", embedding)

    @classmethod
    def uniform(cls, provider: str) -> "ProviderBindings":
        normalized = normalize_provider(provider)
        return cls(
            default_generation_provider=normalized,
            tier_providers={tier: normalized for tier in PROVIDER_TIERS},
            embedding_provider=normalized,
        )

    @property
    def required_providers(self) -> frozenset[str]:
        return frozenset((*self.tier_providers.values(), self.embedding_provider))

    def status_projection(self) -> dict[str, object]:
        return {
            "default_generation_provider": self.default_generation_provider,
            "tier_providers": dict(self.tier_providers),
            "embedding_provider": self.embedding_provider,
        }


def provider_bindings_from_env(
    env: Mapping[str, str] | None = None,
) -> ProviderBindings:
    source = os.environ if env is None else env
    default = normalize_provider(source.get("SONDER_MODEL_BACKEND", "ollama") or "ollama")
    tiers = {
        tier: normalize_provider(source.get(variable, "") or default)
        for tier, variable in TIER_PROVIDER_ENV.items()
    }
    embedding = normalize_provider(source.get("SONDER_EMBEDDING_PROVIDER", "") or default)
    return ProviderBindings(default, tiers, embedding)


__all__ = [
    "PROVIDER_TIERS",
    "TIER_PROVIDER_ENV",
    "ProviderBindings",
    "normalize_provider",
    "provider_bindings_from_env",
]
