"""Persist the trusted root and highest accepted application metadata roles."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from ...application.updates.bounded_state import (
    MetadataChainError, TufLikeMetadata, TufLikeMetadataChain,
)


class SqliteMetadataLedger:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS trusted_metadata ("
                "repository TEXT NOT NULL, role TEXT NOT NULL, version INTEGER NOT NULL,"
                "digest TEXT NOT NULL, signer TEXT NOT NULL,"
                "PRIMARY KEY(repository, role))"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def pin_root(self, repository: str, root: TufLikeMetadata) -> None:
        """One-time operator trust enrollment; never replace a stored root."""
        if not repository or root.role != "root":
            raise MetadataChainError("a repository and root metadata are required")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT digest FROM trusted_metadata WHERE repository=? AND role='root'",
                (repository,),
            ).fetchone()
            if existing:
                if existing[0] != root.digest:
                    raise MetadataChainError("trusted root is already pinned")
                return
            conn.execute(
                "INSERT INTO trusted_metadata VALUES (?, 'root', ?, ?, ?)",
                (repository, root.version, root.digest, root.signer),
            )

    def accept(self, repository: str, chain: TufLikeMetadataChain,
               verifier: Callable[[bytes, str, str], bool]) -> None:
        if not repository:
            raise MetadataChainError("repository is required")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = {
                row[0]: row[1:]
                for row in conn.execute(
                    "SELECT role, version, digest, signer FROM trusted_metadata"
                    " WHERE repository=?", (repository,),
                )
            }
            if "root" not in prior:
                raise MetadataChainError("repository has no operator-pinned root")
            for entry in chain.entries:
                old = prior.get(entry.role)
                if old is not None:
                    old_version, old_digest, old_signer = old
                    if entry.version < old_version:
                        raise MetadataChainError(f"{entry.role} metadata replayed an older version")
                    if entry.version == old_version and entry.digest != old_digest:
                        raise MetadataChainError(f"{entry.role} metadata changed at the same version")
                    if entry.role == "root" and entry.version > old_version:
                        if entry.version != old_version + 1:
                            raise MetadataChainError("root rotation skipped a version")
                        if (entry.prior_signer != old_signer or
                                not verifier(entry.signing_bytes(), entry.prior_signature,
                                             old_signer)):
                            raise MetadataChainError("root rotation lacks prior root signature")
                conn.execute(
                    "INSERT INTO trusted_metadata VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(repository, role) DO UPDATE SET"
                    " version=excluded.version, digest=excluded.digest, signer=excluded.signer",
                    (repository, entry.role, entry.version, entry.digest, entry.signer),
                )


__all__ = ["SqliteMetadataLedger"]
