"""Host-controlled, auditable self-improvement lifecycle.

Candidate code never calls acceptance or deployment primitives directly. This
module owns state transitions, immutable backups, deterministic checks, locks,
deployment, and rollback. It is stdlib-only so recovery remains available when
the rest of Trilobite cannot import.
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import trilobite_paths


MODES = ("observe", "propose", "auto-low-risk")
PHASES = (
    "observed", "proposed", "backed_up", "editing", "testing", "reviewing",
    "approved", "deployed", "rollback_requested", "rejected", "restored",
    "interrupted", "cancelled",
)
ACTIVE_PHASES = {"backed_up", "editing", "testing", "reviewing", "approved", "rollback_requested"}
TERMINAL_PHASES = {"rejected", "restored", "cancelled"}
SENSITIVE_PREFIXES = (
    "permission_rules.py", "admin_auth.py", "file_ops.py", "safe_update.py",
    "selfmod.py", "selfmod_recover.py", "server.py", "reloadable_mcp.py",
    "autopilot_controller.py", "autopilot_store.py", "trilobite_paths.py", "trilobite_serve.py",
    "deploy_", "trilobite-runtime", "tests/test_permission", "tests/test_admin",
    "tests/test_control_plane", "tests/test_read_only_agent_policy",
    "tests/test_selfmod",
)
SENSITIVE_PARTS = (
    ".env", "credential", "secret", "token", "account", "migration",
    "permissions.json", "selfmod_policy", "selfmod.db", "audit",
)
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
  maintenance_authorized INTEGER NOT NULL DEFAULT 0, owner_id TEXT, owner_pid INTEGER,
  owner_host TEXT, lease_until REAL, deployed_commit TEXT, last_error TEXT NOT NULL DEFAULT '',
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL, deployed_ts REAL, restored_ts REAL,
  budgets_json TEXT NOT NULL
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
  output TEXT NOT NULL, passed INTEGER NOT NULL, created_ts REAL NOT NULL
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
"""


def state_root() -> Path:
    return Path(trilobite_paths.state_path("selfmod", "TRILOBITE_SELFMOD_HOME"))


def database_path() -> Path:
    override = os.environ.get("TRILOBITE_SELFMOD_DB", "").strip()
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
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(root: Path, path) -> str:
    raw = str(path or "").replace("\\", "/").strip().lstrip("/")
    if not raw or raw in {".", ".."} or "\x00" in raw:
        raise ValueError("file path must name a repository-relative file")
    candidate = (root / raw).resolve(strict=False)
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("file path escapes authorized repository") from exc
    if any(part in {".git", ".hg", ".svn"} for part in relative.parts):
        raise ValueError("version-control metadata cannot be modified")
    return relative.as_posix()


def _protected(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    return (
        any(lowered == p.lower() or lowered.startswith(p.lower()) for p in SENSITIVE_PREFIXES)
        or any(part in lowered for part in SENSITIVE_PARTS)
    )


def protected_paths():
    return {"prefixes": list(SENSITIVE_PREFIXES), "contains": list(SENSITIVE_PARTS)}


def _run(command, cwd, timeout=30):
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(command), cwd=str(cwd), text=True, capture_output=True,
            timeout=max(1, int(timeout)), check=False,
        )
        output = "\n".join(x.strip() for x in (result.stdout, result.stderr) if x and x.strip())
        return result.returncode, output[:100_000], int((time.monotonic() - started) * 1000)
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(str(x or "") for x in (exc.stdout, exc.stderr)).strip()
        return 124, (output + "\nTIMEOUT")[:100_000], int((time.monotonic() - started) * 1000)


def _git(root: Path, *args, timeout=30):
    return _run(["git", *args], root, timeout)[:2]


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
    rows = [git_status]
    for rel in sorted(files):
        path = root / rel
        rows.append("%s:%s" % (rel, _sha(path) if path.is_file() else "<missing>"))
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _dirty_paths(status):
    paths = set()
    for line in str(status or "").splitlines():
        pieces = line.strip().split(maxsplit=1)
        value = pieces[1] if len(pieces) == 2 else ""
        for item in value.split(" -> "):
            if item:
                paths.add(item.strip('"').replace("\\", "/"))
    return paths


def _test_inventory(root: Path):
    inventory = []
    for path in root.rglob("test_*.py"):
        if ".git" not in path.parts and path.is_file():
            inventory.append(path.relative_to(root).as_posix())
    return sorted(inventory)


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
    maintenance_authorized=False, budgets=None,
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
              created_ts,updated_ts,budgets_json,test_inventory_before)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, objective, problem or objective, _json(ev), _json(normalized),
             _json(acceptance), classification, expected_benefit or "bounded reliability improvement",
             rollback_plan or "restore immutable backup bundle", str(root), phase, config["mode"],
             commit, git_status, fingerprint, int(config["mode"] != "auto-low-risk" or classification != "low"),
             int(bool(maintenance_authorized)), now, now, _json(limits), _json(_test_inventory(root))),
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
    run = get_run(run_id)
    if run["phase"] != "proposed":
        raise RuntimeError("backup requires proposed phase")
    root = Path(run["repository_root"])
    bundle = _backup_dir(run_id)
    if bundle.exists():
        raise RuntimeError("backup bundle already exists")
    files_dir = bundle / "files"
    files_dir.mkdir(parents=True, exist_ok=False)
    records = []
    try:
        for rel in run["files"]:
            source = root / rel
            existed = source.is_file()
            destination = files_dir / rel
            record = {
                "path": rel, "existed_before": existed,
                "sha256_before": _sha(source) if existed else None,
                "size_before": source.stat().st_size if existed else 0,
                "mode_before": stat.S_IMODE(source.stat().st_mode) if existed else None,
                "backup_path": str(destination) if existed else None,
                "sha256_backup": None,
            }
            if existed:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                record["sha256_backup"] = _sha(destination)
                if record["sha256_backup"] != record["sha256_before"]:
                    raise RuntimeError("backup hash mismatch for %s" % rel)
            records.append(record)
        manifest = {
            "run_id": run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "repository_root": str(root), "starting_commit": run["starting_commit"],
            "files": records,
        }
        manifest_path = bundle / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_hash = _sha(manifest_path)
        (bundle / "manifest.sha256").write_text(manifest_hash + "\n", encoding="ascii")
        if os.name != "nt":
            for path in sorted(bundle.rglob("*"), reverse=True):
                with contextlib.suppress(OSError):
                    os.chmod(path, 0o500 if path.is_dir() else 0o400)
            os.chmod(bundle, 0o500)
    except Exception:
        shutil.rmtree(bundle, ignore_errors=True)
        raise
    with _tx() as conn:
        for record in records:
            conn.execute(
                "INSERT INTO selfmod_backups VALUES(?,?,?,?,?,?,?,?)",
                (run_id, record["path"], int(record["existed_before"]), record["sha256_before"],
                 record["size_before"], record["mode_before"], record["backup_path"], record["sha256_backup"]),
            )
    updated = _phase(run_id, {"proposed"}, "backed_up", "backup", "immutable backup verified", backup_manifest=str(manifest_path))
    verify_backup(run_id)
    return updated


def _load_manifest(run_id):
    bundle = _backup_dir(run_id)
    manifest_path = bundle / "manifest.json"
    checksum_path = bundle / "manifest.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise RuntimeError("backup manifest/checksum missing")
    expected = checksum_path.read_text(encoding="ascii").strip()
    if not expected or _sha(manifest_path) != expected:
        raise RuntimeError("backup manifest is corrupted")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError("backup manifest JSON is corrupted") from exc
    if manifest.get("run_id") != run_id:
        raise RuntimeError("backup run identity mismatch")
    return manifest


def verify_backup(run_id):
    manifest = _load_manifest(run_id)
    for record in manifest.get("files", []):
        if not record.get("existed_before"):
            continue
        path = Path(record.get("backup_path") or "")
        if not path.is_file() or _sha(path) != record.get("sha256_backup") or record.get("sha256_backup") != record.get("sha256_before"):
            raise RuntimeError("backup verification failed for %s" % record.get("path"))
    with _tx() as conn:
        _event(conn, run_id, "backup_verified", "%d file records verified" % len(manifest.get("files", [])))
    return manifest


def prepare_workspace(run_id):
    run = get_run(run_id)
    if run["phase"] != "backed_up":
        raise RuntimeError("workspace preparation requires verified backup")
    root = Path(run["repository_root"])
    destination = workspaces_root() / run_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError("candidate workspace already exists")
    git_mode, commit, _ = _git_info(root)
    branch = "selfmod/%s" % run_id
    if git_mode and commit:
        code, output = _git(root, "worktree", "add", "--detach", str(destination), commit, timeout=120)
        if code:
            raise RuntimeError("could not create isolated Git worktree: %s" % output)
        code, output = _git(destination, "switch", "-c", branch)
        if code:
            _git(root, "worktree", "remove", "--force", str(destination))
            raise RuntimeError("could not create selfmod branch: %s" % output)
    else:
        ignore = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".venv*", "venv*")
        shutil.copytree(root, destination, ignore=ignore)
        branch = ""
    return _phase(run_id, {"backed_up"}, "editing", "workspace", "isolated candidate workspace created", workspace_path=str(destination), branch_name=branch)


def candidate_path(run_id):
    run = get_run(run_id)
    path = Path(run.get("workspace_path") or "")
    if not path.is_dir():
        raise RuntimeError("candidate workspace is unavailable")
    return path


def apply_candidate_changes(run_id, changes):
    """Apply already-authorized file contents inside the isolated workspace."""
    run = get_run(run_id)
    if run["phase"] != "editing":
        raise RuntimeError("candidate edits require editing phase")
    if run["mode"] == "observe":
        raise PermissionError("observe mode cannot edit")
    workspace = candidate_path(run_id)
    allowed = set(run["files"])
    if len(changes) > run["budgets"]["max_files_changed"]:
        raise ValueError("change set exceeds file budget")
    for raw, content in changes.items():
        rel = _rel(workspace, raw)
        if rel not in allowed:
            raise PermissionError("candidate attempted an unrelated file: %s" % rel)
        if _protected(rel) and not run["maintenance_authorized"]:
            raise PermissionError("candidate attempted a protected file: %s" % rel)
        target = workspace / rel
        if content is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = str(content).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".selfmod-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, target)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    with _tx() as conn:
        _event(conn, run_id, "edit", "candidate changed: %s" % ", ".join(sorted(changes)))
    return inspect_diff(run_id)


def _workspace_diff(run):
    root = Path(run["repository_root"])
    workspace = candidate_path(run["id"])
    lines = []
    changed = []
    additions = deletions = 0
    for rel in run["files"]:
        before_path, after_path = root / rel, workspace / rel
        before = before_path.read_text(encoding="utf-8", errors="replace").splitlines(True) if before_path.is_file() else []
        after = after_path.read_text(encoding="utf-8", errors="replace").splitlines(True) if after_path.is_file() else []
        if before == after:
            continue
        changed.append(rel)
        diff = list(difflib.unified_diff(before, after, fromfile="a/" + rel, tofile="b/" + rel))
        lines.extend(diff)
        additions += sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        deletions += sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return "".join(lines), changed, additions, deletions


def _all_candidate_changes(run):
    root, workspace = Path(run["repository_root"]), candidate_path(run["id"])
    git_mode, _, _ = _git_info(workspace)
    if git_mode:
        code1, tracked = _git(workspace, "diff", "--name-only", "HEAD")
        code2, untracked = _git(workspace, "ls-files", "--others", "--exclude-standard")
        if code1 or code2:
            raise RuntimeError("could not inventory candidate changes")
        return sorted({
            rel for line in (tracked + "\n" + untracked).splitlines()
            if line.strip()
            for rel in [line.strip().replace("\\", "/")]
            if not any(part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"} for part in Path(rel).parts)
            and not rel.endswith((".pyc", ".pyo"))
        })
    paths = set()
    for base in (root, workspace):
        for path in base.rglob("*"):
            if path.is_file() and not any(part in {".git", "__pycache__", ".pytest_cache"} for part in path.parts):
                paths.add(path.relative_to(base).as_posix())
    changed = []
    for rel in paths:
        before, after = root / rel, workspace / rel
        if before.is_file() != after.is_file() or (before.is_file() and _sha(before) != _sha(after)):
            changed.append(rel)
    return sorted(changed)


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


def begin_testing(run_id):
    inspect_diff(run_id)
    return _phase(run_id, {"editing", "interrupted"}, "testing", "testing", "host-controlled validation started")


def _record_command(run, kind, command, cwd_path, seconds, expect_failure=False):
    run_id = run["id"]
    code, output, duration = _run(command, cwd_path, seconds)
    passed = code != 0 if expect_failure else code == 0
    with _tx() as conn:
        conn.execute(
            "INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, str(kind)[:80], _json(list(command)), code, duration, output, int(passed), time.time()),
        )
        _event(conn, run_id, "test", "%s exit=%s expected=%s duration_ms=%s" % (kind, code, "failure" if expect_failure else "success", duration))
    return {"kind": kind, "command": list(command), "exit_code": code, "duration_ms": duration, "output": output, "passed": passed}


def record_reproducer_before(run_id, command, timeout=None):
    """Prove the declared defect exists in the untouched live source."""
    run = get_run(run_id)
    if run["phase"] not in {"editing", "testing"}:
        raise RuntimeError("baseline reproducer requires editing/testing phase")
    seconds = min(int(timeout or run["budgets"]["max_test_seconds"]), run["budgets"]["max_test_seconds"])
    return _record_command(run, "reproducer_before", command, Path(run["repository_root"]), seconds, expect_failure=True)


def record_test(run_id, kind, command, *, cwd=None, timeout=None):
    run = get_run(run_id)
    if run["phase"] != "testing":
        raise RuntimeError("tests may run only in testing phase")
    workspace = candidate_path(run_id)
    cwd_path = workspace if cwd is None else (workspace / _rel(workspace, cwd)).parent
    seconds = min(int(timeout or run["budgets"]["max_test_seconds"]), run["budgets"]["max_test_seconds"])
    return _record_command(run, kind, command, cwd_path, seconds)


def test_results(run_id):
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM selfmod_tests WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    return [{**dict(row), "command": json.loads(row["command_json"]), "passed": bool(row["passed"])} for row in rows]


def _backup_rehearsal(run_id):
    manifest = verify_backup(run_id)
    with tempfile.TemporaryDirectory(prefix="trilobite-selfmod-rehearse-") as temp:
        root = Path(temp)
        for record in manifest["files"]:
            target = root / record["path"]
            if record["existed_before"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(record["backup_path"], target)
                if _sha(target) != record["sha256_before"]:
                    return False
            elif target.exists():
                target.unlink()
    return True


def review(run_id, *, require_kinds=None):
    run = get_run(run_id)
    if run["phase"] != "testing":
        raise RuntimeError("review requires testing phase")
    diff = inspect_diff(run_id)
    results = test_results(run_id)
    failures = []
    required = set(require_kinds or ("reproducer_before", "syntax", "targeted", "regression", "smoke"))
    if run["maintenance_authorized"]:
        required.add("security")
    seen = {row["kind"] for row in results if row["passed"]}
    if required - seen:
        failures.append("missing passing checks: %s" % ", ".join(sorted(required - seen)))
    if any(not row["passed"] for row in results):
        failures.append("one or more recorded checks failed")
    baseline = [row for row in results if row["kind"] == "reproducer_before"]
    if not baseline or not any(row["exit_code"] not in (None, 0) and row["passed"] for row in baseline):
        failures.append("original failure was not demonstrated before editing")
    if not diff["changed_files"]:
        failures.append("candidate produced no scoped diff")
    if any(_protected(path) for path in diff["changed_files"]) and not run["maintenance_authorized"]:
        failures.append("protected file modified")
    manifest = verify_backup(run_id)
    existing_tests = {
        record["path"] for record in manifest["files"]
        if record["existed_before"] and (
            record["path"].startswith("tests/")
            or Path(record["path"]).name.startswith("test_")
        )
    }
    weakened_surface = existing_tests & set(diff["changed_files"])
    if weakened_surface:
        failures.append(
            "pre-existing required tests were modified: %s"
            % ", ".join(sorted(weakened_surface))
        )
    after_inventory = _test_inventory(candidate_path(run_id))
    before_inventory = set(run["test_inventory_before"])
    if not before_inventory.issubset(set(after_inventory)):
        failures.append("test inventory was weakened")
    if not _backup_rehearsal(run_id):
        failures.append("rollback rehearsal failed")
    target = "rejected" if failures else "reviewing"
    updated = _phase(
        run_id, {"testing"}, target, "review",
        "; ".join(failures) if failures else "deterministic acceptance checks passed",
        test_inventory_after=_json(after_inventory), last_error="; ".join(failures),
    )
    if failures:
        restore(run_id, from_candidate_only=True)
    elif updated["mode"] == "auto-low-risk" and updated["risk"] == "low" and not updated["approval_required"]:
        approve(run_id, approver="host:auto-low-risk")
    return get_run(run_id)


def approve(run_id, approver="user"):
    run = get_run(run_id)
    if run["phase"] != "reviewing":
        raise RuntimeError("only a reviewed run can be approved")
    if run["risk"] in {"high", "critical"} and str(approver).startswith("host:"):
        raise PermissionError("high-risk changes require explicit user approval")
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
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def deployment_lock(run_id, owner_id=None):
    owner_id = owner_id or uuid.uuid4().hex
    now = time.time()
    with _tx() as conn:
        row = conn.execute("SELECT * FROM selfmod_deployment_lock WHERE id=1").fetchone()
        stale = not row["owner_id"] or float(row["lease_until"] or 0) < now or (row["owner_host"] == socket.gethostname() and not _pid_alive(row["owner_pid"]))
        if not stale:
            raise RuntimeError("another deployment/rollback holds the process-safe lock")
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id=?,owner_pid=?,owner_host=?,lease_until=?,run_id=? WHERE id=1",
            (owner_id, os.getpid(), socket.gethostname(), now + LEASE_SECONDS, run_id),
        )
        _event(conn, run_id, "lock", "deployment lock acquired")
    try:
        yield owner_id
    finally:
        with _tx() as conn:
            conn.execute("UPDATE selfmod_deployment_lock SET owner_id=NULL,owner_pid=NULL,owner_host=NULL,lease_until=NULL,run_id=NULL WHERE id=1 AND owner_id=?", (owner_id,))
            _event(conn, run_id, "unlock", "deployment lock released")


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
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".restore-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if mode is not None and os.name != "nt":
            os.chmod(temp_name, int(mode))
        os.replace(temp_name, target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)


def _remove_bytecode_cache(target: Path):
    if target.suffix != ".py":
        return
    cache = target.parent / "__pycache__"
    if not cache.is_dir():
        return
    for path in cache.glob(target.stem + ".*.py[co]"):
        with contextlib.suppress(OSError):
            path.unlink()


def deploy(run_id, *, health_command=None, commit=True):
    run = get_run(run_id)
    if run["phase"] != "approved":
        raise RuntimeError("deployment requires explicit/host approval")
    with deployment_lock(run_id):
        verify_backup(run_id)
        ok, conflict = _current_source_matches(run)
        if not ok:
            raise RuntimeError(conflict)
        diff = inspect_diff(run_id)
        if set(diff["changed_files"]) - set(run["files"]):
            raise RuntimeError("candidate diff no longer matches approved scope")
        root, workspace = Path(run["repository_root"]), candidate_path(run_id)
        try:
            for rel in diff["changed_files"]:
                source, target = workspace / rel, root / rel
                if source.is_file():
                    mode = target.stat().st_mode & 0o777 if target.exists() else source.stat().st_mode & 0o777
                    _atomic_copy(source, target, mode)
                    _remove_bytecode_cache(target)
                elif target.exists():
                    target.unlink()
                    _remove_bytecode_cache(target)
            deployed_commit = ""
            git_mode, _, _ = _git_info(root)
            if git_mode and commit and not run["git_status_start"].strip():
                code, output = _git(root, "add", "--", *diff["changed_files"])
                if code:
                    raise RuntimeError("could not stage isolated self-improvement: %s" % output)
                code, output = _git(root, "commit", "-m", "selfmod: %s" % run["objective"][:100])
                if code:
                    raise RuntimeError("could not commit self-improvement: %s" % output)
                _, deployed_commit = _git(root, "rev-parse", "HEAD")
                deployed_commit = deployed_commit.strip()
            _phase(run_id, {"approved"}, "deployed", "deploy", "candidate deployed atomically", deployed_commit=deployed_commit, deployed_ts=time.time())
            if health_command:
                code, output, duration = _run(health_command, root, min(120, run["budgets"]["max_test_seconds"]))
                with _tx() as conn:
                    conn.execute(
                        "INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, "post_deploy_health", _json(list(health_command)), code, duration, output, int(code == 0), time.time()),
                    )
                    _event(conn, run_id, "health", "post-deploy exit=%s" % code)
                if code:
                    _phase(run_id, {"deployed"}, "rollback_requested", "rollback", "post-deployment health check failed")
                    restore(run_id)
                    raise RuntimeError("deployment health check failed; automatic rollback completed")
            return get_run(run_id)
        except Exception:
            current = get_run(run_id)
            if current["phase"] == "approved":
                _phase(run_id, {"approved"}, "rollback_requested", "deploy_failure", "deployment failed; rollback requested")
                restore(run_id)
            raise


def restore(run_id, from_candidate_only=False):
    run = get_run(run_id)
    if from_candidate_only and run["phase"] in {"rejected", "reviewing", "testing", "editing", "interrupted"}:
        # Live source was never changed; verify backup and mark restored.
        verify_backup(run_id)
        return _phase(run_id, {run["phase"]}, "restored", "restore", "candidate rejected; live source unchanged", restored_ts=time.time())
    if run["phase"] not in {"rollback_requested", "deployed", "approved"}:
        raise RuntimeError("restore is not valid from %s" % run["phase"])
    manifest = verify_backup(run_id)
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
    if run.get("deployed_commit") and not run.get("git_status_start", "").strip():
        git_mode, _, _ = _git_info(root)
        if git_mode:
            paths = [record["path"] for record in manifest["files"]]
            code, output = _git(root, "add", "--", *paths)
            if code:
                raise RuntimeError("restored files but could not stage rollback commit: %s" % output)
            code, output = _git(root, "commit", "-m", "selfmod rollback: %s" % run["objective"][:90])
            if code:
                raise RuntimeError("restored files but could not record rollback commit: %s" % output)
    return _phase(run_id, {run["phase"]}, "restored", "restore", "exact backup hashes restored", restored_ts=time.time())


def rollback(run_id, reason="user requested rollback"):
    run = get_run(run_id)
    if run["phase"] != "deployed":
        raise RuntimeError("only a deployed run can be rolled back")
    with deployment_lock(run_id):
        _phase(run_id, {"deployed"}, "rollback_requested", "rollback", reason)
        return restore(run_id)


def cancel(run_id):
    run = get_run(run_id)
    if run["phase"] in TERMINAL_PHASES or run["phase"] == "deployed":
        raise RuntimeError("run cannot be cancelled")
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
    with _tx() as conn:
        lock = conn.execute("SELECT * FROM selfmod_deployment_lock WHERE id=1").fetchone()
        if not lock["owner_id"]:
            return 0
        stale = float(lock["lease_until"] or 0) < current or (
            lock["owner_host"] == socket.gethostname() and not _pid_alive(lock["owner_pid"])
        )
        if not stale:
            return 0
        run_id = lock["run_id"] or ""
        conn.execute("UPDATE selfmod_deployment_lock SET owner_id=NULL,owner_pid=NULL,owner_host=NULL,lease_until=NULL,run_id=NULL WHERE id=1")
        row = conn.execute("SELECT phase FROM selfmod_runs WHERE id=?", (run_id,)).fetchone() if run_id else None
        if row and row["phase"] in {"approved", "deployed"}:
            conn.execute("UPDATE selfmod_runs SET phase='rollback_requested',last_error='deployment owner interrupted',updated_ts=? WHERE id=?", (current, run_id))
            _event(conn, run_id, "deployment_interrupted", "stale deployment owner; exact restore required")
        else:
            run_id = ""
    if run_id:
        restore(run_id)
        return 1
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


def prune_backups(retention_days=None, retention_bytes=None):
    config = settings()
    days = int(retention_days if retention_days is not None else config["retention_days"])
    byte_limit = int(retention_bytes if retention_bytes is not None else config["retention_bytes"])
    bundles = []
    for path in backups_root().glob("selfmod-*") if backups_root().exists() else []:
        try:
            manifest = _load_manifest(path.name)
            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            bundles.append((path, manifest, size, path.stat().st_mtime))
        except Exception:
            continue
    bundles.sort(key=lambda item: item[3], reverse=True)
    newest_valid = bundles[0][0] if bundles else None
    total = sum(item[2] for item in bundles)
    cutoff = time.time() - max(1, days) * 86400
    removed = []
    for path, _, size, mtime in reversed(bundles):
        if path == newest_valid:
            continue
        if mtime < cutoff or total > byte_limit:
            with contextlib.suppress(OSError):
                if os.name != "nt":
                    for child in path.rglob("*"):
                        os.chmod(child, 0o700 if child.is_dir() else 0o600)
                    os.chmod(path, 0o700)
                shutil.rmtree(path)
                removed.append(path.name)
                total -= size
    return removed


def status_data():
    reconcile_interrupted()
    reconcile_stale_deployment()
    runs = list_runs(20)
    return {
        **settings(), "database": str(database_path()), "backup_root": str(backups_root()),
        "active": sum(1 for run in runs if run["phase"] in ACTIVE_PHASES),
        "deployed": sum(1 for run in runs if run["phase"] == "deployed"),
        "rollback_points": sum(1 for run in runs if run.get("backup_manifest")),
        "runs": [{k: run.get(k) for k in ("id", "objective", "phase", "risk", "approval_required", "updated_ts", "deployed_commit")} for run in runs],
    }


def format_status(data=None):
    data = data or status_data()
    lines = [
        "Trilobite self-modification",
        "  enabled: %s | mode: %s" % ("yes" if data["enabled"] else "no", data["mode"]),
        "  active: %d | deployed: %d | rollback points: %d" % (data["active"], data["deployed"], data["rollback_points"]),
        "  backup root: %s" % data["backup_root"],
    ]
    for run in data["runs"][:10]:
        lines.append("  %s  %-18s %-8s %s" % (run["id"], run["phase"], run["risk"], run["objective"][:80]))
    return "\n".join(lines)


def format_run(run_id):
    run = get_run(run_id)
    return "\n".join([
        "%s | phase=%s | mode=%s | risk=%s" % (run["id"], run["phase"], run["mode"], run["risk"]),
        "objective: %s" % run["objective"],
        "problem: %s" % run["problem"],
        "files: %s" % (", ".join(run["files"]) or "(none declared)"),
        "backup: %s" % (run.get("backup_manifest") or "not created"),
        "approval: %s" % (run.get("approved_by") or ("required" if run["approval_required"] else "host low-risk eligible")),
        "workspace: %s" % (run.get("workspace_path") or "not created"),
        "last error: %s" % (run.get("last_error") or "none"),
    ])


def parse_plan_text(text):
    """Parse `/selfmod plan objective --files a.py,b.py --tests cmd` safely."""
    text = str(text or "").strip()
    marker = " --files "
    if marker not in text:
        return text, [], []
    objective, tail = text.split(marker, 1)
    tests = []
    if " --tests " in tail:
        files_text, tests_text = tail.split(" --tests ", 1)
        tests = [item.strip() for item in tests_text.split(";;") if item.strip()]
    else:
        files_text = tail
    files = [item.strip() for item in files_text.split(",") if item.strip()]
    return objective.strip(), files, tests


def recursive_guard():
    if os.environ.get("TRILOBITE_SELFMOD_ACTIVE") == "1":
        raise RuntimeError("recursive self-improvement runs are forbidden")
