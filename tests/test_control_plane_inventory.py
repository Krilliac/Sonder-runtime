from pathlib import Path
import os
import sqlite3
import pytest
from sonder_runtime.platform import paths
from sonder_runtime.adapters.filesystem import file_ops


def test_supported_surrogate_names_keep_private_classification(tmp_path):
    from sonder_runtime.adapters.security.control_plane_paths import ControlPlaneInventory

    private = tmp_path / ("private-" + chr(0xDCFF))
    try:
        private.write_bytes(b"private")
    except (OSError, UnicodeError):
        pytest.skip("filesystem does not support surrogate test names")
    owned = tmp_path / "owned"
    owned.mkdir()
    inventory = ControlPlaneInventory(frozenset({private.resolve()}), (owned,), (), (), ())
    assert inventory.protects(private)
    assert inventory.protects(owned / private.name)
    assert not inventory.protects(tmp_path / ("ordinary-" + chr(0xDCFF)))


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths only")
def test_inventory_covers_extended_wal_sidecar_after_it_appears(tmp_path, monkeypatch):
    """A live SQLite sidecar keeps the same private identity after resolution."""
    from sonder_runtime.adapters.security.control_plane_paths import (
        ControlPlanePaths,
        live_control_plane_inventory,
    )

    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path / "state"))
    database = tmp_path / "private" / "fleet.db"
    database.parent.mkdir()
    sidecar = Path(str(database) + "-wal")
    assert not sidecar.exists()
    inventory = live_control_plane_inventory(
        additional=lambda: ControlPlanePaths(databases=(database,))
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records(value INTEGER)")
        connection.execute("INSERT INTO records VALUES(1)")
        connection.commit()
        assert sidecar.exists()
        extended_database = Path("\\\\?\\" + str(database))
        assert inventory.covers(ControlPlanePaths(databases=(database,)))
        assert inventory.covers(ControlPlanePaths(databases=(extended_database,)))
        assert inventory.protects(sidecar)
        assert inventory.protects(Path("\\\\?\\" + str(sidecar)))


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-path behavior only")
def test_inventory_protects_resolved_wal_sidecar_under_directory_symlink(
    tmp_path, monkeypatch
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        ControlPlanePaths,
        live_control_plane_inventory,
    )

    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "private-target"
    target.mkdir()
    link = tmp_path / "private-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable to this Windows test user")
    database = link / "fleet.db"
    inventory = live_control_plane_inventory(
        additional=lambda: ControlPlanePaths(databases=(database,))
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records(value INTEGER)")
        connection.execute("INSERT INTO records VALUES(1)")
        connection.commit()
        resolved_database = database.resolve()
        resolved_sidecar = Path(str(resolved_database) + "-wal")
        assert resolved_sidecar.exists()
        assert inventory.covers(ControlPlanePaths(databases=(resolved_database,)))
        assert inventory.protects(resolved_sidecar)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths only")
def test_windows_extended_final_path_normalization_is_allowlisted(tmp_path):
    from sonder_runtime.adapters.security.control_plane_paths import (
        _normalize_windows_extended_final_path,
    )

    normal = tmp_path / "private" / "state.db"
    assert _normalize_windows_extended_final_path(
        Path("\\\\?\\" + str(normal))
    ) == normal
    assert _normalize_windows_extended_final_path(
        Path("\\\\?\\UNC\\server\\share\\folder\\state.db")
    ) == Path("\\\\server\\share\\folder\\state.db")
    for value in (
        "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1",
        "\\\\?\\Volume{abc}\\state.db",
        "\\\\?\\C:relative",
        "\\\\?\\Ä:\\private\\state.db",
        "\\\\?\\C:\\private\\name.",
        "\\\\?\\C:\\private\\name ",
        "\\\\?\\C:\\private\\name:stream",
        "\\\\?\\C:\\private\\CON.txt",
        "\\\\?\\C:\\private\\COM¹.txt",
        "\\\\?\\C:\\private\\COM0.txt",
        "\\\\?\\C:\\private\\LPT².txt",
        "\\\\?\\C:\\private\\LPT³.txt",
        "\\\\?\\C:\\private\\CONIN$.txt",
        "\\\\?\\C:\\private\\CONOUT$",
        "\\\\?\\C:\\private\\CONOUT$.txt",
        "\\\\.\\PhysicalDrive0",
        "\\\\?\\UNC\\server",
        "\\\\?\\UNC\\\\share",
        "\\\\?\\UNC\\server.\\share\\state.db",
        "\\\\?\\UNC\\server\\share.\\state.db",
        "\\\\?\\UNC\\server\\share\\name.",
        "\\\\?\\UNC\\server\\share\\name ",
        "\\\\?\\UNC\\server\\share\\name:stream",
        "\\\\?\\UNC\\server\\share\\NUL.txt",
    ):
        with pytest.raises(ValueError, match="unsupported Windows private path namespace"):
            _normalize_windows_extended_final_path(Path(value))


def test_normal_windows_components_reject_extended_only_aliases():
    from sonder_runtime.adapters.security.control_plane_paths import (
        _normal_windows_components,
    )

    for value in (
        "private\\.\\state.db",
        "private\\..\\state.db",
        "private\\name/file",
        "server\\\\share",
        "server\\share\\\\state.db",
        "server\\share\\name/file",
        "server \\share",
        "server\\share ",
        "server\\share\\.\\state.db",
        "server\\share\\..\\state.db",
    ):
        with pytest.raises(ValueError, match="unsupported Windows private path namespace"):
            _normal_windows_components(value)


def test_windows_extended_final_path_normalization_leaves_other_platforms_unchanged(
    tmp_path, monkeypatch
):
    from sonder_runtime.adapters.security import control_plane_paths

    resolved = tmp_path / "private" / "state.db"
    monkeypatch.setattr(control_plane_paths.os, "name", "posix")
    assert (
        control_plane_paths._normalize_windows_extended_final_path(resolved)
        is resolved
    )


def test_control_inventory_keeps_different_database_owned_and_lock_paths_out(tmp_path):
    from sonder_runtime.adapters.security.control_plane_paths import (
        ControlPlanePaths,
        live_control_plane_inventory,
    )

    database = tmp_path / "private" / "fleet.db"
    owned = tmp_path / "private" / "output"
    locks = tmp_path / "private" / "locks"
    inventory = live_control_plane_inventory(
        additional=lambda: ControlPlanePaths(
            databases=(database,),
            owned_directories=(owned,),
            owner_lock_directories=(locks,),
        )
    )
    assert not inventory.covers(
        ControlPlanePaths(databases=(tmp_path / "other" / "fleet.db",))
    )
    assert not inventory.covers(
        ControlPlanePaths(owned_directories=(tmp_path / "other" / "output",))
    )
    assert not inventory.covers(
        ControlPlanePaths(owner_lock_directories=(tmp_path / "other" / "locks",))
    )


def test_inventory_path_bounds_use_filesystem_bytes_and_encoding_refuses(tmp_path, monkeypatch):
    from sonder_runtime.adapters.security.control_plane_paths import ControlPlaneInventory

    inventory = ControlPlaneInventory(frozenset(), (), (), (), ())
    with pytest.raises(ValueError, match="exceeds bound"):
        inventory.protects(tmp_path / ("é" * 4096))

    def unavailable(path):
        raise UnicodeError("encoding unavailable")

    monkeypatch.setattr(os, "fsencode", unavailable)
    with pytest.raises(ValueError, match="encoding is unavailable"):
        inventory.protects(tmp_path / "ordinary")


@pytest.fixture
def state(tmp_path, monkeypatch):
    for name in (
        "SONDER_FLEET_DB",
        "SONDER_APPROVALS_DB",
        "SONDER_DB",
        "SONDER_SESSIONS_DB",
        "SONDER_JOBS_DB",
        "SONDER_CHILD_SESSIONS_DB",
        "SONDER_QUEUED_ACTION_DB",
        "SONDER_SERVED_ACTION_RECEIPTS_DB",
        "SONDER_OPERATIONS_DB",
        "SONDER_AUTOPILOT_DB",
        "SONDER_COMPOSITION_DB",
        "SONDER_UPDATES_DB",
        "SONDER_TOOL_AUDIT",
        "SONDER_LANE_TEST_TARGETS_FILE",
        "SONDER_FANOUT_DB",
        "SONDER_EXTENSIONS_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "_configured_home", lambda: None)
    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "variable",
    [
        "SONDER_FLEET_DB",
        "SONDER_APPROVALS_DB",
        "SONDER_DB",
        "SONDER_SESSIONS_DB",
        "SONDER_JOBS_DB",
        "SONDER_CHILD_SESSIONS_DB",
    ],
)
def test_actual_relative_overrides_and_sqlite_sidecars_protected(
    state, monkeypatch, variable
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    monkeypatch.setenv(variable, "relocated/private.sqlite")
    inventory = live_control_plane_inventory()
    for suffix in ("", "-wal", "-shm", "-journal"):
        target = state / ("relocated/private.sqlite" + suffix)
        assert inventory.protects(target)
        assert file_ops._is_sensitive_control_path(target)
        with pytest.raises(PermissionError):
            file_ops.write_file(str(target), "tamper", bypass=True)
    assert not inventory.protects(state / "relocated/ordinary.txt")


def test_fleet_owner_locks_audit_rotations_and_catalog(state, monkeypatch):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    monkeypatch.setenv("SONDER_FLEET_DB", "fleet/custom.db")
    monkeypatch.setenv("SONDER_TOOL_AUDIT", "audit/custom.jsonl")
    monkeypatch.setenv("SONDER_LANE_TEST_TARGETS_FILE", "catalog/targets.json")
    inventory = live_control_plane_inventory()
    for name in (
        "fleet/lane-owner-" + "a" * 32 + ".lock",
        "audit/custom.20260904.1.jsonl",
        "catalog/targets.json",
    ):
        assert inventory.protects(state / name)
    assert not inventory.protects(state / "fleet/readme.txt")
    with pytest.raises(PermissionError):
        inventory.require_disjoint((state / "fleet",))


def test_additional_owned_store_scope_restores_default_on_exception(state):
    from sonder_runtime.adapters.security.control_plane_paths import (
        ControlPlanePaths,
        control_plane_scope,
    )

    owned = state / "output"
    extra = ControlPlanePaths(owned_directories=(owned,))
    with pytest.raises(RuntimeError):
        with control_plane_scope(extra):
            assert file_ops._is_sensitive_control_path(owned / "arbitrary.tmp")
            assert file_ops._is_sensitive_control_path(state / "state/fleet.db")
            raise RuntimeError("scope exit")
    assert not file_ops._is_sensitive_control_path(owned / "arbitrary.tmp")
    assert file_ops._is_sensitive_control_path(state / "state/fleet.db")


def test_inventory_is_readonly_bounded_and_configured_home_precedence(
    state, monkeypatch
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
        ControlPlanePaths,
    )

    configured = state / "configured"
    monkeypatch.setattr(paths, "_configured_home", lambda: configured)
    monkeypatch.setenv("SONDER_FLEET_DB", "ignored.db")
    monkeypatch.setenv("SONDER_SESSIONS_DB", "explicit-session.db")
    inventory = live_control_plane_inventory()
    assert inventory.protects(configured / "fleet.db")
    assert inventory.protects(state / "explicit-session.db")
    assert not configured.exists()
    with pytest.raises(ValueError):
        ControlPlanePaths(files=tuple(state / str(index) for index in range(257)))


def test_relocated_database_rejected_by_actual_sqlite_mutation_tool(state, monkeypatch):
    import sqlite3
    import sqlite_mutate as mutate

    database = state / "private.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE records(value INTEGER)")
        conn.execute("INSERT INTO records VALUES(1)")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(state))
    monkeypatch.setenv("SONDER_APPROVALS_DB", str(database))
    with pytest.raises(mutate.SqliteMutateError, match="control"):
        mutate.mutate_sqlite(database, "UPDATE records SET value=?", [2])
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM records").fetchone() == (1,)


def test_model_request_admission_store_resists_ordinary_tool_tampering(state, monkeypatch):
    import sqlite_mutate as mutate
    from sonder_runtime.adapters.model_request_admission import (
        HostModelRequestAdmission,
        ModelRequestRateConfig,
    )

    monkeypatch.setenv("SONDER_FILE_ROOTS", str(state))
    admission = HostModelRequestAdmission(ModelRequestRateConfig(1, 1), clock=lambda: 100.0)
    assert admission.try_acquire().allowed
    database = state / "state/model-request-admission.sqlite3"
    assert database.is_file()
    for suffix in ("", "-wal", "-shm", "-journal", ".lock"):
        target = Path(str(database) + suffix)
        with pytest.raises(PermissionError):
            file_ops.write_file(str(target), "tamper", mode="overwrite", bypass=True)
        with pytest.raises(PermissionError):
            file_ops.read_file(str(target))
    with pytest.raises(mutate.SqliteMutateError, match="control"):
        mutate.mutate_sqlite(database, "UPDATE model_request_rate SET tokens=?", [1])
    assert not HostModelRequestAdmission(
        ModelRequestRateConfig(1, 1), clock=lambda: 100.0,
    ).try_acquire().allowed


def test_live_additional_provider_and_context_scope_are_isolated(state):
    from contextvars import Context
    from sonder_runtime.adapters.security.control_plane_paths import (
        ControlPlanePaths,
        control_plane_scope,
        live_control_plane_inventory,
    )

    current = [ControlPlanePaths(owned_directories=(state / "output",))]
    first = live_control_plane_inventory(additional=lambda: current[0])
    current[0] = ControlPlanePaths(owned_directories=(state / "replacement",))
    second = live_control_plane_inventory(additional=lambda: current[0])
    assert first.protects(state / "output/a")
    assert second.protects(state / "replacement/a")
    with control_plane_scope(current[0]):
        assert file_ops._is_sensitive_control_path(state / "replacement/a")
        assert not Context().run(
            file_ops._is_sensitive_control_path, state / "replacement/a"
        )
    with pytest.raises(TypeError):
        live_control_plane_inventory(additional=lambda: {"files": []})


@pytest.mark.parametrize(
    "variable",
    [
        "SONDER_QUEUED_ACTION_DB",
        "SONDER_SERVED_ACTION_RECEIPTS_DB",
        "SONDER_OPERATIONS_DB",
        "SONDER_AUTOPILOT_DB",
        "SONDER_COMPOSITION_DB",
        "SONDER_UPDATES_DB",
    ],
)
def test_active_control_database_override_admission_and_sidecars(
    state, monkeypatch, variable
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    target = state / "isolated-control" / "relocated.db"
    monkeypatch.setenv(variable, str(target))
    inventory = live_control_plane_inventory()
    for suffix in ("", "-journal", "-wal", "-shm"):
        assert inventory.protects(Path(str(target) + suffix))
        with pytest.raises(PermissionError):
            file_ops.write_file(str(target) + suffix, "mutation", bypass=True)
    with pytest.raises(PermissionError):
        inventory.require_disjoint((target.parent,))


@pytest.mark.parametrize("variable", ["SONDER_RUNTIME_POLICY", "SONDER_ROTATION_STATE"])
def test_policy_secret_rotation_atomic_files_and_admission(
    state, monkeypatch, variable
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    target = state / "isolated-control" / "custom.json"
    monkeypatch.setenv(variable, str(target))
    inventory = live_control_plane_inventory()
    for suffix in ("", ".lock", ".tmp-123", ".tmp-" + "a" * 32):
        assert inventory.protects(Path(str(target) + suffix))
    if variable == "SONDER_RUNTIME_POLICY":
        assert inventory.protects(Path(str(target) + ".transition.json"))
        assert inventory.protects(
            Path(str(target) + ".transition.json.tmp-" + "b" * 32)
        )
    with pytest.raises(PermissionError):
        inventory.require_disjoint((target.parent,))


def test_database_manifest_matches_active_shared_literal_resolvers():
    import ast
    from sonder_runtime.adapters.security.control_plane_paths import STATE_DATABASES
    from sonder_runtime.adapters.persistence.migrations import _STORE_FILENAMES

    root = Path(__file__).resolve().parents[1] / "sonder_runtime"
    sources = (
        "bootstrap/app.py",
        "adapters/persistence/queued_actions.py",
        "adapters/persistence/served_action_receipts.py",
        "adapters/persistence/http_work_runs.py",
        "adapters/persistence/composition_store.py",
        "adapters/persistence/autopilot_store.py",
        "adapters/persistence/fanout_store.py",
        "adapters/persistence/fleet_store.py",
        "adapters/persistence/migrations.py",
        "adapters/embedding_cache.py",
        "adapters/web/lifecycle.py",
    )
    observed = {(filename, variable) for _, filename, variable in _STORE_FILENAMES}
    for source in sources:
        tree = ast.parse((root / source).read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name != "state_path" or not all(
                isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                for arg in node.args[:2]
            ):
                continue
            filename, variable = (arg.value for arg in node.args[:2])
            if filename.endswith(".db"):
                observed.add((filename, variable))
    # These two have distinct direct/session precedence in live inventory.
    covered = set(STATE_DATABASES) | {
        ("fleet.db", "SONDER_FLEET_DB"),
        ("sessions.db", "SONDER_SESSIONS_DB"),
    }
    assert observed <= covered


def test_direct_session_config_paths_preserve_literal_tilde_semantics(
    state, monkeypatch
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    monkeypatch.setenv("SONDER_SESSIONS_DB", "~/sessions.sqlite")
    monkeypatch.setenv("SONDER_SECRETS", "~/local.env")
    inventory = live_control_plane_inventory()
    assert inventory.protects(state / "~" / "sessions.sqlite")
    assert inventory.protects(state / "~" / "local.env")


@pytest.mark.parametrize(
    "variable",
    [
        "SONDER_FILE_ROOTS_FILE",
        "SONDER_WORKFLOWS",
        "SONDER_EMOTION_VECTORS",
        "SONDER_SYSTEM_PROFILE",
    ],
)
def test_relocated_filesystem_authority_config_is_in_admission_inventory(
    state, monkeypatch, variable
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    target = state / "independent-policy" / "custom.txt"
    monkeypatch.setenv(variable, str(target))
    inventory = live_control_plane_inventory()
    assert inventory.protects(target)
    with pytest.raises(PermissionError):
        inventory.require_disjoint((target.parent,))
