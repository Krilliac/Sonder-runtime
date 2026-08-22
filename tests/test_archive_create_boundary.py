from __future__ import annotations

import pytest

import archive_create as native
import sonder_runtime.adapters.archive_create as adapter
import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.application.ports.archive_create import ArchiveCreateRequest


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    return root


def test_typed_adapter_preserves_native_request_mapping(monkeypatch):
    calls = []

    class Native:
        @staticmethod
        def create_archive(*args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

    monkeypatch.setattr(adapter, "_create_archive_native", Native.create_archive)
    request = ArchiveCreateRequest(
        "root", ["payload"], "bundle.zip", max_files=7, max_results=3,
    )

    assert adapter.create_archive(request) == {"ok": True}
    assert calls == [(
        ("root", ["payload"], "bundle.zip"),
        {
            "archive_format": "zip", "deterministic": True,
            "extra_roots": "", "developer_authorized": False,
            "max_files": 7, "max_results": 3,
        },
    )]


def test_legacy_import_is_the_canonical_module_and_monkeypatches_are_shared():
    assert native is adapter
    assert native.__file__.replace("\\", "/").endswith(
        "sonder_runtime/adapters/archive_create.py"
    )
    assert native._write_zip is adapter._write_zip


def test_adapter_preserves_native_safety_limits_and_result(project):
    source = project / "payload"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")

    with pytest.raises(native.ArchiveCreateRejected, match="max_files"):
        adapter.create_archive(
            ArchiveCreateRequest(str(project), ["payload"], "too-many.zip", max_files=1)
        )

    (source / "b.txt").unlink()
    result = adapter.create_archive(
        ArchiveCreateRequest(str(project), ["payload"], "bundle.zip", max_files=1)
    )

    assert result["ok"] is True
    assert result["limits"]["max_files"] == 1
    assert result["overwrote"] is False
    assert (project / "bundle.zip").is_file()

@pytest.mark.parametrize(
    "field,value",
    [("archive_format", "7z"), ("max_files", 0), ("deterministic", 1)],
)
def test_port_rejects_invalid_boundary_values(field, value):
    values = {"root": "root", "inputs_json": ["input"], "destination": "out.zip"}
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        ArchiveCreateRequest(**values)
