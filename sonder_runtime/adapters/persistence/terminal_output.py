"""Bounded private SQLite output storage for trusted host projection codecs."""

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import sqlite3
import stat

from ...application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ...application.ports.lane_continuation import ProjectionBinding
from ...application.ports.terminal_output import (
    DEFAULT_MAX_ROWS,
    DEFAULT_TOTAL_BYTES,
    MAX_OUTPUT_BYTES,
    TerminalOutputReference,
)


class SQLiteTerminalOutputStore:
    """No eviction: all rows remain charged until a separate retention protocol exists."""

    def __init__(
        self,
        private_directory,
        *,
        model_writable_roots=None,
        max_blob_bytes=MAX_OUTPUT_BYTES,
        max_total_bytes=DEFAULT_TOTAL_BYTES,
        max_rows=DEFAULT_MAX_ROWS
    ):
        if not callable(model_writable_roots):
            raise PermissionError("live workspace inventory required")
        for value, maximum in (
            (max_blob_bytes, MAX_OUTPUT_BYTES),
            (max_total_bytes, DEFAULT_TOTAL_BYTES),
            (max_rows, DEFAULT_MAX_ROWS),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("invalid terminal output limit")
        self.root = Path(private_directory).absolute()
        self.model_writable_roots = model_writable_roots
        self.max_blob_bytes = max_blob_bytes
        self.max_total_bytes = max_total_bytes
        self.max_rows = max_rows
        self._safe_root(())
        with PrivateDirectoryAnchor.open_base(self.root):
            pass

    def _safe_root(self, context_roots):
        roots = tuple(islice(self.model_writable_roots(), 257))
        if len(roots) > 256 or len(context_roots) > 256:
            raise PermissionError("workspace inventory exceeds bound")
        root = self.root.resolve()
        for entry in (*roots, *context_roots):
            allowed = Path(entry).resolve()
            if root == allowed or root in allowed.parents or allowed in root.parents:
                raise PermissionError("output store overlaps workspace")

    def _check(self, binding, context):
        if not isinstance(binding, ProjectionBinding):
            raise TypeError("typed projection binding required")
        if (
            context.principal_id != binding.principal_id
            or context.expired
            or context.cancellation.cancelled
        ):
            raise PermissionError("output authority unavailable")
        roots = tuple(Path(root).resolve() for root in context.workspace_roots)
        for entry in binding.project_roots:
            project = Path(entry).resolve()
            if not any(project == root or root in project.parents for root in roots):
                raise PermissionError("projection workspace outside authority")
        self._safe_root(context.workspace_roots)

    def _files(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = self.root / ("terminal-output.sqlite" + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or path.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise PermissionError("unsafe terminal output database")

    @contextmanager
    def _connection(self, binding, context):
        self._check(binding, context)
        with PrivateDirectoryAnchor(self.root) as anchor:
            self._files()
            conn = owned_sqlite_connect(self.root / "terminal-output.sqlite", timeout=1)
            try:
                self._files()
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA max_page_count=32768")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""CREATE TABLE IF NOT EXISTS terminal_outputs(
                    binding_sha TEXT PRIMARY KEY, binding BLOB NOT NULL,
                    payload BLOB NOT NULL, sha TEXT NOT NULL, size INTEGER NOT NULL)""")
                yield conn
                self._check(binding, context)
                anchor.validate()
                self._files()
                conn.commit()
                self._files()
                anchor.validate()
                if os.name != "nt":
                    os.fsync(anchor.fd)
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _binding(binding):
        payload = json.dumps(
            asdict(binding),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > 65536:
            raise ValueError("projection binding exceeds bound")
        return payload, hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _row(conn, binding_sha):
        lengths = conn.execute(
            "SELECT length(binding),length(payload) FROM terminal_outputs WHERE binding_sha=?",
            (binding_sha,),
        ).fetchone()
        if lengths is None:
            return None
        if not (0 < lengths[0] <= 65536 and 0 <= lengths[1] <= MAX_OUTPUT_BYTES):
            raise ValueError("terminal output persisted size invalid")
        return conn.execute(
            "SELECT binding,payload,sha,size FROM terminal_outputs WHERE binding_sha=?",
            (binding_sha,),
        ).fetchone()

    @staticmethod
    def _verify(row, binding_bytes, binding_sha):
        stored_binding, payload, sha, size = row
        if (
            stored_binding != binding_bytes
            or not isinstance(payload, bytes)
            or len(payload) > MAX_OUTPUT_BYTES
            or len(payload) != size
            or hashlib.sha256(payload).hexdigest() != sha
        ):
            raise ValueError("terminal output integrity failure")
        payload.decode("utf-8")
        return TerminalOutputReference(sha, size, binding_sha)

    def put(self, binding, output, *, context):
        self._check(binding, context)
        if not isinstance(output, str) or len(output) > self.max_blob_bytes:
            raise ValueError("terminal output exceeds bound")
        payload = output.encode("utf-8")
        if len(payload) > self.max_blob_bytes:
            raise ValueError("terminal output exceeds bound")
        binding_bytes, binding_sha = self._binding(binding)
        reference = TerminalOutputReference(
            hashlib.sha256(payload).hexdigest(), len(payload), binding_sha
        )
        with self._connection(binding, context) as conn:
            row = self._row(conn, binding_sha)
            if row is not None:
                prior = self._verify(row, binding_bytes, binding_sha)
                if prior != reference or row[1] != payload:
                    raise ValueError("terminal output immutable conflict")
            else:
                count, used = conn.execute(
                    "SELECT count(*),coalesce(sum(length(payload)+length(binding)+192),0) FROM terminal_outputs"
                ).fetchone()
                if (
                    count >= self.max_rows
                    or used + len(payload) + len(binding_bytes) + 192
                    > self.max_total_bytes
                ):
                    raise ValueError("terminal output quota exhausted")
                conn.execute(
                    "INSERT INTO terminal_outputs VALUES(?,?,?,?,?)",
                    (
                        binding_sha,
                        binding_bytes,
                        payload,
                        reference.sha256,
                        reference.size_bytes,
                    ),
                )
        return reference

    def get(self, binding, reference, *, context):
        self._check(binding, context)
        if not isinstance(reference, TerminalOutputReference):
            raise TypeError("typed terminal reference required")
        binding_bytes, binding_sha = self._binding(binding)
        if reference.binding_sha256 != binding_sha:
            raise PermissionError("terminal output binding mismatch")
        with self._connection(binding, context) as conn:
            row = self._row(conn, binding_sha)
            if row is None:
                raise KeyError("terminal output unavailable")
            if self._verify(row, binding_bytes, binding_sha) != reference:
                raise ValueError("terminal output reference mismatch")
            output = row[1].decode("utf-8")
        return output
