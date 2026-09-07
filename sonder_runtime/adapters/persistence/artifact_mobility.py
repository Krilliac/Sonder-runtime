"""Private SQLite journal and real OS dispatch locks for artifact mobility.

This adapter persists only local control facts needed by a later explicit
sender.  It has no HTTP, socket, peer, source, worker, timer, retry, or route
dependency.  Recovery changes expired local leases only; it cannot perform a
network action.

The lock is intentionally an operating-system lock on an open private file,
not a sentinel path.  A later sender must retain that handle continuously from
attempt admission through every peer call and lease-protected completion.
"""

from __future__ import annotations

from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
from typing import Iterator

from sonder_runtime.adapters.persistence.owned_sqlite import (
    connect as owned_sqlite_connect,
)
from sonder_runtime.application.artifacts.mobility import (
    DISPATCH_ELIGIBLE_STATES,
    LEASE_TRANSITIONS,
    MAX_ATTEMPTS,
    TERMINAL_STATES,
    DispatchLease,
    MobilityImmutableFence,
    MobilityJournalError,
    MobilityOperation,
    ReceiptCheckpoint,
)
from sonder_runtime.application.compute_fabric.artifact_spool import (
    ArtifactSpoolError,
    PrivateDirectoryAnchor,
)

_DATABASE_NAME = "artifact-mobility.sqlite"
_DATABASE_ENTRIES = (
    _DATABASE_NAME,
    _DATABASE_NAME + "-journal",
    _DATABASE_NAME + "-wal",
    _DATABASE_NAME + "-shm",
)
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_CAPABILITY = re.compile(r"[0-9a-f]{64}")
_PROTECTED_CAPABILITY = re.compile(r"v1\.[0-9a-f]{32}\.[0-9a-f]{64}\.[0-9a-f]{64}")
_LOCK_PREFIX = "mobility-operation-"
_LOCK_SUFFIX = ".lock"
_OUTCOME_CODES = frozenset(
    {
        "",
        "IMMUTABLE_FENCE",
        "ATTEMPT_LIMIT",
        "SOURCE_UNAVAILABLE",
        "MOBILITY_CAPACITY",
        "MOBILITY_QUOTA",
        "MOBILITY_UNAVAILABLE",
        "MOBILITY_FORBIDDEN",
        "MOBILITY_INTEGRITY",
        "MOBILITY_PROTOCOL",
        "MOBILITY_RECEIPT_EXPIRED",
    }
)

# Supplementary same-interpreter admission only.  It prevents platform-specific
# byte-lock quirks from allowing two handles in one Python process; every
# successful admission below *also* takes the nonblocking platform OS lock.
_LOCAL_HELD_LOCKS: set[tuple[str, str]] = set()
_LOCAL_HELD_LOCKS_GUARD = threading.RLock()


def _fail(code: str) -> None:
    raise MobilityJournalError(code)


def _operation_id(value: object, *, code: str = "NOT_FOUND") -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        _fail(code)
    return value


def _owner(value: object, *, code: str = "NOT_FOUND") -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        _fail(code)
    return value


def _now(value: object, *, code: str = "INVALID_TIME") -> float:
    if type(value) not in (int, float):
        _fail(code)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail(code)
    if (
        not 0 <= result <= 2**63 - 1
        or result != result
        or result in (float("inf"), float("-inf"))
    ):
        _fail(code)
    return result


def _lease_seconds(value: object) -> int:
    if type(value) is not int or not 2 <= value <= 3600:
        _fail("INVALID_LEASE")
    return value


def _lock_name(operation_id: str) -> str:
    return _LOCK_PREFIX + operation_id + _LOCK_SUFFIX


def _credential_key_material(value: object) -> bytes:
    """Validate private configured material without ever echoing it."""
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
        or "://" in value
        or value.startswith("//")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) is not None
    ):
        _fail("INVALID_CREDENTIAL")
    try:
        return value.encode("ascii")
    except UnicodeEncodeError:
        _fail("INVALID_CREDENTIAL")


def _receipt_key(credential_material: object, operation_id: str) -> bytes:
    return hmac.new(
        _credential_key_material(credential_material),
        b"sonder-artifact-mobility-v1/receipt/" + operation_id.encode("ascii"),
        "sha256",
    ).digest()


def _acquire_os_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class OperationDispatchLock:
    """One held private OS lock; never exposes its path or file handle publicly."""

    def __init__(
        self, repository, operation_id: str, anchor, handle, local_key
    ) -> None:
        self._repository = repository
        self._operation_id = operation_id
        self._anchor = anchor
        self._handle = handle
        self._local_key = local_key
        self._held = True

    def __repr__(self) -> str:
        return f"OperationDispatchLock(held={self._held!r})"

    @property
    def held(self) -> bool:
        return self._held

    def _assert_for(self, repository, operation_id: str) -> None:
        if (
            not self._held
            or self._repository is not repository
            or self._operation_id != operation_id
        ):
            _fail("LOCK_REQUIRED")

    def close(self) -> None:
        if not self._held:
            return
        self._held = False
        failure = False
        try:
            _release_os_lock(self._handle)
        except OSError:
            failure = True
        try:
            self._handle.close()
        except (OSError, ValueError):
            failure = True
        try:
            self._anchor.close()
        except (ArtifactSpoolError, OSError, RuntimeError):
            failure = True
        finally:
            with _LOCAL_HELD_LOCKS_GUARD:
                _LOCAL_HELD_LOCKS.discard(self._local_key)
        if failure:
            _fail("UNAVAILABLE")

    release = close

    def __enter__(self) -> "OperationDispatchLock":
        self._assert_for(self._repository, self._operation_id)
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class SQLiteArtifactMobilityJournal:
    """A source-owner-bound private local journal with permanent tombstones.

    It is intentionally not an outbound service.  Callers must explicitly open
    an :class:`OperationDispatchLock`, then pass that held lock to acquire,
    renew, fence, and transition a lease.  The ordering makes it impossible for
    ordinary service code to obtain a lease and forget the OS fencing layer.
    """

    def __init__(self, root, *, max_live_operations: int = 64) -> None:
        if type(max_live_operations) is not int or not 1 <= max_live_operations <= 256:
            _fail("INVALID_BOUND")
        self.root = Path(root).absolute()
        self._max_live_operations = max_live_operations
        try:
            self._safe_root()
            with PrivateDirectoryAnchor.open_base(self.root):
                pass
            self._initialize()
        except MobilityJournalError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError, sqlite3.Error):
            _fail("UNSAFE_STORE")

    def __repr__(self) -> str:
        return "SQLiteArtifactMobilityJournal(private=True)"

    def close(self) -> None:
        """Connections are operation-scoped; this starts no background work."""

    @staticmethod
    def protect_receipt_capability(
        capability: str, credential_material: object, operation_id: str
    ) -> str:
        """Persist an authenticated fixed-size private envelope, never plaintext.

        The per-operation key derives from current credential material and the
        canonical ID.  A fresh 128-bit nonce feeds an HMAC-SHA-256 PRF stream
        for this one 256-bit value; a distinct encrypt-then-MAC tag binds nonce
        and ciphertext.  It is deliberately not a general encryption API.
        """
        identity = _operation_id(operation_id, code="INVALID_CAPABILITY")
        if not isinstance(capability, str) or _CAPABILITY.fullmatch(capability) is None:
            _fail("INVALID_CAPABILITY")
        try:
            plaintext = bytes.fromhex(capability)
        except ValueError:
            _fail("INVALID_CAPABILITY")
        if len(plaintext) != 32:
            _fail("INVALID_CAPABILITY")
        nonce = secrets.token_bytes(16)
        key = _receipt_key(credential_material, identity)
        stream = hmac.new(key, b"enc-v1/" + nonce, "sha256").digest()
        ciphertext = bytes(
            left ^ right for left, right in zip(plaintext, stream, strict=True)
        )
        tag = hmac.new(key, b"auth-v1/" + nonce + ciphertext, "sha256").digest()
        return "v1." + nonce.hex() + "." + ciphertext.hex() + "." + tag.hex()

    @staticmethod
    def recover_receipt_capability(
        protected: str, credential_material: object, operation_id: str
    ) -> str:
        """Recover one internal capability only with current matching material."""
        identity = _operation_id(operation_id, code="INTEGRITY")
        if (
            not isinstance(protected, str)
            or _PROTECTED_CAPABILITY.fullmatch(protected) is None
        ):
            _fail("INTEGRITY")
        _version, nonce_hex, ciphertext_hex, tag_hex = protected.split(".")
        try:
            nonce = bytes.fromhex(nonce_hex)
            ciphertext = bytes.fromhex(ciphertext_hex)
            supplied_tag = bytes.fromhex(tag_hex)
        except ValueError:
            _fail("INTEGRITY")
        key = _receipt_key(credential_material, identity)
        expected_tag = hmac.new(
            key, b"auth-v1/" + nonce + ciphertext, "sha256"
        ).digest()
        if not hmac.compare_digest(supplied_tag, expected_tag):
            _fail("IMMUTABLE_FENCE")
        stream = hmac.new(key, b"enc-v1/" + nonce, "sha256").digest()
        capability = bytes(
            left ^ right for left, right in zip(ciphertext, stream, strict=True)
        ).hex()
        if _CAPABILITY.fullmatch(capability) is None:
            _fail("INTEGRITY")
        return capability

    def _safe_root(self) -> None:
        """Refuse a journal below the workspace file authority."""
        try:
            from sonder_runtime.adapters.filesystem.file_ops import allowed_roots

            root = self.root.resolve()
            for allowed in allowed_roots():
                candidate = Path(allowed).resolve()
                if (
                    root == candidate
                    or root in candidate.parents
                    or candidate in root.parents
                ):
                    _fail("UNSAFE_STORE")
        except MobilityJournalError:
            raise
        except (OSError, RuntimeError, ValueError):
            _fail("UNSAFE_STORE")

    @staticmethod
    def _database_entry_is_safe(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return True
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and not path.is_symlink()
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            self._safe_root()
            with PrivateDirectoryAnchor(self.root) as anchor:
                for name in _DATABASE_ENTRIES:
                    if not self._database_entry_is_safe(self.root / name):
                        _fail("UNSAFE_STORE")
                connection = owned_sqlite_connect(self.root / _DATABASE_NAME, timeout=1)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
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
        except MobilityJournalError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError, sqlite3.Error):
            _fail("UNAVAILABLE")
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS mobility_journal_owner(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  source_owner_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mobility_operations(
                  operation_id TEXT PRIMARY KEY,
                  source_owner_id TEXT NOT NULL,
                  source_scope_id TEXT NOT NULL,
                  source_artifact_id TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  destination_label TEXT NOT NULL,
                  destination_scope_id TEXT NOT NULL,
                  remote_command_id TEXT NOT NULL,
                  credential_generation TEXT NOT NULL,
                  destination_binding_hmac TEXT NOT NULL,
                  protected_receipt_capability TEXT NOT NULL,
                  state TEXT NOT NULL,
                  outcome_code TEXT NOT NULL,
                  attempt_epoch INTEGER NOT NULL,
                  lease_token TEXT,
                  lease_expires_at REAL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  receipt_expires_at REAL NOT NULL,
                  receiver_transfer_id TEXT,
                  receiver_artifact_id TEXT,
                  receipt_state TEXT,
                  receipt_offset INTEGER,
                  receipt_chunk_bytes INTEGER,
                  receipt_revision INTEGER,
                  UNIQUE(source_owner_id, destination_scope_id, operation_id)
                );
                CREATE TABLE IF NOT EXISTS mobility_tombstones(
                  source_owner_id TEXT NOT NULL,
                  destination_scope_id TEXT NOT NULL,
                  operation_id TEXT NOT NULL,
                  terminal_state TEXT NOT NULL,
                  terminal_at REAL NOT NULL,
                  PRIMARY KEY(source_owner_id, destination_scope_id, operation_id)
                );
                CREATE INDEX IF NOT EXISTS mobility_operations_expired_lease
                  ON mobility_operations(state, lease_expires_at);
                """)

    @staticmethod
    def _ensure_owner(connection: sqlite3.Connection, owner: str) -> None:
        row = connection.execute(
            "SELECT source_owner_id FROM mobility_journal_owner WHERE singleton=1"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO mobility_journal_owner(singleton,source_owner_id) VALUES(1,?)",
                (owner,),
            )
        elif row["source_owner_id"] != owner:
            _fail("FORBIDDEN")

    @staticmethod
    def _receipt_from_row(row) -> ReceiptCheckpoint | None:
        required = (
            row["receiver_transfer_id"],
            row["receipt_state"],
            row["receipt_offset"],
            row["receipt_chunk_bytes"],
            row["receipt_revision"],
        )
        artifact_id = row["receiver_artifact_id"]
        if all(value is None for value in required) and artifact_id is None:
            return None
        if any(value is None for value in required):
            _fail("INTEGRITY")
        try:
            return ReceiptCheckpoint(
                transfer_id=row["receiver_transfer_id"],
                artifact_id=artifact_id,
                state=row["receipt_state"],
                offset=row["receipt_offset"],
                chunk_bytes=row["receipt_chunk_bytes"],
                revision=row["receipt_revision"],
                expires_at=row["receipt_expires_at"],
            )
        except MobilityJournalError:
            _fail("INTEGRITY")

    @classmethod
    def _record(cls, row) -> MobilityOperation:
        try:
            spec = json.loads(row["spec_json"])
            return MobilityOperation(
                operation_id=row["operation_id"],
                source_owner_id=row["source_owner_id"],
                source_scope_id=row["source_scope_id"],
                source_artifact_id=row["source_artifact_id"],
                immutable_spec=spec,
                destination_label=row["destination_label"],
                destination_scope_id=row["destination_scope_id"],
                remote_command_id=row["remote_command_id"],
                credential_generation=row["credential_generation"],
                destination_binding_hmac=row["destination_binding_hmac"],
                protected_receipt_capability=row["protected_receipt_capability"],
                state=row["state"],
                outcome_code=row["outcome_code"],
                attempt_epoch=row["attempt_epoch"],
                lease_token=row["lease_token"],
                lease_expires_at=row["lease_expires_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                receipt_expires_at=row["receipt_expires_at"],
                receipt=cls._receipt_from_row(row),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            MobilityJournalError,
        ):
            _fail("INTEGRITY")

    @staticmethod
    def _fetch(connection: sqlite3.Connection, operation_id: str, owner: str):
        row = connection.execute(
            "SELECT * FROM mobility_operations WHERE operation_id=? AND source_owner_id=?",
            (operation_id, owner),
        ).fetchone()
        if row is None:
            _fail("NOT_FOUND")
        return row

    @staticmethod
    def _require_lock(
        lock: OperationDispatchLock, repository, operation_id: str
    ) -> None:
        if not isinstance(lock, OperationDispatchLock):
            _fail("LOCK_REQUIRED")
        lock._assert_for(repository, operation_id)

    @staticmethod
    def _insert_tombstone(
        connection: sqlite3.Connection, operation: MobilityOperation, now: float
    ) -> None:
        connection.execute(
            """INSERT INTO mobility_tombstones
               (source_owner_id,destination_scope_id,operation_id,terminal_state,terminal_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(source_owner_id,destination_scope_id,operation_id)
               DO NOTHING""",
            (
                operation.source_owner_id,
                operation.destination_scope_id,
                operation.operation_id,
                operation.state,
                now,
            ),
        )

    @staticmethod
    def _operation_values(operation: MobilityOperation) -> tuple:
        receipt = operation.receipt
        return (
            operation.operation_id,
            operation.source_owner_id,
            operation.source_scope_id,
            operation.source_artifact_id,
            json.dumps(
                dict(operation.immutable_spec),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            operation.destination_label,
            operation.destination_scope_id,
            operation.remote_command_id,
            operation.credential_generation,
            operation.destination_binding_hmac,
            operation.protected_receipt_capability,
            operation.state,
            operation.outcome_code,
            operation.attempt_epoch,
            operation.lease_token,
            operation.lease_expires_at,
            operation.created_at,
            operation.updated_at,
            operation.receipt_expires_at,
            None if receipt is None else receipt.transfer_id,
            None if receipt is None else receipt.artifact_id,
            None if receipt is None else receipt.state,
            None if receipt is None else receipt.offset,
            None if receipt is None else receipt.chunk_bytes,
            None if receipt is None else receipt.revision,
        )

    def create_operation(self, operation: MobilityOperation) -> MobilityOperation:
        """Write immutable intent before an outside component can be contacted."""
        if not isinstance(operation, MobilityOperation):
            _fail("INVALID_REQUEST")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_owner(connection, operation.source_owner_id)
                existing = connection.execute(
                    """SELECT 1 FROM mobility_tombstones
                       WHERE source_owner_id=? AND destination_scope_id=? AND operation_id=?""",
                    (
                        operation.source_owner_id,
                        operation.destination_scope_id,
                        operation.operation_id,
                    ),
                ).fetchone()
                if existing is not None:
                    _fail("NO_REUSE")
                active = connection.execute(
                    "SELECT COUNT(*) FROM mobility_operations WHERE state NOT IN ('sealed','terminal_blocked','expired')"
                ).fetchone()[0]
                if active >= self._max_live_operations:
                    _fail("CAPACITY")
                try:
                    connection.execute(
                        """INSERT INTO mobility_operations(
                           operation_id,source_owner_id,source_scope_id,source_artifact_id,spec_json,
                           destination_label,destination_scope_id,remote_command_id,credential_generation,
                           destination_binding_hmac,protected_receipt_capability,state,outcome_code,
                           attempt_epoch,lease_token,lease_expires_at,created_at,updated_at,
                           receipt_expires_at,receiver_transfer_id,receiver_artifact_id,receipt_state,
                           receipt_offset,receipt_chunk_bytes,receipt_revision
                         ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        self._operation_values(operation),
                    )
                except sqlite3.IntegrityError:
                    _fail("NO_REUSE")
            return operation
        except MobilityJournalError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            _fail("UNAVAILABLE")

    def load_operation(
        self, operation_id: str, source_owner_id: str
    ) -> MobilityOperation:
        identity = _operation_id(operation_id)
        owner = _owner(source_owner_id)
        with self._connection() as connection:
            self._ensure_owner(connection, owner)
            return self._record(self._fetch(connection, identity, owner))

    def load_operation_for_fencing(self, operation_id: str) -> MobilityOperation:
        """Load private immutable intent solely for a trusted local fence check.

        A changed current source owner cannot use the normal owner-scoped read
        or acquire a dispatch lease.  The later local-only service needs this
        narrow internal read to acquire the original record's lock/lease and
        atomically mark it terminal before it can contact a peer.  It is not a
        status/list API and is never composed into HTTP, CLI, MCP, or REPL.
        """
        identity = _operation_id(operation_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM mobility_operations WHERE operation_id=?", (identity,)
            ).fetchone()
            if row is None:
                _fail("NOT_FOUND")
            return self._record(row)

    def public_status(
        self, operation_id: str, source_owner_id: str
    ) -> dict[str, object]:
        return self.load_operation(operation_id, source_owner_id).public_status()

    def list_public_status(
        self, source_owner_id: str, *, limit: int = 256
    ) -> tuple[dict[str, object], ...]:
        owner = _owner(source_owner_id)
        if type(limit) is not int or not 1 <= limit <= 256:
            _fail("INVALID_BOUND")
        with self._connection() as connection:
            self._ensure_owner(connection, owner)
            rows = connection.execute(
                """SELECT * FROM mobility_operations WHERE source_owner_id=?
                   ORDER BY created_at DESC, operation_id DESC LIMIT ?""",
                (owner, limit),
            ).fetchall()
            return tuple(self._record(row).public_status() for row in rows)

    def _ensure_lock_file(
        self, anchor: PrivateDirectoryAnchor, operation_id: str
    ) -> None:
        name = _lock_name(operation_id)
        if anchor.exists(name):
            return
        descriptor, temporary = anchor.create_temporary()
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"0")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                anchor.publish(temporary, name)
            except FileExistsError:
                anchor.unlink(temporary)
        except Exception:
            try:
                if anchor.exists(temporary):
                    anchor.unlink(temporary)
            except (ArtifactSpoolError, OSError):
                pass
            raise

    def try_acquire_dispatch_lock(self, operation_id: str) -> OperationDispatchLock:
        """Take one nonblocking private OS lock for a known live operation."""
        identity = _operation_id(operation_id)
        # Do not allow arbitrary callers to fill the private root with lock
        # names.  A pruned/tombstoned operation is deliberately not dispatchable.
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM mobility_operations WHERE operation_id=?", (identity,)
            ).fetchone()
            if row is None:
                _fail("NOT_FOUND")
        local_key = (str(self.root.resolve()), identity)
        with _LOCAL_HELD_LOCKS_GUARD:
            if local_key in _LOCAL_HELD_LOCKS:
                _fail("BUSY")
            _LOCAL_HELD_LOCKS.add(local_key)
        anchor = None
        handle = None
        transferred = False
        try:
            self._safe_root()
            anchor = PrivateDirectoryAnchor(self.root)
            self._ensure_lock_file(anchor, identity)
            handle = anchor.open_read(_lock_name(identity))
            try:
                _acquire_os_lock(handle)
            except OSError:
                _fail("BUSY")
            lock = OperationDispatchLock(self, identity, anchor, handle, local_key)
            transferred = True
            return lock
        except MobilityJournalError:
            raise
        except (ArtifactSpoolError, OSError, RuntimeError):
            _fail("UNAVAILABLE")
        finally:
            # A returned lock owns both resources.  Every non-success path
            # releases the physical file handle (which releases its OS lock)
            # and the supplemental same-interpreter mark.
            if not transferred:
                if handle is not None:
                    try:
                        handle.close()
                    except (OSError, ValueError):
                        pass
                if anchor is not None:
                    try:
                        anchor.close()
                    except (ArtifactSpoolError, OSError, RuntimeError):
                        pass
                with _LOCAL_HELD_LOCKS_GUARD:
                    _LOCAL_HELD_LOCKS.discard(local_key)

    def _lease_from_record(self, operation: MobilityOperation) -> DispatchLease:
        if operation.lease_token is None or operation.lease_expires_at is None:
            _fail("INTEGRITY")
        return DispatchLease(
            operation_id=operation.operation_id,
            source_owner_id=operation.source_owner_id,
            epoch=operation.attempt_epoch,
            token=operation.lease_token,
            expires_at=operation.lease_expires_at,
        )

    @staticmethod
    def _verify_lease_shape(lease: DispatchLease) -> DispatchLease:
        if not isinstance(lease, DispatchLease):
            _fail("LEASE_LOST")
        return lease

    def acquire_dispatch(
        self,
        operation_id: str,
        source_owner_id: str,
        *,
        lock: OperationDispatchLock | None = None,
        now: float,
        lease_seconds: int,
    ) -> DispatchLease:
        """CAS an eligible operation into a fresh epoch while its OS lock is held."""
        identity = _operation_id(operation_id)
        owner = _owner(source_owner_id)
        self._require_lock(lock, self, identity)
        timestamp = _now(now)
        seconds = _lease_seconds(lease_seconds)
        expiry = _now(timestamp + seconds)
        token = secrets.token_hex(32)
        attempt_limited = False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_owner(connection, owner)
                operation = self._record(self._fetch(connection, identity, owner))
                if operation.state in TERMINAL_STATES:
                    _fail("TERMINAL")
                if operation.state not in DISPATCH_ELIGIBLE_STATES:
                    _fail("BUSY")
                if operation.attempt_epoch >= MAX_ATTEMPTS:
                    changed = connection.execute(
                        """UPDATE mobility_operations
                           SET state='terminal_blocked', outcome_code='ATTEMPT_LIMIT',
                               lease_token=NULL, lease_expires_at=NULL, updated_at=?
                           WHERE operation_id=? AND source_owner_id=? AND state=?
                             AND attempt_epoch=? AND lease_token IS NULL
                             AND lease_expires_at IS NULL""",
                        (
                            timestamp,
                            identity,
                            owner,
                            operation.state,
                            operation.attempt_epoch,
                        ),
                    ).rowcount
                    if changed != 1:
                        _fail("BUSY")
                    terminal = self._record(self._fetch(connection, identity, owner))
                    self._insert_tombstone(connection, terminal, timestamp)
                    attempt_limited = True
                if not attempt_limited:
                    changed = connection.execute(
                        """UPDATE mobility_operations
                           SET state='dispatching', attempt_epoch=attempt_epoch+1,
                               lease_token=?, lease_expires_at=?, updated_at=?
                           WHERE operation_id=? AND source_owner_id=? AND state=?
                             AND attempt_epoch=? AND lease_token IS NULL
                             AND lease_expires_at IS NULL""",
                        (
                            token,
                            expiry,
                            timestamp,
                            identity,
                            owner,
                            operation.state,
                            operation.attempt_epoch,
                        ),
                    ).rowcount
                    if changed != 1:
                        _fail("BUSY")
                    lease = self._lease_from_record(
                        self._record(self._fetch(connection, identity, owner))
                    )
            if attempt_limited:
                _fail("ATTEMPT_LIMIT")
            return lease
        except MobilityJournalError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            _fail("UNAVAILABLE")

    def assert_current_lease(
        self,
        lease: DispatchLease,
        *,
        lock: OperationDispatchLock | None = None,
        now: float,
    ) -> MobilityOperation:
        """Prove lease freshness immediately before a future peer call."""
        lease = self._verify_lease_shape(lease)
        self._require_lock(lock, self, lease.operation_id)
        timestamp = _now(now)
        with self._connection() as connection:
            operation = self._record(
                self._fetch(connection, lease.operation_id, lease.source_owner_id)
            )
            if (
                operation.state != "dispatching"
                or operation.attempt_epoch != lease.epoch
                or operation.lease_token is None
                or not hmac.compare_digest(operation.lease_token, lease.token)
                or operation.lease_expires_at is None
                or operation.lease_expires_at <= timestamp
            ):
                _fail("LEASE_LOST")
            return operation

    def renew_dispatch(
        self,
        lease: DispatchLease,
        *,
        lock: OperationDispatchLock | None = None,
        now: float,
        lease_seconds: int,
    ) -> DispatchLease:
        """Extend only the current unexpired token/epoch; stale holders cannot revive."""
        lease = self._verify_lease_shape(lease)
        self._require_lock(lock, self, lease.operation_id)
        timestamp = _now(now)
        seconds = _lease_seconds(lease_seconds)
        expiry = _now(timestamp + seconds)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    """UPDATE mobility_operations
                       SET lease_expires_at=?, updated_at=?
                       WHERE operation_id=? AND source_owner_id=? AND state='dispatching'
                         AND attempt_epoch=? AND lease_token=? AND lease_expires_at>?""",
                    (
                        expiry,
                        timestamp,
                        lease.operation_id,
                        lease.source_owner_id,
                        lease.epoch,
                        lease.token,
                        timestamp,
                    ),
                ).rowcount
                if changed != 1:
                    _fail("LEASE_LOST")
                return DispatchLease(
                    operation_id=lease.operation_id,
                    source_owner_id=lease.source_owner_id,
                    epoch=lease.epoch,
                    token=lease.token,
                    expires_at=expiry,
                )
        except MobilityJournalError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            _fail("UNAVAILABLE")

    @staticmethod
    def _validate_checkpoint(
        operation: MobilityOperation, checkpoint: ReceiptCheckpoint | None, target: str
    ) -> ReceiptCheckpoint | None:
        if checkpoint is not None:
            if (
                not isinstance(checkpoint, ReceiptCheckpoint)
                or checkpoint.offset > operation.immutable_spec["size_bytes"]
            ):
                _fail("INVALID_RECEIPT")
        if target == "sealed" and (checkpoint is None or checkpoint.state != "sealed"):
            _fail("INVALID_RECEIPT")
        if target == "awaiting_seal" and (
            checkpoint is None or checkpoint.state != "verifying"
        ):
            _fail("INVALID_RECEIPT")
        return checkpoint

    def transition_with_lease(
        self,
        lease: DispatchLease,
        target_state: str,
        *,
        lock: OperationDispatchLock | None = None,
        now: float,
        receipt: ReceiptCheckpoint | None = None,
        outcome_code: str = "",
    ) -> MobilityOperation:
        """Commit one allowed dispatching transition behind the current lease fence."""
        lease = self._verify_lease_shape(lease)
        self._require_lock(lock, self, lease.operation_id)
        timestamp = _now(now)
        if target_state not in LEASE_TRANSITIONS:
            _fail("INVALID_TRANSITION")
        if outcome_code not in _OUTCOME_CODES:
            _fail("INVALID_OUTCOME")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._record(
                    self._fetch(connection, lease.operation_id, lease.source_owner_id)
                )
                checkpoint = self._validate_checkpoint(current, receipt, target_state)
                receipt_values = current.receipt if checkpoint is None else checkpoint
                if checkpoint is not None:
                    receipt_expiry = checkpoint.expires_at
                else:
                    receipt_expiry = current.receipt_expires_at
                changed = connection.execute(
                    """UPDATE mobility_operations
                       SET state=?, outcome_code=?, lease_token=NULL, lease_expires_at=NULL,
                           updated_at=?, receipt_expires_at=?, receiver_transfer_id=?,
                           receiver_artifact_id=?, receipt_state=?, receipt_offset=?,
                           receipt_chunk_bytes=?, receipt_revision=?
                       WHERE operation_id=? AND source_owner_id=? AND state='dispatching'
                         AND attempt_epoch=? AND lease_token=? AND lease_expires_at>?""",
                    (
                        target_state,
                        outcome_code,
                        timestamp,
                        receipt_expiry,
                        None if receipt_values is None else receipt_values.transfer_id,
                        None if receipt_values is None else receipt_values.artifact_id,
                        None if receipt_values is None else receipt_values.state,
                        None if receipt_values is None else receipt_values.offset,
                        None if receipt_values is None else receipt_values.chunk_bytes,
                        None if receipt_values is None else receipt_values.revision,
                        lease.operation_id,
                        lease.source_owner_id,
                        lease.epoch,
                        lease.token,
                        timestamp,
                    ),
                ).rowcount
                if changed != 1:
                    _fail("LEASE_LOST")
                result = self._record(
                    self._fetch(connection, lease.operation_id, lease.source_owner_id)
                )
                if result.state in TERMINAL_STATES:
                    self._insert_tombstone(connection, result, timestamp)
                return result
        except MobilityJournalError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            _fail("UNAVAILABLE")

    def assert_immutable_fence(
        self,
        lease: DispatchLease,
        current: MobilityImmutableFence,
        *,
        lock: OperationDispatchLock | None = None,
        now: float,
    ) -> MobilityOperation:
        """Terminally fence a changed source/destination identity before transport."""
        lease = self._verify_lease_shape(lease)
        self._require_lock(lock, self, lease.operation_id)
        if not isinstance(current, MobilityImmutableFence):
            _fail("INVALID_REQUEST")
        timestamp = _now(now)
        fenced = False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                operation = self._record(
                    self._fetch(connection, lease.operation_id, lease.source_owner_id)
                )
                matches = (
                    operation.source_owner_id == current.source_owner_id
                    and operation.source_scope_id == current.source_scope_id
                    and operation.source_artifact_id == current.source_artifact_id
                    and operation.immutable_spec == current.immutable_spec
                    and operation.destination_scope_id == current.destination_scope_id
                    and operation.credential_generation == current.credential_generation
                    and hmac.compare_digest(
                        operation.destination_binding_hmac,
                        current.destination_binding_hmac,
                    )
                )
                if matches:
                    # This query is also the pre-peer lease proof.  The caller
                    # still renews immediately before every later peer call.
                    if (
                        operation.state != "dispatching"
                        or operation.attempt_epoch != lease.epoch
                        or operation.lease_token is None
                        or not hmac.compare_digest(operation.lease_token, lease.token)
                        or operation.lease_expires_at is None
                        or operation.lease_expires_at <= timestamp
                    ):
                        _fail("LEASE_LOST")
                    return operation
                changed = connection.execute(
                    """UPDATE mobility_operations
                       SET state='terminal_blocked', outcome_code='IMMUTABLE_FENCE',
                           lease_token=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE operation_id=? AND source_owner_id=? AND state='dispatching'
                         AND attempt_epoch=? AND lease_token=? AND lease_expires_at>?""",
                    (
                        timestamp,
                        lease.operation_id,
                        lease.source_owner_id,
                        lease.epoch,
                        lease.token,
                        timestamp,
                    ),
                ).rowcount
                if changed != 1:
                    _fail("LEASE_LOST")
                terminal = self._record(
                    self._fetch(connection, lease.operation_id, lease.source_owner_id)
                )
                self._insert_tombstone(connection, terminal, timestamp)
                fenced = True
            if fenced:
                _fail("IMMUTABLE_FENCE")
        except MobilityJournalError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            _fail("UNAVAILABLE")

    def recover_expired_leases(self, *, now: float) -> tuple[str, ...]:
        """Explicitly recover only expired local dispatch leases; never do I/O."""
        timestamp = _now(now)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """SELECT operation_id FROM mobility_operations
                       WHERE state='dispatching' AND lease_expires_at<=?
                       ORDER BY operation_id LIMIT 256""",
                    (timestamp,),
                ).fetchall()
                recovered = tuple(row["operation_id"] for row in rows)
                if recovered:
                    placeholders = ",".join("?" for _ in recovered)
                    changed = connection.execute(
                        f"""UPDATE mobility_operations SET state='resumable', lease_token=NULL,
                            lease_expires_at=NULL, updated_at=?
                            WHERE state='dispatching' AND lease_expires_at<=?
                              AND operation_id IN ({placeholders})""",
                        (timestamp, timestamp, *recovered),
                    ).rowcount
                    if changed != len(recovered):
                        _fail("UNAVAILABLE")
                return recovered
        except MobilityJournalError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            _fail("UNAVAILABLE")

    def prune_receipt_keep_tombstone(
        self, operation_id: str, source_owner_id: str, *, now: float
    ) -> None:
        """Explicitly remove detailed terminal receipt data, never its tombstone."""
        identity = _operation_id(operation_id)
        owner = _owner(source_owner_id)
        timestamp = _now(now)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_owner(connection, owner)
                operation = self._record(self._fetch(connection, identity, owner))
                if operation.state not in TERMINAL_STATES:
                    _fail("INVALID_TRANSITION")
                if operation.receipt_expires_at > timestamp:
                    _fail("RECEIPT_LIVE")
                self._insert_tombstone(connection, operation, timestamp)
                if (
                    connection.execute(
                        """DELETE FROM mobility_operations
                       WHERE operation_id=? AND source_owner_id=? AND state=?""",
                        (identity, owner, operation.state),
                    ).rowcount
                    != 1
                ):
                    _fail("UNAVAILABLE")
        except MobilityJournalError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            _fail("UNAVAILABLE")

    def tombstone_exists(
        self, source_owner_id: str, destination_scope_id: str, operation_id: str
    ) -> bool:
        owner = _owner(source_owner_id)
        identity = _operation_id(operation_id)
        if not isinstance(destination_scope_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", destination_scope_id
        ):
            _fail("NOT_FOUND")
        with self._connection() as connection:
            self._ensure_owner(connection, owner)
            return (
                connection.execute(
                    """SELECT 1 FROM mobility_tombstones
                   WHERE source_owner_id=? AND destination_scope_id=? AND operation_id=?""",
                    (owner, destination_scope_id, identity),
                ).fetchone()
                is not None
            )


__all__ = ["OperationDispatchLock", "SQLiteArtifactMobilityJournal"]
