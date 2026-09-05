"""Shared activation algorithm; authority remains on the live host object."""
from pathlib import Path
import os
import json
from ..adapters.filesystem.atomic_json import write_json_atomic
from ..adapters.persistence.child_migration import SQLiteChildMigrationStore
from ..application.subagents.child_migration import MigrationRefused, MigrationUnsupported, STREAMS, stream_descriptor, verify_snapshot, digest
from ..application.subagents.child_migration_activation import issue_host_guard


class ChildMigrationActivation:
    def _require_quiescent(self, manifest):
        self._validate()
        if self._cutover_manifest != digest(manifest) or self._cutover_bundle is None:
            raise MigrationRefused(
                "migration issuer has not verified this exact cutover"
            )
        self._cutover_bundle.validate()
        for store in self._cutover_stores:
            validate = getattr(store, "validate_policy", None)
            if validate is not None:
                validate()
        if (
            self._application is not None
            or self._repository is not None
            or self._tracked
        ):
            raise MigrationRefused("owned cleanup proof is no longer current")

    def _read_selection(self):
        try:
            with self._anchor.open_read("selection.json") as stream:
                raw = stream.read(16385)
        except FileNotFoundError:
            return None
        if len(raw) > 16384:
            raise MigrationRefused("private selection marker exceeds bounds")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise MigrationRefused("private selection marker is invalid") from None

    def activate(self, bundle, source, target, *, timeout=5):
        with self._lock:
            self.quiesce(timeout)
            manifest = bundle.manifest()
            selection = {
                "migration_id": manifest["migration_id"],
                "manifest_digest": digest(manifest),
                "target_identity": target.identity,
            }
            if self._cutover_manifest is not None and self._cutover_manifest != digest(
                manifest
            ):
                raise MigrationRefused(
                    "activation is incomplete; reconcile the same migration ID"
                )
            for store in (source, target):
                validate = getattr(store, "validate_policy", None)
                if validate is not None:
                    validate()
            if (
                bundle.has_phase("COMPLETE", manifest)
                and self._selection.identity == target.identity
            ):
                if self._read_selection() != selection:
                    raise MigrationRefused("completed private selection marker changed")
                return {"phase": "COMPLETE", "migration_id": manifest["migration_id"]}
            if (
                source.identity != self._selection.identity
                or manifest["source_identity"] != source.identity
                or manifest["target_identity"] != target.identity
            ):
                raise MigrationRefused(
                    "migration does not match the owned current selection"
                )
            if (
                isinstance(target, SQLiteChildMigrationStore)
                and target.path.parent != self.path
            ):
                raise MigrationUnsupported(
                    "target SQLite path is outside the owned namespace"
                )

            # A fresh source comparison rejects a stale original backup after
            # any new application writes. No saved hash is cleanup authority.
            def compare(snapshot):
                metadata = snapshot.metadata()
                if metadata["active"] or metadata["unresolved"]:
                    raise MigrationRefused("source execution is not quiescent")
                for stream in STREAMS:
                    if (
                        stream_descriptor(stream, snapshot.records(stream))
                        != manifest["streams"][stream]
                    ):
                        raise MigrationRefused(
                            "source changed after export; a fresh snapshot is required"
                        )
                for key in ("children_high_water", "intents_high_water"):
                    if metadata[key] != manifest["source"][key]:
                        raise MigrationRefused("source ordering changed after export")
                if metadata["backend"] == "postgresql":
                    expected_owner = manifest["source"]["owner"]
                    expected_barrier = manifest["source"]["barrier"]
                    retired_owner = [
                        source.config.owner_id,
                        "retired-" + manifest["migration_id"],
                        False,
                    ]
                    retired_match = (
                        bundle.has_phase("SOURCE_RETIRE_INTENT", manifest)
                        and metadata["owner"] == retired_owner
                        and metadata["barrier"] == expected_barrier + 1
                    )
                    if not retired_match and (
                        metadata["owner"] != expected_owner
                        or metadata["barrier"] != expected_barrier
                    ):
                        raise MigrationRefused(
                            "source ownership changed after export; a fresh snapshot is required"
                        )

            retired = self.path / ("retired-" + manifest["migration_id"] + ".sqlite")
            already_retired = bundle.has_phase("SOURCE_RETIRED", manifest)
            if isinstance(source, SQLiteChildMigrationStore) and (
                already_retired
                or (
                    source.path.is_dir()
                    and retired.is_file()
                    and bundle.has_phase("SOURCE_RETIRE_INTENT", manifest)
                )
            ):
                SQLiteChildMigrationStore(retired).read_snapshot(compare)
                already_retired = True
            else:
                source.read_snapshot(compare)
            verify_snapshot(bundle, target)

            def live():
                self._validate()
                bundle.validate()
                if (
                    self._application is not None
                    or self._repository is not None
                    or self._tracked
                ):
                    raise MigrationRefused("owned cleanup proof is no longer current")

            if self._cutover_manifest is None:
                self._cutover_selection = self._read_selection()
            elif self._read_selection() not in (self._cutover_selection, selection):
                raise MigrationRefused(
                    "incomplete activation private selection marker changed"
                )
            self._cutover_manifest, self._cutover_bundle = digest(manifest), bundle
            self._cutover_stores = (source, target)
            guard = issue_host_guard(self, manifest)
            bundle.record_phase("SOURCE_RETIRE_INTENT", manifest)
            if already_retired and isinstance(source, SQLiteChildMigrationStore):
                pass
            elif isinstance(source, SQLiteChildMigrationStore):
                if source.path.parent != self.path:
                    raise MigrationUnsupported(
                        "SQLite source was not created in the owned namespace"
                    )
                # The gate and owned-handle cleanup are the authority. The
                # directory only blocks future opens by older SQLite binaries.
                before = source.physical_identity()
                if before != manifest["source_files"]:
                    raise MigrationRefused(
                        "source file or sidecar changed after export"
                    )
                live()
                if source.physical_identity() != before:
                    raise MigrationRefused("source file identity changed")
                if retired.exists():
                    raise MigrationRefused(
                        "source retirement needs same-ID reconciliation"
                    )
                os.rename(source.path, retired)
                source.path.mkdir()
            else:
                source.retire(manifest, guard)
            bundle.record_phase("SOURCE_RETIRED", manifest)
            target.activate(manifest, guard)
            bundle.record_phase("TARGET_READY", manifest)
            live()
            if self._read_selection() not in (self._cutover_selection, selection):
                raise MigrationRefused(
                    "incomplete activation private selection marker changed"
                )
            write_json_atomic(
                self.path / "selection.json",
                selection,
            )
            if self._read_selection() != selection:
                raise MigrationRefused("private selection marker was not retained")
            bundle.record_phase("CONFIG_SWITCHED", manifest)
            bundle.record_phase("COMPLETE", manifest)
            self._selection = target
            self._cutover_manifest = self._cutover_bundle = None
            self._cutover_stores = ()
            return {"phase": "COMPLETE", "migration_id": manifest["migration_id"]}

