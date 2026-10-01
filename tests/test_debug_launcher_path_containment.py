"""Debug run records and cleanup stay in the launcher's private run tree."""

import os
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.debugging import launcher as launcher_module
from sonder_runtime.adapters.debugging.launcher import ProcessDebugLauncher

RUN_ID = "debug-run-" + "a" * 32


def _launcher(root):
    return ProcessDebugLauncher(lambda: None, lambda: None,
                                executable_guard=lambda path: path,
                                run_root=str(root), source=None)


def _symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def test_private_records_and_step_output_keep_their_existing_paths(tmp_path):
    root = tmp_path / "debug-runs"
    rundir = root / RUN_ID
    rundir.mkdir(parents=True)
    launcher = _launcher(root)

    launcher.store_json(RUN_ID, "plan.json", {"kind": "crash"})
    (rundir / "step-0.out").write_bytes(b"Function,Weight\n")

    assert launcher.load_json(RUN_ID, "plan.json") == {"kind": "crash"}
    assert launcher.step_output(RUN_ID, 0) == "Function,Weight\n"
    assert launcher.load_json("debug-run-" + "b" * 32, "plan.json") is None


def test_replaced_run_directory_never_reads_writes_or_scrubs_outside(tmp_path):
    root = tmp_path / "debug-runs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plan.json").write_text('{"secret": true}')
    (outside / "step-0.out").write_text("secret")
    (outside / "staging").mkdir()
    _symlink(root / RUN_ID, outside, directory=True)
    launcher = _launcher(root)

    assert launcher.load_json(RUN_ID, "plan.json") is None
    assert launcher.step_output(RUN_ID, 0) is None
    with pytest.raises(OSError, match="debug run directory is gone"):
        launcher.store_json(RUN_ID, "result.json", {"status": "complete"})
    launcher._scrub(root / RUN_ID, keep_step_outputs=False)
    assert (outside / "staging").is_dir()
    assert (outside / "plan.json").read_text() == '{"secret": true}'
    (root / RUN_ID).unlink()
    _symlink(root / RUN_ID, tmp_path / "absent", directory=True)
    assert launcher.load_json(RUN_ID, "plan.json") is None
    with pytest.raises(OSError, match="debug run directory is gone"):
        launcher.store_json(RUN_ID, "result.json", {"status": "complete"})


def test_replaced_run_root_is_rejected_before_preparation_or_pruning(tmp_path, monkeypatch):
    root = tmp_path / "debug-runs"
    launcher = _launcher(root)
    outside = tmp_path / "outside"
    orphan = outside / RUN_ID / "in"
    orphan.mkdir(parents=True)
    (orphan / "capture.dmp").write_bytes(b"memory")
    _symlink(root, outside, directory=True)
    secured = []
    monkeypatch.setattr(launcher_module, "ensure_private_dir", secured.append)

    with pytest.raises(PermissionError, match="debug run root is a symlink"):
        launcher._prepare(RUN_ID, SimpleNamespace(mkdirs=()))
    assert secured == []  # the link target's permissions were never rewritten
    launcher._prune()
    assert (orphan / "capture.dmp").read_bytes() == b"memory"


def test_a_run_root_swapped_for_a_link_after_construction_is_not_followed(tmp_path):
    root = tmp_path / "debug-runs"
    (root / RUN_ID).mkdir(parents=True)
    launcher = _launcher(root)
    launcher.store_json(RUN_ID, "plan.json", {"kind": "crash"})
    assert launcher.load_json(RUN_ID, "plan.json") == {"kind": "crash"}
    outside = tmp_path / "outside"
    (outside / RUN_ID).mkdir(parents=True)
    (outside / RUN_ID / "plan.json").write_text('{"secret": true}')
    (outside / RUN_ID / "step-0.out").write_text("secret")
    root.rename(tmp_path / "moved")
    _symlink(root, outside, directory=True)

    assert launcher.load_json(RUN_ID, "plan.json") is None
    assert launcher.step_output(RUN_ID, 0) is None
    with pytest.raises(OSError, match="debug run directory is gone"):
        launcher.store_json(RUN_ID, "result.json", {"status": "complete"})
    assert sorted(os.listdir(outside / RUN_ID)) == ["plan.json", "step-0.out"]


def test_a_relative_run_root_keeps_working(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "debug-runs" / RUN_ID / "in").mkdir(parents=True)
    launcher = _launcher("debug-runs")

    launcher.store_json(RUN_ID, "plan.json", {"kind": "crash"})
    assert launcher.load_json(RUN_ID, "plan.json") == {"kind": "crash"}
    launcher.store_json(RUN_ID, "result.json", {"status": "complete"})
    assert sorted(os.listdir(tmp_path / "debug-runs" / RUN_ID)) == ["plan.json", "result.json"]


def test_nested_symlinks_cannot_supply_or_replace_run_records(tmp_path):
    root = tmp_path / "debug-runs"
    rundir = root / RUN_ID
    rundir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}')
    _symlink(rundir / "plan.json", outside)
    launcher = _launcher(root)

    assert launcher.load_json(RUN_ID, "plan.json") is None
    with pytest.raises(PermissionError, match="outside its run directory"):
        launcher.store_json(RUN_ID, "plan.json", {"kind": "crash"})
    assert outside.read_text() == '{"secret": true}'


def test_result_cleanup_unlinks_a_context_link_without_touching_its_target(tmp_path):
    root = tmp_path / "debug-runs"
    rundir = root / RUN_ID
    rundir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("private")
    _symlink(rundir / "context.json", outside)
    launcher = _launcher(root)

    launcher.store_json(RUN_ID, "result.json", {"status": "complete"})

    assert not (rundir / "context.json").is_symlink()
    assert outside.read_text() == "private"


def test_file_output_rejects_escaped_file_and_directory(tmp_path):
    root = tmp_path / "debug-runs"
    rundir = root / RUN_ID
    rundir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cpu.csv").write_text("private")
    _symlink(rundir / "cpu.csv", outside / "cpu.csv")
    _symlink(rundir / "out", outside, directory=True)
    launcher = _launcher(root)
    run = SimpleNamespace(rundir=rundir, run_id=RUN_ID)

    assert launcher._file_output(run, "file:{rundir}/cpu.csv") is None
    assert launcher._file_output(run, "dir:{rundir}/out") is None


def test_file_output_still_reads_regular_file_and_csv_directory(tmp_path):
    root = tmp_path / "debug-runs"
    rundir = root / RUN_ID
    output = rundir / "out"
    output.mkdir(parents=True)
    (output / "cpu.csv").write_text("Function,Weight\nmain,10\n")
    launcher = _launcher(root)
    run = SimpleNamespace(rundir=rundir, run_id=RUN_ID)

    assert launcher._file_output(run, "file:{rundir}/out/cpu.csv") == "Function,Weight\nmain,10\n"
    assert launcher._file_output(run, "dir:{rundir}/out") == "Function,Weight\nmain,10\n"
