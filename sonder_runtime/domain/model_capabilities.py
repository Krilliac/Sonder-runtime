"""Pure normalization of model-catalog capability metadata."""

from __future__ import annotations


# Canonical ModelGateway capability vocabulary.  Adapters publish a subset of
# these as typed, static facts about their own transport shape — never a
# live probe result — so callers (routing, ``ProviderHealth.capabilities``)
# can compare gateways without importing a specific adapter module.
GATEWAY_CAPABILITY_CHAT = "chat"
GATEWAY_CAPABILITY_EMBEDDINGS = "embeddings"
# The gateway resolves model identity per request (a tier may select a
# different local or hosted model each call), rather than always talking to
# one fixed configured endpoint/model.
GATEWAY_CAPABILITY_TIERED_ROUTING = "tiered-routing"
# The inverse: one configured endpoint and model serve every request: there
# is no per-request local/cloud tier resolution.
GATEWAY_CAPABILITY_FIXED_ENDPOINT = "fixed-endpoint"

KNOWN_GATEWAY_CAPABILITIES = frozenset({
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_EMBEDDINGS,
    GATEWAY_CAPABILITY_TIERED_ROUTING,
    GATEWAY_CAPABILITY_FIXED_ENDPOINT,
})


def fanout_capabilities(record) -> set[str]:
    """Return normalized capabilities from a catalog record.

    Ollama-compatible catalogs may expose a scalar or collection at the top
    level, or place the same metadata under ``details``.  A non-empty
    top-level declaration is authoritative; otherwise the nested declaration
    is used.  The function is deliberately limited to explicit data so model
    routing callers can share one deterministic normalization rule.
    """
    record = record if isinstance(record, dict) else {}
    details = record.get("details") if isinstance(record.get("details"), dict) else {}

    def normalized(raw):
        if isinstance(raw, str):
            values = (raw,)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            return set()
        return {str(value).strip().casefold() for value in values if str(value).strip()}

    capabilities = normalized(record.get("capabilities"))
    return capabilities or normalized(details.get("capabilities"))
