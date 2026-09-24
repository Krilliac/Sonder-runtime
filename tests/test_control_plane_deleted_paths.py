import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.security import control_plane_paths
from sonder_runtime.adapters.security.control_plane_paths import (
    ControlPlaneInventory,
    ControlPlanePaths,
)

TOMBSTONES = (
    Path(r"C:\$Extend\$Deleted\0054000000A329127FA5F259"),
    Path(r"C:\$extend\$deleted\0054000000A329127FA5F259"),
    Path(r"\\?\C:\$Extend\$Deleted\0054000000A329127FA5F259"),
)


def _controlled_resolve(monkeypatch, values):
    calls = []
    sequence = iter(values)

    def resolve(path, *args, **kwargs):
        calls.append(path)
        return next(sequence)

    monkeypatch.setattr(control_plane_paths.Path, "resolve", resolve)
    return calls


@pytest.mark.skipif(os.name != "nt", reason="Windows tombstone namespace")
@pytest.mark.parametrize("tombstone", TOMBSTONES)
def test_windows_deleted_final_path_retries_to_ordinary_target(
    monkeypatch, tmp_path, tombstone
):
    ordinary = tmp_path / "private" / "fleet.db"
    calls = _controlled_resolve(monkeypatch, (tombstone, ordinary))

    assert control_plane_paths._canonical(Path(r"C:\private\fleet.db")) == ordinary
    assert len(calls) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows tombstone namespace")
def test_windows_deleted_final_path_outside_target_does_not_cover(monkeypatch, tmp_path):
    ordinary = tmp_path / "private" / "fleet.db"
    outside = tmp_path / "outside" / "fleet.db"
    inventory = ControlPlaneInventory(frozenset({ordinary}), (), (), (), ())
    paths = ControlPlanePaths(files=(ordinary,))
    calls = _controlled_resolve(monkeypatch, (TOMBSTONES[0], outside))

    assert not inventory.covers(paths)
    assert len(calls) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows tombstone namespace")
def test_windows_persistent_deleted_final_path_fails_closed(monkeypatch):
    calls = _controlled_resolve(monkeypatch, (TOMBSTONES[0],) * 3)

    with pytest.raises(ValueError, match="private path resolution is unstable"):
        control_plane_paths._canonical(Path(r"C:\private\fleet.db"))
    assert len(calls) == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows nested namespace case")
def test_windows_nested_deleted_namespace_is_not_special(monkeypatch, tmp_path):
    ordinary = tmp_path / "private" / "$Extend" / "$Deleted" / "fleet.db"
    calls = _controlled_resolve(monkeypatch, (ordinary,))

    assert control_plane_paths._canonical(ordinary) == ordinary
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC namespace case")
def test_windows_share_root_is_not_assumed_to_be_volume_root(monkeypatch):
    ordinary = Path(r"\\server\share\$Extend\$Deleted\fleet.db")
    calls = _controlled_resolve(monkeypatch, (ordinary,))

    assert control_plane_paths._canonical(ordinary) == ordinary
    assert len(calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX literal namespace case")
def test_posix_literal_deleted_namespace_is_not_special(monkeypatch, tmp_path):
    ordinary = tmp_path / "$Extend" / "$Deleted" / "fleet.db"
    calls = _controlled_resolve(monkeypatch, (ordinary,))

    assert control_plane_paths._canonical(ordinary) == ordinary
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows tombstone namespace")
def test_transient_deleted_final_path_stays_covered_and_protected(monkeypatch, tmp_path):
    ordinary = tmp_path / "private" / "fleet.db"
    calls = _controlled_resolve(
        monkeypatch, (TOMBSTONES[0], ordinary, ordinary, ordinary)
    )

    paths = ControlPlanePaths(files=(Path(r"C:\private\fleet.db"),))
    inventory = ControlPlaneInventory(frozenset({ordinary}), (), (), (), ())

    assert inventory.covers(paths)
    assert inventory.protects(ordinary)
    assert len(calls) == 4
