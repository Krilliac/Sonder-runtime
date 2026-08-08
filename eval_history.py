"""Persistent, identity-safe history for explicitly completed evaluations.

This module never runs an evaluation or calls a model.  It records aggregate
evidence supplied by an explicit caller and reports trends only within an exact
model + model digest + suite + suite version + suite digest identity.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time

import sonder_paths


SCHEMA = "sonder.eval-history.v1"
DEFAULT_FILENAME = "eval-history.jsonl"
DEFAULT_READ_BYTES = 8 * 1024 * 1024
MAX_READ_BYTES = 32 * 1024 * 1024
MAX_HISTORY_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_RECORDS = 10_000
MAX_RECORDS = 50_000
MAX_RECORD_BYTES = 16_384
LOCK_TIMEOUT_SECONDS = 5.0
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class HistoryError(ValueError):
    """Invalid history identity, result, storage, or lock state."""


def default_path() -> Path:
    configured = os.environ.get("SONDER_EVAL_HISTORY", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(sonder_paths.default_home()) / DEFAULT_FILENAME


def _text(value, label, limit):
    text = str(value or "").strip()
    if not text:
        raise HistoryError("%s is required" % label)
    if len(text) > limit or any(char in text for char in "\r\n\x00"):
        raise HistoryError("%s is invalid or exceeds %d characters" % (label, limit))
    return text


def _digest(value, label):
    digest = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(digest):
        raise HistoryError("%s must be a 64-character SHA-256 digest" % label)
    return digest


def make_identity(model, model_digest, suite, suite_version, suite_digest):
    identity = {
        "model": _text(model, "model", 256),
        "model_digest": _digest(model_digest, "model_digest"),
        "suite": _text(suite, "suite", 128),
        "suite_version": _text(suite_version, "suite_version", 128),
        "suite_digest": _digest(suite_digest, "suite_digest"),
    }
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return identity, hashlib.sha256(encoded).hexdigest()


def make_record(
    *, model, model_digest, suite, suite_version, suite_digest,
    passed, total, recorded_at=None, source="manual",
):
    identity, identity_key = make_identity(
        model, model_digest, suite, suite_version, suite_digest,
    )
    if type(passed) is not int or type(total) is not int:
        raise HistoryError("passed and total must be integers")
    if total <= 0 or passed < 0 or passed > total:
        raise HistoryError("result must satisfy 0 <= passed <= total and total > 0")
    timestamp = time.time() if recorded_at is None else recorded_at
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise HistoryError("recorded_at must be a finite non-negative number")
    timestamp = float(timestamp)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise HistoryError("recorded_at must be a finite non-negative number")
    source = _text(source, "source", 128)
    record = {
        "schema": SCHEMA,
        "recorded_at": timestamp,
        "identity": identity,
        "identity_key": identity_key,
        "result": {
            "passed": passed,
            "total": total,
            "pass_rate": passed / total,
        },
        "source": source,
    }
    canonical = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    record["record_id"] = hashlib.sha256(canonical).hexdigest()
    return record


def validate_record(value):
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise HistoryError("unsupported or missing evaluation history schema")
    identity = value.get("identity")
    result = value.get("result")
    if not isinstance(identity, dict) or not isinstance(result, dict):
        raise HistoryError("record identity and result must be objects")
    normalized = make_record(
        model=identity.get("model"),
        model_digest=identity.get("model_digest"),
        suite=identity.get("suite"),
        suite_version=identity.get("suite_version"),
        suite_digest=identity.get("suite_digest"),
        passed=result.get("passed"),
        total=result.get("total"),
        recorded_at=value.get("recorded_at"),
        source=value.get("source"),
    )
    if value.get("identity_key") != normalized["identity_key"]:
        raise HistoryError("evaluation identity key does not match its fields")
    if value.get("record_id") != normalized["record_id"]:
        raise HistoryError("evaluation record id does not match its contents")
    stored_rate = result.get("pass_rate")
    if (
        isinstance(stored_rate, bool)
        or not isinstance(stored_rate, (int, float))
        or not math.isclose(float(stored_rate), normalized["result"]["pass_rate"])
    ):
        raise HistoryError("pass_rate does not match passed / total")
    return normalized


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


@contextmanager
def _history_lock(path, timeout=LOCK_TIMEOUT_SECONDS):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + max(0.1, float(timeout))
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for evaluation history lock")
                time.sleep(0.025)
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _atomic_append(path, line):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = line.encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise HistoryError("evaluation history record exceeds byte ceiling")
    existing_size = path.stat().st_size if path.exists() else 0
    append_overhead = 1
    if existing_size:
        with path.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            if existing.read(1) != b"\n":
                append_overhead += 1
    if existing_size + append_overhead + len(encoded) > MAX_HISTORY_BYTES:
        raise HistoryError(
            "evaluation history would exceed the %d-byte append ceiling"
            % MAX_HISTORY_BYTES
        )
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as output:
            last = b""
            if path.exists():
                with path.open("rb") as source:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        last = chunk[-1:]
            if last and last != b"\n":
                output.write(b"\n")
            output.write(encoded)
            output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def record_result(path=None, **fields):
    """Atomically append one explicit, precomputed evaluation result."""
    target = Path(path) if path is not None else default_path()
    record = make_record(**fields)
    line = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    with _history_lock(target):
        _atomic_append(target, line)
    return record


def load_history(path=None, *, max_records=DEFAULT_MAX_RECORDS,
                 max_bytes=DEFAULT_READ_BYTES):
    """Load a bounded valid window; malformed/truncated JSONL is counted."""
    target = Path(path) if path is not None else default_path()
    max_records = _bounded_int(max_records, DEFAULT_MAX_RECORDS, 1, MAX_RECORDS)
    max_bytes = _bounded_int(max_bytes, DEFAULT_READ_BYTES, 1024, MAX_READ_BYTES)
    records = deque(maxlen=max_records)
    malformed = 0
    discarded_valid = 0
    byte_truncated = False
    if not target.exists():
        return {
            "path": str(target), "records": [], "malformed": 0,
            "discarded_valid": 0, "truncated": False, "bytes_read": 0,
        }
    size = target.stat().st_size
    with target.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()
            byte_truncated = True
        start = handle.tell()
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            if len(stripped) > MAX_RECORD_BYTES:
                malformed += 1
                continue
            try:
                value = json.loads(stripped.decode("utf-8"))
                record = validate_record(value)
            except (HistoryError, UnicodeDecodeError, json.JSONDecodeError):
                malformed += 1
                continue
            if len(records) == records.maxlen:
                discarded_valid += 1
            records.append(record)
        bytes_read = handle.tell() - start
    return {
        "path": str(target),
        "records": list(records),
        "malformed": malformed,
        "discarded_valid": discarded_valid,
        "truncated": byte_truncated or discarded_valid > 0,
        "bytes_read": bytes_read,
    }


def history_status(
    path=None, *, model="", model_digest="", suite="", suite_version="",
    suite_digest="", tolerance=0.0, max_records=DEFAULT_MAX_RECORDS,
    max_bytes=DEFAULT_READ_BYTES,
):
    """Return per-exact-identity trends; never a promotion authorization."""
    loaded = load_history(path, max_records=max_records, max_bytes=max_bytes)
    filters = {
        "model": str(model or "").strip(),
        "model_digest": str(model_digest or "").strip().lower(),
        "suite": str(suite or "").strip(),
        "suite_version": str(suite_version or "").strip(),
        "suite_digest": str(suite_digest or "").strip().lower(),
    }
    try:
        tolerance = float(tolerance)
    except (TypeError, ValueError):
        tolerance = 0.0
    if not math.isfinite(tolerance):
        tolerance = 0.0
    tolerance = max(0.0, min(tolerance, 1.0))
    grouped = {}
    for record in loaded["records"]:
        identity = record["identity"]
        if any(value and identity[key] != value for key, value in filters.items()):
            continue
        grouped.setdefault(record["identity_key"], []).append(record)
    groups = []
    for key, records in grouped.items():
        records.sort(key=lambda item: (item["recorded_at"], item["record_id"]))
        latest = records[-1]
        prior = records[:-1]
        prior_best = max(
            (item["result"]["pass_rate"] for item in prior), default=None,
        )
        latest_rate = latest["result"]["pass_rate"]
        groups.append({
            "identity_key": key,
            "identity": latest["identity"],
            "samples": len(records),
            "latest": {
                "record_id": latest["record_id"],
                "recorded_at": latest["recorded_at"],
                "source": latest["source"],
                **latest["result"],
            },
            "best_pass_rate": max(
                item["result"]["pass_rate"] for item in records
            ),
            "prior_best_pass_rate": prior_best,
            "regressed": (
                prior_best is not None
                and latest_rate < prior_best - tolerance
            ),
            "tolerance": tolerance,
        })
    groups.sort(
        key=lambda item: (
            -item["latest"]["recorded_at"], item["identity_key"],
        )
    )
    return {
        "schema": SCHEMA,
        "path": loaded["path"],
        "read_only": True,
        "promotion_authority": False,
        "valid_records": len(loaded["records"]),
        "matched_records": sum(group["samples"] for group in groups),
        "malformed_records": loaded["malformed"],
        "discarded_valid_records": loaded["discarded_valid"],
        "truncated": loaded["truncated"],
        "bytes_read": loaded["bytes_read"],
        "groups": groups,
    }
