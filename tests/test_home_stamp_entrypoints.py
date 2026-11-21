"""``migrate`` and ``--adopt-epoch2`` must not leave an unservable state home.

Regression: ``migrate`` on a fresh home created stores without the SPEC-5
epoch marker (so the next ``serve`` refused the home), and
``migrate --adopt-epoch2`` on a missing home raised a traceback.
"""
from __future__ import annotations

import pytest

from sonder_runtime.__main__ import main
from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
    EPOCH2_DATABASES,
    check_epoch,
    require_epoch_2,
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("SONDER_HOME", "SONDER_CONFIG", "SONDER_SECRETS", "SONDER_DB",
                 "SONDER_FLEET_DB", "SONDER_FLEET_PRINCIPAL_FILE", "OLLAMA_HOST"):
        monkeypatch.delenv(name, raising=False)


def test_migrate_on_fresh_home_stamps_it(tmp_path, clean_env, monkeypatch):
    home = tmp_path / "fresh-home"
    home.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(home))

    assert main(["migrate", "--set", f"state.home={home}"]) == 0

    for name in EPOCH2_DATABASES:
        assert check_epoch(home / name) == 2, name
    require_epoch_2(home)


def test_adopt_epoch2_on_missing_home_is_a_clear_error(
    tmp_path, clean_env, monkeypatch, capsys,
):
    home = tmp_path / "does-not-exist"
    monkeypatch.setenv("SONDER_HOME", str(home))

    result = main(["migrate", "--adopt-epoch2", "--set", f"state.home={home}"])

    assert result == 2
    assert "state home does not exist" in capsys.readouterr().err
    assert not home.exists()


# --- repl / mcp entry points -------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (  # noqa: E402
    MigrationRequired,
)
from sonder_runtime.platform.config import SonderConfig  # noqa: E402


@pytest.fixture
def home(tmp_path, clean_env, monkeypatch):
    path = tmp_path / "state-home"
    monkeypatch.setenv("SONDER_HOME", str(path))
    from sonder_runtime import __main__ as entrypoint

    monkeypatch.setattr(entrypoint, "_load_config", lambda _args: SonderConfig())
    monkeypatch.setattr(entrypoint, "_configure_typed_home", lambda _c: None)
    monkeypatch.setattr(entrypoint, "_export_runtime_environment", lambda *_a, **_k: None)
    return path


def _make_legacy_home(path):
    path.mkdir(parents=True, exist_ok=True)
    legacy = path / "memory.db"
    import sqlite3

    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE legacy_rows (id INTEGER)")
    conn.commit()
    conn.close()
    return legacy


def _stub_repl(monkeypatch):
    from sonder_runtime.adapters.persistence import migrations
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.bootstrap import legacy_interfaces
    from sonder_runtime.interfaces.repl import repl

    monkeypatch.setattr(migrations, "migrate_all", lambda **_k: None)
    monkeypatch.setattr(legacy_interfaces, "configure_legacy_interfaces", lambda: None)
    monkeypatch.setattr(legacy_interfaces, "configure_legacy_application", lambda _a: None)
    monkeypatch.setattr(bootstrap_app, "default_app", lambda *, config: object())
    monkeypatch.setattr(bootstrap_app, "start_inference_membership", lambda _a: None)
    monkeypatch.setattr(bootstrap_app, "close_default_runtime_resources", lambda *, timeout: None)
    monkeypatch.setattr(repl, "run_jsonl", lambda: None)


def _stub_native_mcp(monkeypatch):
    from sonder_runtime.adapters.persistence import migrations
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.bootstrap import native_mcp

    app = SimpleNamespace(close_providers=lambda *, timeout: None)
    monkeypatch.setattr(migrations, "migrate_all", lambda **_k: None)
    monkeypatch.setattr(bootstrap_app, "build_application", lambda *, config: app)
    monkeypatch.setattr(bootstrap_app, "start_inference_membership", lambda _a: None)
    monkeypatch.setattr(native_mcp, "run_native_mcp", lambda _a, **_k: 0)


def _stub_legacy_mcp(monkeypatch):
    from sonder_runtime import __main__ as entrypoint
    from sonder_runtime.adapters.inference import ollama_endpoint
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.bootstrap import legacy_mcp

    class Runtime:
        def require_startup_safety(self):
            return None

        def run(self, *, safety_checked):
            return None

    monkeypatch.setattr(entrypoint, "build_legacy_server_mcp_runtime", lambda: Runtime())
    monkeypatch.setattr(ollama_endpoint, "configure_typed_endpoint", lambda _o: None)
    monkeypatch.setattr(bootstrap_app, "default_app", lambda *, config: object())
    monkeypatch.setattr(bootstrap_app, "start_inference_membership", lambda _a: None)
    monkeypatch.setattr(bootstrap_app, "close_default_runtime_resources", lambda *, timeout: None)
    monkeypatch.setattr(legacy_mcp, "configure_legacy_application", lambda _a: None)


def _run_repl(_monkeypatch):
    from sonder_runtime import __main__ as entrypoint

    return entrypoint.cmd_repl(SimpleNamespace(json=True))


def _run_native_mcp(monkeypatch):
    from sonder_runtime import __main__ as entrypoint
    from sonder_runtime.adapters.security import unsafe_lab

    monkeypatch.setattr(unsafe_lab, "require_startup", lambda: None)
    return entrypoint.cmd_mcp(SimpleNamespace(native=True))


def _run_legacy_mcp(monkeypatch):
    from sonder_runtime import __main__ as entrypoint

    return entrypoint.cmd_mcp(SimpleNamespace(native=False))


@pytest.mark.parametrize(
    ("stub", "run"),
    [(_stub_repl, _run_repl), (_stub_native_mcp, _run_native_mcp),
     (_stub_legacy_mcp, _run_legacy_mcp)],
    ids=["repl", "native-mcp", "legacy-mcp"],
)
def test_entrypoint_on_fresh_home_stamps_it_for_serve(home, monkeypatch, stub, run):
    stub(monkeypatch)

    assert run(monkeypatch) == 0

    for name in EPOCH2_DATABASES:
        assert check_epoch(home / name) == 2, name
    require_epoch_2(home)


@pytest.mark.parametrize(
    ("stub", "run"),
    [(_stub_repl, _run_repl), (_stub_native_mcp, _run_native_mcp)],
    ids=["repl", "native-mcp"],
)
def test_entrypoint_refuses_a_legacy_home(home, monkeypatch, capsys, stub, run):
    legacy = _make_legacy_home(home)
    original = legacy.read_bytes()
    stub(monkeypatch)

    assert run(monkeypatch) == 1

    assert "migration required before startup" in capsys.readouterr().err
    assert legacy.read_bytes() == original
    assert check_epoch(legacy) is None
    with pytest.raises(MigrationRequired):
        require_epoch_2(home)


def test_legacy_mcp_leaves_a_legacy_home_untouched(home, monkeypatch):
    legacy = _make_legacy_home(home)
    original = legacy.read_bytes()
    before = sorted(p.name for p in home.glob("*.db"))
    _stub_legacy_mcp(monkeypatch)

    _run_legacy_mcp(monkeypatch)

    assert legacy.read_bytes() == original
    assert check_epoch(legacy) is None
    assert sorted(p.name for p in home.glob("*.db")) == before
