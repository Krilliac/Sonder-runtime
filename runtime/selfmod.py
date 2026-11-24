"""Host-controlled, auditable self-improvement lifecycle.

Candidate code never calls acceptance or deployment primitives directly. This
module owns state transitions, tamper-evident backups, deterministic checks, locks,
deployment, and rollback. It is stdlib-only so recovery remains available when
the rest of Sonder cannot import.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import sonder_paths
import sonder_logging
from sonder_runtime.adapters.process_liveness import pid_alive as _process_pid_alive
from sonder_runtime.adapters.selfmod import (
    backup_restore as _backup_restore,
    verification as _verification,
    workspace as _workspace,
)
from sonder_runtime.adapters.selfmod.backup_restore import (
    ROLLBACK_RECEIPT_PREFIX,
)
from sonder_runtime.adapters.selfmod.verification import (
    SMOKE_PROBE as _SMOKE_PROBE,
    SMOKE_RECEIPT_PREFIX,
    UNATTENDED_REQUIRED_KINDS,
)
from sonder_runtime.adapters.selfmod.workspace import (
    CandidateIsolationRefused,
    INVENTORY_EXCLUDED as _INVENTORY_EXCLUDED,
    UNISOLATED_FLAG,
)
from sonder_runtime.application.selfmod.candidate_isolation import (
    ISOLATION_KINDS,
)
from sonder_runtime.application.selfmod.stage_refusal import (
    SelfmodStageNotApplied,
)
from sonder_runtime.application.selfmod.governance import (
    SENSITIVE_PARTS,
    SENSITIVE_PREFIXES,
)
from sonder_runtime.application.evaluation import integrity as _integrity


MODES = ("observe", "propose", "auto-low-risk")
PHASES = (
    "observed", "proposed", "backed_up", "editing", "testing", "reviewing",
    "approved", "deployed", "rollback_requested", "rejected", "restored",
    "interrupted", "cancelled",
)
ACTIVE_PHASES = {"backed_up", "editing", "testing", "reviewing", "approved", "rollback_requested"}
TERMINAL_PHASES = {"rejected", "restored", "cancelled"}
_ISOLATED_ATTESTATIONS = ISOLATION_KINDS
DEFAULT_BUDGETS = {
    "max_files_inspected": 80,
    "max_files_changed": 8,
    "max_lines_changed": 600,
    "max_model_calls": 8,
    "max_tool_calls": 40,
    "max_test_seconds": 900,
    "max_retries": 2,
    "max_repairs": 2,
    "max_runtime_seconds": 1800,
}
DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETENTION_GB = 5.0
LEASE_SECONDS = 180

# Preserve re-exported symbols and module-level seams.
UNATTENDED_REQUIRED_KINDS = UNATTENDED_REQUIRED_KINDS
UNISOLATED_FLAG = UNISOLATED_FLAG
CandidateIsolationRefused = CandidateIsolationRefused
SelfmodStageNotApplied = SelfmodStageNotApplied
SMOKE_RECEIPT_PREFIX = SMOKE_RECEIPT_PREFIX
ROLLBACK_RECEIPT_PREFIX = ROLLBACK_RECEIPT_PREFIX
_SMOKE_PROBE = _SMOKE_PROBE
_INVENTORY_EXCLUDED = _INVENTORY_EXCLUDED

_SCHEMA = """
CREATE TABLE IF NOT EXISTS selfmod_settings (
  id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL, enabled INTEGER NOT NULL,
  retention_days INTEGER NOT NULL, retention_bytes INTEGER NOT NULL, updated_ts REAL NOT NULL
);
INSERT OR IGNORE INTO selfmod_settings VALUES (1, 'propose', 1, 30, 5368709120, 0);
CREATE TABLE IF NOT EXISTS selfmod_runs (
  id TEXT PRIMARY KEY, objective TEXT NOT NULL, problem TEXT NOT NULL,
  evidence_json TEXT NOT NULL, files_json TEXT NOT NULL, criteria_json TEXT NOT NULL,
  risk TEXT NOT NULL, expected_benefit TEXT NOT NULL, rollback_plan TEXT NOT NULL,
  repository_root TEXT NOT NULL, phase TEXT NOT NULL, mode TEXT NOT NULL,
  starting_commit TEXT, git_status_start TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
  workspace_path TEXT, branch_name TEXT, backup_manifest TEXT, diff_text TEXT NOT NULL DEFAULT '',
  test_inventory_before TEXT NOT NULL DEFAULT '[]', test_inventory_after TEXT NOT NULL DEFAULT '[]',
  approval_required INTEGER NOT NULL DEFAULT 1, approved_by TEXT, approved_ts REAL,
  auto_evaluation_eligible INTEGER NOT NULL DEFAULT 0,
  maintenance_authorized INTEGER NOT NULL DEFAULT 0, owner_id TEXT, owner_pid INTEGER,
  owner_host TEXT, lease_until REAL, deployed_commit TEXT, last_error TEXT NOT NULL DEFAULT '',
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL, deployed_ts REAL, restored_ts REAL,
  budgets_json TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_selfmod_runs_phase ON selfmod_runs(phase, updated_ts DESC);
CREATE TABLE IF NOT EXISTS selfmod_backups (
  run_id TEXT NOT NULL, path TEXT NOT NULL, existed_before INTEGER NOT NULL,
  sha256_before TEXT, size_before INTEGER NOT NULL, mode_before INTEGER,
  backup_path TEXT, sha256_backup TEXT, PRIMARY KEY(run_id, path)
);
CREATE TABLE IF NOT EXISTS selfmod_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL,
  command_json TEXT NOT NULL, exit_code INTEGER, duration_ms INTEGER NOT NULL,
  output TEXT NOT NULL, passed INTEGER NOT NULL, created_ts REAL NOT NULL,
  isolation TEXT NOT NULL DEFAULT 'unverified'
);
CREATE TABLE IF NOT EXISTS selfmod_deployed_files (
  run_id TEXT NOT NULL, path TEXT NOT NULL, existed_after INTEGER NOT NULL,
  sha256_after TEXT, mode_after INTEGER, recorded_ts REAL NOT NULL,
  PRIMARY KEY(run_id, path)
);
CREATE TABLE IF NOT EXISTS selfmod_tested_files (
  run_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT, diff_sha256 TEXT NOT NULL,
  recorded_ts REAL NOT NULL, PRIMARY KEY(run_id, path)
);
CREATE TABLE IF NOT EXISTS selfmod_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL NOT NULL,
  kind TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selfmod_deployment_lock (
  id INTEGER PRIMARY KEY CHECK(id=1), owner_id TEXT, owner_pid INTEGER,
  owner_host TEXT, lease_until REAL, run_id TEXT
);
INSERT OR IGNORE INTO selfmod_deployment_lock(id) VALUES (1);
CREATE TABLE IF NOT EXISTS selfmod_oracle_receipts (
  run_id TEXT PRIMARY KEY, probe_id INTEGER NOT NULL, passed INTEGER NOT NULL,
  independent INTEGER NOT NULL, receipt_json TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL, created_ts REAL NOT NULL
);
"""


def state_root() -> Path:
    return Path(sonder_paths.state_path("selfmod", "SONDER_SELFMOD_HOME"))


def database_path() -> Path:
    override = os.environ.get("SONDER_SELFMOD_DB", "").strip()
    return Path(override).expanduser() if override else state_root() / "selfmod.db"


def backups_root() -> Path:
    return state_root() / "backups"


def workspaces_root() -> Path:
    return state_root() / "workspaces"


def _connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    for table, column, declaration in (
        ("selfmod_runs", "auto_evaluation_eligible", "INTEGER NOT NULL DEFAULT 0"),
        ("selfmod_tests", "isolation", "TEXT NOT NULL DEFAULT 'unverified'"),
        ("selfmod_runs", "created_by", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return conn


@contextlib.contextmanager
def _tx(immediate=True):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _event(conn, run_id, kind, details):
    conn.execute(
        "INSERT INTO selfmod_events(run_id,ts,kind,details) VALUES(?,?,?,?)",
        (run_id, time.time(), str(kind)[:80], str(details)[:8000]),
    )


def _sha(path: Path) -> str:
    return _backup_restore.sha256_file(path)


def _rel(root: Path, path) -> str:
    return _workspace.rel_path(root, path)


def _protected(path: str) -> bool:
    return _workspace.is_protected(path)


def protected_paths():
    return {"prefixes": list(SENSITIVE_PREFIXES), "contains": list(SENSITIVE_PARTS)}


def is_protected_path(path: str) -> bool:
    """Return whether a repository-relative path is outside ordinary selfmod scope."""
    return _protected(str(path or ""))


def _run(command, cwd, timeout=30):
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(command), cwd=str(cwd), text=True, capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=max(1, int(timeout)), check=False,
            env=sonder_logging.child_environment(),
        )
        output = "\n".join(x.strip() for x in (result.stdout, result.stderr) if x and x.strip())
        return result.returncode, output[:100_000], int((time.monotonic() - started) * 1000)
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(str(x or "") for x in (exc.stdout, exc.stderr)).strip()
        return 124, (output + "\nTIMEOUT")[:100_000], int((time.monotonic() - started) * 1000)


def _git(root: Path, *args, timeout=30):
    command = ["git", *args]
    try:
        result = subprocess.run(
            command,
            cwd=str(root),
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=max(1, int(timeout)),
            check=False,
            env=sonder_logging.child_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(str(x or "") for x in (exc.stdout, exc.stderr)).strip()
        return 124, (output + "\nTIMEOUT")[:100_000]
    if result.returncode:
        output = "\n".join(
            value.strip()
            for value in (result.stdout, result.stderr)
            if value and value.strip()
        )
    else:
        output = (result.stdout or "").strip()
    return result.returncode, output[:100_000]


def _git_info(root: Path):
    if not shutil.which("git"):
        return False, None, ""
    code, inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if code or inside.strip().lower() != "true":
        return False, None, ""
    code, commit = _git(root, "rev-parse", "HEAD")
    commit = commit.strip() if code == 0 else None
    code, status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if code:
        raise RuntimeError("could not inspect Git working tree: %s" % status)
    transient = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/")
    status = "\n".join(
        line for line in status.splitlines()
        if not any(marker in line.replace("\\", "/") for marker in transient)
        and not line.rstrip().endswith((".pyc", ".pyo"))
    )
    return True, commit, status


def _source_fingerprint(root: Path, files, git_status=""):
    return _workspace.source_fingerprint(root, files, git_status)


def _dirty_paths(status):
    return _workspace.dirty_paths(status)


def _test_inventory(root: Path):
    return _workspace.test_inventory(root)


def settings():
    with _connect() as conn:
        row = conn.execute("SELECT * FROM selfmod_settings WHERE id=1").fetchone()
    return {
        "mode": row["mode"], "enabled": bool(row["enabled"]),
        "retention_days": row["retention_days"],
        "retention_bytes": row["retention_bytes"],
    }


def set_mode(mode: str):
    mode = str(mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError("mode must be observe, propose, or auto-low-risk")
    with _tx() as conn:
        conn.execute("UPDATE selfmod_settings SET mode=?,updated_ts=? WHERE id=1", (mode, time.time()))
        _event(conn, None, "mode", "self-modification mode set to %s" % mode)
    return settings()


def set_enabled(enabled: bool):
    with _tx() as conn:
        conn.execute("UPDATE selfmod_settings SET enabled=?,updated_ts=? WHERE id=1", (int(bool(enabled)), time.time()))
        _event(conn, None, "enabled", "enabled=%s" % bool(enabled))
    return settings()


def set_retention(days: int, total_bytes: int):
    days = max(1, min(int(days), 3650))
    total_bytes = max(1024 * 1024, min(int(total_bytes), 1024**4))
    with _tx() as conn:
        conn.execute(
            "UPDATE selfmod_settings SET retention_days=?,retention_bytes=?,updated_ts=? WHERE id=1",
            (days, total_bytes, time.time()),
        )
        _event(conn, None, "retention", "days=%d bytes=%d" % (days, total_bytes))
    return settings()


def _risk(files, requested="", objective=""):
    requested = str(requested or "").lower().strip()
    if any(_protected(path) for path in files):
        return "critical"
    high_words = ("auth", "permission", "secret", "network", "cloud", "delete", "migration", "deploy", "dependency")
    risk_text = (" ".join(files) + " " + str(objective or "")).lower()
    if requested in {"high", "critical"} or any(word in risk_text for word in high_words):
        return "high"
    if requested == "low":
        return "low"
    return "medium"


def create_plan(
    objective: str, repository_root, *, problem="", evidence=None, files=None,
    criteria=None, risk="", expected_benefit="", rollback_plan="",
    maintenance_authorized=False, budgets=None, created_by="",
):
    config = settings()
    if not config["enabled"]:
        raise PermissionError("self-modification is disabled")
    objective = str(objective or "").strip()
    if not objective:
        raise ValueError("bounded objective is required")
    root = Path(repository_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("repository root does not exist")
    state = state_root().expanduser().resolve(strict=False)
    if state == root or root in state.parents:
        raise ValueError("selfmod state/backups must be stored outside the editable repository")
    raw_files = list(files or [])
    normalized = sorted({_rel(root, path) for path in raw_files})
    limits = dict(DEFAULT_BUDGETS)
    limits.update({k: int(v) for k, v in (budgets or {}).items() if k in limits})
    if len(normalized) > limits["max_files_changed"]:
        raise ValueError("affected files exceed max_files_changed")
    classification = _risk(normalized, risk, objective)
    protected = [path for path in normalized if _protected(path)]
    if protected and not maintenance_authorized:
        raise PermissionError("protected paths require an explicit maintenance run: %s" % ", ".join(protected))
    git_mode, commit, git_status = _git_info(root)
    overlap = set(normalized) & _dirty_paths(git_status)
    if overlap:
        raise RuntimeError(
            "declared files already contain user-owned Git changes: %s"
            % ", ".join(sorted(overlap))
        )
    run_id = "selfmod-%s" % uuid.uuid4().hex[:12]
    now = time.time()
    ev = [str(item)[:4000] for item in (evidence or []) if str(item).strip()]
    if not ev:
        raise ValueError("proposal requires concrete evidence")
    acceptance = [str(item)[:2000] for item in (criteria or []) if str(item).strip()]
    if not acceptance:
        raise ValueError("proposal requires acceptance criteria")
    phase = "observed" if config["mode"] == "observe" else "proposed"
    fingerprint = _source_fingerprint(root, normalized, git_status)
    with _tx() as conn:
        conn.execute(
            """INSERT INTO selfmod_runs(
              id,objective,problem,evidence_json,files_json,criteria_json,risk,
              expected_benefit,rollback_plan,repository_root,phase,mode,starting_commit,
              git_status_start,source_fingerprint,approval_required,maintenance_authorized,
              created_ts,updated_ts,budgets_json,test_inventory_before,created_by)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, objective, problem or objective, _json(ev), _json(normalized),
             _json(acceptance), classification, expected_benefit or "bounded reliability improvement",
             rollback_plan or "restore tamper-evident backup bundle", str(root), phase, config["mode"],
             commit, git_status, fingerprint, int(config["mode"] != "auto-low-risk" or classification != "low"),
             int(bool(maintenance_authorized)), now, now, _json(limits), _json(_test_inventory(root)), str(created_by or "")[:200]),
        )
        _event(conn, run_id, "proposal", "created %s-risk proposal for %d file(s); git=%s" % (classification, len(normalized), git_mode))
    return get_run(run_id)


def _decode_run(row):
    if row is None:
        return None
    data = dict(row)
    for name in ("evidence", "files", "criteria", "budgets", "test_inventory_before", "test_inventory_after"):
        source = name + "_json" if name in {"evidence", "files", "criteria", "budgets"} else name
        try:
            data[name] = json.loads(data.pop(source, "[]") or "[]")
        except ValueError:
            data[name] = [] if name != "budgets" else dict(DEFAULT_BUDGETS)
    data["approval_required"] = bool(data["approval_required"])
    data["auto_evaluation_eligible"] = bool(data["auto_evaluation_eligible"])
    data["maintenance_authorized"] = bool(data["maintenance_authorized"])
    return data


def get_run(run_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM selfmod_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError("unknown selfmod run %s" % run_id)
    return _decode_run(row)


def list_runs(limit=20):
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM selfmod_runs ORDER BY created_ts DESC LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()
    return [_decode_run(row) for row in rows]


def events(run_id="", limit=200):
    with _connect() as conn:
        if run_id:
            rows = conn.execute("SELECT * FROM selfmod_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM selfmod_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def _phase(run_id, allowed, target, kind, details="", **updates):
    with _tx() as conn:
        row = conn.execute("SELECT phase FROM selfmod_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("unknown selfmod run")
        if row["phase"] not in set(allowed):
            raise RuntimeError("cannot move %s from %s to %s" % (run_id, row["phase"], target))
        if target == "cancelled" and conn.execute("SELECT 1 FROM selfmod_deployment_lock WHERE id=1 AND owner_id IS NOT NULL AND run_id=?", (run_id,)).fetchone():
            raise RuntimeError("run cannot be cancelled while it holds the deployment lock (live or stale); restore it instead")
        fields = ["phase=?", "updated_ts=?"]
        values = [target, time.time()]
        for key, value in updates.items():
            fields.append("%s=?" % key)
            values.append(value)
        values.append(run_id)
        conn.execute("UPDATE selfmod_runs SET %s WHERE id=?" % ",".join(fields), values)
        _event(conn, run_id, kind, details or target)
    return get_run(run_id)


def _backup_dir(run_id):
    return backups_root() / run_id


def create_backup(run_id):
    hooks = _backup_restore.BackupHooks(
        get_run=get_run,
        backup_dir=_backup_dir,
        tx=_tx,
        phase=_phase,
        verify_backup=verify_backup,
    )
    return _backup_restore.create_backup(run_id, hooks=hooks)


def _load_manifest(run_id):
    return _backup_restore.load_manifest(run_id, backup_dir_fn=_backup_dir)


def verify_backup(run_id):
    return _backup_restore.verify_backup(
        run_id, load_manifest_fn=_load_manifest, tx_fn=_tx, event_fn=_event,
    )


def prepare_workspace(run_id):
    return _workspace.prepare_workspace(
        run_id,
        get_run_fn=get_run,
        workspaces_root_fn=workspaces_root,
        git_info_fn=_git_info,
        git_fn=_git,
        phase_fn=_phase,
    )


def candidate_path(run_id):
    run = get_run(run_id)
    path = Path(run.get("workspace_path") or "")
    if not path.is_dir():
        raise RuntimeError("candidate workspace is unavailable")
    return path


def apply_candidate_changes(run_id, changes):
    return _workspace.apply_candidate_changes(
        run_id,
        changes,
        get_run_fn=get_run,
        candidate_path_fn=candidate_path,
        tx_fn=_tx,
        event_fn=_event,
        inspect_diff_fn=inspect_diff,
    )


def _workspace_diff(run):
    return _workspace.workspace_diff(run, candidate_path_fn=candidate_path)


def _all_candidate_changes(run):
    return _workspace.all_candidate_changes(
        run, candidate_path_fn=candidate_path, git_info_fn=_git_info, git_fn=_git,
    )


def inspect_diff(run_id):
    run = get_run(run_id)
    diff, changed, additions, deletions = _workspace_diff(run)
    all_changed = _all_candidate_changes(run)
    unrelated = set(all_changed) - set(run["files"])
    if unrelated:
        raise RuntimeError("candidate diff escaped approved scope: %s" % ", ".join(sorted(unrelated)))
    if len(changed) > run["budgets"]["max_files_changed"] or additions + deletions > run["budgets"]["max_lines_changed"]:
        raise RuntimeError("candidate diff exceeds approved budgets")
    with _tx() as conn:
        conn.execute("UPDATE selfmod_runs SET diff_text=?,updated_ts=? WHERE id=?", (diff, time.time(), run_id))
        _event(conn, run_id, "diff", "%d files, +%d/-%d lines" % (len(changed), additions, deletions))
    return {"diff": diff, "changed_files": changed, "additions": additions, "deletions": deletions}


def diff_text(run_id):
    run = get_run(run_id)
    if run["phase"] in {"deployed", "rollback_requested", "restored", "rejected", "cancelled"}:
        return run.get("diff_text", "")
    return inspect_diff(run_id)["diff"]


def _candidate_snapshot(run_id):
    """SHA-256 of every changed candidate file and of the diff, from disk."""
    diff = inspect_diff(run_id)
    workspace = candidate_path(run_id)
    files = {}
    for rel in sorted(str(item) for item in diff["changed_files"]):
        path = workspace / rel
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    digest = hashlib.sha256(str(diff["diff"]).encode("utf-8", "surrogatepass")).hexdigest()
    return {"files": files, "diff_sha256": digest}


def tested_digests(run_id):
    """The tested-bytes record written when testing began, or None."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT path,sha256,diff_sha256 FROM selfmod_tested_files WHERE run_id=? ORDER BY path",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    return {"files": {row[0]: row[1] for row in rows}, "diff_sha256": rows[0][2]}


def _discard_candidate_bytecode(workspace: Path) -> None:
    _workspace.discard_candidate_bytecode(workspace)


def begin_testing(run_id):
    """Enter testing and bind the candidate bytes that the tests will see."""
    _discard_candidate_bytecode(candidate_path(run_id))
    snapshot = _candidate_snapshot(run_id)
    existing = tested_digests(run_id)
    if existing is not None and existing != snapshot:
        raise RuntimeError("candidate bytes changed since testing began")
    if existing is None:
        now = time.time()
        with _tx() as conn:
            for rel, digest in snapshot["files"].items():
                conn.execute(
                    "INSERT INTO selfmod_tested_files(run_id,path,sha256,diff_sha256,recorded_ts) VALUES(?,?,?,?,?)",
                    (run_id, rel, digest, snapshot["diff_sha256"], now),
                )
            _event(conn, run_id, "tested_bytes", "bound %d file(s) before validation" % len(snapshot["files"]))
    return _phase(run_id, {"editing", "interrupted"}, "testing", "testing", "host-controlled validation started")


def _auto_candidate(run) -> bool:
    return _verification.auto_candidate(run)


def _record_command(
    run, kind, command, cwd_path, seconds, expect_failure=False, receipt=None,
    protected_paths=(), low_integrity=None, isolation=None,
):
    def _supervisor():
        from scripts.selfmod_linux_isolation import candidate_supervisor
        return candidate_supervisor()

    hooks = _verification.RecordCommandHooks(
        candidate_supervisor=_supervisor,
        linux_isolation_unconfigured=_linux_isolation_unconfigured,
        run_cmd=_run,
        tx=_tx,
        event=_event,
    )
    return _verification.record_command(
        run,
        kind,
        command,
        cwd_path,
        seconds,
        expect_failure=expect_failure,
        receipt=receipt,
        protected_paths=protected_paths,
        low_integrity=low_integrity,
        isolation=isolation,
        hooks=hooks,
    )


def _linux_isolation_unconfigured() -> str:
    """The actionable refusal for a Linux host without a candidate uid."""
    try:
        from scripts.selfmod_linux_isolation import UNCONFIGURED_GUIDANCE
    except ImportError:
        return "Linux candidate isolation is not configured"
    return UNCONFIGURED_GUIDANCE


def candidate_isolation_refusal() -> str | None:
    """Why this host cannot isolate candidate checks, else ``None``."""
    from scripts import selfmod_linux_isolation

    refusal = selfmod_linux_isolation.candidate_isolation_preflight()
    if refusal:
        return refusal
    _runner, kind = selfmod_linux_isolation.candidate_supervisor()
    if kind == selfmod_linux_isolation.ATTESTATION:
        try:
            selfmod_linux_isolation.require_not_candidate_writable([database_path()])
        except selfmod_linux_isolation.LinuxIsolationUnavailable as error:
            return "selfmod ledger is exposed to the candidate uid: %s; see %s" % (
                str(error)[:300], selfmod_linux_isolation.ISOLATION_DOC)
    return None


def operator_candidate_isolation(
    *, unisolated_requested: bool, operator_attended: bool, run_id: str = "",
) -> bool:
    return _workspace.operator_candidate_isolation(
        unisolated_requested=unisolated_requested,
        operator_attended=operator_attended,
        run_id=run_id,
        refusal_fn=candidate_isolation_refusal,
        get_run_fn=get_run,
        auto_candidate_fn=_auto_candidate,
        tx_fn=_tx,
        event_fn=_event,
    )


def evaluator_truth_paths(run_id, extra=()) -> tuple[str, ...]:
    return _workspace.evaluator_truth_paths(run_id, extra, backup_dir_fn=_backup_dir)


def record_reproducer_before(run_id, command, timeout=None):
    """Prove the declared defect exists in the untouched live source."""
    run = get_run(run_id)
    if run["phase"] not in {"editing", "testing"}:
        raise RuntimeError("baseline reproducer requires editing/testing phase")
    seconds = min(int(timeout or run["budgets"]["max_test_seconds"]), run["budgets"]["max_test_seconds"])
    return _record_command(run, "reproducer_before", command, Path(run["repository_root"]), seconds, expect_failure=True)


def record_test(
    run_id, kind, command, *, cwd=None, timeout=None, protected_paths=(),
    low_integrity=None, isolation=None,
):
    run = get_run(run_id)
    if run["phase"] != "testing":
        raise RuntimeError("tests may run only in testing phase")
    if kind in ("host_grade", "oracle_grade", *_integrity.LEDGER_KINDS):
        raise PermissionError("%s can only be recorded by the parent scorer" % kind.replace("_", " "))
    workspace = candidate_path(run_id)
    cwd_path = workspace if cwd is None else (workspace / _rel(workspace, cwd)).parent
    seconds = min(int(timeout or run["budgets"]["max_test_seconds"]), run["budgets"]["max_test_seconds"])
    return _record_command(
        run, kind, command, cwd_path, seconds,
        protected_paths=protected_paths, low_integrity=low_integrity,
        isolation=isolation,
    )


def record_host_grade(run_id, probe_id, *, passed: bool, detail: str) -> dict:
    return _verification.record_host_grade(
        run_id,
        probe_id,
        passed=passed,
        detail=detail,
        get_run_fn=get_run,
        tx_fn=_tx,
        event_fn=_event,
    )


def _baseline_binding(run):
    """The baseline a verdict is about: starting commit and sealed manifest."""
    manifest = _load_manifest(run["id"])
    if manifest.get("starting_commit") != run["starting_commit"]:
        raise RuntimeError("backup manifest names a different starting commit")
    return {
        "starting_commit": run["starting_commit"] or "",
        "manifest_sha256": _sha(_backup_dir(run["id"]) / "manifest.json"),
    }


def record_oracle_grade(run_id, probe, *, challenge, loaded, attestation,
                        confidential: bool, read_denied: bool) -> dict:
    from scripts import selfmod_oracle

    hooks = _verification.OracleGradeHooks(
        get_run=get_run,
        candidate_snapshot=_candidate_snapshot,
        tested_digests=tested_digests,
        baseline_binding=_baseline_binding,
        current_case_digest=selfmod_oracle.current_digest,
        tx=_tx,
        event=_event,
    )
    return _verification.record_oracle_grade(
        run_id,
        probe,
        challenge=challenge,
        loaded=loaded,
        attestation=attestation,
        confidential=confidential,
        read_denied=read_denied,
        hooks=hooks,
    )


def oracle_receipt(run_id):
    return _verification.load_oracle_receipt(run_id, connect_fn=_connect)


def _oracle_admission_refusal(run_id, run):
    return _verification.oracle_admission_refusal(
        run_id,
        run,
        oracle_receipt_fn=oracle_receipt,
        tested_digests_fn=tested_digests,
        candidate_snapshot_fn=_candidate_snapshot,
        baseline_binding_fn=_baseline_binding,
    )


def _module_name(rel: str):
    return _verification.module_name(rel)


def smoke_plan(run_id):
    return _verification.smoke_plan(
        run_id, get_run_fn=get_run, candidate_path_fn=candidate_path,
    )


def _smoke_receipt(workspace: Path, present, absent) -> str:
    return _verification.smoke_receipt(workspace, present, absent)


def record_smoke(run_id, *, timeout=None, protected_paths=(), low_integrity=None, isolation=None):
    return _verification.record_smoke(
        run_id,
        timeout=timeout,
        protected_paths=protected_paths,
        low_integrity=low_integrity,
        isolation=isolation,
        smoke_probe_script=_SMOKE_PROBE,
        get_run_fn=get_run,
        smoke_plan_fn=smoke_plan,
        record_command_fn=_record_command,
    )


def test_results(run_id):
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM selfmod_tests WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    return [{**dict(row), "command": json.loads(row["command_json"]), "passed": bool(row["passed"])} for row in rows]


def _backup_rehearsal(run_id):
    return _backup_restore.backup_rehearsal(run_id, verify_backup_fn=verify_backup)


def review(run_id, *, require_kinds=None, unevaluated=()):
    hooks = _verification.ReviewHooks(
        get_run=get_run,
        inspect_diff=inspect_diff,
        test_results=test_results,
        tested_digests=tested_digests,
        candidate_snapshot=_candidate_snapshot,
        verify_backup=verify_backup,
        candidate_path=candidate_path,
        backup_rehearsal=_backup_rehearsal,
        oracle_admission_refusal=_oracle_admission_refusal,
        phase=_phase,
        restore=restore,
        approve=approve,
    )
    return _verification.review_run(
        run_id, require_kinds=require_kinds, unevaluated=unevaluated, hooks=hooks,
    )


def approve(run_id, approver="user"):
    run = get_run(run_id)
    if run["phase"] != "reviewing":
        raise RuntimeError("only a reviewed run can be approved")
    if run["risk"] in {"high", "critical"} and str(approver).startswith("host:"):
        raise PermissionError("high-risk changes require explicit user approval")
    if str(approver).startswith("host:") and (
        approver != "host:auto-low-risk" or not run["auto_evaluation_eligible"]
        or run["mode"] != "auto-low-risk" or run["risk"] != "low"
        or run["approval_required"]
    ):
        raise PermissionError("host approval requires qualified low-integrity candidate checks")
    if str(approver).startswith("host:"):
        refusal = _oracle_admission_refusal(run_id, run)
        if refusal:
            raise PermissionError("host approval requires an independent oracle receipt: %s" % refusal)
    return _phase(run_id, {"reviewing"}, "approved", "approval", "approved by %s" % approver, approved_by=str(approver)[:200], approved_ts=time.time())


def reject(run_id, reason="user rejected"):
    run = get_run(run_id)
    if run["phase"] in {"deployed", "restored"}:
        raise RuntimeError("deployed work must use rollback")
    if run["phase"] not in PHASES or run["phase"] in TERMINAL_PHASES:
        raise RuntimeError("run cannot be rejected from %s" % run["phase"])
    _phase(run_id, {run["phase"]}, "rejected", "rejection", reason, last_error=reason)
    return restore(run_id, from_candidate_only=True)


def _pid_alive(pid):
    return _process_pid_alive(pid)


def _deployment_owner_stale(row, now=None):
    now = float(now or time.time())
    if not row["owner_id"]:
        return True
    if row["owner_host"] == socket.gethostname():
        return not _pid_alive(row["owner_pid"])
    return float(row["lease_until"] or 0) < now


def _renew_deployment_lock(owner_id, lease_seconds=LEASE_SECONDS):
    with _tx() as conn:
        cursor = conn.execute(
            "UPDATE selfmod_deployment_lock SET lease_until=? WHERE id=1 AND owner_id=?",
            (time.time() + max(60, int(lease_seconds)), owner_id),
        )
    if cursor.rowcount != 1:
        raise RuntimeError("deployment lock ownership was lost")


def _release_deployment_lock(run_id, owner_id):
    with _tx() as conn:
        cursor = conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id=NULL,owner_pid=NULL,owner_host=NULL,lease_until=NULL,run_id=NULL WHERE id=1 AND owner_id=?",
            (owner_id,),
        )
        if cursor.rowcount:
            _event(conn, run_id, "unlock", "deployment lock released")


@contextlib.contextmanager
def deployment_lock(run_id, owner_id=None):
    owner_id = owner_id or uuid.uuid4().hex
    now = time.time()
    with _tx() as conn:
        row = conn.execute("SELECT * FROM selfmod_deployment_lock WHERE id=1").fetchone()
        stale = _deployment_owner_stale(row, now)
        if not stale:
            raise SelfmodStageNotApplied("another deployment/rollback holds the process-safe lock")
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id=?,owner_pid=?,owner_host=?,lease_until=?,run_id=? WHERE id=1",
            (owner_id, os.getpid(), socket.gethostname(), now + LEASE_SECONDS, run_id),
        )
        _event(conn, run_id, "lock", "deployment lock acquired")
    try:
        yield owner_id
    finally:
        _release_deployment_lock(run_id, owner_id)


def _current_source_matches(run):
    root = Path(run["repository_root"])
    _, commit, status = _git_info(root)
    if commit != run["starting_commit"]:
        return False, "starting Git commit changed"
    fingerprint = _source_fingerprint(root, run["files"], status)
    if fingerprint != run["source_fingerprint"]:
        return False, "source tree changed since proposal; possible user conflict"
    return True, ""


def _atomic_copy(source: Path, target: Path, mode=None):
    _backup_restore.atomic_copy(source, target, mode)


def _manifest_live_mismatches(manifest):
    return _backup_restore.manifest_live_mismatches(manifest)


def _restore_manifest_files(manifest):
    """Restore only the manifest's preflight-verified, scoped files."""
    root = Path(manifest["repository_root"])
    for record in manifest["files"]:
        target = root / record["path"]
        if record["existed_before"]:
            _atomic_copy(Path(record["backup_path"]), target, record.get("mode_before"))
            _remove_bytecode_cache(target)
            if _sha(target) != record["sha256_before"]:
                raise RuntimeError("restored hash mismatch for %s" % record["path"])
        elif target.exists():
            target.unlink()
            _remove_bytecode_cache(target)


def _record_deployed_files(run_id, root, paths):
    return _backup_restore.record_deployed_files(
        run_id, root, paths, tx_fn=_tx, event_fn=_event,
    )


def _deployed_file_mismatches(run_id):
    return _backup_restore.deployed_file_mismatches(
        run_id, get_run_fn=get_run, connect_fn=_connect,
    )


def _remove_bytecode_cache(target: Path):
    _backup_restore.remove_bytecode_cache(target)


def _receipt_digest(pairs):
    return _backup_restore.receipt_digest(pairs)


def expected_rollback_receipt(run_id):
    return _backup_restore.expected_rollback_receipt(
        run_id, load_manifest_fn=_load_manifest,
    )


def _probe_digest(root, records):
    return _backup_restore.probe_digest(root, records)


def verify_rollback_ready(run_id):
    import selfmod_recover

    hooks = _backup_restore.RollbackVerifyHooks(
        verify_backup=verify_backup,
        deployed_file_mismatches=_deployed_file_mismatches,
        restore_manifest_files=_restore_manifest_files,
        emergency_restore=selfmod_recover.restore,
    )
    return _backup_restore.verify_rollback_ready(run_id, hooks=hooks)


def rollback_probe_command(run_id):
    return _backup_restore.rollback_probe_command(
        run_id, state_root_fn=state_root, database_path_fn=database_path,
    )


def _rollback_probe_detail(code, output, expected_receipt):
    return _backup_restore.rollback_probe_detail(code, output, expected_receipt)


def _verify_deployed_rollback(run_id, root, timeout):
    return _backup_restore.verify_deployed_rollback(
        run_id,
        root,
        timeout,
        probe_command_fn=rollback_probe_command,
        expected_receipt_fn=expected_rollback_receipt,
        run_fn=_run,
    )


def _digest_mismatches(workspace: Path, changed_files, expected_digests) -> list:
    return _backup_restore.digest_mismatches(workspace, changed_files, expected_digests)


def deploy(run_id, *, health_command=None, commit=True, expected_digests=None):
    """Install an approved single-file candidate."""
    hooks = _backup_restore.DeployHooks(
        get_run=get_run,
        deployment_lock=deployment_lock,
        verify_backup=verify_backup,
        current_source_matches=_current_source_matches,
        inspect_diff=inspect_diff,
        renew_deployment_lock=_renew_deployment_lock,
        candidate_path=candidate_path,
        tested_digests=tested_digests,
        atomic_copy=_atomic_copy,
        remove_bytecode_cache=_remove_bytecode_cache,
        git_info=_git_info,
        git=_git,
        tx=_tx,
        event=_event,
        record_deployed_files=_record_deployed_files,
        phase=_phase,
        verify_deployed_rollback=_verify_deployed_rollback,
        run_cmd=_run,
        restore=restore,
    )
    return _backup_restore.deploy_run(
        run_id,
        health_command=health_command,
        commit=commit,
        expected_digests=expected_digests,
        hooks=hooks,
    )


def restore(run_id, from_candidate_only=False):
    hooks = _backup_restore.RestoreHooks(
        get_run=get_run,
        verify_backup=verify_backup,
        manifest_live_mismatches=_manifest_live_mismatches,
        restore_manifest_files=_restore_manifest_files,
        connect=_connect,
        tx=_tx,
        event=_event,
        tested_digests=tested_digests,
        git_info=_git_info,
        git=_git,
        phase=_phase,
    )
    return _backup_restore.restore_run(
        run_id, from_candidate_only=from_candidate_only, hooks=hooks,
    )


def rollback(run_id, reason="user requested rollback"):
    run = get_run(run_id)
    if run["phase"] != "deployed":
        raise SelfmodStageNotApplied("only a deployed run can be rolled back")
    with deployment_lock(run_id):
        conflicts = _deployed_file_mismatches(run_id)
        if conflicts:
            raise SelfmodStageNotApplied(
                "rollback conflict: deployed files changed after deployment; "
                "preserving current bytes: %s" % ", ".join(conflicts)
            )
        _phase(run_id, {"deployed"}, "rollback_requested", "rollback", reason)
        return restore(run_id)


def cancel(run_id):
    run = get_run(run_id)
    if run["phase"] in TERMINAL_PHASES | {"deployed", "rollback_requested"}:
        raise RuntimeError("run cannot be cancelled from %s" % run["phase"])
    return _phase(run_id, {run["phase"]}, "cancelled", "cancel", "cancelled by user")


def reconcile_interrupted(now=None):
    now = float(now or time.time())
    changed = 0
    with _tx() as conn:
        rows = conn.execute("SELECT id,phase,owner_pid,owner_host,lease_until FROM selfmod_runs WHERE phase IN ('editing','testing','reviewing') AND owner_id IS NOT NULL").fetchall()
        for row in rows:
            stale = float(row["lease_until"] or 0) < now or (row["owner_host"] == socket.gethostname() and not _pid_alive(row["owner_pid"]))
            if stale:
                conn.execute("UPDATE selfmod_runs SET phase='interrupted',owner_id=NULL,owner_pid=NULL,owner_host=NULL,lease_until=NULL,updated_ts=? WHERE id=?", (now, row["id"]))
                _event(conn, row["id"], "interrupted", "stale owner reconciled")
                changed += 1
    return changed


def reconcile_stale_deployment(now=None):
    """Fail closed after a deployer dies between atomic file replacements."""
    current = float(now or time.time())
    run_id = ""
    recovery_owner = "recovery-" + uuid.uuid4().hex
    with _tx() as conn:
        lock = conn.execute("SELECT * FROM selfmod_deployment_lock WHERE id=1").fetchone()
        if not lock["owner_id"]:
            return 0
        stale = _deployment_owner_stale(lock, current)
        if not stale:
            return 0
        run_id = lock["run_id"] or ""
        row = conn.execute("SELECT phase FROM selfmod_runs WHERE id=?", (run_id,)).fetchone() if run_id else None
        if row and row["phase"] in {"approved", "deployed", "rollback_requested"}:
            conn.execute(
                "UPDATE selfmod_deployment_lock SET owner_id=?,owner_pid=?,owner_host=?,lease_until=? WHERE id=1 AND owner_id=?",
                (
                    recovery_owner, os.getpid(), socket.gethostname(),
                    current + LEASE_SECONDS, lock["owner_id"],
                ),
            )
            if row["phase"] != "rollback_requested":
                conn.execute("UPDATE selfmod_runs SET phase='rollback_requested',last_error='deployment owner interrupted',updated_ts=? WHERE id=?", (current, run_id))
            _event(conn, run_id, "deployment_interrupted", "stale deployment owner; exact restore required")
        else:
            conn.execute(
                "UPDATE selfmod_deployment_lock SET owner_id=NULL,owner_pid=NULL,owner_host=NULL,lease_until=NULL,run_id=NULL WHERE id=1 AND owner_id=?",
                (lock["owner_id"],),
            )
            run_id = ""
    if run_id:
        try:
            restore(run_id)
            return 1
        finally:
            _release_deployment_lock(run_id, recovery_owner)
    return 0


def resume(run_id):
    run = get_run(run_id)
    if run["phase"] != "interrupted":
        raise RuntimeError("only interrupted runs require explicit resume")
    target = "editing" if run.get("workspace_path") else "backed_up"
    return _phase(run_id, {"interrupted"}, target, "resume", "explicit resume")


def claim(run_id, owner_id=None, lease_seconds=LEASE_SECONDS):
    owner_id = owner_id or uuid.uuid4().hex
    with _tx() as conn:
        row = conn.execute("SELECT phase,owner_id,lease_until FROM selfmod_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("unknown run")
        if row["owner_id"] and float(row["lease_until"] or 0) > time.time():
            raise RuntimeError("selfmod run already has a live owner")
        conn.execute("UPDATE selfmod_runs SET owner_id=?,owner_pid=?,owner_host=?,lease_until=?,updated_ts=? WHERE id=?", (owner_id, os.getpid(), socket.gethostname(), time.time() + lease_seconds, time.time(), run_id))
        _event(conn, run_id, "claim", "owner claimed run")
    return owner_id


def release(run_id, owner_id):
    with _tx() as conn:
        conn.execute("UPDATE selfmod_runs SET owner_id=NULL,owner_pid=NULL,owner_host=NULL,lease_until=NULL,updated_ts=? WHERE id=? AND owner_id=?", (time.time(), run_id, owner_id))
        _event(conn, run_id, "release", "owner released run")


def heartbeat(run_id, owner_id, lease_seconds=LEASE_SECONDS):
    with _tx() as conn:
        cursor = conn.execute(
            "UPDATE selfmod_runs SET lease_until=?,updated_ts=? WHERE id=? AND owner_id=?",
            (time.time() + max(60, int(lease_seconds)), time.time(), run_id, owner_id),
        )
    return cursor.rowcount > 0


def prune_workspaces(retention_days=None):
    return _workspace.prune_workspaces(
        retention_days=retention_days,
        workspaces_root_fn=workspaces_root,
        settings_fn=settings,
        connect_fn=_connect,
        decode_run_fn=_decode_run,
        git_fn=_git,
        active_phases=ACTIVE_PHASES,
    )


def prune_backups(retention_days=None, retention_bytes=None):
    return _backup_restore.prune_backups(
        retention_days=retention_days,
        retention_bytes=retention_bytes,
        settings_fn=settings,
        backups_root_fn=backups_root,
        load_manifest_fn=_load_manifest,
        connect_fn=_connect,
        active_phases=ACTIVE_PHASES,
    )


def run_totals():
    """Counts over EVERY run, not just the most recent window."""
    placeholders = ",".join("?" * len(ACTIVE_PHASES))
    with _connect() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM selfmod_runs WHERE phase IN (%s)" % placeholders,
            tuple(sorted(ACTIVE_PHASES)),
        ).fetchone()[0]
        deployed = conn.execute(
            "SELECT COUNT(*) FROM selfmod_runs WHERE phase='deployed'"
        ).fetchone()[0]
        rollback_points = conn.execute(
            "SELECT COUNT(*) FROM selfmod_runs "
            "WHERE backup_manifest IS NOT NULL AND backup_manifest != ''"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM selfmod_runs").fetchone()[0]
    return {
        "active": active,
        "deployed": deployed,
        "rollback_points": rollback_points,
        "total_runs": total,
    }


def status_data():
    reconcile_interrupted()
    reconcile_stale_deployment()
    runs = list_runs(20)
    return {
        **settings(), "database": str(database_path()), "backup_root": str(backups_root()),
        **run_totals(),
        "runs_shown": len(runs),
        "runs": [{k: run.get(k) for k in ("id", "objective", "phase", "risk", "approval_required", "updated_ts", "deployed_commit")} for run in runs],
    }


def format_status(data=None):
    return _workspace.format_status(data or status_data())


def format_run(run_id):
    return _workspace.format_run(get_run(run_id))


def parse_plan_text(text):
    return _workspace.parse_plan_text(text)


def recursive_guard():
    if os.environ.get("SONDER_SELFMOD_ACTIVE") == "1":
        raise RuntimeError("recursive self-improvement runs are forbidden")
