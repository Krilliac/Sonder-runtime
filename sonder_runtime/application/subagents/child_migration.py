"""Deterministic offline migration of the existing child aggregate."""

import base64
from dataclasses import asdict
import hashlib
import json
from ..ports.child_migration import ChildMigrationStore

from ..ports.continuation_mutations import (
    PreparedContinuationMutation,
    ContinuationMutationOutcome,
    canonical,
)
from .continuation_codec import session_from_data

STREAMS = ("children", "intents", "receipts")
MAX_LINE = 3 * 1024 * 1024
MAX_BUNDLE = 512 * 1024 * 1024


class MigrationRefused(RuntimeError):
    pass


class MigrationUnsupported(MigrationRefused):
    pass


def encode(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def binary(value):
    return base64.b64encode(value).decode("ascii")


def unbinary(value):
    try:
        result = base64.b64decode(value, validate=True)
    except Exception:
        raise MigrationRefused("invalid migration binary encoding") from None
    if not result or len(result) > 2_000_000:
        raise MigrationRefused("migration record exceeds canonical bounds")
    return result


def validate_record(stream, record):
    fields = {
        "children": {"position", "key", "snapshot"},
        "intents": {"position", "key", "child_id", "kind", "digest", "payload"},
        "receipts": {"position", "key", "disposition", "result", "revision"},
    }
    if (
        stream not in fields
        or not isinstance(record, dict)
        or set(record) != fields[stream]
    ):
        raise MigrationRefused("invalid migration record fields")
    if type(record["position"]) is not int or not 0 < record["position"] < 2**63:
        raise MigrationRefused("invalid migration ordering position")
    if (
        not isinstance(record["key"], str)
        or not 1 <= len(record["key"]) <= 4096
        or "\0" in record["key"]
    ):
        raise MigrationRefused("invalid migration record identity")
    if stream == "children":
        payload = unbinary(record["snapshot"])
        child = session_from_data(json.loads(payload))
        if (
            child.request.child_id != record["key"]
            or canonical(asdict(child)) != payload
        ):
            raise MigrationRefused("child snapshot does not match canonical identity")
    elif stream == "intents":
        PreparedContinuationMutation(
            record["kind"],
            record["child_id"],
            record["key"],
            unbinary(record["payload"]),
            record["digest"],
        )
    else:
        ContinuationMutationOutcome(
            record["disposition"], unbinary(record["result"]), record["revision"]
        )
    if len(encode(record)) + 1 > MAX_LINE:
        raise MigrationRefused("migration record line exceeds bound")


def export_snapshot(source: ChildMigrationStore, bundle, *, target_identity):
    if bundle.has_manifest():
        result = bundle.manifest()
        if (
            result["source_identity"] != source.identity
            or result["target_identity"] != target_identity
        ):
            raise MigrationRefused("sealed migration identity conflict")
        return result
    plan = bundle.begin(source.identity, target_identity)

    def collect(snapshot):
        metadata = snapshot.metadata()
        descriptors = {}
        total = 0
        for stream in STREAMS:
            descriptor = bundle.write_stream(
                stream, snapshot.records(stream), remaining_bytes=MAX_BUNDLE - total
            )
            descriptors[stream] = descriptor
            total += descriptor["bytes"]
            if total > MAX_BUNDLE:
                raise MigrationRefused("migration bundle exceeds capacity")
        manifest = {
            "version": 1,
            "migration_id": plan["migration_id"],
            "source_identity": source.identity,
            "target_identity": target_identity,
            "source": metadata,
            "unresolved": metadata["unresolved"],
            "active": metadata["active"],
            "streams": descriptors,
            "aggregate_sha256": digest(descriptors),
            "source_backup_sha256": bundle.backup_digest(),
            "source_files": bundle.source_files(),
        }
        return manifest

    manifest = source.read_snapshot(collect, bundle=bundle)
    # Publishing is outside the storage worker: an expired SQL operation must
    # never seal a usable export after its caller has received ambiguity.
    bundle.seal(manifest)
    return bundle.manifest()


def stage_snapshot(bundle, target: ChildMigrationStore):
    manifest = bundle.manifest()
    if manifest["target_identity"] != target.identity:
        raise MigrationRefused("migration target identity conflict")
    if manifest["unresolved"]:
        raise MigrationRefused("unresolved mutation history prevents migration staging")
    if manifest["active"]:
        raise MigrationRefused("active child execution prevents migration staging")
    target.prepare(manifest)
    state = target.status(manifest)
    if state["phase"] in ("VERIFIED", "ACTIVE"):
        return state
    for stream in STREAMS:
        for index, page in enumerate(bundle.pages(stream)):
            target.copy_page(manifest, stream, index, page)
    target.copied(manifest)
    return target.status(manifest)


def verify_snapshot(bundle, target: ChildMigrationStore):
    manifest = bundle.manifest()
    if manifest["target_identity"] != target.identity:
        raise MigrationRefused("migration target identity conflict")

    def compare(snapshot):
        for stream in STREAMS:
            actual = stream_descriptor(stream, snapshot.records(stream))
            if actual != manifest["streams"][stream]:
                raise MigrationRefused(
                    "migration target read-back differs from sealed snapshot"
                )
        target_metadata = snapshot.metadata()
        if target_metadata["unresolved"] or target_metadata["active"]:
            raise MigrationRefused("migration target eligibility changed")
        for key in ("children_high_water", "intents_high_water"):
            if target_metadata[key] != manifest["source"][key]:
                raise MigrationRefused(
                    "migration target sequence differs from sealed snapshot"
                )

    target.read_snapshot(compare)
    target.verified(manifest)
    return {
        "verified": True,
        "migration_id": manifest["migration_id"],
        "aggregate_sha256": manifest["aggregate_sha256"],
    }


def stream_descriptor(stream, records, sink=None, *, max_bytes=MAX_BUNDLE):
    count = size = last = binary_bytes = 0
    sha = hashlib.sha256()
    for record in records:
        validate_record(stream, record)
        if record["position"] <= last:
            raise MigrationRefused(
                "migration stream ordering is not strictly increasing"
            )
        last = record["position"]
        raw = encode(record) + b"\n"
        size += len(raw)
        count += 1
        if count > 100_000 or size > max_bytes:
            raise MigrationRefused("migration stream exceeds capacity")
        sha.update(raw)
        if stream in ("intents", "receipts"):
            binary_bytes += len(
                unbinary(record["payload" if stream == "intents" else "result"])
            )
        if sink is not None:
            sink.write(raw)
    return {
        "count": count,
        "bytes": size,
        "sha256": sha.hexdigest(),
        "last_position": last,
        "binary_bytes": binary_bytes,
    }


def validate_manifest(value):
    expected = {
        "version",
        "migration_id",
        "source_identity",
        "target_identity",
        "source",
        "unresolved",
        "active",
        "streams",
        "aggregate_sha256",
        "source_backup_sha256",
        "source_files",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise MigrationRefused("invalid migration manifest fields")
    for key, length in (
        ("migration_id", 32),
        ("source_identity", 64),
        ("target_identity", 64),
        ("aggregate_sha256", 64),
    ):
        text = value[key]
        if (
            not isinstance(text, str)
            or len(text) != length
            or any(c not in "0123456789abcdef" for c in text)
        ):
            raise MigrationRefused("invalid migration manifest identity")
    source = value["source"]
    files = value["source_files"]
    if files is not None and (
        not isinstance(files, list)
        or len(files) != 4
        or any(
            row is not None
            and (
                not isinstance(row, list)
                or len(row) != 4
                or any(type(item) is not int or item < 0 for item in row)
            )
            for row in files
        )
    ):
        raise MigrationRefused("invalid source file identity")
    backup = value["source_backup_sha256"]
    if backup is not None and (
        not isinstance(backup, str)
        or len(backup) != 64
        or any(c not in "0123456789abcdef" for c in backup)
    ):
        raise MigrationRefused("invalid source backup identity")
    fields = {
        "backend",
        "schema",
        "unresolved",
        "active",
        "children_high_water",
        "intents_high_water",
        "owner",
        "barrier",
    }
    if (
        not isinstance(source, dict)
        or set(source) != fields
        or source["backend"] not in ("sqlite", "postgresql")
        or type(source["schema"]) is not int
        or source["schema"] != 1
    ):
        raise MigrationRefused("invalid migration source metadata")
    for key in ("unresolved", "active", "children_high_water", "intents_high_water"):
        if type(source[key]) is not int or not 0 <= source[key] < 2**63:
            raise MigrationRefused("invalid migration source count")
    for key in ("unresolved", "active"):
        if type(value[key]) is not int or value[key] != source[key]:
            raise MigrationRefused("inconsistent migration eligibility metadata")
    owner = source["owner"]
    if owner is not None and (
        not isinstance(owner, list)
        or len(owner) != 3
        or not all(
            isinstance(item, str) and 1 <= len(item) <= 256 for item in owner[:2]
        )
        or type(owner[2]) is not bool
    ):
        raise MigrationRefused("invalid source owner provenance")
    if source["barrier"] is not None and (
        type(source["barrier"]) is not int or not 0 <= source["barrier"] < 2**63
    ):
        raise MigrationRefused("invalid source barrier provenance")
    streams = value["streams"]
    if (
        not isinstance(streams, dict)
        or set(streams) != set(STREAMS)
        or digest(streams) != value["aggregate_sha256"]
    ):
        raise MigrationRefused("invalid migration stream manifest")
    total = history = 0
    for kind, descriptor in streams.items():
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "count",
            "bytes",
            "sha256",
            "last_position",
            "binary_bytes",
        }:
            raise MigrationRefused("invalid migration stream descriptor")
        for key in ("count", "bytes", "last_position", "binary_bytes"):
            if type(descriptor[key]) is not int or not 0 <= descriptor[key] < 2**63:
                raise MigrationRefused("invalid migration stream count")
        if descriptor["count"] > 100_000:
            raise MigrationRefused("migration history exceeds capacity")
        total += descriptor["bytes"]
        history += descriptor["binary_bytes"]
        if (
            kind in ("children", "intents")
            and descriptor["last_position"] > source[kind + "_high_water"]
        ):
            raise MigrationRefused("migration source sequence precedes records")
    if total > MAX_BUNDLE or history > 64 * 1024 * 1024:
        raise MigrationRefused("migration history exceeds capacity")
