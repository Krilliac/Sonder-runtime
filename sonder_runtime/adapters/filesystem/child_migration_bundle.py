"""Anchored private, immutable and bounded child migration bundles."""

import json
import os
import uuid
import hashlib
from pathlib import Path

from ...application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ...application.subagents.child_migration import (
    STREAMS,
    MAX_LINE,
    MigrationRefused,
    encode,
    digest,
    stream_descriptor,
    validate_manifest,
)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise MigrationRefused("duplicate migration JSON field")
        value[key] = item
    return value


class ChildMigrationBundle:
    def __init__(self, path, *, writable_roots):
        self.path = Path(path).absolute()
        self._roots = writable_roots
        self._anchor = None
        self.validate()
        self._anchor = PrivateDirectoryAnchor.open_base(self.path)

    def validate(self):
        root = self.path.resolve()
        for value in self._roots():
            writable = Path(value).resolve()
            if (
                root == writable
                or root.is_relative_to(writable)
                or writable.is_relative_to(root)
            ):
                raise MigrationRefused("migration bundle overlaps writable roots")
        if self._anchor is not None:
            self._anchor.validate()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._anchor.close()

    def has_manifest(self):
        self.validate()
        return self._anchor.exists("manifest.json")

    def begin(self, source_identity, target_identity):
        self.validate()
        if not self._anchor.exists("plan.json"):
            self._anchor.write_json_once(
                "plan.json",
                {
                    "migration_id": uuid.uuid4().hex,
                    "source_identity": source_identity,
                    "target_identity": target_identity,
                },
            )
        plan = json.loads(
            self._anchor.read_bytes("plan.json", max_bytes=4096),
            object_pairs_hook=_unique,
        )
        if (
            set(plan) != {"migration_id", "source_identity", "target_identity"}
            or plan["source_identity"] != source_identity
            or plan["target_identity"] != target_identity
        ):
            raise MigrationRefused("migration plan identity conflict")
        return plan

    def backup_digest(self):
        self.validate()
        if not self._anchor.exists("source-backup.sqlite"):
            return None
        sha, count = hashlib.sha256(), 0
        with self._anchor.open_read("source-backup.sqlite") as source:
            while chunk := source.read(1024 * 1024):
                count += len(chunk)
                if count > 512 * 1024 * 1024:
                    raise MigrationRefused("migration source backup exceeds capacity")
                sha.update(chunk)
        return sha.hexdigest()

    def seal_backup(self, source_files):
        value = {"sha256": self.backup_digest(), "source_files": source_files}
        self._anchor.write_json_once("source-backup.json", value)

    def has_sealed_backup(self):
        self.validate()
        if not self._anchor.exists("source-backup.json"):
            return False
        value = json.loads(
            self._anchor.read_bytes("source-backup.json", max_bytes=4096),
            object_pairs_hook=_unique,
        )
        if (
            set(value) != {"sha256", "source_files"}
            or value["sha256"] != self.backup_digest()
        ):
            raise MigrationRefused("source backup identity changed")
        return True

    def source_files(self):
        if not self.has_sealed_backup():
            return None
        return json.loads(
            self._anchor.read_bytes("source-backup.json", max_bytes=4096),
            object_pairs_hook=_unique,
        )["source_files"]

    def write_stream(self, stream, records, *, remaining_bytes=512 * 1024 * 1024):
        if stream not in STREAMS:
            raise MigrationRefused("invalid migration stream")
        self.validate()
        if self._anchor.exists(stream + ".jsonl"):
            descriptor = stream_descriptor(stream, records, max_bytes=remaining_bytes)
            if stream_descriptor(stream, self.records(stream)) != descriptor:
                raise MigrationRefused("resumed export differs from retained stream")
            return descriptor
        fd, temporary = self._anchor.create_temporary()
        try:
            with os.fdopen(fd, "wb") as output:
                descriptor = stream_descriptor(
                    stream, records, output, max_bytes=remaining_bytes
                )
                output.flush()
                os.fsync(output.fileno())
            self._anchor.publish(temporary, stream + ".jsonl")
            return descriptor
        finally:
            if self._anchor.exists(temporary):
                self._anchor.unlink(temporary)

    def seal(self, manifest):
        validate_manifest(manifest)
        if len(encode(manifest)) > 65536:
            raise MigrationRefused("migration manifest exceeds bounds")
        self.validate()
        self._anchor.write_json_once("manifest.json", manifest)

    def record_phase(self, phase, manifest):
        phases = (
            "SOURCE_RETIRE_INTENT",
            "SOURCE_RETIRED",
            "TARGET_READY",
            "CONFIG_SWITCHED",
            "COMPLETE",
        )
        if phase not in phases:
            raise MigrationRefused("unknown activation phase")
        self.validate()
        value = {
            "migration_id": manifest["migration_id"],
            "manifest_digest": digest(manifest),
            "phase": phase,
        }
        name = "phase-" + str(phases.index(phase)) + ".json"
        if self._anchor.exists(name):
            if json.loads(self._anchor.read_bytes(name, max_bytes=4096)) != value:
                raise MigrationRefused("activation phase identity conflict")
        else:
            self._anchor.write_json_once(name, value)

    def has_phase(self, phase, manifest):
        phases = (
            "SOURCE_RETIRE_INTENT",
            "SOURCE_RETIRED",
            "TARGET_READY",
            "CONFIG_SWITCHED",
            "COMPLETE",
        )
        if phase not in phases:
            raise MigrationRefused("unknown activation phase")
        self.validate()
        name = "phase-" + str(phases.index(phase)) + ".json"
        if not self._anchor.exists(name):
            return False
        expected = {
            "migration_id": manifest["migration_id"],
            "manifest_digest": digest(manifest),
            "phase": phase,
        }
        if (
            json.loads(
                self._anchor.read_bytes(name, max_bytes=4096), object_pairs_hook=_unique
            )
            != expected
        ):
            raise MigrationRefused("activation phase identity conflict")
        return True

    def manifest(self):
        self.validate()
        value = json.loads(
            self._anchor.read_bytes("manifest.json", max_bytes=65536),
            object_pairs_hook=_unique,
        )
        validate_manifest(value)
        plan = self.begin(value["source_identity"], value["target_identity"])
        if (
            plan["migration_id"] != value["migration_id"]
            or self.backup_digest() != value["source_backup_sha256"]
            or self.source_files() != value["source_files"]
        ):
            raise MigrationRefused("migration plan or backup identity changed")
        for stream in STREAMS:
            if (
                stream_descriptor(stream, self.records(stream))
                != value["streams"][stream]
            ):
                raise MigrationRefused("migration bundle stream changed")
        return value

    def records(self, stream):
        if stream not in STREAMS:
            raise MigrationRefused("invalid migration stream")
        self.validate()
        with self._anchor.open_read(stream + ".jsonl") as source:
            while raw := source.readline(MAX_LINE + 1):
                if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                    raise MigrationRefused("invalid migration record framing")
                value = json.loads(raw, object_pairs_hook=_unique)
                if encode(value) + b"\n" != raw:
                    raise MigrationRefused("noncanonical migration record")
                yield value

    def pages(self, stream):
        page, size = [], 0
        for record in self.records(stream):
            length = len(encode(record)) + 1
            if page and (len(page) >= 100 or size + length > 4 * 1024 * 1024):
                yield tuple(page)
                page, size = [], 0
            page.append(record)
            size += length
        if page:
            yield tuple(page)
