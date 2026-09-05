"""Host-only foreground owner of a newly created disposable Windows namespace.

Private same-user composition, not authenticated external IPC or installed
old-writer exclusion. Losing this live owner cannot mint recovery authority.
"""

import json
import os
from pathlib import Path
from threading import RLock
import time
import uuid

from ..application.ports.runtime_owner import (
    OwnerRefused,
    OwnerUnsupported,
    OwnerCommitAmbiguous,
    PreparedOwnerOperation,
    prepare_owner_operation,
)
from ..application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ..adapters.filesystem.atomic_json import file_lock
from ..adapters.persistence.runtime_owner import SQLiteRuntimeOwnerJournal
from ..adapters.execution.runtime_owner import WindowsOwnedRuntimeProcess
from ..application.ports.jobs import JobStatus

_ISSUER = object()


class _LaunchPermit:
    def __init__(self, key, owner, command):
        if key is not _ISSUER:
            raise OwnerRefused("live host issuer required")
        self.owner, self.command = owner, command

    def require(self):
        self.owner._validate()
        if self.owner.journal.pending() != self.command:
            raise OwnerRefused("exact owner admission is no longer pending")


class DisposableRuntimeOwner:
    def __init__(self, path, *, writable_roots):
        self.path = Path(path).absolute()
        if self.path.exists():
            raise OwnerUnsupported(
                "cannot adopt an existing or installed owner namespace"
            )
        self._roots = writable_roots
        self._live = True
        self._lock = RLock()
        self._anchor = None
        self._gate = None
        self._process = None
        self._launch_id = None
        self._stopped = None
        self.namespace = uuid.uuid4().hex
        self._private_source_paths = (str(self.path),)
        self._validate()
        self._anchor = PrivateDirectoryAnchor.open_base(self.path)
        try:
            self._gate = file_lock(self.path / "launch", timeout=0)
            self._gate.__enter__()
            for name in ("state", "temp", "profile"):
                (self.path / name).mkdir()
            self.journal = SQLiteRuntimeOwnerJournal(
                self.path / "owner.sqlite", namespace=self.namespace, create=True
            )
            self._process = WindowsOwnedRuntimeProcess(self.path)
        except BaseException:
            self._live = False
            if self._gate is not None:
                self._gate.__exit__(None, None, None)
            self._anchor.close()
            raise

    @property
    def private_source_paths(self):
        """Exact owned private namespace for host admission inventory only."""
        return self._private_source_paths

    def status(self):
        with self._lock:
            self._validate()
            return {
                **self.journal.status(),
                "scope": "disposable-live-host",
                "installed_namespace_coverage": False,
                "authenticated_ipc": False,
            }

    def _validate(self):
        if not self._live:
            raise OwnerRefused("live owner launch exclusion is no longer held")
        if self._anchor is not None:
            self._anchor.validate()
        for root in self._roots():
            candidates = (Path(os.path.abspath(root)), Path(root).resolve())
            for private in (self.path, self.path.resolve()):
                if any(
                    private == candidate
                    or private.is_relative_to(candidate)
                    or candidate.is_relative_to(private)
                    for candidate in candidates
                ):
                    raise OwnerRefused("owner namespace overlaps writable roots")

    @staticmethod
    def _config(value):
        if (
            type(value) is not dict
            or set(value) != {"port"}
            or type(value["port"]) is not int
            or not 1024 <= value["port"] <= 65535
        ):
            raise OwnerRefused(
                "disposable owner accepts only a fixed numeric loopback port"
            )
        return value

    def prepare(self, operation_id, action, arguments):
        with self._lock:
            self._validate()
            if action == "select":
                self._config(arguments.get("config"))
            command = prepare_owner_operation(
                operation_id, action, self.journal.status()["revision"], arguments
            )
            self.journal.prepare(command)
            return command

    def _evidence(self, job_id):
        try:
            with self._anchor.open_read("runtime-" + job_id + ".json") as stream:
                raw = stream.read(16385)
        except FileNotFoundError:
            return None
        if len(raw) > 16384:
            raise OwnerRefused("owned runtime evidence exceeds bounds")
        value = json.loads(raw)
        record = self._process.registry.view(job_id)
        if (
            value.get("job_id") != job_id
            or value.get("namespace") != self.namespace
            or value.get("pid") != record.process_id
            or value.get("process_identity")
            != dict(record.metadata).get("process_instance_identity")
            or value.get("selection") != self.journal.status()["selection"]
        ):
            raise OwnerRefused("owned runtime evidence identity changed")
        return value

    def execute(self, command, *, timeout=30):
        if (
            type(command) is not PreparedOwnerOperation
            or type(timeout) not in (int, float)
            or not 1 <= timeout <= 30
        ):
            raise OwnerRefused("bounded exact owner command required")
        with self._lock:
            deadline = time.monotonic() + timeout
            command = PreparedOwnerOperation(
                command.operation_id,
                command.action,
                command.expected_revision,
                command.payload,
            )
            self._validate()
            replay = self.journal.prepare(command)
            if replay is not None:
                return replay
            permit = _LaunchPermit(_ISSUER, self, command)
            permit.require()
            if command.action == "select":
                self._config(json.loads(command.payload)["config"])
                return self._complete(
                    command, {"selected": True}, "STOPPED_CLEAN", deadline
                )
            if command.action == "launch":
                self._config(self.journal.selected_config())
                if self._launch_id is None:
                    # Retained before calling a provider that may have started a
                    # suspended child before raising. Never launch it twice.
                    self._launch_id = command.operation_id
                    self._process.launch(self.namespace, command)
                elif self._launch_id != command.operation_id:
                    raise OwnerRefused("another owned launch needs reconciliation")
                while time.monotonic() < deadline:
                    permit.require()
                    if not self._process.alive(self._launch_id):
                        raise OwnerRefused(
                            "owned runtime exited before readiness; retain launch identity"
                        )
                    evidence = self._evidence(self._launch_id)
                    if evidence and evidence.get("phase") == "READY":
                        return self._complete(
                            command, {"job_id": self._launch_id}, "RUNNING", deadline
                        )
                    time.sleep(0.1)
                raise OwnerRefused(
                    "owned runtime readiness is unresolved; retain launch identity"
                )
            if self._launch_id is None:
                raise OwnerUnsupported(
                    "no live owned process proof; restart recovery unavailable"
                )
            if self._stopped is None:
                waited = self._process.wait(
                    self._launch_id, max(0, deadline - time.monotonic())
                )
                if waited.timed_out:
                    cancelled = self._process.force_stop(self._launch_id)
                    if not cancelled.cleanup_completed:
                        raise OwnerRefused(
                            "owned process containment cleanup remains unresolved"
                        )
                    self._stopped = (
                        "STOPPED_UNCLEAN",
                        {"containment_empty": True, "application_closed": False},
                    )
                else:
                    evidence = self._evidence(self._launch_id)
                    clean = (
                        waited.record.status is JobStatus.SUCCEEDED
                        and evidence is not None
                        and evidence.get("phase") == "CLEAN"
                    )
                    if waited.record.status not in {
                        JobStatus.SUCCEEDED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                    }:
                        raise OwnerRefused(
                            "owned process cleanup has no terminal proof"
                        )
                    self._stopped = (
                        "STOPPED_CLEAN" if clean else "STOPPED_UNCLEAN",
                        {"containment_empty": True, "application_closed": clean},
                    )
            state, result = self._stopped
            receipt = self._complete(command, result, state, deadline)
            self._launch_id = None
            self._stopped = None
            return receipt

    def _complete(self, command, result, state, deadline):
        if time.monotonic() >= deadline:
            raise OwnerRefused(
                "owner deadline elapsed; reconcile the exact operation ID"
            )
        receipt = self.journal.complete(command, result, state)
        if time.monotonic() >= deadline:
            raise OwnerCommitAmbiguous(command)
        return receipt

    def close(self):
        with self._lock:
            if not self._live:
                return
            if self._launch_id is not None:
                result = self._process.force_stop(self._launch_id)
                if not result.cleanup_completed:
                    raise OwnerRefused("owned containment cleanup remains unresolved")
                self._launch_id = None
            self._live = False
            self._gate.__exit__(None, None, None)
            self._anchor.close()
