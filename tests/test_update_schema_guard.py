"""The downgrade guard in the update path must fail CLOSED.

check_compatibility returns a list of problems, and the update engine reads an
empty list as "compatible" and advances the plan to install. So a check that
silently skips itself does not merely lose information -- it green-lights the
install it exists to block.
"""
import sys
import types
from pathlib import Path

import pytest

import sonder_runtime.adapters.updates.service as sonder_updates
from sonder_runtime.adapters.updates.service import BundleManifest, check_compatibility


def _manifest(state_schema=None):
    return BundleManifest.from_dict({
        "schema_version": 1,
        "bundle_id": "b", "version": "1.0.0", "commit_sha": "c",
        "channel": "stable",
        "platform": sonder_updates.current_platform(),
        "architecture": sonder_updates.current_architecture(),
        "package_format": "tar.gz",
        "runtime": {"python_version": "%d.%d" % sys.version_info[:2]},
        "state_schema": state_schema or {}, "resources": {}, "entrypoints": {},
        "health_checks": [], "rollback": {},
        "files": [{"path": "a.py", "length": 1, "sha256": "0" * 64}],
    })


def _status(applied=(), unknown=(), mismatches=()):
    return types.SimpleNamespace(
        store="memory", db_path="", applied=tuple(applied),
        pending=(), unknown=tuple(unknown), checksum_mismatches=tuple(mismatches),
    )


def test_an_unreadable_store_is_a_problem_not_a_skip(monkeypatch, tmp_path):
    """`except Exception: continue` skipped the ONLY downgrade guard in the
    update path. status() opens a SQLite connection and runs ledger SQL, so an
    OperationalError on a locked or corrupt store during an update -- exactly
    when this matters most -- left problems empty and the install proceeded."""
    def boom(store):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sonder_updates.sonder_migrations, "status", boom)
    problems = check_compatibility(_manifest(), releases_dir=tmp_path)
    assert problems, "an unreadable store must block the update, not be skipped"
    assert any("could not be read" in p for p in problems)


def test_future_migrations_block_even_when_the_count_looks_fine(monkeypatch, tmp_path):
    """`len(applied) > target` misses the real signal. After a partial rollback
    `applied` can SHRINK below target while the ledger still records migrations
    this build does not ship, so the count passes and the downgrade proceeds.
    `unknown` is what actually says "future schema"."""
    monkeypatch.setattr(
        sonder_updates.sonder_migrations, "status",
        lambda store: _status(applied=("m1",), unknown=("m9_from_a_newer_build",)),
    )
    schema = {"%s_target" % s: 5 for s in sonder_updates.sonder_migrations.STORE_NAMES}
    problems = check_compatibility(_manifest(schema), releases_dir=tmp_path)
    assert any("does not ship" in p for p in problems), problems


def test_modified_migration_history_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sonder_updates.sonder_migrations, "status",
        lambda store: _status(applied=("m1",), mismatches=("m1",)),
    )
    schema = {"%s_target" % s: 5 for s in sonder_updates.sonder_migrations.STORE_NAMES}
    problems = check_compatibility(_manifest(schema), releases_dir=tmp_path)
    assert any("history was modified" in p for p in problems), problems


def test_a_healthy_store_still_passes(monkeypatch, tmp_path):
    """The guard must not become so strict that no update can ever proceed."""
    monkeypatch.setattr(
        sonder_updates.sonder_migrations, "status",
        lambda store: _status(applied=("m1", "m2")),
    )
    schema = {"%s_target" % s: 5 for s in sonder_updates.sonder_migrations.STORE_NAMES}
    problems = check_compatibility(_manifest(schema), releases_dir=tmp_path)
    schema_problems = [p for p in problems if "store " in p]
    assert not schema_problems, schema_problems


def test_a_newer_store_than_the_bundle_supports_still_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sonder_updates.sonder_migrations, "status",
        lambda store: _status(applied=("m1", "m2", "m3")),
    )
    schema = {"%s_target" % s: 1 for s in sonder_updates.sonder_migrations.STORE_NAMES}
    problems = check_compatibility(_manifest(schema), releases_dir=tmp_path)
    assert any("forward recovery required" in p for p in problems), problems


def test_a_store_missing_from_an_older_manifest_fails_closed(monkeypatch, tmp_path):
    """A manifest built before a store existed (e.g. queued_actions/jobs) has
    no ``<store>_target`` key at all. The guard must treat that as target=0,
    not skip the store -- an applied migration for it is then correctly read
    as newer than anything the old bundle declares support for."""
    monkeypatch.setattr(
        sonder_updates.sonder_migrations, "status",
        lambda store: _status(applied=("m1",) if store == "jobs" else ()),
    )
    schema = {"%s_target" % s: 5 for s in
              ("memory", "autopilot", "fleet", "operations", "updates")}
    problems = check_compatibility(_manifest(schema), releases_dir=tmp_path)
    assert any("jobs" in p and "forward recovery required" in p for p in problems), problems
