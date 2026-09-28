"""The TUF trust anchor for offline bundles must come from outside the bundle.

Bootstrapping from the bundle's own ``metadata/root.json`` let whoever built a
bundle choose the keys that verify it: a self-signed bundle from a freshly
generated repository passed verification and could reach install (Codex
security scan, 2026-09-27).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.updates.service import (
    BundleManifest,
    TrustError,
    build_bundle,
    verify_bundle_trust,
)

pytestmark = pytest.mark.integration


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


def _publish(tmp_path: Path, tuf_repo_mod, name: str) -> tuple[Path, Path]:
    """A signed offline bundle from a fresh repository; returns (bundle, root)."""
    base = tmp_path / name
    tuf_repo_mod.init_repo(base / "repo")
    build_bundle(_mini_src(base), base / "built", version="1.4.0")
    info = tuf_repo_mod.build_offline_bundle(base / "repo", base / "built", base / "offline")
    return Path(info["offline_bundle"]), base / "repo" / "metadata" / "root.json"


def _verify(bundle: Path) -> str:
    return verify_bundle_trust(
        bundle, BundleManifest.load(bundle / "manifest.json"), allow_unverified=False,
    )


def test_bundle_signed_by_the_trusted_root_passes(tmp_path, tuf_repo_mod, monkeypatch):
    bundle, root = _publish(tmp_path, tuf_repo_mod, "vendor")
    monkeypatch.setenv("SONDER_UPDATE_TRUSTED_ROOT", str(root))
    assert _verify(bundle) == "tuf"


def test_self_signed_bundle_from_another_root_is_refused(tmp_path, tuf_repo_mod, monkeypatch):
    _vendor_bundle, vendor_root = _publish(tmp_path, tuf_repo_mod, "vendor")
    forged, _attacker_root = _publish(tmp_path, tuf_repo_mod, "attacker")
    monkeypatch.setenv("SONDER_UPDATE_TRUSTED_ROOT", str(vendor_root))
    with pytest.raises(TrustError):
        _verify(forged)


def test_signed_bundle_without_a_configured_trusted_root_is_refused(
    tmp_path, tuf_repo_mod, monkeypatch,
):
    bundle, _root = _publish(tmp_path, tuf_repo_mod, "vendor")
    monkeypatch.delenv("SONDER_UPDATE_TRUSTED_ROOT", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "empty-home"))
    with pytest.raises(TrustError, match="trusted"):
        _verify(bundle)
