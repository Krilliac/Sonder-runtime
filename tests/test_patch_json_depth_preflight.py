"""Patch depth refusals happen before avoidable JSON decoding or serialization."""
import json

import pytest

from sonder_runtime.adapters.filesystem import file_ops, json_patch
from sonder_runtime.domain.build import repair
from sonder_runtime.domain.build.model import BuildDomainError

pytestmark = pytest.mark.unit


def _nested_empty(depth):
    value = []
    for _ in range(1, depth):
        value = [value]
    return value


def _record_json_work(monkeypatch, module):
    calls = {"loads": [], "dumps": 0}

    class RecordingJson:
        def loads(self, text, *args, **kwargs):
            calls["loads"].append(text)
            return json.loads(text, *args, **kwargs)

        def dumps(self, value, *args, **kwargs):
            calls["dumps"] += 1
            return json.dumps(value, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(json, name)

    # Replace this consumer's JSON binding, leaving policy/snapshot JSON alone.
    monkeypatch.setattr(module, "json", RecordingJson())
    return calls


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "data.json").write_bytes(b'{"value":1}\n')
    (root / "neighbor.json").write_bytes(b'{"untouched":true}\n')
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    with file_ops.read_policy_snapshot():
        yield root


def _snapshot(root):
    return {path.name: path.read_bytes() for path in root.iterdir()}


@pytest.mark.parametrize("representation", ["text", "list"])
def test_overdeep_operations_are_refused_before_json_work(workspace, monkeypatch, representation):
    # List + operation object + 63 empty arrays = depth65 under the existing policy.
    operations = [{"op": "add", "path": "/deep", "value": _nested_empty(63)}]
    supplied = json.dumps(operations) if representation == "text" else operations
    before = _snapshot(workspace)
    calls = _record_json_work(monkeypatch, json_patch)
    with pytest.raises(ValueError):
        json_patch.patch_json("data.json", supplied, mode="apply")
    assert _snapshot(workspace) == before
    if representation == "text":
        assert calls["loads"] == []
    else:
        assert calls["dumps"] == 0


def test_overdeep_target_document_is_refused_before_decoding(workspace, monkeypatch):
    text = "[" * 65 + "]" * 65
    (workspace / "data.json").write_text(text, encoding="utf-8")
    before = _snapshot(workspace)
    calls = _record_json_work(monkeypatch, json_patch)
    with pytest.raises(ValueError):
        json_patch.patch_json("data.json", [{"op": "add", "path": "/-", "value": 1}], mode="apply")
    assert _snapshot(workspace) == before
    assert text not in calls["loads"]


@pytest.mark.parametrize("depth", [64, 65])
def test_operation_depth_boundary_preserves_the_existing_policy(workspace, depth):
    operations = [{"op": "add", "path": "/deep", "value": _nested_empty(depth - 2)}]
    before = _snapshot(workspace)
    if depth == 64:
        result = json_patch.patch_json("data.json", operations)
        assert result["document"]["deep"] == operations[0]["value"]
        assert result["applied"] is False
    else:
        with pytest.raises(ValueError):
            json_patch.patch_json("data.json", operations)
    assert _snapshot(workspace) == before


@pytest.mark.parametrize("depth", [64, 65])
def test_document_depth_boundary_preserves_the_existing_policy(workspace, depth):
    text = "[" * depth + "]" * depth
    (workspace / "data.json").write_text(text, encoding="utf-8")
    before = _snapshot(workspace)
    operations = [{"op": "add", "path": "/-", "value": 1}]
    if depth == 64:
        result = json_patch.patch_json("data.json", operations)
        assert result["document"][-1] == 1
        assert result["applied"] is False
    else:
        with pytest.raises(ValueError):
            json_patch.patch_json("data.json", operations)
    assert _snapshot(workspace) == before


@pytest.mark.parametrize("fenced", [False, True])
def test_deep_repair_answer_is_refused_before_decoding(monkeypatch, fenced):
    text = "[" * 65 + "]" * 65
    if fenced:
        text = "```json\n" + text + "\n```"
    calls = _record_json_work(monkeypatch, repair)
    with pytest.raises(BuildDomainError) as caught:
        repair.parse_candidate_patch(text)
    assert caught.value.code == "FIX_SCOPE_REJECTED"
    assert calls["loads"] == []


@pytest.mark.parametrize("depth", [64, 65])
def test_repair_depth_boundary_preserves_normalized_rationale_contract(depth):
    # Non-string rationale already normalizes to empty; hunks remain fully valid.
    text = ('{"hunks":[{"file":"src/main.cpp","anchor":"old","replace":"new"}],'
            '"rationale":' + "[" * (depth - 1) + "]" * (depth - 1) + "}")
    if depth == 64:
        result = repair.parse_candidate_patch(text, model_id="synthetic-model")
        assert result.files() == ("src/main.cpp",)
        assert result.hunks[0].replacement == "new"
        assert result.rationale == ""
        assert result.model_id == "synthetic-model"
    else:
        with pytest.raises(BuildDomainError) as caught:
            repair.parse_candidate_patch(text)
        assert caught.value.code == "FIX_SCOPE_REJECTED"


@pytest.mark.parametrize("representation", ["text", "list"])
def test_quoted_brackets_remain_ordinary_patch_text(workspace, representation):
    literal = "[{" * 40 + '\\"' + "}]" * 40
    operations = [{"op": "add", "path": "/note", "value": literal}]
    supplied = json.dumps(operations) if representation == "text" else operations
    before = _snapshot(workspace)
    result = json_patch.patch_json("data.json", supplied)
    assert result["document"] == {"value": 1, "note": literal}
    assert result["applied"] is False
    assert _snapshot(workspace) == before


def test_quoted_brackets_remain_ordinary_repair_text():
    literal = "[{" * 40 + '\\"' + "}]" * 40
    text = json.dumps({"hunks": [{"file": "src/main.cpp", "anchor": "old", "replace": literal}],
                       "rationale": literal})
    result = repair.parse_candidate_patch(text)
    assert result.hunks[0].replacement == literal
    assert result.rationale == literal


def test_normal_apply_still_commits_only_the_authorized_target(workspace):
    neighbor_before = (workspace / "neighbor.json").read_bytes()
    result = json_patch.patch_json(
        "data.json", [{"op": "replace", "path": "/value", "value": 2}], mode="apply",
    )
    assert result["applied"] is True
    assert json.loads((workspace / "data.json").read_text(encoding="utf-8")) == {"value": 2}
    assert (workspace / "neighbor.json").read_bytes() == neighbor_before
    assert sorted(path.name for path in workspace.iterdir()) == ["data.json", "neighbor.json"]


def test_valid_patch_still_cannot_write_outside_authorized_workspace(workspace):
    outside = workspace.parent / "outside.json"
    outside.write_bytes(b'{"value":1}\n')
    before = _snapshot(workspace)
    with pytest.raises(PermissionError):
        json_patch.patch_json(str(outside), [{"op": "replace", "path": "/value", "value": 2}], mode="apply")
    assert outside.read_bytes() == b'{"value":1}\n'
    assert _snapshot(workspace) == before


_JSON_BYTE_ENCODINGS = ("utf-8", "utf-8-sig", "utf-16", "utf-16-be", "utf-32", "utf-32-be")


@pytest.mark.parametrize("encoding", _JSON_BYTE_ENCODINGS)
def test_private_strict_loader_preserves_installed_json_byte_encodings(encoding):
    value = {"note": "café Δ雪"}
    raw = json.dumps(value, ensure_ascii=False).encode(encoding)
    assert json_patch._loads_strict(raw, "encoded document") == value


@pytest.mark.parametrize("encoding", _JSON_BYTE_ENCODINGS)
def test_deep_encoded_json_is_refused_before_decoder_work(monkeypatch, encoding):
    text = "[" * 65 + json.dumps("é", ensure_ascii=False) + "]" * 65
    raw = text.encode(encoding)
    calls = _record_json_work(monkeypatch, json_patch)
    with pytest.raises(ValueError):
        json_patch._loads_strict(raw, "encoded document")
    assert calls["loads"] == []
