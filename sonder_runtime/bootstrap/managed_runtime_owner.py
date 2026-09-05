"""One live foreground owner for a required-new runtime and its child aggregate.

Existing namespaces cannot be adopted. This private host composition is not an
installed service manager or a transferable process fencing capability.
"""
from dataclasses import asdict
from hashlib import sha256
import json
import time

from .runtime_owner import DisposableRuntimeOwner
from .child_migration_activation import ChildMigrationActivation
from .managed_configuration import MANIFEST_DIGEST, read_configuration
from ..adapters.execution.runtime_owner import WindowsManagedRuntimeProcess
from ..adapters.persistence.runtime_owner import SQLiteManagedRuntimeOwnerJournal
from ..adapters.persistence.child_migration import SQLiteChildMigrationStore
from ..adapters.filesystem.atomic_json import write_json_atomic
from ..application.ports.managed_runtime_owner import PreparedManagedOwnerOperation, managed_operation
from ..application.ports.runtime_owner import OwnerRefused, OwnerUnsupported, canonical
from ..application.subagents.child_migration import digest
from ..application.subagents.child_migration_activation import _register_host_issuer, _unregister_host_issuer
from ..platform.child_storage_config import ChildStorageConfig


class ManagedRuntimeOwner(DisposableRuntimeOwner, ChildMigrationActivation):
    journal_type = SQLiteManagedRuntimeOwnerJournal
    process_type = WindowsManagedRuntimeProcess
    command_type = PreparedManagedOwnerOperation

    def __init__(self, path, *, writable_roots):
        super().__init__(path, writable_roots=writable_roots)
        self._application = self._repository = None
        self._tracked = 0
        self._cutover_manifest = self._cutover_bundle = self._cutover_selection = None
        self._cutover_stores = ()
        self._selection = SQLiteChildMigrationStore(self.path / "children.sqlite")
        self._catalog = set()
        self._activations = {}
        _register_host_issuer(self, self._require_quiescent)

    @property
    def selected_store(self):
        with self._lock:
            self.quiesce()
            if self.journal.pending() is not None:
                raise OwnerRefused("pending owner operation fences storage publication")
            return self._selection

    def quiesce(self, timeout=5):
        self._validate()
        if self._launch_id is not None or self.journal.status()["state"] not in {"STOPPED_CLEAN", "MIGRATING", "ACTIVATION_INCOMPLETE"}:
            raise OwnerRefused("exact contained runtime cleanup is required")

    def _require_quiescent(self, manifest):
        self.quiesce()
        pending = self.journal.pending()
        if pending is None or pending.action != "activate" or json.loads(pending.payload)["manifest_digest"] != digest(manifest):
            raise OwnerRefused("exact managed activation admission required")
        ChildMigrationActivation._require_quiescent(self, manifest)

    def register_configuration(self, *, port, target=None):
        with self._lock:
            self.quiesce()
            if self.journal.pending() is not None:
                raise OwnerRefused("pending operation fences configuration creation")
            target = self._selection if target is None else target
            if type(target) is not SQLiteChildMigrationStore or target.path.parent != self.path:
                raise OwnerUnsupported("this foreground runtime profile currently supports owned SQLite only")
            if len(self._catalog) >= 64:
                raise OwnerRefused("immutable configuration capacity exhausted")
            status = self.journal.status()
            value = dict(schema=1, namespace=self.namespace, incarnation=status["incarnation"], port=port,
                request_timeout_seconds=5, stream_idle_timeout_seconds=5, components=MANIFEST_DIGEST,
                child_storage=asdict(ChildStorageConfig()), child_path=str(target.path), child_identity=target.identity)
            reference = dict(generation=status["config_revision"]+1, digest=sha256(canonical(value)).hexdigest(),
                selector_revision=status["selector_revision"] + (target.identity != self._selection.identity))
            from .managed_configuration import validate_configuration
            validate_configuration(value, root=self.path, namespace=self.namespace, incarnation=status["incarnation"])
            filename = "configuration-" + reference["digest"] + ".json"
            if reference["digest"] not in self._catalog:
                write_json_atomic(self.path / filename, value)
                self._catalog.add(reference["digest"])
            self._read_config(reference)
            return reference

    def _read_config(self, reference):
        return read_configuration(self._anchor, reference, root=self.path, namespace=self.namespace,
            incarnation=self.journal.status()["incarnation"])

    def _config(self, reference):
        value = self._read_config(reference)
        if value["child_identity"] != self._selection.identity:
            raise OwnerRefused("configuration cannot bypass aggregate activation")
        return value

    def prepare(self, operation_id, action, arguments):
        with self._lock:
            self._validate()
            if action == "select":
                self._config(arguments.get("config"))
            elif action == "activate":
                if operation_id not in self._activations:
                    raise OwnerRefused("live registered migration required")
            command = managed_operation(operation_id, action, self.journal.status(), arguments)
            self.journal.prepare(command)
            return command

    def prepare_activation(self, operation_id, bundle, target, reference):
        with self._lock:
            self.quiesce()
            manifest = bundle.manifest()
            if self._read_config(reference)["child_identity"] != target.identity:
                raise OwnerRefused("activation configuration target differs")
            if operation_id in self._activations or len(self._activations) >= 64:
                raise OwnerRefused("activation registration conflict or capacity")
            self._activations[operation_id] = (bundle, self._selection, target)
            return self.prepare(operation_id, "activate", {"manifest_digest": digest(manifest), "target": reference})

    def activate(self, *args, **kwargs):
        raise OwnerRefused("activation requires an immutable managed owner operation")

    def execute(self, command, *, timeout=30):
        if type(command) is not PreparedManagedOwnerOperation:
            raise OwnerRefused("exact managed command required")
        if command.action != "activate":
            return super().execute(command, timeout=timeout)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise OwnerRefused("bounded managed deadline required")
        with self._lock:
            self._validate()
            deadline = time.monotonic() + timeout
            replay = self.journal.prepare(command)
            if replay is not None:
                return replay
            registered = self._activations.get(command.operation_id)
            if registered is None:
                raise OwnerUnsupported("activation requires its original live issuer")
            bundle, source, target = registered
            payload = json.loads(command.payload)
            if digest(bundle.manifest()) != payload["manifest_digest"] or self._read_config(payload["target"])["child_identity"] != target.identity:
                raise OwnerRefused("registered activation identity changed")
            self.journal.phase(command, "ACTIVATION_INCOMPLETE", payload)
            result = ChildMigrationActivation.activate(self, bundle, source, target, timeout=timeout)
            return self._complete(command, result, "STOPPED_CLEAN", deadline)

    def _launch_prepared(self, command):
        self.journal.phase(command, "STARTING", {"config": self.journal.selected_config(), "manifest": MANIFEST_DIGEST})
        super()._launch_prepared(command)

    def _evidence(self, job_id):
        value = super()._evidence(job_id)
        if value is not None:
            status = self.journal.status()
            if value.get("incarnation") != status["incarnation"] or value.get("epoch") != status["epoch"] or value.get("manifest") != MANIFEST_DIGEST:
                raise OwnerRefused("managed runtime evidence ownership changed")
            if value.get("phase") == "CLEAN":
                rows = value.get("components")
                from .managed_configuration import COMPONENTS
                if type(rows) is not list or len(rows) != len(COMPONENTS) or {item.get("component") for item in rows} != set(COMPONENTS) or any(item.get("state") != "CLOSED" for item in rows):
                    raise OwnerRefused("full managed cleanup manifest is missing")
        return value

    def close(self):
        super().close()
        _unregister_host_issuer(self)
