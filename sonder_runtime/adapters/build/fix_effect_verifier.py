"""Prove an interrupted ``build-fix`` source edit from the file's current digest.

A crash between a fix's journal intent and its receipt leaves the edit's
outcome unknown. The journaled identity names the file (project root +
normalized relative path) and the SHA-256 the file had before the edit and
must have after it. This verifier hashes the file as it is now:

* the after-digest: the edit reached the file -> a ``completed`` proof;
* the before-digest: the edit did not reach the file -> a ``failed`` proof
  (definitively not applied);
* anything else (another digest, a missing, oversized, linked or unreadable
  file, an intent that does not round-trip): no proof, so the intent stays
  ``uncertain`` and the run stays fenced.

The proof is about the file's current state, which is what a resumed fix and
``build_fix_restore`` act on: they compare that state with their records
before writing. It cannot tell an edit that ran and was later undone by hand
from one that never ran; both are reported as not applied.

Bounded and read-only: at most ``MAX_FILE_BYTES`` are read, the path is
opened without following a final link, every parent must resolve inside the
project root, and the journal bounds the call with its verifier timeout.
Nothing here writes a file or invokes an effect.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from ...application.build.fix_effects import FAMILY, edit_from_intent
from ...application.execution.effect_journal import (
    EffectIntent,
    EffectState,
    ReconciliationProof,
)

MAX_FILE_BYTES = 2 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CHUNK = 64 * 1024


def _current_sha256(root: str, rel: str) -> str | None:
    """SHA-256 of ``root/rel`` as it is now, or None when it cannot be proven."""
    base = Path(root)
    if not base.is_absolute():
        return None
    try:
        real_root = base.resolve(strict=True)
        path = base.joinpath(*rel.split("/"))
        # Parents must stay inside the root; the file itself is opened
        # without following a link.
        real_parent = path.parent.resolve(strict=True)
        if real_parent != real_root and real_root not in real_parent.parents:
            return None
        target = real_parent / path.name
        if not stat.S_ISREG(os.lstat(target).st_mode):
            return None
        fd = os.open(target, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_BINARY", 0))
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        digest = hashlib.sha256()
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining <= 0:
            return None
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


class BuildFixEditVerifier:
    """Host-owned proof of one ``build-fix`` edit from the edited file."""

    verifier_id = "build-fix-source-sha256-v1"
    operation_ids = frozenset({FAMILY})

    def verify(self, intent: EffectIntent) -> ReconciliationProof | None:
        edit = edit_from_intent(intent)
        if edit is None:
            return None
        current = _current_sha256(edit.project_root, edit.rel)
        if current is None:
            return None
        if current == edit.after_sha256:
            state, label = EffectState.COMPLETED, "applied"
        elif current == edit.before_sha256:
            state, label = EffectState.FAILED, "not-applied"
        else:
            return None
        return ReconciliationProof(
            intent_id=intent.intent_id,
            operation_id=intent.operation_id,
            receipt_key="%s:%s:sha256:%s" % (edit.operation_id, label, current),
            outcome_digest=hashlib.sha256(
                ("%s\x00%s\x00%s" % (intent.intent_id, label, current)).encode("ascii")
            ).hexdigest(),
            state=state,
            verifier_id=self.verifier_id,
            external_reference="%s:sha256:%s" % (label, current),
        )


__all__ = ["BuildFixEditVerifier", "MAX_FILE_BYTES"]
