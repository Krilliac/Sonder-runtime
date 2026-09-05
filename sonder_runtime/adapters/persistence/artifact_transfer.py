"""Single-host scoped transfer ledger and immutable files in an anchored private spool."""

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from ...application.artifacts.transfer import ArtifactRange, TransferError
from ...application.compute_fabric.artifact_spool import PrivateDirectoryAnchor


def _command(value):
    if not isinstance(value, str) or not re.fullmatch("[A-Za-z0-9_.:-]{1,128}", value):
        raise TransferError("INVALID_COMMAND")


class SQLiteArtifactTransferStore:
    """Published bytes remain pinned; only private staging is reclaimed.

    One nonblocking OS lock serializes mutations/verification across processes.
    Reads/status remain available. Contention returns BUSY, never queues an
    unbounded verifier or silently multiplies temporary disk reservations.
    """

    def __init__(self, root):
        self.root = Path(root).absolute()
        self._safe_root()
        with PrivateDirectoryAnchor.open_base(self.root) as anchor:
            if not anchor.exists("transfer.lock"):
                fd, temporary = anchor.create_temporary()
                with os.fdopen(fd, "wb") as stream:
                    stream.write(b"0")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    anchor.publish(temporary, "transfer.lock")
                except FileExistsError:
                    anchor.unlink(temporary)
        with self._connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS artifact_uploads(
              id TEXT PRIMARY KEY,scope TEXT NOT NULL,command TEXT NOT NULL,spec TEXT NOT NULL,
              grant_id TEXT NOT NULL,grant_revision INTEGER NOT NULL,expires REAL NOT NULL,
              state TEXT NOT NULL,offset INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,
              reserved INTEGER NOT NULL,chunk_bytes INTEGER NOT NULL,seal_command TEXT,abort_command TEXT,
              UNIQUE(scope,command));
            CREATE TABLE IF NOT EXISTS artifact_chunks(
              upload_id TEXT NOT NULL,offset INTEGER NOT NULL,size INTEGER NOT NULL,digest TEXT NOT NULL,
              receipt TEXT NOT NULL,PRIMARY KEY(upload_id,offset));
            """)

    def _safe_root(self):
        from ..filesystem.file_ops import allowed_roots

        root = self.root.resolve()
        for allowed in allowed_roots():
            allowed = Path(allowed).resolve()
            if root == allowed or allowed in root.parents or root in allowed.parents:
                raise TransferError("UNSAFE_STORE")

    @contextmanager
    def _connection(self):
        self._safe_root()
        with PrivateDirectoryAnchor(self.root) as anchor:
            for name in (
                "transfers.sqlite",
                "transfers.sqlite-wal",
                "transfers.sqlite-shm",
            ):
                path = self.root / name
                if path.exists() or path.is_symlink():
                    info = path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or path.is_symlink()
                    ):
                        raise TransferError("UNSAFE_STORE")
            conn = owned_sqlite_connect(self.root / "transfers.sqlite", timeout=1)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=FULL")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
            anchor.validate()

    @contextmanager
    def _mutation(self):
        self._safe_root()
        with PrivateDirectoryAnchor(self.root) as anchor:
            with anchor.open_read("transfer.lock") as lock:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise TransferError("BUSY") from None
                try:
                    yield
                finally:
                    if os.name == "nt":
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _directories(self, row):
        with PrivateDirectoryAnchor(self.root) as base:
            scope, _ = base.child(row["scope"])
            with scope:
                stage, _ = scope.child(row["id"])
                with stage:
                    yield scope, stage

    @staticmethod
    def _fsync_directory(anchor):
        # POSIX rename/link durability needs an explicit directory sync.
        # Windows files use FlushFileBuffers through os.fsync; no HA claim.
        anchor.validate()
        if os.name != "nt":
            os.fsync(anchor.fd)

    @staticmethod
    def _row(conn, identity, grant, *, upload=True):
        if not isinstance(identity, str) or not re.fullmatch("[0-9a-f]{32}", identity):
            raise TransferError("NOT_FOUND")
        row = conn.execute(
            "SELECT * FROM artifact_uploads WHERE id=? AND scope=?",
            (identity, grant.scope_id),
        ).fetchone()
        if row is None:
            raise TransferError("NOT_FOUND")
        if upload and row["state"] not in ("sealed", "aborted"):
            if (
                row["grant_id"] != grant.grant_id
                or row["grant_revision"] != grant.revision
                or time.time() >= min(row["expires"], grant.expires_at)
            ):
                raise TransferError("FORBIDDEN")
        return row

    @staticmethod
    def _receipt(row):
        result = dict(
            transfer_id=row["id"],
            state=row["state"],
            offset=row["offset"],
            chunk_bytes=row["chunk_bytes"],
            expires_at=row["expires"],
            revision=row["revision"],
        )
        if row["state"] == "sealed":
            spec = json.loads(row["spec"])
            result["artifact"] = dict(artifact_id=row["id"], **spec)
        return result

    def begin(self, spec, command_id, grant, limits):
        _command(command_id)
        encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        with self._mutation(), self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM artifact_uploads WHERE scope=? AND command=?",
                (grant.scope_id, command_id),
            ).fetchone()
            if prior:
                if prior["spec"] != encoded:
                    raise TransferError("COMMAND_CONFLICT")
                row = self._row(conn, prior["id"], grant)
                self._verify_resume_prefix(conn, row)
                return self._receipt(row)
            # Reserve stage plus publication copy; never assume dedup before verification.
            reservation = 2 * spec["size_bytes"]
            used = conn.execute(
                "SELECT COALESCE(SUM(reserved),0) FROM artifact_uploads"
            ).fetchone()[0]
            scoped = conn.execute(
                "SELECT COALESCE(SUM(reserved),0) FROM artifact_uploads WHERE scope=?",
                (grant.scope_id,),
            ).fetchone()[0]
            active = conn.execute(
                "SELECT scope FROM artifact_uploads WHERE state IN ('open','verifying')"
            ).fetchall()
            if (
                conn.execute("SELECT COUNT(*) FROM artifact_uploads").fetchone()[0]
                >= 4096
            ):
                raise TransferError("CAPACITY")
            if (
                used + reservation > limits.total_bytes
                or scoped + reservation > grant.quota_bytes
            ):
                raise TransferError("QUOTA")
            if (
                len(active) >= limits.active_total
                or sum(r[0] == grant.scope_id for r in active)
                >= limits.active_per_scope
            ):
                raise TransferError("CAPACITY")
            identity = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO artifact_uploads
              (id,scope,command,spec,grant_id,grant_revision,expires,state,reserved,chunk_bytes)
              VALUES(?,?,?,?,?,?,?,'open',?,?)""",
                (
                    identity,
                    grant.scope_id,
                    command_id,
                    encoded,
                    grant.grant_id,
                    grant.revision,
                    min(grant.expires_at, time.time() + limits.ttl_seconds),
                    reservation,
                    limits.chunk_bytes,
                ),
            )
            return self._receipt(self._row(conn, identity, grant))

    def _verify_resume_prefix(self, conn, row):
        if row["state"] not in ("open", "verifying") or not row["offset"]:
            return
        spec = json.loads(row["spec"])
        with self._directories(row) as (_, stage):
            if stage.exists(spec["sha256"]):
                self._verify_blob(stage, spec["sha256"], spec, row["chunk_bytes"])
                return
            total = 0
            for chunk in conn.execute(
                "SELECT * FROM artifact_chunks WHERE upload_id=? ORDER BY offset",
                (row["id"],),
            ):
                data = stage.read_bytes(
                    f"{chunk['offset']:016x}", max_bytes=row["chunk_bytes"]
                )
                if (
                    chunk["offset"] != total
                    or len(data) != chunk["size"]
                    or hashlib.sha256(data).hexdigest() != chunk["digest"]
                ):
                    raise TransferError("STAGED_DIGEST_MISMATCH")
                total += len(data)
            if total != row["offset"]:
                raise TransferError("STAGED_DIGEST_MISMATCH")

    def inspect(self, identity, grant):
        with self._connection() as conn:
            return self._receipt(self._row(conn, identity, grant))

    def _after_chunk_publish(self):
        """Fault-injection seam after durable bytes, before SQLite commit."""

    def _after_object_publish(self):
        """Fault-injection seam after immutable bytes, before sealed receipt."""

    def _after_stage_cleanup(self):
        """Fault-injection seam after stage cleanup, before quota release."""

    @staticmethod
    def _remove_temporaries(stage):
        stage.validate()
        for item in os.scandir(stage.path):
            if re.fullmatch(r"snapshot-[A-Za-z0-9_-]+\.part", item.name):
                stage.unlink(item.name)

    def append(self, identity, offset, digest, body, grant):
        with self._mutation(), self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, identity, grant)
            prior = conn.execute(
                "SELECT * FROM artifact_chunks WHERE upload_id=? AND offset=?",
                (identity, offset),
            ).fetchone()
            if prior:
                if prior["digest"] != digest or prior["size"] != len(body):
                    raise TransferError("CHUNK_CONFLICT")
                if row["state"] in ("aborted", "failed"):
                    raise TransferError("STATE_CONFLICT")
                try:
                    with self._directories(row) as (_, stage):
                        spec = json.loads(row["spec"])
                        if row["state"] == "sealed" or stage.exists(spec["sha256"]):
                            with stage.open_read(spec["sha256"]) as stream:
                                stream.seek(offset)
                                stored = stream.read(prior["size"])
                        else:
                            stored = stage.read_bytes(
                                f"{offset:016x}", max_bytes=row["chunk_bytes"]
                            )
                        if (
                            len(stored) != prior["size"]
                            or hashlib.sha256(stored).hexdigest() != digest
                        ):
                            raise TransferError("STAGED_DIGEST_MISMATCH")
                except OSError:
                    raise TransferError("STAGED_DIGEST_MISMATCH") from None
                return json.loads(prior["receipt"])
            if row["state"] != "open":
                raise TransferError("STATE_CONFLICT")
            spec = json.loads(row["spec"])
            if offset != row["offset"]:
                raise TransferError("OFFSET_CONFLICT")
            if (
                len(body) > row["chunk_bytes"]
                or offset + len(body) > spec["size_bytes"]
            ):
                raise TransferError("INVALID_BOUND")
            if len(body) != min(row["chunk_bytes"], spec["size_bytes"] - offset):
                raise TransferError("INVALID_CHUNK_LENGTH")
            with self._directories(row) as (_, stage):
                self._remove_temporaries(stage)
                name = f"{offset:016x}"
                if stage.exists(name):
                    existing = stage.read_bytes(name, max_bytes=row["chunk_bytes"])
                    if existing != body:
                        raise TransferError("CHUNK_CONFLICT")
                else:
                    fd, temporary = stage.create_temporary()
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                    stage.publish(temporary, name)
                    self._fsync_directory(stage)
                self._after_chunk_publish()
            ack = dict(
                offset=offset,
                next_offset=offset + len(body),
                chunk_sha256=digest,
                revision=row["revision"] + 1,
            )
            conn.execute(
                "INSERT INTO artifact_chunks VALUES(?,?,?,?,?)",
                (identity, offset, len(body), digest, json.dumps(ack)),
            )
            conn.execute(
                "UPDATE artifact_uploads SET offset=?,revision=revision+1 WHERE id=?",
                (offset + len(body), identity),
            )
            return ack

    def admit_seal(self, identity, command_id, grant):
        _command(command_id)
        with self._mutation(), self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, identity, grant)
            if row["state"] not in ("open", "verifying", "sealed"):
                raise TransferError("STATE_CONFLICT")
            if row["seal_command"] is not None and row["seal_command"] != command_id:
                raise TransferError("COMMAND_CONFLICT")
            if row["offset"] != json.loads(row["spec"])["size_bytes"]:
                raise TransferError("INCOMPLETE")
            if row["state"] == "open":
                conn.execute(
                    "UPDATE artifact_uploads SET state='verifying',seal_command=?,revision=revision+1 WHERE id=?",
                    (command_id, identity),
                )
            return self._receipt(self._row(conn, identity, grant))

    def seal(self, identity, grant, revalidate):
        with self._mutation():
            with self._connection() as conn:
                row = self._row(conn, identity, grant)
                chunks = conn.execute(
                    "SELECT * FROM artifact_chunks WHERE upload_id=? ORDER BY offset",
                    (identity,),
                ).fetchall()
            if row["state"] != "verifying":
                return
            spec = json.loads(row["spec"])
            with self._directories(row) as (scope, stage):
                self._remove_temporaries(stage)
                published = stage.exists(spec["sha256"])
                fd, temporary = stage.create_temporary()
                digest = hashlib.sha256()
                total = 0
                try:
                    with os.fdopen(fd, "wb") as output:
                        for chunk in (() if published else chunks):
                            current = revalidate()
                            if current != grant or time.time() >= row["expires"]:
                                raise TransferError("FORBIDDEN")
                            data = stage.read_bytes(
                                f"{chunk['offset']:016x}", max_bytes=row["chunk_bytes"]
                            )
                            if (
                                chunk["offset"] != total
                                or len(data) != chunk["size"]
                                or hashlib.sha256(data).hexdigest() != chunk["digest"]
                            ):
                                raise TransferError("STAGED_DIGEST_MISMATCH")
                            output.write(data)
                            digest.update(data)
                            total += len(data)
                        output.flush()
                        os.fsync(output.fileno())
                    if not published and (
                        total != spec["size_bytes"]
                        or digest.hexdigest() != spec["sha256"]
                    ):
                        raise TransferError("ARTIFACT_DIGEST_MISMATCH")
                    if revalidate() != grant or time.time() >= row["expires"]:
                        raise TransferError("FORBIDDEN")
                    # Stage-local immutable publication avoids cross-directory rename races.
                    if published:
                        self._verify_blob(
                            stage,
                            spec["sha256"],
                            spec,
                            row["chunk_bytes"],
                            revalidate=lambda: self._require_grant(
                                revalidate, grant, row["expires"]
                            ),
                        )
                        stage.unlink(temporary)
                    else:
                        stage.publish(temporary, spec["sha256"])
                        self._fsync_directory(stage)
                    self._after_object_publish()
                    self._require_grant(revalidate, grant, row["expires"])
                    # Cleanup before returning the stage reservation. Retried sealing
                    # reuses published bytes if a crash interrupts cleanup/ledger commit.
                    for chunk in chunks:
                        name = f"{chunk['offset']:016x}"
                        if stage.exists(name):
                            stage.unlink(name)
                    self._fsync_directory(stage)
                    self._after_stage_cleanup()
                    self._require_grant(revalidate, grant, row["expires"])
                    with self._connection() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        self._row(conn, identity, grant)
                        conn.execute(
                            "UPDATE artifact_uploads SET state='sealed',reserved=?,revision=revision+1 WHERE id=?",
                            (spec["size_bytes"], identity),
                        )
                except TransferError as error:
                    if "DIGEST" in str(error):
                        with self._connection() as conn:
                            conn.execute(
                                "UPDATE artifact_uploads SET state='failed',revision=revision+1 WHERE id=?",
                                (identity,),
                            )
                    raise
                finally:
                    if stage.exists(temporary):
                        stage.unlink(temporary)

    @staticmethod
    def _require_grant(revalidate, grant, expires):
        if revalidate() != grant or time.time() >= expires:
            raise TransferError("FORBIDDEN")

    @staticmethod
    def _verify_blob(stage, name, spec, chunk_bytes, *, revalidate=None):
        total = 0
        digest = hashlib.sha256()
        with stage.open_read(name) as stream:
            while data := stream.read(chunk_bytes):
                if revalidate is not None:
                    revalidate()
                total += len(data)
                if total > spec["size_bytes"]:
                    raise TransferError("ARTIFACT_DIGEST_MISMATCH")
                digest.update(data)
        if total != spec["size_bytes"] or digest.hexdigest() != spec["sha256"]:
            raise TransferError("ARTIFACT_DIGEST_MISMATCH")

    def abort(self, identity, command_id, grant):
        _command(command_id)
        with self._mutation(), self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, identity, grant)
            if row["state"] == "sealed":
                raise TransferError("STATE_CONFLICT")
            if row["abort_command"] is not None and row["abort_command"] != command_id:
                raise TransferError("COMMAND_CONFLICT")
            with self._directories(row) as (_, stage):
                self._remove_temporaries(stage)
                # All names are receiver-generated; no recursive deletion or caller paths.
                for item in os.scandir(stage.path):
                    if re.fullmatch("[0-9a-f]{16}|[0-9a-f]{64}", item.name):
                        stage.unlink(item.name)
                self._fsync_directory(stage)
            conn.execute(
                "UPDATE artifact_uploads SET state='aborted',abort_command=?,reserved=0,revision=revision+? WHERE id=?",
                (command_id, 0 if row["state"] == "aborted" else 1, identity),
            )
            return self._receipt(self._row(conn, identity, grant))

    def reap_expired(self, *, limit=8):
        """Trusted local maintenance only; never deletes sealed objects or their receipts."""
        if type(limit) is not int or not 1 <= limit <= 64:
            raise TransferError("INVALID_BOUND")
        with self._mutation(), self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT * FROM artifact_uploads
                WHERE expires<=? AND state IN ('open','verifying','failed')
                ORDER BY expires LIMIT ?""",
                (time.time(), limit),
            ).fetchall()
            for row in rows:
                with self._directories(row) as (_, stage):
                    self._remove_temporaries(stage)
                    for item in os.scandir(stage.path):
                        if re.fullmatch("[0-9a-f]{16}|[0-9a-f]{64}", item.name):
                            stage.unlink(item.name)
                    self._fsync_directory(stage)
                conn.execute(
                    "UPDATE artifact_uploads SET state='aborted',reserved=0,revision=revision+1 WHERE id=?",
                    (row["id"],),
                )
            return len(rows)

    def artifact(self, identity, grant):
        with self._connection() as conn:
            row = self._row(conn, identity, grant, upload=False)
            if row["state"] != "sealed":
                raise TransferError("NOT_FOUND")
            return self._receipt(row)["artifact"]

    def read_range(self, identity, offset, length, grant):
        with self._connection() as conn:
            row = self._row(conn, identity, grant, upload=False)
            if row["state"] != "sealed":
                raise TransferError("NOT_FOUND")
            spec = json.loads(row["spec"])
            if offset > spec["size_bytes"]:
                raise TransferError("INVALID_RANGE")
            end = min(offset + length, spec["size_bytes"])
            chunks = conn.execute(
                """SELECT * FROM artifact_chunks
              WHERE upload_id=? AND offset<? AND offset+size>? ORDER BY offset""",
                (identity, end, offset),
            ).fetchall()
        result = bytearray()
        with self._directories(row) as (_, stage):
            with stage.open_read(spec["sha256"]) as stream:
                for chunk in chunks:
                    stream.seek(chunk["offset"])
                    data = stream.read(chunk["size"])
                    if (
                        len(data) != chunk["size"]
                        or hashlib.sha256(data).hexdigest() != chunk["digest"]
                    ):
                        raise TransferError("ARTIFACT_DIGEST_MISMATCH")
                    result.extend(
                        data[
                            max(0, offset - chunk["offset"]) : min(
                                len(data), end - chunk["offset"]
                            )
                        ]
                    )
        if len(result) != end - offset:
            raise TransferError("ARTIFACT_DIGEST_MISMATCH")
        body = bytes(result)
        return ArtifactRange(
            identity,
            spec["sha256"],
            spec["size_bytes"],
            offset,
            body,
            hashlib.sha256(body).hexdigest(),
        )
