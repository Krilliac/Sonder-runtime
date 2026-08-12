from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

import fanout_prompt_vault as vault


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FANOUT_KEY_FILE", str(tmp_path / "private" / "fanout.key"))
    vault.reset_cache_for_tests()
    yield
    vault.reset_cache_for_tests()


def test_encrypt_round_trip_creates_private_key():
    token = vault.encrypt_prompt("private prompt: \U0001f512")
    path = vault.key_path()

    assert token != "private prompt: \U0001f512"
    assert path.is_file()
    assert vault.decrypt_prompt(token) == "private prompt: \U0001f512"
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_decrypt_fails_closed_for_tampered_ciphertext():
    token = vault.encrypt_prompt("do not leak this exact prompt")

    with pytest.raises(vault.PromptVaultError) as error:
        vault.decrypt_prompt(token[:-1] + ("A" if token[-1] != "A" else "B"))

    assert "do not leak" not in str(error.value)


def test_decrypt_does_not_create_missing_key():
    with pytest.raises(vault.PromptVaultError):
        vault.decrypt_prompt("gAAAAABinvalid")

    assert not vault.key_path().exists()


def test_reset_cache_reloads_replaced_key():
    first = vault.encrypt_prompt("one")
    path = vault.key_path()
    path.write_bytes(Fernet.generate_key())
    vault.reset_cache_for_tests()

    with pytest.raises(vault.PromptVaultError):
        vault.decrypt_prompt(first)
    assert vault.decrypt_prompt(vault.encrypt_prompt("two")) == "two"
