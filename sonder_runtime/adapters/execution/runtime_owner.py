"""Fixed disposable runtime launch through the existing process ledger."""

import os
from pathlib import Path
import subprocess
import sys
import time

from .process_jobs import SubprocessJobProvider
from ..persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from ..process_termination import ProcessTreeSupervisor
from ..process_liveness import probe_process, PROCESS_ALIVE
from ...application.execution.process_jobs import ProcessJobRequest, ProcessJobWait
from ...application.ports.jobs import JobIdentity
from ...application.ports.runtime_owner import OwnerUnsupported, OwnerRefused


class WindowsOwnedRuntimeProcess:
    child_module = "sonder_runtime.bootstrap.owned_http_runtime"

    def __init__(self, root):
        if os.name != "nt":
            raise OwnerUnsupported(
                "disposable runtime owner requires Windows Job Objects"
            )
        self.root = Path(root)
        self._readers = ()
        self._cancelled = None
        self._waited = None
        self.registry = SQLiteDurableJobRegistry(self.root / "processes.sqlite")
        self.provider = SubprocessJobProvider(
            self.registry,
            process_cleanup=ProcessTreeSupervisor(),
            launcher=self._launch_hidden,
            max_concurrent_processes=1,
        )

    @staticmethod
    def _launch_hidden(argv, **options):
        options["creationflags"] = (
            options.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        )
        return subprocess.Popen(argv, **options)

    def launch(self, namespace, command):
        self._cancelled = self._waited = None
        source, arguments, python_path, search_path, metadata = self._launch_layout(command)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH"}
        }
        environment.update(
            {
                "PYTHONPATH": python_path,
                "PATH": search_path,
                "PYTHONUNBUFFERED": "1",
                "SONDER_HOME": str(self.root / "state"),
                "SONDER_FILE_ROOTS": str(
                    self.root.parent / (self.root.name + "-workspace")
                ),
                "TEMP": str(self.root / "temp"),
                "TMP": str(self.root / "temp"),
                "USERPROFILE": str(self.root / "profile"),
                "HOME": str(self.root / "profile"),
                "APPDATA": str(self.root / "profile" / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(self.root / "profile" / "AppData" / "Local"),
            }
        )
        for name in (
            "SONDER_ALLOW_CLOUD",
            "SONDER_WEB_TOOLS",
            "SONDER_LIVE_RELOAD",
            "SONDER_SOURCE_MODIFICATION",
            "SONDER_HOST_CONTROL",
            "SONDER_TRAINING",
            "SONDER_NPU",
            "SONDER_EXPOSE_REASONING",
            "SONDER_ALLOW_PRIVATE_COT",
            "SONDER_LOCATION_CONSENT",
        ):
            environment[name] = "0"
        request = ProcessJobRequest(
            JobIdentity(
                command.operation_id,
                "owned-http-runtime",
                command.operation_id,
                command.digest,
            ),
            (
                *arguments,
                str(self.root),
                namespace,
                command.operation_id,
            ),
            cwd=source,
            environment=tuple(environment.items()),
            inherit_environment=False,
            max_descendants=8,
            require_job_scope=True,
            metadata=(("owner_namespace", namespace), *metadata),
        )
        try:
            return self.provider.start(request)
        finally:
            self._readers = self.provider.snapshot_output_readers(command.operation_id)

    def _launch_layout(self, command):
        source = Path(__file__).resolve().parents[3]
        return (source, (sys._base_executable, "-m", self.child_module),
            os.pathsep.join((str(source), str(Path(sys.prefix) / "Lib" / "site-packages"))),
            os.environ.get("PATH", ""), ())

    def wait(self, job_id, timeout):
        if self._cancelled is not None and self._cancelled[0] == job_id:
            self._join_readers()
            return ProcessJobWait(self._cancelled[1].records[-1], None)
        if self._waited is not None and self._waited[0] == job_id:
            self._join_readers()
            return self._waited[1]
        result = self.provider.wait(job_id, timeout=timeout)
        if not result.timed_out:
            self._waited = (job_id, result)
            self._join_readers()
        return result

    def alive(self, job_id):
        view = self.registry.view(job_id)
        expected = dict(view.metadata).get("process_instance_identity")
        state, observed = probe_process(view.process_id, expected)
        return state == PROCESS_ALIVE and observed == expected

    def force_stop(self, job_id):
        if self._cancelled is not None and self._cancelled[0] == job_id:
            self._join_readers()
            return self._cancelled[1]
        result = self.provider.cancel(job_id, "owned runtime bounded shutdown")
        if result.cleanup_completed:
            self._cancelled = (job_id, result)
            self._join_readers()
        return result

    def _join_readers(self):
        deadline = time.monotonic() + 3
        for reader in self._readers:
            reader.join(max(0, deadline - time.monotonic()))
        if any(reader.is_alive() for reader in self._readers):
            raise OwnerRefused("owned process output handles remain live")


class WindowsManagedRuntimeProcess(WindowsOwnedRuntimeProcess):
    child_module = "sonder_runtime.bootstrap.managed_http_runtime"

    def bind_payload(self, payload, writable_roots):
        from .runtime_payload import RuntimePayload
        if type(payload) is not RuntimePayload or hasattr(self, "_payload"):
            raise OwnerRefused("exact single runtime payload binding required")
        self._payload, self._payload_roots = payload, writable_roots

    def _launch_layout(self, command):
        import json
        if not hasattr(self, "_payload") or json.loads(command.payload) != {"artifact_digest": self._payload.digest}:
            raise OwnerRefused("prepared launch artifact binding is missing")
        self._payload.validate(self._payload_roots())
        value = self._payload.manifest
        code = "import sys,json; sys.path[:]=json.loads(sys.argv.pop(1)); import runpy; runpy.run_module('sonder_runtime.bootstrap.managed_http_runtime',run_name='__main__')"
        arguments = (value["executable"], "-E", "-S", "-B", "-X", "pycache_prefix=" + str(self.root / "python-cache"),
            "-c", code, json.dumps(value["paths"]))
        return (Path(value["payload"]), arguments, str(value["payload"]),
            os.pathsep.join(value["dll_paths"]), (("runtime_artifact_digest", self._payload.digest),))
