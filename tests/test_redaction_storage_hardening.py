"""Regression tests for Wave 2 redaction, work-run storage, activity isolation,
session export, and SQLite concurrency/hygiene hardening.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from sonder_runtime.adapters import memory_store
from sonder_runtime.adapters.observability import activity_tracker as at
from sonder_runtime.adapters.persistence import http_work_runs
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.query_export import (
    SessionEventRecord,
    SessionQueryEngine,
)
from sonder_runtime.bootstrap import http_work_narration
from sonder_runtime.domain import chat_tools, work_narration
from sonder_runtime.domain.security import redaction
from sonder_runtime.interfaces.http.work_runs import WorkRunner, current_run_id
from sonder_runtime.platform import logging as platform_logging

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Canonical redaction (domain/security/redaction.py + platform/logging.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,leaks,must_keep",
    [
        ('password: "correct horse=staple"', ("correct", "horse", "staple"), ()),
        ('token: "part1, part2"', ("part1", "part2"), ()),
        ('{"token": "part1, part2", "user": "bob"}', ("part1", "part2"), ('"user": "bob"',)),
        ('password="foo\\"bar secret" next', ("foo", "bar", "secret"), ("next",)),
        ("password: 'a=b, c; d'", ("a=b", "c; d"), ()),
        ('password: "correct horse\'s=staple"', ("correct", "horse", "staple"), ()),
        ('password=hunter2, note="hello world"', ("hunter2",), ('note="hello world"',)),
        (
            'Authorization: Digest username="bob", realm="http-auth@example.org", '
            'nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v", '
            'response="753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"',
            ("http-auth@example.org", "7ypf/xlj9", "753927fa0e85d155"),
            (),
        ),
        (
            "Authorization: AWS4-HMAC-SHA256 "
            "Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
            "SignedHeaders=content-type;host;x-amz-date, "
            "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7",
            ("AKIDEXAMPLE", "5d672d79c15b1316"),
            (),
        ),
        (
            '{"Authorization": "Digest username=\\"bob\\", response=\\"deadbeef1234\\"", "x": 1}',
            ("deadbeef1234", "bob"),
            ('"x": 1}',),
        ),
        (
            "curl -H 'Authorization: Digest username=\"bob\", response=\"deadbeef1234\"' https://host/x",
            ("deadbeef1234", "bob"),
            ("https://host/x",),
        ),
        (
            "Authorization: Bearer fixture-token-014 followed by ordinary notes",
            ("fixture-token-014",),
            ("followed by ordinary notes",),
        ),
    ],
)
def test_canonical_redaction_quoted_punctuation_and_multiparam_auth(text, leaks, must_keep):
    plat = platform_logging.Redactor(env={})
    for out in (redaction.redact_text(text), plat.redact(text)):
        for secret in leaks:
            assert secret not in out, (text, out)
        for keep in must_keep:
            assert keep in out, (text, out)
        assert redaction.REDACTED in out
        assert redaction.redact_text(out) == out
        assert plat.redact(out) == out


def test_canonical_redaction_multiparam_auth_and_quoted_split_are_linear():
    plat = platform_logging.Redactor(env={})
    payloads = [
        "Authorization: Digest " + "k=v," * 5_000,
        'Authorization: "' + "a" * 30_000,
        'password: "' + "a" * 30_000,
        "password: [REDACTED] " + "x=y," * 5_000,
    ]
    for payload in payloads:
        started = time.perf_counter()
        redaction.redact_text(payload)
        plat.redact(payload)
        assert time.perf_counter() - started < 1.0


# ---------------------------------------------------------------------------
# 2. Secret persistence in previews, work_narration, and http_work_runs.db
# ---------------------------------------------------------------------------


def test_preview_tool_arguments_redacts_secrets_and_preserves_bidi_and_sentinels():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAsecretpembody123456\n"
        "-----END RSA PRIVATE KEY-----"
    )
    sentinel = object()
    preview = chat_tools.preview_tool_arguments(
        "run_code",
        {
            "code": 'print("\u202ehi")\npassword = "correct horse=staple"\nkey = "sk-proj-FAKE1234567890abcd"\n' + pem,
            "approval": sentinel,
            "token": "top-secret-tok",
            "extra_roots": "C:\\secret\\root",
            "dbPassword": "raw-db-password",
        },
    )
    assert preview["bidi_warning"] is True
    assert "U+202E" in preview["bidi_chars"]
    assert "approval" not in preview["arguments"]
    assert "token" not in preview["arguments"]
    assert "extra_roots" not in preview["arguments"]
    assert preview["arguments"]["dbPassword"] == redaction.REDACTED
    blob = json.dumps(preview)
    for leaked in ("horse", "staple", "sk-proj-FAKE", "secretpembody", "top-secret-tok", "raw-db-password"):
        assert leaked not in blob, blob

    write_preview = chat_tools.preview_tool_arguments(
        "file_write",
        {
            "path": "config.json",
            "content": "postgres://admin:supersecretpw@db.internal/app",
            "cookie": "sid=sessionsecret99",
            "private_key": "raw-private-key-material",
        },
    )
    write_blob = json.dumps(write_preview)
    for leaked in ("supersecretpw", "sessionsecret99", "raw-private-key-material"):
        assert leaked not in write_blob, write_blob


def test_work_narration_text_scrubs_canonical_shapes():
    pem = (
        "-----BEGIN EC PRIVATE KEY-----\n"
        "MHcCAQEEISECRETKEYBODY987654321\n"
        "-----END EC PRIVATE KEY-----"
    )
    samples = [
        ("key sk-proj-AbCdEfGhIjKlMnOpQrSt", "AbCdEfGhIjKl"),
        ("aws AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("gh ghp_16C7e42F292c6912E7710c838347Ae178B4a", "16C7e42F292c"),
        ("gl glpat-abcdef1234567890", "abcdef1234567890"),
        ("hf hf_abcdef1234567890", "abcdef1234567890"),
        ("db postgres://admin:dbpass99@db.internal:5432/prod", "dbpass99"),
        ("Cookie: sid=mysecretcookievalue", "mysecretcookievalue"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        (pem, "SECRETKEYBODY"),
        ('password: "correct horse=staple"', "staple"),
    ]
    for raw, secret in samples:
        out = work_narration._text(raw, 400)
        assert secret not in out, (raw, out)
        assert "<redacted>" in out


def test_http_work_runs_redacts_output_and_narration_before_sqlite_write(monkeypatch, tmp_path):
    db_file = tmp_path / "runs.db"
    monkeypatch.setenv("SONDER_HTTP_WORK_RUNS_DB", str(db_file))
    http_work_runs.reset_for_tests()
    run_id = "wr-" + ("ab" * 16)
    scope = "wo-test-scope"
    http_work_runs.start(run_id, owner_scope=scope, process_id="p1", deadline_ts=time.time() + 600)

    http_work_runs.set_narration(
        run_id,
        acknowledgement='Plan using password: "correct horse=staple" and glpat-secret12345678',
        pending_refusal={
            "call_id": "call-1",
            "tool": "run_code",
            "reason": "Blocked with Cookie: sid=cookie_leak_42",
            "detail": "postgres://alice:pgsecret99@db.internal/app and AKIAIOSFODNN7EXAMPLE",
            "remedies": [{"hint": "never expose sk-proj-AbCdEfGhIjKlMnOpQrSt", "api_key": "raw-api-key"}],
        },
        result_receipt={"status": "returned", "routing_reason": "token=receiptsecret77"},
    )
    http_work_runs.finish(
        run_id,
        "failed",
        output="Failed: Authorization: AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/2026/us/s3/aws4_request, Signature=deadbeefcafebabe99\n"
        "-----BEGIN PRIVATE KEY-----\nMIISECRETBODY\n-----END PRIVATE KEY-----",
    )

    row = http_work_runs.get(run_id, owner_scope=scope)
    assert row is not None
    raw_conn = sqlite3.connect(str(db_file))
    try:
        raw_row = raw_conn.execute(
            "SELECT output, narration FROM http_work_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        raw_conn.close()
    combined = json.dumps(row) + "\n" + raw_row[0] + "\n" + raw_row[1]
    for secret in (
        "horse", "staple", "glpat-secret12345678", "cookie_leak_42",
        "pgsecret99", "AKIAIOSFODNN7EXAMPLE", "AbCdEfGhIjKlMnOpQrSt",
        "raw-api-key", "receiptsecret77", "AKIDEXAMPLE", "deadbeefcafebabe99",
        "MIISECRETBODY",
    ):
        assert secret not in combined, (secret, combined)


# ---------------------------------------------------------------------------
# 3. Cross-owner activity & reasoning isolation + activity_tracker hardening
# ---------------------------------------------------------------------------


def test_run_scoped_and_deferred_start_work_preserve_feed_and_reasoning_owners():
    at.reset_for_tests()
    alice_feed = "lf-alice-owner"
    alice_reason = "ro-alice-owner"
    bob_feed = "lf-bob-owner"
    bob_reason = "ro-bob-owner"

    # Direct explicit owner propagation
    def work_direct():
        at.record_reasoning("alice direct thought", model="m")
        return "ok"

    http_work_narration.run_scoped(
        "wr-1", lambda *_: None, "ack", "", work_direct,
        feed_owner=alice_feed, reasoning_owner=alice_reason,
    )
    assert at.live_feed_for_owner("")["recent"] == []
    assert at.live_feed_for_owner(bob_feed)["recent"] == []
    assert len(at.live_feed_for_owner(alice_feed)["recent"]) == 1
    assert at.reasoning_for_owner("") is None
    assert at.reasoning_for_owner(bob_reason) is None
    assert at.reasoning_for_owner(alice_reason)["text"] == "alice direct thought"

    # Deferred start_work inherits owners from admission-time response_span
    at.reset_for_tests()

    class _Store:
        def __init__(self):
            self.rows = {}
        def reconcile(self, _p): return 0
        def start(self, rid, **kw): self.rows[rid] = {"status": "running", **kw}
        def finish(self, rid, status, _out): self.rows[rid]["status"] = status
        def cancel_requested(self, _rid): return False
        def set_narration(self, rid, **kw): self.rows[rid].update(kw)
        def link_run(self, *_a): pass

    class _Effects:
        class Fence:
            def __init__(self, _n, _c): pass
        def held(self, _f):
            import contextlib
            return contextlib.nullcontext()
        def reason_lost(self, _f): return ""

    class _InlineThread:
        def __init__(self, *, target, args, **_kw):
            self._target, self._args = target, args
        def start(self):
            self._target(*self._args)

    store = _Store()
    runner = WorkRunner(store=store, effects=_Effects(), thread_factory=_InlineThread)

    def deferred_body():
        return http_work_narration.run_scoped(
            current_run_id(), store.link_run, "plan", "",
            lambda: (at.record_reasoning("alice deferred thought", model="m"), "done")[1],
        )

    with http_work_narration.response_scope() as deferred:
        with at.response_span(
            "http-chat", "hello", surface="http",
            feed_owner=alice_feed, reasoning_owner=alice_reason,
        ):
            http_work_narration.start_work(
                runner, "alice-principal", deferred_body, "Plan.", store,
                classify=lambda r: ("returned", str(r)),
            )
        # Outer HTTP span is now closed before deferred worker executes!
        deferred.flush()

    assert at.live_feed_for_owner("")["recent"] == []
    assert at.live_feed_for_owner(bob_feed)["recent"] == []
    surfaces = [entry["surface"] for entry in at.live_feed_for_owner(alice_feed)["recent"]]
    assert "http-work" in surfaces
    assert at.reasoning_for_owner("") is None
    assert at.reasoning_for_owner(bob_reason) is None
    assert at.reasoning_for_owner(alice_reason)["text"] == "alice deferred thought"
    at.reset_for_tests()


def test_safe_command_survives_deep_brackets_and_masks_compound_flags():
    for hostile in (
        "prog " + "[" * 5000,
        "prog " + "[" * 5000 + "]" * 5000,
        "[" * 5000 + "]" * 5000,
    ):
        started = time.perf_counter()
        out = at._safe_command(hostile)
        assert isinstance(out, str)
        assert time.perf_counter() - started < 1.0

    masked = at._safe_command([
        "cli",
        "--auth-token", "tok-secret-1",
        "--db-password", "db-secret-2",
        "--private-key=pk-secret-3",
        "--session-cookie", "ck-secret-4",
        "/app/auth_token.py", "--verbose",
    ])
    for secret in ("tok-secret-1", "db-secret-2", "pk-secret-3", "ck-secret-4"):
        assert secret not in masked, masked
    assert "/app/auth_token.py" in masked
    assert "--verbose" in masked


def test_activity_tracker_redacts_pem_cookies_provider_prefixes_escaped_quotes_and_reasoning():
    at.reset_for_tests()
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAactivitypemsecret\n"
        "-----END RSA PRIVATE KEY-----"
    )
    cases = [
        (pem, "activitypemsecret"),
        ("Cookie: session=cookie-val-123; theme=dark", "cookie-val-123"),
        ("Set-Cookie: sid=setcookie-val-456; HttpOnly", "setcookie-val-456"),
        ('password="foo\\"bar secret" ok', "bar secret"),
        ("glpat-1234567890abcdef", "1234567890abcdef"),
        ("hf_1234567890abcdef", "1234567890abcdef"),
        ("npm_1234567890abcdef", "1234567890abcdef"),
        ("pypi-1234567890abcdef1234", "1234567890abcdef"),
        ("ya29.1234567890abcdef", "1234567890abcdef"),
        ("rk_live_1234567890abcdef", "1234567890abcdef"),
        ("GOCSPX-1234567890abcdefghij", "1234567890abcdefghij"),
        ("xoxs-1234567890-abcdef", "1234567890-abcdef"),
    ]
    for raw, secret in cases:
        out = at._redact_text(raw)
        assert secret not in out, (raw, out)

    safe_args = at._safe_args({"cookie": "sid=abc", "private_key": "raw-pk", "note": "ok"})
    assert safe_args["cookie"] == "<redacted>"
    assert safe_args["private_key"] == "<redacted>"
    assert safe_args["note"] == "ok"

    # Reasoning redaction scrubs credentials without mangling ordinary prose
    with at.response_span("chat", "hi", reasoning_owner="ro-a"):
        at.record_reasoning(
            "User asked about token buckets and password resets. "
            "Leaked password=hunter2 and sk-proj-AbCdEfGhIjKlMnOpQrSt and " + pem,
            model="m",
        )
        rec = at.current_reasoning()
    assert "token buckets and password resets" in rec["text"]
    for secret in ("hunter2", "AbCdEfGhIjKlMnOpQrSt", "activitypemsecret"):
        assert secret not in rec["text"]
    at.reset_for_tests()


def test_activity_tracker_eviction_cleanup_and_per_owner_reasoning_isolation():
    at.reset_for_tests()
    # Eviction cleans up _FEED_OWNERS, _REASONING_OWNERS, _REASONING
    with at._LOCK:
        for idx in range(at.MAX_ACTIVE + 5):
            rid = f"r-evict-{idx:02d}"
            at._ACTIVE[rid] = {"id": rid, "started_at": float(idx)}
            at._FEED_OWNERS[rid] = f"lf-{idx}"
            at._REASONING_OWNERS[rid] = f"ro-{idx}"
            at._REASONING[rid] = [{"model": "m", "text": f"t-{idx}"}]
    with at.response_span("trigger", "hi", feed_owner="lf-new", reasoning_owner="ro-new"):
        with at._LOCK:
            assert "r-evict-00" not in at._ACTIVE
            assert "r-evict-00" not in at._FEED_OWNERS
            assert "r-evict-00" not in at._REASONING_OWNERS
            assert "r-evict-00" not in at._REASONING

    # Owner B finishing does not clobber Owner A's latest reasoning
    at.reset_for_tests()
    with at.response_span("turn-a", "hi", reasoning_owner="ro-alice"):
        at.record_reasoning("alice private thought", model="m")
    with at.response_span("turn-b", "hi", reasoning_owner="ro-bob"):
        at.record_reasoning("bob private thought", model="m")
    assert at.reasoning_for_owner("ro-alice")["text"] == "alice private thought"
    assert at.reasoning_for_owner("ro-bob")["text"] == "bob private thought"
    assert at.reasoning_for_owner("ro-carol") is None
    at.reset_for_tests()


# ---------------------------------------------------------------------------
# 4. Session export & SQLite WAL / handle hygiene / preference scope
# ---------------------------------------------------------------------------


def test_query_export_redacts_sensitive_keys_and_bounds_deep_payloads(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    repo.append(
        "s-sec",
        "fact.recorded",
        {"password": "hunter2", "api_key": "short", "cookie": "sid=1", "note": "safe"},
    )
    deep: object = "sk-proj-AbCdEfGhIjKlMnOpQrSt"
    for _ in range(redaction.MAX_WALK_DEPTH + 10):
        deep = [deep]
    repo.append("s-sec", "deep.event", {"nested": deep})

    engine = SessionQueryEngine(repo)
    exported = engine.export_events("s-sec", max_events=10)
    first = exported.events[0]
    assert first.redacted is True
    assert first.payload["password"] == redaction.REDACTED
    assert first.payload["api_key"] == redaction.REDACTED
    assert first.payload["cookie"] == redaction.REDACTED
    assert first.payload["note"] == "safe"

    second = exported.events[1]
    assert second.redacted is True
    assert "AbCdEfGhIjKlMnOpQrSt" not in json.dumps(second.to_dict())
    assert redaction.REDACTED in json.dumps(second.to_dict())


def test_session_repository_enables_wal_mode(tmp_path):
    db_path = tmp_path / "sessions.db"
    repo = SQLiteSessionRepository(db_path)
    try:
        with repo._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert int(busy) == 5000
    finally:
        assert repo.close(timeout=5)


def test_memory_store_preference_scope_enforced_and_connect_closes_on_init_failure(monkeypatch, tmp_path):
    conn = memory_store.connect(":memory:")
    try:
        memory_store.upsert_preference(conn, "pref-b-id", "project:B", "style", "Project B pref")
        # Another scope cannot toggle project:B's preference by id
        assert memory_store.set_preference_enabled(conn, "pref-b-id", False, scope="project:A") == 0
        assert memory_store.set_preference_enabled(conn, "pref-b-id", False, scope="global") == 0
        rows = memory_store.all_preferences(conn, include_disabled=True)
        assert [r["enabled"] for r in rows if r["id"] == "pref-b-id"] == [1]
        # Matching scope succeeds
        assert memory_store.set_preference_enabled(conn, "pref-b-id", False, scope="project:B") == 1
        rows = memory_store.all_preferences(conn, include_disabled=True)
        assert [r["enabled"] for r in rows if r["id"] == "pref-b-id"] == [0]
    finally:
        conn.close()

    # Post-connect init failure must close the opened SQLite handle
    closed: list[bool] = []
    real_connect = memory_store.owned_sqlite_connect

    class _TrackingConn:
        def __init__(self, inner):
            self._inner = inner
        def __getattr__(self, name):
            return getattr(self._inner, name)
        def __setattr__(self, name, value):
            if name == "_inner":
                object.__setattr__(self, name, value)
            else:
                setattr(self._inner, name, value)
        def close(self):
            closed.append(True)
            return self._inner.close()

    monkeypatch.setattr(
        memory_store, "owned_sqlite_connect",
        lambda *a, **kw: _TrackingConn(real_connect(*a, **kw)),
    )
    monkeypatch.setattr(
        memory_store, "init_db",
        lambda _c: (_ for _ in ()).throw(sqlite3.OperationalError("simulated lock")),
    )
    with pytest.raises(sqlite3.OperationalError, match="simulated lock"):
        memory_store.connect(str(tmp_path / "fail.db"))
    assert closed == [True]
