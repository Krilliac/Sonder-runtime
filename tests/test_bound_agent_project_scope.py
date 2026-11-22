"""A project-bound agent's tools and the project boundary, through the dispatcher.

``memory_search`` is confined to the host-selected project (see
``test_memory_search_project_scope``).  ``preferences_status`` is also in
``_PROJECT_BOUND_AGENT_TOOLS`` and listed every project's preferences; a bound
run now lists ``global`` plus its own project's scopes, with the limit counting
only those rows.  The root comes from the host, never a model argument.

The remaining tests pin what must not change: the operator's direct MCP calls
still see every project, a bound agent can still work on its own project's
tasks, and the audited aggregate tools carry no other project's records.
"""
from __future__ import annotations

import re

import pytest

import memory_store
import server

FOREIGN = "ZULUFOREIGN"
OWN = "ALPHAOWN"


@pytest.fixture
def two_projects(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    project_a = tmp_path / "proj_a"
    project_b = tmp_path / "proj_b"
    project_a.mkdir()
    project_b.mkdir()
    a, b = str(project_a), str(project_b)
    conn = server._open_db()
    try:
        memory_store.add_fact(conn, "FACTB", b, FOREIGN + " fact", None)
        memory_store.touch_session(conn, "SESSB", b)
        memory_store.set_session_title(conn, "SESSB", FOREIGN + " session title")
        memory_store.log_interaction(
            conn, "INTB", FOREIGN + " task", "", "ok", "code",
            session_id="SESSB", project=b,
        )
        memory_store.update_session_project_summary(
            conn, "SESSB", b, FOREIGN + " summary", "INTB",
        )
        memory_store.touch_session(conn, "SESSA", a)
        memory_store.set_session_title(conn, "SESSA", OWN + " session title")
        memory_store.log_interaction(
            conn, "INTA", OWN + " task", "", "ok", "code",
            session_id="SESSA", project=a,
        )
        memory_store.upsert_preference(conn, "PREFG", "global", "kg", "global pref text")
        memory_store.upsert_preference(conn, "PREFA", "project:" + a, "ka", OWN + " pref")
        memory_store.upsert_preference(conn, "PREFABARE", a, "kab", OWN + " bare pref")
        memory_store.upsert_preference(conn, "PREFB", "project:" + b, "kb", FOREIGN + " pref")
        memory_store.upsert_preference(conn, "PREFBBARE", b, "kbb", FOREIGN + " bare pref")
        # Sibling and child spellings of the bound root are other projects.
        memory_store.upsert_preference(
            conn, "PREFNEAR", "project:" + a + "b", "kn", FOREIGN + " sibling pref",
        )
        memory_store.upsert_preference(
            conn, "PREFSUB", "project:" + a + "\\sub", "ks", FOREIGN + " child pref",
        )
        task_b = memory_store.create_task(conn, FOREIGN + " task row", project=b)
        task_a = memory_store.create_task(conn, OWN + " task row", project=a)
    finally:
        conn.close()
    checklist_b = _checklist_id(
        server.checklist_create(FOREIGN + " checklist", '["one", "two"]', project=b)
    )
    checklist_a = _checklist_id(
        server.checklist_create(OWN + " checklist", '["one", "two"]', project=a)
    )
    snapshot = {
        "active_agents": 2,
        "agents": [
            {"id": "worker-own", "status": "running", "activity": "reading",
             "task": OWN + " agent task", "project": a, "role": "worker"},
            {"id": "worker-foreign", "status": "running", "activity": "reading",
             "task": FOREIGN + " agent task", "project": b, "role": "worker"},
            {"id": "worker-sibling", "status": "running", "activity": "reading",
             "task": FOREIGN + " sibling agent", "project": a + "b", "role": "worker"},
        ],
        "latest_master_result": FOREIGN + " master result",
        "latest_master": {"id": "master-foreign", "task": FOREIGN, "project": b},
    }
    monkeypatch.setattr(
        server.master_orchestrator, "snapshot",
        lambda include_finished=True, limit=20: {
            **snapshot, "agents": [dict(row) for row in snapshot["agents"]],
        },
    )
    return {
        "a": a, "b": b, "task_a": task_a["id"], "task_b": task_b["id"],
        "checklist_a": checklist_a, "checklist_b": checklist_b,
    }


def _checklist_id(output):
    match = re.search(r"sonder checklist ([0-9a-f]+)", output)
    assert match, output
    return match.group(1)


def _assert_no_foreign(out, ctx):
    assert FOREIGN not in out, out
    assert "proj_b" not in out, out
    assert "SESSB" not in out, out
    assert ctx["task_b"][:8] not in out and ctx["checklist_b"][:8] not in out, out


def _bound(ctx, tool, args, read_only=False):
    return server._agent_dispatch(
        tool, args, read_only=read_only, repository_extra_roots=ctx["a"],
    )


# -- preferences_status ------------------------------------------------------

@pytest.mark.parametrize("read_only", [False, True])
def test_preferences_status_lists_only_global_and_own_project(two_projects, read_only):
    ctx = two_projects
    out = _bound(ctx, "preferences_status", {"limit": 200}, read_only=read_only)
    _assert_no_foreign(out, ctx)
    assert "global pref text" in out, out
    assert OWN + " pref" in out and OWN + " bare pref" in out, out


def test_preferences_status_limit_counts_only_visible_rows(two_projects):
    """Foreign rows must not consume the limit and crowd out the own rows."""
    ctx = two_projects
    conn = server._open_db()
    try:
        for index in range(30):
            memory_store.upsert_preference(
                conn, "PREFBX%d" % index, "project:" + ctx["b"], "kbx%d" % index,
                "%s filler %d" % (FOREIGN, index), confidence=0.99,
            )
    finally:
        conn.close()
    out = _bound(ctx, "preferences_status", {"limit": 3})
    _assert_no_foreign(out, ctx)
    assert out.count("\n- ") == 3, out


def test_operator_preferences_status_is_unchanged(two_projects):
    out = server.preferences_status(limit=200)
    for text in (FOREIGN + " pref", FOREIGN + " sibling pref", OWN + " pref"):
        assert text in out, out


# -- context_health (operator view) -----------------------------------------


def test_operator_context_health_is_unchanged(two_projects):
    ctx = two_projects
    out = server.context_health(session="SESSB", project=ctx["b"])
    assert FOREIGN + " session title" in out, out


# -- master_status (operator view) -------------------------------------------


def test_operator_master_status_is_unchanged(two_projects):
    out = server.master_status()
    assert "worker-foreign" in out and FOREIGN + " master result" in out, out


# -- task and checklist tools -----------------------------------------------


@pytest.mark.parametrize(
    "tool,make_args,expect",
    [
        ("task_show", lambda ctx: {"task_id": ctx["task_a"]}, OWN + " task row"),
        ("task_update", lambda ctx: {"task_id": ctx["task_a"], "note": "n"}, OWN + " task row"),
        ("checklist_show", lambda ctx: {"checklist_id": ctx["checklist_a"]}, OWN + " checklist"),
        ("checklist_update", lambda ctx: {
            "checklist_id": ctx["checklist_a"], "item": "1", "status": "done",
        }, "1/2 complete"),
        ("task_ledger", lambda ctx: {"goal_id": ctx["checklist_a"]}, '"goal_id"'),
        ("task_depend", lambda ctx: {
            "task_id": ctx["task_a"], "depends_on": ctx["checklist_a"],
        }, "dependency added"),
    ],
)
def test_id_addressed_task_tools_work_on_own_project(two_projects, tool, make_args, expect):
    ctx = two_projects
    out = _bound(ctx, tool, make_args(ctx))
    assert expect in out, out
    _assert_no_foreign(out, ctx)


def test_operator_task_tools_are_unchanged(two_projects):
    ctx = two_projects
    listed = server.task_list(limit=200)
    assert FOREIGN + " task row" in listed and OWN + " task row" in listed, listed
    shown = server.task_show(ctx["task_b"])
    assert FOREIGN + " task row" in shown, shown


def test_unbound_agent_task_list_is_unchanged(two_projects):
    out = server._agent_dispatch("task_list", {})
    assert FOREIGN + " task row" in out and OWN + " task row" in out, out


# -- tools audited clean: no per-project records in their output -------------

@pytest.mark.parametrize("tool", [
    "learning_health_status", "evaluation_history_status", "memory_quality_report",
    "memory_privacy_review", "self_heal_check", "activity_status",
])
@pytest.mark.parametrize("read_only", [False, True])
def test_audited_aggregate_tools_carry_no_foreign_records(two_projects, tool, read_only):
    ctx = two_projects
    out = _bound(ctx, tool, {}, read_only=read_only)
    assert not out.startswith("ERROR"), out
    _assert_no_foreign(out, ctx)
