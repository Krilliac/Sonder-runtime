"""Catalog-bound remote compute job contracts and worker orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from threading import RLock
from typing import Any, Mapping

from ..execution.process_jobs import ProcessJobProvider, ProcessJobRequest
from ..ports.jobs import JobIdentity
from ...domain.common.errors import Conflict, DependencyUnavailable, InvalidInput, NotFound
from ...domain.compute_fabric import WorkloadKind


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENVIRONMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_OPTION = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BOUNDED_OPTION_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REMOTE_JOB_STATES = frozenset({
    "pending", "claimed", "running", "paused", "cancelling",
    "cancellation_requested", "succeeded", "failed", "cancelled", "interrupted",
})


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
        if len(self.artifact_paths) > 256:
            raise ValueError("catalog artifact path count exceeds 256")
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
        if len(self.artifacts) > 256:
            raise ValueError("receipt artifact count exceeds its bound")


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
        self._lock = RLock()
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
            self._verify_inputs(root, cwd, envelope.input_artifacts)
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
            deadline_seconds=envelope.deadline_seconds,
            memory_limit_bytes=entry.memory_limit_bytes,
            metadata=(
                ("compute_worker_id", self.worker_id),
                ("compute_controller_job_id", envelope.controller_job_id),
                ("compute_request_sha256", envelope.request_sha256),
                ("compute_catalog_entry_id", envelope.catalog_entry_id),
                ("compute_workspace_mapping", envelope.workspace_mapping),
                ("compute_relative_cwd", envelope.relative_cwd),
            ),
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
            self._artifact_context[remote_job_id] = (
                root, cwd, entry.artifact_paths,
            )
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
        artifacts: list[RemoteArtifactReceipt] = []
        for relative_path in artifact_paths:
            candidate = (cwd / relative_path).resolve()
            if not candidate.is_relative_to(root):
                raise InvalidInput("catalog artifact would escape its configured workspace")
            try:
                stat = candidate.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise InvalidInput("catalog artifact could not be inspected") from exc
            if not candidate.is_file() or stat.st_size > 1 << 40:
                raise InvalidInput("catalog artifact is not a bounded regular file")
            digest = hashlib.sha256()
            try:
                with candidate.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError as exc:
                raise InvalidInput("catalog artifact could not be hashed") from exc
            mime_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            artifacts.append(RemoteArtifactReceipt(
                name=relative_path,
                size_bytes=stat.st_size,
                mime_type=mime_type,
                sha256=digest.hexdigest(),
            ))
        if not artifacts:
            return receipt
        return RemoteJobReceipt(
            worker_id=receipt.worker_id,
            remote_job_id=receipt.remote_job_id,
            controller_job_id=receipt.controller_job_id,
            idempotency_key=receipt.idempotency_key,
            request_sha256=receipt.request_sha256,
            state=receipt.state,
            process_id=receipt.process_id,
            artifacts=tuple(artifacts),
        )

    @staticmethod
    def _verify_inputs(
        root: Path,
        cwd: Path,
        inputs: tuple[DigestBoundInput, ...],
    ) -> None:
        for expected in inputs:
            candidate = (cwd / expected.name).resolve()
            if not candidate.is_relative_to(root):
                raise ValueError("input artifact would escape its configured workspace")
            try:
                stat = candidate.stat()
            except OSError as exc:
                raise ValueError("input artifact is unavailable") from exc
            if not candidate.is_file() or stat.st_size != expected.size_bytes:
                raise ValueError("input artifact size does not match its digest contract")
            digest = hashlib.sha256()
            try:
                with candidate.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError as exc:
                raise ValueError("input artifact could not be verified") from exc
            if digest.hexdigest() != expected.sha256:
                raise ValueError("input artifact digest does not match its contract")

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
            )
            with self._lock:
                self._by_job[remote_job_id] = refreshed
                self._by_idempotency[receipt.idempotency_key] = refreshed
            receipt = refreshed
        receipt = self._collect_artifacts(receipt)
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
        max_bytes: int = 64 * 1024 * 1024,
    ) -> RemoteArtifactPayload:
        if isinstance(max_bytes, bool) or not 1 <= max_bytes <= 256 * 1024 * 1024:
            raise InvalidInput("artifact read bound must be within 1..256MiB")
        receipt = self.status(remote_job_id)
        artifact = next((item for item in receipt.artifacts if item.name == name), None)
        if artifact is None:
            raise NotFound("compute artifact was not found")
        if artifact.size_bytes > max_bytes:
            raise InvalidInput("compute artifact exceeds the requested read bound")
        context = self._artifact_context.get(remote_job_id)
        if context is None:
            raise NotFound("compute artifact workspace is unavailable")
        root, cwd, _artifact_paths = context
        candidate = (cwd / artifact.name).resolve()
        if not candidate.is_relative_to(root):
            raise InvalidInput("compute artifact would escape its configured workspace")
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            raise NotFound("compute artifact is unavailable") from exc
        try:
            return RemoteArtifactPayload(artifact, content)
        except ValueError as exc:
            raise InvalidInput("compute artifact changed after receipt publication") from exc

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
        )
        with self._lock:
            self._by_job[remote_job_id] = cancelled
            self._by_idempotency[receipt.idempotency_key] = cancelled
        cancelled = self._collect_artifacts(cancelled)
        with self._lock:
            self._by_job[remote_job_id] = cancelled
            self._by_idempotency[receipt.idempotency_key] = cancelled
        return cancelled


__all__ = [
    "ArgumentPolicy",
    "ComputeJobWorker",
    "DigestBoundInput",
    "JobCatalogEntry",
    "RemoteArtifactReceipt",
    "RemoteArtifactPayload",
    "RemoteJobEnvelope",
    "RemoteJobReceipt",
    "validate_remote_job_receipt",
]
