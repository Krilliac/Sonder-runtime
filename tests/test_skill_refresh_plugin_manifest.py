from sonder_runtime.application.plugin_manifest import PluginManifest, validate_manifest
from sonder_runtime.application.skill_refresh import SkillRevision, SkillTrust, refresh_decision


def test_skill_refresh_rejects_untrusted_and_tracks_digest_changes():
    candidate = SkillRevision("demo", "new", "1", SkillTrust.PROJECT)
    assert refresh_decision(None, candidate) == "install"
    assert refresh_decision(candidate, candidate) == "unchanged"
    assert not SkillRevision("bad", "x", "1", SkillTrust.UNTRUSTED).refresh_allowed()


def test_plugin_manifest_is_declarative_and_permission_bounded():
    manifest = PluginManifest("demo", "1", ("tool",), ("network",), compatible_runtime="2")
    assert validate_manifest(manifest, runtime_version="2", granted_permissions={"network"}) == (True, ())
    assert validate_manifest(manifest, runtime_version="2", granted_permissions=set())[0] is False
