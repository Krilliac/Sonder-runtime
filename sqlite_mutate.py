"""Guarded single-statement SQLite mutations with preview rollback."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import secrets
import threading
import time

from sonder_runtime.adapters.inspection import data_query
import sonder_runtime.adapters.filesystem.file_ops as file_ops


MAX_SQL_BYTES = 32_768
MAX_PARAMETERS_BYTES = 128_000
MAX_PARAMETERS = 999
MAX_PARAMETER_BYTES = 64_000
DEFAULT_MAX_ROWS = 1_000
MAX_ROWS = 10_000
DEFAULT_TIMEOUT_SECONDS = 2.0
MAX_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_DB_BYTES = 64 * 1024 * 1024
MAX_DB_BYTES = 256 * 1024 * 1024
PREVIEW_LEASE_TTL_SECONDS = 60.0
MAX_PREVIEW_LEASES = 32
_MUTATION_ACTIONS = {
    "INSERT": sqlite3.SQLITE_INSERT,
    "UPDATE": sqlite3.SQLITE_UPDATE,
    "DELETE": sqlite3.SQLITE_DELETE,
}


class SqliteMutateError(RuntimeError):
    pass


# A preview is normally advisory: a later apply opens a new SQLite connection,
# and SQLite's data_version is deliberately connection-local.  When callers
# opt into this short-lived lease, keep the preview connection alive so its
# data_version can detect a commit by any *other* connection before apply.
# This is deliberately bounded and in-memory only; it is not a durable work
# queue or an authorization grant.
_preview_lease_lock = threading.Lock()
_preview_leases = {}


def _close_preview_lease(lease):
    try:
        lease["conn"].close()
    except sqlite3.Error:
        pass


def _purge_preview_leases(now=None):
    now = time.monotonic() if now is None else now
    expired = []
    with _preview_lease_lock:
        for token, lease in list(_preview_leases.items()):
            if lease["expires_at"] <= now:
                expired.append(_preview_leases.pop(token))
    for lease in expired:
        _close_preview_lease(lease)


def _preview_binding(target, identity, sql, parameters, max_rows, timeout, max_db_bytes):
    value = {
        "path": str(target), "identity": list(identity), "sql": sql,
        "parameters": parameters, "max_rows": max_rows, "timeout": timeout,
        "max_db_bytes": max_db_bytes,
    }
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _store_preview_lease(conn, binding, data_version):
    _purge_preview_leases()
    token = secrets.token_urlsafe(32)
    lease = {
        "conn": conn, "binding": binding, "data_version": data_version,
        "expires_at": time.monotonic() + PREVIEW_LEASE_TTL_SECONDS,
    }
    evicted = None
    with _preview_lease_lock:
        # Tokens are random, but do not silently overwrite a live lease if a
        # provider or test double ever produces a collision.
        while token in _preview_leases:
            token = secrets.token_urlsafe(32)
        if len(_preview_leases) >= MAX_PREVIEW_LEASES:
            oldest = min(_preview_leases, key=lambda item: _preview_leases[item]["expires_at"])
            evicted = _preview_leases.pop(oldest)
        _preview_leases[token] = lease
    if evicted is not None:
        _close_preview_lease(evicted)
    return token


def _claim_preview_lease(token, binding):
    if not isinstance(token, str) or not token or len(token) > 256:
        raise SqliteMutateError("SQLite preview token is invalid, expired, or already used")
    _purge_preview_leases()
    with _preview_lease_lock:
        lease = _preview_leases.pop(token, None)
    if lease is None:
        raise SqliteMutateError("SQLite preview token is invalid, expired, or already used")
    if not secrets.compare_digest(lease["binding"], binding):
        _close_preview_lease(lease)
        raise SqliteMutateError("SQLite preview token does not match this mutation")
    return lease


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _bounded_timeout(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(0.05, min(value, MAX_TIMEOUT_SECONDS))


def _reject_constant(value):
    raise ValueError("non-finite parameter is not allowed: %s" % value)


def _parse_parameters(value):
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_PARAMETERS_BYTES:
            raise SqliteMutateError("parameters_json exceeds max input bytes")
        try:
            value = json.loads(value, parse_constant=_reject_constant)
        except (TypeError, ValueError) as exc:
            raise SqliteMutateError("parameters_json must be strict JSON") from exc
    else:
        try:
            encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SqliteMutateError("parameters must be JSON-compatible") from exc
        if len(encoded) > MAX_PARAMETERS_BYTES:
            raise SqliteMutateError("parameters_json exceeds max input bytes")
    if not isinstance(value, list) or not value:
        raise SqliteMutateError("parameters_json must be a non-empty positional JSON array")
    if len(value) > MAX_PARAMETERS:
        raise SqliteMutateError("parameters exceed max count (%d)" % MAX_PARAMETERS)
    parameters = []
    for index, item in enumerate(value):
        if item is None or isinstance(item, bool):
            parameters.append(item)
        elif isinstance(item, int):
            if item < -(1 << 63) or item > (1 << 63) - 1:
                raise SqliteMutateError(
                    "parameter %d integer exceeds SQLite signed 64-bit range" % index
                )
            parameters.append(item)
        elif isinstance(item, float) and math.isfinite(item):
            parameters.append(item)
        elif isinstance(item, str):
            if len(item.encode("utf-8")) > MAX_PARAMETER_BYTES:
                raise SqliteMutateError("parameter %d exceeds max bytes" % index)
            parameters.append(item)
        else:
            raise SqliteMutateError(
                "parameter %d must be null, boolean, number, or string" % index
            )
    return parameters


def _scan_sql(sql):
    if not isinstance(sql, str) or not sql.strip() or "\x00" in sql:
        raise SqliteMutateError("sql must be one non-empty statement")
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise SqliteMutateError("SQL exceeds max statement bytes (%d)" % MAX_SQL_BYTES)
    words = []
    placeholders = 0
    semicolon = False
    position = 0
    length = len(sql)
    while position < length:
        char = sql[position]
        if char.isspace():
            position += 1
            continue
        if sql.startswith("--", position):
            newline = sql.find("\n", position + 2)
            position = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", position):
            end = sql.find("*/", position + 2)
            if end < 0:
                raise SqliteMutateError("SQL contains an unterminated comment")
            position = end + 2
            continue
        if semicolon:
            raise SqliteMutateError("exactly one SQL statement is allowed")
        if char in "'\"`[":
            closing = "]" if char == "[" else char
            position += 1
            while position < length:
                if sql[position] == closing:
                    if closing != "]" and position + 1 < length and sql[position + 1] == closing:
                        position += 2
                        continue
                    position += 1
                    break
                position += 1
            else:
                raise SqliteMutateError("SQL contains an unterminated quoted value")
            continue
        if char == ";":
            semicolon = True
            position += 1
            continue
        if char == "?":
            if position + 1 < length and sql[position + 1].isdigit():
                raise SqliteMutateError("numbered parameters are not allowed; use bare ?")
            placeholders += 1
            position += 1
            continue
        if char in ":@$" and position + 1 < length and (
            sql[position + 1].isalnum() or sql[position + 1] == "_"
        ):
            raise SqliteMutateError("named parameters are not allowed; use bare ?")
        if char.isalpha() or char == "_":
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", sql[position:])
            words.append(match.group(0).upper())
            position += match.end()
            continue
        position += 1
    if not words or words[0] not in _MUTATION_ACTIONS:
        raise SqliteMutateError("SQL must start with exactly INSERT, UPDATE, or DELETE")
    if "RETURNING" in words:
        raise SqliteMutateError("RETURNING is not supported")
    if words[0] in {"INSERT", "UPDATE"} and "REPLACE" in words:
        raise SqliteMutateError("REPLACE conflict actions are not supported")
    return words[0], placeholders


def _resolve_target(path, *, extra_roots, bypass):
    try:
        requested = file_ops._requested_path(str(path or ""))
        file_ops._require_no_reparse_components(requested)
        target = data_query._resolve_target(
            path, extra_roots=extra_roots, bypass=bypass,
        )
        file_ops._require_mutation_access(target, False)
    except (OSError, PermissionError, TypeError, ValueError, data_query.DataQueryError) as exc:
        raise SqliteMutateError("SQLite mutation path rejected: %s" % exc) from exc
    if target.suffix.casefold() not in {".db", ".sqlite", ".sqlite3"}:
        raise SqliteMutateError("SQLite mutation target must use .db, .sqlite, or .sqlite3")
    if file_ops._is_protected_read_path(target) or file_ops._is_protected_mutation_path(target):
        raise SqliteMutateError("SQLite mutation target is secret or control state")
    return requested, target


def _storage_bytes(target: Path) -> int:
    total = 0
    for candidate in (target, Path(str(target) + "-wal"), Path(str(target) + "-journal")):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if file_ops._is_reparse_point(candidate) or not stat.S_ISREG(metadata.st_mode):
            raise SqliteMutateError("SQLite database sidecar must be a regular non-symlink file")
        total += metadata.st_size
    return total


def _authorizer(statement, virtual_tables):
    wanted = _MUTATION_ACTIONS[statement]
    state = {"table": ""}
    allowed_reads = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT}

    def authorize(action, arg1, arg2, database, trigger):
        table = str(arg1 or "")
        if trigger or str(database or "main").casefold() != "main":
            return sqlite3.SQLITE_DENY
        if action == wanted:
            lowered = table.casefold()
            if not table or lowered.startswith("sqlite_") or lowered in virtual_tables:
                return sqlite3.SQLITE_DENY
            if state["table"] and state["table"] != lowered:
                return sqlite3.SQLITE_DENY
            state["table"] = lowered
            return sqlite3.SQLITE_OK
        if action in allowed_reads:
            if table.casefold().startswith("sqlite_") or table.casefold() in virtual_tables:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        # Includes every function, PRAGMA, ATTACH/DETACH, DDL, transaction,
        # savepoint, virtual-table, and mismatched DML action.
        return sqlite3.SQLITE_DENY

    return authorize, state


def _restricted_table_names(conn) -> set[str]:
    """Return virtual and implementation-shadow tables, or fail closed."""
    try:
        rows = conn.execute("PRAGMA table_list").fetchall()
    except sqlite3.Error as exc:
        raise SqliteMutateError(
            "SQLite cannot classify virtual and shadow tables safely: %s" % exc
        ) from exc
    if not rows or any(len(row) < 3 for row in rows):
        raise SqliteMutateError(
            "SQLite cannot classify virtual and shadow tables safely"
        )
    return {
        str(row[1]).casefold()
        for row in rows
        if str(row[0]).casefold() == "main"
        and str(row[2]).casefold() in {"virtual", "shadow"}
    }


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise SqliteMutateError("SQLite mutation exceeded the timeout ceiling")


def mutate_sqlite(path, sql, parameters, *, mode="preview",
                  max_rows=DEFAULT_MAX_ROWS, timeout=DEFAULT_TIMEOUT_SECONDS,
                  max_db_bytes=DEFAULT_MAX_DB_BYTES, preview_token="",
                  extra_roots="", bypass=False):
    mode = str(mode or "preview").strip().lower()
    if mode not in {"preview", "apply"}:
        raise SqliteMutateError("mode must be preview or apply")
    statement, placeholders = _scan_sql(sql)
    parameters = _parse_parameters(parameters)
    if placeholders != len(parameters):
        raise SqliteMutateError(
            "bare ? placeholder count (%d) must equal parameter count (%d)"
            % (placeholders, len(parameters))
        )
    max_rows = _bounded_int(max_rows, DEFAULT_MAX_ROWS, 1, MAX_ROWS)
    timeout = _bounded_timeout(timeout)
    max_db_bytes = _bounded_int(
        max_db_bytes, DEFAULT_MAX_DB_BYTES, 1024, MAX_DB_BYTES,
    )
    requested, target = _resolve_target(path, extra_roots=extra_roots, bypass=bypass)
    storage_before = _storage_bytes(target)
    if storage_before > max_db_bytes:
        raise SqliteMutateError("SQLite database exceeds the configured size ceiling")
    initial = target.stat()
    identity = (initial.st_dev, initial.st_ino)
    binding = _preview_binding(
        target, identity, sql, parameters, max_rows, timeout, max_db_bytes,
    )
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    uri = target.as_uri() + "?mode=rw"
    lease = None
    retained_preview = False
    if mode == "apply" and preview_token:
        lease = _claim_preview_lease(preview_token, binding)
        conn = lease["conn"]
    else:
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
        except sqlite3.Error as exc:
            raise SqliteMutateError("SQLite open failed: %s" % exc) from exc
    transaction = False
    try:
        conn.execute("PRAGMA busy_timeout=0")
        # Preserve declared relational constraints. Cross-table cascades are
        # still denied by the authorizer's single-target-table contract.
        conn.execute("PRAGMA foreign_keys=ON")
        # Prevent dirty WAL pages from spilling before the projection below;
        # commit then appends at most one frame per changed logical page.
        conn.execute("PRAGMA cache_spill=OFF")
        database_path = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
        opened = target.stat()
        if database_path != target or (opened.st_dev, opened.st_ino) != identity:
            raise SqliteMutateError("SQLite target identity changed during open")
        restricted_tables = _restricted_table_names(conn)
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count_before = int(conn.execute("PRAGMA page_count").fetchone()[0])
        if page_count_before * page_size > max_db_bytes:
            raise SqliteMutateError("SQLite logical database exceeds size ceiling")
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, MAX_PARAMETERS)
            # SQLITE_LIMIT_LENGTH governs complete encoded rows/blobs, not
            # bound parameters. Parameter bytes are already validated above.
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max_db_bytes)
        conn.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 100)
        conn.execute("BEGIN IMMEDIATE")
        transaction = True
        if lease is not None:
            # Check after acquiring the writer lock: any external writer has
            # either committed (and advances this retained connection's
            # data_version) or prevents BEGIN IMMEDIATE.  There is then no
            # race between this check and the guarded mutation below.
            current_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            if current_version != lease["data_version"]:
                raise SqliteMutateError("SQLite preview is stale; rerun preview")
        authorize, state = _authorizer(statement, restricted_tables)
        conn.set_authorizer(authorize)
        before_changes = conn.total_changes
        try:
            conn.execute(sql, parameters)
        except sqlite3.Error as exc:
            raise SqliteMutateError("SQLite mutation rejected: %s" % exc) from exc
        finally:
            conn.set_authorizer(None)
        _check_deadline(deadline)
        affected = conn.total_changes - before_changes
        if not state["table"]:
            raise SqliteMutateError("SQLite statement did not authorize a target table")
        if affected > max_rows:
            raise SqliteMutateError(
                "SQLite mutation exceeds row ceiling (%d)" % max_rows
            )
        storage_during = _storage_bytes(target)
        if storage_during > max_db_bytes:
            raise SqliteMutateError("SQLite mutation exceeds database size ceiling")
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        if page_count * page_size > max_db_bytes:
            raise SqliteMutateError(
                "SQLite mutation exceeds logical database size ceiling"
            )
        if journal_mode == "wal":
            # Python's sqlite3 API does not expose SQLite's dirty-page set. A
            # same-value UPDATE can dirty a page even when the logical image is
            # byte-identical, so only the all-pages upper bound is fail-closed.
            projected_frames = page_count if affected else 0
            wal_path = Path(str(target) + "-wal")
            try:
                wal_bytes = wal_path.lstat().st_size
            except FileNotFoundError:
                wal_bytes = 0
            projected_storage = (
                storage_during
                + projected_frames * (page_size + 24)
                + (32 if projected_frames and wal_bytes == 0 else 0)
            )
            if projected_storage > max_db_bytes:
                raise SqliteMutateError(
                    "SQLite mutation exceeds projected WAL storage ceiling"
                )
        _check_deadline(deadline)
        file_ops._require_no_reparse_components(requested)
        if file_ops.resolve_path(str(path), extra_roots=extra_roots, bypass=bypass) != target:
            raise SqliteMutateError("SQLite target resolution changed before completion")
        current = target.stat()
        if (current.st_dev, current.st_ino) != identity:
            raise SqliteMutateError("SQLite target identity changed before completion")
        if mode == "apply":
            _check_deadline(deadline)
            conn.commit()
            transaction = False
            applied = True
        else:
            conn.rollback()
            transaction = False
            _check_deadline(deadline)
            applied = False
        storage_after = _storage_bytes(target)
        if not applied and storage_after > max_db_bytes:
            raise SqliteMutateError("SQLite database exceeds size ceiling after completion")
        if not applied:
            # A caller can opt into optimistic preview/apply fencing by
            # returning this opaque token with the exact same request.  The
            # connection remains idle (not in a transaction) until then.
            data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            preview_token = _store_preview_lease(conn, binding, data_version)
            retained_preview = True
        return {
            "ok": True,
            "path": str(target),
            "mode": mode,
            "applied": applied,
            "statement": statement,
            "table": state["table"],
            "rows_affected": affected,
            "database_bytes_before": storage_before,
            "database_bytes_after": storage_after,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
            "limits": {
                "rows": max_rows, "timeout_seconds": timeout,
                "database_bytes": max_db_bytes, "statement_bytes": MAX_SQL_BYTES,
                "parameters": MAX_PARAMETERS,
            },
            **({"preview_token": preview_token,
                "preview_token_expires_in_seconds": int(PREVIEW_LEASE_TTL_SECONDS)}
               if mode == "preview" else {}),
        }
    except SqliteMutateError:
        if transaction:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise
    except sqlite3.Error as exc:
        if transaction:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise SqliteMutateError("SQLite mutation failed: %s" % exc) from exc
    finally:
        try:
            conn.set_authorizer(None)
            conn.set_progress_handler(None, 0)
        except sqlite3.Error:
            pass
        if not retained_preview:
            conn.close()
