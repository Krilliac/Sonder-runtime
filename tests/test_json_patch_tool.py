import json
import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import json_patch_tool as patcher


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _patch(path, operations, mode="preview"):
    return patcher.patch_json(str(path), operations, mode=mode, extra_roots=str(path.parent), bypass=True)


def test_preview_supports_strict_rfc_subset_without_writing(tmp_path):
    target = tmp_path / "data.json"
    original = {"a/b": {"~key": 1}, "items": ["a", "c"], "remove": True}
    _write(target, original)
    operations = [
        {"op": "test", "path": "/a~1b/~0key", "value": 1},
        {"op": "replace", "path": "/a~1b/~0key", "value": 2},
        {"op": "add", "path": "/items/1", "value": "b"},
        {"op": "add", "path": "/items/-", "value": "d"},
        {"op": "remove", "path": "/remove"},
    ]

    first = _patch(target, operations)
    second = _patch(target, operations)

    assert first == second
    assert first["applied"] is False
    assert first["document"] == {"a/b": {"~key": 2}, "items": ["a", "b", "c", "d"]}
    assert json.loads(target.read_text(encoding="utf-8")) == original


def test_reused_python_operations_are_not_mutated_by_preview(tmp_path):
    target = tmp_path / "data.json"
    _write(target, {})
    operations = [
        {"op": "add", "path": "/nested", "value": {"a": 1}},
        {"op": "test", "path": "/nested", "value": {"a": 1}},
        {"op": "replace", "path": "/nested/a", "value": 2},
    ]

    first = _patch(target, operations)
    second = _patch(target, operations)

    assert first == second
    assert first["document"] == {"nested": {"a": 2}}
    assert operations[0]["value"] == {"a": 1}


def test_apply_is_deterministically_formatted_and_root_replace_is_allowed(tmp_path):
    target = tmp_path / "data.json"
    _write(target, {"z": 1})
    result = _patch(target, [{"op": "replace", "path": "", "value": {"b": 2, "a": 1}}], "apply")

    assert result["applied"] is True
    assert target.read_text(encoding="utf-8") == '{\n  "a": 1,\n  "b": 2\n}\n'
    assert result["sha256_before"] != result["sha256_after"]


@pytest.mark.parametrize("op", ["move", "copy"])
def test_move_and_copy_are_rejected(op, tmp_path):
    target = tmp_path / "data.json"
    _write(target, {})
    with pytest.raises(ValueError, match="add, remove, replace, or test"):
        _patch(target, [{"op": op, "path": "/x", "from": "/y"}])


@pytest.mark.parametrize("path", ["x", "/bad~", "/bad~2", "/items/01"])
def test_pointer_validation_is_strict(path, tmp_path):
    target = tmp_path / "data.json"
    _write(target, {"items": [1, 2]})
    operation = {"op": "remove", "path": path}
    with pytest.raises(ValueError, match="operation|Pointer|array index"):
        _patch(target, [operation])


def test_exact_test_precondition_is_type_safe_and_aborts_apply(tmp_path):
    target = tmp_path / "data.json"
    _write(target, {"enabled": True, "version": 1})
    before = target.read_bytes()
    with pytest.raises(ValueError, match="test precondition failed"):
        _patch(target, [
            {"op": "test", "path": "/enabled", "value": 1},
            {"op": "replace", "path": "/version", "value": 2},
        ], "apply")
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "raw,error",
    [
        (b"1", "root must be"),
        (b'{"a":1,"a":2}', "duplicate"),
        (b'{"n":NaN}', "non-finite"),
        (b"\xff", "strict UTF-8"),
    ],
)
def test_target_must_be_strict_utf8_object_or_array(tmp_path, raw, error):
    target = tmp_path / "data.json"
    target.write_bytes(raw)
    with pytest.raises(ValueError, match=error):
        _patch(target, [{"op": "add", "path": "/x", "value": 1}])


def test_operation_fields_root_removal_and_missing_targets_fail_closed(tmp_path):
    target = tmp_path / "data.json"
    _write(target, {"a": 1})
    with pytest.raises(ValueError, match="exactly"):
        _patch(target, [{"op": "add", "path": "/b", "value": 2, "extra": True}])
    with pytest.raises(ValueError, match="root"):
        _patch(target, [{"op": "remove", "path": ""}])
    with pytest.raises(ValueError, match="does not exist"):
        _patch(target, [{"op": "replace", "path": "/missing", "value": 2}])


def test_hard_operation_depth_document_and_output_caps(monkeypatch, tmp_path):
    target = tmp_path / "data.json"
    _write(target, {"a": 1})
    monkeypatch.setattr(patcher, "MAX_OPERATIONS", 1)
    with pytest.raises(ValueError, match="max operations"):
        _patch(target, [
            {"op": "test", "path": "/a", "value": 1},
            {"op": "test", "path": "/a", "value": 1},
        ])
    monkeypatch.setattr(patcher, "MAX_OPERATIONS", 100)
    monkeypatch.setattr(patcher, "MAX_JSON_DEPTH", 2)
    with pytest.raises(ValueError, match="nesting"):
        _patch(target, [{"op": "add", "path": "/deep", "value": {"x": {"y": 1}}}])
    monkeypatch.setattr(patcher, "MAX_JSON_DEPTH", 64)
    monkeypatch.setattr(patcher, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(ValueError, match="output bytes"):
        _patch(target, [{"op": "test", "path": "/a", "value": 1}])


def test_file_and_patch_input_byte_caps(monkeypatch, tmp_path):
    target = tmp_path / "data.json"
    target.write_text('{"payload":"long"}', encoding="utf-8")
    monkeypatch.setattr(patcher, "MAX_DOCUMENT_BYTES", 4)
    with pytest.raises(ValueError, match="document exceeds"):
        _patch(target, [{"op": "add", "path": "/x", "value": 1}])

    monkeypatch.setattr(patcher, "MAX_DOCUMENT_BYTES", 256_000)
    monkeypatch.setattr(patcher, "MAX_OPERATIONS_BYTES", 4)
    with pytest.raises(ValueError, match="input bytes"):
        _patch(target, '[{"op":"remove","path":"/x"}]')


def test_sensitive_outside_and_symlink_targets_are_rejected(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    (workspace / ".git").mkdir()
    _write(workspace / ".git" / "config", {})
    with pytest.raises(PermissionError, match="control state|protected"):
        patcher.patch_json(".git/config", [{"op": "add", "path": "/x", "value": 1}])

    outside = tmp_path / "outside.json"
    _write(outside, {})
    with pytest.raises(PermissionError, match="outside allowed roots"):
        patcher.patch_json(str(outside), [{"op": "add", "path": "/x", "value": 1}])

    link = workspace / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    with pytest.raises(PermissionError, match="symlink|junction"):
        patcher.patch_json("linked.json", [{"op": "add", "path": "/x", "value": 1}])


def test_sensitive_authorized_root_itself_is_rejected(tmp_path):
    sensitive_root = tmp_path / ".azure"
    sensitive_root.mkdir()
    target = sensitive_root / "credentials.json"
    _write(target, {"token": "secret"})
    operations = [{"op": "test", "path": "/token", "value": "secret"}]

    with pytest.raises(PermissionError, match="secret or control state"):
        patcher.patch_json(
            str(target), operations, extra_roots=str(sensitive_root), bypass=True,
        )


def test_post_replace_failure_atomically_restores_original(monkeypatch, tmp_path):
    target = tmp_path / "data.json"
    _write(target, {"value": 1})
    original = target.read_bytes()
    calls = {"count": 0}
    real_verify = patcher._verify_written

    def fail_first_verification(path, expected):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("injected post-replace failure")
        return real_verify(path, expected)

    monkeypatch.setattr(patcher, "_verify_written", fail_first_verification)
    with pytest.raises(patcher.JsonPatchError) as caught:
        _patch(target, [{"op": "replace", "path": "/value", "value": 2}], "apply")

    assert caught.value.report["transaction"] == "rolled_back"
    assert target.read_bytes() == original
    assert list(tmp_path.glob(".sonder-json-patch-*.tmp")) == []


def test_identity_change_before_commit_preserves_replacement_file(monkeypatch, tmp_path):
    target = tmp_path / "data.json"
    _write(target, {"value": 1})
    real_snapshot = patcher._read_snapshot
    calls = {"count": 0}

    def stale_snapshot(path):
        calls["count"] += 1
        raw, identity, mode = real_snapshot(path)
        if calls["count"] == 2:
            return raw, (identity[0], identity[1] + 1), mode
        return raw, identity, mode

    monkeypatch.setattr(patcher, "_read_snapshot", stale_snapshot)
    with pytest.raises(patcher.JsonPatchError) as caught:
        _patch(target, [{"op": "replace", "path": "/value", "value": 2}], "apply")
    assert caught.value.report["transaction"] == "not_committed"
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 1}


def test_same_identity_content_change_before_commit_is_rejected(monkeypatch, tmp_path):
    target = tmp_path / "data.json"
    _write(target, {"value": 1})
    real_snapshot = patcher._read_snapshot
    calls = {"count": 0}

    def changed_snapshot(path):
        calls["count"] += 1
        raw, identity, mode = real_snapshot(path)
        return (raw + b" ", identity, mode) if calls["count"] == 2 else (raw, identity, mode)

    monkeypatch.setattr(patcher, "_read_snapshot", changed_snapshot)
    with pytest.raises(patcher.JsonPatchError) as caught:
        _patch(target, [{"op": "replace", "path": "/value", "value": 2}], "apply")
    assert caught.value.report["transaction"] == "not_committed"
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 1}
