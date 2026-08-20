from sonder_runtime.adapters.runtime_readiness_formatting import format_model_readiness


def test_format_model_readiness_reports_core_memory_and_optional_states():
    assert format_model_readiness({
        "local_models": {"fast": "sonder:latest", "code": "sonder:latest",
                          "general": "missing-general:latest", "reasoning": "",
                          "vision": "missing-vision:latest"},
        "embedding_model": "nomic-embed-text",
        "missing_models": ["missing-general:latest", "nomic-embed-text",
                           "missing-vision:latest"],
    }) == [
        "  readiness:",
        "    local chat/code: requires general=missing-general:latest",
        "    semantic memory: requires embedding model nomic-embed-text",
        "    reasoning: not configured (optional)",
        "    vision: requires missing-vision:latest",
    ]


def test_format_model_readiness_reports_capability_errors():
    assert format_model_readiness({
        "local_models": {"fast": "embed", "code": "chat", "general": "chat"},
        "embedding_model": "chat",
        "capability_errors": {"fast": "embedding-only capability",
                               "embedding": "does not declare embedding capability"},
    })[:3] == [
        "  readiness:",
        "    local chat/code: requires fast=embed (embedding-only capability)",
        "    semantic memory: requires embedding model chat (does not declare embedding capability)",
    ]


def test_format_model_readiness_handles_unknown_inventory():
    assert format_model_readiness({"inventory_error": "offline"}) == [
        "  readiness: unknown (local model inventory unavailable)",
    ]
