"""Small JSON-lines durable adapter for typed activation outcomes."""
from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

from ...application.updates.durable_activation import ActivationJournalEntry


class JsonActivationJournal:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(self, entry: ActivationJournalEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        created = not self._path.exists()
        value = {
            "activation_id": entry.activation_id, "phase": entry.phase,
            "platform": entry.platform, "current_release": entry.current_release,
            "target_release": entry.target_release, "evidence_digest": entry.evidence_digest,
            "recovery_digest": entry.recovery_digest, "error_types": list(entry.error_types),
            "helper_nonce": entry.helper_nonce, "helper_argv": list(entry.helper_argv),
        }
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        frame = {"record": value, "sha256": sha256(canonical.encode("utf-8")).hexdigest()}
        payload = (json.dumps(frame, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise OSError("activation journal write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created and os.name != "nt":
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def entries(self) -> tuple[ActivationJournalEntry, ...]:
        if not self._path.exists():
            return ()
        raw = self._path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("corrupt activation journal: incomplete record")
        rows = []
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if "record" in value:
                record = value["record"]
                canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
                if value.get("sha256") != sha256(canonical.encode("utf-8")).hexdigest():
                    raise ValueError("corrupt activation journal checksum")
                value = record
            rows.append(ActivationJournalEntry(
                value["activation_id"], value["phase"], value["platform"],
                value["current_release"], value["target_release"], value["evidence_digest"],
                value.get("recovery_digest", ""), tuple(value.get("error_types", ())),
                value.get("helper_nonce", ""), tuple(value.get("helper_argv", ())),
            ))
        return tuple(rows)


__all__ = ["JsonActivationJournal"]
