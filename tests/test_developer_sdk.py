"""Compatibility, schema, manifest, and permission tests for the public SDK."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.application.errors import InvalidInput
from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.tools.facade import ToolApplicationFacade
from sonder_runtime.application.tools.gateway_contract import (
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs
from sonder_runtime.application.tools.resource_policy import Decision, PolicyRule, ResourcePolicy
from sonder_runtime.platform.version import VERSION
from sonder_runtime.sdk import (
    GatewayTransport,
    PLUGIN_MANIFEST_JSON_SCHEMA,
    SDK_PROTOCOL_VERSION,
    CapabilitySnapshot,
    SdkContractError,
    SdkError,
    SdkPluginManifest,
    SdkRequest,
    SdkResult,
    SonderClient,
)


SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1}},
    "required": ["text"],
    "additionalProperties": False,
}


class EchoExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, descriptor, call, context, execution_class):
        del descriptor, context, execution_class
        self.calls += 1
        return ToolExecutionResult(call.tool_name, True, {"echo": call.arguments["text"]})


def _request_factory(request: SdkRequest) -> ToolGatewayRequest:
    return ToolGatewayRequest(
        request_id=request.request_id,
        tool_name=request.tool,
        arguments=request.arguments,
        scope=ToolScope("sdk-user"),
        permission=ToolPermission(),
    )


def _facade(*, allowed: bool):
    registry = InMemoryToolRegistry((ToolDescriptor("echo", "Echo text", SCHEMA),))
    executor = EchoExecutor()
    rules = (PolicyRule("sdk-echo", Decision.ALLOW, tool="echo"),) if allowed else ()
    facade = ToolApplicationFacade.compose(
        registry, executor, policy=ResourcePolicy(rules)
    )
    return facade, executor


def test_versioned_request_and_result_contracts_are_strict_and_json_safe():
    request = SdkRequest("sdk-1", "echo", {"text": "hello"}, "a" * 64)
    assert SdkRequest.from_dict(request.as_dict()) == request
    result = SdkResult.success("sdk-1", {"text": "hello"})
    assert SdkResult.from_dict(result.as_dict()) == result
    assert request.version == SDK_PROTOCOL_VERSION

    with pytest.raises(SdkContractError, match="unknown field"):
        SdkRequest.from_dict({**request.as_dict(), "permissions": ["host"]})
    with pytest.raises(SdkContractError, match="arguments must be an object"):
        SdkRequest.from_dict({**request.as_dict(), "arguments": []})
    with pytest.raises(SdkContractError, match="JSON serializable"):
        SdkResult.success("sdk-1", object())
    assert SdkError.from_exception(RuntimeError("secret detail")).message == (
        "the SDK request failed inside the runtime"
    )


def test_discovery_is_generated_with_mcp_parity_and_validates_arguments():
    catalogs = GeneratedCatalogs.generate(
        InMemoryToolRegistry((ToolDescriptor("echo", "Echo text", SCHEMA),)),
        event_kinds=[],
    )
    snapshot = CapabilitySnapshot.from_catalogs(catalogs, runtime_version=VERSION)
    assert snapshot.catalog_digest == catalogs.digest
    assert snapshot.authorization == "runtime-evaluated"
    assert "2025-11-25" in snapshot.mcp_versions
    assert CapabilitySnapshot.from_dict(snapshot.as_dict()) == snapshot
    snapshot.require_tool("echo").validate_arguments({"text": "ok"})
    with pytest.raises(InvalidInput, match="unknown field"):
        snapshot.require_tool("echo").validate_arguments({"text": "ok", "permission": "allow"})


def test_client_uses_gateway_and_preserves_default_deny_permission_gate():
    denied_facade, denied_executor = _facade(allowed=False)
    denied = SonderClient(GatewayTransport(
        denied_facade, _request_factory, runtime_version=VERSION
    )).call(
        "echo", {"text": "blocked"}, request_id="sdk-denied"
    )
    assert not denied.ok
    assert denied.error is not None and denied.error.code == "FORBIDDEN"
    assert denied_executor.calls == 0

    allowed_facade, allowed_executor = _facade(allowed=True)
    allowed = SonderClient(GatewayTransport(
        allowed_facade, _request_factory, runtime_version=VERSION
    )).call(
        "echo", {"text": "hello"}, request_id="sdk-allowed"
    )
    assert allowed.ok and allowed.output == {"echo": "hello"}
    assert allowed.metadata["redaction_applied"] is False
    assert allowed_executor.calls == 1

    with pytest.raises(SdkContractError, match="arguments must be an object"):
        SonderClient(GatewayTransport(
            allowed_facade, _request_factory, runtime_version=VERSION
        )).call("echo", [])


def test_gateway_rejects_stale_catalog_before_execution():
    facade, executor = _facade(allowed=True)
    response = GatewayTransport(facade, _request_factory, runtime_version=VERSION).invoke(
        SdkRequest("sdk-stale", "echo", {"text": "hello"}, "0" * 64).as_dict()
    )
    result = SdkResult.from_dict(response)
    assert not result.ok
    assert result.error is not None and result.error.code == "STALE_CATALOG"
    assert executor.calls == 0


def test_plugin_manifest_is_strict_versioned_and_never_grants_permissions():
    raw = {
        "schema_version": "1",
        "name": "sample-plugin",
        "publisher": "example-org",
        "version": "1.2.3",
        "minimum_runtime": "0.9.0",
        "maximum_runtime_exclusive": "1.0.0",
        "capabilities": ["tools.echo"],
        "permissions": ["network.read"],
        "dependencies": [{"name": "example-org.base", "version": "2.0.0", "required": False}],
    }
    manifest = SdkPluginManifest.from_dict(raw)
    denied = manifest.compatibility(
        runtime_version="0.9.0.dev0",
        granted_permissions=set(),
        available_dependencies={"example-org.base"},
        supported_capabilities={"tools.echo"},
    )
    assert not denied.compatible
    assert [issue.code for issue in denied.issues] == ["PLUGIN_PERMISSION_DENIED"]
    extension = manifest.to_extension_manifest()
    assert extension.permissions == ("network.read",)
    assert extension.dependencies[0].required is False
    assert SdkPluginManifest.from_dict(manifest.as_dict()) == manifest
    json.dumps(PLUGIN_MANIFEST_JSON_SCHEMA)

    with pytest.raises(SdkContractError, match="unknown field"):
        SdkPluginManifest.from_dict({**raw, "auto_approve": True})
    with pytest.raises(SdkContractError, match="list fields must be arrays"):
        SdkPluginManifest.from_dict({**raw, "capabilities": "tools.echo"})


def test_gateway_request_factory_cannot_rewrite_discovered_call():
    facade, executor = _facade(allowed=True)

    def rewrite(request: SdkRequest) -> ToolGatewayRequest:
        result = _request_factory(request)
        return ToolGatewayRequest(
            result.request_id, result.tool_name, {"text": "rewritten"},
            result.scope, result.permission,
        )

    client = SonderClient(GatewayTransport(facade, rewrite, runtime_version=VERSION))
    result = client.call("echo", {"text": "original"}, request_id="sdk-rewrite")
    assert not result.ok
    assert result.error is not None and result.error.code == "SDK_CONTRACT_INVALID"
    assert executor.calls == 0
