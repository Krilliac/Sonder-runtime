"""Security guards for first-party ``tools/`` code and local state/context roots.

Covers:
1. ``tools`` in ``FIRST_PARTY_CODE_DIRS`` so ``tools/tuf_repo.py``, ``tools/*.py``,
   scripts, and ``.ini/.cfg/.toml`` files under ``tools/`` require developer auth.
2. Lexical refusal of UNC, device/NT-namespace, relative, and drive-root spellings
   for ``SONDER_HOME`` / ``SONDER_STATE_HOME`` / ``[state].home``,
   ``[artifact_mobility_source].store_dir``, and ``[context].*_roots`` BEFORE any
   ``resolve()`` / ``stat()`` / ``is_symlink()`` call (zero SMB/NTLM leaks),
   while cleanly stripping valid local extended-length ``\\\\?\\<drive>:\\...``
   prefixes.
"""
from __future__ import annotations

import os
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.inspection import archive_tools
from sonder_runtime.adapters.security import control_plane_paths
from sonder_runtime.platform import config as config_mod
from sonder_runtime.platform import paths as runtime_paths
from sonder_runtime.platform.artifact_mobility_source_config import (
    ArtifactMobilitySourceConfig,
    artifact_mobility_source_errors,
)

PAYLOAD = "import os\nos.system('payload')\n"

HOSTILE_NETWORK_OR_DEVICE_HOMES = [
    r"\\attacker.invalid\share\sonder",
    "//attacker.invalid/share/sonder",
    r"\\?\UNC\attacker.invalid\share\sonder",
    "//?/UNC/attacker.invalid/share/sonder",
    r"\\.\pipe\sonder",
    "//./pipe/sonder",
    r"\??\C:\sonder",
    "/??/C:/sonder",
    r"\\?\GLOBALROOT\Device\HarddiskVolume1\sonder",
]

HOSTILE_NON_NETWORK_HOMES = [
    "relative/sonder-home",
    "sonder-home",
    "C:\\" if os.name == "nt" else "/",
    r"\\?\C:\\" if os.name == "nt" else "/",
    r"C:\sonder\..\.." if os.name == "nt" else "/a/..",
]
if os.name == "nt":
    HOSTILE_NON_NETWORK_HOMES.extend([
        r"C:\...",
        r"C:\sonder.",
        r"C:\sonder::$DATA",
    ])


@pytest.fixture()
def isolated_ws(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(runtime_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS_FILE", raising=False)
    monkeypatch.delenv("SONDER_HOME", raising=False)
    monkeypatch.delenv("SONDER_STATE_HOME", raising=False)
    return ws, home


def _install_hostile_fs_spy(monkeypatch, hostile_marker: str) -> list[tuple[str, str]]:
    """Fail immediately if any FS call touches a hostile UNC/device/NT spelling."""
    touched: list[tuple[str, str]] = []
    marker_cf = hostile_marker.replace("/", "\\").casefold()
    real_resolve = Path.resolve
    real_stat = Path.stat
    real_lstat = Path.lstat
    real_is_symlink = Path.is_symlink
    real_is_file = Path.is_file
    real_os_stat = os.stat
    real_isdir = os.path.isdir

    def _check(op: str, raw: object) -> None:
        text = str(raw).replace("/", "\\").casefold()
        if (
            marker_cf in text
            or text.startswith(("\\\\", "\\??\\"))
            and not runtime_paths._EXTENDED_LOCAL_RE.match(str(raw))
        ):
            touched.append((op, str(raw)))
            raise AssertionError(f"FS call {op} touched hostile path {raw!r}")

    def spy_resolve(self, strict=False):
        _check("Path.resolve", self)
        return real_resolve(self, strict=strict)

    def spy_stat(self, *args, **kwargs):
        _check("Path.stat", self)
        return real_stat(self, *args, **kwargs)

    def spy_lstat(self):
        _check("Path.lstat", self)
        return real_lstat(self)

    def spy_is_symlink(self):
        _check("Path.is_symlink", self)
        return real_is_symlink(self)

    def spy_is_file(self):
        _check("Path.is_file", self)
        return real_is_file(self)

    def spy_os_stat(path, *args, **kwargs):
        _check("os.stat", path)
        return real_os_stat(path, *args, **kwargs)

    def spy_isdir(path):
        _check("os.path.isdir", path)
        return real_isdir(path)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    monkeypatch.setattr(Path, "stat", spy_stat)
    monkeypatch.setattr(Path, "lstat", spy_lstat)
    monkeypatch.setattr(Path, "is_symlink", spy_is_symlink)
    monkeypatch.setattr(Path, "is_file", spy_is_file)
    monkeypatch.setattr(os, "stat", spy_os_stat)
    monkeypatch.setattr(os.path, "isdir", spy_isdir)
    return touched


# ---- 1. tools/ protection (FIRST_PARTY_CODE_DIRS) ---------------------------


@pytest.mark.parametrize("relative", [
    "tools/tuf_repo.py",
    "tools/evil.py",
    "tools/sub/helper.py",
    "tools/deploy.ps1",
    "tools/pytest.ini",
    "tools/setup.cfg",
    "tools/pyproject.toml",
])
def test_tools_code_and_config_mutations_require_developer_auth(isolated_ws, relative):
    ws, _home = isolated_ws
    assert "tools" in file_ops.FIRST_PARTY_CODE_DIRS
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(relative, PAYLOAD, mode="create")
    assert not (ws / relative).exists()

    # Pre-seed and verify edit, delete, move, batch_write, and read are refused.
    target = ws / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.edit_file(relative, "x = 1", "x = 2")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.delete_path(relative, dry_run=False, confirm=f"DELETE {target.resolve()}")
    with pytest.raises(PermissionError, match="read protected Sonder secret/control-plane"):
        file_ops.read_file(relative)
    with pytest.raises(file_ops.BatchWriteError) as batch_info:
        file_ops.batch_write_files([{"path": relative, "content": PAYLOAD, "mode": "overwrite"}])
    assert batch_info.value.report["transaction"] == "not_started"

    # Developer authorization clears the mutation guard.
    edited = file_ops.edit_file(relative, "x = 1", "x = 2", developer_authorized=True)
    assert edited["replacements"] == 1
    assert target.read_text(encoding="utf-8") == "x = 2\n"


def test_tools_move_and_copy_refused_without_token_and_nested_project_tools_allowed(isolated_ws):
    ws, _home = isolated_ws
    (ws / "tools").mkdir()
    src = ws / "project" / "tools" / "helper.py"
    out = file_ops.write_file("project/tools/helper.py", "value = 1\n", mode="create")
    assert out["bytes"] > 0
    assert src.is_file()

    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.copy_file("project/tools/helper.py", "tools/tuf_repo.py")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.move_file("project/tools/helper.py", "tools/evil.py")
    assert not (ws / "tools" / "tuf_repo.py").exists()
    assert not (ws / "tools" / "evil.py").exists()


# ---- 2. Hostile SONDER_HOME / SONDER_STATE_HOME in file_ops & control_plane -


@pytest.mark.parametrize("env_var", ["SONDER_HOME", "SONDER_STATE_HOME"])
@pytest.mark.parametrize("hostile", HOSTILE_NETWORK_OR_DEVICE_HOMES + HOSTILE_NON_NETWORK_HOMES)
def test_file_ops_refuses_hostile_state_home_with_zero_smb_calls(
    tmp_path, monkeypatch, env_var, hostile
):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(runtime_paths, "_HOME_OVERRIDE", None)
    monkeypatch.delenv("SONDER_HOME", raising=False)
    monkeypatch.delenv("SONDER_STATE_HOME", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS_FILE", raising=False)
    monkeypatch.setenv(env_var, hostile)

    touched = _install_hostile_fs_spy(monkeypatch, hostile) if hostile in HOSTILE_NETWORK_OR_DEVICE_HOMES else []

    roots = file_ops.allowed_roots()
    assert roots == [ws.resolve()]

    lane_roots = file_ops.lane_grant_roots()
    assert all(hostile not in entry for entry in lane_roots)

    with pytest.raises(PermissionError, match="refusing unsafe SONDER_HOME/SONDER_STATE_HOME"):
        file_ops.roots_file_path()

    cp_paths = file_ops._control_plane_paths()
    assert ws.resolve() / "permissions.json" in cp_paths

    with pytest.raises(PermissionError, match="refusing unsafe SONDER_"):
        control_plane_paths.live_control_plane_inventory()

    assert touched == []


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length local path spelling")
@pytest.mark.parametrize("prefix", ["\\\\?\\", "//?/"])
def test_valid_extended_local_state_home_is_stripped_cleanly(tmp_path, monkeypatch, prefix):
    ws = tmp_path / "workspace"
    home = tmp_path / "local-home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(runtime_paths, "_HOME_OVERRIDE", None)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS_FILE", raising=False)
    monkeypatch.delenv("SONDER_STATE_HOME", raising=False)
    raw_home = prefix + (str(home).replace("\\", "/") if prefix.startswith("//") else str(home))
    monkeypatch.setenv("SONDER_HOME", raw_home)

    roots = file_ops.allowed_roots()
    assert home.resolve() in roots
    assert file_ops.roots_file_path() == home / file_ops.DEFAULT_ROOTS_FILE
    cp_paths = file_ops._control_plane_paths()
    assert home.resolve() / "permissions.json" in cp_paths
    cfg = config_mod.load_config(env={"SONDER_HOME": raw_home})
    assert cfg.state.home == str(home)


# ---- 3. config.py: [state].home, artifact_mobility_source, [context].*_roots -


@pytest.mark.parametrize("env_var", ["SONDER_HOME", "SONDER_STATE_HOME"])
@pytest.mark.parametrize("hostile", HOSTILE_NETWORK_OR_DEVICE_HOMES + HOSTILE_NON_NETWORK_HOMES)
def test_config_load_refuses_hostile_state_home_env_with_zero_smb_calls(
    monkeypatch, env_var, hostile
):
    touched = _install_hostile_fs_spy(monkeypatch, hostile) if hostile in HOSTILE_NETWORK_OR_DEVICE_HOMES else []
    with pytest.raises(config_mod.ConfigError, match=r"\[state\]\.home must be a local absolute non-root path"):
        config_mod.load_config(env={env_var: hostile})
    assert touched == []


@pytest.mark.parametrize("hostile", HOSTILE_NETWORK_OR_DEVICE_HOMES + HOSTILE_NON_NETWORK_HOMES)
def test_config_toml_state_home_and_mobility_source_refuse_hostile_paths_lexically(
    tmp_path, monkeypatch, hostile
):
    touched = _install_hostile_fs_spy(monkeypatch, hostile) if hostile in HOSTILE_NETWORK_OR_DEVICE_HOMES else []
    valid_spool = tmp_path / "spool"
    valid_spool.mkdir()
    source = ArtifactMobilitySourceConfig(
        enabled=True,
        store_dir=str(valid_spool),
        principal_id="private-cluster",
        project_id="sonder",
        source_owner_id="node-a",
    )
    cfg_bad_home = config_mod.SonderConfig(
        state=replace(config_mod.StateConfig(), home=hostile),
        artifact_mobility_source=source,
    )
    source_errs = artifact_mobility_source_errors(cfg_bad_home)
    assert "[state].home invalid" in source_errs
    all_errs: list[str] = []
    config_mod._validate(cfg_bad_home, all_errs)
    assert any("[state].home" in err for err in all_errs)

    cfg_bad_spool = config_mod.SonderConfig(
        state=replace(config_mod.StateConfig(), home=str(tmp_path / "home")),
        artifact_mobility_source=replace(source, store_dir=hostile),
    )
    spool_errs = artifact_mobility_source_errors(cfg_bad_spool)
    assert "[artifact_mobility_source].store_dir invalid" in spool_errs
    assert touched == []


@pytest.mark.parametrize("field_name", [
    "instruction_bundled_roots",
    "instruction_global_roots",
    "instruction_configured_roots",
    "skill_bundled_roots",
    "skill_global_roots",
    "skill_configured_roots",
])
@pytest.mark.parametrize("hostile", HOSTILE_NETWORK_OR_DEVICE_HOMES + HOSTILE_NON_NETWORK_HOMES)
def test_context_root_errors_refuse_hostile_spellings_with_zero_smb_calls(
    monkeypatch, field_name, hostile
):
    touched = _install_hostile_fs_spy(monkeypatch, hostile) if hostile in HOSTILE_NETWORK_OR_DEVICE_HOMES else []
    ctx = replace(config_mod.ContextConfig(), **{field_name: (hostile,)})
    cfg = config_mod.SonderConfig(context=ctx)
    errs = config_mod._context_root_errors(cfg)
    assert f"[context].{field_name} root must be an absolute non-link path" in errs
    assert touched == []


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction check")
def test_context_root_errors_refuses_windows_junction_and_accepts_extended_local(tmp_path):
    real_dir = tmp_path / "real_ctx"
    real_dir.mkdir()
    junction = tmp_path / "ctx_junction"
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"mklink /J unavailable: {proc.stderr or proc.stdout}")
    cfg_junction = config_mod.SonderConfig(
        context=config_mod.ContextConfig(instruction_configured_roots=(str(junction),))
    )
    errs = config_mod._context_root_errors(cfg_junction)
    assert "[context].instruction_configured_roots root must be an absolute non-link path" in errs

    extended = "\\\\?\\" + str(real_dir)
    cfg_extended = config_mod.SonderConfig(
        context=config_mod.ContextConfig(instruction_configured_roots=(extended,))
    )
    assert config_mod._context_root_errors(cfg_extended) == []

    # F4: ancestor junction (ctx_junction/subdir) must also be refused.
    subdir = real_dir / "subdir"
    subdir.mkdir()
    cfg_ancestor_junction = config_mod.SonderConfig(
        context=config_mod.ContextConfig(
            instruction_configured_roots=(str(junction / "subdir"),)
        )
    )
    errs_ancestor = config_mod._context_root_errors(cfg_ancestor_junction)
    assert "[context].instruction_configured_roots root must be an absolute non-link path" in errs_ancestor


# ---- 4. F1: archive_tools.extract_archive into first-party code dirs --------


def _make_zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return path


def test_archive_extract_into_first_party_code_dirs_refused_without_dev_auth(isolated_ws):
    ws, _home = isolated_ws
    payload_zip = _make_zip(
        ws / "payload.zip",
        {"tuf_repo.py": PAYLOAD, "pytest.ini": "[pytest]\n"},
    )

    # Case 1: ws/tools does NOT exist yet -> destination == ws/tools.
    with pytest.raises(PermissionError, match="developer authorization"):
        archive_tools.extract_archive(str(payload_zip), str(ws / "tools"))
    assert not (ws / "tools").exists()

    # Case 2: ws/tools DOES exist -> destination == ws/tools/sub.
    (ws / "tools").mkdir()
    with pytest.raises(PermissionError, match="developer authorization"):
        archive_tools.extract_archive(str(payload_zip), str(ws / "tools" / "sub"))
    assert not (ws / "tools" / "sub").exists()

    # Case 3: other first-party & runtime package dirs (scripts, sonder_runtime, tests).
    conftest_zip = _make_zip(ws / "conftest.zip", {"conftest.py": PAYLOAD})
    for dirname in ("scripts", "sonder_runtime", "tests"):
        with pytest.raises(PermissionError, match="developer (authorization|token)"):
            archive_tools.extract_archive(str(conftest_zip), str(ws / dirname))
        assert not (ws / dirname).exists()

    # Case 4: benign sibling directory succeeds without developer auth.
    benign_zip = _make_zip(ws / "benign.zip", {"foo.py": "x = 1\n"})
    res = archive_tools.extract_archive(str(benign_zip), str(ws / "out"))
    assert res["ok"] is True
    assert (ws / "out" / "foo.py").is_file()

    # Case 5: developer_authorized=True clears the guard.
    res_dev = archive_tools.extract_archive(
        str(payload_zip), str(ws / "tools" / "dev_ok"), developer_authorized=True
    )
    assert res_dev["ok"] is True
    assert (ws / "tools" / "dev_ok" / "tuf_repo.py").is_file()


# ---- 5. F2: binary/bytecode/pth import-shadowing suffixes -------------------


@pytest.mark.parametrize("suffix", [".pyd", ".pyc", ".pyo", ".pyw", ".pth", ".so", ".dll"])
@pytest.mark.parametrize("directory", ["tools", "sonder_runtime"])
def test_first_party_binary_and_pth_suffixes_require_developer_auth(isolated_ws, directory, suffix):
    ws, _home = isolated_ws
    relative = f"{directory}/shadow{suffix}"
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(relative, "binary-payload", mode="create")
    assert not (ws / relative).exists()

    out = file_ops.write_file(relative, "binary-payload", mode="create", developer_authorized=True)
    assert out["bytes"] > 0
    assert (ws / relative).is_file()


# ---- 6. F3: leading/trailing control chars, reserved DOS devices, norm ------


CONTROL_CHAR_HOMES = [
    "\nC:\\sonder" if os.name == "nt" else "\n/tmp/sonder",
    "C:\\sonder\r\n" if os.name == "nt" else "/tmp/sonder\r\n",
    "\tC:\\sonder" if os.name == "nt" else "\t/tmp/sonder",
    "\x1fC:\\sonder" if os.name == "nt" else "\x1f/tmp/sonder",
]


@pytest.mark.parametrize("bad_home", CONTROL_CHAR_HOMES)
def test_parse_local_root_and_config_refuse_leading_trailing_control_chars(tmp_path, bad_home):
    assert runtime_paths.try_parse_local_root(bad_home) is None
    with pytest.raises(config_mod.ConfigError, match=r"\[state\]\.home must be a local absolute non-root path"):
        config_mod.load_config(env={"SONDER_HOME": bad_home})

    toml_file = tmp_path / "bad_control.toml"
    escaped = bad_home.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t").replace("\x1f", "\\u001f")
    toml_file.write_text(f'[state]\nhome = "{escaped}"\n', encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match=r"\[state\]\.home must be a local absolute non-root path"):
        config_mod.load_config(toml_path=toml_file, env={})


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved DOS device names")
@pytest.mark.parametrize("reserved", [
    r"C:\CON",
    r"C:\NUL",
    r"C:\tmp\NUL",
    r"C:\tmp\PRN.txt",
    r"C:\tmp\COM1",
    r"C:\tmp\LPT1",
    r"C:\tmp\CON .txt",
    r"C:\tmp\AUX\sub",
    r"C:\tmp\CONIN$",
    r"C:\tmp\CONOUT$",
])
def test_parse_local_root_and_config_refuse_windows_reserved_device_names(reserved):
    assert runtime_paths.try_parse_local_root(reserved) is None
    with pytest.raises(config_mod.ConfigError, match=r"\[state\]\.home must be a local absolute non-root path"):
        config_mod.load_config(env={"SONDER_HOME": reserved})


def test_load_config_unconditionally_normalizes_valid_state_home(tmp_path):
    home = tmp_path / "clean-home"
    home.mkdir()
    toml_file = tmp_path / "spaced.toml"
    escaped = str(home).replace("\\", "\\\\")
    toml_file.write_text(f'[state]\nhome = "  {escaped}  "\n', encoding="utf-8")
    cfg = config_mod.load_config(toml_path=toml_file, env={})
    assert cfg.state.home == str(home)


# ---- 7. F5: zero SMB/FS calls for remaining env overrides & config paths ----


@pytest.mark.parametrize("env_var", [
    "SONDER_DB",
    "SONDER_FANOUT_DB",
    "SONDER_WORKFLOWS",
    "SONDER_EMOTION_VECTORS",
    "SONDER_SYSTEM_PROFILE",
])
@pytest.mark.parametrize("hostile", HOSTILE_NETWORK_OR_DEVICE_HOMES[:5])
def test_file_ops_control_plane_paths_refuses_hostile_env_overrides_zero_smb(
    isolated_ws, monkeypatch, env_var, hostile
):
    ws, _home = isolated_ws
    monkeypatch.setenv(env_var, hostile)
    touched = _install_hostile_fs_spy(monkeypatch, hostile)
    cp_paths = file_ops._control_plane_paths()
    assert ws.resolve() / "permissions.json" in cp_paths
    assert touched == []


@pytest.mark.parametrize("env_var", [
    "SONDER_DB",
    "SONDER_FLEET_DB",
    "SONDER_SESSIONS_DB",
    "SONDER_FILE_ROOTS_FILE",
    "SONDER_WORKFLOWS",
    "SONDER_EMOTION_VECTORS",
    "SONDER_SYSTEM_PROFILE",
    "SONDER_RUNTIME_POLICY",
    "SONDER_ROTATION_STATE",
    "SONDER_CONFIG",
    "SONDER_SECRETS",
    "SONDER_MODEL_TOOL_POLICY",
    "SONDER_LANE_TEST_TARGETS_FILE",
    "SONDER_UNSAFE_LAB_AUDIT_PATH",
])
@pytest.mark.parametrize("hostile", [
    r"\\attacker.invalid\share\evil",
    r"\\?\UNC\attacker.invalid\share\evil",
    r"\??\C:\evil",
])
def test_live_control_plane_inventory_refuses_hostile_env_overrides_zero_smb(
    isolated_ws, monkeypatch, env_var, hostile
):
    _ws, _home = isolated_ws
    monkeypatch.setattr(runtime_paths, "_HOME_OVERRIDE", None)
    monkeypatch.setenv(env_var, hostile)
    touched = _install_hostile_fs_spy(monkeypatch, hostile)
    with pytest.raises(PermissionError, match="refusing unsafe"):
        control_plane_paths.live_control_plane_inventory()
    assert touched == []


def test_live_control_plane_inventory_refuses_post_resolve_drive_root(isolated_ws, monkeypatch):
    _ws, home = isolated_ws
    monkeypatch.setattr(runtime_paths, "_HOME_OVERRIDE", None)
    real_canonical = control_plane_paths._canonical
    root_path = Path("C:\\") if os.name == "nt" else Path("/")

    def fake_canonical(value):
        if Path(value) == home:
            return root_path
        return real_canonical(value)

    monkeypatch.setattr(control_plane_paths, "_canonical", fake_canonical)
    with pytest.raises(PermissionError, match="refusing unsafe SONDER_HOME"):
        control_plane_paths.live_control_plane_inventory()


@pytest.mark.parametrize("hostile", HOSTILE_NETWORK_OR_DEVICE_HOMES[:5])
def test_config_validate_refuses_hostile_workspace_and_ca_bundle_zero_smb(
    tmp_path, monkeypatch, hostile
):
    touched = _install_hostile_fs_spy(monkeypatch, hostile)
    home = str(tmp_path / "home")
    cfg = config_mod.SonderConfig(
        state=replace(
            config_mod.StateConfig(),
            home=home,
            default_workspace_root=hostile,
            workspace_roots=(hostile,),
        ),
        ollama=replace(config_mod.OllamaConfig(), ca_bundle=hostile),
    )
    errs: list[str] = []
    config_mod._validate(cfg, errs)
    assert any("[state].default_workspace_root" in err for err in errs)
    assert any("[state].workspace_roots" in err for err in errs)
    assert any("[ollama].ca_bundle" in err for err in errs)
    assert touched == []
