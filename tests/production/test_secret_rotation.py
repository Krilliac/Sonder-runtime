"""SPEC-2 WP2: key rotation with a mandatory overlap expiry."""
from __future__ import annotations

import os
import time

import pytest

import sonder_secrets

pytestmark = pytest.mark.unit


@pytest.fixture()
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "sonder.env"
    path.write_text("SONDER_API_KEY=old-key-value-0123456789abcdef\n",
                    encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv(
        "SONDER_ROTATION_STATE", str(tmp_path / "rotation.json")
    )
    return path


def test_rotate_replaces_key_and_records_previous_hash(secrets_file):
    old_key = "old-key-value-0123456789abcdef"
    report = sonder_secrets.rotate_api_key(secrets_file, overlap_seconds=3600)
    content = secrets_file.read_text(encoding="utf-8")
    assert old_key not in content
    assert "SONDER_API_KEY=" in content
    new_key = [
        line.partition("=")[2]
        for line in content.splitlines()
        if line.startswith("SONDER_API_KEY=")
    ][0]
    assert len(new_key) >= 32
    assert report["previous_accepted_until"]
    # The plaintext previous key is not stored anywhere.
    state_raw = (secrets_file.parent / "rotation.json").read_text()
    assert old_key not in state_raw


def test_secret_rotation_accepts_previous_until_expiry(secrets_file):
    old_key = "old-key-value-0123456789abcdef"
    sonder_secrets.rotate_api_key(secrets_file, overlap_seconds=3600)
    assert sonder_secrets.previous_key_valid(old_key) is True
    assert sonder_secrets.previous_key_valid("some-other-key") is False
    # Mandatory expiration: after the window the previous key dies.
    future = time.time() + 3601
    assert sonder_secrets.previous_key_valid(old_key, now=future) is False


def test_rotation_via_auth_context(secrets_file, monkeypatch):
    import sonder_serve

    old_key = "old-key-value-0123456789abcdef"
    sonder_secrets.rotate_api_key(secrets_file, overlap_seconds=3600)
    new_key = [
        line.partition("=")[2]
        for line in secrets_file.read_text().splitlines()
        if line.startswith("SONDER_API_KEY=")
    ][0]
    monkeypatch.setattr(sonder_serve, "API_KEY", new_key)
    assert sonder_serve._auth_context(f"Bearer {new_key}")["authorized"]
    assert sonder_serve._auth_context(f"Bearer {old_key}")["authorized"]
    assert not sonder_serve._auth_context("Bearer bogus")["authorized"]


def test_rotate_refuses_world_readable_secrets(secrets_file):
    if os.name != "posix":
        pytest.skip("posix permissions")
    os.chmod(secrets_file, 0o644)
    with pytest.raises(sonder_secrets.RotationError, match="group/world"):
        sonder_secrets.rotate_api_key(secrets_file)


def test_rotate_missing_file_fails(tmp_path):
    with pytest.raises(sonder_secrets.RotationError, match="not found"):
        sonder_secrets.rotate_api_key(tmp_path / "missing.env")


def test_rotate_adds_key_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SONDER_ROTATION_STATE", str(tmp_path / "rotation.json")
    )
    path = tmp_path / "sonder.env"
    path.write_text("SONDER_AUTH_SECRET=abc\n", encoding="utf-8")
    os.chmod(path, 0o600)
    report = sonder_secrets.rotate_api_key(path, overlap_seconds=3600)
    assert "SONDER_API_KEY=" in path.read_text()
    # No previous key existed, so no overlap window is created.
    assert report["previous_accepted_until"] is None
    assert not (tmp_path / "rotation.json").exists()
