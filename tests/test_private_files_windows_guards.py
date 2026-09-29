"""Fail-closed guards for Windows state-home ACL handling."""

import pytest

from sonder_runtime.platform import private_files


@pytest.mark.parametrize("location", ["home", "secrets"])
def test_low_integrity_protection_refuses_secret_scan_overflow(tmp_path, monkeypatch, location):
    home = tmp_path / "state"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    root = home if location == "home" else secrets
    (root / "first.key").write_bytes(b"first")
    (root / "second.key").write_bytes(b"second")
    monkeypatch.setattr(private_files, "_MAX_SECRET_FILES", 1)
    monkeypatch.setattr(private_files, "low_integrity_readable", lambda _path: False)

    with pytest.raises(RuntimeError, match="secret scan limit"):
        private_files.protect_state_from_low_integrity(home)


def test_low_integrity_protection_accepts_exact_secret_scan_limit(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    (home / "first.key").write_bytes(b"first")
    monkeypatch.setattr(private_files, "_MAX_SECRET_FILES", 1)
    monkeypatch.setattr(private_files, "low_integrity_readable", lambda _path: False)

    assert private_files.protect_state_from_low_integrity(home) == []


def test_unsafe_windows_state_home_skips_entire_acl_sweep(tmp_path, monkeypatch):
    home = tmp_path / "state"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "token.key").write_bytes(b"secret")
    profile = home / "user"
    profile.mkdir()
    calls = []
    monkeypatch.setattr(private_files.os.path, "expanduser", lambda _path: str(profile))
    monkeypatch.setattr(
        private_files,
        "_windows_restrict",
        lambda path, **kwargs: calls.append((path, kwargs)) or True,
    )

    assert private_files._never_tighten_windows(str(home))
    assert private_files._windows_tighten_state_home(str(home)) == 0
    assert calls == []
