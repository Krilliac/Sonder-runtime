"""Native diagnostics for a possible isolated Codegen build worker.

These are OS observations, not a production Codegen adapter or an admission
certificate. No test enables compose_isolated_codegen_build(). Secrets are
never printed; the low child reports only access outcomes and bounded markers.
This does not test Task Scheduler or other same-user brokers, and the current
runner does not create the separate desktop needed to prevent UI message
attacks against unrestricted desktop applications.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows MIC")
_MARKER = "CODEGEN_ISOLATION_PROBE="
# Microsoft SYSTEM_MANDATORY_LABEL_ACE_TYPE (winnt.h); this ACE type is not
# reliably exported through pywin32's win32security module.
_SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
_READ_PROBE = r"""
import json, sys
results = {}
for name, path in json.loads(sys.argv[1]).items():
    try:
        with open(path, 'rb') as stream:
            stream.read(1)
        result = 'readable'
    except PermissionError:
        result = 'denied'
    except FileNotFoundError:
        result = 'missing'
    except OSError as error:
        result = 'error:' + str(getattr(error, 'winerror', None) or type(error).__name__)
    results[name] = result
print('CODEGEN_ISOLATION_PROBE=' + json.dumps(results, sort_keys=True))
"""
_PROCESS_PROBE = r"""
import ctypes, json, os, sys
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
results = {}
for label, pid, rights in (
    ('own', os.getpid(), 0x0010 | 0x0040),
    ('host_vm_read', int(sys.argv[1]), 0x0010),
    ('host_dup_handle', int(sys.argv[1]), 0x0040),
    ('host_combined', int(sys.argv[1]), 0x0010 | 0x0040),
):
    handle = kernel.OpenProcess(rights, False, pid)
    if handle:
        results[label] = 'opened'
        kernel.CloseHandle(handle)
    else:
        results[label] = 'denied:' + str(ctypes.get_last_error())
print('CODEGEN_ISOLATION_PROBE=' + json.dumps(results, sort_keys=True))
"""
_STAGED_COMPILE = r"""
import json, os, pathlib, py_compile, shutil, sys
source = pathlib.Path(sys.argv[1])
stage = pathlib.Path(os.environ['TEMP']) / 'codegen-stage'
stage.mkdir()
copy = stage / 'candidate.py'
shutil.copyfile(source, copy)
compiled = stage / 'candidate.pyc'
py_compile.compile(str(copy), cfile=str(compiled), doraise=True)
print('CODEGEN_ISOLATION_PROBE=' + json.dumps({
    'stage_under_low_temp': stage.is_relative_to(pathlib.Path(os.environ['TEMP'])),
    'compiled': compiled.is_file(),
    'source_unchanged': source.read_text(encoding='utf-8') == copy.read_text(encoding='utf-8'),
}, sort_keys=True))
"""
_LABEL_TAMPER_PROBE = r"""
import ctypes, json, sys
from ctypes import wintypes
import win32security
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
targets = json.loads(sys.argv[1])
results = {}
low_sid = win32security.CreateWellKnownSid(win32security.WinLowLabelSid, None)
for name, path in targets.items():
    handle = kernel.CreateFileW(path, 0x00040000, 0x00000001 | 0x00000002 | 0x00000004,
                                 None, 3, 0x02000000, None)
    if handle != ctypes.c_void_p(-1).value:
        results[name + '_write_dac'] = 'opened'
        kernel.CloseHandle(handle)
    else:
        results[name + '_write_dac'] = 'denied:' + str(ctypes.get_last_error())
    sacl = win32security.ACL()
    sacl.AddMandatoryAce(win32security.ACL_REVISION, 0, 0, low_sid)
    try:
        win32security.SetNamedSecurityInfo(path, win32security.SE_FILE_OBJECT,
            win32security.LABEL_SECURITY_INFORMATION, None, None, None, sacl)
        results[name + '_label'] = 'changed'
    except Exception as error:
        results[name + '_label'] = 'denied:' + str(getattr(error, 'winerror', None)
                                                  or type(error).__name__)
print('CODEGEN_ISOLATION_PROBE=' + json.dumps(results, sort_keys=True))
"""
_WMI_LAUNCH_PROBE = r"""
import json, os, pathlib, subprocess
power_shell = pathlib.Path(os.environ['SystemRoot']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
cmd = pathlib.Path(os.environ['SystemRoot']) / 'System32' / 'cmd.exe'
script = ("$ErrorActionPreference='Stop'; "
          "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          "-Arguments @{CommandLine='" + str(cmd).replace("'", "''") + " /d /c exit 0'}; "
          "[Console]::WriteLine('WMI_RETURN=' + [string]$r.ReturnValue)")
try:
    completed = subprocess.run([str(power_shell), '-NoProfile', '-NonInteractive',
                                '-Command', script], capture_output=True, text=True,
                               timeout=12, check=False)
    codes = [line.split('=', 1)[1].strip() for line in completed.stdout.splitlines()
             if line.startswith('WMI_RETURN=')]
    if codes and completed.returncode == 0:
        outcome = 'launched' if codes == ['0'] else 'refused:' + codes[-1]
    else:
        outcome = 'unavailable:' + str(completed.returncode)
except (OSError, subprocess.TimeoutExpired):
    outcome = 'unavailable:launcher'
print('CODEGEN_ISOLATION_PROBE=' + json.dumps({'same_user_wmi_launch': outcome}))
"""


def _run_probe(script: str, arguments, *, cwd: Path, timeout: int = 20):
    from scripts.selfmod_low_integrity import run_isolated

    result = run_isolated(
        [sys.executable, "-I", "-c", script, *(str(arg) for arg in arguments)],
        cwd=cwd, timeout=timeout,
    )
    assert result["passed"] and result["job"]["integrity"] == "low", result
    messages = [line[len(_MARKER):] for line in str(result["output"]).splitlines()
                if line.startswith(_MARKER)]
    assert len(messages) == 1, result
    return json.loads(messages[0])


def _report(request, name: str, values: dict):
    rendered = json.dumps(values, sort_keys=True, separators=(",", ":"))
    request.node.user_properties.append((name, rendered))
    print(f"CODEGEN_ISOLATION_DIAGNOSTIC {name}={rendered}", flush=True)


def _state(root: Path, *, inherit_medium_label: bool):
    root.mkdir()
    if inherit_medium_label:
        # Label the *empty* owner directory first so key, DB, WAL, and SHM
        # must inherit the real NO_READ_UP policy when created later.
        import win32con
        import win32security

        sid = win32security.CreateWellKnownSid(win32security.WinMediumLabelSid, None)
        sacl = win32security.ACL()
        sacl.AddMandatoryAce(
            win32security.ACL_REVISION,
            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
            win32security.SYSTEM_MANDATORY_LABEL_NO_READ_UP
            | win32security.SYSTEM_MANDATORY_LABEL_NO_WRITE_UP,
            sid,
        )
        win32security.SetNamedSecurityInfo(
            str(root), win32security.SE_FILE_OBJECT,
            win32security.LABEL_SECURITY_INFORMATION,
            None, None, None, sacl,
        )

    from sonder_runtime.bootstrap.strategy import compose_strategy_trace

    key = root / "strategy-private" / "checkpoint.key"
    database = root / "strategy" / "checkpoints.db"
    compose_strategy_trace(db_path=database, key_path=key)
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    connection.execute("CREATE TABLE IF NOT EXISTS diagnostic (identity TEXT)")
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO diagnostic VALUES ('owner-write-while-child-probes')")
    paths = {"key": key, "db": database, "wal": Path(str(database) + "-wal"),
             "shm": Path(str(database) + "-shm")}
    assert all(path.is_file() for path in paths.values()), paths
    return connection, paths


def _inherited_medium_labels(paths: dict[str, Path]) -> dict[str, bool]:
    """Verify the child files themselves inherited the intended MIC mask."""
    import win32security

    expected_sid = win32security.ConvertSidToStringSid(
        win32security.CreateWellKnownSid(win32security.WinMediumLabelSid, None)
    )
    required = (win32security.SYSTEM_MANDATORY_LABEL_NO_READ_UP
                | win32security.SYSTEM_MANDATORY_LABEL_NO_WRITE_UP)
    result = {}
    for name, path in paths.items():
        descriptor = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.LABEL_SECURITY_INFORMATION,
        )
        sacl = descriptor.GetSecurityDescriptorSacl()
        result[name] = bool(sacl) and any(
            (ace[0][0] == _SYSTEM_MANDATORY_LABEL_ACE_TYPE
             and ace[1] & required == required
             and win32security.ConvertSidToStringSid(ace[2]) == expected_sid)
            for ace in (sacl.GetAce(index) for index in range(sacl.GetAceCount()))
        )
    return result


def test_unlabeled_host_owned_strategy_state_readability_is_reported(request, tmp_path):
    connection, paths = _state(tmp_path / "unlabeled-owner", inherit_medium_label=False)
    try:
        result = _run_probe(_READ_PROBE, [json.dumps({name: str(path) for name, path in paths.items()})],
                            cwd=tmp_path)
        _report(request, "unlabeled_strategy_read", result)
        assert set(result) == set(paths)
        assert all(value in {"readable", "denied"} for value in result.values()), result
        # This is an observation, not a policy approval. Some Windows hosts
        # let low-integrity code read ordinary medium files by default.
    finally:
        connection.rollback()
        connection.close()


def test_inherited_no_read_up_denies_key_db_and_sqlite_sidecars(request, tmp_path):
    try:
        connection, paths = _state(tmp_path / "labeled-owner", inherit_medium_label=True)
    except (OSError, PermissionError) as error:
        pytest.skip(f"host cannot provision inherited medium MIC label: {type(error).__name__}")
    try:
        try:
            inherited = _inherited_medium_labels(paths)
        except (OSError, PermissionError) as error:
            pytest.skip(f"host cannot inspect inherited child MIC labels: {type(error).__name__}")
        _report(request, "inherited_medium_label_inspection", inherited)
        assert all(inherited.values()), inherited
        key_before = paths["key"].read_bytes()
        result = _run_probe(_READ_PROBE, [json.dumps({name: str(path) for name, path in paths.items()})],
                            cwd=tmp_path)
        _report(request, "inherited_medium_label_read", result)
        assert result == dict.fromkeys(paths, "denied"), result
        tamper_paths = {"directory": str(paths["db"].parents[1]),
                        **{name: str(path) for name, path in paths.items()}}
        tamper = _run_probe(_LABEL_TAMPER_PROBE, [json.dumps(tamper_paths)], cwd=tmp_path)
        _report(request, "inherited_label_owner_tamper", tamper)
        assert all(value.startswith("denied:") for value in tamper.values()), tamper
        after = _run_probe(_READ_PROBE, [json.dumps({name: str(path) for name, path in paths.items()})],
                           cwd=tmp_path)
        assert after == result, {"before": result, "after": after}
        assert paths["key"].read_bytes() == key_before
    finally:
        connection.rollback()
        connection.close()


def test_low_child_process_handle_rights_against_medium_owner(request, tmp_path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    medium_self = kernel.OpenProcess(0x0010 | 0x0040, False, os.getpid())
    assert medium_self, f"medium owner cannot open its own process: {ctypes.get_last_error()}"
    kernel.CloseHandle(medium_self)
    result = _run_probe(_PROCESS_PROBE, [os.getpid()], cwd=tmp_path)
    _report(request, "low_process_access", result)
    assert result["own"] == "opened", result
    assert all(result[name].startswith("denied:") for name in
               ("host_vm_read", "host_dup_handle", "host_combined")), result


def test_low_child_compiles_only_staged_writable_python_copy(request, tmp_path):
    source = tmp_path / "candidate.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    result = _run_probe(_STAGED_COMPILE, [source], cwd=tmp_path)
    _report(request, "low_stage_compile", result)
    assert result == {"stage_under_low_temp": True, "compiled": True,
                      "source_unchanged": True}, result
    assert not (tmp_path / "candidate.pyc").exists()
    assert source.read_text(encoding="utf-8") == "answer = 42\n"


def test_wmi_same_user_process_broker_does_not_launch_outside_job(request, tmp_path):
    result = _run_probe(_WMI_LAUNCH_PROBE, (), cwd=tmp_path, timeout=20)
    _report(request, "low_wmi_process_broker", result)
    status = result["same_user_wmi_launch"]
    if status.startswith("unavailable:"):
        pytest.skip(f"Windows WMI process broker precondition unavailable: {status}")
    assert status.startswith("refused:"), result


def _staged_restricted_runtime(root: Path, sid) -> Path:
    """Copy a minimal stdlib runtime; grant this test's SID only on its tree."""
    import win32con
    import win32security

    from scripts import selfmod_low_integrity

    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("disposable runtime stage is unavailable")
    security = win32security.GetNamedSecurityInfo(
        str(root), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = security.GetSecurityDescriptorDacl()
    if dacl is None:
        raise RuntimeError("disposable stage has a null DACL")
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION_DS,
        win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE | win32con.GENERIC_EXECUTE,
        sid,
    )
    win32security.SetNamedSecurityInfo(
        str(root), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None, None, dacl, None,
    )
    base = Path(sys.base_prefix)
    python = root / "python.exe"
    shutil.copy2(base / "python.exe", python)
    for name in ("python3.dll", f"python{sys.version_info.major}{sys.version_info.minor}.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll"):
        source = base / name
        if source.is_file():
            shutil.copy2(source, root / name)
    for name in ("DLLs", "Lib"):
        source = base / name
        if not source.is_dir():
            raise RuntimeError(f"isolated Python {name} directory is unavailable")
        shutil.copytree(
            source, root / name,
            ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"),
        )
    shutil.copy2(Path(selfmod_low_integrity.__file__), root / "supervisor.py")
    return python


def test_unique_restricting_sid_profile_requires_proven_launch_and_deny_checks(
    request, monkeypatch, tmp_path,
):
    """Explore a token with a real restricting SID, never a new OS identity."""
    import win32api
    import win32con
    import win32security

    from scripts import selfmod_low_integrity

    base_home = Path(os.environ.get("USERPROFILE") or Path.home())
    stage = None
    nonce = tuple(int.from_bytes(os.urandom(4), "little") for _ in range(4))
    sid = win32security.ConvertStringSidToSid(
        "S-1-5-21-" + "-".join(str(component) for component in nonce)
    )
    connection = None
    try:
        # mkdtemp atomically creates a new path; cleanup below is restricted
        # to that owned directory even if a candidate name already existed.
        stage = Path(tempfile.mkdtemp(prefix="sc", dir=base_home))
        if len(str(stage)) > selfmod_low_integrity.MAX_WORK_ROOT_CHARS - 10:
            pytest.skip("unique restricted SID stage cannot fit Windows CreateProcess path bound")
        try:
            python = _staged_restricted_runtime(stage, sid)
        except (OSError, RuntimeError) as error:
            pytest.fail(f"restricted SID runtime stage is unavailable: {type(error).__name__}: {error}")

        connection, paths = _state(tmp_path / "restricted-owner", inherit_medium_label=False)
        stage_source = stage / "candidate.py"
        stage_source.write_text("answer = 42\n", encoding="utf-8")

        def restricted_low_token():
            current = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS,
            )
            token = win32security.CreateRestrictedToken(
                current, win32security.DISABLE_MAX_PRIVILEGE,
                [], [], [(sid, 0)],
            )
            assert win32security.IsTokenRestricted(token), "restricting SID was not installed"
            low_sid = win32security.CreateWellKnownSid(win32security.WinLowLabelSid, None)
            win32security.SetTokenInformation(
                token, win32security.TokenIntegrityLevel, (low_sid, 96),
            )
            return token

        # The stock supervisor launches sys.executable and reads its own
        # __file__. Redirect BOTH to copies in the disposable SID-granted
        # stage, solely inside this test's monkeypatch context. Never change
        # ACLs of the installed Python, OS DLLs, repository, or host state.
        with monkeypatch.context() as local:
            local.setattr(selfmod_low_integrity.sys, "executable", str(python))
            local.setattr(selfmod_low_integrity, "__file__", str(stage / "supervisor.py"))
            local.setattr(selfmod_low_integrity, "_low_token", restricted_low_token)
            local.setenv("SONDER_SELFMOD_SCRATCH_ROOT", str(stage))
            try:
                reads = _run_probe(
                    _READ_PROBE, [json.dumps({name: str(path) for name, path in paths.items()})],
                    cwd=stage,
                )
                process = _run_probe(_PROCESS_PROBE, [os.getpid()], cwd=stage)
                build = _run_probe(_STAGED_COMPILE, [stage_source], cwd=stage)
            except (OSError, RuntimeError) as error:
                pytest.fail(f"restricted SID child launch/probe failed: {type(error).__name__}: {error}")

        _report(request, "unique_restricted_sid_reads", reads)
        _report(request, "unique_restricted_sid_process", process)
        _report(request, "unique_restricted_sid_build", build)
        assert reads == dict.fromkeys(paths, "denied"), reads
        # A new restricting SID may be absent from the child's own process
        # DACL. The unrestricted positive own-process check above controls
        # the API; the build under this token controls child liveness.
        assert all(
            process[name].startswith("denied:") for name in
            ("host_vm_read", "host_dup_handle", "host_combined")
        ), process
        assert build == {"stage_under_low_temp": True, "compiled": True,
                         "source_unchanged": True}, build
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
