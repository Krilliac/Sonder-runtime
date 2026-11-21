"""A project-bound agent's ``memory_search`` sees only its own project.

``memory_search`` is in ``_PROJECT_BOUND_AGENT_TOOLS``, but the dispatcher
forwarded only ``query`` and ``limit`` and the search SQL had no project
predicate.  An agent the host bound to project A therefore read project B's
facts, sessions, interactions and project-scoped preferences.  The host now
injects its selected project root and every project-carrying category is
filtered to it.  Global memory (lessons, ``global`` preferences) stays
visible, matching what a project-scoped chat turn already injects.  The
operator's direct MCP call is unchanged and still sees every project.
"""
from __future__ import annotations

import pytest

import memory_store
import server
import sonder_runtime.adapters.observability.activity_tracker as activity_tracker


@pytest.fixture
def two_projects(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    project_a = tmp_path / "project_a"
    project_b = tmp_path / "project_b"
    project_a.mkdir()
    project_b.mkdir()
    a, b = str(project_a), str(project_b)
    conn = server._open_db()
    try:
        memory_store.add_lesson(conn, "LGLOBAL", "needle lesson shared", None, "i0")
        memory_store.add_fact(conn, "FACTA", a, "needle fact alpha", None)
        memory_store.add_fact(conn, "FACTB", b, "needle fact beta", None)
        memory_store.touch_session(conn, "SESSA", a)
        memory_store.set_session_title(conn, "SESSA", "needle session alpha")
        memory_store.touch_session(conn, "SESSB", b)
        memory_store.set_session_title(conn, "SESSB", "needle session beta")
        memory_store.log_interaction(
            conn, "INTA", "needle task alpha", "", "ok", "code",
            session_id="SESSA", project=a,
        )
        memory_store.log_interaction(
            conn, "INTB", "needle task beta", "", "ok", "code",
            session_id="SESSB", project=b,
        )
        memory_store.upsert_preference(conn, "PREFG", "global", "kg", "needle pref global")
        memory_store.upsert_preference(conn, "PREFA", "project:" + a, "ka", "needle pref alpha")
        memory_store.upsert_preference(conn, "PREFB", "project:" + b, "kb", "needle pref beta")
    finally:
        conn.close()
    return a, b


def _assert_only_project_a(out):
    for own in ("FACTA", "SESSA", "INTA", "PREFA"):
        assert own in out, out
    for foreign in ("FACTB", "SESSB", "INTB", "PREFB", "beta"):
        assert foreign not in out, out
    # Global memory stays visible to a bound agent.
    assert "LGLOBAL" in out and "PREFG" in out, out


@pytest.mark.parametrize("read_only", [False, True])
def test_bound_agent_dispatch_sees_only_its_project(two_projects, read_only):
    a, _b = two_projects
    out = server._agent_dispatch(
        "memory_search", {"query": "needle", "limit": 10},
        read_only=read_only, repository_extra_roots=a,
    )
    _assert_only_project_a(out)


def test_bound_agent_observed_path_sees_only_its_project(two_projects):
    a, _b = two_projects
    with activity_tracker.response_span("test", prompt="t", project=a):
        out = server._agent_dispatch_observed(
            "memory_search", {"query": "needle"}, project=a,
        )
    _assert_only_project_a(out)


def test_model_cannot_widen_the_scope_with_its_own_arguments(two_projects):
    a, b = two_projects
    out = server._agent_dispatch(
        "memory_search",
        {"query": "needle", "project": b, "project_root": b, "bound_project": b},
        repository_extra_roots=a,
    )
    _assert_only_project_a(out)


def test_interaction_without_explicit_project_provenance_is_hidden(two_projects):
    a, _b = two_projects
    conn = server._open_db()
    try:
        memory_store.log_interaction(
            conn, "INTLEGACY", "needle task legacy", "", "ok", "code",
            project=a, project_explicit=False,
        )
    finally:
        conn.close()
    out = server._agent_dispatch(
        "memory_search", {"query": "needle"}, repository_extra_roots=a,
    )
    assert "INTA" in out and "INTLEGACY" not in out


def test_unscoped_operator_call_still_sees_every_project(two_projects):
    out = server.memory_search("needle", limit=10)
    for name in ("FACTA", "FACTB", "SESSA", "SESSB", "INTA", "INTB",
                 "PREFA", "PREFB", "PREFG", "LGLOBAL"):
        assert name in out, out


def test_unbound_agent_call_is_unchanged(two_projects):
    out = server._agent_dispatch("memory_search", {"query": "needle", "limit": 10})
    for name in ("FACTA", "FACTB", "SESSA", "SESSB", "INTA", "INTB"):
        assert name in out, out


def test_other_spellings_of_the_bound_root_match_and_bare_names_do_not(two_projects):
    a, _b = two_projects
    conn = server._open_db()
    try:
        memory_store.add_fact(conn, "FACTSLASH", a.replace("\\", "/") + "/", "needle slash", None)
        memory_store.add_fact(conn, "FACTNAME", "project_a", "needle bare name", None)
        memory_store.add_fact(conn, "FACTREL", "./project_a", "needle relative", None)
    finally:
        conn.close()
    out = server._agent_dispatch(
        "memory_search", {"query": "needle", "limit": 20}, repository_extra_roots=a,
    )
    assert "FACTA" in out and "FACTSLASH" in out, out
    assert "FACTNAME" not in out and "FACTREL" not in out, out


def test_all_project_session_summary_is_not_searched_for_a_bound_agent(two_projects):
    a, _b = two_projects
    conn = server._open_db()
    try:
        memory_store.touch_session(conn, "SESSMIX", a)
        # The all-project summary can describe another project's turns.
        memory_store.update_session_summary(conn, "SESSMIX", "needle mixed summary", "x")
        memory_store.touch_session(conn, "SESSOWN", a)
        memory_store.update_session_project_summary(conn, "SESSOWN", a, "needle own summary", "y")
    finally:
        conn.close()
    out = server._agent_dispatch(
        "memory_search", {"query": "summary"}, repository_extra_roots=a,
    )
    assert "SESSOWN" in out and "needle own summary" in out, out
    assert "SESSMIX" not in out and "mixed" not in out, out
    operator = server.memory_search("summary")
    assert "SESSMIX" in operator


@pytest.mark.parametrize("suffix", ["b", "\\sub", "/sub"])
def test_sibling_and_child_paths_are_other_projects(two_projects, suffix):
    """``<root>b`` and ``<root>\\sub`` share the root's spelling as a prefix but
    name a different project; a prefix match would expose them."""
    a, _b = two_projects
    other = a + suffix
    conn = server._open_db()
    try:
        memory_store.add_fact(conn, "FACTNEAR", other, "needle near fact", None)
        memory_store.touch_session(conn, "SESSNEAR", other)
        memory_store.set_session_title(conn, "SESSNEAR", "needle near session")
        memory_store.log_interaction(
            conn, "INTNEAR", "needle near task", "", "ok", "code",
            session_id="SESSNEAR", project=other,
        )
        memory_store.upsert_preference(
            conn, "PREFNEAR", "project:" + other, "kn", "needle near pref scoped",
        )
        memory_store.upsert_preference(
            conn, "PREFNEARBARE", other, "knb", "needle near pref bare",
        )
    finally:
        conn.close()
    out = server._agent_dispatch(
        "memory_search", {"query": "needle", "limit": 50}, repository_extra_roots=a,
    )
    _assert_only_project_a(out)
    for near in ("FACTNEAR", "SESSNEAR", "INTNEAR", "PREFNEAR", "near"):
        assert near not in out, out
    operator = server.memory_search("needle near", limit=50)
    for near in ("FACTNEAR", "SESSNEAR", "INTNEAR", "PREFNEAR", "PREFNEARBARE"):
        assert near in operator, operator


def test_non_string_bound_project_fails_closed(two_projects):
    """A bound scope that is not a path string must never become the global view."""
    from sonder_runtime.adapters.memory_search import search_memory

    conn = server._open_db()
    try:
        with pytest.raises(TypeError, match="bound_project"):
            search_memory(conn, memory_store, "needle", 10, bound_project=["x"])  # type: ignore[arg-type]
    finally:
        conn.close()
