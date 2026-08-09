"""Crash-safe command receipt and result replay journal.

The journal sits immediately in front of a mutation dispatcher.  A caller must
persist :meth:`CommandJournal.receive` before dispatch, persist ``complete``
before returning a result, and accept ``acknowledge`` only after the client has
received that result.  An incomplete receipt found after restart is
``uncertain`` and is never automatically dispatched again.

Records are checksummed JSON lines.  Appends are serialized across processes,
flushed with ``fsync``, and compacted to a bounded canonical record set.
Acknowledgement is the protocol promise that a command will not be retried;
only a bounded recent tombstone set is retained after acknowledgement.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path


SCHEMA_VERSION = 1
MAX_CLIENT_ID = 64
MAX_COMMAND_ID = 128
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_EVENT_BYTES = 72 * 1024
DEFAULT_MAX_COMMANDS = 128
DEFAULT_COMPACT_AFTER_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _open_no_follow(path, flags, mode=0o600):
    """Open a journal-owned file without following POSIX symlinks."""
    return os.open(str(path), flags | getattr(os, "O_NOFOLLOW", 0), mode)


class CommandJournalError(RuntimeError):
    """Base class for a journal failure that must fail dispatch closed."""


class CommandConflict(CommandJournalError):
    """The command key was reused with a different request."""


class CommandJournalFull(CommandJournalError):
    """Unacknowledged commands filled the configured bounded journal."""


class CommandJournalCorrupt(CommandJournalError):
    """A complete on-disk record failed validation."""


def _canonical(value, *, limit, label):
    if not isinstance(value, dict):
        raise ValueError("%s must be a JSON object" % label)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s must contain bounded JSON values" % label) from exc
    if len(encoded) > limit:
        raise ValueError("%s exceeds %d encoded bytes" % (label, limit))
    return encoded


def _identifier(value, *, label, limit):
    value = str(value or "").strip()
    if len(value) > limit or not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            "%s must be 1-%d letters, numbers, or . _ : -" % (label, limit)
        )
    return value


def _checksum(event):
    body = dict(event)
    body.pop("checksum", None)
    return hashlib.sha256(_canonical(body, limit=MAX_EVENT_BYTES, label="event")).hexdigest()


class CommandJournal:
    """Durable receive/complete/ack state keyed by ``client_id+command_id``."""

    def __init__(
        self,
        path,
        *,
        max_commands=DEFAULT_MAX_COMMANDS,
        compact_after_bytes=DEFAULT_COMPACT_AFTER_BYTES,
    ):
        self.path = Path(path).expanduser().absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_commands = max(1, min(int(max_commands), 4096))
        self.compact_after_bytes = max(
            MAX_EVENT_BYTES, min(int(compact_after_bytes), 64 * 1024 * 1024)
        )
        self.instance_id = uuid.uuid4().hex
        self._thread_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for candidate in (self.path, self.lock_path):
            if candidate.is_symlink():
                raise ValueError("command journal paths must not be symbolic links")

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            descriptor = _open_no_follow(
                self.lock_path, os.O_RDWR | os.O_CREAT
            )
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(descriptor).st_size < 1:
                        os.write(descriptor, b"0")
                        os.fsync(descriptor)
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                acquired = True
                yield
            finally:
                try:
                    if acquired:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    @staticmethod
    def _apply(states, event):
        key = (event["client_id"], event["command_id"])
        kind = event["event"]
        if kind == "received":
            if key in states:
                raise CommandJournalCorrupt("duplicate command receipt")
            states[key] = {
                "client_id": key[0],
                "command_id": key[1],
                "request_sha256": event["request_sha256"],
                "owner_instance": event["owner_instance"],
                "received_ts": event["ts"],
                "state": "received",
                "result": None,
            }
        elif kind == "completed":
            current = states.get(key)
            if current is None or current["state"] not in {"received", "uncertain"}:
                raise CommandJournalCorrupt("completion has no open receipt")
            current["state"] = "completed"
            current["result"] = event["result"]
            current["completed_ts"] = event["ts"]
        elif kind == "uncertain":
            current = states.get(key)
            if current is None or current["state"] not in {"received", "uncertain"}:
                raise CommandJournalCorrupt("uncertain marker has no open receipt")
            current["state"] = "uncertain"
            current["uncertain_ts"] = event["ts"]
        elif kind == "acknowledged":
            current = states.get(key)
            if current is None or current["state"] != "completed":
                raise CommandJournalCorrupt("acknowledgement has no completion")
            current["state"] = "acknowledged"
            current["acknowledged_ts"] = event["ts"]
        else:
            raise CommandJournalCorrupt("unsupported command journal event")

    def _read_locked(self):
        states = {}
        if not self.path.exists():
            return states
        if self.path.is_symlink():
            raise CommandJournalCorrupt("command journal became a symbolic link")
        valid_end = 0
        descriptor = _open_no_follow(self.path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as stream:
            while True:
                raw = stream.readline(MAX_EVENT_BYTES + 2)
                if not raw:
                    break
                if len(raw) > MAX_EVENT_BYTES + 1:
                    raise CommandJournalCorrupt("command journal record is too large")
                if not raw.endswith(b"\n"):
                    # A crash can leave only the final append incomplete.  It
                    # is not a durable event and is removed before any append.
                    break
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CommandJournalCorrupt("invalid complete journal record") from exc
                if not isinstance(event, dict) or event.get("v") != SCHEMA_VERSION:
                    raise CommandJournalCorrupt("invalid command journal schema")
                try:
                    checksum = event.get("checksum")
                    if not isinstance(checksum, str) or checksum != _checksum(event):
                        raise CommandJournalCorrupt(
                            "command journal checksum mismatch"
                        )
                    event["client_id"] = _identifier(
                        event.get("client_id"), label="client_id", limit=MAX_CLIENT_ID
                    )
                    event["command_id"] = _identifier(
                        event.get("command_id"), label="command_id", limit=MAX_COMMAND_ID
                    )
                    self._apply(states, event)
                except (KeyError, TypeError, ValueError) as exc:
                    raise CommandJournalCorrupt("invalid command journal event") from exc
                valid_end = stream.tell()
        size = self.path.stat().st_size
        if valid_end != size:
            descriptor = _open_no_follow(self.path, os.O_RDWR)
            with os.fdopen(descriptor, "r+b") as stream:
                stream.truncate(valid_end)
                stream.flush()
                os.fsync(stream.fileno())
        return states

    def _event(self, kind, client_id, command_id, **fields):
        event = {
            "v": SCHEMA_VERSION,
            "event": kind,
            "client_id": client_id,
            "command_id": command_id,
            "ts": time.time(),
            **fields,
        }
        event["checksum"] = _checksum(event)
        encoded = _canonical(event, limit=MAX_EVENT_BYTES, label="event") + b"\n"
        return event, encoded

    def _append_locked(self, event, encoded):
        created = not self.path.exists()
        descriptor = _open_no_follow(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("command journal append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created and os.name != "nt":
            directory = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _compact_locked(self, states):
        unacknowledged = [
            row for row in states.values() if row["state"] != "acknowledged"
        ]
        # Keep a bounded recent tombstone set.  This makes a lost ACK response
        # retryable and prevents a recently acknowledged command ID from being
        # reused, without allowing acknowledgements to grow the file forever.
        acknowledged = sorted(
            (
                row for row in states.values()
                if row["state"] == "acknowledged"
            ),
            key=lambda item: item.get("acknowledged_ts", 0),
            reverse=True,
        )[:self.max_commands]
        kept = unacknowledged + acknowledged
        temporary = self.path.with_name(".%s.%s.tmp" % (self.path.name, uuid.uuid4().hex))
        descriptor = os.open(
            str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                for row in sorted(
                    kept, key=lambda item: (item["received_ts"], item["client_id"], item["command_id"])
                ):
                    received, encoded = self._event(
                        "received", row["client_id"], row["command_id"],
                        request_sha256=row["request_sha256"],
                        owner_instance=row["owner_instance"],
                    )
                    received["ts"] = row["received_ts"]
                    received["checksum"] = _checksum(received)
                    encoded = _canonical(received, limit=MAX_EVENT_BYTES, label="event") + b"\n"
                    stream.write(encoded)
                    if row["state"] == "uncertain":
                        uncertain, _ = self._event(
                            "uncertain", row["client_id"], row["command_id"]
                        )
                        uncertain["ts"] = row["uncertain_ts"]
                        uncertain["checksum"] = _checksum(uncertain)
                        stream.write(
                            _canonical(uncertain, limit=MAX_EVENT_BYTES, label="event") + b"\n"
                        )
                    elif row["state"] == "completed":
                        completed, _ = self._event(
                            "completed", row["client_id"], row["command_id"],
                            result=row["result"],
                        )
                        completed["ts"] = row["completed_ts"]
                        completed["checksum"] = _checksum(completed)
                        stream.write(
                            _canonical(completed, limit=MAX_EVENT_BYTES, label="event") + b"\n"
                        )
                    elif row["state"] == "acknowledged":
                        completed, _ = self._event(
                            "completed", row["client_id"], row["command_id"],
                            result=row["result"],
                        )
                        completed["ts"] = row["completed_ts"]
                        completed["checksum"] = _checksum(completed)
                        stream.write(
                            _canonical(completed, limit=MAX_EVENT_BYTES, label="event") + b"\n"
                        )
                        acknowledged_event, _ = self._event(
                            "acknowledged", row["client_id"], row["command_id"]
                        )
                        acknowledged_event["ts"] = row["acknowledged_ts"]
                        acknowledged_event["checksum"] = _checksum(acknowledged_event)
                        stream.write(
                            _canonical(acknowledged_event, limit=MAX_EVENT_BYTES, label="event") + b"\n"
                        )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _maybe_compact_locked(self, states, *, force=False):
        if force or (self.path.exists() and self.path.stat().st_size >= self.compact_after_bytes):
            try:
                self._compact_locked(states)
            except OSError:
                # The append+fsync above is the source of truth.  Compaction
                # is maintenance and can legitimately lose an os.replace race
                # to antivirus/backup readers on Windows; retry later instead
                # of misreporting a durable transition as failed.
                pass

    @staticmethod
    def _public(row, instance_id):
        state = row["state"]
        if state == "received":
            state = "pending" if row["owner_instance"] == instance_id else "uncertain"
        return {
            "client_id": row["client_id"],
            "command_id": row["command_id"],
            "state": state,
            "dispatch": False,
            "result": row.get("result") if state == "completed" else None,
        }

    def mark_uncertain(self, client_id, command_id):
        """Fail a dispatched-or-not gap closed without inventing a result."""
        client_id = _identifier(client_id, label="client_id", limit=MAX_CLIENT_ID)
        command_id = _identifier(command_id, label="command_id", limit=MAX_COMMAND_ID)
        key = (client_id, command_id)
        with self._locked():
            states = self._read_locked()
            existing = states.get(key)
            if existing is None:
                raise CommandJournalError("uncertain command has no durable receipt")
            if existing["state"] == "uncertain":
                return self._public(existing, self.instance_id)
            if existing["state"] != "received":
                raise CommandJournalError("completed command cannot become uncertain")
            event, encoded = self._event("uncertain", client_id, command_id)
            self._append_locked(event, encoded)
            self._apply(states, event)
            self._maybe_compact_locked(states)
            return self._public(states[key], self.instance_id)

    def receive(self, client_id, command_id, request):
        """Persist a receipt and return whether this process may dispatch it."""
        client_id = _identifier(client_id, label="client_id", limit=MAX_CLIENT_ID)
        command_id = _identifier(command_id, label="command_id", limit=MAX_COMMAND_ID)
        request_bytes = _canonical(request, limit=MAX_REQUEST_BYTES, label="request")
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        key = (client_id, command_id)
        with self._locked():
            states = self._read_locked()
            existing = states.get(key)
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    raise CommandConflict("command id was reused for a different request")
                return self._public(existing, self.instance_id)
            unacknowledged = sum(
                1 for row in states.values() if row["state"] != "acknowledged"
            )
            if unacknowledged >= self.max_commands:
                raise CommandJournalFull(
                    "command journal is full; clients must acknowledge completed results"
                )
            event, encoded = self._event(
                "received", client_id, command_id,
                request_sha256=request_sha256,
                owner_instance=self.instance_id,
            )
            self._append_locked(event, encoded)
            self._apply(states, event)
            self._maybe_compact_locked(states)
            result = self._public(states[key], self.instance_id)
            result["dispatch"] = True
            return result

    def complete(self, client_id, command_id, result):
        """Persist the replayable result before it is returned to the client."""
        client_id = _identifier(client_id, label="client_id", limit=MAX_CLIENT_ID)
        command_id = _identifier(command_id, label="command_id", limit=MAX_COMMAND_ID)
        result = json.loads(
            _canonical(result, limit=MAX_RESULT_BYTES, label="result").decode("utf-8")
        )
        key = (client_id, command_id)
        with self._locked():
            states = self._read_locked()
            existing = states.get(key)
            if existing is None:
                raise CommandJournalError("command completion has no durable receipt")
            if existing["state"] == "completed":
                if existing["result"] != result:
                    raise CommandConflict("command already has a different result")
                return self._public(existing, self.instance_id)
            if existing["state"] not in {"received", "uncertain"}:
                raise CommandJournalError("command cannot be completed in its current state")
            event, encoded = self._event(
                "completed", client_id, command_id, result=result
            )
            self._append_locked(event, encoded)
            self._apply(states, event)
            self._maybe_compact_locked(states)
            return self._public(states[key], self.instance_id)

    def acknowledge(self, client_id, command_id):
        """Record client receipt; acknowledged commands become compactable."""
        client_id = _identifier(client_id, label="client_id", limit=MAX_CLIENT_ID)
        command_id = _identifier(command_id, label="command_id", limit=MAX_COMMAND_ID)
        key = (client_id, command_id)
        with self._locked():
            states = self._read_locked()
            existing = states.get(key)
            if existing is None:
                raise CommandJournalError("command was not found")
            if existing["state"] == "acknowledged":
                return {
                    "client_id": client_id,
                    "command_id": command_id,
                    "state": "acknowledged",
                }
            if existing["state"] != "completed":
                raise CommandJournalError("only a completed command can be acknowledged")
            event, encoded = self._event("acknowledged", client_id, command_id)
            self._append_locked(event, encoded)
            self._apply(states, event)
            self._maybe_compact_locked(states, force=True)
            return {
                "client_id": client_id,
                "command_id": command_id,
                "state": "acknowledged",
            }

    def inspect(self, client_id, command_id):
        """Return current state without changing it."""
        client_id = _identifier(client_id, label="client_id", limit=MAX_CLIENT_ID)
        command_id = _identifier(command_id, label="command_id", limit=MAX_COMMAND_ID)
        with self._locked():
            row = self._read_locked().get((client_id, command_id))
            return self._public(row, self.instance_id) if row else None
