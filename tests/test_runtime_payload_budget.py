"""The runtime closure must be cheap to refuse and expensive to counterfeit."""

import os
from pathlib import Path

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


def test_base_python_alias_inventories_only_its_ordinary_target(tmp_path):
    interpreter = tmp_path / "python.exe"
    interpreter.write_bytes(b"MZ ordinary interpreter")
    try:
        (tmp_path / "python3.exe").symlink_to(interpreter)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows runner lacks the privilege to create test symlinks")
        raise
    (tmp_path / "python312.dll").write_bytes(b"DLL")

    paths = runtime_payload.base_runtime_files(tmp_path)

    assert paths == (interpreter, tmp_path / "python312.dll")
    assert [row[0] for row in runtime_payload.inventory((str(path), False) for path in paths)] == [
        str(interpreter), str(tmp_path / "python312.dll"),
    ]


@pytest.mark.parametrize("alias,target", [
    ("python3.exe", "unrelated.exe"),
    ("tool.exe", "python.exe"),
    ("helper.dll", "python.exe"),
])
def test_base_python_rejects_other_reparse_binaries(tmp_path, alias, target):
    (tmp_path / "python.exe").write_bytes(b"MZ ordinary interpreter")
    (tmp_path / "unrelated.exe").write_bytes(b"MZ unrelated tool")
    try:
        (tmp_path / alias).symlink_to(Path(target))
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows runner lacks the privilege to create test symlinks")
        raise

    with pytest.raises(OwnerRefused, match="unrecognized reparse"):
        runtime_payload.base_runtime_files(tmp_path)
