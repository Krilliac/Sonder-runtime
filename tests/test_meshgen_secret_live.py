"""Real-Blender proof that the secret guard holds at the egress even though the registry is readable.

A CANARY value is written under HKCU\\Software\\SonderMeshgenCanary (never HKCU\\Environment) and
deleted afterwards; a guard holding that canary stands in for the user's real secrets.  Each blocked
case has a control showing the leak is real without the guard (empty guard) or authorised
(SONDER_MESHGEN_SECRET_ALLOW).  The scripts bypass the AST gate on purpose (build_candidate does not run
it) and get ``winreg`` through the harness' test-only ``extra_import_roots`` job key.
Skipped unless SONDER_BLENDER points at Blender on Windows.
"""
from __future__ import annotations

import base64
import json
import os
import secrets

import pytest

from sonder_runtime.adapters.meshgen import blender_runner
from sonder_runtime.adapters.meshgen import secret_sources
from sonder_runtime.domain.meshgen.secret_guard import REDACTED, SecretGuard

BLENDER = os.environ.get("SONDER_BLENDER", "")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not BLENDER or not os.path.isfile(BLENDER),
    reason="needs Windows and SONDER_BLENDER pointing at blender.exe")

KEY_PATH = r"Software\SonderMeshgenCanary"
NAME = "SONDER_CANARY_TOKEN"

READ_CANARY = '''
import bpy
import winreg
import base64


def canary():
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\SonderMeshgenCanary")
    return winreg.QueryValueEx(key, "Canary")[0]

'''
RAISE = READ_CANARY + '''
def build():
    raise ValueError(canary())
'''
OBJECT_NAME = READ_CANARY + '''
def build():
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.active_object.name = base64.b64encode(canary().encode()).decode()
'''
MATERIAL_NAME = READ_CANARY + '''
def build():
    bpy.ops.mesh.primitive_cube_add()
    material = bpy.data.materials.new(name=base64.b64encode(canary().encode()).decode())
    bpy.context.active_object.data.materials.append(material)
'''
HARMLESS = '''
import bpy


def build():
    bpy.ops.mesh.primitive_cube_add()
'''


def _grant_all_application_packages_read(key) -> None:
    """Give the canary key the ACE the real user-environment key has (ALL APPLICATION PACKAGES: read).

    Without it the sandbox could not read the new key at all, and the test would prove nothing about
    the guard.  Only the test's own key is touched.
    """
    import ctypes
    from ctypes import wintypes as W

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        W.LPCWSTR, W.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.RegSetKeySecurity.argtypes = [ctypes.c_void_p, W.DWORD, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    ok = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:PAI(A;;KA;;;OW)(A;;KA;;;SY)(A;;KR;;;AC)", 1, ctypes.byref(descriptor), None)
    assert ok, ctypes.get_last_error()
    try:
        status = advapi.RegSetKeySecurity(int(key.handle), 4, descriptor)
        assert status == 0, status
    finally:
        kernel.LocalFree(descriptor)


@pytest.fixture()
def canary():
    import winreg

    value = "canary-" + secrets.token_hex(10)
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY_PATH, 0, winreg.KEY_SET_VALUE | 0x40000)
    try:
        winreg.SetValueEx(key, "Canary", 0, winreg.REG_SZ, value)
        _grant_all_application_packages_read(key)
        yield value
    finally:
        winreg.CloseKey(key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY_PATH)


@pytest.fixture(autouse=True)
def winreg_shim(monkeypatch):
    """Test-only: let the harness import winreg and base64 (the AST gate would refuse winreg)."""
    original = blender_runner._job_dict
    monkeypatch.setattr(blender_runner, "_job_dict",
                        lambda *a, **k: dict(original(*a, **k), extra_import_roots=["winreg", "base64"]))


class NoGuard(SecretGuard):
    """Control: a guard that lets everything through, to prove each leak below is real."""

    def hit(self, text):
        return False


def build(tmp_path, source, guard, label, export="glb"):
    code = tmp_path / (label + ".py")
    code.write_text(source, encoding="utf-8")
    return blender_runner.build_candidate(BLENDER, str(code), str(tmp_path / ("out-" + label)), name="n",
                                          max_tris=1000, height_m=0, render=False, timeout=150, guard=guard,
                                          sandbox="appcontainer", export=export)


def leaked_anywhere(result, value):
    dumped = json.dumps(result)
    forms = [value, value[::-1], base64.b64encode(value.encode()).decode(), value.encode().hex()]
    return any(form in dumped for form in forms)


def test_a_script_that_reads_the_canary_from_the_registry_and_raises_it_is_blocked(tmp_path, canary):
    control = build(tmp_path, RAISE, NoGuard({}), "control")  # no secrets known: the leak is real
    assert control["ok"] is False and canary in control["error"], "the sandboxed script could not leak the canary"
    result = build(tmp_path, RAISE, SecretGuard({NAME: canary}), "guarded")
    assert result["ok"] is False and result["secret_leak_blocked"] is True and result["error"] == REDACTED
    assert not leaked_anywhere(result, canary)
    assert NAME not in json.dumps(result)


def test_the_canary_base64_encoded_into_an_object_name_is_caught(tmp_path, canary):
    control = build(tmp_path, OBJECT_NAME, NoGuard({}), "control")
    assert control["ok"] is True, control
    result = build(tmp_path, OBJECT_NAME, SecretGuard({NAME: canary}), "guarded")
    assert result["ok"] is False and result["secret_leak_blocked"] is True
    assert "files" not in result and not list((tmp_path / "out-guarded").glob("*.glb"))
    assert not leaked_anywhere(result, canary)


def test_the_canary_in_a_material_name_is_caught_in_the_glb_metadata(tmp_path, canary):
    control = build(tmp_path, MATERIAL_NAME, NoGuard({}), "control")
    assert control["ok"] is True, control
    exported = tmp_path / "out-control" / "n.glb"
    assert exported.is_file() and base64.b64encode(canary.encode()).decode().encode() in exported.read_bytes(), (
        "the control export must carry the name, otherwise this test proves nothing")
    result = build(tmp_path, MATERIAL_NAME, SecretGuard({NAME: canary}), "guarded")
    assert result["ok"] is False and result["secret_leak_blocked"] is True
    assert not list((tmp_path / "out-guarded").glob("*.glb")), "a leaking model must not be copied out"


def test_an_authorised_name_passes_through_unredacted(tmp_path, canary):
    guard = SecretGuard({NAME: canary}, allow=[NAME])
    result = build(tmp_path, RAISE, guard, "allowed")
    assert result["ok"] is False and canary in result["error"] and "secret_leak_blocked" not in result
    exported = build(tmp_path, MATERIAL_NAME, guard, "allowed-model")
    assert exported["ok"] is True and (tmp_path / "out-allowed-model" / "n.glb").is_file()


def test_the_authorisation_comes_from_the_environment_variable(tmp_path, canary, monkeypatch):
    monkeypatch.setenv(secret_sources.ALLOW_ENV, NAME)
    built = secret_sources.load_guard({NAME: canary, secret_sources.ALLOW_ENV: NAME})
    assert not built.hit(canary)
    monkeypatch.delenv(secret_sources.ALLOW_ENV)
    assert secret_sources.load_guard({NAME: canary}).hit(canary)


def test_a_harmless_build_still_works_and_carries_no_flag(tmp_path, canary):
    result = build(tmp_path, HARMLESS, SecretGuard({NAME: canary}), "harmless")
    assert result["ok"] is True and "secret_leak_blocked" not in result and result["sandbox"] == "appcontainer"
    assert (tmp_path / "out-harmless" / "n.glb").is_file()
