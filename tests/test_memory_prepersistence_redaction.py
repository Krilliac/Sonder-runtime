"""M-11: Pre-persistence secret redaction for memory_store, orchestrator, and memory_search."""
from __future__ import annotations

import pytest

import memory_store
import orchestrator
from sonder_runtime.adapters.memory_search import search_memory
from sonder_runtime.domain.security.redaction import REDACTED

pytestmark = pytest.mark.unit

_PEM_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAsecretpembody9988776655\n"
    "-----END RSA PRIVATE KEY-----"
)
_OPENAI_KEY = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_BEARER_SECRET = "my-secret-bearer-token-99887766"


def test_log_interaction_redacts_secrets_prior_to_sqlite_write(monkeypatch):
    monkeypatch.setenv("SONDER_API_KEY", "env-sonder-live-key-445566")
    conn = memory_store.connect(":memory:")
    try:
        task = (
            f"Deploy with key {_OPENAI_KEY} and Authorization: Bearer {_BEARER_SECRET}\n"
            f"{_PEM_KEY}"
        )
        retrieved = f"Prior note with aws={_AWS_KEY} and env-sonder-live-key-445566"
        response = f"Configured using Bearer {_BEARER_SECRET} and {_OPENAI_KEY}"

        memory_store.log_interaction(
            conn, "int-m11-1", task, retrieved, response, "code",
            session_id="sess-1", project="/proj/a",
        )

        row = conn.execute(
            "SELECT task, retrieved_ctx, response FROM interactions WHERE id='int-m11-1'"
        ).fetchone()
        assert row is not None
        raw_blob = "\n".join(str(col or "") for col in (row["task"], row["retrieved_ctx"], row["response"]))
        for secret in (
            _OPENAI_KEY,
            _AWS_KEY,
            _BEARER_SECRET,
            "secretpembody9988776655",
            "env-sonder-live-key-445566",
        ):
            assert secret not in raw_blob, raw_blob
        assert REDACTED in row["task"]
        assert REDACTED in row["retrieved_ctx"]
        assert REDACTED in row["response"]
    finally:
        conn.close()


def test_orchestrator_run_redacts_interaction_and_lesson_usage_before_storage():
    conn = memory_store.connect(":memory:")
    try:
        memory_store.add_lesson(conn, "L1", "Always validate auth tokens.", None, "seed")
        task = f"Fix auth with Authorization: Bearer {_BEARER_SECRET} and {_OPENAI_KEY}"
        live_reply = f"Done using {_PEM_KEY}"

        resp, iid = orchestrator.run_with_learning(
            conn,
            task,
            "code",
            lambda _prompt: live_reply,
            id_fn=lambda: "int-orch-m11",
            session_id="sess-orch",
            project="/proj/orch",
        )
        assert resp == live_reply

        row = conn.execute(
            "SELECT task, response FROM interactions WHERE id=?", (iid,)
        ).fetchone()
        assert row is not None
        assert _BEARER_SECRET not in row["task"]
        assert _OPENAI_KEY not in row["task"]
        assert "secretpembody9988776655" not in row["response"]
        assert REDACTED in row["task"]
        assert REDACTED in row["response"]

        usage_rows = conn.execute(
            "SELECT task FROM lesson_usage WHERE interaction_id=?", (iid,)
        ).fetchall()
        for usage in usage_rows:
            assert _BEARER_SECRET not in (usage["task"] or "")
            assert _OPENAI_KEY not in (usage["task"] or "")
    finally:
        conn.close()


def test_memory_entries_redact_secrets_prior_to_sqlite_write():
    conn = memory_store.connect(":memory:")
    try:
        memory_store.add_fact(
            conn, "F1", "/proj/a",
            f"Service uses {_OPENAI_KEY} and Bearer {_BEARER_SECRET}",
        )
        memory_store.upsert_preference(
            conn, "P1", "global", "auth_pref",
            f"Never log {_AWS_KEY} or {_PEM_KEY}",
        )
        memory_store.touch_session(conn, "S1", "/proj/a")
        memory_store.set_session_title(conn, "S1", f"Debug {_OPENAI_KEY}")
        memory_store.update_session_summary(conn, "S1", f"Used Bearer {_BEARER_SECRET}", "int-1")
        memory_store.update_session_project_summary(
            conn, "S1", "/proj/a", f"Project key {_AWS_KEY}", "int-1",
        )

        fact = conn.execute("SELECT text FROM facts WHERE id='F1'").fetchone()["text"]
        pref = conn.execute("SELECT text FROM preferences WHERE id='P1'").fetchone()["text"]
        sess = conn.execute("SELECT title, summary FROM sessions WHERE session_id='S1'").fetchone()
        psumm = conn.execute(
            "SELECT summary FROM session_project_summaries WHERE session_id='S1'"
        ).fetchone()["summary"]

        for blob in (fact, pref, sess["title"], sess["summary"], psumm):
            for secret in (_OPENAI_KEY, _AWS_KEY, _BEARER_SECRET, "secretpembody9988776655"):
                assert secret not in blob, blob
            assert REDACTED in blob
    finally:
        conn.close()


def test_memory_search_redacts_legacy_unredacted_rows_and_blocks_secret_probing():
    conn = memory_store.connect(":memory:")
    try:
        # Simulate pre-existing legacy rows inserted directly into SQLite before M-11.
        long_prefix = "needle-legacy " + ("x" * 190) + " "
        conn.execute(
            "INSERT INTO interactions(id, task, retrieved_ctx, response, tier, project, project_explicit) "
            "VALUES(?, ?, ?, ?, ?, ?, 1)",
            (
                "LEG-INT",
                f"{long_prefix}{_PEM_KEY} and Bearer {_BEARER_SECRET}",
                "",
                f"resp {_OPENAI_KEY}",
                "code",
                "/proj/a",
            ),
        )
        conn.execute(
            "INSERT INTO facts(id, project, text) VALUES(?, ?, ?)",
            ("LEG-FACT", "/proj/a", f"needle-legacy fact with {_OPENAI_KEY}"),
        )
        conn.execute(
            "INSERT INTO preferences(id, scope, key, text, enabled) VALUES(?, ?, ?, ?, 1)",
            ("LEG-PREF", "global", "k1", f"needle-legacy pref with {_AWS_KEY}"),
        )
        conn.execute(
            "INSERT INTO sessions(session_id, title, summary, project) VALUES(?, ?, ?, ?)",
            ("LEG-SESS", f"needle-legacy title Bearer {_BEARER_SECRET}", "", "/proj/a"),
        )
        conn.execute(
            "INSERT INTO lessons(id, text, source_interaction) VALUES(?, ?, ?)",
            ("LEG-LESS", f"needle-legacy lesson {_OPENAI_KEY}", "seed"),
        )
        conn.execute(
            "INSERT INTO lessons_fts(lesson_id, text) VALUES(?, ?)",
            ("LEG-LESS", f"needle-legacy lesson {_OPENAI_KEY}"),
        )
        conn.commit()

        # Searching by a benign term returns matches with every secret scrubbed.
        report = search_memory(conn, memory_store, "needle-legacy", limit=10)
        assert "LEG-INT" in report
        assert "LEG-FACT" in report
        assert "LEG-PREF" in report
        assert "LEG-SESS" in report
        assert "LEG-LESS" in report
        for secret in (_OPENAI_KEY, _AWS_KEY, _BEARER_SECRET, "secretpembody9988776655"):
            assert secret not in report, report
        assert REDACTED in report

        # Probing by the raw secret value itself returns 0 hits and redacts the query echo.
        probe = search_memory(conn, memory_store, _OPENAI_KEY, limit=10)
        assert _OPENAI_KEY not in probe
        assert "LEG-INT" not in probe
        assert "LEG-FACT" not in probe
        assert "LEG-LESS" not in probe
    finally:
        conn.close()
