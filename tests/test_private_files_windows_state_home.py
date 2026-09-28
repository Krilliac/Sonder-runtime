"""Windows confidentiality for Sonder's own state home.

``private_files`` was a no-op on Windows on the assumption that
``%LOCALAPPDATA%`` grants only the user, SYSTEM and Administrators. That is
false on hosts where another principal (for example a sandbox group) holds an
inherited read grant on the profile, and in every case the low-integrity
selfmod candidate runs with the user's SID and can read any medium file that
lacks a no-read-up label. Sonder now gives its own state home an explicit,
protected DACL and labels its secret stores medium no-read-up.

Every test works on pytest temp directories only.
"""
from __future__ import annotations

import os

import pytest

from sonder_runtime.platform import private_files

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACL semantics")

win32security = pytest.importorskip("win32security")
ntsecuritycon = pytest.importorskip("ntsecuritycon")


def _dacl_sids(path):
    sd = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    control, _revision = sd.GetSecurityDescriptorControl()
    dacl = sd.GetSecurityDescriptorDacl()
    sids = {
        win32security.ConvertSidToStringSid(dacl.GetAce(index)[-1])
        for index in range(dacl.GetAceCount())
    }
    return bool(control & win32security.SE_DACL_PROTECTED), sids


def _user_sid():
    import win32api
    import win32con

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    return win32security.ConvertSidToStringSid(
        win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    )


def _home(tmp_path):
    home = tmp_path / "sonder"
    (home / "secrets").mkdir(parents=True)
    (home / "selfmod" / "workspaces" / "w1").mkdir(parents=True)
    for name in ("memory.db", "memory.db-wal", "secrets.env", "fleet-principal.json",
                 "audit.jsonl"):
        (home / name).write_bytes(b"secret")
    (home / "secrets" / "rotation.json").write_bytes(b"{}")
    (home / "notes.txt").write_bytes(b"ordinary")
    (home / "selfmod" / "workspaces" / "w1" / "module.py").write_bytes(b"x = 1\n")
    # Stand in for the inherited foreign read grant seen on real profiles.
    (home / "memory.db").chmod(0o666)
    return home


def test_state_home_and_secret_stores_get_owner_only_protected_dacl(tmp_path):
    home = _home(tmp_path)
    allowed = {_user_sid(), "S-1-5-18", "S-1-5-32-544"}
    assert private_files.tighten_existing_stores(home) > 0
    for path in (home, home / "memory.db", home / "secrets.env",
                 home / "fleet-principal.json", home / "secrets" / "rotation.json"):
        protected, sids = _dacl_sids(path)
        assert protected, path
        assert sids <= allowed, (path, sids - allowed)


def test_secret_stores_are_not_low_integrity_readable(tmp_path):
    home = _home(tmp_path)
    assert private_files.low_integrity_readable_state_files(home)
    remaining = private_files.protect_state_from_low_integrity(home)
    assert remaining == []
    assert private_files.low_integrity_readable_state_files(home) == []
    for name in ("memory.db", "secrets.env", "fleet-principal.json"):
        assert not private_files.low_integrity_readable(home / name)


def test_candidate_workspaces_and_ordinary_files_stay_untouched(tmp_path):
    home = _home(tmp_path)
    before = _dacl_sids(home / "selfmod" / "workspaces" / "w1" / "module.py")
    private_files.protect_state_from_low_integrity(home)
    private_files.tighten_existing_stores(home)
    assert _dacl_sids(home / "selfmod" / "workspaces" / "w1" / "module.py") == before
    assert private_files.low_integrity_readable(home / "notes.txt")
    assert private_files.low_integrity_readable(
        home / "selfmod" / "workspaces" / "w1" / "module.py"
    )


def test_file_owned_by_another_account_is_not_rewritten(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setattr(private_files, "_windows_owned_by_me", lambda _path: False)
    before = _dacl_sids(home / "memory.db")
    private_files.tighten_existing_stores(home)
    assert _dacl_sids(home / "memory.db") == before
    # ...and the low-integrity gate reports it instead of passing silently.
    assert home / "memory.db" in [
        path for path in map(type(home), private_files.protect_state_from_low_integrity(home))
    ]
