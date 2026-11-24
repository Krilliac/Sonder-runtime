"""Unit tests for tools/tuf_repo.py path traversal and keystore encryption hardening (H-02, H-03).

Runs even when optional ``tuf`` / ``securesystemslib`` packages are absent in
the default dev venv by exercising ``tools/tuf_repo.py`` with real
``cryptography`` Ed25519 keys and lightweight in-memory stubs when needed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import warnings
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

pytestmark = pytest.mark.unit


def _load_tuf_repo_module():
    """Import tools/tuf_repo.py, installing minimal stubs only if tuf is absent."""
    repo_root = Path(__file__).resolve().parents[1]
    tuf_path = repo_root / "tools" / "tuf_repo.py"
    saved: dict[str, types.ModuleType | None] = {}
    need_stubs = False
    try:
        import tuf.api.metadata  # noqa: F401
        import securesystemslib.signer  # noqa: F401
    except ImportError:
        need_stubs = True

    if need_stubs:
        class _FakePublicKey:
            def __init__(self, keyid: str):
                self.keyid = keyid

        class _FakeCryptoSigner:
            def __init__(self, private_key):
                self._private_key = private_key
                self.public_key = _FakePublicKey("fake-keyid")

            @classmethod
            def generate_ed25519(cls):
                return cls(ed25519.Ed25519PrivateKey.generate())

        sslib_mod = types.ModuleType("securesystemslib")
        sslib_signer = types.ModuleType("securesystemslib.signer")
        sslib_signer.CryptoSigner = _FakeCryptoSigner
        tuf_mod = types.ModuleType("tuf")
        tuf_api = types.ModuleType("tuf.api")
        tuf_meta = types.ModuleType("tuf.api.metadata")
        for attr in ("Metadata", "MetaFile", "Role", "Root", "Snapshot", "TargetFile", "Targets", "Timestamp"):
            setattr(tuf_meta, attr, type(attr, (), {}))

        stubs = {
            "securesystemslib": sslib_mod,
            "securesystemslib.signer": sslib_signer,
            "tuf": tuf_mod,
            "tuf.api": tuf_api,
            "tuf.api.metadata": tuf_meta,
        }
        for name, mod in stubs.items():
            saved[name] = sys.modules.get(name)
            sys.modules[name] = mod

    try:
        spec = importlib.util.spec_from_file_location("_tuf_repo_under_test", tuf_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for name, orig in saved.items():
            if orig is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = orig


@pytest.mark.parametrize("bad_name", [
    "",
    ".",
    "..",
    "../outside.tar.gz",
    "..\\outside.tar.gz",
    "a/b.tar.gz",
    "a\\b.tar.gz",
    "C:evil.tar.gz",
    "C:\\evil.tar.gz",
    "evil\x00.tar.gz",
    "evil\n.tar.gz",
    " leading.tar.gz",
    "trailing.tar.gz ",
    "trailing-dot.tar.gz.",
    "CON",
    "nul.tar.gz",
    "COM1.json",
    "LPT9.tar.gz",
])
def test_tuf_repo_rejects_unsafe_target_names(tmp_path, bad_name):
    tuf_repo = _load_tuf_repo_module()
    with pytest.raises(ValueError):
        tuf_repo._validate_safe_target_name(bad_name)

    src = tmp_path / "good.tar.gz"
    src.write_bytes(b"ok")
    with pytest.raises(ValueError):
        tuf_repo.publish_target(tmp_path / "repo", src, bad_name)

    built = tmp_path / "built"
    built.mkdir(exist_ok=True)
    (built / "manifest.json").write_text(
        json.dumps({"archive": {"name": bad_name}}), encoding="utf-8",
    )
    with pytest.raises(ValueError):
        tuf_repo.build_offline_bundle(tmp_path / "repo", built, tmp_path / "out")


def test_tuf_repo_accepts_safe_target_names():
    tuf_repo = _load_tuf_repo_module()
    assert tuf_repo._validate_safe_target_name("sonder-engine-1.4.0.tar.gz") == "sonder-engine-1.4.0.tar.gz"
    assert tuf_repo._validate_safe_target_name("manifest.json") == "manifest.json"


def test_tuf_keystore_encrypts_with_passphrase_and_fails_closed_without_it(tmp_path, monkeypatch):
    tuf_repo = _load_tuf_repo_module()
    repo = tmp_path / "repo"
    signer = tuf_repo.CryptoSigner(ed25519.Ed25519PrivateKey.generate())

    monkeypatch.setenv("SONDER_TUF_KEYSTORE_PASSPHRASE", "ceremony-passphrase-xyz")
    tuf_repo._save_signer(repo, "root", 0, signer)

    stored = json.loads((repo / "keystore" / "root_0.json").read_text(encoding="utf-8"))
    assert stored["encrypted"] is True
    assert "BEGIN ENCRYPTED PRIVATE KEY" in stored["private_pem"]
    assert "BEGIN PRIVATE KEY" not in stored["private_pem"]

    # Round-trip decrypts cleanly with correct passphrase.
    loaded = tuf_repo._load_signers(repo, "root")
    assert len(loaded) == 1

    # Missing passphrase fails closed.
    monkeypatch.delenv("SONDER_TUF_KEYSTORE_PASSPHRASE", raising=False)
    with pytest.raises(ValueError, match="SONDER_TUF_KEYSTORE_PASSPHRASE"):
        tuf_repo._load_signers(repo, "root")

    # Wrong passphrase fails closed.
    monkeypatch.setenv("SONDER_TUF_KEYSTORE_PASSPHRASE", "wrong-secret")
    with pytest.raises(ValueError, match="invalid SONDER_TUF_KEYSTORE_PASSPHRASE"):
        tuf_repo._load_signers(repo, "root")


def test_tuf_keystore_warns_when_unencrypted_passphrase_absent(tmp_path, monkeypatch):
    tuf_repo = _load_tuf_repo_module()
    repo = tmp_path / "repo"
    signer = tuf_repo.CryptoSigner(ed25519.Ed25519PrivateKey.generate())
    monkeypatch.delenv("SONDER_TUF_KEYSTORE_PASSPHRASE", raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        tuf_repo._save_signer(repo, "targets", 0, signer)
        loaded = tuf_repo._load_signers(repo, "targets")
    assert len(loaded) == 1
    assert any("SONDER_TUF_KEYSTORE_PASSPHRASE" in str(w.message) for w in caught)
