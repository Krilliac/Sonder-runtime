"""Security and integration tests for the bounded read-only data_query tool."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

import data_query
import server


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    return tmp_path


@pytest.fixture
def database(roots):
    path = roots / "records.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE records (id INTEGER, name TEXT, active INTEGER)")
    conn.executemany(
        "INSERT INTO records VALUES (?, ?, ?)",
        [(1, "alpha", 1), (2, "beta", 0), (3, "gamma", 1)],
    )
    conn.commit()
    conn.close()
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sqlite_select_and_cte_are_structured_and_read_only(database):
    before = _digest(database)
    result = data_query.query_data(
        database,
        sql=(
            "WITH chosen AS (SELECT id, upper(name) AS name FROM records "
            "WHERE active = 1) SELECT id, name FROM chosen ORDER BY id"
        ),
    )
    assert result["kind"] == "sqlite"
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [
        {"id": 1, "name": "ALPHA"}, {"id": 3, "name": "GAMMA"},
    ]
    assert result["truncated"] is False
    assert _digest(database) == before
    assert not Path(str(database) + "-journal").exists()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM records",
        "CREATE TABLE injected (value TEXT)",
        "WITH chosen AS (SELECT id FROM records) UPDATE records SET name = 'x'",
        "PRAGMA user_version",
        "ATTACH DATABASE 'other.db' AS other",
        "SELECT 1; SELECT 2",
        "WITH changed AS (DELETE FROM records RETURNING id) SELECT * FROM changed",
        "SELECT load_extension('evil')",
        "SELECT random()",
    ],
)
def test_sqlite_injection_side_effects_and_non_whitelisted_functions_denied(
    database, sql,
):
    before = _digest(database)
    with pytest.raises(data_query.DataQueryError, match="accepts|rejected"):
        data_query.query_data(database, sql=sql)
    assert _digest(database) == before
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 3
    finally:
        conn.close()


def test_sqlite_comments_single_statement_and_whitelisted_aggregate(database):
    result = data_query.query_data(
        database, sql="/* bounded */\n-- one statement\nSELECT count(*) AS total FROM records;",
    )
    assert result["rows"] == [{"total": 3}]


def test_sqlite_row_and_output_limits_truncate(database):
    conn = sqlite3.connect(database)
    conn.executemany(
        "INSERT INTO records VALUES (?, ?, 1)",
        [(number, "x" * 400) for number in range(4, 204)],
    )
    conn.commit()
    conn.close()
    result = data_query.query_data(
        database, sql="SELECT id, name FROM records ORDER BY id",
        max_rows=1000, max_output_bytes=1024,
    )
    encoded = data_query.encode_result(result).encode("utf-8")
    assert result["truncated"] is True
    assert 0 < result["count"] < 203
    assert len(encoded) <= 1024
    assert result["output_bytes"] == len(encoded)


def test_sqlite_column_limit_and_duplicate_names_rejected(database):
    with pytest.raises(data_query.DataQueryError, match="column|columns"):
        data_query.query_data(
            database, sql="SELECT id, name FROM records", max_columns=1,
        )
    with pytest.raises(data_query.DataQueryError, match="duplicate"):
        data_query.query_data(database, sql="SELECT id, id FROM records")


def test_sqlite_result_column_limit_does_not_reject_wide_schema(roots):
    path = roots / "wide.db"
    columns = ["c%d" % index for index in range(80)]
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE wide (%s)" % ", ".join(columns))
    conn.execute(
        "INSERT INTO wide VALUES (%s)" % ", ".join("?" for _ in columns),
        list(range(len(columns))),
    )
    conn.commit()
    conn.close()

    result = data_query.query_data(path, sql="SELECT c0 FROM wide", max_columns=1)
    assert result["columns"] == ["c0"]
    assert result["rows"] == [{"c0": 0}]
    with pytest.raises(data_query.DataQueryError, match="column ceiling"):
        data_query.query_data(path, sql="SELECT * FROM wide", max_columns=50)


def test_sqlite_progress_deadline_interrupts_huge_recursive_query(database):
    started = time.monotonic()
    with pytest.raises(data_query.DataQueryError, match="interrupted|rejected"):
        data_query.query_data(
            database,
            sql=(
                "WITH RECURSIVE counter(x) AS (VALUES(1) UNION ALL "
                "SELECT x + 1 FROM counter WHERE x < 100000000) "
                "SELECT sum(x) AS total FROM counter"
            ),
            timeout=0.05,
        )
    assert time.monotonic() - started < 2


def test_sqlite_lock_wait_is_zero(database):
    locker = sqlite3.connect(database, timeout=0)
    locker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises(data_query.DataQueryError, match="locked|open|rejected"):
            data_query.query_data(database, sql="SELECT * FROM records")
    finally:
        locker.rollback()
        locker.close()
    assert time.monotonic() - started < 1


def test_json_projection_pointer_and_exact_typed_filter(roots):
    path = roots / "records.json"
    path.write_text(json.dumps([
        {"id": 1, "active": True, "nested": {"name": "alpha"}},
        {"id": True, "active": True, "nested": {"name": "wrong type"}},
        {"id": 2, "active": False, "nested": {"name": "beta"}},
    ]), encoding="utf-8")
    result = data_query.query_data(
        path,
        projection=["id", "/nested/name", "/missing"],
        filters={"id": 1, "/nested/name": "alpha"},
    )
    assert result["rows"] == [{
        "id": 1, "/nested/name": "alpha", "/missing": None,
    }]
    assert result["columns"] == ["id", "/nested/name", "/missing"]


def test_json_loading_is_included_in_timeout(roots, monkeypatch):
    path = roots / "records.json"
    path.write_text('[{"id":1}]', encoding="utf-8")
    original_load = data_query.json.load

    def slow_load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        # Keep enough margin for coarse Windows timer scheduling.
        time.sleep(0.12)
        return result

    monkeypatch.setattr(data_query.json, "load", slow_load)
    with pytest.raises(data_query.DataQueryError, match="timeout ceiling"):
        data_query.query_data(path, timeout=0.05)


def test_structured_records_and_column_ceiling_are_enforced(roots):
    path = roots / "records.json"
    path.write_text('[{"a":1,"b":2}]', encoding="utf-8")
    with pytest.raises(data_query.DataQueryError, match="column ceiling"):
        data_query.query_data(path, max_columns=1)
    path.write_text('[1,2,3]', encoding="utf-8")
    with pytest.raises(data_query.DataQueryError, match="JSON objects"):
        data_query.query_data(path)


def test_jsonl_and_csv_stream_rows_with_exact_filters(roots):
    jsonl = roots / "records.jsonl"
    jsonl.write_text("\n".join(
        json.dumps({"id": i, "group": i % 2}) for i in range(10)
    ), encoding="utf-8")
    result = data_query.query_data(
        jsonl, projection=["id"], filters={"group": 1}, max_rows=2,
    )
    assert result["rows"] == [{"id": 1}, {"id": 3}]
    assert result["truncated"] is True

    csv_path = roots / "records.csv"
    csv_path.write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8")
    result = data_query.query_data(
        csv_path, projection=["name"], filters={"id": "2"},
    )
    assert result["rows"] == [{"name": "beta"}]
    assert data_query.query_data(csv_path, filters={"id": 2})["rows"] == []


@pytest.mark.parametrize(
    "name,payload,error",
    [
        ("bad.json", "{bad", "malformed JSON"),
        ("bad.jsonl", '{"ok": 1}\n{bad', "line 2"),
        ("bad.csv", "id,id\n1,2\n", "duplicates"),
    ],
)
def test_malformed_structured_inputs_return_stable_errors(roots, name, payload, error):
    path = roots / name
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(data_query.DataQueryError, match=error):
        data_query.query_data(path)


def test_structured_args_reject_expressions_and_invalid_json(roots):
    path = roots / "records.json"
    path.write_text('[{"id":1}]', encoding="utf-8")
    with pytest.raises(data_query.DataQueryError, match="list of strings"):
        data_query.query_data(path, projection={"$eval": "id > 0"})
    with pytest.raises(data_query.DataQueryError, match="valid JSON"):
        data_query.query_data(path, filters="{not-json")
    with pytest.raises(data_query.DataQueryError, match="invalid escape"):
        data_query.query_data(path, projection=["/~2bad"])
    path.write_text('[{"value":NaN}]', encoding="utf-8")
    with pytest.raises(data_query.DataQueryError, match="malformed JSON"):
        data_query.query_data(path)


def test_scan_ceiling_rejects_oversized_text_data(roots):
    path = roots / "large.jsonl"
    path.write_text("{}\n" * 1000, encoding="utf-8")
    with pytest.raises(data_query.DataQueryError, match="scan byte ceiling"):
        data_query.query_data(path, max_scan_bytes=1024)


def test_path_escape_sensitive_and_symlink_are_rejected(roots, tmp_path):
    outside = tmp_path.parent / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    with pytest.raises(data_query.DataQueryError, match="path rejected"):
        data_query.query_data(outside)

    secret = roots / ".env"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    with pytest.raises(data_query.DataQueryError, match="path rejected"):
        data_query.query_data(secret)

    metadata = roots / ".git"
    metadata.mkdir()
    metadata_db = metadata / "query.db"
    sqlite3.connect(metadata_db).close()
    with pytest.raises(data_query.DataQueryError, match="control state"):
        data_query.query_data(metadata_db, sql="SELECT 1")

    target = roots / "target.json"
    target.write_text("[]", encoding="utf-8")
    link = roots / "link.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    with pytest.raises(data_query.DataQueryError, match="symlink"):
        data_query.query_data(link)


def test_server_manifest_dispatch_read_only_project_dedup_and_activity(
    roots, monkeypatch,
):
    path = roots / "records.jsonl"
    path.write_text('{"id":1}\n{"id":2}\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        server.activity_tracker, "record_tool_result",
        lambda name, args, **kwargs: calls.append((name, kwargs)),
    )
    direct = json.loads(server.data_query(
        str(path), projection_json='["id"]', max_rows=1,
    ))
    assert direct["rows"] == [{"id": 1}]
    assert calls[-1][0] == "data_query" and calls[-1][1]["ok"] is True

    dispatched = json.loads(server._agent_dispatch(
        "data_query",
        {"path": str(path), "projection_json": ["id"], "max_rows": 2},
        read_only=True,
        repository_extra_roots=str(roots),
    ))
    assert dispatched["count"] == 2
    assert "data_inspect/data_query" in server.tool_manifest()
    assert "- data_query:" in server._agent_tool_help(read_only=True)
    assert "data_query" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "data_query" in server._PROJECT_SCOPED_PATH_TOOLS
    assert "data_query" in server._WORK_INSPECTION_TOOLS
    assert "data_query" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert "data_query" in server._AUTOPILOT_OBSERVE_TOOLS
