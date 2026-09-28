"""A permission-mode change that cannot be saved must not be reported as done.

Before this fix ``_save`` swallowed every ``OSError``: an operator lowering
``auto`` to ``manual`` was told the change worked, and the next process start
read the stale file and restored ``auto``. The file was also truncated in
place outside the lock, so concurrent changes could leave invalid JSON.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

import permission_modes as pm


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "permission_mode.json"
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    monkeypatch.setattr(pm, "_state_path", lambda: str(path))
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = True
    try:
        yield path
    finally:
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        with pm._LOCK:
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


def _restart():
    with pm._LOCK:
        pm._STATE["mode"] = pm.DEFAULT_MODE
    pm._LOADED = False
    return pm.current_mode()


def test_successful_change_is_persisted_atomically(state_file):
    pm.set_mode(pm.AUTO)
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"mode": pm.AUTO}
    assert [p.name for p in state_file.parent.iterdir()] == [state_file.name]
    assert _restart() == pm.AUTO


def test_lowering_that_cannot_be_written_is_reported_and_never_restores_higher(
    state_file, monkeypatch,
):
    pm.set_mode(pm.AUTO)

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pm.os, "replace", refuse)
    with pytest.raises(pm.ModePersistenceError) as caught:
        pm.set_mode(pm.MANUAL)
    assert isinstance(caught.value, ValueError)
    assert "manual" in str(caught.value)
    # The lowering holds for this session...
    assert pm.current_mode() == pm.MANUAL
    monkeypatch.undo()
    # ...and the stale higher mode is not what the next start restores.
    assert _restart() != pm.AUTO
    assert [p.name for p in state_file.parent.iterdir() if ".tmp-" in p.name] == []


def test_lowering_with_an_unwritable_stale_file_is_reported(state_file, monkeypatch):
    pm.set_mode(pm.AUTO)

    def refuse(*_args, **_kwargs):
        # Portable stand-in for a state file that cannot be replaced: on POSIX
        # a read-only file is still replaceable (rename needs directory write).
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(pm.os, "replace", refuse)
    with pytest.raises(pm.ModePersistenceError):
        pm.set_mode(pm.MANUAL)
    assert pm.current_mode() == pm.MANUAL
    monkeypatch.undo()
    # The stale higher mode was removed, so a restart falls back to the default.
    assert _restart() == pm.DEFAULT_MODE


def test_raise_that_cannot_be_written_is_refused_and_rolled_back(state_file, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(pm.os, "replace", refuse)
    with pytest.raises(pm.ModePersistenceError):
        pm.set_mode(pm.AUTO)
    assert pm.current_mode() == pm.DEFAULT_MODE
    with pytest.raises(pm.ModePersistenceError):
        pm.cycle_mode(1)
    assert pm.current_mode() == pm.DEFAULT_MODE


def test_load_treats_a_non_object_state_file_as_the_default(state_file):
    for payload in ("[]", '"auto"', "3", "null"):
        state_file.write_text(payload, encoding="utf-8")
        assert _restart() == pm.DEFAULT_MODE, payload
