import json
from pathlib import Path
import sqlite3
import time

import pytest

import file_ops
import sqlite_mutate as mutate


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    path = tmp_path / "records.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, name TEXT, active INTEGER)")
    conn.executemany(
        "INSERT INTO records VALUES (?, ?, ?)",
        [(1, "alpha", 1), (2, "beta", 1), (3, "gamma", 0)],
    )
    conn.commit()
    conn.close()
    return path


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, name, active FROM records ORDER BY id").fetchall()
    finally:
        conn.close()


def test_preview_executes_then_rolls_back_with_exact_row_count(database):
    before = _rows(database)
    result = mutate.mutate_sqlite(
        database, "UPDATE records SET active = ? WHERE active = ?", [0, 1],
    )
    assert result["mode"] == "preview"
    assert result["applied"] is False
    assert result["statement"] == "UPDATE"
    assert result["table"] == "records"
    assert result["rows_affected"] == 2
    assert _rows(database) == before
    assert not Path(str(database) + "-journal").exists()


@pytest.mark.parametrize(
    "sql,params,expected",
    [
        ("INSERT INTO records VALUES (?, ?, ?)", [4, "delta", 1], 4),
        ("UPDATE records SET name = ? WHERE id = ?", ["changed", 2], 2),
        ("DELETE FROM records WHERE id = ?", [3], None),
    ],
)
def test_apply_commits_one_parameterized_dml_statement(database, sql, params, expected):
    result = mutate.mutate_sqlite(database, sql, params, mode="apply")
    assert result["applied"] is True and result["rows_affected"] == 1
    row = next((row for row in _rows(database) if row[0] == (params[-1] if sql.startswith("UPDATE") else 4)), None)
    if sql.startswith("INSERT"):
        assert row == (4, "delta", 1)
    elif sql.startswith("UPDATE"):
        assert row[1] == "changed"
    else:
        assert all(item[0] != 3 for item in _rows(database))


@pytest.mark.parametrize(
    "sql,params,error",
    [
        ("SELECT * FROM records WHERE id = ?", [1], "start"),
        ("CREATE TABLE x (id INTEGER)", [1], "start"),
        ("PRAGMA user_version = ?", [1], "start"),
        ("ATTACH DATABASE ? AS other", ["x.db"], "start"),
        ("UPDATE records SET name = ?; DELETE FROM records WHERE id = ?", ["x", 1], "one SQL"),
        ("UPDATE records SET name = :name WHERE id = ?", ["x", 1], "named"),
        ("UPDATE records SET name = ?1 WHERE id = ?2", ["x", 1], "numbered"),
        ("UPDATE records SET name = 'literal' WHERE id = 1", [], "non-empty"),
        ("UPDATE records SET name = ? WHERE id = ?", ["x"], "placeholder count"),
        ("INSERT OR REPLACE INTO records VALUES (?, ?, ?)", [1, "x", 1], "REPLACE"),
        ("UPDATE OR REPLACE records SET id = ? WHERE id = ?", [1, 2], "REPLACE"),
        ("UPDATE records SET name = ? WHERE id = ? RETURNING id", ["x", 1], "RETURNING"),
    ],
)
def test_statement_and_parameter_surface_is_strict(database, sql, params, error):
    before = _rows(database)
    with pytest.raises(mutate.SqliteMutateError, match=error):
        mutate.mutate_sqlite(database, sql, params, mode="apply")
    assert _rows(database) == before


def test_comments_quotes_and_trailing_semicolon_do_not_confuse_parameter_scan(database):
    result = mutate.mutate_sqlite(
        database,
        "/* ? ignored */ UPDATE records SET name = ? WHERE name != '-- ?' AND id = ?; -- tail ?",
        ["safe", 1], mode="apply",
    )
    assert result["rows_affected"] == 1


def test_functions_triggers_cross_table_effects_and_system_tables_are_denied(database):
    before = _rows(database)
    with pytest.raises(mutate.SqliteMutateError, match="rejected"):
        mutate.mutate_sqlite(
            database, "UPDATE records SET name = upper(?) WHERE id = ?", ["x", 1], mode="apply",
        )
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE audit (value TEXT)")
    conn.execute(
        "CREATE TRIGGER audit_records AFTER UPDATE ON records "
        "BEGIN INSERT INTO audit VALUES (NEW.name); END"
    )
    conn.commit()
    conn.close()
    with pytest.raises(mutate.SqliteMutateError, match="rejected"):
        mutate.mutate_sqlite(
            database, "UPDATE records SET name = ? WHERE id = ?", ["x", 1], mode="apply",
        )
    assert _rows(database) == before
    authorizer, _state = mutate._authorizer("UPDATE", set())
    for action in (
        sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_CREATE_VTABLE,
    ):
        assert authorizer(action, "x", "", "main", None) == sqlite3.SQLITE_DENY


def test_virtual_table_mutation_is_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    path = tmp_path / "virtual.db"
    conn = sqlite3.connect(path)
    try:
        try:
            conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
        except sqlite3.OperationalError as exc:
            pytest.skip("FTS5 unavailable: %s" % exc)
        conn.execute("INSERT INTO docs(body) VALUES ('safe')")
        conn.commit()
        shadow_before = conn.execute(
            "SELECT id, block FROM docs_data ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    with pytest.raises(mutate.SqliteMutateError, match="rejected"):
        mutate.mutate_sqlite(path, "INSERT INTO docs(body) VALUES (?)", ["no"], mode="apply")
    with pytest.raises(mutate.SqliteMutateError, match="rejected"):
        mutate.mutate_sqlite(
            path, "DELETE FROM docs_data WHERE id = ?", [1], mode="apply",
        )
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT id, block FROM docs_data ORDER BY id"
        ).fetchall() == shadow_before
    finally:
        conn.close()


def test_row_cap_rolls_back_before_commit(database):
    before = _rows(database)
    with pytest.raises(mutate.SqliteMutateError, match="row ceiling"):
        mutate.mutate_sqlite(
            database, "UPDATE records SET active = ? WHERE id > ?", [0, 0],
            mode="apply", max_rows=1,
        )
    assert _rows(database) == before


def test_timeout_and_busy_wait_are_bounded(database):
    started = time.monotonic()
    with pytest.raises(mutate.SqliteMutateError, match="timeout|interrupted|rejected"):
        mutate.mutate_sqlite(
            database,
            "DELETE FROM records WHERE id IN (WITH RECURSIVE c(x) AS "
            "(VALUES(?) UNION ALL SELECT x + 1 FROM c WHERE x < ?) SELECT x FROM c)",
            [1, 100000000], mode="apply", timeout=0.05,
        )
    assert time.monotonic() - started < 2

    locker = sqlite3.connect(database, timeout=0)
    locker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises(mutate.SqliteMutateError, match="locked|failed"):
            mutate.mutate_sqlite(
                database, "DELETE FROM records WHERE id = ?", [1], mode="apply",
            )
    finally:
        locker.rollback()
        locker.close()
    assert time.monotonic() - started < 1


def test_database_parameter_and_statement_caps(database, monkeypatch):
    with pytest.raises(mutate.SqliteMutateError, match="size ceiling"):
        mutate.mutate_sqlite(
            database, "DELETE FROM records WHERE id = ?", [1], max_db_bytes=1024,
        )
    with pytest.raises(mutate.SqliteMutateError, match="signed 64-bit"):
        mutate.mutate_sqlite(
            database, "DELETE FROM records WHERE id = ?", [1 << 63],
        )
    monkeypatch.setattr(mutate, "MAX_PARAMETER_BYTES", 2)
    with pytest.raises(mutate.SqliteMutateError, match="parameter 0"):
        mutate.mutate_sqlite(database, "UPDATE records SET name = ?", ["long"])
    monkeypatch.setattr(mutate, "MAX_SQL_BYTES", 10)
    with pytest.raises(mutate.SqliteMutateError, match="statement bytes"):
        mutate.mutate_sqlite(database, "DELETE FROM records WHERE id = ?", [1])


def test_existing_large_row_is_not_limited_by_parameter_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    path = tmp_path / "large-row.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, payload TEXT, active INTEGER)")
    conn.execute("INSERT INTO records VALUES (1, ?, 0)", ("x" * 70_000,))
    conn.commit()
    conn.close()

    result = mutate.mutate_sqlite(
        path, "UPDATE records SET active = ? WHERE id = ?", [1, 1], mode="apply",
    )

    assert result["rows_affected"] == 1
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT length(payload), active FROM records WHERE id = 1"
        ).fetchone() == (70_000, 1)
    finally:
        conn.close()


def test_wal_growth_is_projected_and_rejected_before_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    path = tmp_path / "wal.db"
    keeper = sqlite3.connect(path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, payload TEXT, active INTEGER)")
        keeper.execute("INSERT INTO records VALUES (1, ?, 0)", ("x" * 32_000,))
        keeper.commit()
        keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        storage_before = mutate._storage_bytes(path)
        max_db_bytes = storage_before + 4096

        with pytest.raises(mutate.SqliteMutateError, match="projected WAL"):
            mutate.mutate_sqlite(
                path, "UPDATE records SET active = ? WHERE id = ?", [1, 1],
                mode="apply", max_db_bytes=max_db_bytes,
            )

        assert keeper.execute(
            "SELECT active FROM records WHERE id = 1"
        ).fetchone() == (0,)
        assert mutate._storage_bytes(path) <= max_db_bytes
    finally:
        keeper.close()


def test_resolution_identity_and_sidecar_reparse_are_revalidated(database, monkeypatch):
    before = _rows(database)
    real_resolve = file_ops.resolve_path
    calls = {"count": 0}

    def changed_resolution(*args, **kwargs):
        calls["count"] += 1
        resolved = real_resolve(*args, **kwargs)
        return resolved.parent / "other.db" if calls["count"] >= 2 else resolved

    monkeypatch.setattr(file_ops, "resolve_path", changed_resolution)
    with pytest.raises(mutate.SqliteMutateError, match="resolution changed"):
        mutate.mutate_sqlite(
            database, "UPDATE records SET name = ? WHERE id = ?", ["x", 1], mode="apply",
        )
    assert _rows(database) == before


def test_path_escape_sensitive_and_symlink_are_rejected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    outside = tmp_path / "outside.db"
    sqlite3.connect(outside).close()
    with pytest.raises(mutate.SqliteMutateError, match="outside allowed roots"):
        mutate.mutate_sqlite(outside, "DELETE FROM x WHERE id = ?", [1])
    metadata = workspace / ".git"
    metadata.mkdir()
    secret = metadata / "secret.db"
    sqlite3.connect(secret).close()
    with pytest.raises(mutate.SqliteMutateError, match="control state"):
        mutate.mutate_sqlite(secret, "DELETE FROM x WHERE id = ?", [1])
    link = workspace / "link.db"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip("symlink unavailable: %s" % exc)
    with pytest.raises(mutate.SqliteMutateError, match="symlink|junction"):
        mutate.mutate_sqlite("link.db", "DELETE FROM x WHERE id = ?", [1])


def test_custom_named_fanout_receipt_store_is_not_a_mutation_target(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    receipt = tmp_path / "runtime-receipts.sqlite"
    sqlite3.connect(receipt).close()
    monkeypatch.setenv("SONDER_FANOUT_DB", str(receipt))

    with pytest.raises(mutate.SqliteMutateError, match="protected Sonder secret/control-plane"):
        mutate.mutate_sqlite(receipt, "DELETE FROM fanout_results WHERE 1 = ?", [1])
