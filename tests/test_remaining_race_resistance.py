import os
from pathlib import Path

import pytest

from sonder_runtime.application.security.race_resistant_paths import (
    DestructiveTargetLimits,
    PlatformCapabilityError,
    RaceResistanceError,
    build_open_intent,
    check_destructive_targets,
    platform_path_capabilities,
    resolve_authorized_root,
)


def test_capability_report_is_truthful_and_fail_closed():
    report = platform_path_capabilities()
    assert report.platform == os.name
    assert report.fail_closed is True
    assert report.race_resistant_destructive_ops == (
        os.name == "posix" and report.directory_handles and report.no_follow
    )
    assert report.reason


def test_resolution_rejects_symlink_component_and_escape(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "safe.txt").write_text("ok", encoding="utf-8")
    assert resolve_authorized_root(root / "safe.txt", [root]).relative_parts == ("safe.txt",)
    link = root / "link"
    try:
        link.symlink_to(tmp_path / "outside", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(RaceResistanceError):
        resolve_authorized_root(link / "file.txt", [root])
    with pytest.raises(RaceResistanceError):
        resolve_authorized_root(root / ".." / "outside", [root])


def test_destructive_open_intent_fails_closed_on_windows(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("x", encoding="utf-8")
    read_intent = build_open_intent(target, [root], "read")
    assert read_intent.destructive is False
    assert read_intent.no_follow == bool(getattr(os, "O_NOFOLLOW", 0))
    if os.name == "nt":
        with pytest.raises(PlatformCapabilityError):
            build_open_intent(target, [root], "delete")
    else:
        delete_intent = build_open_intent(target, [root], "delete")
        assert delete_intent.destructive is True


def test_destructive_targets_are_bounded_before_capability_gate(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(RaceResistanceError, match="duplicate"):
        check_destructive_targets([target, target], [root])
    with pytest.raises(RaceResistanceError, match="root deletion"):
        check_destructive_targets([root], [root])
    second = root / "second.txt"
    second.write_text("y", encoding="utf-8")
    with pytest.raises(RaceResistanceError, match="count"):
        check_destructive_targets([target, second], [root], DestructiveTargetLimits(max_targets=1))
