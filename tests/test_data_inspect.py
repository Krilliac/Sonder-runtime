"""data_inspect: structured, read-only previews across file types."""
from __future__ import annotations

import json
import sqlite3
import tarfile
import zipfile

import pytest

import file_ops


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    return tmp_path


def test_json_object_preview(roots):
    p = roots / "config.json"
    p.write_text(json.dumps({"name": "sonder", "port": 11435, "cloud": False}),
                 encoding="utf-8")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "json"
    assert out["type"] == "dict"
    assert "name" in out["keys"] and out["key_count"] == 3


def test_jsonl_record_count(roots):
    p = roots / "data.jsonl"
    p.write_text("\n".join(json.dumps({"i": i, "v": i * i}) for i in range(5)),
                 encoding="utf-8")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "jsonl"
    assert out["records"] == 5
    assert "i" in out["record_keys"]


def test_csv_columns_and_rows(roots):
    p = roots / "t.csv"
    p.write_text("name,score\nalice,10\nbob,20\n", encoding="utf-8")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "csv"
    assert out["rows"] == 2
    assert out["column_count"] == 2
    assert "name" in out["columns"]


def test_tsv_delimiter(roots):
    p = roots / "t.tsv"
    p.write_text("a\tb\tc\n1\t2\t3\n", encoding="utf-8")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "tsv"
    assert out["column_count"] == 3


def test_toml_tables(roots):
    p = roots / "sonder.toml"
    p.write_text("[server]\nport = 11435\n[state]\nhome = '/x'\n",
                 encoding="utf-8")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "toml"
    assert out["table_count"] == 2
    assert "server" in out["tables"]


def test_sqlite_tables_and_counts(roots):
    p = roots / "mem.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE facts (id INTEGER, body TEXT)")
    conn.executemany("INSERT INTO facts VALUES (?, ?)",
                     [(1, "a"), (2, "b"), (3, "c")])
    conn.commit()
    conn.close()
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "sqlite"
    assert out["tables"] == 1
    assert "facts: 3 rows" in out["text"]


def test_zip_members(roots):
    p = roots / "bundle.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.txt", "hello")
        z.writestr("b/c.txt", "world")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "zip"
    assert out["members"] == 2
    assert "a.txt" in out["text"]


def test_tar_members(roots):
    payload = roots / "x.txt"
    payload.write_text("data", encoding="utf-8")
    p = roots / "bundle.tar"
    with tarfile.open(p, "w") as t:
        t.add(payload, arcname="x.txt")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "tar"
    assert out["members"] == 1


def test_ini_sections(roots):
    p = roots / "app.ini"
    p.write_text("[core]\nkey = value\n[net]\nhost = local\n", encoding="utf-8")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "ini"
    assert out["section_count"] == 2


def test_unknown_text_falls_back_to_stats(roots):
    p = roots / "notes.md"
    p.write_text("# title\nline two\nline three\n", encoding="utf-8")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "text"
    assert out["lines"] == 3


def test_binary_signature(roots):
    p = roots / "blob.bin"
    p.write_bytes(bytes([0, 1, 2, 3, 255, 128]))
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "binary"
    assert "signature" in out


def test_malformed_json_reports_error_not_raise(roots):
    p = roots / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    out = file_ops.inspect_data(str(p))
    assert out["kind"] == "json"
    assert "error" in out


def test_rejects_path_outside_roots(roots, tmp_path):
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises((PermissionError, ValueError)):
        file_ops.inspect_data(str(outside))


def test_oversize_file_refused(roots):
    p = roots / "big.json"
    p.write_text("[" + ",".join("0" for _ in range(100)) + "]", encoding="utf-8")
    with pytest.raises(ValueError):
        file_ops.inspect_data(str(p), max_bytes=10)


def test_mcp_tool_wraps_result(roots):
    import server

    p = roots / "config.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    out = server.data_inspect(str(p))
    assert "data inspect" in out
    assert "kind: json" in out


def test_mcp_tool_reports_error_string(roots):
    import server

    out = server.data_inspect(str(roots / "missing.json"))
    assert out.startswith("ERROR:")
