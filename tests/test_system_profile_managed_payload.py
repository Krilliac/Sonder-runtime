"""A managed runtime's hashed payload must never be written by the profile module."""

import os

import pytest

from sonder_runtime.platform import system_profile


def _sealed_payload(tmp_path, monkeypatch, bundled=None):
    payload = tmp_path / "runtime-payload"
    payload.mkdir()
    (tmp_path / "runtime-artifacts.json").write_text("{}", encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(payload))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    monkeypatch.delenv("SONDER_STATE_HOME", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(state))
    if bundled is not None:
        (payload / "system_profile.md").write_bytes(bundled)
    return payload, state / "system_profile.md"


def _snapshot(root):
    return sorted(
        (str(p.relative_to(root)), p.read_bytes() if p.is_file() else None)
        for p in root.rglob("*")
    )


def test_live_path_is_the_state_home_copy(tmp_path, monkeypatch):
    _, live = _sealed_payload(tmp_path, monkeypatch)

    assert os.path.normcase(system_profile.live_path()) == os.path.normcase(str(live))


@pytest.mark.parametrize("operation", ["replace", "append", "clear"])
def test_profile_edits_in_a_sealed_payload_go_to_the_state_home(
    tmp_path, monkeypatch, operation
):
    payload, live = _sealed_payload(tmp_path, monkeypatch, bundled=b"- bundled\n")
    before = _snapshot(payload)

    if operation == "replace":
        written = system_profile.write_profile("- edited")
    elif operation == "append":
        written = system_profile.append_profile("- extra")
    else:
        written = system_profile.write_profile("")

    assert os.path.normcase(written) == os.path.normcase(str(live))
    assert _snapshot(payload) == before
    expected = {
        "replace": "- edited",
        "append": "- bundled\n\n- extra",
        "clear": "",
    }[operation]
    assert system_profile.read_profile() == expected


def test_reads_fall_back_to_the_bundled_payload_copy(tmp_path, monkeypatch):
    payload, live = _sealed_payload(tmp_path, monkeypatch, bundled=b"- bundled\n")

    assert system_profile.read_profile() == "- bundled"
    assert system_profile.system_prompt().endswith("- bundled")
    assert not live.exists()


def test_a_missing_bundled_profile_is_created_in_the_state_home(tmp_path, monkeypatch):
    payload, live = _sealed_payload(tmp_path, monkeypatch)

    prompt = system_profile.system_prompt()

    assert prompt.startswith("Standing instructions")
    assert list(payload.iterdir()) == []
    assert live.read_text(encoding="utf-8") == system_profile.DEFAULT_TEXT
    text, path = system_profile.ensure_profile()
    assert text == system_profile.DEFAULT_TEXT.strip()
    assert os.path.normcase(path) == os.path.normcase(str(live))


def test_an_explicit_payload_path_is_also_redirected(tmp_path, monkeypatch):
    payload, live = _sealed_payload(tmp_path, monkeypatch, bundled=b"- bundled\n")
    before = _snapshot(payload)

    written = system_profile.write_profile("explicit", str(payload / "system_profile.md"))

    assert os.path.normcase(written) == os.path.normcase(str(live))
    assert _snapshot(payload) == before


def test_sealed_payload_still_refuses_paths_outside_workspace_and_state_copy(
    tmp_path, monkeypatch
):
    _sealed_payload(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="inside workspace"):
        system_profile.write_profile("x", str(tmp_path / "state" / "other.md"))


def test_an_ordinary_workspace_keeps_writing_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))

    assert system_profile.write_profile("editable") == str(
        tmp_path / "system_profile.md"
    )
    assert system_profile.read_profile() == "editable"
    assert not (tmp_path / "state").exists()


def test_a_workspace_named_runtime_payload_without_a_manifest_is_not_sealed(
    tmp_path, monkeypatch
):
    payload = tmp_path / "runtime-payload"
    payload.mkdir()
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(payload))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)

    assert system_profile.write_profile("local") == str(payload / "system_profile.md")


def test_status_tool_reports_the_live_copy_it_reads(tmp_path, monkeypatch):
    import server

    payload, live = _sealed_payload(tmp_path, monkeypatch, bundled=b"- bundled\n")
    monkeypatch.setattr(server.system_profile, "workspace_root", lambda: str(payload))

    bundled_out = server.system_profile_text()
    assert "- bundled" in bundled_out
    assert os.path.normcase(str(payload / "system_profile.md")) in os.path.normcase(
        bundled_out
    )

    system_profile.write_profile("- live")
    live_out = server.system_profile_text()

    assert live_out.startswith("profile: ")
    assert os.path.normcase(str(live)) in os.path.normcase(live_out.splitlines()[0])
    assert "- live" in live_out


def test_live_profile_copy_is_in_the_control_plane_inventory(tmp_path, monkeypatch):
    from pathlib import Path

    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    _, live = _sealed_payload(tmp_path, monkeypatch)

    assert live_control_plane_inventory().protects(Path(system_profile.live_path()))
    assert live_control_plane_inventory().protects(live)
