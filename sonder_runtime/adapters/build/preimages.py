"""``PreimageStore`` on disk: the originals a build fix may have to give back (F17).

Layout under the store root (``state/build-fix`` by default)::

    <job_id>/            0700
      manifest.json      0600  job, principal, project root, status, per-file digests
      blobs/<sha>.txt    0600  one original per edited file, named by its content digest

The loop saves a file's original before its first write and records every
digest it writes afterwards. ``load`` re-hashes the blob and refuses a
mismatch, so a tampered or torn pre-image is never written back. Writes are
atomic (temp file + ``os.replace``), no path is followed through a link, and
everything is bounded: at most 16 files of 2 MiB per job and 64 retained
jobs (oldest finished jobs are pruned first; unfinished ones are kept).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from ...application.build.fix_ports import PreimageEntry, PreimageIntegrityError
from ...domain.build.model import safe_rel
from ...domain.common.errors import InvalidInput, NotFound
from ...platform.private_files import ensure_private_dir

JOB_ID_RE = re.compile(r"^build-fix-[0-9a-f]{16,32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FILES_PER_JOB = 16
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_RETAINED_JOBS = 64
FINISHED = frozenset({"fixed", "improved", "unchanged", "aborted", "restored", "failed"})
_MANIFEST = "manifest.json"
_BLOBS = "blobs"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FilePreimageStore:
    def __init__(self, root: str | os.PathLike[str], *, label_prefix: str = "state/build-fix",
                 max_jobs: int = MAX_RETAINED_JOBS, clock=time.time) -> None:
        self._root = Path(root)
        self._label_prefix = label_prefix.rstrip("/")
        self._max_jobs = int(max_jobs)
        self._clock = clock
        self._lock = threading.RLock()

    # -- paths -------------------------------------------------------------------

    def _job_dir(self, job_id: str, *, create: bool = False) -> Path:
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise InvalidInput("invalid build fix job id")
        path = self._root / job_id
        if create:
            ensure_private_dir(self._root)
            ensure_private_dir(path)
            ensure_private_dir(path / _BLOBS)
        self._require_dir(path)
        return path

    @staticmethod
    def _require_dir(path: Path) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise NotFound("no pre-images for this job") from None
        if not stat.S_ISDIR(info.st_mode):
            raise PreimageIntegrityError("the pre-image directory is not a plain directory")

    def label(self, job_id: str) -> str:
        return "%s/%s" % (self._label_prefix, job_id) if JOB_ID_RE.fullmatch(str(job_id)) else ""

    # -- private file IO -------------------------------------------------------------

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        temp = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(6)))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_BINARY", 0)
        fd = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    @staticmethod
    def _read_private(path: Path, limit: int) -> bytes:
        flags = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            raise NotFound("pre-image file missing") from None
        except OSError as exc:
            raise PreimageIntegrityError("pre-image file cannot be opened safely: %s"
                                         % type(exc).__name__) from None
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise PreimageIntegrityError("pre-image file is not a regular file")
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise PreimageIntegrityError("pre-image file exceeds its bound")
        return data

    def _load_manifest(self, job_dir: Path) -> dict:
        data = self._read_private(job_dir / _MANIFEST, MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise PreimageIntegrityError("the pre-image manifest is not valid JSON") from None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files", {}), dict):
            raise PreimageIntegrityError("the pre-image manifest has an invalid shape")
        return manifest

    def _store_manifest(self, job_dir: Path, manifest: Mapping[str, Any]) -> None:
        data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_MANIFEST_BYTES:
            raise PreimageIntegrityError("the pre-image manifest exceeds its bound")
        self._write_atomic(job_dir / _MANIFEST, data)

    # -- port --------------------------------------------------------------------

    def begin(self, job_id: str, manifest: Mapping[str, Any]) -> None:
        with self._lock:
            self._prune()
            job_dir = self._job_dir(job_id, create=True)
            body = {key: value for key, value in dict(manifest).items()
                    if isinstance(key, str) and isinstance(value, (str, int, float, bool))}
            body.update({"job_id": job_id, "files": {}, "status": str(body.get("status") or "running"),
                         "updated_at": float(self._clock())})
            self._store_manifest(job_dir, body)

    def save(self, job_id: str, rel: str, text: str, sha256: str) -> None:
        clean = safe_rel(rel)
        if clean is None:
            raise InvalidInput("pre-image path must be source-relative")
        if not isinstance(sha256, str) or not _DIGEST_RE.fullmatch(sha256):
            raise InvalidInput("pre-image digest must be sha256 hex")
        data = text.encode("utf-8", "surrogatepass")
        if len(data) > MAX_FILE_BYTES:
            raise InvalidInput("pre-image exceeds 2 MiB")
        if _sha(data) != sha256:
            raise PreimageIntegrityError("pre-image text does not match its digest")
        with self._lock:
            job_dir = self._job_dir(job_id)
            manifest = self._load_manifest(job_dir)
            files = manifest["files"]
            if clean in files:
                return  # the first original wins; later saves never overwrite it
            if len(files) >= MAX_FILES_PER_JOB:
                raise InvalidInput("too many pre-images for one fix")
            self._write_atomic(job_dir / _BLOBS / (sha256 + ".txt"), data)
            files[clean] = {"original_sha256": sha256, "last_written_sha256": ""}
            manifest["updated_at"] = float(self._clock())
            self._store_manifest(job_dir, manifest)

    def record_write(self, job_id: str, rel: str, sha256: str) -> None:
        clean = safe_rel(rel)
        if clean is None or not isinstance(sha256, str) or not _DIGEST_RE.fullmatch(sha256):
            raise InvalidInput("invalid pre-image write record")
        with self._lock:
            job_dir = self._job_dir(job_id)
            manifest = self._load_manifest(job_dir)
            entry = manifest["files"].get(clean)
            if entry is None:
                raise NotFound("no pre-image for %s" % clean)
            entry["last_written_sha256"] = sha256
            manifest["updated_at"] = float(self._clock())
            self._store_manifest(job_dir, manifest)

    def load(self, job_id: str, rel: str) -> tuple[str, str]:
        clean = safe_rel(rel)
        if clean is None:
            raise InvalidInput("pre-image path must be source-relative")
        with self._lock:
            job_dir = self._job_dir(job_id)
            manifest = self._load_manifest(job_dir)
            entry = manifest["files"].get(clean)
            if not isinstance(entry, dict):
                raise NotFound("no pre-image for %s" % clean)
            digest = str(entry.get("original_sha256", ""))
            if not _DIGEST_RE.fullmatch(digest):
                raise PreimageIntegrityError("pre-image record has no valid digest")
            data = self._read_private(job_dir / _BLOBS / (digest + ".txt"), MAX_FILE_BYTES)
        if _sha(data) != digest:
            raise PreimageIntegrityError("pre-image of %s failed its sha256 check" % clean)
        try:
            return data.decode("utf-8", "surrogatepass"), digest
        except UnicodeDecodeError:
            raise PreimageIntegrityError("pre-image of %s is not text" % clean) from None

    def list(self, job_id: str) -> tuple[PreimageEntry, ...]:
        with self._lock:
            manifest = self._load_manifest(self._job_dir(job_id))
        out = []
        for rel, entry in sorted(manifest["files"].items()):
            if isinstance(entry, dict) and safe_rel(rel) == rel:
                out.append(PreimageEntry(rel=rel, original_sha256=str(entry.get("original_sha256", "")),
                                         last_written_sha256=str(entry.get("last_written_sha256", ""))))
        return tuple(out[:MAX_FILES_PER_JOB])

    def manifest(self, job_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            try:
                return self._load_manifest(self._job_dir(job_id))
            except NotFound:
                return None

    def set_status(self, job_id: str, status: str) -> None:
        if not isinstance(status, str) or not re.fullmatch(r"[a-z_]{1,32}", status):
            raise InvalidInput("invalid pre-image status")
        with self._lock:
            try:
                job_dir = self._job_dir(job_id)
            except NotFound:
                return
            manifest = self._load_manifest(job_dir)
            manifest["status"] = status
            manifest["updated_at"] = float(self._clock())
            self._store_manifest(job_dir, manifest)

    def jobs(self) -> tuple[str, ...]:
        with self._lock:
            try:
                names = sorted(os.listdir(self._root))[:4096]
            except FileNotFoundError:
                return ()
        return tuple(name for name in names if JOB_ID_RE.fullmatch(name))

    def purge(self, job_id: str) -> None:
        with self._lock:
            try:
                job_dir = self._job_dir(job_id)
            except NotFound:
                return
            shutil.rmtree(job_dir)

    def _prune(self) -> None:
        jobs = self.jobs()
        if len(jobs) < self._max_jobs:
            return
        finished = []
        for job_id in jobs:
            try:
                manifest = self._load_manifest(self._job_dir(job_id))
            except (NotFound, PreimageIntegrityError, InvalidInput):
                continue
            if manifest.get("status") in FINISHED:
                applied = any(isinstance(entry, dict) and entry.get("last_written_sha256")
                              and entry.get("last_written_sha256") != entry.get("original_sha256")
                              for entry in manifest.get("files", {}).values())
                # Jobs whose edits are back to the originals go first; unfinished
                # (running or interrupted) jobs are never pruned.
                finished.append((applied, float(manifest.get("updated_at", 0) or 0), job_id))
        for _, _, job_id in sorted(finished)[: len(jobs) - self._max_jobs + 1]:
            self.purge(job_id)


__all__ = ["FilePreimageStore", "JOB_ID_RE", "MAX_FILES_PER_JOB"]
