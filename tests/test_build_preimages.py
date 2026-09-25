"""The private pre-image store a build fix restores from (F17)."""
from __future__ import annotations

import hashlib
import os
import stat
import uuid

import pytest

pytest.importorskip("sonder_runtime.domain.build.model", reason="needs lane A-domain-build")

from sonder_runtime.adapters.build.preimages import FilePreimageStore  # noqa: E402
from sonder_runtime.application.build.fix_ports import PreimageIntegrityError  # noqa: E402
from sonder_runtime.domain.common.errors import InvalidInput, NotFound  # noqa: E402

pytestmark = pytest.mark.unit

POSIX = os.name != "nt"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def job_id() -> str:
    return "build-fix-" + uuid.uuid4().hex


@pytest.fixture
def store(tmp_path):
    return FilePreimageStore(tmp_path / "state" / "build-fix")


def begun(store, **manifest):
    job = job_id()
    store.begin(job, {"principal_id": "owner", "project_root": "/p", "status": "running", **manifest})
    return job


def test_originals_round_trip_byte_exact(store):
    job = begun(store)
    text = "int a;\r\n// café ☃\r\nint b;"
    store.save(job, "src/a.cpp", text, sha(text))
    assert store.load(job, "src/a.cpp") == (text, sha(text))
    store.record_write(job, "src/a.cpp", sha("new"))
    entry = store.list(job)[0]
    assert (entry.rel, entry.original_sha256, entry.last_written_sha256) == ("src/a.cpp", sha(text), sha("new"))
    assert store.label(job) == "state/build-fix/" + job and "/" not in store.label(job)[:1]


def test_the_first_original_wins(store):
    job = begun(store)
    store.save(job, "src/a.cpp", "first", sha("first"))
    store.save(job, "src/a.cpp", "second", sha("second"))
    assert store.load(job, "src/a.cpp")[0] == "first"


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_directories_are_0700_and_files_0600(store, tmp_path):
    job = begun(store)
    store.save(job, "src/a.cpp", "x", sha("x"))
    root = tmp_path / "state" / "build-fix"
    for path in (root, root / job, root / job / "blobs"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    for path in [root / job / "manifest.json", *(root / job / "blobs").iterdir()]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_a_tampered_blob_fails_its_sha256_check(store, tmp_path):
    job = begun(store)
    store.save(job, "src/a.cpp", "original", sha("original"))
    blob = next((tmp_path / "state" / "build-fix" / job / "blobs").iterdir())
    blob.write_text("tampered")
    with pytest.raises(PreimageIntegrityError):
        store.load(job, "src/a.cpp")


@pytest.mark.skipif(not POSIX, reason="symlink semantics")
def test_links_are_never_followed(store, tmp_path):
    job = begun(store)
    store.save(job, "src/a.cpp", "original", sha("original"))
    blob = next((tmp_path / "state" / "build-fix" / job / "blobs").iterdir())
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    blob.unlink()
    blob.symlink_to(outside)
    with pytest.raises(PreimageIntegrityError):
        store.load(job, "src/a.cpp")
    manifest = tmp_path / "state" / "build-fix" / job / "manifest.json"
    manifest.unlink()
    manifest.symlink_to(outside)
    with pytest.raises(PreimageIntegrityError):
        store.list(job)


def test_inputs_are_validated_and_bounded(store):
    job = begun(store)
    with pytest.raises(InvalidInput):
        store.begin("../escape", {})
    with pytest.raises(InvalidInput):
        store.save(job, "../outside.cpp", "x", sha("x"))
    with pytest.raises(InvalidInput):
        store.save(job, "/etc/passwd", "x", sha("x"))
    with pytest.raises(PreimageIntegrityError):
        store.save(job, "src/a.cpp", "x", sha("not x"))
    big = "x" * (2 * 1024 * 1024 + 1)
    with pytest.raises(InvalidInput):
        store.save(job, "src/big.cpp", big, sha(big))
    for index in range(16):
        store.save(job, "src/f%d.cpp" % index, str(index), sha(str(index)))
    with pytest.raises(InvalidInput):
        store.save(job, "src/f16.cpp", "16", sha("16"))
    with pytest.raises(NotFound):
        store.record_write(job, "src/unknown.cpp", sha("y"))
    assert store.manifest(job_id()) is None


def test_status_jobs_and_purge(store):
    first = begun(store)
    second = begun(store)
    store.set_status(first, "interrupted")
    assert store.manifest(first)["status"] == "interrupted"
    assert set(store.jobs()) == {first, second}
    store.purge(first)
    assert store.jobs() == (second,)
    with pytest.raises(InvalidInput):
        store.set_status(second, "Bad Status!")


def test_pruning_keeps_unfinished_jobs(tmp_path):
    store = FilePreimageStore(tmp_path / "pre", max_jobs=3)
    running = begun(store)
    finished = []
    for _ in range(2):
        job = begun(store)
        store.set_status(job, "fixed")
        finished.append(job)
    newest = begun(store)
    jobs = set(store.jobs())
    assert running in jobs and newest in jobs
    assert len(jobs) <= 3 and not set(finished) <= jobs
