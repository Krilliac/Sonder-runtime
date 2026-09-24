"""The runtime closure must be cheap to refuse and expensive to counterfeit."""

import os

import pytest

from sonder_runtime.adapters.execution import runtime_payload
from sonder_runtime.application.ports.runtime_owner import OwnerRefused


def test_oversized_closure_is_refused_before_reading_any_file(tmp_path, monkeypatch):
    first = tmp_path / "a-first.bin"
    first.write_bytes(b"ordinary")
    (tmp_path / "z-too-large.bin").write_bytes(b"x" * 64)
    monkeypatch.setattr(runtime_payload, "MAX_BYTES", 32)

    def unexpected_read(self, *args, **kwargs):
        pytest.fail("an oversized closure must be refused before hashing starts")

    monkeypatch.setattr(runtime_payload.Path, "open", unexpected_read)
    with pytest.raises(OwnerRefused, match="dedicated runtime environment"):
        runtime_payload.inventory(((str(tmp_path), False),))


def test_unchanged_metadata_never_reuses_a_previous_content_digest(tmp_path):
    target = tmp_path / "module.py"
    target.write_bytes(b"AAAA")
    roots = ((str(tmp_path), False),)
    before = runtime_payload.inventory(roots)
    original = target.stat()
    target.write_bytes(b"BBBB")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))

    after = runtime_payload.inventory(roots)
    assert after != before
    assert after[0][3] == before[0][3] == 4
    assert after[0][4] != before[0][4]


def test_declared_closure_limit_refuses_sparse_files_without_hashing(
    tmp_path, monkeypatch
):
    target = tmp_path / "large-dependency.bin"
    monkeypatch.setattr(runtime_payload, "MAX_BYTES", 32 * 1024**2)
    with target.open("wb") as stream:
        stream.truncate(runtime_payload.MAX_BYTES + 1)
    with pytest.raises(OwnerRefused, match="dedicated runtime environment"):
        runtime_payload.inventory(((str(tmp_path), False),))
