"""Opt-in cataloged test execution over the existing durable process provider.

This is host execution, not a filesystem sandbox. A trusted host must configure
exact test commands and separately authorize execution through the tool policy.
Models select a target name; they cannot supply argv, environment or roots.
"""

from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from ..application.execution.process_jobs import ProcessJobRequest
from ..application.ports.jobs import JobIdentity
from ..application.ports.tool_registry import ToolDescriptor
from ..application.ports.tool_execution import ToolExecutionResult
from ..domain.tools.descriptors import ToolEffect, ExecutionClass
from .typed_tool_executor import PackagedToolExecutor


def _minimal_environment(executable):
    """Launch-only replacement environment; no ambient credentials or controls."""
    environment = {
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "TEMP": tempfile.gettempdir(),
        "TMP": tempfile.gettempdir(),
    }
    paths = [str(Path(executable).parent)]
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", "")
        if not system_root or not Path(system_root).is_absolute():
            raise PermissionError("Windows system launch directory is unavailable")
        environment.update(SystemRoot=system_root, WINDIR=system_root)
        paths.append(str(Path(system_root) / "System32"))
    else:
        paths.extend(("/usr/bin", "/bin"))
    environment["PATH"] = os.pathsep.join(paths)
    return tuple(environment.items())


@dataclass(frozen=True)
class LaneTestTarget:
    name: str
    workspace_root: Path
    argv: tuple[str, ...]
    timeout_seconds: int = 30
    max_descendants: int = 4
    memory_limit_bytes: int = 536870912

    @property
    def argv_json(self):
        return json.dumps(self.argv, separators=(",", ":"))

    @property
    def argv_digest(self):
        return hashlib.sha256(self.argv_json.encode("utf-8")).hexdigest()


class LaneTestCatalog:
    def __init__(self, path, digest, targets):
        self.path, self.digest = path, digest
        self.targets = MappingProxyType(dict(targets))

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("lane test catalog path must be absolute")
        path = path.resolve()
        cls._require_protected_paths((path,))
        with path.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("lane test catalog exceeds 64 KiB")
        body = json.loads(raw)
        if (
            not isinstance(body, dict)
            or set(body) != {"targets"}
            or not isinstance(body["targets"], list)
            or len(body["targets"]) > 16
        ):
            raise ValueError("lane test catalog must contain at most 16 targets")
        targets = {}
        for entry in body["targets"]:
            if not isinstance(entry, dict) or set(entry) - {
                "name",
                "workspace_root",
                "argv",
                "timeout_seconds",
                "max_descendants",
                "memory_limit_bytes",
            }:
                raise ValueError("unknown test target configuration")
            name = entry.get("name", "")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 64
                or not all(c.isalnum() or c in "_-" for c in name)
                or name in targets
            ):
                raise ValueError("invalid or duplicate test target name")
            root = Path(entry["workspace_root"])
            if not root.is_absolute():
                raise ValueError("test workspace must be absolute")
            root = root.resolve()
            if not root.is_dir() or path == root or root in path.parents:
                raise ValueError(
                    "test catalog must be outside its delegated workspaces"
                )
            argv = entry["argv"]
            if (
                not isinstance(argv, list)
                or not 1 <= len(argv) <= 32
                or any(
                    not isinstance(a, str) or not a or len(a) > 2048 or "\x00" in a
                    for a in argv
                )
            ):
                raise ValueError("test target argv must be bounded nonempty strings")
            executable = Path(argv[0])
            if len(json.dumps(argv, separators=(",", ":")).encode("utf-8")) > 4096:
                raise ValueError("test argv snapshot exceeds 4 KiB")
            if not executable.is_absolute() or not executable.is_file():
                raise ValueError(
                    "test target executable must be an existing absolute path"
                )
            if root == executable.resolve() or root in executable.resolve().parents:
                raise ValueError(
                    "test interpreter must be outside the model-writable workspace"
                )
            cls._require_protected_paths((executable.resolve(),))
            values = {}
            for key, default, maximum in [
                ("timeout_seconds", 30, 600),
                ("max_descendants", 4, 16),
                ("memory_limit_bytes", 536870912, 4294967296),
            ]:
                value = entry.get(key, default)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 1 <= value <= maximum
                ):
                    raise ValueError("test target resource bound is invalid")
                values[key] = value
            targets[name] = LaneTestTarget(name, root, tuple(argv), **values)
        return cls(path, hashlib.sha256(raw).hexdigest(), targets)

    @staticmethod
    def _require_protected_paths(paths):
        from .filesystem.file_ops import allowed_roots

        roots = [Path(root).resolve() for root in allowed_roots()]
        for path in paths:
            if any(path == root or root in path.parents for root in roots):
                raise ValueError(
                    "test configuration and executable must be outside every model-writable root"
                )

    def require_current(self):
        self._require_protected_paths(
            (
                self.path,
                *(Path(target.argv[0]).resolve() for target in self.targets.values()),
            )
        )
        with self.path.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != self.digest:
            raise PermissionError("test catalog changed; host recomposition required")


def lane_test_descriptor(catalog):
    return ToolDescriptor(
        "run_tests",
        "Execute one explicitly configured test target using its fixed command and resource limits.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": sorted(catalog.targets)}
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        effects=frozenset(
            {ToolEffect.READ_FILES, ToolEffect.WRITE_FILES, ToolEffect.EXECUTE}
        ),
        execution_class=ExecutionClass.HOST,
    )


class LaneTestExecutor:
    def __init__(self, catalog, provider, *, files=None):
        self.catalog, self.provider = catalog, provider
        self.files = files or PackagedToolExecutor()

    def execute(self, descriptor, call, context, execution_class):
        if descriptor.name != "run_tests":
            return self.files.execute(descriptor, call, context, execution_class)
        if set(call.arguments) != {"target"}:
            raise PermissionError("test tool accepts only a configured target name")
        self.catalog.require_current()
        target = self.catalog.targets.get(call.arguments["target"])
        if target is None:
            raise PermissionError("test target is not configured")
        if context.workspace_roots != (target.workspace_root,):
            raise PermissionError(
                "test target workspace must match the exclusive lane grant"
            )
        if context.expired or context.cancellation.cancelled:
            raise PermissionError("test execution authority expired or cancelled")
        remaining = context.remaining_seconds
        deadline = (
            min(target.timeout_seconds, max(1, math.ceil(remaining)))
            if remaining is not None
            else target.timeout_seconds
        )
        job_id = "lane-test-" + call.call_id
        request = ProcessJobRequest(
            JobIdentity(
                job_id,
                "agent_lane.test",
                call.call_id,
                call.call_id,
                parent_session_id=getattr(context, "session_id", None),
            ),
            target.argv,
            cwd=target.workspace_root,
            environment=_minimal_environment(target.argv[0]),
            inherit_environment=False,
            require_job_scope=True,
            max_descendants=target.max_descendants,
            deadline_seconds=deadline,
            memory_limit_bytes=target.memory_limit_bytes,
            metadata=(
                ("principal_id", context.principal_id),
                ("target", target.name),
                ("catalog_digest", self.catalog.digest),
                ("argv_digest", target.argv_digest),
                ("argv_snapshot", target.argv_json),
                ("workspace_root", str(target.workspace_root)),
            ),
        )
        self.provider.start(request)
        cancelled = False
        cleanup_completed = False
        exit_code = None
        try:
            while True:
                try:
                    self.catalog.require_current()
                    allowed = not context.expired and not context.cancellation.cancelled
                except (OSError, PermissionError, ValueError):
                    allowed = False
                if not allowed:
                    cancelled = True
                    cleanup = self.provider.cancel(
                        job_id, reason="lane test authority ended"
                    )
                    cleanup_completed = cleanup.cleanup_completed
                    record = self.provider.poll(job_id)
                    break
                try:
                    waited = self.provider.wait(job_id, timeout=0.2)
                except KeyError:
                    # The provider's deadline controller can finish cleanup and
                    # release its process handle between our admission and wait.
                    # Only its durable cancelled terminal state proves this case.
                    record = self.provider.poll(job_id)
                    if not record.is_terminal or record.status.value != "cancelled":
                        raise
                    cleanup_completed = True
                    cancelled = True
                    break
                if not waited.timed_out:
                    record = waited.record
                    exit_code = waited.exit_code
                    cleanup_completed = record.is_terminal
                    cancelled = record.status.value == "cancelled"
                    break
        except BaseException:
            # Keep provider-owned cancellation/cleanup evidence on every exit.
            self.provider.cancel(job_id, reason="lane test controller stopped")
            raise
        if not cleanup_completed:
            raise RuntimeError("test process cleanup remains unresolved")
        page = self.provider.stream(job_id, max_events=64, max_bytes=16384)
        output = "".join(event.data for event in page.events)[:16384]
        body = dict(
            job_id=job_id,
            target=target.name,
            exit_code=exit_code,
            status=record.status.value,
            cancelled=cancelled,
            cleanup_completed=cleanup_completed,
            output=output,
            output_truncated=bool(page.has_more or page.truncated),
        )
        success = exit_code == 0 and not cancelled and cleanup_completed
        return ToolExecutionResult(
            "run_tests",
            success,
            json.dumps(body),
            error_code=(
                "" if success else "TEST_CANCELLED" if cancelled else "TEST_FAILED"
            ),
            metadata={
                "evidence": {
                    "job_id": job_id,
                    "catalog_digest": self.catalog.digest,
                    "argv_digest": target.argv_digest,
                    "workspace_root": str(target.workspace_root),
                    "cleanup_completed": cleanup_completed,
                }
            },
        )
