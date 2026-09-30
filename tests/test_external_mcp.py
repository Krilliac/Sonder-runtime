import asyncio
import time
from types import SimpleNamespace

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.adapters.external_mcp import (
    ExternalMcpBridge,
    ExternalMcpError,
    ExternalMcpServerPolicy,
    ExternalMcpToolPolicy,
    policies_from_mapping,
    traits_from_mcp_annotations,
)
import sonder_runtime.adapters.external_mcp as external_mcp
from sonder_runtime.domain.tools.traits import TriState


class RecordingEvents:
    def __init__(self):
        self.events = []

    def emit(self, event_code, **fields):
        self.events.append((event_code, fields))


class FailingEvents:
    def emit(self, event_code, **fields):
        raise RuntimeError("observability unavailable")


class RecordingTransport:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    async def invoke(self, request):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class MutableCancellation:
    def __init__(self):
        self.cancelled = False

    def wait(self, timeout=None):
        return self.cancelled


def _context(*, cloud_allowed=False, timeout_seconds=5):
    return local_owner_context(
        correlation_id="test-correlation",
        source="mcp",
        cloud_allowed=cloud_allowed,
        timeout_seconds=timeout_seconds,
    )


def _server(**changes):
    values = {
        "name": "docs",
        "endpoint": "http://127.0.0.1:8765/mcp",
        "tools": (ExternalMcpToolPolicy("lookup"),),
    }
    values.update(changes)
    return ExternalMcpServerPolicy(**values)


def _bridge(transport, events, *, server=None, capabilities=frozenset({"read"}), secret=None):
    return ExternalMcpBridge(
        (server or _server(),),
        transport=transport,
        events=events,
        secret_resolver=lambda _name: secret,
        enabled_capabilities=capabilities,
    )


def test_structured_result_is_preferred_and_every_call_is_one_shot():
    events = RecordingEvents()
    transport = RecordingTransport([
        {
            "structuredContent": {"matches": []},
            "content": [{"type": "text", "text": "untrusted fallback"}],
            "isError": False,
        },
        {"structuredContent": {}, "content": [], "isError": False},
    ])
    server = _server(credential_env="SONDER_TEST_MCP_TOKEN")
    bridge = _bridge(transport, events, server=server, secret="super-secret-token")

    first = asyncio.run(bridge.call("docs", "lookup", {"q": "x"}, context=_context()))
    second = asyncio.run(bridge.call("docs", "lookup", {"q": "y"}, context=_context()))

    assert first.value == {"matches": []}
    assert second.value == {}
    assert len(transport.requests) == 2
    assert transport.requests[0] is not transport.requests[1]
    assert transport.requests[0].resolved_addresses == ("127.0.0.1",)
    assert transport.requests[0].credential == "super-secret-token"
    assert "super-secret-token" not in repr(transport.requests[0])
    assert bridge.safe_manifest()["servers"][0]["credential_configured"] is True
    assert "SONDER_TEST_MCP_TOKEN" not in repr(bridge.safe_manifest())
    assert first.receipt.structured is True
    assert events.events[-1][1]["detail"]["ok"] is True
    assert "q" not in events.events[-1][1]["detail"]


@pytest.mark.parametrize("failure_stage", ["transport", "credential"])
def test_upstream_exception_secret_never_enters_logs(failure_stage, caplog):
    secret = "upstream-secret-value"
    transport = RecordingTransport([RuntimeError(secret)])
    server = _server(credential_env="SONDER_TEST_MCP_TOKEN")
    bridge = ExternalMcpBridge(
        (server,), transport=transport, events=RecordingEvents(),
        secret_resolver=(lambda _name: (_ for _ in ()).throw(RuntimeError(secret)))
        if failure_stage == "credential" else (lambda _name: "credential"),
    )
    with pytest.raises(ExternalMcpError):
        asyncio.run(bridge.call("docs", "lookup", {}, context=_context()))
    assert secret not in caplog.text


def test_allowlists_and_read_only_capability_default_fail_closed():
    events = RecordingEvents()
    transport = RecordingTransport([])
    write_tool = ExternalMcpToolPolicy(
        "update", read_only=False, capabilities=("read", "mutate")
    )
    bridge = _bridge(transport, events, server=_server(tools=(write_tool,)))

    with pytest.raises(ExternalMcpError, match="not allowed") as denied_server:
        asyncio.run(bridge.call("unknown", "update", {}, context=_context()))
    with pytest.raises(ExternalMcpError, match="not allowed") as denied_tool:
        asyncio.run(bridge.call("docs", "unknown", {}, context=_context()))
    with pytest.raises(ExternalMcpError, match="not enabled") as denied_capability:
        asyncio.run(bridge.call("docs", "update", {}, context=_context()))

    assert denied_server.value.code == "SERVER_NOT_ALLOWED"
    assert denied_tool.value.code == "TOOL_NOT_ALLOWED"
    assert denied_capability.value.code == "CAPABILITY_NOT_ENABLED"
    assert transport.requests == []
    assert events.events[0][1]["detail"]["server"] == "<denied>"
    assert [event[1]["detail"]["error_code"] for event in events.events] == [
        "SERVER_NOT_ALLOWED", "TOOL_NOT_ALLOWED", "CAPABILITY_NOT_ENABLED"
    ]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://169.254.169.254/mcp",
        "http://10.0.0.1/mcp",
        "http://0.0.0.0/mcp",
        "http://2130706433/mcp",
    ],
)
def test_ssrf_and_noncanonical_loopback_targets_are_blocked(endpoint):
    if endpoint == "http://2130706433/mcp":
        with pytest.raises(ValueError, match="non-canonical"):
            _server(endpoint=endpoint)
        return
    events = RecordingEvents()
    transport = RecordingTransport([])
    bridge = _bridge(transport, events, server=_server(endpoint=endpoint))

    with pytest.raises(ExternalMcpError) as failure:
        asyncio.run(bridge.call("docs", "lookup", {}, context=_context()))

    assert failure.value.code == "ENDPOINT_BLOCKED"
    assert transport.requests == []


def test_remote_https_requires_server_policy_and_request_cloud_consent():
    events = RecordingEvents()
    transport = RecordingTransport([{"structuredContent": {"ok": True}}])
    server = _server(endpoint="https://8.8.8.8/mcp", allow_remote=True)
    bridge = _bridge(transport, events, server=server)

    with pytest.raises(ExternalMcpError) as failure:
        asyncio.run(bridge.call("docs", "lookup", {}, context=_context()))
    result = asyncio.run(
        bridge.call("docs", "lookup", {}, context=_context(cloud_allowed=True))
    )

    assert failure.value.code == "REMOTE_NOT_CONSENTED"
    assert result.value == {"ok": True}
    assert transport.requests[0].resolved_addresses == ("8.8.8.8",)


def test_result_limit_upstream_error_and_transport_details_are_safe():
    events = RecordingEvents()
    transport = RecordingTransport([
        {"structuredContent": {"value": "x" * 100}, "isError": False},
        {"content": [{"type": "text", "text": "upstream detail"}], "isError": True},
        RuntimeError("Authorization: Bearer should-never-escape"),
    ])
    bridge = _bridge(transport, events, server=_server(max_result_bytes=40))

    expected = ["RESULT_TOO_LARGE", "UPSTREAM_TOOL_ERROR", "TRANSPORT_ERROR"]
    for code in expected:
        with pytest.raises(ExternalMcpError) as failure:
            asyncio.run(bridge.call("docs", "lookup", {}, context=_context()))
        assert failure.value.code == code
        assert "Bearer" not in str(failure.value)
    assert [event[1]["detail"]["error_code"] for event in events.events] == expected


def test_host_config_rejects_inline_secrets_and_unknown_fields():
    with pytest.raises(ValueError, match="credentials"):
        policies_from_mapping({
            "servers": [{
                "name": "bad",
                "endpoint": "https://example.com/mcp",
                "token": "inline-secret",
                "tools": [{"name": "read"}],
            }]
        })
    with pytest.raises(ValueError, match="unknown"):
        policies_from_mapping({
            "servers": [{
                "name": "bad",
                "endpoint": "https://example.com/mcp",
                "auto_discover": True,
                "tools": [{"name": "read"}],
            }]
        })


def test_host_config_builds_only_the_explicit_allowlist():
    policies = policies_from_mapping({
        "servers": [{
            "name": "docs",
            "endpoint": "http://127.0.0.1:8765/mcp",
            "credential_env": "SONDER_DOCS_MCP_TOKEN",
            "tools": [{"name": "lookup", "capabilities": ["read"]}],
        }]
    })

    assert [server.name for server in policies] == ["docs"]
    assert [tool.name for tool in policies[0].tools] == ["lookup"]
    assert policies[0].credential_env == "SONDER_DOCS_MCP_TOKEN"


def test_host_config_null_read_only_is_not_treated_as_omission():
    with pytest.raises(ValueError, match="read_only must be a Boolean"):
        policies_from_mapping({
            "servers": [{
                "name": "docs", "endpoint": "http://127.0.0.1:8765/mcp",
                "tools": [{"name": "lookup", "read_only": None}],
            }]
        })


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_remote_consent_requires_an_actual_boolean(value):
    with pytest.raises(ValueError, match="allow_remote must be a Boolean"):
        _server(allow_remote=value)


def test_deadline_covers_endpoint_and_credential_resolution(monkeypatch):
    original_resolve = external_mcp._resolve_endpoint

    def slow_resolve(endpoint):
        time.sleep(0.04)
        return original_resolve(endpoint)

    monkeypatch.setattr(external_mcp, "_resolve_endpoint", slow_resolve)
    transport = RecordingTransport([])
    bridge = _bridge(
        transport,
        RecordingEvents(),
        server=_server(timeout_seconds=0.01),
    )
    with pytest.raises(ExternalMcpError) as endpoint_failure:
        asyncio.run(bridge.call("docs", "lookup", {}, context=_context()))
    assert endpoint_failure.value.code == "TIMEOUT"
    assert transport.requests == []

    monkeypatch.setattr(external_mcp, "_resolve_endpoint", original_resolve)

    def slow_secret(_name):
        time.sleep(0.04)
        return "eventual-secret"

    bridge = ExternalMcpBridge(
        (_server(credential_env="SONDER_TEST_MCP_TOKEN", timeout_seconds=0.01),),
        transport=transport,
        events=RecordingEvents(),
        secret_resolver=slow_secret,
    )
    with pytest.raises(ExternalMcpError) as credential_failure:
        asyncio.run(bridge.call("docs", "lookup", {}, context=_context()))
    assert credential_failure.value.code == "TIMEOUT"
    assert transport.requests == []


def test_in_flight_transport_is_cancelled_when_context_token_trips():
    cancellation = MutableCancellation()
    transport_cancelled = {"value": False}
    transport_started = asyncio.Event()

    class BlockingTransport:
        async def invoke(self, request):
            transport_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                transport_cancelled["value"] = True
                raise

    bridge = _bridge(BlockingTransport(), RecordingEvents())

    async def exercise():
        context = local_owner_context(
            correlation_id="cancel-test",
            source="mcp",
            timeout_seconds=2,
            cancellation=cancellation,
        )
        call = asyncio.create_task(bridge.call("docs", "lookup", {}, context=context))
        await asyncio.wait_for(transport_started.wait(), timeout=1)
        cancellation.cancelled = True
        with pytest.raises(ExternalMcpError) as failure:
            await call
        assert failure.value.code == "CONTEXT_EXPIRED"

    asyncio.run(exercise())
    assert transport_cancelled["value"] is True


def test_result_is_discarded_when_transport_and_cancellation_finish_together():
    cancellation = MutableCancellation()

    class CancellingTransport:
        async def invoke(self, request):
            cancellation.cancelled = True
            return {"structuredContent": {"must_not_escape": True}}

    bridge = _bridge(CancellingTransport(), RecordingEvents())
    context = local_owner_context(
        correlation_id="cancel-race",
        source="mcp",
        timeout_seconds=2,
        cancellation=cancellation,
    )

    with pytest.raises(ExternalMcpError) as failure:
        asyncio.run(bridge.call("docs", "lookup", {}, context=context))

    assert failure.value.code == "CONTEXT_EXPIRED"


def test_observability_failure_does_not_change_completed_call_semantics():
    transport = RecordingTransport([{"structuredContent": {"ok": True}}])
    bridge = _bridge(transport, FailingEvents())

    result = asyncio.run(
        bridge.call("docs", "lookup", {}, context=_context())
    )

    assert result.value == {"ok": True}
    assert result.receipt.ok is True


def test_host_config_requires_an_object():
    with pytest.raises(ValueError, match="must be an object"):
        policies_from_mapping([])


def test_mcp_annotations_omit_hints_as_unknown_and_reject_malformed_values():
    omitted = traits_from_mcp_annotations({})
    assert omitted.read_only is TriState.UNKNOWN
    assert omitted.destructive is TriState.UNKNOWN
    assert omitted.idempotent is TriState.UNKNOWN
    assert omitted.open_world is TriState.UNKNOWN

    malformed = traits_from_mcp_annotations({"readOnlyHint": "true", "idempotentHint": 1})
    assert malformed.read_only is TriState.UNKNOWN
    assert malformed.idempotent is TriState.UNKNOWN


@pytest.mark.parametrize(
    "hint, expected",
    [(True, TriState.TRUE), (False, TriState.FALSE), (None, TriState.UNKNOWN)],
)
def test_each_mcp_hint_is_strictly_tri_state(hint, expected):
    for name in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        annotations = {} if hint is None else {name: hint}
        traits = traits_from_mcp_annotations(annotations)
        assert getattr(traits, {
            "readOnlyHint": "read_only", "destructiveHint": "destructive",
            "idempotentHint": "idempotent", "openWorldHint": "open_world",
        }[name]) is expected


def test_sdk_defaulted_annotations_are_unknown_when_not_set():
    annotations = SimpleNamespace(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
        model_fields_set=set(),
    )
    traits = traits_from_mcp_annotations(annotations)
    assert traits.read_only is TriState.UNKNOWN
    assert traits.destructive is TriState.UNKNOWN
    assert traits.idempotent is TriState.UNKNOWN
    assert traits.open_world is TriState.UNKNOWN


def test_external_annotations_are_advisory_and_do_not_grant_read_only_authority():
    writable = ExternalMcpToolPolicy("lookup", read_only=False, capabilities=("read", "mutate"))
    server = _server(tools=(writable,))
    bridge = _bridge(RecordingTransport([]), RecordingEvents(), server=server)
    assert bridge.ingest_tool_list("docs", {
        "tools": [{"name": "lookup", "annotations": {
            "readOnlyHint": True, "idempotentHint": True,
        }}]
    }) == 1
    traits = bridge.tool_traits("docs", "lookup")
    assert traits is not None
    # The host allowlist remains the authority and the external hint cannot
    # make an otherwise writable tool eligible for speculation.
    assert traits.host_declared is True
    assert traits.read_only is TriState.FALSE
    assert traits.idempotent is TriState.UNKNOWN


def test_external_negative_annotation_can_only_make_host_read_only_more_conservative():
    bridge = _bridge(RecordingTransport([]), RecordingEvents(), server=_server(
        tools=(ExternalMcpToolPolicy("lookup", read_only=True),),
    ))
    bridge.ingest_tool_list("docs", {
        "tools": [{"name": "lookup", "annotations": {"readOnlyHint": False}}]
    })
    traits = bridge.tool_traits("docs", "lookup")
    assert traits is not None
    assert traits.read_only is TriState.UNKNOWN
    assert traits.may_be_destructive


def test_omitted_host_read_only_is_unknown_until_explicitly_declared():
    bridge = _bridge(RecordingTransport([]), RecordingEvents(), server=_server())
    traits = bridge.tool_traits("docs", "lookup")
    assert traits is not None
    assert traits.read_only is TriState.UNKNOWN

    explicit = _bridge(
        RecordingTransport([]), RecordingEvents(),
        server=_server(tools=(ExternalMcpToolPolicy("lookup", read_only=True),)),
    )
    explicit_traits = explicit.tool_traits("docs", "lookup")
    assert explicit_traits is not None
    assert explicit_traits.read_only is TriState.TRUE


def test_server_annotation_trust_is_explicit_and_strict():
    with pytest.raises(ValueError, match="trust_annotations must be a Boolean"):
        _server(trust_annotations="true")
    trusted = _server(trust_annotations=True)
    bridge = _bridge(RecordingTransport([]), RecordingEvents(), server=trusted)
    bridge.ingest_tool_list("docs", {
        "tools": [{"name": "lookup", "annotations": {
            "readOnlyHint": True, "destructiveHint": False,
        }}]
    })
    traits = bridge.tool_traits("docs", "lookup")
    assert traits is not None and traits.host_declared is True
    assert traits.read_only is TriState.TRUE
    with pytest.raises(ValueError, match="trusted must be a Boolean"):
        traits_from_mcp_annotations({}, trusted="yes")


@pytest.mark.parametrize(
    "hint, expected",
    [("destructiveHint", TriState.TRUE), ("openWorldHint", TriState.TRUE)],
)
def test_advisory_risk_hints_can_only_tighten_unknown_or_safe_host_traits(hint, expected):
    server = _server(tools=(ExternalMcpToolPolicy("lookup", read_only=True),))
    bridge = _bridge(RecordingTransport([]), RecordingEvents(), server=server)
    bridge.ingest_tool_list("docs", {"tools": [{"name": "lookup", "annotations": {hint: True}}]})
    traits = bridge.tool_traits("docs", "lookup")
    assert traits is not None
    assert getattr(traits, {"destructiveHint": "destructive", "openWorldHint": "open_world"}[hint]) is expected


def test_trusted_hints_tighten_known_host_safety_but_cannot_override_mutation():
    safe = _server(
        trust_annotations=True,
        tools=(ExternalMcpToolPolicy("lookup", read_only=True),),
    )
    bridge = _bridge(RecordingTransport([]), RecordingEvents(), server=safe)
    bridge.ingest_tool_list("docs", {"tools": [{"name": "lookup", "annotations": {"readOnlyHint": False}}]})
    traits = bridge.tool_traits("docs", "lookup")
    assert traits is not None and traits.read_only is TriState.UNKNOWN

    unsafe_tool = ExternalMcpToolPolicy("lookup", read_only=False, capabilities=("read", "mutate"))
    unsafe = _bridge(
        RecordingTransport([]), RecordingEvents(),
        server=_server(tools=(unsafe_tool,), trust_annotations=True),
    )
    unsafe.ingest_tool_list("docs", {"tools": [{"name": "lookup", "annotations": {"readOnlyHint": True}}]})
    unsafe_traits = unsafe.tool_traits("docs", "lookup")
    assert unsafe_traits is not None and unsafe_traits.read_only is TriState.FALSE


def test_bridge_serializes_concurrent_external_calls_by_default():
    state = {"active": 0, "peak": 0}

    class OverlapDetectingTransport:
        async def invoke(self, request):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.01)
            state["active"] -= 1
            return {"structuredContent": {"ok": True}}

    bridge = _bridge(OverlapDetectingTransport(), RecordingEvents())

    async def exercise():
        await asyncio.gather(
            bridge.call("docs", "lookup", {}, context=_context()),
            bridge.call("docs", "lookup", {}, context=_context()),
        )

    asyncio.run(exercise())
    assert state["peak"] == 1
