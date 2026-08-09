"""Guarded single-statement SQLite mutations with preview rollback."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

import data_query
import file_ops


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
_MUTATION_ACTIONS = {
    "INSERT": sqlite3.SQLITE_INSERT,
    "UPDATE": sqlite3.SQLITE_UPDATE,
    "DELETE": sqlite3.SQLITE_DELETE,
}


class SqliteMutateError(RuntimeError):
    pass


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
        if item is None or isinstance(item, (bool, int)):
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
    if words[0] == "INSERT" and "REPLACE" in words:
        raise SqliteMutateError("INSERT OR REPLACE is not supported")
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


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise SqliteMutateError("SQLite mutation exceeded the timeout ceiling")


def mutate_sqlite(path, sql, parameters, *, mode="preview",
                  max_rows=DEFAULT_MAX_ROWS, timeout=DEFAULT_TIMEOUT_SECONDS,
                  max_db_bytes=DEFAULT_MAX_DB_BYTES, extra_roots="", bypass=False):
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
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    uri = target.as_uri() + "?mode=rw"
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
        database_path = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
        opened = target.stat()
        if database_path != target or (opened.st_dev, opened.st_ino) != identity:
            raise SqliteMutateError("SQLite target identity changed during open")
        virtual_tables = {
            str(row[0]).casefold() for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND upper(sql) LIKE 'CREATE VIRTUAL TABLE%'"
            )
        }
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, MAX_PARAMETERS)
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_PARAMETER_BYTES)
        conn.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 100)
        conn.execute("BEGIN IMMEDIATE")
        transaction = True
        authorize, state = _authorizer(statement, virtual_tables)
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
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        if page_count * page_size > max_db_bytes:
            raise SqliteMutateError(
                "SQLite mutation exceeds logical database size ceiling"
            )
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
        conn.close()
