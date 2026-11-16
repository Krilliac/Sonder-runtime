"""``/runtime tools``: console edits of the model tool overlay, and every way they are refused.

Writes happen only with the console operator attending (``control_command``
gets ``operator_approved`` only from the REPL); every other surface, and every
MCP tool parameter, leaves the overlay file untouched.
"""
from __future__ import annotations

import inspect
import json

import pytest

import server
from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters import model_tool_policy_editor as editor
from sonder_runtime.adapters import restricted_tool_lane as lane
from sonder_runtime.domain.model_tool_grants import ModelToolStatus
from tests.support.tool_lane import ALLOWED, DENIED, DENIED_SMALL, clear_environment

FLOOR_SELECTOR = "astra-cyber-*"  # the security-advisers floor's own prefix


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    (tmp_path / "overlay").mkdir()
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(tmp_path / "overlay" / "model_tool_policy.json"))
    monkeypatch.setattr(server, "_RUNTIME_POLICY", server._RUNTIME_POLICY)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    server._refresh_runtime_policy(create=False)
    mtp.clear_cache()
    yield
    mtp.clear_cache()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "overlay" / "model_tool_policy.json"


def attended(text):
    return editor.command(text, operator_attended=True, policy=server._RUNTIME_POLICY)


def status(model):
    mtp.clear_cache()
    return lane.decide(model, server._RUNTIME_POLICY)


def snapshot(path):
    return path.read_bytes() if path.exists() else None


# ---- attended writes round-trip ------------------------------------------------

def test_allow_then_revoke_round_trips_through_the_file(path):
    assert status(DENIED).status is ModelToolStatus.DENIED
    # A name under the floor's prefix cannot be granted alone: the floor grants nothing.
    out = attended("allow %s text_search" % DENIED)
    assert out.startswith("refused /runtime tools allow:") and "security-advisers" in out, out
    assert "allow astra-cyber-* text_search" in out and not path.exists()
    out = attended("allow %s text_search" % FLOOR_SELECTOR)
    assert out.startswith("granted text_search to %s in lane agent" % FLOOR_SELECTOR), out
    assert "RESTRICTED to text_search in agent" in out
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["model_profiles"] == [
        {"id": "security-advisers", "tools": ["text_search"], "lanes": ["agent"]},
    ]
    decision = status(DENIED)
    assert decision.status is ModelToolStatus.RESTRICTED
    assert decision.tools == frozenset({"text_search"}) and decision.permits("agent")
    assert not decision.permits("workbench_agent")
    assert "OVERLAY" in editor.show(server._RUNTIME_POLICY).upper()

    out = attended("allow %s directory_tree workbench_agent" % FLOOR_SELECTOR)
    assert "granted directory_tree" in out
    assert status(DENIED).permits("workbench_agent")

    attended("revoke %s text_search" % FLOOR_SELECTOR)
    attended("revoke %s directory_tree" % FLOOR_SELECTOR)
    assert status(DENIED).status is ModelToolStatus.DENIED
    assert json.loads(path.read_text(encoding="utf-8"))["model_profiles"] == []
    # The shipped floor still lists the model after the extension is gone.
    assert "astra-cyber-" in mtp.load().profile("security-advisers").values("prefixes")


def test_allow_for_an_unlisted_model_creates_a_grant_profile_and_revoke_removes_it(path):
    out = attended("allow %s file_read_range" % ALLOWED)
    assert "granted file_read_range" in out
    entry = json.loads(path.read_text(encoding="utf-8"))["model_profiles"][0]
    assert entry["id"].startswith("grant-") and entry["match"] == {"names": [ALLOWED]}
    assert status(ALLOWED).status is ModelToolStatus.RESTRICTED  # listed, so restricted
    attended("revoke %s file_read_range" % ALLOWED)
    assert json.loads(path.read_text(encoding="utf-8"))["model_profiles"] == []
    assert status(ALLOWED).status is ModelToolStatus.ALLOWED


def test_deny_beats_a_grant_and_undeny_restores_it(path):
    attended("allow %s text_search" % FLOOR_SELECTOR)
    out = attended("deny %s" % DENIED)
    assert out.startswith("denied %s" % DENIED), out
    assert status(DENIED).status is ModelToolStatus.DENIED
    assert "already denied" in attended("deny %s" % DENIED)
    out = attended("undeny %s" % DENIED)
    assert out.startswith("removed the overlay deny entry"), out
    assert status(DENIED).status is ModelToolStatus.RESTRICTED


def test_deny_and_undeny_a_prefix(path):
    out = attended("deny other-model*")
    assert "denied other-model*" in out
    assert status("other-model:7b").status is ModelToolStatus.DENIED
    attended("undeny other-model*")
    assert status("other-model:7b").status is ModelToolStatus.ALLOWED


# ---- refusals ------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["web_fetch", "run_code", "file_write", "memory_search", "find_references", "nope"])
def test_allow_refuses_a_tool_outside_the_ceiling_or_predicate(path, tool):
    out = attended("allow %s %s" % (DENIED, tool))
    assert out.startswith("refused /runtime tools allow: tool %r cannot be granted" % tool), out
    assert not path.exists()


def test_allow_refuses_an_unknown_lane_and_a_bad_selector(path):
    assert "is not a restricted lane" in attended("allow %s text_search chat" % DENIED)
    assert "is not a model name" in attended("allow a*b text_search")
    assert "is not a model name" in attended("allow * text_search")
    assert not path.exists()


def test_floor_and_shipped_entries_cannot_be_removed(path):
    out = attended("undeny %s" % FLOOR_SELECTOR)
    assert out.startswith("refused /runtime tools undeny:") and "shipped entry" in out and "floor" in out
    out = attended("undeny %s" % DENIED)
    assert out.startswith("refused /runtime tools undeny:") and "security-advisers" in out, out
    out = attended("revoke %s text_search" % DENIED)
    assert out.startswith("refused /runtime tools revoke:"), out
    assert not path.exists()


def test_undeny_refuses_a_legacy_entry(path, tmp_path):
    (tmp_path / "runtime_policy.json").write_text(json.dumps({
        "tool_lane_denied_models": {"names": [ALLOWED]},
    }), encoding="utf-8")
    server._refresh_runtime_policy(create=False)
    out = attended("undeny %s" % ALLOWED)
    assert "deprecated tool_lane_denied_models" in out
    assert not path.exists()


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps({"version": 1, "restricted_tool_ceiling": ["web_fetch"]}),
    json.dumps({"version": 1, "model_profiles": [{"id": "security-advisers", "floor": True}]}),
    json.dumps({"version": 2}),
])
def test_an_unreadable_or_invalid_overlay_is_never_overwritten(path, content):
    path.write_text(content, encoding="utf-8")
    before = snapshot(path)
    out = attended("allow %s text_search" % FLOOR_SELECTOR)
    assert out.startswith("refused /runtime tools allow:") and "It was not overwritten" in out, out
    assert snapshot(path) == before


def test_writes_go_through_the_lock_and_the_atomic_writer(path, monkeypatch):
    seen = []
    real_lock, real_write = editor._atomic_json.file_lock, editor._atomic_json.write_json_atomic

    def lock(target, *a, **k):
        seen.append(("lock", target))
        return real_lock(target, *a, **k)

    def write(target, document, *a, **k):
        seen.append(("write", target))
        return real_write(target, document, *a, **k)

    monkeypatch.setattr(editor._atomic_json, "file_lock", lock)
    monkeypatch.setattr(editor._atomic_json, "write_json_atomic", write)
    attended("allow %s text_search" % FLOOR_SELECTOR)
    assert seen == [("lock", path), ("write", path)]


# ---- unattended surfaces are refused --------------------------------------------

@pytest.mark.parametrize("action", [
    "allow %s text_search" % FLOOR_SELECTOR, "revoke %s text_search" % FLOOR_SELECTOR, "deny %s" % DENIED_SMALL,
    "undeny %s" % DENIED,
])
def test_an_unattended_command_is_refused_and_the_file_unchanged(path, action):
    path.write_text(json.dumps({"version": 1, "model_profiles": []}), encoding="utf-8")
    before = snapshot(path)
    for attending in (False, None, 1, "yes"):
        out = editor.command(action, operator_attended=attending, policy=server._RUNTIME_POLICY)
        assert "edited only at the console with the operator present" in out
    assert snapshot(path) == before


def test_an_unattended_control_command_runtime_tools_allow_is_refused(path, monkeypatch):
    before = snapshot(path)
    out = server.control_command("/runtime tools allow %s text_search" % FLOOR_SELECTOR)
    assert out.startswith("refused /runtime"), out
    assert snapshot(path) == before
    # Even with the chain's permission gate out of the way (an allow rule, a
    # permissive mode), the editor itself refuses without the operator.
    monkeypatch.setattr(server, "_control_tool_refusal", lambda *a, **k: None)
    out = server.control_command("/runtime tools allow %s text_search" % FLOOR_SELECTOR)
    assert "edited only at the console with the operator present" in out, out
    assert snapshot(path) == before
    assert status(DENIED).status is ModelToolStatus.DENIED
    # The chat path (answer_with_history, which the MCP sonder tool reaches) too.
    answer = server.answer_with_history("/runtime tools allow %s text_search" % FLOOR_SELECTOR, [])
    assert "refused" in str(answer).lower()
    assert snapshot(path) == before
    # The chat path a model can reach forwards no approval either.
    out = server.control_command("/runtime tools deny %s" % ALLOWED, operator_approved=False)
    assert "refused" in out.lower()
    assert snapshot(path) == before


def test_an_attended_control_command_writes(path):
    out = server.control_command("/runtime tools allow %s text_search" % FLOOR_SELECTOR, operator_approved=True)
    assert "granted text_search" in out, out
    assert status(DENIED).status is ModelToolStatus.RESTRICTED


def test_the_http_slash_surface_classes_tools_writes_as_a_policy_change():
    from sonder_runtime.interfaces.http import serve

    for action in ("allow", "revoke", "deny", "undeny"):
        assert serve._slash_system_operation("/runtime", "tools %s x y" % action) == "runtime_policy_change"


def test_no_mcp_tool_parameter_writes_the_overlay(path, monkeypatch):
    """No registered tool takes a parameter that edits model tool grants, and
    runtime_policy_update leaves the overlay alone whatever it is given."""
    registered = server.mcp._tool_manager.list_tools() if hasattr(server.mcp, "_tool_manager") else []
    assert registered, "no registered tools found: this check would check nothing"
    words = ("model_tool", "model_profile", "tool_grant", "restricted_tool", "tool_ceiling", "secret_path")
    offenders = []
    for tool in registered:
        fn = getattr(tool, "fn", None)
        names = list(inspect.signature(fn).parameters) if fn is not None else []
        offenders += ["%s(%s)" % (tool.name, name) for name in names if any(word in name for word in words)]
    assert offenders == []
    assert "runtime_tools" not in {tool.name for tool in registered}
    before = snapshot(path)
    server.runtime_policy_update(local_models_json=json.dumps({"model_profiles": [
        {"id": "x", "match": {"names": [ALLOWED]}, "tools": ["text_search"], "lanes": ["agent"]},
    ]}))
    server.runtime_policy_update(routing_json=json.dumps({"tools": ["text_search"]}))
    assert snapshot(path) == before
    assert status(ALLOWED).status is ModelToolStatus.ALLOWED


def test_show_needs_no_operator_and_writes_nothing(path):
    out = editor.command("", operator_attended=False, policy=server._RUNTIME_POLICY)
    assert "security-advisers" in out and "ceiling" in out.lower()
    assert not path.exists()
