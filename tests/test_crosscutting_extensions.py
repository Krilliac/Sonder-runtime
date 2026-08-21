from sonder_runtime.application.extensions.quarantine import QuarantineReason, QuarantineRegistry
from sonder_runtime.domain.extensions.manifest import (
    CleanupPolicy, ExtensionDependency, ExtensionHealth, ExtensionIdentity, ExtensionManifest,
)
import pytest


def _manifest(**overrides):
    values = dict(
        identity=ExtensionIdentity("search", "sonder"), version="1.2.3", protocol="extension-v1",
        dependencies=(ExtensionDependency("core"),), permissions=("read",),
        health=ExtensionHealth(crash_limit=2), cleanup=CleanupPolicy("unload", False),
    )
    values.update(overrides)
    return ExtensionManifest(**values)


def test_manifest_is_typed_and_compatible_with_boundary():
    manifest = _manifest()
    assert manifest.extension_id == "sonder.search"
    assert manifest.is_compatible(protocol="extension-v1", available_dependencies={"core"}, granted_permissions={"read"})
    assert manifest.from_plugin_manifest  # compatibility seam remains available


def test_manifest_digest_is_order_stable_and_dependency_identity_is_bounded():
    first = _manifest(
        dependencies=(ExtensionDependency("zeta"), ExtensionDependency("alpha")),
        permissions=("write", "read"),
    )
    second = _manifest(
        dependencies=(ExtensionDependency("alpha"), ExtensionDependency("zeta")),
        permissions=("read", "write"),
    )
    assert first.digest() == second.digest()
    with pytest.raises(ValueError, match="dependencies must be unique"):
        _manifest(dependencies=(ExtensionDependency("core"), ExtensionDependency("core")))
    with pytest.raises(ValueError, match="cannot depend on itself"):
        _manifest(dependencies=(ExtensionDependency("sonder.search"),))


def test_incompatibility_is_quarantined_with_cleanup_metadata():
    manifest = _manifest()
    decision = QuarantineRegistry().evaluate(
        manifest, protocol="extension-v2", available_dependencies=set(), granted_permissions=set()
    )
    assert decision.quarantined
    assert "protocol-incompatible" in decision.reasons
    assert "missing-dependency:core" in decision.reasons
    assert decision.cleanup_action == "unload" and not decision.retain_state


def test_repeated_crashes_quarantine_only_at_manifest_threshold():
    manifest = _manifest()
    registry = QuarantineRegistry()
    assert not registry.record_crash(manifest).quarantined
    decision = registry.record_crash(manifest)
    assert decision.quarantined
    assert decision.reasons == (QuarantineReason.REPEATED_CRASH.value,)
    assert registry.crash_count(manifest.extension_id) == 2
