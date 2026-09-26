"""Validated, content-free provider bindings for model-gateway composition."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

PROVIDER_TIERS = ("fast", "general", "code", "reasoning", "vision")
TIER_PROVIDER_ENV = {
    "fast": "SONDER_FAST_PROVIDER",
    "general": "SONDER_GENERAL_PROVIDER",
    "code": "SONDER_CODE_PROVIDER",
    "reasoning": "SONDER_REASONING_PROVIDER",
    "vision": "SONDER_VISION_PROVIDER",
}
INFERENCE_FALLBACK_ENV = "SONDER_INFERENCE_FALLBACK"
_ALIASES = {
    "ollama": "ollama",
    "openai": "openai_compatible",
    "openai-compatible": "openai_compatible",
    "openai_compatible": "openai_compatible",
    "llamacpp": "openai_compatible",
    "vllm": "openai_compatible",
    "sonder-inference": "sonder_inference",
    "sonder_inference": "sonder_inference",
    "sonder-infer": "sonder_inference",
    "inference": "sonder_inference",
}
# "sonder" names the logical chat tier and the local model alias, so it must
# never silently select a transport.  It stays an unknown-provider error with
# a pointer at the provider name the operator most likely meant.
_RESERVED_NAMES = {
    "sonder": (
        "'sonder' is a tier/model name, not a provider; "
        "use 'sonder-inference' for the Sonder Inference server"
    ),
}
# The only fallback a provider may declare.  A fallback is attempted solely
# when the primary provably did not execute the request, so the target must
# be a local provider whose own consent rules still apply.
ALLOWED_FALLBACKS = MappingProxyType({"sonder_inference": frozenset({"ollama"})})
# dispatch_provider() labels are persisted as capture evidence and must not
# change; this maps each label to the provider id used by bindings, status
# and telemetry.
PROVIDER_LABEL_IDS = MappingProxyType({
    "ollama": "ollama",
    "openai-compatible": "openai_compatible",
    "sonder-inference": "sonder_inference",
})


def normalize_provider(value: str) -> str:
    normalized = str(value or "").strip().lower()
    try:
        return _ALIASES[normalized]
    except KeyError as exc:
        hint = _RESERVED_NAMES.get(normalized)
        if hint:
            raise ValueError("unknown model provider %r: %s" % (value, hint)) from exc
        raise ValueError("unknown model provider %r" % value) from exc


def provider_id_for_label(label: str) -> str:
    """Return the binding id for a ``dispatch_provider`` label."""
    try:
        return PROVIDER_LABEL_IDS[str(label)]
    except KeyError as exc:
        raise ValueError("unknown provider label %r" % label) from exc


@dataclass(frozen=True)
class ProviderBindings:
    default_generation_provider: str
    tier_providers: Mapping[str, str]
    embedding_provider: str
    fallbacks: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        default = normalize_provider(self.default_generation_provider)
        embedding = normalize_provider(self.embedding_provider)
        tiers = {str(k): normalize_provider(v) for k, v in self.tier_providers.items()}
        if set(tiers) != set(PROVIDER_TIERS):
            raise ValueError("provider bindings must define exactly %r" % (PROVIDER_TIERS,))
        generation = {default, *tiers.values()}
        fallbacks: dict[str, str] = {}
        for primary, target in dict(self.fallbacks or {}).items():
            source = normalize_provider(primary)
            destination = normalize_provider(target)
            if destination not in ALLOWED_FALLBACKS.get(source, frozenset()):
                raise ValueError(
                    "unsupported provider fallback %s -> %s (only "
                    "sonder_inference -> ollama is allowed)" % (source, destination)
                )
            if source not in generation:
                raise ValueError(
                    "provider fallback declared for %s, which serves no "
                    "generation binding" % source
                )
            fallbacks[source] = destination
        object.__setattr__(self, "default_generation_provider", default)
        object.__setattr__(self, "tier_providers", MappingProxyType(tiers))
        object.__setattr__(self, "embedding_provider", embedding)
        object.__setattr__(self, "fallbacks", MappingProxyType(fallbacks))

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
        """Every provider composition must construct, fallback targets included.

        Callers that need only the providers a request can be *routed* to
        directly (for example the strict local-alias gate) use
        :attr:`bound_providers`: a fallback target is reachable only through
        its primary's pre-send fallback, never by binding.
        """
        return frozenset((*self.bound_providers, *self.fallbacks.values()))

    @property
    def bound_providers(self) -> frozenset[str]:
        """Providers a request can be routed to directly (no fallbacks)."""
        return frozenset((
            self.default_generation_provider,
            *self.tier_providers.values(),
            self.embedding_provider,
        ))

    def status_projection(self) -> dict[str, object]:
        return {
            "default_generation_provider": self.default_generation_provider,
            "tier_providers": dict(self.tier_providers),
            "embedding_provider": self.embedding_provider,
            "fallbacks": dict(self.fallbacks),
        }


def inference_fallback_from_env(source: Mapping[str, str]) -> str | None:
    """Parse ``SONDER_INFERENCE_FALLBACK`` (``none`` or ``ollama``)."""
    raw = str(source.get(INFERENCE_FALLBACK_ENV, "") or "").strip().lower()
    if raw in ("", "none"):
        return None
    if raw == "ollama":
        return "ollama"
    raise ValueError(
        "%s must be 'none' or 'ollama', got %r" % (INFERENCE_FALLBACK_ENV, raw)
    )


def provider_bindings_from_env(
    env: Mapping[str, str] | None = None,
) -> ProviderBindings:
    source = os.environ if env is None else env
    default = normalize_provider(
        str(source.get("SONDER_MODEL_BACKEND", "") or "").strip() or "ollama"
    )
    tiers = {
        tier: normalize_provider(
            str(source.get(variable, "") or "").strip() or default
        )
        for tier, variable in TIER_PROVIDER_ENV.items()
    }
    embedding = normalize_provider(
        str(source.get("SONDER_EMBEDDING_PROVIDER", "") or "").strip() or default
    )
    fallback = inference_fallback_from_env(source)
    fallbacks: dict[str, str] = {}
    if fallback is not None:
        # A fallback for a provider nothing is bound to would be a silent
        # no-op; the operator almost certainly mis-set the bindings.
        if "sonder_inference" not in {default, *tiers.values()}:
            raise ValueError(
                "%s=%s is set but no generation tier is bound to "
                "sonder_inference" % (INFERENCE_FALLBACK_ENV, fallback)
            )
        fallbacks["sonder_inference"] = fallback
    return ProviderBindings(default, tiers, embedding, fallbacks)


__all__ = [
    "ALLOWED_FALLBACKS",
    "INFERENCE_FALLBACK_ENV",
    "PROVIDER_LABEL_IDS",
    "PROVIDER_TIERS",
    "TIER_PROVIDER_ENV",
    "ProviderBindings",
    "inference_fallback_from_env",
    "normalize_provider",
    "provider_bindings_from_env",
    "provider_id_for_label",
]
