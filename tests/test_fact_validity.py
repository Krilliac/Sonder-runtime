"""Facts with validity intervals: migration, parity, supersede, recall."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import embeddings, memory_store
from sonder_runtime.adapters.persistence import migrations as sonder_migrations
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.memory.authoritative_fact_metadata import (
    fact_metadata_from_inputs,
)
from sonder_runtime.domain.memory.fact_validity import (
    FactValidity,
    fact_write_inputs,
)

# origin/main's facts DDL, verbatim, before validity columns existed.
_LEGACY_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    project TEXT,
    text TEXT,
    embedding BLOB,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
"""
# origin/main's recall queries, verbatim, for the parity comparison.
_LEGACY_RECALL = (
    "SELECT id, project, text, embedding FROM facts WHERE project=? "
    "ORDER BY ts ASC, rowid ASC"
)
_LEGACY_COUNT = "SELECT COUNT(*) FROM facts WHERE project=?"

_LEGACY_ROWS = [
    ("f1", "alpha", "uses cmake presets", b"\x00\x01", "2026-01-01 00:00:00"),
    ("f2", "alpha", "tests live in tests/", None, "2026-01-01 00:00:00"),
    ("f3", "beta", "python 3.12", None, "2025-06-01 10:00:00"),
    ("f4", "alpha", "ship on fridays", None, "2024-03-03 03:03:03"),
    ("f5", None, "unscoped legacy fact", None, "2026-02-02 02:02:02"),
]


def _legacy_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEGACY_FACTS_DDL)
    conn.executemany(
        "INSERT INTO facts(id, project, text, embedding, ts) VALUES(?,?,?,?,?)",
        _LEGACY_ROWS,
    )
    conn.commit()
    conn.close()


def _legacy_recall(path, project):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(_LEGACY_RECALL, (project,))]
        count = conn.execute(_LEGACY_COUNT, (project,)).fetchone()[0]
    finally:
        conn.close()
    return rows, count


def test_migration_on_legacy_schema_is_additive_and_recall_is_unchanged(tmp_path):
    db = tmp_path / "memory.db"
    _legacy_db(db)
    before = {p: _legacy_recall(db, p) for p in ("alpha", "beta", "missing")}

    conn = memory_store.connect(str(db))
    try:
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(facts)")}
        for name in ("valid_from", "valid_to", "superseded_by"):
            assert name in columns
            assert not columns[name][3] and columns[name][4] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM facts WHERE valid_from IS NOT NULL "
            "OR valid_to IS NOT NULL OR superseded_by IS NOT NULL"
        ).fetchone()[0] == 0
        raw = [tuple(r) for r in conn.execute(
            "SELECT id, project, text, embedding, ts FROM facts ORDER BY rowid"
        )]
        assert raw == _LEGACY_ROWS
        far_past = datetime(1990, 1, 1, tzinfo=timezone.utc)
        far_future = datetime(2400, 1, 1, tzinfo=timezone.utc)
        for project, (rows, count) in before.items():
            for now in (None, far_past, far_future):
                assert memory_store.facts_for_project(conn, project, now=now) == rows
                assert memory_store.count_facts(conn, project, now=now) == count
            expected = [r for r in rows if r["text"] == "uses cmake presets"]
            assert memory_store.find_duplicate_fact(conn, project, "USES  cmake presets") == (
                expected[0] if expected else None
            )
    finally:
        conn.close()
    # The migrated file still answers the legacy queries identically.
    assert {p: _legacy_recall(db, p) for p in before} == before


def test_ledgered_migration_applies_and_verifies_on_a_legacy_copy(tmp_path):
    db = tmp_path / "memory.db"
    _legacy_db(db)
    status = sonder_migrations.migrate_store("memory", str(db))
    assert "0003_fact_validity" in status.applied
    assert status.current
    again = sonder_migrations.migrate_store("memory", str(db))
    # The public migration receipt lists all applied ledger entries on replay.
    assert again.applied == status.applied
    assert again.current and again.pending == ()
    rows, count = _legacy_recall(db, "alpha")
    assert count == 3 and [r["id"] for r in rows] == ["f4", "f1", "f2"]


def test_migration_verify_refuses_a_defaulted_or_missing_column(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "m0003", Path(__file__).resolve().parents[1] / "migrations" / "memory" / "0003_fact_validity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = sqlite3.connect(":memory:")
    conn.executescript(_LEGACY_FACTS_DDL)
    with pytest.raises(RuntimeError, match="valid_from was not created"):
        module.verify(conn)
    conn.execute("ALTER TABLE facts ADD COLUMN valid_from TEXT DEFAULT 'x'")
    conn.execute("ALTER TABLE facts ADD COLUMN valid_to TEXT")
    conn.execute("ALTER TABLE facts ADD COLUMN superseded_by TEXT")
    with pytest.raises(RuntimeError, match="no DEFAULT"):
        module.verify(conn)


# --- store level -------------------------------------------------------------

T0 = datetime(2026, 5, 1, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = memory_store.connect(str(tmp_path / "mem.db"))
    yield c
    c.close()


def test_expired_and_future_facts_are_excluded_by_default(conn):
    memory_store.add_fact(conn, "open", "p", "legacy style")
    memory_store.add_fact(conn, "old", "p", "was true", validity=FactValidity(
        valid_from="2026-01-01T00:00:00Z", valid_to="2026-04-01T00:00:00Z",
    ))
    memory_store.add_fact(conn, "soon", "p", "will be true", validity=FactValidity(
        valid_from="2026-06-01T00:00:00+02:00",
    ))
    def ids(**kw):
        return [r["id"] for r in memory_store.facts_for_project(conn, "p", **kw)]

    assert ids(now=T0) == ["open"]
    assert memory_store.count_facts(conn, "p", now=T0) == 1
    assert ids(now=datetime(2026, 2, 1, tzinfo=timezone.utc)) == ["open", "old"]
    assert ids(now=datetime(2026, 7, 1, tzinfo=timezone.utc)) == ["open", "soon"]
    history = memory_store.facts_for_project(conn, "p", include_history=True)
    assert [r["id"] for r in history] == ["open", "old", "soon"]
    assert history[1]["valid_to"] == "2026-04-01T00:00:00.000000+00:00"
    assert history[2]["valid_from"] == "2026-05-31T22:00:00.000000+00:00"
    assert memory_store.count_facts(conn, "p", include_history=True) == 3
    # Default rows keep exactly the legacy shape.
    assert set(memory_store.facts_for_project(conn, "p", now=T0)[0]) == {
        "id", "project", "text", "embedding",
    }


def test_supersede_closes_predecessor_exactly_at_successor_start(conn):
    memory_store.add_fact(conn, "v1", "p", "port is 8080")
    memory_store.add_fact(
        conn, "v2", "p", "port is 9090", validity=FactValidity(supersedes="v1"), now=T0,
    )
    rows = {r["id"]: r for r in memory_store.facts_for_project(conn, "p", include_history=True)}
    assert rows["v1"]["valid_to"] == rows["v2"]["valid_from"] == T0.isoformat(timespec="microseconds")
    assert rows["v1"]["superseded_by"] == "v2"
    assert rows["v1"]["valid_from"] is None  # unknown start stays unknown
    later = T0 + timedelta(seconds=1)
    assert [r["text"] for r in memory_store.facts_for_project(conn, "p", now=later)] == ["port is 9090"]
    earlier = T0 - timedelta(seconds=1)
    assert [r["text"] for r in memory_store.facts_for_project(conn, "p", now=earlier)] == ["port is 8080"]


def test_supersede_never_extends_an_earlier_end(conn):
    memory_store.add_fact(conn, "v1", "p", "a", validity=FactValidity(
        valid_to="2026-02-01T00:00:00Z",
    ))
    memory_store.add_fact(conn, "v2", "p", "b", validity=FactValidity(supersedes="v1"), now=T0)
    row = memory_store.facts_for_project(conn, "p", include_history=True)[0]
    assert row["valid_to"] == "2026-02-01T00:00:00.000000+00:00"
    assert row["superseded_by"] == "v2"


@pytest.mark.parametrize("target, message", [
    ("missing", "no fact 'missing'"),
    ("other", "no fact 'other' in project 'p'"),
])
def test_supersede_refuses_unknown_or_cross_project_ids(conn, target, message):
    memory_store.add_fact(conn, "other", "q", "belongs to q")
    with pytest.raises(ValueError, match=message):
        memory_store.add_fact(conn, "new", "p", "x", validity=FactValidity(supersedes=target))
    assert memory_store.count_facts(conn, "p", include_history=True) == 0
    assert memory_store.facts_for_project(conn, "q", include_history=True)[0]["valid_to"] is None


def test_a_fact_can_be_superseded_only_once(conn):
    memory_store.add_fact(conn, "v1", "p", "a")
    memory_store.add_fact(conn, "v2", "p", "b", validity=FactValidity(supersedes="v1"), now=T0)
    with pytest.raises(ValueError, match="already superseded by 'v2'"):
        memory_store.add_fact(conn, "v3", "p", "c", validity=FactValidity(supersedes="v1"))
    assert memory_store.count_facts(conn, "p", include_history=True) == 2


def test_failed_supersede_rolls_back_the_close(conn):
    memory_store.add_fact(conn, "v1", "p", "a")
    with pytest.raises(ValueError, match="must follow the superseding instant"):
        memory_store.add_fact(conn, "v2", "p", "b", validity=FactValidity(
            supersedes="v1", valid_to="2020-01-01T00:00:00Z",
        ), now=T0)
    rows = memory_store.facts_for_project(conn, "p", include_history=True)
    assert [(r["id"], r["valid_to"], r["superseded_by"]) for r in rows] == [("v1", None, None)]
    assert not conn.in_transaction


def test_failed_supersede_inside_caller_transaction_keeps_caller_writes(conn):
    memory_store.add_fact(conn, "v1", "p", "a")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO facts(id, project, text) VALUES('pending', 'p', 'caller row')")
    with pytest.raises(ValueError):
        memory_store.add_fact(conn, "v2", "p", "b", validity=FactValidity(supersedes="nope"))
    # Only the failed pair is undone: the caller's pending row and its open
    # transaction survive for the caller to commit or roll back.
    assert conn.in_transaction
    ids = [r["id"] for r in memory_store.facts_for_project(conn, "p", include_history=True)]
    assert ids == ["v1", "pending"]


def test_validity_rejects_malformed_bounds():
    with pytest.raises(ValueError, match="timezone"):
        FactValidity(valid_from="2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="must follow valid_from"):
        FactValidity(valid_from="2026-02-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        FactValidity(supersedes="  ")


# --- input split / authoritative parity -------------------------------------

def test_fact_write_inputs_keeps_indexed_and_empty_inputs_on_the_old_decoder():
    assert fact_write_inputs() == (None, None)
    args = ('["parser"]', "", "2026-01-01T00:00:00Z", "", "", '["review:1"]')
    assert fact_write_inputs(*args) == (fact_metadata_from_inputs(*args), None)
    metadata, validity = fact_write_inputs(valid_until="2027-01-01T00:00:00Z")
    assert metadata is None and validity.valid_to.startswith("2027-01-01T00:00:00")
    with pytest.raises(ValueError, match="exceeds the input bound"):
        fact_write_inputs(valid_from="x" * 65)
    with pytest.raises(ValueError):
        fact_write_inputs(valid_from=5)


@pytest.mark.parametrize("inputs", [
    {"valid_from": "2026-01-01T05:00:00+05:00"},
    {"valid_until": "2027-01-01T00:00:00Z"},
    {"valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z"},
])
def test_authoritative_source_receives_the_same_metadata_as_before(inputs):
    _, validity = fact_write_inputs(**inputs)
    assert validity.authoritative_metadata() == fact_metadata_from_inputs(**inputs)


def test_authoritative_supersedes_without_claim_is_still_refused():
    _, validity = fact_write_inputs(supersedes="old")
    with pytest.raises(ValueError, match="requires an indexed claim"):
        validity.authoritative_metadata()


# --- tool surfaces ------------------------------------------------------------

@pytest.fixture
def legacy_surface(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db))
    monkeypatch.setattr(embeddings, "embed", lambda _text: None)
    return db


def _fact_id(result):
    return result.rsplit("id=", 1)[1].strip()


def _history(db, project="default"):
    conn = memory_store.connect(str(db))
    try:
        return memory_store.facts_for_project(conn, project, include_history=True)
    finally:
        conn.close()


def test_remember_fact_plain_call_writes_a_legacy_shaped_row(legacy_surface):
    result = server.sonder_remember_fact("plain fact")
    assert result.startswith("Remembered fact for project 'default' (1 total). id=")
    (row,) = _history(legacy_surface)
    assert (row["valid_from"], row["valid_to"], row["superseded_by"]) == (None, None, None)


def test_remember_fact_supersedes_and_recall_drops_the_old_fact(legacy_surface):
    old = _fact_id(server.sonder_remember_fact("deploy target is staging-1"))
    new_result = server.sonder_remember_fact("deploy target is staging-2", supersedes=old)
    assert "(1 total)" in new_result
    new = _fact_id(new_result)
    rows = {r["id"]: r for r in _history(legacy_surface)}
    assert rows[old]["superseded_by"] == new
    assert rows[old]["valid_to"] == rows[new]["valid_from"]
    injected = server._ensemble_prompt_with_project_facts("do the deploy", "default")
    assert "staging-2" in injected and "staging-1" not in injected
    search = server.memory_search("deploy target")
    assert new in search and old not in search
    # Re-asserting the retired statement is no longer a duplicate.
    again = server.sonder_remember_fact("deploy target is staging-1")
    assert again.startswith("Remembered fact")


def test_remember_fact_expired_fact_is_stored_but_not_recalled(legacy_surface):
    result = server.sonder_remember_fact(
        "freeze until march", valid_from="2026-01-01T00:00:00Z",
        valid_until="2026-03-01T00:00:00Z",
    )
    assert "(0 total)" in result
    assert len(_history(legacy_surface)) == 1
    assert "freeze until march" not in server._ensemble_prompt_with_project_facts("t", "default")


@pytest.mark.parametrize("kwargs", [
    {"supersedes": "does-not-exist"},
    {"valid_from": "2026-01-01T00:00:00"},
    {"valid_from": "2026-02-01T00:00:00Z", "valid_until": "2026-01-01T00:00:00Z"},
])
def test_remember_fact_rejects_bad_validity_without_writing(legacy_surface, kwargs):
    with pytest.raises(InvalidInput):
        server.sonder_remember_fact("anything", **kwargs)
    assert _history(legacy_surface) == []


def test_remember_fact_indexed_metadata_still_needs_the_authoritative_source(legacy_surface):
    with pytest.raises(InvalidInput, match="requires the configured fact source"):
        server.sonder_remember_fact(
            "x", entities_json='["parser"]', provenance_json='["review:1"]',
        )


def test_authoritative_surface_keeps_prior_validity_behavior(monkeypatch, tmp_path):
    from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
        SQLiteAuthoritativeFactSource,
    )
    from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter

    db = tmp_path / "memory.db"
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    seen = []
    original = source.add_fact

    def recording_add_fact(conn, fact_id, project, text, embedding, metadata):
        seen.append(metadata)
        return original(conn, fact_id, project, text, embedding, metadata)

    monkeypatch.setattr(source, "add_fact", recording_add_fact)
    application = SimpleNamespace(
        unit_of_work=lambda db_path=None: UnitOfWorkAdapter(str(db), authoritative_fact_source=source),
    )
    monkeypatch.setattr(server, "_application", lambda: application)
    monkeypatch.setattr(server, "_DB_PATH", str(db))
    monkeypatch.setattr(embeddings, "embed", lambda _text: None)

    server.sonder_remember_fact("a", project="repo-a", valid_from="2026-01-01T00:00:00Z")
    assert seen == [fact_metadata_from_inputs(valid_from="2026-01-01T00:00:00Z")]
    with pytest.raises(InvalidInput, match="requires an indexed claim"):
        server.sonder_remember_fact("b", project="repo-a", supersedes="whatever")
    assert len(seen) == 1
