"""Small JSON-lines durable adapter for typed activation outcomes."""
from __future__ import annotations

import json
from pathlib import Path

from ...application.updates.durable_activation import ActivationJournalEntry


class JsonActivationJournal:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(self, entry: ActivationJournalEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "activation_id": entry.activation_id, "phase": entry.phase,
            "platform": entry.platform, "current_release": entry.current_release,
            "target_release": entry.target_release, "evidence_digest": entry.evidence_digest,
            "recovery_digest": entry.recovery_digest, "error_types": list(entry.error_types),
        }
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")

    def entries(self) -> tuple[ActivationJournalEntry, ...]:
        if not self._path.exists():
            return ()
        rows = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            rows.append(ActivationJournalEntry(
                value["activation_id"], value["phase"], value["platform"],
                value["current_release"], value["target_release"], value["evidence_digest"],
                value.get("recovery_digest", ""), tuple(value.get("error_types", ())),
            ))
        return tuple(rows)


__all__ = ["JsonActivationJournal"]
