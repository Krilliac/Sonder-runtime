from sonder_runtime.application.protocol.mcp_compatibility import (
    LegacyMcpContract,
    McpCompatibility,
    McpNegotiationError,
    SubscriptionNotificationRouter,
)


def test_negotiation_prefers_supported_v2_and_intersects_capabilities():
    compatibility = McpCompatibility(capabilities=("notifications", "tools"))
    result = compatibility.negotiate(("2.0", "1.0"), client_capabilities=("tools", "other"))
    assert result.agreed_version == "2.0"
    assert result.capabilities == ("tools",)


def test_legacy_negotiation_requires_declared_contract():
    legacy = LegacyMcpContract("legacy-stream", "1.0", ("notifications",))
    compatibility = McpCompatibility(legacy_contracts=(legacy,))
    result = compatibility.negotiate(("1.0",), legacy_contract="legacy-stream")
    assert result.legacy_contract == legacy
    try:
        compatibility.negotiate(("0.1",), legacy_contract="unknown")
    except McpNegotiationError:
        pass
    else:
        raise AssertionError("unknown legacy contracts must fail closed")


def test_notification_router_delivers_only_subscribed_local_events():
    router = SubscriptionNotificationRouter()
    received = []
    router.subscribe("client", "job.updated", lambda event, payload: received.append((event, payload)))
    assert router.publish("job.created", {"id": "1"}) == 0
    assert router.publish("job.updated", {"id": "1"}) == 1
    assert received == [("job.updated", {"id": "1"})]
    router.unsubscribe("client")
    assert router.subscriber_count() == 0


def test_contract_is_data_only_and_router_supports_subscription_removal():
    contract = LegacyMcpContract("legacy", "1.0")
    assert contract.capabilities == ()
    router = SubscriptionNotificationRouter()
    router.subscribe("a", "event", lambda *_: None)
    router.subscribe("b", "event", lambda *_: None)
    assert router.subscriber_count("event") == 2
    router.unsubscribe("a", "event")
    assert router.subscriber_count("event") == 1

