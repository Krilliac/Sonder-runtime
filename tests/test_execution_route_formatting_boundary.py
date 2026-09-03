"""Boundary tests for the execution_route_formatting packaged module."""

from sonder_runtime.domain.execution_route_formatting import execution_route_header


def test_basic_header():
    result = execution_route_header("workbench", "user", "manual request")
    assert "sonder execution decision" in result
    assert "foreground workbench" in result
    assert "  source: user" in result
    assert "  reason: manual request" in result
    assert "  boundary:" in result


def test_unknown_mode_passed_through():
    result = execution_route_header("custom", "test", "reason")
    assert "  mode: custom" in result


def test_confidence_formatting():
    result = execution_route_header("workbench", "test", "reason", confidence=0.85)
    assert "  confidence: 85%" in result


def test_no_confidence_omitted():
    result = execution_route_header("workbench", "test", "reason")
    assert "confidence" not in result


def test_tier_shown_when_in_local_tiers():
    tiers = {"fast": "llama3:fast"}
    result = execution_route_header(
        "workbench", "test", "reason",
        tier="fast", tiers_map=tiers, local_tiers=("fast", "slow"),
    )
    assert "  tier: fast -> llama3:fast" in result


def test_tier_hidden_when_not_local():
    tiers = {"fast": "llama3:fast"}
    result = execution_route_header(
        "workbench", "test", "reason",
        tier="fast", tiers_map=tiers, local_tiers=("slow",),
    )
    assert "tier:" not in result


def test_unmapped_tier_shows_placeholder():
    result = execution_route_header(
        "workbench", "test", "reason",
        tier="unknown", tiers_map={}, local_tiers=("unknown",),
    )
    assert "  tier: unknown -> (unmapped)" in result


def test_compatibility_delegate_exists():
    import server
    result = server._execution_route_header("workbench", "src", "reason")
    assert "sonder execution decision" in result
