"""Catalog-bound remote compute job contracts and worker orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat as stat_module
import tempfile
from threading import RLock
from typing import Any, Mapping

from ..execution.process_jobs import ProcessJobProvider, ProcessJobRequest
from ..ports.jobs import JobIdentity
from ...domain.common.errors import Conflict, DependencyUnavailable, InvalidInput, NotFound
from ...domain.compute_fabric import WorkloadKind
from .artifact_spool import (
    ArtifactSpoolConflict,
    ArtifactSpoolError,
    PrivateDirectoryAnchor,
)


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENVIRONMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_OPTION = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BOUNDED_OPTION_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REMOTE_JOB_STATES = frozenset({
    "pending", "claimed", "running", "paused", "cancelling",
    "cancellation_requested", "succeeded", "failed", "cancelled", "interrupted",
})
MAX_COMPUTE_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_COMPUTE_ARTIFACTS = 256
_ARTIFACT_LOCK_STRIPES = 64


def _identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError(f"{label} must be a bounded stable identity")
    return value


def _relative(value: str, label: str, *, allow_dot: bool = True) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError(f"{label} must be a bounded relative path")
    normalized = value.replace("\\", "/")
    if normalized == ".":
        if allow_dot:
            return normalized
        raise ValueError(f"{label} must name a relative artifact")
    path = PurePosixPath(normalized)
    if (
        not path.parts
        or normalized.startswith("/")
        or ":" in path.parts[0]
        or ".." in path.parts
    ):
        raise ValueError(f"{label} must not escape its workspace")
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
class DigestBoundInput:
    name: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _relative(self.name, "input artifact name", allow_dot=False)
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= 1 << 40
        ):
            raise ValueError("input artifact size must be within 0..2^40")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError("input artifact digest must be SHA-256")


@dataclass(frozen=True, slots=True)
class JobCatalogEntry:
    entry_id: str
    workload: WorkloadKind
    program: str
    fixed_args: tuple[str, ...] = ()
    argument_policy: ArgumentPolicy = ArgumentPolicy.NONE
    environment_allowlist: frozenset[str] = frozenset()
    workspace_mappings: frozenset[str] = frozenset()
    allowed_flags: frozenset[str] = frozenset()
    allowed_bounded_options: frozenset[str] = frozenset()
    allowed_relative_path_options: frozenset[str] = frozenset()
    memory_limit_bytes: int | None = None
    artifact_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identity(self.entry_id, "catalog entry id")
        if self.workload is WorkloadKind.INFERENCE:
            raise ValueError("inference remains owned by the model gateway")
        if (
            not isinstance(self.program, str)
            or not self.program
            or len(self.program) > 4096
            or "\x00" in self.program
            or not (
                PurePosixPath(self.program).is_absolute()
                or PureWindowsPath(self.program).is_absolute()
            )
        ):
            raise ValueError("catalog program must be an absolute bounded path")
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
        option_sets = (
            self.allowed_flags,
            self.allowed_bounded_options,
            self.allowed_relative_path_options,
        )
        if any(not _OPTION.fullmatch(option) for values in option_sets for option in values):
            raise ValueError("catalog options must be bounded option names")
        if any(left & right for index, left in enumerate(option_sets) for right in option_sets[index + 1:]):
            raise ValueError("catalog option names must have one value policy")
        if self.memory_limit_bytes is not None and (
            isinstance(self.memory_limit_bytes, bool)
            or not isinstance(self.memory_limit_bytes, int)
            or not 1 <= self.memory_limit_bytes <= 1 << 50
        ):
            raise ValueError("catalog memory_limit_bytes must be within 1..2^50")
        if len(self.artifact_paths) > MAX_COMPUTE_ARTIFACTS:
            raise ValueError(
                f"catalog artifact path count exceeds {MAX_COMPUTE_ARTIFACTS}"
            )
        normalized_artifacts = [
            _relative(path, "catalog artifact path", allow_dot=False)
            for path in self.artifact_paths
        ]
        if len(normalized_artifacts) != len(set(normalized_artifacts)):
            raise ValueError("catalog artifact paths must be unique")

    def argv_for(self, arguments: tuple[str, ...]) -> tuple[str, ...]:
        _validate_arguments(
            arguments,
            self.argument_policy,
            allowed_flags=self.allowed_flags,
            allowed_bounded_options=self.allowed_bounded_options,
            allowed_relative_path_options=self.allowed_relative_path_options,
        )
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


def _validate_arguments(
    arguments: tuple[str, ...],
    policy: ArgumentPolicy,
    *,
    allowed_flags: frozenset[str] = frozenset(),
    allowed_bounded_options: frozenset[str] = frozenset(),
    allowed_relative_path_options: frozenset[str] = frozenset(),
) -> None:
    if not isinstance(arguments, tuple) or len(arguments) > 64 or any(
        not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value
        for value in arguments
    ):
        raise ValueError("arguments exceed their bound")
    if policy is ArgumentPolicy.NONE and arguments:
        raise ValueError("catalog entry does not accept arguments")
    for value in arguments:
        if value.startswith("-"):
            option, separator, option_value = value.partition("=")
            if not _OPTION.fullmatch(option):
                raise ValueError("argument option name is invalid")
            if not separator:
                if option not in allowed_flags:
                    raise ValueError(f"argument option {option!r} is not allowed by the catalog")
                continue
            if option in allowed_bounded_options:
                if not _BOUNDED_OPTION_VALUE.fullmatch(option_value):
                    raise ValueError(f"argument option {option!r} has an invalid bounded value")
                continue
            if option in allowed_relative_path_options:
                _relative(option_value, f"argument option {option!r}")
                continue
            raise ValueError(f"argument option {option!r} is not allowed by the catalog")
        if policy is ArgumentPolicy.RELATIVE_PATHS_AND_TEST_SELECTORS:
            path_part = value.split("::", 1)[0]
            _relative(path_part, "argument")


def _validate_envelope_arguments(arguments: tuple[str, ...]) -> None:
    """Validate transport shape without granting any catalog option authority."""
    if not isinstance(arguments, tuple) or len(arguments) > 64 or any(
        not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value
        for value in arguments
    ):
        raise ValueError("arguments exceed their bound")
    for value in arguments:
        if value.startswith("-"):
            option = value.partition("=")[0]
            if not _OPTION.fullmatch(option):
                raise ValueError("argument option name is invalid")
            continue
        _relative(value.split("::", 1)[0], "argument")


def _relative_argument_paths(
    entry: JobCatalogEntry,
    arguments: tuple[str, ...],
) -> tuple[str, ...]:
    paths: list[str] = []
    for value in arguments:
        if value.startswith("-"):
            option, separator, option_value = value.partition("=")
            if separator and option in entry.allowed_relative_path_options:
                paths.append(option_value)
        elif entry.argument_policy is ArgumentPolicy.RELATIVE_PATHS_AND_TEST_SELECTORS:
            paths.append(value.split("::", 1)[0])
    return tuple(paths)


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
    input_artifacts: tuple[DigestBoundInput, ...] = ()

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
        input_artifacts: tuple[DigestBoundInput, ...] = (),
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
            "input_artifacts": input_artifacts,
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
        _validate_envelope_arguments(arguments)
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
        input_artifacts = values["input_artifacts"]
        if (
            not isinstance(input_artifacts, tuple)
            or len(input_artifacts) > 64
            or any(not isinstance(item, DigestBoundInput) for item in input_artifacts)
        ):
            raise ValueError("input artifacts must be a bounded typed tuple")
        names = [item.name for item in input_artifacts]
        if len(names) != len(set(names)):
            raise ValueError("input artifact names must be unique")
        if sum(item.size_bytes for item in input_artifacts) > 1 << 40:
            raise ValueError("input artifact total exceeds 2^40 bytes")
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
            "input_artifacts": [
                {"name": item.name, "size_bytes": item.size_bytes, "sha256": item.sha256}
                for item in sorted(input_artifacts, key=lambda item: item.name)
            ],
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
            input_artifacts=self.input_artifacts,
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
class RemoteArtifactPayload:
    receipt: RemoteArtifactReceipt
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, RemoteArtifactReceipt):
            raise TypeError("artifact payload requires a receipt")
        if not isinstance(self.content, bytes):
            raise TypeError("artifact payload content must be bytes")
        if len(self.content) != self.receipt.size_bytes:
            raise ValueError("artifact payload size does not match its receipt")
        if hashlib.sha256(self.content).hexdigest() != self.receipt.sha256:
            raise ValueError("artifact payload digest does not match its receipt")


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
    output_preview: str = ""
    output_watermark: int = 0
    output_truncated: bool = False

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
        if self.state not in _REMOTE_JOB_STATES:
            raise ValueError("receipt state is not a recognized bounded state")
        if self.process_id is not None and (
            isinstance(self.process_id, bool) or not isinstance(self.process_id, int) or self.process_id < 1
        ):
            raise ValueError("receipt process id must be positive")
        if len(self.artifacts) > MAX_COMPUTE_ARTIFACTS:
            raise ValueError("receipt artifact count exceeds its bound")
        if not isinstance(self.output_preview, str) or len(
            self.output_preview.encode("utf-8")
        ) > 16 * 1024:
            raise ValueError("receipt output preview exceeds its bound")
        if (
            isinstance(self.output_watermark, bool)
            or not isinstance(self.output_watermark, int)
            or self.output_watermark < 0
        ):
            raise ValueError("receipt output watermark must be non-negative")
        if not isinstance(self.output_truncated, bool):
            raise ValueError("receipt output truncation flag must be boolean")


def validate_remote_job_receipt(
    receipt: RemoteJobReceipt,
    *,
    worker_id: str,
    controller_job_id: str | None = None,
    idempotency_key: str | None = None,
    request_sha256: str | None = None,
    remote_job_id: str | None = None,
) -> RemoteJobReceipt:
    """Fail closed unless a receipt belongs to the exact requested operation."""
    if not isinstance(receipt, RemoteJobReceipt):
        raise DependencyUnavailable("compute job response is not a receipt")
    expected = (
        ("worker identity", receipt.worker_id, worker_id),
        ("controller identity", receipt.controller_job_id, controller_job_id),
        ("idempotency identity", receipt.idempotency_key, idempotency_key),
        ("request digest", receipt.request_sha256, request_sha256),
        ("remote job identity", receipt.remote_job_id, remote_job_id),
    )
    for label, actual, wanted in expected:
        if wanted is not None and actual != wanted:
            raise DependencyUnavailable(f"compute job receipt {label} mismatch")
    return receipt


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
        self._artifact_context: dict[
            str, tuple[Path, Path, tuple[str, ...]]
        ] = {}
        self._input_stages: dict[str, Path] = {}
        self._lock = RLock()
        self._artifact_locks = tuple(RLock() for _ in range(_ARTIFACT_LOCK_STRIPES))
        try:
            self._artifact_root = PrivateDirectoryAnchor.open_base(
                self._artifact_stage_base()
            )
        except (ArtifactSpoolError, OSError) as exc:
            raise InvalidInput("private compute artifact spool is unsafe") from exc
        self._rehydrate()

    def _rehydrate(self) -> None:
        recover = getattr(self._provider, "recover", None)
        if not callable(recover):
            return
        for view in recover(kind_prefix="compute-", limit=1024):
            record = view.record
            metadata = getattr(view, "metadata", None) or {}
            if metadata.get("compute_worker_id") != self.worker_id:
                continue
            controller_job_id = metadata.get("compute_controller_job_id")
            request_sha256 = metadata.get("compute_request_sha256")
            if not isinstance(controller_job_id, str) or not isinstance(request_sha256, str):
                continue
            try:
                receipt = RemoteJobReceipt(
                    worker_id=self.worker_id,
                    remote_job_id=record.identity.job_id,
                    controller_job_id=controller_job_id,
                    idempotency_key=record.identity.idempotency_key,
                    request_sha256=request_sha256,
                    state=record.status.value,
                    process_id=getattr(view, "process_id", None),
                )
            except ValueError:
                continue
            prior = self._by_idempotency.get(receipt.idempotency_key)
            if prior is not None and prior.request_sha256 != receipt.request_sha256:
                raise Conflict("durable compute idempotency metadata is inconsistent")
            self._by_idempotency[receipt.idempotency_key] = receipt
            self._by_job[receipt.remote_job_id] = receipt
            catalog_entry_id = metadata.get("compute_catalog_entry_id")
            workspace_mapping = metadata.get("compute_workspace_mapping")
            relative_cwd = metadata.get("compute_relative_cwd")
            entry = self._catalog.get(catalog_entry_id)
            root = self._workspaces.get(workspace_mapping)
            if (
                entry is not None
                and root is not None
                and isinstance(relative_cwd, str)
            ):
                cwd = (root / relative_cwd).resolve()
                if cwd.is_relative_to(root):
                    self._artifact_context[receipt.remote_job_id] = (
                        root, cwd, entry.artifact_paths,
                    )
            raw_stage = metadata.get("compute_input_stage")
            if isinstance(raw_stage, str) and raw_stage:
                stage = Path(raw_stage).resolve()
                base = self._input_stage_base().resolve()
                if stage.is_relative_to(base) and stage.is_dir():
                    self._input_stages[receipt.remote_job_id] = stage

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
            for relative_path in _relative_argument_paths(entry, envelope.arguments):
                resolved_argument = (cwd / relative_path).resolve()
                if not resolved_argument.is_relative_to(root):
                    raise ValueError("argument path would escape its configured workspace")
            environment = entry.environment_for(envelope.environment)
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc
        remote_job_id = "cf-" + hashlib.sha256(
            f"{self.worker_id}\x00{envelope.idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        input_stage = None
        if envelope.input_artifacts:
            try:
                input_stage, argv = self._stage_inputs(
                    remote_job_id,
                    root,
                    cwd,
                    argv,
                    envelope.input_artifacts,
                )
            except ValueError as exc:
                raise InvalidInput(str(exc)) from exc
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
            deadline_seconds=envelope.deadline_seconds,
            memory_limit_bytes=entry.memory_limit_bytes,
            require_job_scope=True,
            metadata=(
                ("compute_worker_id", self.worker_id),
                ("compute_controller_job_id", envelope.controller_job_id),
                ("compute_request_sha256", envelope.request_sha256),
                ("compute_catalog_entry_id", envelope.catalog_entry_id),
                ("compute_workspace_mapping", envelope.workspace_mapping),
                ("compute_relative_cwd", envelope.relative_cwd),
                ("compute_input_stage", "" if input_stage is None else str(input_stage)),
            ),
        )
        with self._lock:
            prior = self._by_idempotency.get(envelope.idempotency_key)
            if prior is not None:
                if prior.request_sha256 != envelope.request_sha256:
                    raise Conflict("idempotency key is already bound to another request")
                return prior
            # Keep reservation, process creation, and receipt publication in one
            # worker critical section. The provider durably reserves the stable
            # job identity before it creates the process, so concurrent callers
            # cannot execute the same idempotency key twice.
            try:
                started = self._provider.start(request)
            except Exception:
                if input_stage is not None:
                    self._remove_input_stage(input_stage)
                poll = getattr(self._provider, "poll", None)
                if callable(poll):
                    try:
                        failed_record = poll(remote_job_id)
                    except Exception:
                        failed_record = None
                    failed_status = getattr(failed_record, "status", None)
                    failed_state = getattr(failed_status, "value", None)
                    if failed_state in {
                        "failed", "cancellation_requested", "cancelled", "interrupted",
                    }:
                        failed = RemoteJobReceipt(
                            worker_id=self.worker_id,
                            remote_job_id=remote_job_id,
                            controller_job_id=envelope.controller_job_id,
                            idempotency_key=envelope.idempotency_key,
                            request_sha256=envelope.request_sha256,
                            state=failed_state,
                            process_id=None,
                        )
                        self._by_idempotency[envelope.idempotency_key] = failed
                        self._by_job[remote_job_id] = failed
                raise
            receipt = RemoteJobReceipt(
                worker_id=self.worker_id,
                remote_job_id=remote_job_id,
                controller_job_id=envelope.controller_job_id,
                idempotency_key=envelope.idempotency_key,
                request_sha256=envelope.request_sha256,
                state=started.record.status.value,
                process_id=started.process_id,
            )
            self._by_idempotency[envelope.idempotency_key] = receipt
            self._by_job[remote_job_id] = receipt
            self._artifact_context[remote_job_id] = (
                root, cwd, entry.artifact_paths,
            )
            if input_stage is not None:
                self._input_stages[remote_job_id] = input_stage
        return receipt

    def _collect_artifacts(self, receipt: RemoteJobReceipt) -> RemoteJobReceipt:
        if receipt.artifacts or receipt.state not in {
            "succeeded", "failed", "cancelled", "interrupted",
        }:
            return receipt
        context = self._artifact_context.get(receipt.remote_job_id)
        if context is None:
            return receipt
        root, cwd, artifact_paths = context
        artifact_lock = self._artifact_lock_for(receipt.remote_job_id)
        with artifact_lock:
            with self._artifact_stage(receipt) as stage:
                durable = self._load_artifact_manifest(stage, receipt)
                if durable is not None:
                    return self._with_artifacts(receipt, durable)
                artifacts: list[RemoteArtifactReceipt] = []
                for relative_path in artifact_paths:
                    candidate = (cwd / relative_path).resolve()
                    if not candidate.is_relative_to(root):
                        raise InvalidInput(
                            "catalog artifact would escape its configured workspace"
                        )
                    snapshot_name = self._artifact_snapshot_name(
                        receipt.request_sha256, relative_path,
                    )
                    if stage.exists(snapshot_name):
                        raise Conflict(
                            "compute artifact snapshot exists without a request-bound receipt"
                        )
                    try:
                        size_bytes, sha256 = self._snapshot_artifact(
                            stage, root, candidate, snapshot_name,
                        )
                    except FileNotFoundError:
                        continue
                    except ArtifactSpoolConflict as exc:
                        raise Conflict(str(exc)) from exc
                    except (ArtifactSpoolError, OSError) as exc:
                        raise InvalidInput(
                            "catalog artifact could not be snapshotted"
                        ) from exc
                    mime_type = (
                        mimetypes.guess_type(candidate.name)[0]
                        or "application/octet-stream"
                    )
                    artifacts.append(RemoteArtifactReceipt(
                        name=relative_path,
                        size_bytes=size_bytes,
                        mime_type=mime_type,
                        sha256=sha256,
                    ))
                self._publish_artifact_manifest(stage, receipt, tuple(artifacts))
                return self._with_artifacts(receipt, tuple(artifacts))

    @staticmethod
    def _with_artifacts(
        receipt: RemoteJobReceipt,
        artifacts: tuple[RemoteArtifactReceipt, ...],
    ) -> RemoteJobReceipt:
        return RemoteJobReceipt(
            worker_id=receipt.worker_id,
            remote_job_id=receipt.remote_job_id,
            controller_job_id=receipt.controller_job_id,
            idempotency_key=receipt.idempotency_key,
            request_sha256=receipt.request_sha256,
            state=receipt.state,
            process_id=receipt.process_id,
            artifacts=artifacts,
            output_preview=receipt.output_preview,
            output_watermark=receipt.output_watermark,
            output_truncated=receipt.output_truncated,
        )

    @staticmethod
    def _artifact_stage_base() -> Path:
        return Path(tempfile.gettempdir()) / "sonder-compute-artifacts"

    @staticmethod
    def _artifact_snapshot_name(request_sha256: str, name: str) -> str:
        return hashlib.new(
            "sha256", f"{request_sha256}\x00{name}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _artifact_binding(receipt: RemoteJobReceipt) -> dict[str, str]:
        return {
            "worker_id": receipt.worker_id,
            "remote_job_id": receipt.remote_job_id,
            "controller_job_id": receipt.controller_job_id,
            "idempotency_key": receipt.idempotency_key,
            "request_sha256": receipt.request_sha256,
        }

    def _artifact_lock_for(self, remote_job_id: str) -> RLock:
        stripe = int.from_bytes(
            hashlib.new("sha256", remote_job_id.encode("utf-8")).digest()[:8], "big"
        ) % len(self._artifact_locks)
        return self._artifact_locks[stripe]

    def _artifact_stage(self, receipt: RemoteJobReceipt) -> PrivateDirectoryAnchor:
        binding = self._artifact_binding(receipt)
        job_key = hashlib.new(
            "sha256", receipt.remote_job_id.encode("utf-8")
        ).hexdigest()
        try:
            stage, created = self._artifact_root.child(job_key)
        except (ArtifactSpoolError, OSError) as exc:
            raise InvalidInput("private compute artifact job spool is unsafe") from exc
        if created:
            try:
                stage.write_json_once("binding.json", binding)
            except FileExistsError:
                pass
        try:
            raw = stage.read_bytes("binding.json", max_bytes=16 * 1024)
            observed = json.loads(raw.decode("utf-8"))
        except FileNotFoundError as exc:
            stage.close()
            raise Conflict("compute artifact spool is not request-bound") from exc
        except (ArtifactSpoolError, OSError, UnicodeError, ValueError) as exc:
            stage.close()
            raise InvalidInput("compute artifact spool binding is invalid") from exc
        if observed != binding:
            stage.close()
            raise Conflict("compute artifact spool belongs to a different request")
        return stage

    def _load_artifact_manifest(
        self,
        stage: PrivateDirectoryAnchor,
        receipt: RemoteJobReceipt,
    ) -> tuple[RemoteArtifactReceipt, ...] | None:
        if not stage.exists("receipt.json"):
            return None
        try:
            raw = stage.read_bytes("receipt.json", max_bytes=256 * 1024)
            value = json.loads(raw.decode("utf-8"))
        except (ArtifactSpoolError, OSError, UnicodeError, ValueError) as exc:
            raise InvalidInput("compute artifact snapshot receipt is invalid") from exc
        expected = self._artifact_binding(receipt)
        if not isinstance(value, dict) or any(
            value.get(key) != wanted for key, wanted in expected.items()
        ):
            raise Conflict("compute artifact snapshot receipt belongs to a different request")
        rows = value.get("artifacts")
        if not isinstance(rows, list) or len(rows) > MAX_COMPUTE_ARTIFACTS:
            raise InvalidInput("compute artifact snapshot receipt is invalid")
        artifacts: list[RemoteArtifactReceipt] = []
        for row in rows:
            if not isinstance(row, dict):
                raise InvalidInput("compute artifact snapshot receipt is invalid")
            try:
                artifact = RemoteArtifactReceipt(
                    name=row["name"],
                    size_bytes=row["size_bytes"],
                    mime_type=row["mime_type"],
                    sha256=row["sha256"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidInput("compute artifact snapshot receipt is invalid") from exc
            snapshot_name = self._artifact_snapshot_name(
                receipt.request_sha256, artifact.name,
            )
            try:
                with stage.open_read(snapshot_name) as stream:
                    size_bytes, digest = self._copy_stable_stream(
                        stream, root=stage.path,
                    )
            except (ArtifactSpoolError, OSError) as exc:
                raise InvalidInput("compute artifact snapshot is unavailable") from exc
            if size_bytes != artifact.size_bytes or digest != artifact.sha256:
                raise InvalidInput("compute artifact snapshot does not match its receipt")
            artifacts.append(artifact)
        return tuple(artifacts)

    def _publish_artifact_manifest(
        self,
        stage: PrivateDirectoryAnchor,
        receipt: RemoteJobReceipt,
        artifacts: tuple[RemoteArtifactReceipt, ...],
    ) -> None:
        value: dict[str, Any] = self._artifact_binding(receipt)
        value["artifacts"] = [
            {
                "name": artifact.name,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
            }
            for artifact in artifacts
        ]
        try:
            stage.write_json_once("receipt.json", value)
        except FileExistsError:
            durable = self._load_artifact_manifest(stage, receipt)
            if durable != artifacts:
                raise Conflict("compute artifact snapshot receipt publication raced")

    @staticmethod
    def _opened_file_path(stream) -> Path:
        """Return the kernel-resolved path for an already-open file handle."""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            handle = msvcrt.get_osfhandle(stream.fileno())
            get_final_path = ctypes.WinDLL(
                "kernel32", use_last_error=True,
            ).GetFinalPathNameByHandleW
            get_final_path.argtypes = (
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            )
            get_final_path.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32_768)
            length = get_final_path(handle, buffer, len(buffer), 0)
            if length == 0 or length >= len(buffer):
                raise OSError(ctypes.get_last_error(), "open file path is unavailable")
            value = buffer.value
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
            return Path(value)
        proc_path = Path(f"/proc/self/fd/{stream.fileno()}")
        if proc_path.exists():
            value = os.readlink(proc_path)
            if value.endswith(" (deleted)"):
                raise OSError("open file was unlinked during snapshot")
            return Path(value)
        if hasattr(os, "uname") and os.uname().sysname == "Darwin":
            import fcntl

            value = fcntl.fcntl(stream.fileno(), 50, b"\0" * 4096)
            return Path(value.split(b"\0", 1)[0].decode("utf-8"))
        raise OSError("kernel-resolved open file paths are unsupported")

    @classmethod
    def _copy_stable_stream(cls, stream, *, root: Path, target=None) -> tuple[int, str]:
        opened_path = cls._opened_file_path(stream)
        if not opened_path.is_relative_to(root):
            raise InvalidInput("catalog artifact opened outside its configured workspace")
        digest = hashlib.sha256()
        total = 0
        before = os.fstat(stream.fileno())
        if not stat_module.S_ISREG(before.st_mode):
            raise InvalidInput("catalog artifact is not a regular file")
        if before.st_size > MAX_COMPUTE_ARTIFACT_BYTES:
            raise InvalidInput("catalog artifact exceeds the transport limit")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(block)
            if total > MAX_COMPUTE_ARTIFACT_BYTES:
                raise InvalidInput("catalog artifact exceeds the transport limit")
            digest.update(block)
            if target is not None:
                target.write(block)
        after = os.fstat(stream.fileno())
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if total != before.st_size or before_identity != after_identity:
            raise InvalidInput("catalog artifact changed while hashing")
        return total, digest.hexdigest()

    @classmethod
    def _snapshot_artifact(
        cls,
        stage: PrivateDirectoryAnchor,
        root: Path,
        candidate: Path,
        snapshot_name: str,
    ) -> tuple[int, str]:
        """Copy one contained stable handle into a private immutable snapshot."""
        descriptor, temporary_name = stage.create_temporary()
        try:
            with os.fdopen(descriptor, "wb") as target:
                target_path = cls._opened_file_path(target)
                if not target_path.is_relative_to(stage.path):
                    raise InvalidInput("compute artifact snapshot escaped its private root")
                with candidate.open("rb") as source:
                    result = cls._copy_stable_stream(source, root=root, target=target)
                target.flush()
                os.fsync(target.fileno())
            try:
                stage.publish(temporary_name, snapshot_name)
            except FileExistsError as exc:
                raise ArtifactSpoolConflict(
                    "compute artifact snapshot publication raced"
                ) from exc
            return result
        except Exception:
            try:
                if stage.exists(temporary_name):
                    stage.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _project_output(self, receipt: RemoteJobReceipt) -> RemoteJobReceipt:
        stream = getattr(self._provider, "stream", None)
        if not callable(stream):
            return receipt
        try:
            page = stream(
                receipt.remote_job_id,
                max_events=32,
                max_bytes=16 * 1024,
            )
            preview = "".join(event.data for event in page.events)
            encoded = preview.encode("utf-8")
            if len(encoded) > 16 * 1024:
                preview = encoded[: 16 * 1024].decode("utf-8", errors="ignore")
            watermark = page.next_watermark.sequence
            truncated = bool(page.truncated or page.has_more)
        except Exception:
            return receipt
        return RemoteJobReceipt(
            worker_id=receipt.worker_id,
            remote_job_id=receipt.remote_job_id,
            controller_job_id=receipt.controller_job_id,
            idempotency_key=receipt.idempotency_key,
            request_sha256=receipt.request_sha256,
            state=receipt.state,
            process_id=receipt.process_id,
            artifacts=receipt.artifacts,
            output_preview=preview,
            output_watermark=watermark,
            output_truncated=truncated,
        )

    @staticmethod
    def _input_stage_base() -> Path:
        base = Path(tempfile.gettempdir()) / "sonder-compute-inputs"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        return base

    @classmethod
    def _stage_inputs(
        cls,
        remote_job_id: str,
        root: Path,
        cwd: Path,
        argv: tuple[str, ...],
        inputs: tuple[DigestBoundInput, ...],
    ) -> tuple[Path, tuple[str, ...]]:
        """Copy verified bytes to a private snapshot and bind argv to it."""
        stage = Path(tempfile.mkdtemp(
            prefix=f"{remote_job_id}-",
            dir=cls._input_stage_base(),
        )).resolve()
        replacements: dict[str, str] = {}
        try:
            for index, expected in enumerate(inputs):
                candidate = (cwd / expected.name).resolve()
                if not candidate.is_relative_to(root):
                    raise ValueError("input artifact would escape its configured workspace")
                destination = stage / f"{index:02d}-{Path(expected.name).name}"
                digest = hashlib.sha256()
                size = 0
                try:
                    with candidate.open("rb") as source, destination.open("xb") as target:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            size += len(block)
                            if size > expected.size_bytes:
                                raise ValueError(
                                    "input artifact size does not match its digest contract"
                                )
                            digest.update(block)
                            target.write(block)
                except ValueError:
                    raise
                except OSError as exc:
                    raise ValueError("input artifact could not be staged") from exc
                if size != expected.size_bytes:
                    raise ValueError("input artifact size does not match its digest contract")
                if digest.hexdigest() != expected.sha256:
                    raise ValueError("input artifact digest does not match its contract")
                try:
                    destination.chmod(0o400)
                except OSError as exc:
                    raise ValueError("staged input could not be made read-only") from exc
                replacements[expected.name.replace("\\", "/")] = str(destination)
            rewritten: list[str] = []
            consumed: set[str] = set()
            for item in argv:
                normalized = item.replace("\\", "/")
                replacement = replacements.get(normalized)
                if replacement is None:
                    rewritten.append(item)
                else:
                    rewritten.append(replacement)
                    consumed.add(normalized)
            if consumed != set(replacements):
                raise ValueError(
                    "every digest-bound input must be an explicit catalog argument"
                )
            return stage, tuple(rewritten)
        except Exception:
            cls._remove_input_stage(stage)
            raise

    @staticmethod
    def _remove_input_stage(stage: Path) -> None:
        base = ComputeJobWorker._input_stage_base().resolve()
        resolved = stage.resolve()
        if not resolved.is_relative_to(base) or resolved == base:
            return
        def make_writable_and_retry(function, path, _error) -> None:
            os.chmod(path, 0o700)
            function(path)

        try:
            shutil.rmtree(resolved, onerror=make_writable_and_retry)
        except OSError:
            # Cleanup failure cannot rewrite execution truth. The stage remains
            # inside the dedicated temp root for later maintenance.
            return

    def _cleanup_input_stage(self, remote_job_id: str) -> None:
        stage = self._input_stages.pop(remote_job_id, None)
        if stage is not None:
            self._remove_input_stage(stage)

    def status(self, remote_job_id: str) -> RemoteJobReceipt:
        _identity(remote_job_id, "remote_job_id")
        with self._lock:
            receipt = self._by_job.get(remote_job_id)
        if receipt is None:
            raise NotFound("compute job was not found")
        wait = getattr(self._provider, "wait", None)
        if callable(wait) and receipt.state in {
            "pending",
            "running",
            "cancelling",
            "cancellation_requested",
        }:
            try:
                outcome = wait(remote_job_id, timeout=0)
                record = outcome.record
            except KeyError:
                poll = getattr(self._provider, "poll", None)
                if not callable(poll):
                    raise
                record = poll(remote_job_id)
            refreshed = RemoteJobReceipt(
                worker_id=receipt.worker_id,
                remote_job_id=receipt.remote_job_id,
                controller_job_id=receipt.controller_job_id,
                idempotency_key=receipt.idempotency_key,
                request_sha256=receipt.request_sha256,
                state=record.status.value,
                process_id=receipt.process_id,
                artifacts=receipt.artifacts,
                output_preview=receipt.output_preview,
                output_watermark=receipt.output_watermark,
                output_truncated=receipt.output_truncated,
            )
            with self._lock:
                self._by_job[remote_job_id] = refreshed
                self._by_idempotency[receipt.idempotency_key] = refreshed
            receipt = refreshed
        receipt = self._project_output(receipt)
        receipt = self._collect_artifacts(receipt)
        if receipt.state in {"succeeded", "failed", "cancelled", "interrupted"}:
            self._cleanup_input_stage(remote_job_id)
        with self._lock:
            self._by_job[remote_job_id] = receipt
            self._by_idempotency[receipt.idempotency_key] = receipt
        return receipt

    def by_idempotency(self, idempotency_key: str) -> RemoteJobReceipt | None:
        _identity(idempotency_key, "idempotency_key")
        with self._lock:
            return self._by_idempotency.get(idempotency_key)

    def read_artifact(
        self,
        remote_job_id: str,
        name: str,
        *,
        max_bytes: int = MAX_COMPUTE_ARTIFACT_BYTES,
    ) -> RemoteArtifactPayload:
        if (
            isinstance(max_bytes, bool)
            or not 1 <= max_bytes <= MAX_COMPUTE_ARTIFACT_BYTES
        ):
            raise InvalidInput("artifact read bound must be within 1..64MiB")
        receipt = self.status(remote_job_id)
        artifact = next((item for item in receipt.artifacts if item.name == name), None)
        if artifact is None:
            raise NotFound("compute artifact was not found")
        if artifact.size_bytes > max_bytes:
            raise InvalidInput("compute artifact exceeds the requested read bound")
        snapshot_name = self._artifact_snapshot_name(
            receipt.request_sha256, artifact.name,
        )
        try:
            with self._artifact_lock_for(remote_job_id):
                with self._artifact_stage(receipt) as stage:
                    with stage.open_read(snapshot_name) as stream:
                        opened_path = self._opened_file_path(stream)
                        if not opened_path.is_relative_to(stage.path):
                            raise InvalidInput(
                                "compute artifact snapshot escaped its private root"
                            )
                        before = os.fstat(stream.fileno())
                        if not stat_module.S_ISREG(before.st_mode):
                            raise InvalidInput(
                                "compute artifact snapshot is not a regular file"
                            )
                        content = stream.read(artifact.size_bytes + 1)
                        after = os.fstat(stream.fileno())
        except (ArtifactSpoolError, OSError) as exc:
            raise NotFound("compute artifact is unavailable") from exc
        before_identity = (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        )
        if (
            len(content) != artifact.size_bytes
            or before_identity != after_identity
        ):
            raise InvalidInput("compute artifact snapshot changed after publication")
        try:
            return RemoteArtifactPayload(artifact, content)
        except ValueError as exc:
            raise InvalidInput("compute artifact snapshot changed after publication") from exc

    def cancel(self, remote_job_id: str, reason: str = "cancelled") -> RemoteJobReceipt:
        receipt = self.status(remote_job_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise InvalidInput("cancellation reason must be non-empty and bounded")
        result = self._provider.cancel(remote_job_id, reason=reason)
        cleanup_completed = bool(getattr(result, "cleanup_completed", False))
        if isinstance(result, Mapping):
            cleanup_completed = bool(
                result.get("cleanup_completed", result.get("quiescent", False))
            )
        cancelled = RemoteJobReceipt(
            worker_id=receipt.worker_id,
            remote_job_id=receipt.remote_job_id,
            controller_job_id=receipt.controller_job_id,
            idempotency_key=receipt.idempotency_key,
            request_sha256=receipt.request_sha256,
            state="cancelled" if cleanup_completed else "cancellation_requested",
            process_id=receipt.process_id,
            artifacts=receipt.artifacts,
            output_preview=receipt.output_preview,
            output_watermark=receipt.output_watermark,
            output_truncated=receipt.output_truncated,
        )
        with self._lock:
            self._by_job[remote_job_id] = cancelled
            self._by_idempotency[receipt.idempotency_key] = cancelled
        cancelled = self._project_output(cancelled)
        cancelled = self._collect_artifacts(cancelled)
        if cancelled.state in {"cancelled", "interrupted"}:
            self._cleanup_input_stage(remote_job_id)
        with self._lock:
            self._by_job[remote_job_id] = cancelled
            self._by_idempotency[receipt.idempotency_key] = cancelled
        return cancelled


__all__ = [
    "ArgumentPolicy",
    "ComputeJobWorker",
    "DigestBoundInput",
    "JobCatalogEntry",
    "MAX_COMPUTE_ARTIFACTS",
    "MAX_COMPUTE_ARTIFACT_BYTES",
    "RemoteArtifactReceipt",
    "RemoteArtifactPayload",
    "RemoteJobEnvelope",
    "RemoteJobReceipt",
    "validate_remote_job_receipt",
]
