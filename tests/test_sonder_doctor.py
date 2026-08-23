"""Offline, deterministic tests for the read-only ``sonder doctor`` report.

Every collaborator is injected as a fake check callable -- no DB, no network,
no model, no clock, no subprocess. The default (production) checks are not
exercised here; the module's contract is that ``run_doctor`` faithfully rolls up
whatever registry it is handed and never lets one check abort the report.
"""
import sonder_doctor
from types import SimpleNamespace


def test_default_config_checks_use_packaged_configuration_boundary():
    source = open(sonder_doctor.__file__, encoding="utf-8").read()
    assert "from sonder_runtime.bootstrap.config_loading import (" in source
    assert "import sonder_config" not in source


def test_config_check_preserves_validated_output_from_packaged_boundary(monkeypatch):
    import sonder_runtime.platform.config as packaged_config

    monkeypatch.setattr(
        packaged_config,
        "load_config",
        lambda: SimpleNamespace(ollama=SimpleNamespace(url="http://local")),
    )
    assert sonder_doctor._check_config() == {
        "status": "ok",
        "detail": "ollama=http://local",
    }


def test_config_check_preserves_config_error_diagnostic(monkeypatch):
    import sonder_runtime.platform.config as packaged_config

    def fail():
        raise packaged_config.ConfigError(["bad config"])

    monkeypatch.setattr(packaged_config, "load_config", fail)
    assert sonder_doctor._check_config() == {
        "status": "fail",
        "detail": "config invalid: invalid configuration:\n  - bad config",
    }


def test_memory_quality_check_preserves_root_injection_compatibility(monkeypatch):
    import sys
    import types

    calls = []
    monkeypatch.setenv("SONDER_DB", "memory.sqlite")
    monkeypatch.setattr(
        sonder_doctor,
        "_summarize_memory_quality",
        lambda connect, audit, path: calls.append((connect, audit, path))
        or {"status": "ok", "detail": "delegated"},
    )
    fake_memory_quality = types.SimpleNamespace(audit=lambda _conn: {})
    monkeypatch.setitem(sys.modules, "memory_quality", fake_memory_quality)

    import sonder_runtime.adapters.memory_store as memory_store

    fake_connect = lambda path: path
    monkeypatch.setattr(memory_store, "connect", fake_connect)

    assert sonder_doctor._check_memory_quality() == {
        "status": "ok",
        "detail": "delegated",
    }
    assert calls == [(fake_connect, fake_memory_quality.audit, "memory.sqlite")]


def _ok(detail=""):
    return lambda: {"status": "ok", "detail": detail}


def _warn(detail=""):
    return lambda: {"status": "warn", "detail": detail}


def _fail(detail=""):
    return lambda: {"status": "fail", "detail": detail}


def _boom(message="kaboom"):
    def check():
        raise RuntimeError(message)

    return check


def test_all_ok_rolls_up_to_ok():
    report = sonder_doctor.run_doctor(
        {"a": _ok("fine"), "b": _ok(), "c": _ok()}
    )
    assert report["overall"] == "ok"
    assert [c["name"] for c in report["checks"]] == ["a", "b", "c"]
    assert all(c["status"] == "ok" for c in report["checks"])


def test_any_warn_without_fail_rolls_up_to_warn():
    report = sonder_doctor.run_doctor(
        {"a": _ok(), "b": _warn("hygiene"), "c": _ok()}
    )
    assert report["overall"] == "warn"
    warn_entry = next(c for c in report["checks"] if c["name"] == "b")
    assert warn_entry == {"name": "b", "status": "warn", "detail": "hygiene"}


def test_any_fail_dominates_warn_and_ok():
    report = sonder_doctor.run_doctor(
        {"a": _ok(), "b": _warn(), "c": _fail("broken")}
    )
    assert report["overall"] == "fail"


def test_raising_check_becomes_captured_fail_not_exception():
    # The whole point: a check that raises must not propagate. It becomes a
    # ``fail`` entry whose detail names the exception, and later checks still run.
    report = sonder_doctor.run_doctor(
        {"a": _ok(), "boom": _boom("disk gone"), "z": _ok("still ran")}
    )
    assert report["overall"] == "fail"
    boom = next(c for c in report["checks"] if c["name"] == "boom")
    assert boom["status"] == "fail"
    assert "RuntimeError" in boom["detail"]
    assert "disk gone" in boom["detail"]
    # The check after the exploding one still executed.
    tail = next(c for c in report["checks"] if c["name"] == "z")
    assert tail == {"name": "z", "status": "ok", "detail": "still ran"}


def test_skipped_is_neutral_all_skipped_is_ok():
    def skip_missing():
        return {"status": "skipped", "detail": "skipped: collaborator absent"}

    report = sonder_doctor.run_doctor(
        {"a": skip_missing, "b": skip_missing}
    )
    assert report["overall"] == "ok"
    assert all(c["status"] == "skipped" for c in report["checks"])


def test_skipped_does_not_mask_a_warn():
    report = sonder_doctor.run_doctor(
        {"a": lambda: "skipped", "b": _warn("something")}
    )
    assert report["overall"] == "warn"


def test_ordering_is_preserved_for_pairs_and_bare_callables():
    def first():
        return "ok"

    def second():
        return "ok"

    report = sonder_doctor.run_doctor([("later", first), second])
    # Pair name is honoured; bare callable falls back to __name__.
    assert [c["name"] for c in report["checks"]] == ["later", "second"]


def test_result_shapes_are_normalized():
    # Bare string, (status, detail) tuple, bool True, and a name-overriding
    # mapping all normalize to {name, status, detail}.
    checks = [
        ("s", lambda: "warn"),
        ("t", lambda: ("fail", "detail here")),
        ("b", lambda: True),
        ("m", lambda: {"name": "renamed", "status": "ok", "detail": "d"}),
    ]
    report = sonder_doctor.run_doctor(checks)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["s"]["status"] == "warn"
    assert by_name["t"] == {"name": "t", "status": "fail", "detail": "detail here"}
    assert by_name["b"]["status"] == "ok"
    # Mapping overrode the registry name.
    assert "renamed" in by_name
    assert by_name["renamed"]["status"] == "ok"


def test_render_report_summarizes_overall_and_each_check():
    report = sonder_doctor.run_doctor(
        {"config": _ok("loaded"), "ollama": _fail("127.0.0.1: refused")}
    )
    text = sonder_doctor.render_report(report)
    lines = text.splitlines()
    assert lines[0] == "sonder doctor: FAIL"
    body = "\n".join(lines[1:])
    assert "config" in body and "loaded" in body
    assert "ollama" in body and "refused" in body
    # Rendering is pure text: no ANSI escape sequences.
    assert "\x1b[" not in text


def test_render_report_handles_empty_registry():
    report = sonder_doctor.run_doctor({})
    assert report == {"overall": "ok", "checks": []}
    text = sonder_doctor.render_report(report)
    assert "sonder doctor: OK" in text
    assert "no checks run" in text


def test_run_doctor_is_read_only_does_not_mutate_registry_or_call_extra():
    calls = []

    def spy_ok():
        calls.append("ok")
        return "ok"

    registry = {"only": spy_ok}
    snapshot = dict(registry)
    report = sonder_doctor.run_doctor(registry)
    # The registry passed in is untouched (no injected keys, same identity map).
    assert registry == snapshot
    # Each check is invoked exactly once, nothing extra.
    assert calls == ["ok"]
    assert report["overall"] == "ok"


def test_default_checks_registry_is_read_only_pairs_and_stable():
    # We do not run the defaults (they touch env/db/network); we only assert the
    # registry is well-formed, ordered, and returns fresh pairs each call so a
    # caller mutating the list cannot corrupt the module.
    first = sonder_doctor.default_checks()
    second = sonder_doctor.default_checks()
    names = [name for name, _ in first]
    assert names == [
        "config",
        "storage_state",
        "storage_models",
        "schemas",
        "backup",
        "self_heal",
        "memory_quality",
        "runtime_policy",
        "ollama",
        "ollama_workers",
        "ollama_residency",
    ]
    assert all(callable(fn) for _, fn in first)
    first.append(("extra", lambda: "ok"))
    assert len(sonder_doctor.default_checks()) == len(second)


def test_schema_check_reports_healthy_counts(monkeypatch):
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations
    from types import SimpleNamespace

    monkeypatch.setattr(
        sonder_migrations,
        "status_all_read_only",
        lambda home: {
            "memory": SimpleNamespace(
                applied=("0001",), pending=(), unknown=(),
                checksum_mismatches=(), db_path="C:/private/memory.db",
            ),
            "fleet": SimpleNamespace(
                applied=("0001", "0002"), pending=(), unknown=(),
                checksum_mismatches=(), db_path="C:/private/fleet.db",
            ),
        },
    )

    config = SimpleNamespace(state=SimpleNamespace(home="C:/selected/home"))
    assert sonder_doctor.schema_check(config)() == {
        "status": "ok",
        "detail": "2 store(s) current; applied migrations=3",
    }


def test_schema_check_fails_safely_for_modified_history(monkeypatch):
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations
    from types import SimpleNamespace

    secret_path = "C:/Users/private/secret-memory.db"
    secret_migration = "0001_private_name"
    monkeypatch.setattr(
        sonder_migrations,
        "status_all_read_only",
        lambda home: {
            "memory": SimpleNamespace(
                applied=(secret_migration,), pending=("0002",), unknown=(),
                checksum_mismatches=(secret_migration,), db_path=secret_path,
            )
        },
    )

    config = SimpleNamespace(state=SimpleNamespace(home="C:/selected/home"))
    result = sonder_doctor.schema_check(config)()
    assert result == {
        "status": "fail",
        "detail": (
            "1 store(s); unhealthy history: modified=1 future=0; pending=1"
        ),
    }
    assert secret_path not in result["detail"]
    assert secret_migration not in result["detail"]
    report = sonder_doctor.run_doctor([
        ("schemas", sonder_doctor.schema_check(config))
    ])
    assert report["overall"] == "fail"


def _backup_config(tmp_path, *, enabled=True):
    return SimpleNamespace(
        backup=SimpleNamespace(enabled=enabled),
        state=SimpleNamespace(home=str(tmp_path)),
    )


def test_backup_check_is_ok_when_backups_are_disabled(tmp_path):
    config = _backup_config(tmp_path, enabled=False)
    assert sonder_doctor.backup_check(config)() == {
        "status": "ok",
        "detail": "backups disabled by configuration",
    }


def test_backup_check_warns_when_no_backup_has_ever_run(tmp_path):
    config = _backup_config(tmp_path)
    result = sonder_doctor.backup_check(config)()
    assert result["status"] == "warn"
    assert "no backups have been created yet" in result["detail"]


def test_backup_check_does_not_create_operations_db(tmp_path):
    """A read-only check must never have the write side effect that a plain
    OperationsStore() open would (auto-migration on first use)."""
    config = _backup_config(tmp_path)
    sonder_doctor.backup_check(config)()
    assert not (tmp_path / "operations.db").exists()


def _make_operations_store(tmp_path):
    from sonder_runtime.adapters.persistence.operations_store import OperationsStore

    return OperationsStore(str(tmp_path / "operations.db"))


def test_backup_check_warns_for_a_backup_still_running(tmp_path):
    store = _make_operations_store(tmp_path)
    store.start_backup_run("1.0.0")
    config = _backup_config(tmp_path)
    result = sonder_doctor.backup_check(config)()
    assert result["status"] == "warn"
    assert "running" in result["detail"]


def test_backup_check_fails_for_a_failed_backup(tmp_path):
    store = _make_operations_store(tmp_path)
    backup_id = store.start_backup_run("1.0.0")
    store.finish_backup_run(backup_id, status="failed", error_code="BackupError")
    config = _backup_config(tmp_path)
    result = sonder_doctor.backup_check(config)()
    assert result["status"] == "fail"
    assert backup_id in result["detail"]
    assert "BackupError" in result["detail"]


def test_backup_check_is_ok_for_a_fresh_verified_backup(tmp_path):
    store = _make_operations_store(tmp_path)
    backup_id = store.start_backup_run("1.0.0")
    store.finish_backup_run(backup_id, status="verified", total_bytes=100)
    config = _backup_config(tmp_path)
    result = sonder_doctor.backup_check(config)()
    assert result["status"] == "ok"
    assert "verified" in result["detail"]


def test_backup_check_warns_for_a_stale_verified_backup(tmp_path):
    import sqlite3

    store = _make_operations_store(tmp_path)
    backup_id = store.start_backup_run("1.0.0")
    store.finish_backup_run(backup_id, status="verified", total_bytes=100)
    conn = sqlite3.connect(str(tmp_path / "operations.db"))
    try:
        conn.execute(
            "UPDATE backup_run SET completed_at_utc = ? WHERE backup_id = ?",
            ("2000-01-01T00:00:00Z", backup_id),
        )
        conn.commit()
    finally:
        conn.close()
    config = _backup_config(tmp_path)
    result = sonder_doctor.backup_check(config, max_age_hours=48.0)()
    assert result["status"] == "warn"
    assert "old" in result["detail"]
