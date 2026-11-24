"""Tamper-evident backup bundles, rollback verification, restoration, and pruning."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager

from sonder_runtime.application.selfmod.rollback_scope import rollback_manifest
from sonder_runtime.application.selfmod.stage_refusal import (
    SelfmodStageNotApplied,
    require_atomic_checkout_promotion,
)


ROLLBACK_RECEIPT_PREFIX = "SELFMOD-ROLLBACK-RECEIPT"


def sha256_file(path: Path) -> str:
    """Compute streaming SHA-256 hex digest of *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy(source: Path, target: Path, mode: int | None = None) -> None:
    """Atomically copy *source* onto *target* with fsync and optional POSIX mode."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=target.name + ".restore-", dir=target.parent
    )
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


def remove_bytecode_cache(target: Path) -> None:
    """Remove stale ``__pycache__`` entries corresponding to Python file *target*."""
    if target.suffix != ".py":
        return
    cache = target.parent / "__pycache__"
    if not cache.is_dir():
        return
    for path in cache.glob(target.stem + ".*.py[co]"):
        with contextlib.suppress(OSError):
            path.unlink()


@dataclass(frozen=True)
class BackupHooks:
    """Injectable host callbacks for :func:`create_backup`."""

    get_run: Callable[[str], dict[str, Any]]
    backup_dir: Callable[[str], Path]
    tx: Callable[..., ContextManager[sqlite3.Connection]]
    phase: Callable[..., dict[str, Any]]
    verify_backup: Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class RollbackVerifyHooks:
    """Injectable host callbacks for :func:`verify_rollback_ready`."""

    verify_backup: Callable[[str], dict[str, Any]]
    deployed_file_mismatches: Callable[[str], list[str]]
    restore_manifest_files: Callable[[Mapping[str, Any]], None]
    emergency_restore: Callable[[Path], str | Path]


@dataclass(frozen=True)
class RestoreHooks:
    """Injectable host callbacks for :func:`restore_run`."""

    get_run: Callable[[str], dict[str, Any]]
    verify_backup: Callable[[str], dict[str, Any]]
    manifest_live_mismatches: Callable[[Mapping[str, Any]], list[str]]
    restore_manifest_files: Callable[[Mapping[str, Any]], None]
    connect: Callable[[], sqlite3.Connection]
    tx: Callable[..., ContextManager[sqlite3.Connection]]
    event: Callable[[sqlite3.Connection, str | None, str, str], None]
    tested_digests: Callable[[str], dict[str, Any] | None]
    git_info: Callable[[Path], tuple[bool, str | None, str]]
    git: Callable[..., tuple[int, str]]
    phase: Callable[..., dict[str, Any]]


def create_backup(run_id: str, *, hooks: BackupHooks) -> dict[str, Any]:
    """Create, seal, persist, and verify a tamper-evident backup bundle."""
    run = hooks.get_run(run_id)
    if run["phase"] != "proposed":
        raise RuntimeError("backup requires proposed phase")
    root = Path(run["repository_root"])
    bundle = hooks.backup_dir(run_id)
    if bundle.exists():
        raise RuntimeError("backup bundle already exists")
    files_dir = bundle / "files"
    files_dir.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    try:
        for rel in run["files"]:
            source = root / rel
            existed = source.is_file()
            destination = files_dir / rel
            record: dict[str, Any] = {
                "path": rel,
                "existed_before": existed,
                "sha256_before": sha256_file(source) if existed else None,
                "size_before": source.stat().st_size if existed else 0,
                "mode_before": stat.S_IMODE(source.stat().st_mode) if existed else None,
                "backup_path": str(destination) if existed else None,
                "sha256_backup": None,
            }
            if existed:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if os.name != "nt":
                    with contextlib.suppress(OSError):
                        os.chmod(destination, 0o600)
                record["sha256_backup"] = sha256_file(destination)
                if record["sha256_backup"] != record["sha256_before"]:
                    raise RuntimeError("backup hash mismatch for %s" % rel)
            records.append(record)
        manifest = {
            "run_id": run_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "repository_root": str(root),
            "starting_commit": run["starting_commit"],
            "files": records,
        }
        manifest_path = bundle / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest_hash = sha256_file(manifest_path)
        (bundle / "manifest.sha256").write_text(manifest_hash + "\n", encoding="ascii")
        if os.name != "nt":
            for path in sorted(bundle.rglob("*"), reverse=True):
                with contextlib.suppress(OSError):
                    os.chmod(path, 0o500 if path.is_dir() else 0o400)
            os.chmod(bundle, 0o500)
    except Exception:
        shutil.rmtree(bundle, ignore_errors=True)
        raise
    with hooks.tx() as conn:
        for record in records:
            conn.execute(
                "INSERT INTO selfmod_backups VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    record["path"],
                    int(record["existed_before"]),
                    record["sha256_before"],
                    record["size_before"],
                    record["mode_before"],
                    record["backup_path"],
                    record["sha256_backup"],
                ),
            )
    updated = hooks.phase(
        run_id,
        {"proposed"},
        "backed_up",
        "backup",
        "tamper-evident backup verified",
        backup_manifest=str(manifest_path),
    )
    hooks.verify_backup(run_id)
    return updated


def load_manifest(
    run_id: str, *, backup_dir_fn: Callable[[str], Path]
) -> dict[str, Any]:
    """Load and checksum-verify a run's backup manifest."""
    bundle = backup_dir_fn(run_id)
    manifest_path = bundle / "manifest.json"
    checksum_path = bundle / "manifest.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise RuntimeError("backup manifest/checksum missing")
    expected = checksum_path.read_text(encoding="ascii").strip()
    if not expected or sha256_file(manifest_path) != expected:
        raise RuntimeError("backup manifest is corrupted")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError("backup manifest JSON is corrupted") from exc
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise RuntimeError("backup run identity mismatch")
    return manifest


def verify_backup(
    run_id: str,
    *,
    load_manifest_fn: Callable[[str], dict[str, Any]],
    tx_fn: Callable[..., ContextManager[sqlite3.Connection]],
    event_fn: Callable[[sqlite3.Connection, str | None, str, str], None],
) -> dict[str, Any]:
    """Verify every file record in a run's backup manifest against disk."""
    manifest = load_manifest_fn(run_id)
    for record in manifest.get("files", []):
        if not record.get("existed_before"):
            continue
        path = Path(record.get("backup_path") or "")
        if (
            not path.is_file()
            or sha256_file(path) != record.get("sha256_backup")
            or record.get("sha256_backup") != record.get("sha256_before")
        ):
            raise RuntimeError("backup verification failed for %s" % record.get("path"))
    with tx_fn() as conn:
        event_fn(
            conn,
            run_id,
            "backup_verified",
            "%d file records verified" % len(manifest.get("files", [])),
        )
    return manifest


def backup_rehearsal(
    run_id: str, *, verify_backup_fn: Callable[[str], dict[str, Any]]
) -> bool:
    """Dry-run backup restoration into a temporary directory and verify hashes."""
    manifest = verify_backup_fn(run_id)
    with tempfile.TemporaryDirectory(prefix="sonder-selfmod-rehearse-") as temp:
        root = Path(temp)
        for record in manifest["files"]:
            target = root / record["path"]
            if record["existed_before"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(record["backup_path"], target)
                if sha256_file(target) != record["sha256_before"]:
                    return False
            elif target.exists():
                target.unlink()
    return True


def manifest_live_mismatches(manifest: Mapping[str, Any]) -> list[str]:
    """Return backed-up paths whose live bytes or existence no longer match."""
    root = Path(manifest["repository_root"])
    mismatches: list[str] = []
    for record in manifest["files"]:
        target = root / record["path"]
        if record["existed_before"]:
            if not target.is_file() or sha256_file(target) != record["sha256_before"]:
                mismatches.append(record["path"])
        elif target.exists():
            mismatches.append(record["path"])
    return mismatches


def restore_manifest_files(
    manifest: Mapping[str, Any],
    *,
    atomic_copy_fn: Callable[[Path, Path, int | None], None] = atomic_copy,
    remove_bytecode_cache_fn: Callable[[Path], None] = remove_bytecode_cache,
) -> None:
    """Restore only the manifest's preflight-verified, scoped files."""
    root = Path(manifest["repository_root"])
    for record in manifest["files"]:
        target = root / record["path"]
        if record["existed_before"]:
            atomic_copy_fn(
                Path(record["backup_path"]), target, record.get("mode_before")
            )
            remove_bytecode_cache_fn(target)
            if sha256_file(target) != record["sha256_before"]:
                raise RuntimeError("restored hash mismatch for %s" % record["path"])
        elif target.exists():
            target.unlink()
            remove_bytecode_cache_fn(target)


def record_deployed_files(
    run_id: str,
    root: str | Path,
    paths: Sequence[str],
    *,
    tx_fn: Callable[..., ContextManager[sqlite3.Connection]],
    event_fn: Callable[[sqlite3.Connection, str | None, str, str], None],
) -> list[tuple[str, bool, str | None, int | None]]:
    """Record exact post-deploy SHA-256 digests and modes for conflict detection."""
    records: list[tuple[str, bool, str | None, int | None]] = []
    for rel in paths:
        target = Path(root) / rel
        existed = target.is_file()
        mode = target.stat().st_mode & 0o777 if target.exists() else None
        records.append((rel, existed, sha256_file(target) if existed else None, mode))
    with tx_fn() as conn:
        conn.execute("DELETE FROM selfmod_deployed_files WHERE run_id=?", (run_id,))
        conn.executemany(
            "INSERT INTO selfmod_deployed_files(run_id,path,existed_after,sha256_after,mode_after,recorded_ts) VALUES(?,?,?,?,?,?)",
            [
                (run_id, rel, int(existed), digest, mode, time.time())
                for rel, existed, digest, mode in records
            ],
        )
        event_fn(conn, run_id, "deployed_hashes", "%d deployed paths recorded" % len(records))
    return records


def deployed_file_mismatches(
    run_id: str,
    *,
    get_run_fn: Callable[[str], dict[str, Any]],
    connect_fn: Callable[[], sqlite3.Connection],
) -> list[str]:
    """Return deployed paths whose current on-disk state diverged since deploy."""
    run = get_run_fn(run_id)
    root = Path(run["repository_root"])
    with connect_fn() as conn:
        rows = conn.execute(
            "SELECT path,existed_after,sha256_after,mode_after FROM selfmod_deployed_files WHERE run_id=? ORDER BY path",
            (run_id,),
        ).fetchall()
    if not rows:
        return ["deployed state was not recorded"]
    mismatches: list[str] = []
    for row in rows:
        target = root / row["path"]
        existed = bool(row["existed_after"])
        if existed:
            if not target.is_file() or sha256_file(target) != row["sha256_after"]:
                mismatches.append(row["path"])
            elif (
                os.name != "nt"
                and row["mode_after"] is not None
                and target.stat().st_mode & 0o777 != int(row["mode_after"])
            ):
                mismatches.append(row["path"])
        elif target.exists():
            mismatches.append(row["path"])
    return mismatches


def receipt_digest(pairs: Iterable[tuple[str, str]]) -> str:
    """Compute canonical SHA-256 over ``(path, content_sha)`` pairs."""
    digest = hashlib.sha256()
    for path, content_sha in sorted(pairs):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((content_sha or "-").encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def expected_rollback_receipt(
    run_id: str, *, load_manifest_fn: Callable[[str], dict[str, Any]]
) -> tuple[str, int]:
    """Derive the expected rollback receipt digest and file count from the manifest."""
    manifest = load_manifest_fn(run_id)
    records = manifest.get("files") or []
    if not records:
        raise RuntimeError(
            "backup manifest records no files; a rollback would restore nothing"
        )
    pairs = [
        (
            str(record["path"]),
            str(record["sha256_before"]) if record["existed_before"] else "",
        )
        for record in records
    ]
    return receipt_digest(pairs), len(records)


def probe_digest(
    root: str | Path, records: Sequence[Mapping[str, Any]]
) -> str:
    """Digest the bytes a dry-run rollback actually put on disk."""
    pairs: list[tuple[str, str]] = []
    for record in records:
        rel = str(record["path"])
        target = Path(root) / rel
        if record["existed_before"]:
            if not target.is_file():
                raise RuntimeError("rollback did not restore %s" % rel)
            pairs.append((rel, sha256_file(target)))
        else:
            if target.exists():
                raise RuntimeError("rollback did not remove %s" % rel)
            pairs.append((rel, ""))
    return receipt_digest(pairs)


def verify_rollback_ready(run_id: str, *, hooks: RollbackVerifyHooks) -> str:
    """Dry-run both in-tree and emergency rollback routes into a scratch directory."""
    manifest = hooks.verify_backup(run_id)
    records = manifest.get("files") or []
    if not records:
        raise RuntimeError(
            "backup manifest records no files; a rollback would restore nothing"
        )
    stale = hooks.deployed_file_mismatches(run_id)
    if stale:
        raise RuntimeError(
            "rollback would refuse before it started; deployed bytes already diverge: %s"
            % ", ".join(stale)
        )
    with tempfile.TemporaryDirectory(prefix="sonder-selfmod-rollback-probe-") as temp:
        scratch = Path(temp)
        intree = scratch / "intree"
        intree.mkdir()
        hooks.restore_manifest_files(dict(manifest, repository_root=str(intree)))
        intree_digest = probe_digest(intree, records)

        emergency_root = scratch / "emergency"
        emergency_root.mkdir()
        bundle = scratch / "bundle"
        bundle.mkdir()
        probe_records: list[dict[str, Any]] = []
        for record in records:
            probe_record = dict(record)
            if record["existed_before"]:
                backup = bundle / "files" / record["path"]
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(record["backup_path"], backup)
                probe_record["backup_path"] = str(backup)
            probe_records.append(probe_record)
        manifest_path = bundle / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                dict(manifest, repository_root=str(emergency_root), files=probe_records),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (bundle / "manifest.sha256").write_text(
            sha256_file(manifest_path) + "\n", encoding="ascii"
        )
        restored_root = hooks.emergency_restore(manifest_path)
        if Path(restored_root).resolve() != emergency_root.resolve():
            raise RuntimeError(
                "emergency recovery restored into %s, not the probe's scratch tree"
                % restored_root
            )
        emergency_digest = probe_digest(emergency_root, records)

    if intree_digest != emergency_digest:
        raise RuntimeError(
            "the two rollback routes disagree: in-tree %s vs emergency %s"
            % (intree_digest, emergency_digest)
        )
    receipt = "%s intree=%s recover=%s files=%d" % (
        ROLLBACK_RECEIPT_PREFIX, intree_digest, emergency_digest, len(records),
    )
    print(receipt)
    return receipt


def rollback_probe_command(
    run_id: str,
    *,
    state_root_fn: Callable[[], Path],
    database_path_fn: Callable[[], Path],
) -> list[str]:
    """Build the child-process command that runs ``selfmod.verify_rollback_ready``."""
    fallback = [entry for entry in sys.path if entry]
    probe_state = {
        "SONDER_SELFMOD_HOME": str(state_root_fn().resolve()),
        "SONDER_SELFMOD_DB": str(database_path_fn().resolve()),
    }
    return [
        sys.executable,
        "-c",
        "import os, sys; os.environ.update(%r); "
        "sys.path[:0] = ['.']; sys.path.extend(%r); import selfmod; "
        "selfmod.verify_rollback_ready(%r)" % (probe_state, fallback, str(run_id)),
    ]


def rollback_probe_detail(code: int, output: str, expected_receipt: str) -> str:
    """Format an operator-readable failure reason for a failed rollback probe."""
    tail = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if code:
        return tail[-1] if tail else "the probe exited %s with no output" % code
    return (
        "the probe exited 0 but did not report a matching rollback receipt; "
        "expected %r" % expected_receipt
    )


def verify_deployed_rollback(
    run_id: str,
    root: Path,
    timeout: int,
    *,
    probe_command_fn: Callable[[str], list[str]],
    expected_receipt_fn: Callable[[str], tuple[str, int]],
    run_fn: Callable[[Sequence[str], Path, int], tuple[int, str, int]],
) -> tuple[bool, str, list[str], int, str, int]:
    """Execute the deployed rollback probe and compare its output to the expected receipt."""
    probe: list[str] = []
    try:
        probe = probe_command_fn(run_id)
        digest, count = expected_receipt_fn(run_id)
        expected = "%s intree=%s recover=%s files=%d" % (
            ROLLBACK_RECEIPT_PREFIX, digest, digest, count,
        )
        code, output, duration = run_fn(probe, root, timeout)
    except Exception as exc:
        return (
            False,
            "rollback verification could not run: %s: %s" % (type(exc).__name__, exc),
            probe,
            1,
            str(exc),
            0,
        )
    ok = code == 0 and expected in (output or "")
    detail = "" if ok else rollback_probe_detail(code, output, expected)
    return ok, detail, probe, code, output, duration


def digest_mismatches(
    workspace: Path,
    changed_files: Iterable[object],
    expected_digests: Mapping[str, Any],
) -> list[str]:
    """Return changed files whose bytes differ from ``expected_digests``."""
    expected = {str(key): value for key, value in dict(expected_digests).items()}
    names = {str(item) for item in changed_files} | set(expected)
    mismatched: list[str] = []
    for rel in sorted(names):
        path = Path(workspace) / rel
        actual = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        if rel not in expected or actual != expected[rel]:
            mismatched.append(rel)
    return mismatched


@dataclass(frozen=True)
class DeployHooks:
    """Injectable host callbacks for :func:`deploy_run`."""

    get_run: Callable[[str], dict[str, Any]]
    deployment_lock: Callable[[str], ContextManager[str]]
    verify_backup: Callable[[str], dict[str, Any]]
    current_source_matches: Callable[[Mapping[str, Any]], tuple[bool, str]]
    inspect_diff: Callable[[str], dict[str, Any]]
    renew_deployment_lock: Callable[[str], None]
    candidate_path: Callable[[str], Path]
    tested_digests: Callable[[str], dict[str, Any] | None]
    atomic_copy: Callable[[Path, Path, int | None], None]
    remove_bytecode_cache: Callable[[Path], None]
    git_info: Callable[[Path], tuple[bool, str | None, str]]
    git: Callable[..., tuple[int, str]]
    tx: Callable[..., ContextManager[sqlite3.Connection]]
    event: Callable[[sqlite3.Connection, str | None, str, str], None]
    record_deployed_files: Callable[[str, Path, Sequence[str]], Any]
    phase: Callable[..., dict[str, Any]]
    verify_deployed_rollback: Callable[[str, Path, int], tuple[bool, str, list[str], int, str, int]]
    run_cmd: Callable[[Sequence[str], Path, int], tuple[int, str, int]]
    restore: Callable[..., dict[str, Any]]


def deploy_run(
    run_id: str,
    *,
    health_command: Sequence[str] | None = None,
    commit: bool = True,
    expected_digests: Mapping[str, Any] | None = None,
    hooks: DeployHooks,
) -> dict[str, Any]:
    """Install an approved single-file candidate with unconditional rollback verification."""
    run = hooks.get_run(run_id)
    if run["phase"] != "approved":
        raise SelfmodStageNotApplied("deployment requires explicit/host approval")
    with hooks.deployment_lock(run_id) as deployment_owner:
        try:
            if hooks.get_run(run_id)["phase"] != "approved":
                raise SelfmodStageNotApplied("run left the approved phase before deployment")
            hooks.verify_backup(run_id)
            ok, conflict = hooks.current_source_matches(run)
            if not ok:
                raise SelfmodStageNotApplied(conflict)
            diff = hooks.inspect_diff(run_id)
            require_atomic_checkout_promotion(diff["changed_files"])
            hooks.renew_deployment_lock(deployment_owner)
            if set(diff["changed_files"]) - set(run["files"]):
                raise SelfmodStageNotApplied("candidate diff no longer matches approved scope")
            root, workspace = Path(run["repository_root"]), hooks.candidate_path(run_id)
            tested = hooks.tested_digests(run_id)
            if tested is None:
                raise SelfmodStageNotApplied(
                    "deployment requires a tested-bytes record; run was never bound"
                )
            if expected_digests is not None and dict(expected_digests) != tested["files"]:
                raise SelfmodStageNotApplied(
                    "caller digests do not match the tested-bytes record"
                )
            bound_digests = tested["files"]
            mismatched = digest_mismatches(workspace, diff["changed_files"], bound_digests)
            if mismatched:
                raise SelfmodStageNotApplied(
                    "candidate bytes differ from tested bytes: %s" % ", ".join(mismatched)
                )
        except SelfmodStageNotApplied:
            raise
        except RuntimeError as exc:
            raise SelfmodStageNotApplied(str(exc)) from exc
        try:
            for rel in diff["changed_files"]:
                source, target = workspace / rel, root / rel
                if source.is_file():
                    mode = (
                        target.stat().st_mode & 0o777
                        if target.exists()
                        else source.stat().st_mode & 0o777
                    )
                    hooks.atomic_copy(source, target, mode)
                    hooks.remove_bytecode_cache(target)
                elif target.exists():
                    target.unlink()
                    hooks.remove_bytecode_cache(target)
            mismatched = digest_mismatches(root, diff["changed_files"], bound_digests)
            if mismatched:
                raise RuntimeError(
                    "installed bytes differ from tested bytes: %s" % ", ".join(mismatched)
                )
            deployed_commit = ""
            git_mode, _, _ = hooks.git_info(root)
            if git_mode and commit and not str(run.get("git_status_start") or "").strip():
                with hooks.tx() as conn:
                    conn.execute(
                        "UPDATE selfmod_runs SET last_error=?,updated_ts=? WHERE id=?",
                        ("deployment Git commit in progress", time.time(), run_id),
                    )
                    hooks.event(
                        conn, run_id, "deploy_commit_start", "recording isolated selfmod commit"
                    )
                code, output = hooks.git(root, "add", "--", *diff["changed_files"])
                if not code:
                    code, output = hooks.git(
                        root,
                        "commit",
                        "-m",
                        "selfmod: %s" % run["objective"][:100],
                        "--only",
                        "--",
                        *diff["changed_files"],
                    )
                if code:
                    hooks.git(root, "reset", "-q", "--", *diff["changed_files"])
                    raise RuntimeError(
                        "could not stage or commit self-improvement: %s" % output
                    )
                _, deployed_commit = hooks.git(root, "rev-parse", "HEAD")
                deployed_commit = deployed_commit.strip()
                with hooks.tx() as conn:
                    conn.execute(
                        "UPDATE selfmod_runs SET deployed_commit=?,last_error='',updated_ts=? WHERE id=?",
                        (deployed_commit, time.time(), run_id),
                    )
                    hooks.event(
                        conn, run_id, "deploy_commit", "Git commit persisted: %s" % deployed_commit
                    )
            hooks.record_deployed_files(run_id, root, diff["changed_files"])
            hooks.phase(
                run_id,
                {"approved"},
                "deployed",
                "deploy",
                "candidate deployed atomically",
                deployed_commit=deployed_commit,
                deployed_ts=time.time(),
            )
            hooks.renew_deployment_lock(deployment_owner)
            budget = min(120, int(run["budgets"]["max_test_seconds"]))
            ok, detail, probe, code, output, duration = hooks.verify_deployed_rollback(
                run_id, root, budget
            )
            with hooks.tx() as conn:
                conn.execute(
                    "INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        "post_deploy_rollback",
                        json.dumps(list(probe), ensure_ascii=False, sort_keys=True),
                        code,
                        duration,
                        output,
                        int(ok),
                        time.time(),
                    ),
                )
                hooks.event(
                    conn,
                    run_id,
                    "rollback_check",
                    "post-deploy rollback verification exit=%s passed=%s" % (code, ok),
                )
            if not ok:
                hooks.phase(
                    run_id,
                    {"deployed"},
                    "rollback_requested",
                    "rollback",
                    "deployed code cannot perform a rollback",
                )
                hooks.restore(run_id)
                raise RuntimeError(
                    "deployed code cannot perform a rollback; automatic rollback completed: %s"
                    % detail
                )
            if health_command:
                hooks.renew_deployment_lock(deployment_owner)
                code, output, duration = hooks.run_cmd(
                    health_command, root, min(120, int(run["budgets"]["max_test_seconds"]))
                )
                with hooks.tx() as conn:
                    conn.execute(
                        "INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            "post_deploy_health",
                            json.dumps(list(health_command), ensure_ascii=False, sort_keys=True),
                            code,
                            duration,
                            output,
                            int(code == 0),
                            time.time(),
                        ),
                    )
                    hooks.event(conn, run_id, "health", "post-deploy exit=%s" % code)
                if code:
                    hooks.phase(
                        run_id,
                        {"deployed"},
                        "rollback_requested",
                        "rollback",
                        "post-deployment health check failed",
                    )
                    hooks.restore(run_id)
                    raise RuntimeError(
                        "deployment health check failed; automatic rollback completed"
                    )
            return hooks.get_run(run_id)
        except Exception:
            current = hooks.get_run(run_id)
            if current["phase"] in {"approved", "deployed"}:
                hooks.phase(
                    run_id,
                    {current["phase"]},
                    "rollback_requested",
                    "deploy_failure",
                    "deployment failed; rollback requested",
                )
                hooks.restore(run_id)
            raise


def restore_run(
    run_id: str,
    *,
    from_candidate_only: bool = False,
    hooks: RestoreHooks,
) -> dict[str, Any]:
    """Restore backed-up live files for *run_id* and record rollback Git commit if needed."""
    run = hooks.get_run(run_id)
    if from_candidate_only and run["phase"] in {
        "rejected", "reviewing", "testing", "editing", "interrupted",
    }:
        manifest = hooks.verify_backup(run_id)
        mismatches = hooks.manifest_live_mismatches(manifest)
        if mismatches:
            with hooks.tx() as conn:
                hooks.event(
                    conn,
                    run_id,
                    "live_source_recovery",
                    "restoring unexpected live changes: %s" % ", ".join(mismatches),
                )
            hooks.restore_manifest_files(manifest)
            remaining = hooks.manifest_live_mismatches(manifest)
            if remaining:
                raise RuntimeError(
                    "unexpected live source changes could not be restored: %s"
                    % ", ".join(remaining)
                )
            details = "candidate rejected; unexpected live source changes restored"
        else:
            details = "candidate rejected; live source unchanged"
        return hooks.phase(
            run_id, {run["phase"]}, "restored", "restore", details,
            restored_ts=time.time(),
        )
    if run["phase"] not in {"rollback_requested", "deployed", "approved"}:
        raise RuntimeError("restore is not valid from %s" % run["phase"])
    manifest = hooks.verify_backup(run_id)
    with hooks.connect() as conn:
        paths = [
            row[0]
            for row in conn.execute(
                "SELECT path FROM selfmod_deployed_files WHERE run_id=?", (run_id,)
            )
        ]
    scoped_paths = paths or list((hooks.tested_digests(run_id) or {}).get("files", {}))
    manifest = rollback_manifest(manifest, scoped_paths)
    root = Path(manifest["repository_root"])
    hooks.restore_manifest_files(manifest)
    if not str(run.get("git_status_start") or "").strip():
        git_mode, current_commit, _ = hooks.git_info(root)
        deployment_commit_present = bool(run.get("deployed_commit")) or (
            bool(current_commit) and current_commit != run.get("starting_commit")
        )
        if git_mode and deployment_commit_present:
            manifest_paths = [record["path"] for record in manifest["files"]]
            code, output = hooks.git(root, "add", "--", *manifest_paths)
            if code:
                raise RuntimeError(
                    "restored files but could not stage rollback commit: %s" % output
                )
            code, output = hooks.git(
                root,
                "commit",
                "-m",
                "selfmod rollback: %s" % run["objective"][:90],
                "--only",
                "--",
                *manifest_paths,
            )
            if code:
                raise RuntimeError(
                    "restored files but could not record rollback commit: %s" % output
                )
    return hooks.phase(
        run_id,
        {run["phase"]},
        "restored",
        "restore",
        "exact backup hashes restored",
        restored_ts=time.time(),
    )


def prune_backups(
    retention_days: int | None = None,
    retention_bytes: int | None = None,
    *,
    settings_fn: Callable[[], Mapping[str, Any]],
    backups_root_fn: Callable[[], Path],
    load_manifest_fn: Callable[[str], dict[str, Any]],
    connect_fn: Callable[[], sqlite3.Connection],
    active_phases: frozenset[str] | set[str],
) -> list[str]:
    """Prune expired backup bundles while preserving active/deployed/newest rollback points."""
    config = settings_fn()
    days = int(retention_days if retention_days is not None else config["retention_days"])
    byte_limit = int(
        retention_bytes if retention_bytes is not None else config["retention_bytes"]
    )
    bundles: list[tuple[Path, dict[str, Any], int, float]] = []
    root = backups_root_fn()
    for path in root.glob("selfmod-*") if root.exists() else []:
        try:
            manifest = load_manifest_fn(path.name)
            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            bundles.append((path, manifest, size, path.stat().st_mtime))
        except Exception:
            continue
    bundles.sort(key=lambda item: item[3], reverse=True)
    newest_valid = bundles[0][0] if bundles else None
    total = sum(item[2] for item in bundles)
    cutoff = time.time() - max(1, days) * 86400
    protected_phases = set(active_phases) | {"deployed", "interrupted"}
    with connect_fn() as conn:
        keep = {
            r["id"]
            for r in conn.execute("SELECT id,phase FROM selfmod_runs")
            if r["phase"] in protected_phases
        }
    removed: list[str] = []
    for path, _, size, mtime in reversed(bundles):
        if path == newest_valid or path.name in keep:
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
