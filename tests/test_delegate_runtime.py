"""Focused contracts for chat delegation into durable agent lanes."""

from pathlib import Path
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.interfaces.http.delegate import dispatch_delegate


class _Model:
    def generate(self, request, context):
        return ModelResponse("done", "fake", request.tier, tokens_out=1)


def _service(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    lanes = AgentLaneService(store, sessions, _Model(), auto_start=False)
    context = local_owner_context(
        correlation_id="delegate-test", workspace_roots=(tmp_path,)
    )
    return lanes, context


def test_delegate_uses_isolated_state_home_creation_folder(monkeypatch, tmp_path):
    lanes, context = _service(tmp_path)
    state_home = tmp_path / "state"
    receipt = dispatch_delegate(
        lanes,
        {"task": "write primes.py", "command_id": "delegate-test-1"},
        context,
        state_home=state_home,
        allow_creation=True,
    )

    lane = receipt["lane"]
    assert Path(lane["workspace_root"]) == state_home / "creations" / lane["id"]
    assert receipt["delegation"]["folder_kind"] == "creation"
    assert receipt["delegation"]["open"] == {
        "surface": "agents",
        "lane_id": lane["id"],
    }
    assert lane["id"] in receipt["delegation"]["acknowledgement"]
    assert lane["status"] == "queued"


def test_delegate_respects_selected_project_and_runs_normal_lane_loop(tmp_path):
    lanes, context = _service(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    parent = lanes.open_model_parent(context)["parent_session_id"]

    receipt = dispatch_delegate(
        lanes,
        {
            "task": "inspect project",
            "command_id": "delegate-test-2",
            "parent_session_id": parent,
            "project": str(project),
        },
        context,
        state_home=tmp_path / "state",
    )
    lane_id = receipt["lane"]["id"]
    assert Path(receipt["delegation"]["folder"]) == project.resolve()
    lanes.run_pending(lane_id, context)
    assert lanes.inspect(lane_id, context)["lane"]["status"] == "completed"


def test_delegate_rejects_project_outside_inherited_workspace(tmp_path):
    lanes, context = _service(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    try:
        try:
            dispatch_delegate(
                lanes,
                {"task": "inspect", "project": str(outside)},
                context,
                state_home=tmp_path / "state",
            )
        except PermissionError as exc:
            assert "outside" in str(exc)
        else:
            raise AssertionError("outside project was accepted")
    finally:
        outside.rmdir()


def test_an_outside_project_is_refused_alike_whether_or_not_it_exists(tmp_path):
    # Probing a caller-named path before the containment check made the two
    # refusals an existence oracle for any folder on the machine: "must be an
    # existing directory" for an absent one, "outside" for a present one.
    lanes, context = _service(tmp_path)
    present = tmp_path.parent / (tmp_path.name + "-present")
    absent = tmp_path.parent / (tmp_path.name + "-absent")
    present.mkdir()
    try:
        refusals = []
        for folder in (present, absent, tmp_path / ".." / absent.name):
            with pytest.raises(PermissionError) as refused:
                dispatch_delegate(
                    lanes,
                    {"task": "inspect", "project": str(folder)},
                    context,
                    state_home=tmp_path / "state",
                )
            refusals.append(str(refused.value))
        assert len(set(refusals)) == 1 and "outside" in refusals[0]
    finally:
        present.rmdir()


def test_delegate_rejects_malformed_project_without_creating_creation_folder(tmp_path):
    lanes, context = _service(tmp_path)
    state_home = tmp_path / "state"
    try:
        dispatch_delegate(
            lanes,
            {"task": "inspect", "project": {"path": "bad"}},
            context,
            state_home=state_home,
        )
    except ValueError as exc:
        assert "project" in str(exc)
    else:
        raise AssertionError("malformed project was accepted")
    assert not state_home.exists()


def test_creation_retry_reuses_parent_lane_and_folder_without_session(tmp_path):
    lanes, context = _service(tmp_path)
    payload = {"task": "write primes.py", "command_id": "retry-1"}
    first = dispatch_delegate(lanes, payload, context, state_home=tmp_path / "state", allow_creation=True)
    second = dispatch_delegate(lanes, payload, context, state_home=tmp_path / "state", allow_creation=True)
    assert second == first
    assert len(lanes.list(context)["lanes"]) == 1
    with pytest.raises(ValueError):
        dispatch_delegate(lanes, {**payload, "task": "different task"}, context,
                          state_home=tmp_path / "state", allow_creation=True)
    assert Path(first["lane"]["workspace_root"]).is_dir()


@pytest.mark.parametrize("changes", [
    {"task": ""}, {"task": None}, {"task": "a" * 12001}, {"command_id": ""},
    {"max_steps": 0}, {"max_steps": True}, {"max_wall_seconds": 601},
    {"parent_session_id": "x" * 161}, {"tier": ""}, {"title": "x" * 161},
])
def test_invalid_requests_have_no_creation_or_lane_side_effects(tmp_path, changes):
    lanes, context = _service(tmp_path)
    home = tmp_path / "state"
    with pytest.raises(ValueError):
        dispatch_delegate(lanes, {"task": "write file", **changes}, context,
                          state_home=home, allow_creation=True)
    assert not home.exists()
    assert not lanes.list(context)["lanes"]


def test_unprivileged_account_cannot_get_creation_authority(tmp_path):
    lanes, context = _service(tmp_path)
    context = replace(context, principal_id="account:reader", workspace_roots=())
    home = tmp_path / "state"
    with pytest.raises(PermissionError, match="administrator"):
        dispatch_delegate(lanes, {"task": "write file"}, context, state_home=home)
    assert not home.exists()


def test_cancelled_context_cannot_create_a_folder(tmp_path):
    lanes, context = _service(tmp_path)
    context = replace(context, cancellation=SimpleNamespace(cancelled=True))
    with pytest.raises(PermissionError):
        dispatch_delegate(lanes, {"task": "write file"}, context,
                          state_home=tmp_path / "state", allow_creation=True)
    assert not (tmp_path / "state").exists()


def test_newest_lane_pages_preserve_legacy_default_and_finished_lanes(tmp_path):
    lanes, context = _service(tmp_path)
    receipts = [dispatch_delegate(lanes, {"task": "task", "command_id": f"command-{i}"}, context,
                                 state_home=tmp_path / "state", allow_creation=True) for i in range(3)]
    ids = [receipt["lane"]["id"] for receipt in receipts]
    lanes.run_pending(ids[-1], context)
    assert [lane["id"] for lane in lanes.list(context)["lanes"]] == ids
    page = lanes.list(context, limit=2, newest_first=True)
    assert [lane["id"] for lane in page["lanes"]] == ids[::-1][:2]
    assert page["lanes"][0]["status"] == "completed"
    assert page["has_more"]
    assert [lane["id"] for lane in lanes.list(context, cursor=page["next_cursor"], newest_first=True)["lanes"]] == ids[:1]


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("mode", ["acceptEdits", "manual"])
def test_composed_lane_writes_file_with_scripted_model(tmp_path, monkeypatch, selected, mode):
    """Exercise the real lane graph, live grant authorizer, permissions and file executor."""
    from sonder_runtime.bootstrap.app import build_application
    import permission_modes
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_FLEET_DB", str(home / "fleet.db"))
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(home / "sessions.db"))
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))
    monkeypatch.setattr(permission_modes, "_LOADED", True)
    monkeypatch.setitem(permission_modes._STATE, "mode", mode)
    monkeypatch.setattr(permission_modes, "_rule_lookup", lambda name: None)
    application = build_application(config=SonderConfig(state=StateConfig(home=str(home), workspace_roots=(str(project),))))
    lanes = application.agent_lanes()
    lanes._pool.shutdown(wait=True)
    lanes._pool = None
    replies = iter([json.dumps({"tool": "write_file", "arguments": {
        "path": "primes.py", "content": "print([2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71])\n",
    }}), "Saved primes.py"])
    lanes.gateway = SimpleNamespace(generate=lambda req, ctx: ModelResponse(next(replies), "scripted", "code", tokens_out=10))
    context = local_owner_context(correlation_id="scripted-delegation", workspace_roots=(project,))
    receipt = dispatch_delegate(lanes, {"task": "write a python script that prints the first 20 primes and save it as primes.py",
                                       "project": str(project) if selected else ""}, context,
                                state_home=home, allow_creation=True)
    root = Path(receipt["lane"]["workspace_root"])
    # Scheduled workers receive the host-issued exact creation root. Reproduce
    # that same context while driving the service deterministically in this test.
    effective = context if selected else replace(context, workspace_roots=(project, root))
    try:
        lanes.run_pending(receipt["lane"]["id"], effective)
        result = lanes.inspect(receipt["lane"]["id"], effective)
        assert (root / "primes.py").exists() == (mode == "acceptEdits"), result
        if mode == "acceptEdits":
            assert (root / "primes.py").read_text(encoding="utf-8").startswith("print([2, 3, 5")
        assert result["lane"]["status"] == ("completed" if mode == "acceptEdits" else "awaiting_input")
    finally:
        lanes.close()


def test_creation_grant_requires_exact_original_root_and_current_lane_id(tmp_path):
    from sonder_runtime.application.agents.delegation import is_creation_workspace
    root = tmp_path / "creations" / ("lane-" + "a" * 32)
    root.mkdir(parents=True)
    lane = {"id": root.name, "workspace_root": str(root)}
    context = local_owner_context(correlation_id="grant-check", workspace_roots=(tmp_path,))
    assert not is_creation_workspace(lane, context, tmp_path)
    exact = replace(context, workspace_roots=(root,))
    assert is_creation_workspace(lane, exact, tmp_path)
    assert not is_creation_workspace({**lane, "id": "lane-" + "b" * 32}, exact, tmp_path)
