from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock

import pytest

import sonder_runtime.application.compute_fabric.jobs as jobs_module
import sonder_runtime.application.compute_fabric.artifact_spool as artifact_spool_module
from sonder_runtime.application.compute_fabric.jobs import (
    ArgumentPolicy,
    ComputeJobWorker,
    DigestBoundInput,
    JobCatalogEntry,
    MAX_COMPUTE_ARTIFACT_BYTES,
    RemoteJobEnvelope,
)
from sonder_runtime.application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from sonder_runtime.application.execution.process_jobs import ProcessJobStart, ProcessJobWait
from sonder_runtime.application.execution.world_control import (
    OutputEvent,
    OutputPage,
    OutputStream,
    OutputWatermark,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.domain.common.errors import Conflict, InvalidInput
from sonder_runtime.domain.compute_fabric import WorkloadKind
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor


@pytest.fixture(autouse=True)
def _isolate_compute_artifact_snapshots(tmp_path: Path, monkeypatch) -> None:
    snapshot_root = tmp_path / ".compute-artifact-snapshots"
    monkeypatch.setattr(
        ComputeJobWorker,
        "_artifact_stage_base",
        staticmethod(lambda: snapshot_root),
    )


def _process_handle_count() -> int:
    if os.name == "nt":
        import ctypes

        count = ctypes.c_ulong()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        get_handle_count = kernel32.GetProcessHandleCount
        get_handle_count.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_handle_count.restype = ctypes.c_int
        if not get_handle_count(get_current_process(), ctypes.byref(count)):
            raise OSError(ctypes.get_last_error(), "handle count unavailable")
        return int(count.value)
    return len(tuple(Path("/proc/self/fd").iterdir()))


class CapturingProvider:
    def __init__(self) -> None:
        self.request = None
        self.cancelled = None

    def start(self, request):
        self.request = request
        return ProcessJobStart(
            JobRecord(request.identity, status=JobStatus.RUNNING),
            process_id=42,
            process_group_id=42,
        )

    def cancel(self, job_id, reason="cancelled"):
        self.cancelled = (job_id, reason)
        return {"quiescent": True}


def _entry() -> JobCatalogEntry:
    return JobCatalogEntry(
        entry_id="pytest",
        workload=WorkloadKind.TEST,
        program=sys.executable,
        fixed_args=("-m", "pytest"),
        argument_policy=ArgumentPolicy.RELATIVE_PATHS_AND_TEST_SELECTORS,
        environment_allowlist=frozenset({"PYTEST_ADDOPTS"}),
        workspace_mappings=frozenset({"sonder"}),
        memory_limit_bytes=512 * 1024 * 1024,
    )


def _envelope(**changes) -> RemoteJobEnvelope:
    values = dict(
        controller_job_id="controller-job",
        idempotency_key="idem-1",
        workload=WorkloadKind.TEST,
        catalog_entry_id="pytest",
        workspace_mapping="sonder",
        relative_cwd="tests",
        arguments=("test_api.py",),
        environment=(("PYTEST_ADDOPTS", "-q"),),
        deadline_seconds=60,
        idempotent=True,
    )
    values.update(changes)
    return RemoteJobEnvelope.create(**values)


def test_worker_resolves_catalog_program_and_workspace(tmp_path: Path) -> None:
    provider = CapturingProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    receipt = worker.submit(_envelope())
    assert provider.request.argv == (sys.executable, "-m", "pytest", "test_api.py")
    assert provider.request.cwd == (tmp_path / "tests").resolve()
    assert provider.request.environment == (("PYTEST_ADDOPTS", "-q"),)
    assert provider.request.deadline_seconds == 60
    assert provider.request.memory_limit_bytes == 512 * 1024 * 1024
    assert receipt.worker_id == "worker-1"
    assert receipt.request_sha256 == _envelope().request_sha256
    assert receipt.state == "running"


def test_worker_rejects_unknown_catalog_workspace_and_environment(tmp_path: Path) -> None:
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    with pytest.raises(InvalidInput, match="catalog"):
        worker.submit(_envelope(catalog_entry_id="unknown"))
    with pytest.raises(InvalidInput, match="workspace"):
        worker.submit(_envelope(workspace_mapping="other"))
    with pytest.raises(InvalidInput, match="environment"):
        worker.submit(_envelope(environment=(("SECRET", "x"),)))


def test_worker_rejects_unconfigured_options_and_controller_paths(tmp_path: Path) -> None:
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    with pytest.raises(InvalidInput, match="option"):
        worker.submit(_envelope(arguments=("--basetemp=C:/Windows/Temp",)))
    with pytest.raises(ValueError, match="workspace"):
        _envelope(arguments=("C:/Windows/Temp",))


def test_worker_accepts_only_explicit_typed_options(tmp_path: Path) -> None:
    entry = replace(
        _entry(),
        allowed_bounded_options=frozenset({"--color"}),
        allowed_relative_path_options=frozenset({"--basetemp"}),
    )
    provider = CapturingProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": entry},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    worker.submit(_envelope(arguments=("--color=yes", "--basetemp=.tmp", "test_api.py")))
    assert provider.request.argv[-3:] == (
        "--color=yes", "--basetemp=.tmp", "test_api.py",
    )


def test_worker_rejects_argument_symlink_that_escapes_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    with pytest.raises(InvalidInput, match="escape"):
        worker.submit(_envelope(relative_cwd=".", arguments=("escape/test_api.py",)))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_worker_rejects_argument_junction_escape_without_symlink_privilege(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_api.py").write_text("pass", encoding="utf-8")
    junction = workspace / "escape"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    try:
        worker = ComputeJobWorker(
            worker_id="worker-1",
            catalog={"pytest": _entry()},
            workspace_mappings={"sonder": workspace},
            provider=CapturingProvider(),
        )
        with pytest.raises(InvalidInput, match="escape"):
            worker.submit(_envelope(
                relative_cwd=".",
                arguments=("escape/test_api.py",),
            ))
    finally:
        os.rmdir(junction)


def test_worker_idempotency_returns_same_job_and_conflict_rejects(tmp_path: Path) -> None:
    provider = CapturingProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    first = worker.submit(_envelope())
    second = worker.submit(_envelope())
    assert second.remote_job_id == first.remote_job_id
    with pytest.raises(Conflict):
        worker.submit(_envelope(arguments=("different.py",)))


def test_worker_revalidates_digest_even_if_constructed_unsafely(tmp_path: Path) -> None:
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    envelope = _envelope()
    object.__setattr__(envelope, "request_sha256", "0" * 64)
    with pytest.raises(InvalidInput, match="digest"):
        worker.submit(envelope)


def test_worker_verifies_digest_bound_inputs_before_launch(tmp_path: Path) -> None:
    import hashlib

    tests = tmp_path / "tests"
    tests.mkdir()
    payload = tests / "input.bin"
    payload.write_bytes(b"abc")
    provider = CapturingProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    digest = hashlib.sha256(b"abc").hexdigest()
    worker.submit(_envelope(
        arguments=("input.bin",),
        input_artifacts=(DigestBoundInput("input.bin", 3, digest),),
    ))
    assert provider.request is not None
    staged = Path(provider.request.argv[-1])
    assert staged.is_absolute()
    assert staged.read_bytes() == b"abc"
    payload.write_bytes(b"mutated")
    assert staged.read_bytes() == b"abc"

    with pytest.raises(InvalidInput, match="digest"):
        worker.submit(_envelope(
            idempotency_key="idem-bad-input",
            arguments=("input.bin",),
            input_artifacts=(DigestBoundInput("input.bin", 3, "0" * 64),),
        ))


def test_worker_rejects_digest_input_not_consumed_by_catalog_argv(tmp_path: Path) -> None:
    import hashlib

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "input.bin").write_bytes(b"abc")
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    with pytest.raises(InvalidInput, match="explicit catalog argument"):
        worker.submit(_envelope(
            input_artifacts=(DigestBoundInput(
                "input.bin", 3, hashlib.sha256(b"abc").hexdigest()
            ),),
        ))


def test_concurrent_identical_submissions_launch_exactly_once(tmp_path: Path) -> None:
    import time

    class SlowProvider(CapturingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0

        def start(self, request):
            self.starts += 1
            time.sleep(0.05)
            return super().start(request)

    (tmp_path / "tests").mkdir()
    provider = SlowProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    envelope = _envelope()
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(lambda _index: worker.submit(envelope), range(2)))

    assert provider.starts == 1
    assert receipts[0] == receipts[1]


def test_failed_launch_is_immediately_discoverable_by_idempotency(tmp_path: Path) -> None:
    class Cleanup:
        def cleanup(self, _request):
            raise AssertionError("no process was launched")

    def fail_launch(_argv, **_kwargs):
        raise RuntimeError("launcher unavailable")

    (tmp_path / "tests").mkdir()
    provider = SubprocessJobProvider(
        SQLiteDurableJobRegistry(tmp_path / "launch-failure.db"),
        process_cleanup=Cleanup(),
        launcher=fail_launch,
        platform_name=os.name,
    )
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    envelope = _envelope()

    with pytest.raises(RuntimeError, match="launcher unavailable"):
        worker.submit(envelope)

    failed = worker.by_idempotency(envelope.idempotency_key)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.request_sha256 == envelope.request_sha256
    assert worker.status(failed.remote_job_id) == failed
    assert worker.submit(envelope) == failed


def test_incomplete_launch_cleanup_is_discoverable_by_idempotency(tmp_path: Path) -> None:
    class IncompleteLaunchProvider:
        def start(self, _request):
            raise RuntimeError("launch cleanup incomplete")

        def poll(self, job_id):
            return JobRecord(
                JobIdentity(job_id, "compute-test", "controller-job", "idem-1"),
                status=JobStatus.CANCELLATION_REQUESTED,
                error="scope still populated",
            )

    (tmp_path / "tests").mkdir()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=IncompleteLaunchProvider(),
    )
    envelope = _envelope()

    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        worker.submit(envelope)

    receipt = worker.by_idempotency(envelope.idempotency_key)
    assert receipt is not None
    assert receipt.state == "cancellation_requested"
    assert worker.status(receipt.remote_job_id) == receipt
    assert worker.submit(envelope) == receipt


def test_worker_status_refreshes_terminal_state_and_cancel_reports_cleanup_truth(
    tmp_path: Path,
) -> None:
    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            assert timeout == 0
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(
                        job_id,
                        kind="compute-test",
                        operation_id="controller-job",
                        idempotency_key="idem-1",
                    ),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    provider = CompletedProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    started = worker.submit(_envelope())
    assert worker.status(started.remote_job_id).state == "succeeded"
    assert worker.cancel(started.remote_job_id, reason="done").state == "cancelled"


def test_worker_status_projects_bounded_output_preview_and_watermark(
    tmp_path: Path,
) -> None:
    class OutputProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(
                        job_id,
                        kind="compute-test",
                        operation_id="controller-job",
                        idempotency_key="idem-1",
                    ),
                    status=JobStatus.FAILED,
                ),
                exit_code=1,
            )

        def stream(self, job_id, *, max_events, max_bytes):
            assert max_events == 32
            assert max_bytes == 16 * 1024
            return OutputPage(
                (
                    OutputEvent(
                        OutputWatermark(7),
                        OutputStream.STDERR,
                        "compile failed\n",
                    ),
                ),
                OutputWatermark(7),
                has_more=True,
            )

    (tmp_path / "tests").mkdir()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=OutputProvider(),
    )
    receipt = worker.status(worker.submit(_envelope()).remote_job_id)
    assert receipt.output_preview == "compile failed\n"
    assert receipt.output_watermark == 7
    assert receipt.output_truncated is True


def test_worker_emits_verified_receipts_for_catalog_artifacts(tmp_path: Path) -> None:
    import hashlib

    tests = tmp_path / "tests"
    tests.mkdir()
    report = tests / "report.json"
    report.write_bytes(b'{"ok":true}')

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(
                        job_id,
                        kind="compute-test",
                        operation_id="controller-job",
                        idempotency_key="idem-1",
                    ),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": replace(_entry(), artifact_paths=("report.json",))},
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    started = worker.submit(_envelope())
    completed = worker.status(started.remote_job_id)
    assert completed.artifacts[0].name == "report.json"
    assert completed.artifacts[0].size_bytes == len(b'{"ok":true}')
    assert completed.artifacts[0].sha256 == hashlib.sha256(b'{"ok":true}').hexdigest()
    payload = worker.read_artifact(started.remote_job_id, "report.json")
    assert payload.content == b'{"ok":true}'

    report.write_bytes(b'{"ok":false}')
    assert worker.read_artifact(
        started.remote_job_id, "report.json"
    ).content == b'{"ok":true}'

    report.write_bytes(b"x" * (1024 * 1024))
    assert worker.read_artifact(
        started.remote_job_id, "report.json", max_bytes=1024
    ).content == b'{"ok":true}'


def test_worker_rejects_preexisting_linked_artifact_spool(tmp_path: Path) -> None:
    snapshot_root = tmp_path / ".compute-artifact-snapshots"
    outside = tmp_path / "outside-snapshots"
    outside.mkdir()
    linked = False
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(snapshot_root), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                pytest.skip(f"directory junction unavailable: {completed.stderr}")
        else:
            snapshot_root.symlink_to(outside, target_is_directory=True)
        linked = True

        with pytest.raises(InvalidInput, match="symlink|junction|reparse|private"):
            ComputeJobWorker(
                worker_id="worker-1",
                catalog={"pytest": _entry()},
                workspace_mappings={"sonder": tmp_path},
                provider=CapturingProvider(),
            )
    finally:
        if linked:
            if os.name == "nt":
                os.rmdir(snapshot_root)
            else:
                snapshot_root.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
def test_worker_rejects_permissive_preexisting_artifact_spool(tmp_path: Path) -> None:
    snapshot_root = tmp_path / ".compute-artifact-snapshots"
    snapshot_root.mkdir()
    completed = subprocess.run(
        [
            "icacls.exe",
            str(snapshot_root),
            "/inheritance:r",
            "/grant:r",
            "*S-1-1-0:(OI)(CI)F",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"could not construct permissive ACL fixture: {completed.stderr}")

    with pytest.raises(InvalidInput, match="private|permission|owner"):
        ComputeJobWorker(
            worker_id="worker-1",
            catalog={"pytest": _entry()},
            workspace_mappings={"sonder": tmp_path},
            provider=CapturingProvider(),
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
@pytest.mark.parametrize(
    "grants",
    [
        ("owner-inheritable",),
        ("owner-direct", "system-direct"),
    ],
)
def test_worker_rejects_artifact_spool_without_full_inheritable_system_acl(
    tmp_path: Path,
    grants: tuple[str, ...],
) -> None:
    snapshot_root = tmp_path / ".compute-artifact-snapshots"
    snapshot_root.mkdir()
    owner_sid = artifact_spool_module._windows_current_user_sid_string()
    access = {
        "owner-inheritable": f"*{owner_sid}:(OI)(CI)F",
        "owner-direct": f"*{owner_sid}:F",
        "system-direct": "*S-1-5-18:F",
    }
    completed = subprocess.run(
        [
            "icacls.exe",
            str(snapshot_root),
            "/inheritance:r",
            "/grant:r",
            *(access[name] for name in grants),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"could not construct private ACL fixture: {completed.stderr}")

    with pytest.raises(InvalidInput, match="private|permission|owner|ACL"):
        ComputeJobWorker(
            worker_id="worker-1",
            catalog={"pytest": _entry()},
            workspace_mappings={"sonder": tmp_path},
            provider=CapturingProvider(),
        )


def test_artifact_open_identity_failure_does_not_leak_descriptor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    anchor = PrivateDirectoryAnchor.open_base(tmp_path / "snapshots")
    try:
        anchor.write_json_once("receipt.json", {"ok": True})
        monkeypatch.setattr(
            artifact_spool_module,
            "_opened_fd_path",
            lambda _fd: (_ for _ in ()).throw(OSError("identity unavailable")),
        )
        before = _process_handle_count()
        for _ in range(20):
            with pytest.raises(OSError, match="identity unavailable"):
                anchor.open_read("receipt.json")
        after = _process_handle_count()
    finally:
        anchor.close()

    assert after - before <= 2


@pytest.mark.skipif(os.name != "nt", reason="Windows temporary handle regression")
def test_artifact_temporary_identity_failure_does_not_leak_descriptor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    anchor = PrivateDirectoryAnchor.open_base(tmp_path / "snapshots")
    try:
        monkeypatch.setattr(
            artifact_spool_module,
            "_opened_fd_path",
            lambda _fd: (_ for _ in ()).throw(OSError("identity unavailable")),
        )
        before = _process_handle_count()
        for _ in range(20):
            with pytest.raises(OSError, match="identity unavailable"):
                anchor.create_temporary()
        after = _process_handle_count()
    finally:
        anchor.close()

    assert after - before <= 2


def test_concurrent_terminal_status_publishes_one_artifact_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import hashlib

    tests = tmp_path / "tests"
    tests.mkdir()
    report = tests / "report.json"
    report.write_bytes(b'{"source":"original"}')
    source_a = tests / "source-a.json"
    source_b = tests / "source-b.json"
    source_a.write_bytes(b'{"source":"a"}')
    source_b.write_bytes(b'{"source":"b"}')

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(job_id, "compute-test", "controller-job", "idem-1"),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": replace(_entry(), artifact_paths=("report.json",))},
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    started = worker.submit(_envelope())
    original_open = Path.open
    first_opened = Event()
    second_opened = Event()
    release_first = Event()
    counter_lock = Lock()
    calls = 0

    def competing_open(path, *args, **kwargs):
        nonlocal calls
        if path != report:
            return original_open(path, *args, **kwargs)
        with counter_lock:
            calls += 1
            call = calls
        if call == 1:
            first_opened.set()
            release_first.wait(timeout=5)
            return original_open(source_a, *args, **kwargs)
        second_opened.set()
        return original_open(source_b, *args, **kwargs)

    monkeypatch.setattr(Path, "open", competing_open)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker.status, started.remote_job_id)
            assert first_opened.wait(timeout=5)
            second = pool.submit(worker.status, started.remote_job_id)
            second_opened.wait(timeout=0.5)
            release_first.set()
            receipts = (first.result(timeout=5), second.result(timeout=5))
    finally:
        release_first.set()

    assert receipts[0].artifacts == receipts[1].artifacts
    payload = worker.read_artifact(started.remote_job_id, "report.json").content
    assert hashlib.sha256(payload).hexdigest() == receipts[0].artifacts[0].sha256


def test_snapshot_manifest_rejects_reuse_by_a_different_request(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "report.json").write_bytes(b'{"request":"first"}')

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(job_id, "compute-test", "controller-job", "idem-1"),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    catalog = {"pytest": replace(_entry(), artifact_paths=("report.json",))}
    first_worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog=catalog,
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    first = first_worker.submit(_envelope())
    first_worker.status(first.remote_job_id)

    second_worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog=catalog,
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    second = second_worker.submit(_envelope(arguments=("different-test.py",)))
    assert second.remote_job_id == first.remote_job_id
    assert second.request_sha256 != first.request_sha256

    with pytest.raises(Conflict, match="request"):
        second_worker.status(second.remote_job_id)


def test_completed_artifact_jobs_do_not_retain_one_os_handle_per_job(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "report.json").write_bytes(b'{"ok":true}')

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(job_id, "compute-test", "controller-job", "idem-result"),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": replace(_entry(), artifact_paths=("report.json",))},
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    before = _process_handle_count()
    for index in range(80):
        started = worker.submit(_envelope(idempotency_key=f"idem-handle-{index}"))
        worker.status(started.remote_job_id)
    after = _process_handle_count()

    assert after - before <= 16


def test_artifact_manifest_reopens_at_the_catalog_cardinality_limit(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    artifact_names = tuple(f"report-{index:03}.json" for index in range(129))
    for name in artifact_names:
        (tests / name).write_bytes(b"{}")

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(job_id, "compute-test", "controller-job", "idem-1"),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    catalog = {"pytest": replace(_entry(), artifact_paths=artifact_names)}
    first_worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog=catalog,
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    first = first_worker.submit(_envelope())
    assert len(first_worker.status(first.remote_job_id).artifacts) == 129

    second_worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog=catalog,
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    second = second_worker.submit(_envelope())
    assert len(second_worker.status(second.remote_job_id).artifacts) == 129


def test_worker_rejects_artifact_handle_opened_outside_workspace_after_path_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    tests = workspace / "tests"
    tests.mkdir(parents=True)
    report = tests / "report.json"
    report.write_bytes(b'{"inside":true}')
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_report = outside / "report.json"
    outside_report.write_bytes(b'{"secret":true}')

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(job_id, "compute-test", "controller-job", "idem-1"),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": replace(_entry(), artifact_paths=("report.json",))},
        workspace_mappings={"sonder": workspace},
        provider=CompletedProvider(),
    )
    started = worker.submit(_envelope())
    original_open = Path.open
    original_stat = Path.stat

    def raced_open(path, *args, **kwargs):
        if path == report:
            return original_open(outside_report, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    def raced_stat(path, *args, **kwargs):
        if path == report:
            return original_stat(outside_report, *args, **kwargs)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", raced_open)
    monkeypatch.setattr(Path, "stat", raced_stat)

    with pytest.raises(InvalidInput, match="workspace"):
        worker.status(started.remote_job_id)


def test_worker_refuses_artifact_larger_than_transport_limit(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    report = tests / "report.bin"
    with report.open("wb") as stream:
        stream.truncate(MAX_COMPUTE_ARTIFACT_BYTES + 1)

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(job_id, "compute-test", "controller-job", "idem-1"),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": replace(_entry(), artifact_paths=("report.bin",))},
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    started = worker.submit(_envelope())

    with pytest.raises(InvalidInput, match="transport limit"):
        worker.status(started.remote_job_id)


def test_worker_never_publishes_digest_from_mutating_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    report = tests / "report.bin"
    report.write_bytes(b"a" * (2 * 1024 * 1024))

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(job_id, "compute-test", "controller-job", "idem-1"),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": replace(_entry(), artifact_paths=("report.bin",))},
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    started = worker.submit(_envelope())
    original_sha256 = jobs_module.hashlib.sha256

    class MutatingDigest:
        def __init__(self):
            self._digest = original_sha256()
            self._mutated = False

        def update(self, block):
            self._digest.update(block)
            if not self._mutated:
                self._mutated = True
                with report.open("ab") as stream:
                    stream.write(b"changed")

        def hexdigest(self):
            return self._digest.hexdigest()

    monkeypatch.setattr(jobs_module.hashlib, "sha256", MutatingDigest)

    with pytest.raises(InvalidInput, match="changed while hashing"):
        worker.status(started.remote_job_id)


def test_input_stage_cleanup_uses_python311_compatible_onerror(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "stages"
    stage = base / "job-stage"
    stage.mkdir(parents=True)
    captured = {}

    monkeypatch.setattr(
        ComputeJobWorker,
        "_input_stage_base",
        staticmethod(lambda: base),
    )

    def fake_rmtree(path, **kwargs):
        captured["path"] = path
        captured["kwargs"] = kwargs

    monkeypatch.setattr(jobs_module.shutil, "rmtree", fake_rmtree)

    ComputeJobWorker._remove_input_stage(stage)

    assert captured["path"] == stage.resolve()
    assert "onerror" in captured["kwargs"]
    assert "onexc" not in captured["kwargs"]


def test_worker_rehydrates_digest_bound_receipt_after_restart(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    database = tmp_path / "compute-restart.db"
    cleanup = ProcessTreeSupervisor(platform_name=os.name, timeout_seconds=5)
    first_provider = SubprocessJobProvider(
        SQLiteDurableJobRegistry(database),
        process_cleanup=cleanup,
        platform_name=os.name,
    )
    entry = replace(_entry(), program=sys.executable)
    first = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": entry},
        workspace_mappings={"sonder": tmp_path},
        provider=first_provider,
    )
    envelope = _envelope(arguments=("test_api.py",), deadline_seconds=30)
    started = first.submit(envelope)
    try:
        reopened_provider = SubprocessJobProvider(
            SQLiteDurableJobRegistry(database),
            process_cleanup=cleanup,
            platform_name=os.name,
        )
        reopened = ComputeJobWorker(
            worker_id="worker-1",
            catalog={"pytest": entry},
            workspace_mappings={"sonder": tmp_path},
            provider=reopened_provider,
        )

        recovered = reopened.by_idempotency(envelope.idempotency_key)
        assert recovered is not None
        assert recovered.remote_job_id == started.remote_job_id
        assert recovered.controller_job_id == envelope.controller_job_id
        assert recovered.request_sha256 == envelope.request_sha256
        assert reopened.status(started.remote_job_id).state in {"pending", "running"}
    finally:
        first_provider.cancel(started.remote_job_id, reason="test cleanup")
