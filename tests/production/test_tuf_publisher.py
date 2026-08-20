"""SPEC-4 publisher: TUF repo init/publish and client verification."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The publisher and the client's TUF verify path use the optional update
# dependencies (requirements-update.txt). Skip cleanly where they are absent
# (e.g. base CI) rather than erroring at collection.
pytest.importorskip("tuf")
pytest.importorskip("securesystemslib")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import sonder_runtime.adapters.updates.service as sonder_updates
import tuf_repo
from sonder_runtime.adapters.updates.service import BundleManifest, TrustError, verify_bundle_trust

pytestmark = pytest.mark.integration


def _mini_src(tmp_path):
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (src / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return src


@pytest.fixture()
def published(tmp_path):
    tuf_repo.init_repo(tmp_path / "repo")
    sonder_updates.build_bundle(
        _mini_src(tmp_path), tmp_path / "built", version="1.4.0",
    )
    info = tuf_repo.build_offline_bundle(
        tmp_path / "repo", tmp_path / "built", tmp_path / "offline",
    )
    return tmp_path, Path(info["offline_bundle"])


def test_init_creates_all_role_metadata(tmp_path):
    info = tuf_repo.init_repo(tmp_path / "repo")
    md = tmp_path / "repo" / "metadata"
    for role in ("root", "targets", "snapshot", "timestamp"):
        assert (md / f"{role}.json").is_file()
    # Thresholds match SPEC-4 section 1.
    assert info["roles"]["root"]["threshold"] == 2
    assert info["roles"]["targets"]["threshold"] == 2


def test_root_carries_threshold_and_multiple_keys(tmp_path):
    tuf_repo.init_repo(tmp_path / "repo")
    root = json.loads(
        (tmp_path / "repo" / "metadata" / "root.json").read_text()
    )
    roles = root["signed"]["roles"]
    assert roles["root"]["threshold"] == 2
    assert len(roles["root"]["keyids"]) == 3
    # Root is signed by its threshold of keys.
    assert len(root["signatures"]) >= 2


def test_signed_bundle_verifies_through_client(published):
    _, bundle = published
    manifest = BundleManifest.load(bundle / "manifest.json")
    assert verify_bundle_trust(bundle, manifest, allow_unverified=False) == "tuf"


def test_tampered_target_is_rejected(published):
    _, bundle = published
    manifest = BundleManifest.load(bundle / "manifest.json")
    target = next((bundle / "targets").glob("sonder-engine-*.tar.gz"))
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    with pytest.raises(TrustError):
        verify_bundle_trust(bundle, manifest, allow_unverified=False)


def test_forged_metadata_is_rejected(published):
    _, bundle = published
    manifest = BundleManifest.load(bundle / "manifest.json")
    # Corrupt every targets signature so valid ones drop below the 2-of-3
    # threshold; TUF must then refuse the metadata.
    targets_path = bundle / "metadata" / "targets.json"
    forged = json.loads(targets_path.read_text())
    for sig in forged["signatures"]:
        sig["sig"] = "00" * 32
    targets_path.write_text(json.dumps(forged))
    with pytest.raises(TrustError):
        verify_bundle_trust(bundle, manifest, allow_unverified=False)


def test_below_threshold_targets_signatures_rejected(published):
    _, bundle = published
    manifest = BundleManifest.load(bundle / "manifest.json")
    # Drop to a single valid signature (threshold is 2): must be rejected.
    targets_path = bundle / "metadata" / "targets.json"
    forged = json.loads(targets_path.read_text())
    for sig in forged["signatures"][1:]:
        sig["sig"] = "00" * 32
    targets_path.write_text(json.dumps(forged))
    with pytest.raises(TrustError):
        verify_bundle_trust(bundle, manifest, allow_unverified=False)


def test_missing_target_in_metadata_is_rejected(tmp_path):
    # Build a bundle whose manifest names an archive the repo never signed.
    tuf_repo.init_repo(tmp_path / "repo")
    sonder_updates.build_bundle(
        _mini_src(tmp_path), tmp_path / "built", version="2.0.0",
    )
    info = tuf_repo.build_offline_bundle(
        tmp_path / "repo", tmp_path / "built", tmp_path / "offline",
    )
    bundle = Path(info["offline_bundle"])
    manifest_raw = json.loads((bundle / "manifest.json").read_text())
    manifest_raw["archive"]["name"] = "sonder-engine-9.9.9.tar.gz"
    (bundle / "manifest.json").write_text(json.dumps(manifest_raw))
    manifest = BundleManifest.load(bundle / "manifest.json")
    with pytest.raises(TrustError):
        verify_bundle_trust(bundle, manifest, allow_unverified=False)


def test_end_to_end_install_from_signed_bundle(published, monkeypatch, tmp_path):
    # A TUF-signed bundle installs through the real update engine WITHOUT the
    # unsigned gate (allow_unverified=False), proving the publisher and the
    # engine agree on the trust contract.
    repo_root, bundle = published
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
    plan = manager.import_offline(bundle, allow_unverified=False)
    assert plan["status"] == "available", plan
