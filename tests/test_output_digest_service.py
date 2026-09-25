"""OutputDigestService: ownership, redaction-before-parse, guarded files."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.platform.logging as sonder_logging
from sonder_runtime.adapters.diagnostics.sources import GuardedFileWindowSource
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.diagnostics.ports import TextWindow
from sonder_runtime.application.diagnostics.service import (
    MODEL_DIGEST_JOB_KINDS,
    DigestSourceRejected,
    OutputDigestService,
)
from sonder_runtime.domain.common.errors import Cancelled, NotFound
from sonder_runtime.platform.logging import REDACTION_FAILED, Redactor


class _Jobs:
    def __init__(self, jobs):
        self.jobs = jobs
        self.reads = []

    def job_metadata(self, job_id):
        row = self.jobs.get(job_id)
        return None if row is None else dict(row["meta"])

    def read_output(self, job_id, *, max_bytes=2_000_000, head_bytes=65_536):
        self.reads.append((job_id, max_bytes, head_bytes))
        text = self.jobs[job_id]["text"]
        return TextWindow(text, "job:%s" % job_id, len(text), len(text), False)


class _Files:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    def read_file_window(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return TextWindow(self.text, path, len(self.text), len(self.text), False)


OUTPUT = "FAILED t.py::test_a - assert 1 == 2\n1 failed, 1 passed in 0.10s\n"


def _context(**kwargs):
    return local_owner_context(correlation_id="c", **kwargs)


def _service(jobs=None, files=None, redact=None):
    return OutputDigestService(
        files or _Files(), jobs, redact=redact or Redactor(env={}).redact,
    )


def _jobs():
    return _Jobs({
        "test-run-own": {"meta": {"kind": "tool.test_run", "principal_id": "owner"}, "text": OUTPUT},
        "lane-test-own": {"meta": {"kind": "agent_lane.test", "principal_id": "owner"}, "text": OUTPUT},
        "test-run-other": {"meta": {"kind": "tool.test_run", "principal_id": "someone-else"}, "text": OUTPUT},
        "proc-own": {"meta": {"kind": "process", "principal_id": "owner"}, "text": OUTPUT},
    })


def test_model_may_digest_its_own_test_jobs():
    jobs = _jobs()
    service = _service(jobs)
    for job in ("test-run-own", "lane-test-own"):
        digest = service.digest_job(job, _context())
        assert digest.summary.failed == 1
        assert digest.source_kind == "job" and digest.source_label == "job:%s" % job
    assert jobs.reads[0][1:] == (2_000_000, 65_536)
    assert MODEL_DIGEST_JOB_KINDS == {"tool.test_run", "agent_lane.test"}


@pytest.mark.parametrize("job_id", ["test-run-other", "proc-own", "missing", "bad id!", ""])
def test_other_principals_kinds_and_missing_jobs_are_all_not_found(job_id):
    jobs = _jobs()
    with pytest.raises(NotFound) as refused:
        _service(jobs).digest_job(job_id, _context())
    assert str(refused.value) == "job not found"
    assert jobs.reads == []


def test_operator_may_digest_any_existing_job():
    service = _service(_jobs())
    assert service.digest_job("proc-own", _context(), operator=True).summary.passed == 1
    assert service.digest_job("test-run-other", _context(), operator=True).summary.failed == 1
    with pytest.raises(NotFound):
        service.digest_job("missing", _context(), operator=True)


def test_cancelled_context_is_refused_before_reading():
    class _Cancelled:
        cancelled = True

        def wait(self, timeout=None):
            return True

    jobs = _jobs()
    with pytest.raises(Cancelled):
        _service(jobs).digest_job("test-run-own", _context(cancellation=_Cancelled()))
    assert jobs.reads == []


def _secret():
    # Runtime-built so no credential-shaped literal is committed.
    return "".join(["zq", "Xv", "9"]) + "Lm4" * 8


def test_redaction_happens_before_parsing_every_field():
    secret = _secret()
    text = (
        "FAILED t.py::test_login - AssertionError: expected %s\n"
        "t.c:3:1: error: bad value %s\n"
        "api_token = %s\n"
        "1 failed in 0.1s\n"
    ) % (secret, secret, secret)
    service = _service(_Jobs({"test-run-x": {
        "meta": {"kind": "tool.test_run", "principal_id": "owner"}, "text": text,
    }}), redact=Redactor(secret_values=[secret], env={}).redact)
    digest = service.digest_job("test-run-x", _context())
    wire = repr(digest.to_wire())
    assert secret not in wire
    assert "[REDACTED]" in wire
    assert any("[REDACTED]" in d.message for d in digest.first_errors)
    # control: without the secret registered, the value would have surfaced
    plain = _service(_Jobs({"test-run-x": {
        "meta": {"kind": "tool.test_run", "principal_id": "owner"}, "text": "FAILED t.py::a - %s\n" % secret,
    }}), redact=Redactor(env={}).redact).digest_job("test-run-x", _context())
    assert secret in repr(plain.to_wire())


def test_redaction_failure_sentinel_is_propagated_never_the_original(monkeypatch):
    failures = []

    class _Exploding:
        groups = 0

        def sub(self, *args, **kwargs):
            raise RuntimeError("pattern exploded")

    monkeypatch.setattr(sonder_logging, "_PATTERNS", (_Exploding(),))
    redactor = Redactor(env={}, failure_hook=lambda: failures.append(1))
    secret = _secret()
    service = _service(_Jobs({"test-run-x": {
        "meta": {"kind": "tool.test_run", "principal_id": "owner"},
        "text": "FAILED t.py::a - %s\n" % secret,
    }}), redact=redactor.redact)
    digest = service.digest_job("test-run-x", _context())
    assert digest.final_line == REDACTION_FAILED
    assert secret not in repr(digest.to_wire())
    assert failures


def test_digest_text_redacts_and_labels():
    secret = _secret()
    service = _service(redact=Redactor(secret_values=[secret], env={}).redact)
    digest = service.digest_text("error: %s\n" % secret, label="paste")
    assert digest.source_kind == "text" and digest.source_label == "paste"
    assert secret not in repr(digest.to_wire())


def test_file_digest_passes_bounded_arguments_to_the_source():
    files = _Files(OUTPUT)
    digest = _service(files=files).digest_file("logs/run.txt", _context(), max_scan_bytes=10**12)
    assert digest.summary.failed == 1
    path, kwargs = files.calls[0]
    assert path == "logs/run.txt"
    assert kwargs == {"extra_roots": "", "max_scan_bytes": 4_000_000,
                      "tail_lines": 50_000, "timeout_seconds": 5.0}


# --- real guarded file source ----------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    return root


def _real_service():
    return OutputDigestService(GuardedFileWindowSource(), None, redact=Redactor(env={}).redact)


def test_real_file_digest_control_case(project):
    (project / "build.log").write_text(OUTPUT, encoding="utf-8")
    digest = _real_service().digest_file(str(project / "build.log"), _context())
    assert digest.summary.failed == 1
    assert digest.source_label == "build.log"


@pytest.mark.parametrize("name", [".env", ".env.local", "id_rsa", "x.pem", "server.key", "credentials.json"])
def test_secret_files_are_refused_with_a_permitting_neighbour(project, name):
    (project / name).write_text(OUTPUT, encoding="utf-8")
    with pytest.raises(DigestSourceRejected) as refused:
        _real_service().digest_file(str(project / name), _context())
    assert isinstance(refused.value, PermissionError)
    assert refused.value.code == "DIGEST_SOURCE_REJECTED"
    (project / "neighbour.log").write_text(OUTPUT, encoding="utf-8")
    assert _real_service().digest_file(str(project / "neighbour.log"), _context()).summary


def test_credential_directory_is_refused(project):
    store = project / ".ssh"
    store.mkdir()
    (store / "notes.txt").write_text(OUTPUT, encoding="utf-8")
    with pytest.raises(DigestSourceRejected):
        _real_service().digest_file(str(store / "notes.txt"), _context())


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlink_is_refused(project):
    target = project / "real.log"
    target.write_text(OUTPUT, encoding="utf-8")
    link = project / "link.log"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(DigestSourceRejected):
        _real_service().digest_file(str(link), _context())
    assert _real_service().digest_file(str(target), _context()).summary.failed == 1


def test_path_outside_the_roots_is_refused(project, tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_text(OUTPUT, encoding="utf-8")
    with pytest.raises(DigestSourceRejected):
        _real_service().digest_file(str(outside), _context())
    assert Path(outside).exists()


@pytest.mark.parametrize("name", [".env", ".env.production", "id_ed25519", "cert.pem", "a.pfx", "token.json"])
def test_source_own_secret_guard_holds_even_if_the_lower_guard_admits(project, name):
    """Defense in depth: with a permissive resolver/reader, the source still refuses."""
    target = project / name
    target.write_text(OUTPUT, encoding="utf-8")
    reads = []

    def permissive_reader(path, **kwargs):
        reads.append(path)
        return [OUTPUT], {"bytes_read": len(OUTPUT), "source_bytes": len(OUTPUT)}, Path(path)

    source = GuardedFileWindowSource(
        resolver=lambda path, extra_roots="": Path(path), reader=permissive_reader,
    )
    with pytest.raises(DigestSourceRejected):
        source.read_file_window(str(target), extra_roots="", max_scan_bytes=100,
                                tail_lines=10, timeout_seconds=1.0)
    assert reads == []  # refused before a single byte was read
    control = project / "ordinary.log"
    control.write_text(OUTPUT, encoding="utf-8")
    window = source.read_file_window(str(control), extra_roots="", max_scan_bytes=100,
                                     tail_lines=10, timeout_seconds=1.0)
    assert window.label == "ordinary.log" and reads == [str(control)]


def test_source_rechecks_the_opened_path(project):
    """A swap between resolve and open cannot smuggle a secret file in."""
    decoy = project / "plain.log"
    decoy.write_text(OUTPUT, encoding="utf-8")
    source = GuardedFileWindowSource(
        resolver=lambda path, extra_roots="": Path(path),
        reader=lambda path, **kwargs: ([OUTPUT], {}, project / ".env"),
    )
    with pytest.raises(DigestSourceRejected):
        source.read_file_window(str(decoy), extra_roots="", max_scan_bytes=100,
                                tail_lines=10, timeout_seconds=1.0)
