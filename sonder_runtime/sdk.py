"""Public import surface for the dependency-free Sonder developer SDK.

The implementation lives under :mod:`sonder_runtime.interfaces.sdk` so it
remains a thin protocol adapter over application-owned contracts.
"""
from .interfaces.sdk import (
    CallableTransport,
    CapabilitySnapshot,
    DISCOVERY_SCHEMA,
    GatewayTransport,
    PLUGIN_MANIFEST_JSON_SCHEMA,
    PLUGIN_MANIFEST_SCHEMA_VERSION,
    PluginCompatibility,
    PluginDependency,
    SDK_PROTOCOL_VERSION,
    SUPPORTED_SDK_PROTOCOL_VERSIONS,
    SdkContractError,
    SdkDiagnostic,
    SdkError,
    SdkPluginManifest,
    SdkRequest,
    SdkResult,
    SdkTransport,
    SonderClient,
    ToolCapability,
)

__all__ = [
    "CallableTransport", "CapabilitySnapshot", "DISCOVERY_SCHEMA", "GatewayTransport",
    "PLUGIN_MANIFEST_JSON_SCHEMA", "PLUGIN_MANIFEST_SCHEMA_VERSION", "PluginCompatibility",
    "PluginDependency", "SDK_PROTOCOL_VERSION", "SUPPORTED_SDK_PROTOCOL_VERSIONS",
    "SdkContractError", "SdkDiagnostic", "SdkError", "SdkPluginManifest", "SdkRequest",
    "SdkResult", "SdkTransport", "SonderClient", "ToolCapability",
]
