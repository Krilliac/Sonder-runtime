"""A small transport-neutral SDK client and permission-preserving local seam."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import uuid4

from ...application.tools.facade import ToolApplicationFacade
from ...application.tools.gateway_contract import ToolGatewayRequest
from .contracts import SdkContractError, SdkError, SdkRequest, SdkResult
from .discovery import CapabilitySnapshot


class SdkTransport(Protocol):
    def discover(self) -> Mapping[str, Any]: ...
    def invoke(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CallableTransport:
    """Adapt two callables without adding a network or provider dependency."""

    def __init__(
        self,
        discover: Callable[[], Mapping[str, Any]],
        invoke: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        if not callable(discover) or not callable(invoke):
            raise TypeError("SDK transport callables are required")
        self._discover = discover
        self._invoke = invoke

    def discover(self) -> Mapping[str, Any]:
        return self._discover()

    def invoke(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._invoke(request)


class GatewayTransport:
    """Expose a composed gateway while keeping authority out of SDK payloads.

    ``request_factory`` is host-owned composition.  It must resolve the
    authenticated principal, workspace scope, permission effects, approvals,
    deadlines, and cancellation independently of the SDK request.
    """

    def __init__(
        self,
        facade: ToolApplicationFacade,
        request_factory: Callable[[SdkRequest], ToolGatewayRequest],
        *,
        runtime_version: str,
    ) -> None:
        if not isinstance(facade, ToolApplicationFacade) or not callable(request_factory):
            raise TypeError("GatewayTransport requires a tool facade and request factory")
        self._facade = facade
        self._request_factory = request_factory
        if not isinstance(runtime_version, str) or not runtime_version.strip():
            raise TypeError("GatewayTransport requires a runtime_version")
        self._runtime_version = runtime_version

    def discover(self) -> Mapping[str, Any]:
        return CapabilitySnapshot.from_catalogs(
            self._facade.catalogs, runtime_version=self._runtime_version
        ).as_dict()

    def invoke(self, value: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = "invalid-request"
        try:
            request = SdkRequest.from_dict(value)
            request_id = request.request_id
            snapshot = CapabilitySnapshot.from_catalogs(
                self._facade.catalogs, runtime_version=self._runtime_version
            )
            if request.catalog_digest != snapshot.catalog_digest:
                return SdkResult.failure(
                    request.request_id,
                    SdkError(
                        "STALE_CATALOG",
                        "SDK catalog digest differs from the runtime",
                        details={"expected_digest": snapshot.catalog_digest},
                    ),
                ).as_dict()
            snapshot.require_tool(request.tool).validate_arguments(request.arguments)
            gateway_request = self._request_factory(request)
            if not isinstance(gateway_request, ToolGatewayRequest):
                raise SdkContractError("request_factory must return ToolGatewayRequest")
            # The host may add authority context, but it may not rewrite the
            # signed/discovered call identity or arguments.
            if (
                gateway_request.request_id != request.request_id
                or gateway_request.tool_name != request.tool
                or dict(gateway_request.arguments) != dict(request.arguments)
            ):
                raise SdkContractError("request_factory changed SDK request identity or arguments")
            receipt = self._facade.execute(gateway_request)
            metadata = {
                "approval_required": receipt.approval_required,
                "duration_ms": receipt.duration_ms,
                "redaction_applied": receipt.redaction_applied,
                "schema_version": receipt.schema_version,
            }
            if receipt.success:
                result = SdkResult.success(request.request_id, receipt.output, metadata=metadata)
            else:
                result = SdkResult.failure(
                    request.request_id,
                    SdkError(receipt.error_code or "TOOL_FAILED", receipt.error or "tool execution failed"),
                    metadata=metadata,
                )
            return result.as_dict()
        except Exception as exc:
            return SdkResult.failure(request_id, SdkError.from_exception(exc)).as_dict()


class SonderClient:
    """Typed client that discovers and validates before every transport call."""

    def __init__(self, transport: SdkTransport) -> None:
        if not callable(getattr(transport, "discover", None)) or not callable(getattr(transport, "invoke", None)):
            raise TypeError("transport must implement discover and invoke")
        self._transport = transport
        self._snapshot: CapabilitySnapshot | None = None

    @property
    def capabilities(self) -> CapabilitySnapshot | None:
        return self._snapshot

    def refresh(self) -> CapabilitySnapshot:
        self._snapshot = CapabilitySnapshot.from_dict(self._transport.discover())
        self._snapshot.negotiate(("1.0",))
        return self._snapshot

    def call(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> SdkResult:
        snapshot = self._snapshot or self.refresh()
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, Mapping):
            raise SdkContractError("tool arguments must be an object")
        snapshot.require_tool(tool).validate_arguments(arguments)
        request = SdkRequest(
            request_id=request_id or f"sdk-{uuid4().hex}",
            tool=tool,
            arguments=arguments,
            catalog_digest=snapshot.catalog_digest,
        )
        result = SdkResult.from_dict(self._transport.invoke(request.as_dict()))
        if result.request_id != request.request_id:
            raise SdkContractError("SDK transport returned a mismatched request_id")
        return result


__all__ = ["CallableTransport", "GatewayTransport", "SdkTransport", "SonderClient"]
