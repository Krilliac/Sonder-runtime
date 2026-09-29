from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

import pytest

from sonder_runtime.adapters.updates.metadata_ledger import SqliteMetadataLedger
from sonder_runtime.application.updates.bounded_state import (
    MetadataChainError, TufLikeMetadata, TufLikeMetadataChain,
)


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _verify(_payload, signature, signer):
    return signature == "valid" and signer in {"old-key", "new-key"}


def _chain(versions, *, root_signer="old-key", prior_signer="", prior_signature=""):
    rows = []
    for role, version in zip(("root", "timestamp", "snapshot", "targets"), versions, strict=True):
        rows.append(TufLikeMetadata(
            role, version, "2026-08-21T00:00:00Z", sha256(role.encode()).hexdigest(),
            root_signer if role == "root" else "old-key", "valid",
            rows[-1].digest if rows else "", prior_signer=prior_signer if role == "root" else "",
            prior_signature=prior_signature if role == "root" else "",
        ))
    return TufLikeMetadataChain(tuple(rows))


def test_replayed_unexpired_roles_are_refused_across_processes(tmp_path):
    path = tmp_path / "metadata.db"
    old, fresh = _chain((1, 2, 3, 4)), _chain((1, 3, 4, 5))
    ledger = SqliteMetadataLedger(path)
    ledger.pin_root("stable", old.entries[0])
    old.verify(_verify, now=NOW, repository="stable", ledger=ledger)
    fresh.verify(_verify, now=NOW, repository="stable", ledger=ledger)
    with pytest.raises(MetadataChainError, match="replay|older|rollback"):
        old.verify(_verify, now=NOW, repository="stable",
                   ledger=SqliteMetadataLedger(path))


def test_root_rotation_requires_prior_root_signature(tmp_path):
    old = _chain((1, 2, 3, 4))
    ledger = SqliteMetadataLedger(tmp_path / "metadata.db")
    ledger.pin_root("stable", old.entries[0])
    rotated = _chain((2, 3, 4, 5), root_signer="new-key")
    with pytest.raises(MetadataChainError, match="root|rotation"):
        rotated.verify(_verify, now=NOW, repository="stable", ledger=ledger)
    rotated = _chain((2, 3, 4, 5), root_signer="new-key",
                     prior_signer="old-key", prior_signature="valid")
    rotated.verify(_verify, now=NOW, repository="stable", ledger=ledger)


def test_unpinned_repository_cannot_choose_its_own_root(tmp_path):
    ledger = SqliteMetadataLedger(tmp_path / "metadata.db")
    with pytest.raises(MetadataChainError, match="pinned root"):
        _chain((1, 2, 3, 4)).verify(
            _verify, now=NOW, repository="untrusted", ledger=ledger,
        )
