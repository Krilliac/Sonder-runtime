"""Catalog-bound remote compute job contracts and worker orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from threading import RLock
from typing import Any, Mapping

from ..execution.process_jobs import ProcessJobProvider, ProcessJobRequest
from ..ports.jobs import JobIdentity
from ...domain.common.errors import Conflict, InvalidInput, NotFound
from ...domain.compute_fabric import WorkloadKind


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENVIRONMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError(f"{label} must be a bounded stable identity")
    return value


def _relative(value: str, label: str, *, allow_dot: bool = True) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError(f"{label} must be a bounded relative path")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if normalized.startswith("/") or ":" in path.parts[0] or ".." in path.parts:
        raise ValueError(f"{label} must not escape its workspace")
    if not allow_dot and normalized in ("", "."):
        raise ValueError(f"{label} must name a relative artifact")
    return normalized


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ArgumentPolicy(StrEnum):
    NONE = "none"
    BOUNDED = "bounded"
    RELATIVE_PATHS_AND_TEST_SELECTORS = "relative-paths-and-test-selectors"


@dataclass(frozen=True, slots=True)
class JobCatalogEntry:
    entry_id: str
    workload: WorkloadKind
    program: str
    fixed_args: tuple[str, ...] = ()
    argument_policy: ArgumentPolicy = ArgumentPolicy.NONE
    environment_allowlist: frozenset[str] = frozenset()
    workspace_mappings: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _identity(self.entry_id, "catalog entry id")
        if self.workload is WorkloadKind.INFERENCE:
            raise ValueError("inference remains owned by the model gateway")
        if not isinstance(self.program, str) or not self.program or len(self.program) > 4096:
            raise ValueError("catalog program must be non-empty and bounded")
        if len(self.fixed_args) > 64 or any(
            not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value
            for value in self.fixed_args
        ):
            raise ValueError("catalog fixed_args exceeds its bound")
        if not isinstance(self.argument_policy, ArgumentPolicy):
            raise ValueError("catalog argument_policy is invalid")
        if len(self.environment_allowlist) > 32 or any(
            not _ENVIRONMENT.fullmatch(value) for value in self.environment_allowlist
        ):
            raise ValueError("catalog environment allowlist is invalid")
        for mapping in self.workspace_mappings:
            _identity(mapping, "catalog workspace mapping")

    def argv_for(self, arguments: tuple[str, ...]) -> tuple[str, ...]:
        _validate_arguments(arguments, self.argument_policy)
        return (self.program, *self.fixed_args, *arguments)

    def environment_for(
        self,
        environment: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[str, str], ...]:
        seen: set[str] = set()
        for key, value in environment:
            if key not in self.environment_allowlist:
                raise ValueError(f"environment key {key!r} is not allowed by the catalog")
            if key in seen:
                raise ValueError("environment keys must be unique")
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise ValueError("environment value exceeds its bound")
            seen.add(key)
        return tuple(sorted(environment))


def _validate_arguments(arguments: tuple[str, ...], policy: ArgumentPolicy) -> None:
    if not isinstance(arguments, tuple) or len(arguments) > 64 or any(
        not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value
        for value in arguments
    ):
        raise ValueError("arguments exceed their bound")
    if policy is ArgumentPolicy.NONE and arguments:
        raise ValueError("catalog entry does not accept arguments")
    if policy is ArgumentPolicy.RELATIVE_PATHS_AND_TEST_SELECTORS:
        for value in arguments:
            if value.startswith("-"):
                continue
            path_part = value.split("::", 1)[0]
            _relative(path_part, "argument")


@dataclass(frozen=True, slots=True)
class RemoteJobEnvelope:
    controller_job_id: str
    idempotency_key: str
    workload: WorkloadKind
    catalog_entry_id: str
    workspace_mapping: str
    relative_cwd: str
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    deadline_seconds: int
    idempotent: bool
    request_sha256: str

    @classmethod
    def create(
        cls,
        *,
        controller_job_id: str,
        idempotency_key: str,
        workload: WorkloadKind,
        catalog_entry_id: str,
        workspace_mapping: str,
        relative_cwd: str = ".",
        arguments: tuple[str, ...] = (),
        environment: tuple[tuple[str, str], ...] = (),
        deadline_seconds: int = 300,
        idempotent: bool = False,
    ) -> "RemoteJobEnvelope":
        values = {
            "controller_job_id": controller_job_id,
            "idempotency_key": idempotency_key,
            "workload": workload,
            "catalog_entry_id": catalog_entry_id,
            "workspace_mapping": workspace_mapping,
            "relative_cwd": relative_cwd,
            "arguments": arguments,
            "environment": environment,
            "deadline_seconds": deadline_seconds,
            "idempotent": idempotent,
        }
        canonical = cls._canonical_values(**values)
        return cls(**values, request_sha256=_digest(canonical))

    @staticmethod
    def _canonical_values(**values: Any) -> dict[str, Any]:
        _identity(values["controller_job_id"], "controller_job_id")
        _identity(values["idempotency_key"], "idempotency_key")
        if not isinstance(values["workload"], WorkloadKind):
            raise ValueError("workload is invalid")
        if values["workload"] is WorkloadKind.INFERENCE:
            raise ValueError("inference remains owned by the model gateway")
        _identity(values["catalog_entry_id"], "catalog_entry_id")
        _identity(values["workspace_mapping"], "workspace_mapping")
        relative_cwd = _relative(values["relative_cwd"], "relative_cwd")
        arguments = values["arguments"]
        _validate_arguments(arguments, ArgumentPolicy.BOUNDED)
        for argument in arguments:
            if argument.startswith("-"):
                continue
            _relative(argument.split("::", 1)[0], "argument")
        environment = values["environment"]
        if not isinstance(environment, tuple) or len(environment) > 32:
            raise ValueError("environment exceeds its bound")
        seen: set[str] = set()
        for pair in environment:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("environment entries must be key/value pairs")
            key, value = pair
            if not isinstance(key, str) or not _ENVIRONMENT.fullmatch(key) or key in seen:
                raise ValueError("environment keys must be unique valid names")
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise ValueError("environment value exceeds its bound")
            seen.add(key)
        deadline = values["deadline_seconds"]
        if isinstance(deadline, bool) or not isinstance(deadline, int) or not 1 <= deadline <= 86_400:
            raise ValueError("deadline_seconds must be within 1..86400")
        if not isinstance(values["idempotent"], bool):
            raise ValueError("idempotent must be boolean")
        return {
            "controller_job_id": values["controller_job_id"],
            "idempotency_key": values["idempotency_key"],
            "workload": values["workload"].value,
            "catalog_entry_id": values["catalog_entry_id"],
            "workspace_mapping": values["workspace_mapping"],
            "relative_cwd": relative_cwd,
            "arguments": list(arguments),
            "environment": [list(pair) for pair in sorted(environment)],
            "deadline_seconds": deadline,
            "idempotent": values["idempotent"],
        }

    def __post_init__(self) -> None:
        self.verify()

    def canonical(self) -> dict[str, Any]:
        return self._canonical_values(
            controller_job_id=self.controller_job_id,
            idempotency_key=self.idempotency_key,
            workload=self.workload,
            catalog_entry_id=self.catalog_entry_id,
            workspace_mapping=self.workspace_mapping,
            relative_cwd=self.relative_cwd,
            arguments=self.arguments,
            environment=self.environment,
            deadline_seconds=self.deadline_seconds,
            idempotent=self.idempotent,
        )

    def verify(self) -> None:
        if not isinstance(self.request_sha256, str) or not _SHA256.fullmatch(self.request_sha256):
            raise ValueError("request digest must be a SHA-256 value")
        if _digest(self.canonical()) != self.request_sha256:
            raise ValueError("request digest does not match the envelope")


@dataclass(frozen=True, slots=True)
class RemoteArtifactReceipt:
    name: str
    size_bytes: int
    mime_type: str
    sha256: str

    def __post_init__(self) -> None:
        _relative(self.name, "artifact name", allow_dot=False)
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("artifact size must be non-negative")
        if not isinstance(self.mime_type, str) or not self.mime_type or len(self.mime_type) > 255:
            raise ValueError("artifact MIME type must be bounded")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError("artifact digest must be SHA-256")


@dataclass(frozen=True, slots=True)
class RemoteJobReceipt:
    worker_id: str
    remote_job_id: str
    controller_job_id: str
    idempotency_key: str
    request_sha256: str
    state: str
    process_id: int | None = None
    artifacts: tuple[RemoteArtifactReceipt, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.worker_id, "worker_id"),
            (self.remote_job_id, "remote_job_id"),
            (self.controller_job_id, "controller_job_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identity(value, label)
        if not _SHA256.fullmatch(self.request_sha256):
            raise ValueError("receipt request digest must be SHA-256")
        if not isinstance(self.state, str) or not self.state or len(self.state) > 64:
            raise ValueError("receipt state must be bounded")
        if self.process_id is not None and (
            isinstance(self.process_id, bool) or not isinstance(self.process_id, int) or self.process_id < 1
        ):
            raise ValueError("receipt process id must be positive")
        if len(self.artifacts) > 256:
            raise ValueError("receipt artifact count exceeds its bound")


class ComputeJobWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        catalog: Mapping[str, JobCatalogEntry],
        workspace_mappings: Mapping[str, Path],
        provider: ProcessJobProvider,
    ) -> None:
        _identity(worker_id, "worker_id")
        if set(catalog) != {entry.entry_id for entry in catalog.values()}:
            raise ValueError("catalog keys must exactly match entry identities")
        roots: dict[str, Path] = {}
        for name, root in workspace_mappings.items():
            _identity(name, "workspace mapping")
            resolved = Path(root).resolve()
            if not resolved.is_absolute():
                raise ValueError("workspace mapping roots must be absolute")
            roots[name] = resolved
        self.worker_id = worker_id
        self._catalog = dict(catalog)
        self._workspaces = roots
        self._provider = provider
        self._by_idempotency: dict[str, RemoteJobReceipt] = {}
        self._by_job: dict[str, RemoteJobReceipt] = {}
        self._lock = RLock()

    def submit(self, envelope: RemoteJobEnvelope) -> RemoteJobReceipt:
        try:
            envelope.verify()
        except (TypeError, ValueError) as exc:
            raise InvalidInput(f"compute job envelope digest/shape is invalid: {exc}") from exc
        with self._lock:
            prior = self._by_idempotency.get(envelope.idempotency_key)
            if prior is not None:
                if prior.request_sha256 != envelope.request_sha256:
                    raise Conflict("idempotency key is already bound to another request")
                return prior
        entry = self._catalog.get(envelope.catalog_entry_id)
        if entry is None:
            raise InvalidInput("compute job catalog entry is not configured")
        if entry.workload is not envelope.workload:
            raise InvalidInput("compute job workload does not match its catalog entry")
        if envelope.workspace_mapping not in entry.workspace_mappings:
            raise InvalidInput("compute job workspace is not allowed by its catalog entry")
        root = self._workspaces.get(envelope.workspace_mapping)
        if root is None:
            raise InvalidInput("compute job workspace mapping is not configured")
        try:
            cwd = (root / envelope.relative_cwd).resolve()
            if not cwd.is_relative_to(root):
                raise ValueError("workspace escape")
            argv = entry.argv_for(envelope.arguments)
            environment = entry.environment_for(envelope.environment)
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc
        remote_job_id = "cf-" + hashlib.sha256(
            f"{self.worker_id}\x00{envelope.idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        request = ProcessJobRequest(
            identity=JobIdentity(
                job_id=remote_job_id,
                kind=f"compute-{envelope.workload.value}",
                operation_id=envelope.controller_job_id,
                idempotency_key=envelope.idempotency_key,
            ),
            argv=argv,
            cwd=cwd,
            environment=environment,
        )
        started = self._provider.start(request)
        receipt = RemoteJobReceipt(
            worker_id=self.worker_id,
            remote_job_id=remote_job_id,
            controller_job_id=envelope.controller_job_id,
            idempotency_key=envelope.idempotency_key,
            request_sha256=envelope.request_sha256,
            state=started.record.status.value,
            process_id=started.process_id,
        )
        with self._lock:
            prior = self._by_idempotency.get(envelope.idempotency_key)
            if prior is not None:
                if prior.request_sha256 != envelope.request_sha256:
                    raise Conflict("idempotency key is already bound to another request")
                return prior
            self._by_idempotency[envelope.idempotency_key] = receipt
            self._by_job[remote_job_id] = receipt
        return receipt

    def status(self, remote_job_id: str) -> RemoteJobReceipt:
        _identity(remote_job_id, "remote_job_id")
        with self._lock:
            receipt = self._by_job.get(remote_job_id)
        if receipt is None:
            raise NotFound("compute job was not found")
        return receipt

    def by_idempotency(self, idempotency_key: str) -> RemoteJobReceipt | None:
        _identity(idempotency_key, "idempotency_key")
        with self._lock:
            return self._by_idempotency.get(idempotency_key)

    def cancel(self, remote_job_id: str, reason: str = "cancelled") -> RemoteJobReceipt:
        receipt = self.status(remote_job_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise InvalidInput("cancellation reason must be non-empty and bounded")
        self._provider.cancel(remote_job_id, reason=reason)
        cancelled = RemoteJobReceipt(
            worker_id=receipt.worker_id,
            remote_job_id=receipt.remote_job_id,
            controller_job_id=receipt.controller_job_id,
            idempotency_key=receipt.idempotency_key,
            request_sha256=receipt.request_sha256,
            state="cancellation_requested",
            process_id=receipt.process_id,
            artifacts=receipt.artifacts,
        )
        with self._lock:
            self._by_job[remote_job_id] = cancelled
            self._by_idempotency[receipt.idempotency_key] = cancelled
        return cancelled


__all__ = [
    "ArgumentPolicy",
    "ComputeJobWorker",
    "JobCatalogEntry",
    "RemoteArtifactReceipt",
    "RemoteJobEnvelope",
    "RemoteJobReceipt",
]
