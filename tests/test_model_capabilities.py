import server

from sonder_runtime.domain.model_capabilities import (
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_EMBEDDINGS,
    GATEWAY_CAPABILITY_FIXED_ENDPOINT,
    GATEWAY_CAPABILITY_TIERED_ROUTING,
    KNOWN_GATEWAY_CAPABILITIES,
    fanout_capabilities,
)


def test_capabilities_normalize_scalar_and_casefold_values():
    assert fanout_capabilities({"capabilities": " CHAT "}) == {"chat"}


def test_capabilities_use_nested_metadata_when_top_level_is_empty():
    assert fanout_capabilities({
        "capabilities": [],
        "details": {"capabilities": ["Embedding", " vision "]},
    }) == {"embedding", "vision"}


def test_capabilities_ignore_malformed_records_and_values():
    assert fanout_capabilities(None) == set()
    assert fanout_capabilities({"capabilities": {"not": "a list"}}) == set()


def test_server_keeps_identity_preserving_compatibility_alias():
    assert server._fanout_capabilities is fanout_capabilities


def test_known_gateway_capabilities_cover_the_declared_constants():
    assert KNOWN_GATEWAY_CAPABILITIES == {
        GATEWAY_CAPABILITY_CHAT,
        GATEWAY_CAPABILITY_EMBEDDINGS,
        GATEWAY_CAPABILITY_TIERED_ROUTING,
        GATEWAY_CAPABILITY_FIXED_ENDPOINT,
    }
    # tiered-routing and fixed-endpoint describe mutually exclusive ways a
    # gateway resolves model identity; no adapter should ever declare both.
    assert GATEWAY_CAPABILITY_TIERED_ROUTING != GATEWAY_CAPABILITY_FIXED_ENDPOINT
