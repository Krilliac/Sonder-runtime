"""The model tool policy files are protected from every general file tool.

The shipped file holds the tool-lane floor and the operator overlay holds the
grants and deny profiles. Only the attended ``/runtime tools`` editor writes the
overlay, and nothing at run time writes the shipped file. A file_write,
file_edit or file_copy from an agent turn or the MCP file tools would let a
model grant itself tools, drop the operator's deny profiles, or narrow the
floor, so each is refused without a developer token.

Every refusal is paired with a control that the same call succeeds on a plain
file in the same folder, so a refusal for another reason (the folder outside
the allowed roots, say) cannot pass these tests.
"""
from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.platform import paths as runtime_paths

PROTECTED = "control-plane|control state"
GRANT = json.dumps({"version": 1, "model_profiles": [
    {"id": "forged", "match": {"names": [], "prefixes": ["astra-cyber-"]},
     "tools": ["text_search"], "lanes": ["agent"]},
]})


@pytest.fixture(params=["state-home", "env-override"])
def overlay(request, monkeypatch, tmp_path):
    """The overlay at its default state-home location, or under the env override."""
    monkeypatch.delenv(mtp.OVERLAY_ENV, raising=False)
    # tmp_path is an allowed file root, so a refusal can only come from protection.
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    if request.param == "state-home":
        home = tmp_path / "home"
        home.mkdir()
        runtime_paths.configure_home(home)  # restored by conftest's _isolate_runtime_home
        path = home / mtp.OVERLAY_FILE_NAME
    else:
        folder = tmp_path / "elsewhere"
        folder.mkdir()
        path = folder / mtp.OVERLAY_FILE_NAME
        monkeypatch.setenv(mtp.OVERLAY_ENV, str(path))
    assert mtp.overlay_path().resolve() == path.resolve()
    path.write_text('{"version": 1, "model_profiles": []}\n', encoding="utf-8")
    return path


def _control(folder, name="plain.txt"):
    """The same write to an ordinary file in the same folder succeeds."""
    result = file_ops.write_file(str(folder / name), "ok")
    assert (folder / name).read_text(encoding="utf-8") == "ok", result


def test_overlay_and_its_lock_and_temp_siblings_are_protected(overlay):
    for path in (
        overlay,
        overlay.with_name(overlay.name + ".lock"),
        overlay.with_name(overlay.name + ".tmp-0123456789abcdef"),
    ):
        assert file_ops._is_protected_mutation_path(path), path
    assert not file_ops._is_protected_mutation_path(overlay.with_name("plain.txt"))


def test_file_write_to_the_overlay_is_refused(overlay):
    before = overlay.read_bytes()
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file(str(overlay), GRANT, mode="overwrite")
    assert overlay.read_bytes() == before
    _control(overlay.parent)


def test_file_write_creating_a_missing_overlay_is_refused(overlay):
    overlay.unlink()
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file(str(overlay), GRANT)
    assert not overlay.exists()
    _control(overlay.parent)


def test_file_edit_of_the_overlay_is_refused(overlay):
    before = overlay.read_bytes()
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.edit_file(
            str(overlay), '"model_profiles": []', '"model_profiles": [1]'
        )
    assert overlay.read_bytes() == before


def test_file_copy_onto_the_overlay_is_refused(overlay):
    source = overlay.with_name("forged.json")
    source.write_text(GRANT, encoding="utf-8")
    before = overlay.read_bytes()
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.copy_file(str(source), str(overlay), overwrite=True)
    assert overlay.read_bytes() == before
    # Control: the same copy to an ordinary name in the same folder works.
    file_ops.copy_file(str(source), str(overlay.with_name("copy.json")))
    assert overlay.with_name("copy.json").read_text(encoding="utf-8") == GRANT


# ---- the shipped file ------------------------------------------------------------
# These calls are chosen so that, if the protection were missing, they still
# could not change the real shipped file: create mode on an existing file, an
# edit whose old text is absent, and a copy without overwrite.

def test_shipped_policy_is_protected_from_mutation():
    assert mtp.SHIPPED_PATH.is_file()
    assert file_ops._is_protected_mutation_path(mtp.SHIPPED_PATH)
    # Control: an ordinary JSON file elsewhere in the checkout is not.
    workspace = file_ops.workspace_root()
    assert not file_ops._is_protected_mutation_path(workspace / "docs" / "unprotected.json")


def test_file_write_to_the_shipped_policy_is_refused():
    before = mtp.SHIPPED_PATH.read_bytes()
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file(str(mtp.SHIPPED_PATH), GRANT, mode="create")
    assert mtp.SHIPPED_PATH.read_bytes() == before


def test_file_edit_of_the_shipped_policy_is_refused():
    before = mtp.SHIPPED_PATH.read_bytes()
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.edit_file(str(mtp.SHIPPED_PATH), "text absent from the shipped file", "x")
    assert mtp.SHIPPED_PATH.read_bytes() == before


def test_file_copy_onto_the_shipped_policy_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    source = tmp_path / "forged.json"
    source.write_text(GRANT, encoding="utf-8")
    before = mtp.SHIPPED_PATH.read_bytes()
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.copy_file(str(source), str(mtp.SHIPPED_PATH))
    assert mtp.SHIPPED_PATH.read_bytes() == before
