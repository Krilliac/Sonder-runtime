"""Regression tests for open LEDGER.md backlog fixes."""
from __future__ import annotations

import hashlib
import io
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

import pytest

import command_router as cr
import sonder_paths
import web_tools
from sonder_runtime.adapters import artifact_fetch, memory_store
from sonder_runtime.adapters.desktop.session import SessionController
from sonder_runtime.adapters.persistence import autopilot_store
from sonder_runtime.adapters.persistence.session_checkpoint_privacy import (
    build_session_checkpoint_privacy_adapter,
)
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.adapters.playbook_store import PlaybookPolicy, PlaybookStore
from sonder_runtime.adapters.process_liveness import PROCESS_ALIVE
from sonder_runtime.adapters.resource_leases import SqliteResourceLeaseRegistry
from sonder_runtime.adapters.updates import service as updates_service
from sonder_runtime.adapters.updates.engine import UpdateManager, confirm_nonce_for
from sonder_runtime.application.compute_fabric.jobs import (
    ArgumentPolicy,
    ComputeJobWorker,
    DigestBoundInput,
    JobCatalogEntry,
    RemoteJobEnvelope,
)
from sonder_runtime.application.evaluation.promotion_gates import (
    DEFAULT_PROMOTION_GATE_POLICIES,
    PromotionGateEvaluator,
    PromotionKind,
    evaluate_promotion_gate,
)
from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationDimension,
    EvaluationLifecycleError,
    EvaluationMode,
    EvaluationResult,
    EvaluationSuite,
    ProposalLifecycle,
    ShadowCanaryObservation,
)
from sonder_runtime.application.execution.process_jobs import (
    ProcessJobStart,
    ProcessJobWait,
)
from sonder_runtime.application.ports.jobs import (
    JobIdentity,
    JobRecord,
    JobStatus,
)
from sonder_runtime.application.protocol.editor_interop import (
    EditorInteropError,
    ProtocolEnvelope,
)
from sonder_runtime.application.protocol.resumable_streams import ResumableStream
from sonder_runtime.application.session.checkpoints import checkpoint_projection
from sonder_runtime.application.session.query_export import SessionQueryEngine
from sonder_runtime.domain import agent_mutation_policy
from sonder_runtime.domain.common.events import DomainEvent
from sonder_runtime.domain.compute_fabric import WorkloadKind
from sonder_runtime.domain.resource_leases import KIND_DESKTOP_SESSION
from sonder_runtime.platform import paths as runtime_paths
from sonder_runtime.platform.logging import Redactor


# ---------------------------------------------------------------------------
# A1, A3, A14 (updates/service.py) & A11 (updates/engine.py)
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self._buf = io.BytesIO(data)
        self.status = status
        self.headers: dict[str, str] = {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_a1_resumable_download_cleans_bad_partial_for_clean_retry(tmp_path: Path) -> None:
    payload = b"valid-update-payload-12345"
    digest = hashlib.sha256(payload).hexdigest()
    dest = tmp_path / "bundle.tar.gz"
    partial = tmp_path / "bundle.tar.gz.partial"
    sidecar = tmp_path / "bundle.tar.gz.partial.validators.json"

    seen_ranges: list[str | None] = []

    def opener_short_then_resume(_url: str, headers: dict[str, str]) -> _FakeStreamResponse:
        seen_ranges.append(headers.get("Range"))
        if len(seen_ranges) == 1:
            return _FakeStreamResponse(payload[:5])
        return _FakeStreamResponse(payload[5:], status=206)

    with pytest.raises(updates_service.UpdateError, match="downloaded length"):
        updates_service.resumable_download(
            "http://mirror/bundle.tar.gz",
            dest,
            validators={"etag": "v1"},
            expected_length=len(payload),
            expected_sha256=digest,
            opener=opener_short_then_resume,
        )
    # A short prefix is kept so the validator-guarded resume can continue.
    assert partial.read_bytes() == payload[:5]
    assert sidecar.exists()
    result = updates_service.resumable_download(
        "http://mirror/bundle.tar.gz",
        dest,
        validators={"etag": "v1"},
        expected_length=len(payload),
        expected_sha256=digest,
        opener=opener_short_then_resume,
    )
    assert seen_ranges == [None, "bytes=5-"]
    assert dest.read_bytes() == payload
    assert result["resumed"] is True

    dest2 = tmp_path / "bundle2.tar.gz"
    partial2 = tmp_path / "bundle2.tar.gz.partial"
    sidecar2 = tmp_path / "bundle2.tar.gz.partial.validators.json"
    bad = b"X" * len(payload)
    calls2 = 0

    def opener_corrupt_then_ok(_url: str, headers: dict[str, str]) -> _FakeStreamResponse:
        nonlocal calls2
        calls2 += 1
        assert "Range" not in headers
        return _FakeStreamResponse(bad if calls2 == 1 else payload)

    with pytest.raises(updates_service.TrustError, match="hash mismatch"):
        updates_service.resumable_download(
            "http://mirror/bundle2.tar.gz",
            dest2,
            validators={"etag": "v1"},
            expected_length=len(payload),
            expected_sha256=digest,
            opener=opener_corrupt_then_ok,
        )
    assert not partial2.exists()
    assert not sidecar2.exists()
    updates_service.resumable_download(
        "http://mirror/bundle2.tar.gz",
        dest2,
        validators={"etag": "v1"},
        expected_length=len(payload),
        expected_sha256=digest,
        opener=opener_corrupt_then_ok,
    )
    assert dest2.read_bytes() == payload


def test_a3_check_compatibility_python_version_no_prefix_collision(tmp_path: Path) -> None:
    def _manifest(py_ver: str) -> updates_service.BundleManifest:
        return updates_service.BundleManifest.from_dict({
            "schema_version": 1,
            "bundle_id": "b",
            "version": "1.0.0",
            "commit_sha": "c",
            "channel": "stable",
            "platform": updates_service.current_platform(),
            "architecture": updates_service.current_architecture(),
            "package_format": "tar.gz",
            "runtime": {"python_version": py_ver},
            "state_schema": {},
            "resources": {},
            "entrypoints": {},
            "health_checks": [],
            "rollback": {},
            "files": [{"path": "a.py", "length": 1, "sha256": "0" * 64}],
        })

    minor = sys.version_info.minor
    wrong_minor = 99 if minor != 99 else 98
    prefix_trap = f"{sys.version_info.major}.{str(minor)[0]}"
    if prefix_trap == f"{sys.version_info.major}.{minor}":
        prefix_trap = f"{sys.version_info.major}.{wrong_minor}"

    for bad_ver in (prefix_trap, f"{prefix_trap}.0", f"{sys.version_info.major}.{wrong_minor}", "3"):
        problems = updates_service.check_compatibility(_manifest(bad_ver), releases_dir=tmp_path)
        assert any("bundle needs Python" in p for p in problems), bad_ver

    for ok_ver in (
        f"{sys.version_info.major}.{minor}",
        f"{sys.version_info.major}.{minor}.2",
    ):
        problems = updates_service.check_compatibility(_manifest(ok_ver), releases_dir=tmp_path)
        assert not any("bundle needs Python" in p for p in problems), ok_ver


def test_a14_commit_activation_preserves_begin_error(monkeypatch, tmp_path: Path) -> None:
    repo = updates_service.UpdateRepository(tmp_path / "updates.db")

    class _FakeConn:
        in_transaction = False

        def execute(self, sql: str, *_args: object):
            if sql == "BEGIN IMMEDIATE":
                raise sqlite3.OperationalError("database is locked")
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("cannot rollback - no transaction is active")
            raise AssertionError(f"unexpected sql: {sql}")

        def close(self) -> None:
            return None

    monkeypatch.setattr(repo, "_connect", lambda: _FakeConn())
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        repo.commit_activation(expected_active_id=None, target_release_id="rel_1", new_release={})


def test_a11_update_engine_rolls_back_on_migrate_timeout(monkeypatch, tmp_path: Path) -> None:
    runtime_paths.reset_home()
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_UPDATES_DB", str(home / "updates.db"))
    monkeypatch.setenv("SONDER_UPDATE_ALLOW_UNSIGNED", "1")

    src = tmp_path / "src"
    pkg = src / "sonder_runtime"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text("print('ok')\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    updates_service.build_bundle(src, bundle, version="2.1.0")

    manager = UpdateManager(
        repository=updates_service.UpdateRepository(),
        releases_dir=tmp_path / "releases",
        current_link=tmp_path / "current",
        backup_target=str(tmp_path / "backups"),
    )
    plan = manager.import_offline(bundle, allow_unverified=True)

    def _timeout(*_args: object, **_kwargs: object):
        raise subprocess.TimeoutExpired(["python", "-m", "sonder_runtime", "migrate"], 1.0)

    monkeypatch.setattr(manager, "_run_in_release", _timeout)
    done = manager.install(
        plan["update_id"],
        confirm=confirm_nonce_for(plan),
        allow_unverified=True,
        skip_backup=True,
    )
    assert done["status"] == "rolled_back"
    assert done["error_code"] == "MIGRATION_FAILED"
    steps = {s["step_name"]: s["status"] for s in manager.repository.steps(plan["update_id"])}
    assert steps["migrate"] == "failed"


# ---------------------------------------------------------------------------
# A2 (web_tools.py), A8 (artifact_fetch.py), A13 (desktop/session.py)
# ---------------------------------------------------------------------------


def test_a2_web_tools_request_raises_http_error_on_4xx_5xx(monkeypatch) -> None:
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (web_tools.socket.AF_INET, web_tools.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )

    class _StatusResp:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self.code = status
            self.reason = "Error"
            self.headers = {"Content-Type": "text/plain"}
            self._body = body

        def read(self, n: int = -1) -> bytes:
            return self._body if n == -1 else self._body[:n]

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    for code in (404, 500):
        monkeypatch.setattr(web_tools, "_urlopen", lambda _req, timeout=10, c=code: _StatusResp(c, b"not ok"))
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            web_tools._request("https://example.com/missing")
        assert exc_info.value.code == code


def test_a8_artifact_fetch_rejects_truncated_content_length(monkeypatch, tmp_path: Path) -> None:
    payload = b"PK\x03\x04" + b"a" * 512
    declared = len(payload) + 4096

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _serve_once() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(4096)
            header = (
                f"HTTP/1.0 200 OK\r\n"
                f"Content-Type: application/octet-stream\r\n"
                f"Content-Length: {declared}\r\n\r\n"
            ).encode("ascii")
            conn.sendall(header + payload)

    thread = threading.Thread(target=_serve_once, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
        monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
        monkeypatch.setattr(
            artifact_fetch,
            "_validate_target",
            lambda url: (urllib.parse.urlparse(url), ("127.0.0.1",)),
        )
        dest = tmp_path / "pkg.zip"
        result = artifact_fetch.fetch_artifact(f"http://127.0.0.1:{port}/pkg.zip", str(dest))
        assert not result["ok"]
        assert result["verdict"] == "rejected"
        assert any(row["check"] == "content_length" and not row["ok"] for row in result["checks"])
        assert not dest.exists()
    finally:
        listener.close()
        thread.join(timeout=5)


def test_a13_desktop_session_start_cleans_up_on_non_refusal_failure(tmp_path: Path) -> None:
    from tests.test_computer_use_session import FakeDesktop, _launcher

    class _FailingTicksDesktop(FakeDesktop):
        def idle_ticks(self):
            raise RuntimeError("idle tick probe failed")

    helpers: list[object] = []
    db = tmp_path / "leases.sqlite3"
    reg = SqliteResourceLeaseRegistry(db, probe=lambda _pid, _ident=None: (PROCESS_ALIVE, None))
    ctl = SessionController(
        tmp_path / "session",
        desktop=_FailingTicksDesktop(),
        launcher=_launcher(helpers),
        leases=reg,
    )
    with pytest.raises(RuntimeError, match="idle tick probe failed"):
        ctl.start(7, allowed_apps=("notepad.exe",), ttl_seconds=60, per_minute=10, per_session=10)
    assert len(helpers) == 1
    assert helpers[0].terminated is True
    assert reg.holder(KIND_DESKTOP_SESSION, "desktop") is None


# ---------------------------------------------------------------------------
# P3, P4, P5, P6, P7, P8
# ---------------------------------------------------------------------------


def test_p3_checkpoint_privacy_loads_newest_and_scans_all_pages(tmp_path: Path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=3)
    adapter = build_session_checkpoint_privacy_adapter(repo, max_scan=10)
    latest_cp = None
    for idx in range(1, 8):
        cp = checkpoint_projection(
            [DomainEvent("session.started", "session", "s1", idx, {})],
            source_hash=f"hash-{idx}",
        )
        adapter.save_checkpoint(cp)
        latest_cp = cp
    assert adapter.load_checkpoint("s1") == latest_cp

    old = "2020-01-01T00:00:00Z"
    repo2 = SQLiteSessionRepository(tmp_path / "ret.db", max_read_limit=3)
    for idx in range(7):
        repo2.append("s2", "message.received", {"privacy_class": "secret", "i": idx}, occurred_at_utc=old)
    adapter2 = build_session_checkpoint_privacy_adapter(repo2, max_scan=10)
    candidates = adapter2.retention_candidates("s2", now_utc=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert [c.sequence for c in candidates] == [1, 2, 3, 4, 5, 6, 7]


def test_p4_session_repository_search_escapes_like_and_ignores_keys(tmp_path: Path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    repo.append("s1", "a", {"text": "100% real"})
    repo.append("s1", "b", {"text": "100x fake"})
    repo.append("s1", "c", {"secret_key_name": "benign"})
    repo.append("s1", "d", {"quote": 'say "hi"'})

    assert [e.event_type for e in repo.search(session_id="s1", text="100%")] == ["a"]
    assert repo.search(session_id="s1", text="secret_key_name") == ()
    assert [e.event_type for e in repo.search(session_id="s1", text='say "hi"')] == ["d"]


def test_p5_query_export_redacts_before_text_filter(tmp_path: Path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    repo.append("s1", "user.message", {"content": "hello"})
    repo.append("s1", "model.response", {"content": "token=super-secret"})
    repo.append("s1", "session.retention.applied", {
        "targets": [{"sequence": 1, "privacy_class": "secret"}],
    })
    engine = SessionQueryEngine(repo, redactor=Redactor(secret_values=("super-secret",)))
    assert engine.query_events("s1", text="super-secret").records == ()
    assert engine.query_events("s1", text="hello").records == ()


def test_p6_autopilot_store_handles_concurrent_duplicate_column(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "autopilot.db"
    real_connect = autopilot_store.owned_sqlite_connect

    class _RaceConn:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn
            self._raced = False

        def execute(self, sql: str, *args: object):
            if sql.startswith("ALTER TABLE autopilot_runs ADD COLUMN") and not self._raced:
                self._raced = True
                self._conn.execute(sql, *args)
                raise sqlite3.OperationalError("duplicate column name: infra_retries")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name: str):
            return getattr(self._conn, name)

    monkeypatch.setattr(
        autopilot_store,
        "owned_sqlite_connect",
        lambda path, **kwargs: _RaceConn(real_connect(path, **kwargs)),
    )
    autopilot_store._ensure_schema(str(db_path))


def test_p7_memory_store_escapes_like_and_rejects_ambiguous_prefix() -> None:
    conn = memory_store.connect(":memory:")
    memory_store.touch_session(conn, "s_pct")
    memory_store.set_session_title(conn, "s_pct", "100% done")
    memory_store.touch_session(conn, "s_other")
    memory_store.set_session_title(conn, "s_other", "100x other")
    assert memory_store.find_session(conn, "100%") == "s_pct"

    memory_store.touch_session(conn, "s_dup1")
    memory_store.set_session_title(conn, "s_dup1", "Refactor A")
    memory_store.touch_session(conn, "s_dup2")
    memory_store.set_session_title(conn, "s_dup2", "Refactor B")
    assert memory_store.find_session(conn, "Refactor") is None

    memory_store.create_task(conn, "Task 1", task_id="a_b_1")
    memory_store.create_task(conn, "Task 2", task_id="axb_1")
    assert memory_store.resolve_task_id(conn, "a_b") == "a_b_1"


def test_p8_playbook_store_edit_allows_backslash_titles(tmp_path: Path) -> None:
    store = PlaybookStore(tmp_path / "playbooks", policy=PlaybookPolicy())
    created = store.note(
        "debugging",
        "procedure",
        "Original title for debugging",
        "Run the targeted regression suite before committing changes to disk.",
        "Observed in unit test run with clean exit status.",
    )
    raw_title = r"Fix \1 and \g<0> path C:\new"
    edited = store.edit("debugging", created["id"], title=raw_title)
    assert edited["title"] == raw_title


# ---------------------------------------------------------------------------
# C4, C5, C6
# ---------------------------------------------------------------------------


def test_c4_legacy_db_migration_preserves_wal_and_drops_stale_wal(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "state"
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_db = legacy_root / "memory.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("CREATE TABLE items(val TEXT)")
    conn.execute("INSERT INTO items VALUES ('fresh-clean')")
    conn.commit()
    conn.close()

    module_path = legacy_root / "sonder_paths.py"
    module_path.write_text("# legacy\n", encoding="utf-8")
    monkeypatch.setattr(sonder_paths, "__file__", str(module_path))
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.delenv("SONDER_DB", raising=False)

    # Plant a stale target-wal / target-shm in home before migration runs.
    home.mkdir(parents=True, exist_ok=True)
    (home / "memory.db-wal").write_bytes(b"stale-wal-bytes")
    (home / "memory.db-shm").write_bytes(b"stale-shm-bytes")

    migrated_path = sonder_paths.memory_db_path()
    assert migrated_path == str(home / "memory.db")
    assert not (home / "memory.db-wal").exists()
    assert not (home / "memory.db-shm").exists()
    check = sqlite3.connect(migrated_path)
    try:
        assert check.execute("SELECT val FROM items").fetchall() == [("fresh-clean",)]
    finally:
        check.close()


def test_c5_command_router_does_not_hijack_prose() -> None:
    for prose in (
        "append to the list of items",
        "append to my prompt and explain",
        "preferences are broken after restart",
        "my preferences don't save properly",
        "show preferences and explain them",
        "note the error in main.py",
        "remember to run pytest before pushing",
        "remember how we fixed the crash?",
        "master branch has merge conflicts",
        "master the fundamentals of rust",
        "use the project structure below to fix the bug",
        "set the project up for CI",
    ):
        assert cr.resolve(prose) is None, prose

    assert cr.resolve("append to file notes.md") == "/append notes.md"
    assert cr.resolve("append to notes.md") == "/append notes.md"
    assert cr.resolve("preferences verbose") == "/prefer verbose"
    assert cr.resolve("note that tests pass") == "/fact tests pass"
    assert cr.resolve("master build everything") == "/master build everything"
    assert cr.resolve("switch project to duetos") == "/project duetos"


def test_c6_agent_mutation_policy_matches_truthy_harness() -> None:
    assert agent_mutation_policy.invocation_mutates("lint_run", {"fix": "true"}) is True
    assert agent_mutation_policy.invocation_mutates("lint_run", {"fix": 1}) is True
    assert agent_mutation_policy.invocation_mutates("rename_symbol", {"dry_run": 0}) is True
    assert agent_mutation_policy.invocation_mutates("rename_symbol", {"dry_run": ""}) is True
    assert agent_mutation_policy.invocation_mutates("apply_patch", {"check_only": 1}) is False
    assert agent_mutation_policy.invocation_mutates("format_code", {"check_only": 1}) is False


# ---------------------------------------------------------------------------
# X11, X12, X15, X19, X20, X21
# ---------------------------------------------------------------------------


def test_x11_promotion_gates_enforce_offline_passed_and_rates() -> None:
    suite = EvaluationSuite("s", "v1", (EvaluationDimension("split", "holdout"),), ("error_rate", "pass_rate", "timeout_rate"))
    shadow = ShadowCanaryObservation(EvaluationMode.SHADOW, "s1", True, 20, {"error": 0}, 0)
    canary = ShadowCanaryObservation(EvaluationMode.CANARY, "c1", True, 20, {"error": 0}, 0.05)
    policy = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.PROMPT]

    failed_offline = EvaluationResult(
        "r1", suite, "cand", "base", EvaluationMode.OFFLINE, suite.dimensions,
        {"error_rate": 0.0, "pass_rate": 1.0, "timeout_rate": 0.0},
        False, 40, replay_equivalent=True, provenance=("test",),
    )
    dec1 = evaluate_promotion_gate(policy, results=[failed_offline], baseline_pass_rate=1.0, shadow=shadow, canary=canary)
    assert not dec1.passed
    assert "gate_failed:offline_passed" in dec1.reason_codes

    timed_out = EvaluationResult(
        "r2", suite, "cand", "base", EvaluationMode.OFFLINE, suite.dimensions,
        {"error_rate": 0.05, "pass_rate": 1.0, "timeout_rate": 0.05},
        True, 40, replay_equivalent=True, provenance=("test",),
    )
    dec2 = evaluate_promotion_gate(policy, results=[timed_out], baseline_pass_rate=1.0, shadow=shadow, canary=canary)
    assert not dec2.passed
    assert "gate_failed:error_rate" in dec2.reason_codes
    assert "gate_failed:timeout_rate" in dec2.reason_codes


def test_x12_proposal_lifecycle_bounds_holdout_and_baseline_regressions() -> None:
    suite = EvaluationSuite(
        "s", "v1",
        (EvaluationDimension("split", "holdout"),),
        ("baseline_pass_rate", "case_regressions", "pass_rate"),
    )
    lifecycle = ProposalLifecycle(promotion_gate=PromotionGateEvaluator())
    lifecycle.create("p1", "cand", "base", suite, promotion_kind="prompt")
    lifecycle.submit("p1")
    lifecycle.begin_evaluation("p1")
    lifecycle.record_result(
        "p1",
        EvaluationResult(
            "r1", suite, "cand", "base", EvaluationMode.OFFLINE, suite.dimensions,
            {"baseline_pass_rate": 0.98, "case_regressions": 2.0, "pass_rate": 1.0},
            False, 40, replay_equivalent=True, provenance=("test",),
        ),
    )
    lifecycle.begin_shadow("p1")
    lifecycle.record_observation("p1", ShadowCanaryObservation(EvaluationMode.SHADOW, "s1", True, 20, {"error": 0}, 0))
    lifecycle.begin_canary("p1")
    lifecycle.record_observation("p1", ShadowCanaryObservation(EvaluationMode.CANARY, "c1", True, 20, {"error": 0}, 0.05))

    with pytest.raises(EvaluationLifecycleError, match="baseline_pass_rate"):
        lifecycle.build_gated_promotion_evidence(
            "p1", baseline_pass_rate=0.5, case_regressions=2, holdout_passed=True,
            rollback_reference="base", provenance=("ci",),
        )
    with pytest.raises(EvaluationLifecycleError, match="case_regressions"):
        lifecycle.build_gated_promotion_evidence(
            "p1", baseline_pass_rate=0.98, case_regressions=0, holdout_passed=True,
            rollback_reference="base", provenance=("ci",),
        )
    evidence, decision = lifecycle.build_gated_promotion_evidence(
        "p1", baseline_pass_rate=0.98, case_regressions=2, holdout_passed=True,
        rollback_reference="base", provenance=("ci",),
    )
    assert not decision.passed
    assert evidence.holdout_passed is False
    assert evidence.accepted is False


def test_x15_x19_resumable_streams_prune_and_reject_regression() -> None:
    stream = ResumableStream("s", capacity=2)
    stream.publish("e", {"i": 1}, event_id="id1")
    stream.publish("e", {"i": 2}, event_id="id2")
    stream.publish_snapshot({"ok": True}, watermark=2)
    assert stream._event_ids == {}
    stream.publish("e", {"i": 3}, event_id="id3")
    with pytest.raises(ValueError, match="regress"):
        stream.publish_snapshot({"old": True}, watermark=1)


def test_x20_compute_fabric_stray_spool_file_does_not_break_staging(
    monkeypatch, tmp_path: Path,
) -> None:
    from sonder_runtime.application.compute_fabric.artifact_spool import (
        PrivateDirectoryAnchor,
    )

    input_base = tmp_path / "inputs"
    artifact_base = tmp_path / "artifacts"
    monkeypatch.setattr(ComputeJobWorker, "_input_stage_base", staticmethod(lambda: input_base))
    monkeypatch.setattr(ComputeJobWorker, "_artifact_stage_base", staticmethod(lambda: artifact_base))

    work = tmp_path / "work"
    work.mkdir()
    (work / "in.txt").write_bytes(b"hi")
    (work / "out.txt").write_bytes(b"ok")

    class _DoneProvider:
        def start(self, request):
            return ProcessJobStart(
                JobRecord(request.identity, status=JobStatus.RUNNING),
                process_id=123,
                process_group_id=123,
            )

        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(JobIdentity(job_id, "compute-test", "ctrl", "idem-1"), status=JobStatus.SUCCEEDED),
                exit_code=0,
            )

    entry = JobCatalogEntry(
        entry_id="job",
        workload=WorkloadKind.TEST,
        program=sys.executable,
        fixed_args=("-m", "pytest"),
        argument_policy=ArgumentPolicy.RELATIVE_PATHS_AND_TEST_SELECTORS,
        workspace_mappings=frozenset({"ws"}),
        memory_limit_bytes=512 * 1024 * 1024,
        artifact_paths=("out.txt",),
    )
    worker = ComputeJobWorker(
        worker_id="w1",
        catalog={"job": entry},
        workspace_mappings={"ws": work},
        provider=_DoneProvider(),
    )
    with PrivateDirectoryAnchor.open_base(input_base):
        (input_base / "stray.tmp").write_bytes(b"stray-in")
    (worker._artifact_root.path / "stray.tmp").write_bytes(b"stray-art")

    digest = hashlib.sha256(b"hi").hexdigest()
    env = RemoteJobEnvelope.create(
        controller_job_id="ctrl",
        idempotency_key="idem-1",
        workload=WorkloadKind.TEST,
        catalog_entry_id="job",
        workspace_mapping="ws",
        arguments=("in.txt",),
        input_artifacts=(DigestBoundInput("in.txt", 2, digest),),
    )
    receipt = worker.submit(env)
    done = worker.status(receipt.remote_job_id)
    assert done.state == "succeeded"
    assert [a.name for a in done.artifacts] == ["out.txt"]


def test_x21_editor_interop_catches_deep_nesting_recursion_error() -> None:
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(10_000):
        nxt: dict[str, object] = {}
        cursor["child"] = nxt
        cursor = nxt
    with pytest.raises(EditorInteropError, match="JSON-compatible"):
        ProtocolEnvelope.create("ping", nested)
