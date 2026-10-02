"""The durable run, worker, status text and app payload share one safe root."""
import json
import os
from pathlib import Path

import pytest

import autopilot_controller
import server
from sonder_runtime.adapters.persistence import autopilot_store
from sonder_runtime.platform import paths


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(home / "autopilot.db"))
    paths.reset_home()
    autopilot_store.reset_schema_cache_for_tests()
    monkeypatch.setattr(server, "_launch_autopilot", lambda *a, **k: True)
    yield home
    autopilot_store.reset_schema_cache_for_tests()


@pytest.mark.parametrize("project", ["", "default", "unresolved-project"])
def test_start_persists_and_displays_created_workspace(monkeypatch, tmp_path, isolated, project):
    source = tmp_path / "sonder-checkout"
    (source / ".git").mkdir(parents=True)
    (source / "sonder_runtime").mkdir()
    monkeypatch.chdir(source)
    output = server._autopilot_start("create a self-contained page", project=project)
    run = autopilot_store.get_run()
    expected = (isolated / "creations" / run["id"]).resolve()
    assert expected.is_dir()
    assert Path(run["project"]) == expected
    assert not expected.is_relative_to(source)
    assert "working in: " + str(expected) in output
    assert "working in: " + str(expected) in server._autopilot_status(run["id"])
    assert "working in: " + str(expected) in autopilot_controller.format_report(run)
    # The app consumes this unchanged project field in AutopilotRun.
    assert autopilot_store.get_run(run["id"])["project"] == str(expected)


def test_explicit_project_unchanged_and_worker_uses_saved_root(monkeypatch, tmp_path, isolated):
    project = tmp_path / "chosen"
    project.mkdir()
    server._autopilot_start("write a page", project=str(project))
    run = autopilot_store.get_run()
    assert run["project"] == str(project)
    captured = {}

    def agent(*args, **kwargs):
        captured.update(kwargs)
        return autopilot_controller.HostTaskResult(output="done", project_scope=kwargs["project"])

    monkeypatch.setattr(server, "_agent_impl", agent)
    server._autopilot_work_model(run, {"id": "t1", "kind": "implement"}, "")
    assert captured["project"] == str(project)
    assert not (isolated / "creations").exists()


def test_observe_namespace_and_existing_project_are_unchanged(isolated):
    run = autopilot_store.create_run("inspect only", project="demo", policy="observe")
    assert run["project"] == "demo"
    assert not (isolated / "creations").exists()


def test_default_workspace_survives_store_reload(isolated):
    run = autopilot_store.create_run("write an artifact")
    autopilot_store.reset_schema_cache_for_tests()
    recovered = autopilot_store.get_run(run["id"])
    assert recovered["project"] == run["project"]
    assert Path(recovered["project"]).is_dir()


def test_project_less_internal_loop_keeps_its_own_root(monkeypatch, tmp_path, isolated):
    """The selfmod editor passes no project and its policy refuses host extra_roots.

    Binding an omitted project to a creations folder inside the loop injected
    ``extra_roots`` into every file call, so the editor could touch nothing.
    """
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    target = str(workspace / "a.py")
    run = {"workspace_path": str(workspace), "files": ["a.py"], "budgets": {
        "max_tool_calls": 20, "max_runtime_seconds": 600, "max_files_inspected": 50}}
    replies = [json.dumps({"tool": "file_read", "args": {"path": target}})]
    dispatched = []
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: (
        lambda *a, **k: replies.pop(0) if replies else '{"final":"done"}'))
    monkeypatch.setattr(server, "_agent_dispatch_observed", lambda tool, args, **k: (
        dispatched.append((tool, dict(args))) or "x = 1"))
    server._agent_impl(
        "edit the candidate", max_steps=3, allow_web=False, auto_checklist=True,
        tool_allowlist={"file_read", "file_edit"}, tool_policy=server._selfmod_agent_policy(run),
    )
    assert dispatched == [("file_read", {"path": target})]
    assert not (isolated / "creations").exists()


# Windows refuses to create a directory whose path is 248 characters or longer
# (MAX_PATH minus room for an 8.3 name) unless the host sets LongPathsEnabled.
# Node1 does not, and pytest's temp base there is already ~90 characters, so a
# fixed 168-character nesting overflowed (WinError 3 at the home's mkdir). The
# test only needs a project path over 200 characters: size the nesting to land
# it at _PROJECT_LENGTH under any base. Where the base itself leaves no room,
# fall back to the original nesting on hosts that can create it, else skip.
_PROJECT_LENGTH = 225
_WINDOWS_DIRECTORY_LIMIT = 247
_RUN_TAIL = len(os.sep + "creations" + os.sep) + len("auto-") + 12  # <home>/creations/<run-id>


def _long_paths_supported() -> bool:
    if os.name != "nt":
        return True
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def _nested_home(base: Path, tail: int) -> tuple[Path, bool]:
    """``base`` plus nesting so that the home and a ``tail`` suffix total _PROJECT_LENGTH."""
    base = base.resolve()
    room = _PROJECT_LENGTH - tail - len(str(base)) - 1
    if room < 1:
        if not _long_paths_supported():
            pytest.skip(
                "temp base is %d characters: no project path over 200 characters fits "
                "under Windows' %d-character directory limit without LongPathsEnabled"
                % (len(str(base)), _WINDOWS_DIRECTORY_LIMIT)
            )
        return base / ("nested-" * 12) / ("nested-" * 12), False
    count = -(-room // 84)  # components of at most 84 characters, far below 255
    width, extra = divmod(room - (count - 1), count)
    return base.joinpath(*("n" * (width + (index < extra)) for index in range(count))), True


def test_long_default_workspace_is_not_truncated(monkeypatch, isolated):
    home, sized = _nested_home(isolated, _RUN_TAIL)
    monkeypatch.setattr(paths, "default_home", lambda: home)
    run = autopilot_store.create_run("write an artifact")
    assert len(run["project"]) > 200
    if sized:
        # Pin the Windows-safe length everywhere: CI runs this test only on
        # Linux, where nothing else would notice the path creeping past the
        # limit (a larger _PROJECT_LENGTH, or a longer run id or folder name).
        assert len(run["project"]) == _PROJECT_LENGTH <= _WINDOWS_DIRECTORY_LIMIT
    assert Path(run["project"]) == (home / "creations" / run["id"]).resolve()
    assert Path(run["project"]).is_dir()
