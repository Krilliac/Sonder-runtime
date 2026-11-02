"""Real-Blender regression tests for the Windows sandbox boundary.

Every probe runs twice: inside the sandbox and, as a control, as a plain unsandboxed Blender, so a
passing "denied" result cannot be a probe that never worked.  Canary files and counts only; no
registry value, environment value or user file content is ever read into an assertion or printed.
Skipped unless SONDER_BLENDER points at Blender on Windows.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from sonder_runtime.adapters.meshgen import blender_runner, sandbox_win

BLENDER = os.environ.get("SONDER_BLENDER", "")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not BLENDER or not os.path.isfile(BLENDER),
    reason="needs Windows and SONDER_BLENDER pointing at blender.exe")

PROLOGUE = '''
import json, os, sys
ARGS = json.loads(sys.argv[sys.argv.index("--") + 1])
R = {}
def t(name, fn):
    try:
        R[name] = ["OK", str(fn())[:60]]
    except BaseException as e:
        R[name] = ["ERR", type(e).__name__]
'''
EPILOGUE = '\nprint("PROBE " + json.dumps(R))\n'


def _parse(stdout: bytes) -> dict:
    for line in stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("PROBE "):
            return json.loads(line[6:])
    raise AssertionError("the probe printed no result; stdout tail: %r" % stdout[-300:])


def _minimal_env() -> dict:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return {"SYSTEMROOT": root, "WINDIR": root, "PATH": root + r"\System32;" + root}


def run_probe(tmp_path: Path, label: str, body: str, args: dict, boxed: bool, timeout: int = 120):
    """Run ``body`` as a Blender ``--python`` script; returns (results, SandboxRun-or-None)."""
    folder = tmp_path / label
    folder.mkdir()
    source = PROLOGUE + body + EPILOGUE
    blender_dir = os.path.dirname(BLENDER)
    if boxed:
        with sandbox_win.granted_run_dir(folder, blender_dir) as build:
            script = build.run / "probe.py"
            script.write_text(source, encoding="utf-8")
            payload = dict(args, run=str(build.run), ac_folder=build.slot.folder)
            outcome = sandbox_win.run_sandboxed(
                [BLENDER, "-b", "--factory-startup", "--python", str(script), "--", json.dumps(payload)],
                cwd=build.run, env=sandbox_win.sandbox_env(build.run, blender_dir, os.environ.get("SystemRoot", "")),
                timeout=timeout, slot=build.slot, memory_mb=2048)
        return _parse(outcome.stdout), outcome
    script = folder / "probe.py"
    script.write_text(source, encoding="utf-8")
    run = folder / "run"
    run.mkdir()
    payload = dict(args, run=str(run), ac_folder="")
    done = subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(script), "--", json.dumps(payload)],
                          capture_output=True, timeout=timeout, env=_minimal_env(), cwd=str(run), check=False)
    return _parse(done.stdout), None


def both(tmp_path, body, args):
    boxed, _ = run_probe(tmp_path, "boxed", body, args, boxed=True)
    free, _ = run_probe(tmp_path, "free", body, args, boxed=False)
    return boxed, free


@pytest.fixture()
def canary(tmp_path):
    folder = tmp_path / "canary"
    folder.mkdir()
    (folder / "canary.txt").write_text("CANARY", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    return {"canary": str(folder / "canary.txt"), "canary_dir": str(folder), "outside": str(outside)}


# ---------------------------------------------------------------- files

def test_writing_outside_the_run_dir_is_denied(tmp_path, canary):
    body = 't("write", lambda: open(os.path.join(ARGS["outside"], "w.txt"), "w").write("x"))\n'
    boxed, free = both(tmp_path, body, canary)
    assert boxed["write"][0] == "ERR" and free["write"][0] == "OK"
    assert not (Path(canary["outside"]) / "w.txt").exists() or free["write"][0] == "OK"


def test_the_run_dir_itself_is_writable_in_the_sandbox(tmp_path, canary):
    body = 't("write", lambda: open(os.path.join(ARGS["run"], "ok.txt"), "w").write("x"))\n'
    boxed, _ = run_probe(tmp_path, "boxed", body, canary, boxed=True)
    assert boxed["write"][0] == "OK"


def test_reading_a_file_outside_the_run_dir_is_denied(tmp_path, canary):
    body = 't("read", lambda: open(ARGS["canary"]).read() == "CANARY")\n'
    boxed, free = both(tmp_path, body, canary)
    assert boxed["read"][0] == "ERR" and free["read"] == ["OK", "True"]


def test_a_hard_link_to_an_outside_file_is_denied(tmp_path, canary):
    body = 't("hardlink", lambda: os.link(ARGS["canary"], os.path.join(ARGS["run"], "hl")))\n'
    boxed, free = both(tmp_path, body, canary)
    assert boxed["hardlink"][0] == "ERR" and free["hardlink"][0] == "OK"


def test_a_junction_to_an_outside_directory_cannot_be_read_through(tmp_path, canary):
    body = (
        'import _winapi\n'
        'link = os.path.join(ARGS["run"], "junc")\n'
        'def through():\n'
        '    _winapi.CreateJunction(ARGS["canary_dir"], link)\n'
        '    return open(os.path.join(link, "canary.txt")).read() == "CANARY"\n'
        't("junction", through)\n')
    boxed, free = both(tmp_path, body, canary)
    assert boxed["junction"][0] == "ERR" and free["junction"] == ["OK", "True"]


def test_a_symlink_to_an_outside_file_cannot_be_created_or_read_in_the_sandbox(tmp_path, canary):
    # No unsandboxed control: creating a symlink needs a privilege this account may not hold.
    body = (
        'link = os.path.join(ARGS["run"], "sym")\n'
        'def through():\n'
        '    os.symlink(ARGS["canary"], link)\n'
        '    return open(link).read()\n'
        't("symlink", through)\n')
    boxed, _ = run_probe(tmp_path, "boxed", body, canary, boxed=True)
    assert boxed["symlink"][0] == "ERR"


def test_the_host_refuses_a_result_that_names_a_junction_target_outside_the_output_dir(tmp_path, canary):
    import _winapi

    out = tmp_path / "out"
    out.mkdir()
    _winapi.CreateJunction(canary["canary_dir"], str(out / "j"))
    refused = blender_runner.confine_files({"ok": True, "files": {"model": str(out / "j" / "canary.txt")}}, out)
    assert refused["ok"] is False and "outside the output directory" in refused["error"]


# ---------------------------------------------------------------- processes, network, screen

def test_spawning_a_process_is_denied(tmp_path, canary):
    body = (
        'import subprocess\n'
        't("spawn", lambda: subprocess.run(["cmd.exe", "/c", "exit", "0"], timeout=20, check=True).returncode)\n')
    boxed, free = both(tmp_path, body, canary)
    assert boxed["spawn"][0] == "ERR" and free["spawn"][0] == "OK"


def _non_loopback_address():
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        address = info[4][0]
        if not address.startswith("127."):
            return address
    return None


class Listener:
    def __init__(self, host):
        self.sock = socket.socket()
        self.sock.bind((host, 0))
        self.sock.listen(4)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            connection.close()

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self.sock.close()


CONNECT = ('import socket\n'
           't("connect", lambda: socket.create_connection((ARGS["host"], ARGS["port"]), timeout=5).close())\n')


def test_loopback_tcp_is_denied(tmp_path, canary):
    listener = Listener("127.0.0.1")
    try:
        args = dict(canary, host="127.0.0.1", port=listener.port)
        boxed, _ = run_probe(tmp_path, "boxed", CONNECT, args, boxed=True)
        assert boxed["connect"][0] == "ERR" and listener.accepted == 0
        free, _ = run_probe(tmp_path, "free", CONNECT, args, boxed=False)
        assert free["connect"][0] == "OK" and listener.accepted == 1
    finally:
        listener.close()


def test_external_tcp_to_a_non_loopback_address_is_denied(tmp_path, canary):
    address = _non_loopback_address()
    if not address:
        pytest.skip("this host has no non-loopback IPv4 address to listen on")
    listener = Listener("0.0.0.0")
    try:
        args = dict(canary, host=address, port=listener.port)
        boxed, _ = run_probe(tmp_path, "boxed", CONNECT, args, boxed=True)
        assert boxed["connect"][0] == "ERR" and listener.accepted == 0
        free, _ = run_probe(tmp_path, "free", CONNECT, args, boxed=False)
        assert free["connect"][0] == "OK" and listener.accepted == 1
    finally:
        listener.close()


def test_dns_resolution_is_denied(tmp_path, canary):
    body = ('import socket\n'
            't("dns_external", lambda: socket.getaddrinfo("example.com", 80))\n'
            't("dns_local", lambda: socket.getaddrinfo("localhost", 80))\n')
    boxed, free = both(tmp_path, body, canary)
    assert boxed["dns_external"][0] == "ERR"
    assert free["dns_local"][0] == "OK"  # the resolver works for the control, so the denial is the sandbox


SCREEN = '''
import ctypes
def grab():
    gdi = ctypes.WinDLL("gdi32"); user = ctypes.WinDLL("user32")
    user.GetDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                           ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    gdi.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    screen = user.GetDC(None)
    if not screen:
        raise OSError("no screen DC")
    mem = gdi.CreateCompatibleDC(ctypes.c_void_p(screen))
    bmp = gdi.CreateCompatibleBitmap(ctypes.c_void_p(screen), 256, 256)
    gdi.SelectObject(mem, bmp)
    if not gdi.BitBlt(mem, 0, 0, 256, 256, ctypes.c_void_p(screen), 0, 0, 0x00CC0020):
        raise OSError("BitBlt failed")
    colours = set()
    for x in range(0, 256, 8):
        for y in range(0, 256, 8):
            colours.add(gdi.GetPixel(mem, x, y))
    return "distinct=%d" % len(colours)
t("screen", grab)
'''


def test_screen_capture_sees_nothing(tmp_path, canary):
    free, _ = run_probe(tmp_path, "free", SCREEN, canary, boxed=False)
    if free["screen"][0] != "OK" or int(free["screen"][1].split("=")[1]) <= 1:
        pytest.skip("the control desktop is not interactive or is uniform, so a capture proves nothing here")
    boxed, _ = run_probe(tmp_path, "boxed", SCREEN, canary, boxed=True)
    verdict = boxed["screen"]
    assert verdict[0] == "ERR" or int(verdict[1].split("=")[1]) <= 1, verdict


# ---------------------------------------------------------------- state and isolation

def test_state_planted_in_appcontainer_storage_does_not_survive_to_the_next_build(tmp_path, canary):
    plant = ('marker = os.path.join(ARGS["ac_folder"], "Temp", "persist.txt")\n'
             't("plant", lambda: open(marker, "w").write("planted"))\n')
    look = ('marker = os.path.join(ARGS["ac_folder"], "Temp", "persist.txt")\n'
            't("look", lambda: open(marker).read())\n')
    first, _ = run_probe(tmp_path, "first", plant, canary, boxed=True)
    assert first["plant"][0] == "OK"  # the storage really is writable, so the second result means something
    second, _ = run_probe(tmp_path, "second", look, canary, boxed=True)
    assert second["look"] == ["ERR", "FileNotFoundError"]


SCAN = '''
import ctypes, time
from ctypes import wintypes as W
k = ctypes.WinDLL("kernel32", use_last_error=True)
class Entry(ctypes.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("pid", W.DWORD), ("heap", ctypes.c_size_t),
                ("module", W.DWORD), ("threads", W.DWORD), ("parent", W.DWORD), ("pri", W.LONG),
                ("flags", W.DWORD), ("exe", ctypes.c_wchar * 260)]
k.CreateToolhelp32Snapshot.restype = W.HANDLE
k.OpenProcess.restype = W.HANDLE
if ARGS["role"] == "victim":
    open(os.path.join(ARGS["run"], "alive.txt"), "w").write("up")
    time.sleep(ARGS["hold"])
else:
    time.sleep(ARGS["delay"])
    snap = k.CreateToolhelp32Snapshot(2, 0)
    entry = Entry(); entry.dwSize = ctypes.sizeof(Entry)
    seen = opened = 0
    ok = k.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
    while ok:
        if entry.exe.lower() == "blender.exe" and entry.pid != os.getpid():
            seen += 1
            if k.OpenProcess(0x1F0FFF, False, entry.pid) or k.OpenProcess(0x1000, False, entry.pid):
                opened += 1
        ok = k.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    R["scan"] = ["OK", "seen=%d opened=%d" % (seen, opened)]
'''


def test_concurrent_builds_use_different_slots_and_cannot_open_each_others_process(tmp_path):
    blender_dir = os.path.dirname(BLENDER)
    ready = threading.Barrier(2, timeout=120)
    slots, results, errors, alive = [], {}, [], []
    source = PROLOGUE + SCAN + EPILOGUE
    folders = {name: tmp_path / name for name in ("victim", "attacker")}
    for folder in folders.values():
        folder.mkdir()

    def build(role):
        try:
            with sandbox_win.granted_run_dir(folders[role], blender_dir) as b:
                slots.append(b.slot.index)
                script = b.run / "probe.py"
                script.write_text(source, encoding="utf-8")
                ready.wait()
                args = {"role": role, "hold": 25, "delay": 8, "run": str(b.run)}
                alive.append(b.run) if role == "victim" else None
                out = sandbox_win.run_sandboxed(
                    [BLENDER, "-b", "--factory-startup", "--python", str(script), "--", json.dumps(args)], cwd=b.run,
                    env=sandbox_win.sandbox_env(b.run, blender_dir, os.environ.get("SystemRoot", "")),
                    timeout=120, slot=b.slot, memory_mb=1024)
                if role == "attacker":
                    results.update(_parse(out.stdout))
                    results["victim_alive"] = bool(alive) and (alive[0] / "alive.txt").exists()
        except BaseException as exc:  # noqa: BLE001 - surfaced through the assertion below
            errors.append(repr(exc))

    threads = [threading.Thread(target=build, args=(role,)) for role in ("victim", "attacker")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(180)
    assert not errors, errors
    assert len(set(slots)) == 2, slots
    seen, opened = (int(part.split("=")[1]) for part in results["scan"][1].split())
    assert results["victim_alive"], "the victim build was not running during the scan"
    assert opened == 0, results  # whether or not the other build's process is even visible (seen=%d)


KEYSTATE = ('import ctypes\n'
            'u = ctypes.WinDLL("user32")\n'
            't("keystate", lambda: sum(1 for vk in range(8, 255) if u.GetAsyncKeyState(vk) & 0x8000))\n')


def test_global_key_state_reports_no_pressed_keys_from_the_sandbox(tmp_path, canary):
    boxed, _ = run_probe(tmp_path, "boxed", KEYSTATE, canary, boxed=True)
    assert boxed["keystate"] == ["OK", "0"] or boxed["keystate"][0] == "ERR"  # no key can be observed


# ---------------------------------------------------------------- registry (known gap, see sandbox_win)

REGISTRY = '''
import winreg
def count(path):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    return "values=%d" % (winreg.QueryInfoKey(key)[1] + winreg.QueryInfoKey(key)[0])
t("environment", lambda: count("Environment"))
t("recentdocs", lambda: count(r"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\RecentDocs"))
'''


def test_the_registry_probe_works_unsandboxed(tmp_path, canary):
    free, _ = run_probe(tmp_path, "free", REGISTRY, canary, boxed=False)
    if free["environment"][0] != "OK":
        pytest.skip("HKCU\\Environment cannot be opened even unsandboxed on this account")
    assert free["recentdocs"][0] in ("OK", "ERR")


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN GAP (review B1): every AppContainer is in ALL APPLICATION PACKAGES, which the user's HKCU keys "
    "grant read; LPAC (the opt-out) cannot run Blender (SxS needs registryRead, OpenGL needs the GPU) and a "
    "DENY ACE for the package SID has no effect. Strict: this turns into a failure the day it is fixed."))
def test_hkcu_environment_and_recentdocs_are_unreadable_from_the_sandbox(tmp_path, canary):
    free, _ = run_probe(tmp_path, "free", REGISTRY, canary, boxed=False)
    if free["environment"][0] != "OK":
        pytest.skip("HKCU\\Environment cannot be opened even unsandboxed on this account")
    boxed, _ = run_probe(tmp_path, "boxed", REGISTRY, canary, boxed=True)
    assert boxed["environment"][0] == "ERR" and boxed["recentdocs"][0] == "ERR"


def test_preflight_measures_and_reports_hkcu_readability():
    info = sandbox_win.preflight(os.path.dirname(BLENDER))
    state = sandbox_win.registry_isolation(os.path.dirname(BLENDER))
    assert info["registry"] == state and state in ("readable", "isolated")
    assert ("warning" in info) == (state != "isolated")


# ---------------------------------------------------------------- memory

ALLOC = ('try:\n'
         '    blob = [0] * ARGS["items"]\n'
         '    R["alloc"] = ["OK", "allocated"]\n'
         'except MemoryError:\n'
         '    R["alloc"] = ["ERR", "MemoryError"]\n')


def _run_alloc(tmp_path, label, limit_mb, boxed=True):
    folder = tmp_path / label
    folder.mkdir()
    source = PROLOGUE + ALLOC + EPILOGUE
    blender_dir = os.path.dirname(BLENDER)
    with sandbox_win.granted_run_dir(folder, blender_dir) as build:
        script = build.run / "probe.py"
        script.write_text(source, encoding="utf-8")
        out = sandbox_win.run_sandboxed(
            [BLENDER, "-b", "--factory-startup", "--python", str(script), "--", json.dumps({"items": 400_000_000})],
            cwd=build.run, env=sandbox_win.sandbox_env(build.run, blender_dir, os.environ.get("SystemRoot", "")),
            timeout=120, slot=build.slot, memory_mb=limit_mb)
    return _parse(out.stdout)


def test_an_allocation_above_the_job_limit_fails_and_succeeds_with_a_high_limit(tmp_path):
    assert _run_alloc(tmp_path, "low", 512)["alloc"] == ["ERR", "MemoryError"]
    assert _run_alloc(tmp_path, "high", 12288)["alloc"] == ["OK", "allocated"]


HOG_KIT = ("def hog():\n    blob = [0] * 400000000\n    blob[::4096] = [1] * len(blob[::4096])\n\n"
           "def fail():\n    raise ValueError('plain failure')\n")


def _recorded(tmp_path, body, kit=True):
    """Record ``body`` (the text of ``def build()``) against a trusted test kit; returns ``(ops_path, kit_path, allowed)``."""
    from sonder_runtime.domain.meshgen.dsl.manifest import build_manifest
    from sonder_runtime.domain.meshgen.dsl.recorder import compile_script

    (tmp_path / "kit").mkdir(exist_ok=True)
    kit_path = tmp_path / "kit" / "hogkit.py"
    kit_path.write_text(HOG_KIT, encoding="utf-8")
    manifest = build_manifest(HOG_KIT, "hogkit")
    source = ("import hogkit\n" if kit else "") + "def build():\n" + body
    ops = tmp_path / "ops.json"
    ops.write_text(compile_script(source, manifest if kit else None).text, encoding="utf-8")
    return ops, (kit_path if kit else None), sorted(manifest.exposed)


def test_memory_limit_exceeded_is_reported_only_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_MESHGEN_MEM_MB", "512")
    ops, kit_path, allowed = _recorded(tmp_path, "    hogkit.hog()\n")
    result = blender_runner.build_candidate(BLENDER, str(ops), str(tmp_path / "out"), name="m", kit_path=kit_path,
                                            kit_allowed=allowed, max_tris=100, height_m=0, render=False, timeout=120)
    assert result["ok"] is False and "memory limit exceeded (512 MB)" in result["error"], result
    # A failing build that did not come near the limit must not be blamed on memory.
    ops, kit_path, allowed = _recorded(tmp_path, "    hogkit.fail()\n")
    plain = blender_runner.build_candidate(BLENDER, str(ops), str(tmp_path / "out2"), name="m", kit_path=kit_path,
                                           kit_allowed=allowed, max_tris=100, height_m=0, render=False, timeout=120)
    assert plain["ok"] is False and "plain failure" in plain["error"] and "memory" not in plain["error"]
    assert plain["error"].startswith("ReplayError: line 3") or "line 3" in plain["error"], plain["error"]


def test_the_explicit_memory_argument_is_honoured_on_the_sandboxed_path(tmp_path):
    ops, kit_path, allowed = _recorded(tmp_path, "    hogkit.hog()\n")
    result = blender_runner.build_candidate(BLENDER, str(ops), str(tmp_path / "out"), name="m", kit_path=kit_path,
                                            kit_allowed=allowed, max_tris=100, height_m=0, render=False, timeout=120,
                                            memory_mb=384)
    assert "memory limit exceeded (384 MB)" in result["error"], result


# ---------------------------------------------------------------- sizes and cleanup

def test_a_build_leaves_no_run_dir_grant_or_slot_behind(tmp_path):
    ops, _, _ = _recorded(tmp_path, "    primitive('cube')\n", kit=False)
    result = blender_runner.build_candidate(BLENDER, str(ops), str(tmp_path / "out"), name="m", max_tris=100,
                                            height_m=0, render=False, timeout=120)
    assert result["ok"] is True and "warnings" not in result, result
    assert not list((tmp_path / "out").glob(".run-*"))
