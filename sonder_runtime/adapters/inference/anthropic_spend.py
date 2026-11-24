"""Persistent per-day Anthropic spend, backing ``SONDER_ANTHROPIC_DAILY_USD``.

One small JSON document in the Sonder state home (``anthropic-spend.json``)
keyed by UTC calendar day.  Reads and read-modify-write updates are
serialized across threads and processes with the shared advisory file lock, and
written with the shared atomic-replace helper, so the running total survives a
restart and a second process sees the first one's spend.

The document holds dollar totals, call and token counts and the number of
calls each key *slot* (1 or 2) served -- never a key, a prompt or a reply.

Failure policy.  A document that exists but cannot be read is not "zero
spend": :meth:`SpendLedger.today_usd` raises, and the gateway refuses new calls
until the operator repairs it.  Recording a call that was already made never
raises (the response is billable and must still be returned); if that write
fails the amount is kept in memory for this process and logged.

Thread affinity: any thread.  Ownership: the gateway owns one ledger; nothing
here holds a file open between calls.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ...domain.common.errors import DependencyUnavailable
from ..filesystem.atomic_json import file_lock, write_json_atomic
from ..process_liveness import pid_alive

logger = logging.getLogger(__name__)

SCHEMA = "sonder.anthropic_spend.v1"
FILENAME = "anthropic-spend.json"
KEEP_DAYS = 62
_MAX_USD = Decimal(1_000_000_000)
_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_RESERVATION_TTL_SECONDS = 900.0


class SpendLedgerUnreadable(DependencyUnavailable):
    """The persisted spend document exists but is unreadable or invalid."""


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def _default_path(filename: str = FILENAME) -> Path:
    from ...platform.paths import state_path

    return Path(state_path(filename))


def _decimal(value: object, label: str = "Anthropic") -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SpendLedgerUnreadable("%s spend ledger has an invalid amount" % label) from exc
    if not number.is_finite() or number < 0 or number > _MAX_USD:
        raise SpendLedgerUnreadable("%s spend ledger has an out-of-range amount" % label)
    return number


class SpendLedger:
    """Daily USD total for one state home; cheap to construct, lazy on disk."""

    def __init__(
        self, path: Path | str | Callable[[], Path | str] | None = None,
        *, clock: Callable[[], float] = time.time,
        filename: str = FILENAME, schema: str = SCHEMA, label: str = "Anthropic",
        reservation_ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
    ) -> None:
        self._path_source = path
        self._filename = filename
        self._schema = schema
        self._label = label
        self._clock = clock
        self._reservation_ttl = max(1.0, float(reservation_ttl_seconds))
        self._instance_id = uuid.uuid4().hex[:12]
        self._thread_lock = threading.Lock()
        # Spend that could not be persisted, by day: still counted in-process.
        self._unpersisted: dict[str, Decimal] = {}
        # Active reservations held by this ledger: token -> (day, key, amount, persisted).
        self._inflight: dict[int, tuple[str, str, Decimal, bool]] = {}
        self._next_token = 1

    @property
    def path(self) -> Path:
        source = self._path_source
        if source is None:
            return _default_path(self._filename)
        return Path(source() if callable(source) else source)

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"schema": self._schema, "days": {}}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise SpendLedgerUnreadable(
                "the %s spend ledger %s cannot be read; repair or remove it "
                "to resume %s calls" % (self._label, path.name, self._label)
            ) from exc
        days = document.get("days") if isinstance(document, dict) else None
        if not isinstance(days, dict):
            raise SpendLedgerUnreadable(
                "the %s spend ledger %s is not a valid ledger; repair or "
                "remove it to resume %s calls" % (self._label, path.name, self._label)
            )
        return {"schema": self._schema, "days": days}

    def _day_entry(self, document: dict[str, Any], day: str) -> dict[str, Any]:
        entry = document["days"].get(day)
        return entry if isinstance(entry, dict) else {}

    def _prune_reserved(self, entry: dict[str, Any], now: float) -> tuple[dict[str, dict[str, Any]], bool]:
        raw = entry.get("reserved")
        if not isinstance(raw, dict):
            return {}, False
        kept: dict[str, dict[str, Any]] = {}
        changed = False
        current_pid = os.getpid()
        for key, item in raw.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                changed = True
                continue
            try:
                usd = _decimal(item.get("usd", "0"), self._label)
                expires_at = float(item.get("expires_at", 0.0))
                pid = int(item.get("pid", 0))
            except (SpendLedgerUnreadable, TypeError, ValueError):
                changed = True
                continue
            if expires_at <= now or usd <= 0:
                changed = True
                continue
            if pid != current_pid and (pid <= 0 or not pid_alive(pid)):
                changed = True
                continue
            kept[key] = {"usd": str(usd), "pid": pid, "expires_at": expires_at}
        return kept, changed

    def _today_locked(self) -> Decimal:
        """Today's spend; the caller holds ``_thread_lock``."""
        day = utc_day(self._clock())
        path = self.path
        with file_lock(path, timeout=_LOCK_TIMEOUT_SECONDS):
            stored = _decimal(self._day_entry(self._read(path), day).get("usd", "0"), self._label)
        return stored + self._unpersisted.get(day, Decimal(0))

    def today_usd(self) -> Decimal:
        """Spend recorded for the current UTC day (persisted plus unpersisted)."""
        with self._thread_lock:
            return self._today_locked()

    def try_reserve(self, amount: Decimal, cap: Decimal) -> tuple[int | None, Decimal, Decimal]:
        """Atomically refuse over the cap, else hold ``amount`` for a call in flight.

        Persists positive reservations in the file-locked ledger so concurrent
        processes and separate ``SpendLedger`` instances sharing one state home
        observe each other's in-flight reservations.  Stale reservations whose
        TTL expired or whose owning PID is dead are pruned under the lock.
        """
        now = self._clock()
        day = utc_day(now)
        path = self.path
        with self._thread_lock:
            with file_lock(path, timeout=_LOCK_TIMEOUT_SECONDS):
                document = self._read(path)
                days = document["days"]
                entry = dict(self._day_entry(document, day))
                stored = _decimal(entry.get("usd", "0"), self._label)
                spent = stored + self._unpersisted.get(day, Decimal(0))
                active_reserved, pruned = self._prune_reserved(entry, now)
                persisted_inflight = sum(
                    (_decimal(item["usd"], self._label) for item in active_reserved.values()),
                    Decimal(0),
                )
                local_unpersisted = sum(
                    (amt for d, key, amt, persisted in self._inflight.values()
                     if d == day and (not persisted or key not in active_reserved)),
                    Decimal(0),
                )
                inflight = persisted_inflight + local_unpersisted
                if spent + inflight + amount > cap:
                    if pruned and path.exists():
                        if active_reserved:
                            entry["reserved"] = active_reserved
                        else:
                            entry.pop("reserved", None)
                        days[day] = entry
                        try:
                            write_json_atomic(path, document)
                        except OSError:
                            pass
                    return None, spent, inflight
                token = self._next_token
                self._next_token += 1
                res_key = "%d:%s:%d" % (os.getpid(), self._instance_id, token)
                persisted = False
                if amount > 0:
                    active_reserved[res_key] = {
                        "usd": str(amount),
                        "pid": os.getpid(),
                        "expires_at": now + self._reservation_ttl,
                    }
                    entry["reserved"] = active_reserved
                    days[day] = entry
                    for old in sorted(days)[:-KEEP_DAYS]:
                        days.pop(old, None)
                    try:
                        write_json_atomic(path, document)
                        persisted = True
                    except OSError as exc:
                        logger.error(
                            "%s: could not persist in-flight reservation (%s); failing closed",
                            self._label.lower(), type(exc).__name__,
                        )
                        raise SpendLedgerUnreadable(
                            "the %s spend ledger %s could not persist in-flight reservation; "
                            "failing closed to enforce the daily cap"
                            % (self._label, path.name)
                        ) from exc
                self._inflight[token] = (day, res_key, amount, persisted)
                return token, spent, inflight

    def release(self, token: int) -> None:
        with self._thread_lock:
            record = self._inflight.pop(token, None)
            if record is None:
                return
            day, res_key, _amount, persisted = record
            if not persisted:
                return
            path = self.path
            try:
                with file_lock(path, timeout=_LOCK_TIMEOUT_SECONDS):
                    if not path.exists():
                        return
                    document = self._read(path)
                    days = document["days"]
                    entry = dict(self._day_entry(document, day))
                    reserved = entry.get("reserved")
                    if isinstance(reserved, dict) and res_key in reserved:
                        reserved = dict(reserved)
                        reserved.pop(res_key, None)
                        if reserved:
                            entry["reserved"] = reserved
                        else:
                            entry.pop("reserved", None)
                        days[day] = entry
                        write_json_atomic(path, document)
            except (OSError, SpendLedgerUnreadable):
                pass

    def snapshot(self) -> dict[str, object]:
        """Today's content-free totals for status surfaces."""
        day = utc_day(self._clock())
        path = self.path
        with self._thread_lock:
            with file_lock(path, timeout=_LOCK_TIMEOUT_SECONDS):
                entry = self._day_entry(self._read(path), day)
            extra = self._unpersisted.get(day, Decimal(0))
        return {
            "day_utc": day,
            "usd": str(_decimal(entry.get("usd", "0"), self._label) + extra),
            "calls": int(entry.get("calls", 0) or 0),
            "key_slot_calls": {
                "1": int(entry.get("slot_1_calls", 0) or 0),
                "2": int(entry.get("slot_2_calls", 0) or 0),
            },
        }

    def record(
        self, *, usd: Decimal, slot: int, input_tokens: int, output_tokens: int,
    ) -> None:
        """Add one completed call; never raises (the call is already billed)."""
        day = utc_day(self._clock())
        amount = usd if usd.is_finite() and usd >= 0 else Decimal(0)
        try:
            path = self.path
            with self._thread_lock:
                with file_lock(path, timeout=_LOCK_TIMEOUT_SECONDS):
                    document = self._read(path)
                    days = document["days"]
                    entry = dict(self._day_entry(document, day))
                    entry["usd"] = str(_decimal(entry.get("usd", "0"), self._label) + amount)
                    entry["calls"] = int(entry.get("calls", 0) or 0) + 1
                    slot_key = "slot_%d_calls" % (2 if slot == 2 else 1)
                    entry[slot_key] = int(entry.get(slot_key, 0) or 0) + 1
                    entry["input_tokens"] = int(entry.get("input_tokens", 0) or 0) + max(0, input_tokens)
                    entry["output_tokens"] = int(entry.get("output_tokens", 0) or 0) + max(0, output_tokens)
                    days[day] = entry
                    for old in sorted(days)[:-KEEP_DAYS]:
                        days.pop(old, None)
                    write_json_atomic(path, document)
        except Exception as exc:  # noqa: BLE001 - a billed call is never lost to a ledger fault
            with self._thread_lock:
                self._unpersisted[day] = self._unpersisted.get(day, Decimal(0)) + amount
            logger.error(
                "%s: could not persist %s USD of spend (%s); counted in memory only",
                self._label.lower(), amount, type(exc).__name__,
            )


__all__ = ["FILENAME", "SpendLedger", "SpendLedgerUnreadable", "utc_day"]
