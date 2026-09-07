"""Private, sealed source spool for outbound artifact mobility.

The adapter accepts only bytes from a trusted in-process stream.  It has no
receiver protocol, grant, bearer, URL, destination, or source-path surface.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect
from sonder_runtime.application.artifacts.mobility_source import (
    MobilitySourceError,
    SourceArtifactRange,
    SourceAuthority,
)
from sonder_runtime.application.compute_fabric.artifact_spool import (
    ArtifactSpoolError,
    PrivateDirectoryAnchor,
)


_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CHUNK_BYTES = 1024 * 1024
_LOCK_NAME = "mobility-source.lock"
_DATABASE_NAME = "mobility-source.sqlite"
_TEMPORARY = re.compile(r"snapshot-[A-Za-z0-9_-]+\.part")


class SQLiteArtifactMobilitySourceStore:
    """One private source namespace, with immutable objects and bounded reads.

    The file lock rejects concurrent publishers across processes.  Reads are
    independently scope-filtered and re-hash every chunk returned to the
    caller, so a tampered local object fails closed rather than yielding bytes.
    """

    def __init__(self, root) -> None:
        self.root = Path(root).absolute()
        self._thread_lock = threading.RLock()
        try:
            self._safe_root()
            with PrivateDirectoryAnchor.open_base(self.root) as anchor:
                if not anchor.exists(_LOCK_NAME):
                    descriptor, temporary = anchor.create_temporary()
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(b"0")
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        anchor.publish(temporary, _LOCK_NAME)
                    except FileExistsError:
                        anchor.unlink(temporary)
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS mobility_source_artifacts(
                      id TEXT PRIMARY KEY,
                      scope TEXT NOT NULL,
                      sha256 TEXT NOT NULL,
                      size_bytes INTEGER NOT NULL,
                      media_type TEXT NOT NULL,
                      created_at REAL NOT NULL,
                      expires_at REAL NOT NULL,
                      UNIQUE(scope, id)
                    );
                    CREATE TABLE IF NOT EXISTS mobility_source_chunks(
                      artifact_id TEXT NOT NULL,
                      offset INTEGER NOT NULL,
                      size_bytes INTEGER NOT NULL,
                      sha256 TEXT NOT NULL,
                      PRIMARY KEY(artifact_id, offset)
                    );
                    """
                )
        except MobilitySourceError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError, sqlite3.Error):
            raise MobilitySourceError("UNSAFE_STORE") from None

    def _safe_root(self) -> None:
        """Refuse any spool exposed by the regular workspace file authority."""
        try:
            from sonder_runtime.adapters.filesystem.file_ops import allowed_roots

            root = self.root.resolve()
            for allowed in allowed_roots():
                candidate = Path(allowed).resolve()
                if root == candidate or root in candidate.parents or candidate in root.parents:
                    raise MobilitySourceError("UNSAFE_STORE")
        except MobilitySourceError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise MobilitySourceError("UNSAFE_STORE") from None

    @staticmethod
    def _database_entry_is_safe(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return True
        return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and not path.is_symlink()

    @contextmanager
    def _connection(self):
        connection = None
        try:
            self._safe_root()
            with PrivateDirectoryAnchor(self.root) as anchor:
                for name in (
                    _DATABASE_NAME,
                    _DATABASE_NAME + "-journal",
                    _DATABASE_NAME + "-wal",
                    _DATABASE_NAME + "-shm",
                ):
                    if not self._database_entry_is_safe(self.root / name):
                        raise MobilitySourceError("UNSAFE_STORE")
                connection = owned_sqlite_connect(self.root / _DATABASE_NAME, timeout=1)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA synchronous=FULL")
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
                    connection = None
                anchor.validate()
        except MobilitySourceError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError, sqlite3.Error):
            raise MobilitySourceError("UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _mutation(self):
        try:
            with self._thread_lock:
                self._safe_root()
                with PrivateDirectoryAnchor(self.root) as anchor:
                    with anchor.open_read(_LOCK_NAME) as lock:
                        try:
                            if os.name == "nt":
                                import msvcrt

                                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                            else:
                                import fcntl

                                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except OSError:
                            raise MobilitySourceError("BUSY") from None
                        try:
                            yield
                        finally:
                            if os.name == "nt":
                                lock.seek(0)
                                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except MobilitySourceError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError):
            raise MobilitySourceError("UNAVAILABLE") from None

    @staticmethod
    def _fsync_directory(anchor: PrivateDirectoryAnchor) -> None:
        anchor.validate()
        if os.name != "nt":
            os.fsync(anchor.fd)

    @staticmethod
    def _row_identity(row) -> tuple[str, str, str]:
        try:
            source_id, scope, digest = row["id"], row["scope"], row["sha256"]
        except (IndexError, KeyError, TypeError):
            raise MobilitySourceError("INTEGRITY") from None
        if (
            not isinstance(source_id, str)
            or _ID.fullmatch(source_id) is None
            or not isinstance(scope, str)
            or _DIGEST.fullmatch(scope) is None
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            raise MobilitySourceError("INTEGRITY")
        return source_id, scope, digest

    @contextmanager
    def _artifact_directory(self, scope: str, source_id: str, *, create: bool):
        if _DIGEST.fullmatch(scope) is None or _ID.fullmatch(source_id) is None:
            raise MobilitySourceError("INTEGRITY")
        try:
            with PrivateDirectoryAnchor(self.root) as base:
                if create:
                    scope_anchor, _ = base.child(scope)
                    try:
                        artifact_anchor, created = scope_anchor.child(source_id)
                        if not created:
                            artifact_anchor.close()
                            raise MobilitySourceError("BUSY")
                        try:
                            yield artifact_anchor
                        finally:
                            artifact_anchor.close()
                    finally:
                        scope_anchor.close()
                else:
                    with PrivateDirectoryAnchor(base.path / scope) as scope_anchor:
                        with PrivateDirectoryAnchor(scope_anchor.path / source_id) as artifact_anchor:
                            yield artifact_anchor
        except MobilitySourceError:
            raise
        except FileNotFoundError:
            raise MobilitySourceError("INTEGRITY") from None
        except (ArtifactSpoolError, OSError, RuntimeError):
            raise MobilitySourceError("UNAVAILABLE") from None

    @staticmethod
    def _receipt(row) -> dict:
        return {
            "source_artifact_id": row["id"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "media_type": row["media_type"],
        }

    def _row(self, connection, source_artifact_id: str, authority: SourceAuthority):
        row = connection.execute(
            "SELECT * FROM mobility_source_artifacts WHERE id=? AND scope=?",
            (source_artifact_id, authority.scope_id),
        ).fetchone()
        if row is None:
            raise MobilitySourceError("NOT_FOUND")
        self._row_identity(row)
        expires_at = row["expires_at"]
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(expires_at)
        ):
            raise MobilitySourceError("INTEGRITY")
        if expires_at <= time.time():
            raise MobilitySourceError("NOT_FOUND")
        if (
            type(row["size_bytes"]) is not int
            or not 0 <= row["size_bytes"] <= authority.limits.max_object_bytes
            or not isinstance(row["media_type"], str)
            or not 1 <= len(row["media_type"]) <= 128
            or any(
                ord(character) < 32 or ord(character) > 126
                for character in row["media_type"]
            )
        ):
            raise MobilitySourceError("INTEGRITY")
        return row

    def _copy_stream(self, stream, artifact: PrivateDirectoryAnchor, spec: dict):
        descriptor, temporary = artifact.create_temporary()
        digest = hashlib.sha256()
        chunks: list[tuple[int, int, str]] = []
        total = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                while total < spec["size_bytes"]:
                    wanted = min(_CHUNK_BYTES, spec["size_bytes"] - total)
                    try:
                        body = stream.read(wanted)
                    except Exception:
                        raise MobilitySourceError("INVALID_STREAM") from None
                    # A partial read would turn one bounded object into an
                    # unbounded number of metadata rows. Trusted publishers
                    # must provide a locally buffered stream that fills each
                    # requested chunk until the known final boundary.
                    if not isinstance(body, bytes) or len(body) != wanted:
                        raise MobilitySourceError("INVALID_STREAM")
                    output.write(body)
                    digest.update(body)
                    chunks.append((total, len(body), hashlib.sha256(body).hexdigest()))
                    total += len(body)
                try:
                    extra = stream.read(1)
                except Exception:
                    raise MobilitySourceError("INVALID_STREAM") from None
                if not isinstance(extra, bytes) or extra:
                    raise MobilitySourceError("INVALID_STREAM")
                output.flush()
                os.fsync(output.fileno())
            if total != spec["size_bytes"] or digest.hexdigest() != spec["sha256"]:
                raise MobilitySourceError("INTEGRITY")
            artifact.publish(temporary, spec["sha256"])
            self._fsync_directory(artifact)
            return chunks
        except MobilitySourceError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError):
            raise MobilitySourceError("UNAVAILABLE") from None
        finally:
            try:
                if artifact.exists(temporary):
                    artifact.unlink(temporary)
            except (ArtifactSpoolError, OSError):
                pass

    def _remove_expired_payload(self, row) -> None:
        source_id, scope, digest = self._row_identity(row)
        try:
            with self._artifact_directory(scope, source_id, create=False) as artifact:
                if artifact.exists(digest):
                    artifact.unlink(digest)
                for entry in os.scandir(artifact.path):
                    if _TEMPORARY.fullmatch(entry.name):
                        artifact.unlink(entry.name)
                    else:
                        raise MobilitySourceError("INTEGRITY")
                artifact_path = artifact.path
            artifact_path.rmdir()
            scope_path = artifact_path.parent
            try:
                scope_path.rmdir()
            except OSError:
                pass
        except MobilitySourceError:
            raise
        except FileNotFoundError:
            return
        except (ArtifactSpoolError, OSError, RuntimeError):
            raise MobilitySourceError("UNAVAILABLE") from None

    def _reap_expired(self, connection, *, limit: int = 64) -> None:
        rows = connection.execute(
            """SELECT * FROM mobility_source_artifacts
               WHERE expires_at<=? ORDER BY expires_at, id LIMIT ?""",
            (time.time(), limit),
        ).fetchall()
        for row in rows:
            self._remove_expired_payload(row)
            connection.execute(
                "DELETE FROM mobility_source_chunks WHERE artifact_id=?", (row["id"],)
            )
            connection.execute(
                "DELETE FROM mobility_source_artifacts WHERE id=?", (row["id"],)
            )

    def publish_sealed(self, stream, spec: dict, authority: SourceAuthority) -> dict:
        try:
            with self._mutation(), self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._reap_expired(connection)
                if connection.execute(
                    "SELECT COUNT(*) FROM mobility_source_artifacts"
                ).fetchone()[0] >= 4096:
                    raise MobilitySourceError("CAPACITY")
                used = connection.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM mobility_source_artifacts"
                ).fetchone()[0]
                if used + spec["size_bytes"] > authority.limits.total_bytes:
                    raise MobilitySourceError("QUOTA")
                source_id = uuid.uuid4().hex
                with self._artifact_directory(authority.scope_id, source_id, create=True) as artifact:
                    chunks = self._copy_stream(stream, artifact, spec)
                now = time.time()
                connection.execute(
                    """INSERT INTO mobility_source_artifacts
                       (id,scope,sha256,size_bytes,media_type,created_at,expires_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        source_id,
                        authority.scope_id,
                        spec["sha256"],
                        spec["size_bytes"],
                        spec["media_type"],
                        now,
                        now + authority.limits.ttl_seconds,
                    ),
                )
                connection.executemany(
                    """INSERT INTO mobility_source_chunks
                       (artifact_id,offset,size_bytes,sha256) VALUES(?,?,?,?)""",
                    [(source_id, *chunk) for chunk in chunks],
                )
                return {
                    "source_artifact_id": source_id,
                    "sha256": spec["sha256"],
                    "size_bytes": spec["size_bytes"],
                    "media_type": spec["media_type"],
                }
        except MobilitySourceError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError, sqlite3.Error):
            raise MobilitySourceError("UNAVAILABLE") from None

    def inspect_sealed(self, source_artifact_id: str, authority: SourceAuthority) -> dict:
        try:
            with self._connection() as connection:
                return self._receipt(self._row(connection, source_artifact_id, authority))
        except MobilitySourceError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError, sqlite3.Error):
            raise MobilitySourceError("UNAVAILABLE") from None

    def read_range(
        self, source_artifact_id: str, offset: int, length: int, authority: SourceAuthority
    ) -> SourceArtifactRange:
        try:
            with self._connection() as connection:
                row = self._row(connection, source_artifact_id, authority)
                size = row["size_bytes"]
                if offset >= size:
                    raise MobilitySourceError("INVALID_RANGE")
                end = min(offset + length, size)
                chunks = connection.execute(
                    """SELECT * FROM mobility_source_chunks
                       WHERE artifact_id=? AND offset<? AND offset+size_bytes>?
                       ORDER BY offset""",
                    (source_artifact_id, end, offset),
                ).fetchall()
            expected = offset
            body = bytearray()
            source_id, scope, digest = self._row_identity(row)
            with self._artifact_directory(scope, source_id, create=False) as artifact:
                with artifact.open_read(digest) as stream:
                    for index, chunk in enumerate(chunks):
                        chunk_offset = chunk["offset"]
                        chunk_size = chunk["size_bytes"]
                        chunk_digest = chunk["sha256"]
                        if (
                            type(chunk_offset) is not int
                            or type(chunk_size) is not int
                            or not 0 <= chunk_offset < size
                            or not 1 <= chunk_size <= _CHUNK_BYTES
                            or chunk_offset + chunk_size > size
                            or not isinstance(chunk_digest, str)
                            or _DIGEST.fullmatch(chunk_digest) is None
                        ):
                            raise MobilitySourceError("INTEGRITY")
                        if index == 0:
                            if not chunk_offset <= offset < chunk_offset + chunk_size:
                                raise MobilitySourceError("INTEGRITY")
                        elif chunk_offset != expected:
                            raise MobilitySourceError("INTEGRITY")
                        stream.seek(chunk_offset)
                        stored = stream.read(chunk_size)
                        if (
                            len(stored) != chunk_size
                            or hashlib.sha256(stored).hexdigest() != chunk_digest
                        ):
                            raise MobilitySourceError("INTEGRITY")
                        begin = max(offset, chunk_offset)
                        finish = min(end, chunk_offset + chunk_size)
                        if begin < finish:
                            body.extend(stored[begin - chunk_offset:finish - chunk_offset])
                            expected = finish
            if expected != end or len(body) != end - offset:
                raise MobilitySourceError("INTEGRITY")
            result = bytes(body)
            return SourceArtifactRange(
                source_artifact_id=source_artifact_id,
                sha256=row["sha256"],
                size_bytes=size,
                offset=offset,
                body=result,
                chunk_sha256=hashlib.sha256(result).hexdigest(),
            )
        except MobilitySourceError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError, sqlite3.Error):
            raise MobilitySourceError("UNAVAILABLE") from None

    def close(self) -> None:
        """Connections are operation-scoped; no background or network resource exists."""
