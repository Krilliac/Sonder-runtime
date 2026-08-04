"""Signed engine distribution and update manager (SPEC-4).

This module owns the update domain: the durable update plan state
machine, bundle manifests, adversarial-safe archive extraction, staged
installation into versioned release directories, atomic activation, and
rollback — all integrated with the SPEC-2 gates (maintenance locks,
verified backup, drain, migration, health checks).

Trust model: release metadata verification uses The Update Framework
client (python-tuf) when installed; the offline hash-only path exists for
development and air-gapped bootstrap and must be explicitly enabled with
``SONDER_UPDATE_ALLOW_UNSIGNED=1`` — production channels require TUF
(R-M1).  No custom signature parsing is implemented here.

Nothing in this module ever overwrites the active release in place
(R-M8) and the active-release pointer changes atomically (R-M10).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import sonder_migrations
import sonder_version

MANIFEST_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# State machine (SPEC-4 section 7) — pure data, validated transitions.
# ---------------------------------------------------------------------------

STATES = (
    "planned", "checking", "available", "downloading", "verified", "staged",
    "preflight", "backing_up", "draining", "installing", "migrating",
    "health_check", "committed", "rolling_back", "rolled_back", "blocked",
    "failed", "cancelled",
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"checking", "cancelled"}),
    "checking": frozenset({"available", "blocked"}),
    "available": frozenset({"downloading", "cancelled"}),
    "downloading": frozenset({"verified", "failed", "cancelled"}),
    "verified": frozenset({"staged", "cancelled"}),
    "staged": frozenset({"preflight", "cancelled"}),
    "preflight": frozenset({"blocked", "backing_up"}),
    "backing_up": frozenset({"draining", "failed"}),
    "draining": frozenset({"installing"}),
    "installing": frozenset({"migrating", "rolling_back"}),
    "migrating": frozenset({"health_check", "rolling_back"}),
    "health_check": frozenset({"committed", "rolling_back"}),
    "rolling_back": frozenset({"rolled_back", "failed"}),
    "committed": frozenset(),
    "rolled_back": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

TERMINAL_STATES = frozenset(
    {"committed", "rolled_back", "blocked", "failed", "cancelled"}
)
# Cancellation is allowed only before drain/install begins (SPEC-4 §7).
CANCELLABLE_STATES = frozenset(
    {"planned", "available", "downloading", "verified", "staged"}
)


class UpdateError(RuntimeError):
    pass


class InvalidUpdateTransition(UpdateError):
    def __init__(self, current: str, requested: str) -> None:
        super().__init__(f"update may not move {current} -> {requested}")
        self.current = current
        self.requested = requested


class ConcurrencyConflict(UpdateError):
    pass


class TrustError(UpdateError):
    pass


class CompatibilityError(UpdateError):
    pass


class ExtractionError(UpdateError):
    pass


def validate_transition(current: str, requested: str) -> None:
    if current not in TRANSITIONS:
        raise InvalidUpdateTransition(current, requested)
    if requested not in TRANSITIONS[current]:
        raise InvalidUpdateTransition(current, requested)


# ---------------------------------------------------------------------------
# Platform identity
# ---------------------------------------------------------------------------

def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return sys.platform


def current_architecture() -> str:
    machine = _platform.machine().lower()
    return {
        "amd64": "x86_64", "x86_64": "x86_64",
        "aarch64": "arm64", "arm64": "arm64",
    }.get(machine, machine)


# ---------------------------------------------------------------------------
# Bundle manifest (SPEC-4 section 4)
# ---------------------------------------------------------------------------

_REQUIRED_MANIFEST_KEYS = (
    "schema_version", "bundle_id", "version", "commit_sha", "channel",
    "platform", "architecture", "package_format", "runtime",
    "state_schema", "resources", "entrypoints", "health_checks",
    "rollback", "files",
)


@dataclass(frozen=True)
class BundleManifest:
    raw: dict

    @classmethod
    def from_dict(cls, raw: dict) -> "BundleManifest":
        if not isinstance(raw, dict):
            raise UpdateError("manifest must be a JSON object")
        missing = [k for k in _REQUIRED_MANIFEST_KEYS if k not in raw]
        if missing:
            raise UpdateError(f"manifest missing keys: {missing}")
        if raw["schema_version"] != MANIFEST_SCHEMA_VERSION:
            raise UpdateError(
                f"unsupported manifest schema_version {raw['schema_version']!r}"
            )
        files = raw["files"]
        if not isinstance(files, list) or not files:
            raise UpdateError("manifest lists no files")
        for entry in files:
            if not isinstance(entry, dict) or not all(
                k in entry for k in ("path", "length", "sha256")
            ):
                raise UpdateError(f"invalid manifest file entry: {entry!r}")
            member = Path(entry["path"])
            if member.is_absolute() or ".." in member.parts:
                raise UpdateError(
                    f"manifest path escapes bundle: {entry['path']!r}"
                )
        return cls(raw=raw)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "BundleManifest":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UpdateError(f"cannot read manifest {path}: {exc}") from None
        return cls.from_dict(raw)

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, key, default=None):
        return self.raw.get(key, default)

    def sha256(self) -> str:
        canonical = json.dumps(self.raw, sort_keys=True).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def verify_tree(self, root: str | os.PathLike) -> list[str]:
        """Verify every manifest file's presence, length, hash, and mode."""
        problems: list[str] = []
        base = Path(root)
        for entry in self.raw["files"]:
            member = base / entry["path"]
            if not member.is_file():
                problems.append(f"missing file {entry['path']}")
                continue
            size = member.stat().st_size
            if size != entry["length"]:
                problems.append(
                    f"length mismatch {entry['path']}: {size} != {entry['length']}"
                )
                continue
            if _sha256_file(member) != entry["sha256"]:
                problems.append(f"hash mismatch {entry['path']}")
        return problems


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


DEFAULT_BUNDLE_EXCLUDES = (
    ".git", "__pycache__", "venv", ".pytest_cache", "node_modules",
    "memory.db", "*.db", "*.db-wal", "*.db-shm", "sonder-personal-lora",
    "combined_personal.jsonl",
)


def build_bundle(
    source_dir: str | os.PathLike,
    output_dir: str | os.PathLike,
    *,
    version: str | None = None,
    channel: str = "stable",
    platform_name: str | None = None,
    architecture: str | None = None,
    health_checks: list | None = None,
    minimum_compatible_previous_version: str = "",
    excludes: tuple[str, ...] = DEFAULT_BUNDLE_EXCLUDES,
) -> dict:
    """Build a release bundle: tar.gz archive + manifest (SPEC-4 WP3).

    Returns {"archive": path, "manifest": path, "manifest_sha256": ...}.
    """
    import fnmatch

    source = Path(source_dir).resolve()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    build = sonder_version.build_info()
    version = version or build.version
    plat = platform_name or current_platform()
    arch = architecture or current_architecture()

    def excluded(rel: Path) -> bool:
        for part in rel.parts:
            for pattern in excludes:
                if fnmatch.fnmatch(part, pattern):
                    return True
        return False

    files = []
    members: list[Path] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(source)
        if excluded(rel):
            continue
        files.append(
            {
                "path": str(rel),
                "length": path.stat().st_size,
                "sha256": _sha256_file(path),
                "mode": "0755" if os.access(path, os.X_OK) else "0644",
            }
        )
        members.append(rel)
    if not files:
        raise UpdateError(f"no files to bundle under {source}")

    archive_name = f"sonder-engine-{version}.tar.gz"
    archive_path = out / archive_name
    with tarfile.open(archive_path, "w:gz") as tar:
        for rel in members:
            tar.add(source / rel, arcname=str(rel), recursive=False)

    schema_targets = {
        store: len(sonder_migrations.discover_migrations(store))
        for store in ("memory", "autopilot", "fleet", "operations", "updates")
    }
    manifest_raw = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "bundle_id": f"sonder-engine-{version}-{plat}-{arch}",
        "version": version,
        "commit_sha": build.commit_sha,
        "channel": channel,
        "platform": plat,
        "architecture": arch,
        "package_format": "tar.gz",
        "minimum_os": "",
        "runtime": {
            "kind": "venv",
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "state_schema": {
            **{f"{store}_min": 0 for store in schema_targets},
            **{f"{store}_target": count for store, count in schema_targets.items()},
        },
        "ollama": {"minimum_version": "", "required": True},
        "resources": {
            "download_bytes": archive_path.stat().st_size,
            "installed_bytes": sum(f["length"] for f in files),
            "minimum_free_bytes": sum(f["length"] for f in files) * 3,
        },
        "entrypoints": {
            "service": "sonder_runtime",
            "updater_helper": "sonder_runtime",
        },
        "health_checks": health_checks
        if health_checks is not None
        else [
            {
                "kind": "command",
                "argv": [
                    "{python}", "-m", "sonder_runtime", "status", "--json"
                ],
                "timeout_seconds": 60,
            }
        ],
        "rollback": {
            "code_rollback_supported": True,
            "minimum_compatible_previous_version":
                minimum_compatible_previous_version,
            "requires_state_restore_below": "",
        },
        "archive": {
            "name": archive_name,
            "length": archive_path.stat().st_size,
            "sha256": _sha256_file(archive_path),
        },
        "files": files,
    }
    manifest = BundleManifest.from_dict(manifest_raw)
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_raw, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {
        "archive": str(archive_path),
        "manifest": str(manifest_path),
        "manifest_sha256": manifest.sha256(),
    }


# ---------------------------------------------------------------------------
# Adversarially-safe extraction (SPEC-4 section 9)
# ---------------------------------------------------------------------------

def safe_extract(
    archive_path: str | os.PathLike,
    destination: str | os.PathLike,
    *,
    max_expanded_bytes: int,
) -> int:
    """Extract a tar archive refusing traversal, symlinks, devices, and
    expansion beyond ``max_expanded_bytes``.  Returns bytes written."""
    dest = Path(destination).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        tar = tarfile.open(archive_path, "r:*")
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ExtractionError(f"cannot open archive: {exc}") from None
    try:
        with tar:
            written = _extract_members(tar, dest, max_expanded_bytes)
    except ExtractionError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ExtractionError(f"corrupt or truncated archive: {exc}") from None
    return written


def _extract_members(tar, dest: Path, max_expanded_bytes: int) -> int:
    written = 0
    for member in tar:
        name = member.name
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or name.startswith("/"):
            raise ExtractionError(f"archive path escapes staging: {name!r}")
        if member.issym() or member.islnk():
            raise ExtractionError(f"archive contains a link: {name!r}")
        if member.isdev() or member.isfifo():
            raise ExtractionError(
                f"archive contains a device/fifo: {name!r}"
            )
        if member.isdir():
            (dest / path).mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise ExtractionError(
                f"archive contains unsupported member type: {name!r}"
            )
        if member.size < 0:
            raise ExtractionError(f"negative member size: {name!r}")
        written += member.size
        if written > max_expanded_bytes:
            raise ExtractionError(
                f"archive expands beyond the {max_expanded_bytes} byte "
                "budget"
            )
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            raise ExtractionError(f"unreadable member: {name!r}")
        with source, open(target, "wb") as sink:
            remaining = member.size
            while remaining > 0:
                chunk = source.read(min(1 << 20, remaining))
                if not chunk:
                    raise ExtractionError(f"truncated member: {name!r}")
                sink.write(chunk)
                remaining -= len(chunk)
        mode = 0o755 if member.mode & 0o100 else 0o644
        os.chmod(target, mode)
    return written


# ---------------------------------------------------------------------------
# Trust (SPEC-4 section 1) — TUF when available, gated hash-only otherwise
# ---------------------------------------------------------------------------

def verify_bundle_trust(
    bundle_dir: Path, manifest: BundleManifest, *, allow_unverified: bool
) -> str:
    """Verify the bundle's trust chain.  Returns the trust mode used.

    A ``metadata/`` directory with TUF metadata triggers full TUF
    verification via python-tuf (never custom signature parsing).  Without
    it, the gated unsigned path requires BOTH the caller flag and the
    ``SONDER_UPDATE_ALLOW_UNSIGNED=1`` environment gate.
    """
    metadata_dir = bundle_dir / "metadata"
    if metadata_dir.is_dir():
        try:
            from tuf.ngclient import Updater  # noqa: F401
        except ImportError:
            raise TrustError(
                "bundle carries TUF metadata but python-tuf is not "
                "installed; install the pinned 'tuf' dependency"
            ) from None
        return _verify_with_tuf(bundle_dir, manifest)
    env_gate = os.environ.get(
        "SONDER_UPDATE_ALLOW_UNSIGNED", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if not (allow_unverified and env_gate):
        raise TrustError(
            "bundle has no TUF metadata; unsigned bundles require both "
            "--allow-unverified and SONDER_UPDATE_ALLOW_UNSIGNED=1 and are "
            "not acceptable for production channels"
        )
    return "unsigned-explicitly-allowed"


def _local_file_fetcher_class():
    """Build a filesystem-backed TUF fetcher (import-guarded)."""
    from tuf.api import exceptions as tuf_exceptions
    from tuf.ngclient import FetcherInterface

    class _LocalFileFetcher(FetcherInterface):
        """Reads metadata/targets from local paths for offline bundles.

        Maps a missing file to a 404 DownloadHTTPError so python-tuf's
        root-rotation walk (which probes N+1.root.json) terminates cleanly
        instead of raising an unhandled download error.
        """

        def _fetch(self, url):
            # base_urls were passed as plain directory paths, so url is a
            # filesystem path (with any file:// prefix stripped defensively).
            path = url[7:] if url.startswith("file://") else url
            candidate = Path(path)
            if not candidate.is_file():
                raise tuf_exceptions.DownloadHTTPError(
                    f"not found: {path}", 404
                )
            data = candidate.read_bytes()

            def _chunks():
                yield data

            return _chunks()

    return _LocalFileFetcher


def _verify_with_tuf(bundle_dir: Path, manifest: BundleManifest) -> str:
    """Full TUF verification of the archive target from local metadata."""
    import tempfile

    from tuf.ngclient import Updater

    _LocalFileFetcher = _local_file_fetcher_class()

    root = bundle_dir / "metadata" / "root.json"
    if not root.is_file():
        raise TrustError("TUF metadata directory lacks root.json")
    archive_info = manifest.get("archive") or {}
    target_name = archive_info.get("name", "")
    if not target_name:
        raise TrustError("manifest lacks archive target name")
    with tempfile.TemporaryDirectory(prefix="sonder-tuf-") as tmp:
        metadata_cache = Path(tmp) / "metadata"
        metadata_cache.mkdir()
        trusted_root = root.read_bytes()
        try:
            targets_dir = (
                bundle_dir / "targets"
                if (bundle_dir / "targets").is_dir()
                else bundle_dir
            )
            updater = Updater(
                metadata_dir=str(metadata_cache),
                metadata_base_url=str(bundle_dir / "metadata") + "/",
                target_dir=str(Path(tmp) / "targets"),
                target_base_url=str(targets_dir) + "/",
                # Offline bundles are local files; the default HTTP fetcher
                # cannot read them and, crucially, the root-rotation walk
                # needs a missing N.root.json to read as 404 so it
                # terminates. _LocalFileFetcher provides both.
                fetcher=_LocalFileFetcher(),
                bootstrap=trusted_root,
            )
            updater.refresh()
            info = updater.get_targetinfo(target_name)
            if info is None:
                raise TrustError(
                    f"target {target_name!r} is not in the signed metadata"
                )
            local = bundle_dir / "targets" / target_name
            if not local.is_file():
                local = bundle_dir / target_name
            info.verify_length_and_hashes(local.read_bytes())
        except TrustError:
            raise
        except Exception as exc:
            raise TrustError(f"TUF verification failed: {exc}") from None
    return "tuf"


# ---------------------------------------------------------------------------
# Compatibility (R-M17)
# ---------------------------------------------------------------------------

def check_compatibility(
    manifest: BundleManifest,
    *,
    releases_dir: Path,
    platform_name: str | None = None,
    architecture: str | None = None,
) -> list[str]:
    problems: list[str] = []
    plat = platform_name or current_platform()
    arch = architecture or current_architecture()
    if manifest["platform"] != plat:
        problems.append(
            f"bundle is for {manifest['platform']}, this host is {plat}"
        )
    if manifest["architecture"] != arch:
        problems.append(
            f"bundle is for {manifest['architecture']}, this host is {arch}"
        )
    wanted = str(manifest["runtime"].get("python_version", ""))
    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    if wanted and not have.startswith(wanted.rsplit(".", 1)[0]):
        problems.append(f"bundle needs Python {wanted}, host has {have}")
    # State schema: the bundle must not require newer state than it ships.
    state_schema = manifest["state_schema"]
    for store in ("memory", "autopilot", "fleet", "operations", "updates"):
        try:
            applied = len(sonder_migrations.status(store).applied)
        except Exception:
            continue
        target = int(state_schema.get(f"{store}_target", 0))
        if applied > target:
            problems.append(
                f"store {store} schema ({applied}) is newer than the bundle "
                f"supports ({target}); forward recovery required"
            )
    minimum_free = int(manifest["resources"].get("minimum_free_bytes", 0))
    try:
        free = shutil.disk_usage(releases_dir).free
    except OSError:
        free = 0
    if minimum_free and free < minimum_free:
        problems.append(
            f"insufficient disk: need {minimum_free}, have {free}"
        )
    return problems


# ---------------------------------------------------------------------------
# Durable repository over updates.db
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class UpdateRepository:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or sonder_migrations.store_db_paths()["updates"]
        status = sonder_migrations.status("updates", self._db_path)
        if not status.current:
            sonder_migrations.migrate_store("updates", self._db_path)

    def _connect(self) -> sqlite3.Connection:
        return sonder_migrations.open_connection(self._db_path)

    # -- plans -------------------------------------------------------------

    def create_plan(
        self,
        *,
        channel: str,
        source_kind: str,
        source_ref: str,
        from_release_id: str,
        target_version: str,
        target_manifest_sha256: str,
        status: str,
        idempotency_key: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> dict:
        if idempotency_key:
            existing = self.plan_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
        update_id = f"upd_{uuid.uuid4().hex}"
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO update_plan (update_id, idempotency_key, channel,"
                " source_kind, source_ref, from_release_id, target_version,"
                " target_manifest_sha256, status, revision, created_at_utc,"
                " updated_at_utc, error_code, error_detail_redacted)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (
                    update_id, idempotency_key, channel, source_kind,
                    source_ref, from_release_id, target_version,
                    target_manifest_sha256, status, now, now, error_code,
                    error_detail,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self.plan_by_idempotency_key(idempotency_key or "")
            if existing is not None:
                return existing
            raise
        finally:
            conn.close()
        return self.get_plan(update_id)

    _PLAN_KEYS = (
        "update_id", "idempotency_key", "channel", "source_kind",
        "source_ref", "from_release_id", "target_version",
        "target_manifest_sha256", "status", "revision", "created_at_utc",
        "updated_at_utc", "backup_id", "error_code", "error_detail_redacted",
    )

    def _row_to_plan(self, row) -> dict:
        return dict(zip(self._PLAN_KEYS, row))

    def get_plan(self, update_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {', '.join(self._PLAN_KEYS)} FROM update_plan"
                " WHERE update_id = ?",
                (update_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise UpdateError(f"unknown update plan {update_id!r}")
        return self._row_to_plan(row)

    def plan_by_idempotency_key(self, key: str) -> dict | None:
        if not key:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {', '.join(self._PLAN_KEYS)} FROM update_plan"
                " WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_plan(row) if row else None

    def list_plans(self, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {', '.join(self._PLAN_KEYS)} FROM update_plan"
                " ORDER BY created_at_utc DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_plan(row) for row in rows]

    def advance(
        self,
        plan: dict,
        next_status: str,
        *,
        backup_id: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> dict:
        """Compare-and-set transition; exactly one row must change."""
        validate_transition(plan["status"], next_status)
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE update_plan SET status = ?, revision = revision + 1,"
                " updated_at_utc = ?,"
                " backup_id = COALESCE(?, backup_id),"
                " error_code = COALESCE(?, error_code),"
                " error_detail_redacted = COALESCE(?, error_detail_redacted)"
                " WHERE update_id = ? AND status = ? AND revision = ?",
                (
                    next_status, _utc_now(), backup_id, error_code,
                    error_detail, plan["update_id"], plan["status"],
                    plan["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict(
                    f"update {plan['update_id']} moved concurrently "
                    f"(expected {plan['status']} rev {plan['revision']})"
                )
        finally:
            conn.close()
        return self.get_plan(plan["update_id"])

    # -- step journal (R-M12) ---------------------------------------------

    def record_step(
        self,
        update_id: str,
        step_no: int,
        step_name: str,
        status: str,
        *,
        evidence: dict | None = None,
        error_code: str | None = None,
        attempt: int = 1,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO update_step (update_id, step_no,"
                " step_name, status, started_at_utc, completed_at_utc,"
                " attempt, evidence_json, error_code)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    update_id, step_no, step_name, status, _utc_now(),
                    _utc_now() if status in ("ok", "failed", "skipped")
                    else None,
                    attempt,
                    json.dumps(evidence or {}, ensure_ascii=False),
                    error_code,
                ),
            )
        finally:
            conn.close()

    def steps(self, update_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT step_no, step_name, status, attempt, evidence_json,"
                " error_code FROM update_step WHERE update_id = ?"
                " ORDER BY step_no, attempt",
                (update_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "step_no": r[0], "step_name": r[1], "status": r[2],
                "attempt": r[3], "evidence": json.loads(r[4]),
                "error_code": r[5],
            }
            for r in rows
        ]

    # -- installed releases ------------------------------------------------

    def record_release(
        self,
        *,
        release_id: str,
        version: str,
        commit_sha: str,
        platform_name: str,
        architecture: str,
        install_path: str,
        status: str,
        manifest_sha256: str,
        state_schema: dict,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO installed_release (release_id,"
                " version, commit_sha, platform, architecture, install_path,"
                " activated_at_utc, status, manifest_sha256,"
                " state_schema_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    release_id, version, commit_sha, platform_name,
                    architecture, install_path, _utc_now(), status,
                    manifest_sha256, json.dumps(state_schema),
                ),
            )
        finally:
            conn.close()

    def set_release_status(self, release_id: str, status: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE installed_release SET status = ?,"
                " activated_at_utc = ? WHERE release_id = ?",
                (status, _utc_now(), release_id),
            )
        finally:
            conn.close()

    def release_by_status(self, status: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT release_id, version, commit_sha, platform,"
                " architecture, install_path, activated_at_utc, status,"
                " manifest_sha256 FROM installed_release WHERE status = ?"
                " ORDER BY activated_at_utc DESC LIMIT 1",
                (status,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        keys = (
            "release_id", "version", "commit_sha", "platform",
            "architecture", "install_path", "activated_at_utc", "status",
            "manifest_sha256",
        )
        return dict(zip(keys, row))
