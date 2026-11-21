"""A project-bound agent's ``preferences_status`` sees only global and own project.

Tests ``memory_store.all_preferences(..., bound_project=...)``,
``PreferenceService.status(..., bound_project=...)``, and
``preference_adapters.preferences_status_scoped(...)`` across global,
``project:<root>``, bare ``<root>``, sibling/child path prefixes, limit
crowding, disabled filtering, and operator unscoped calls.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import memory_store
import server
from sonder_runtime.adapters.preference_adapters import (
    LegacyPreferenceCodec,
    LegacyPreferenceRepository,
    NullPreferenceEventSink,
    preferences_status_scoped,
)
from sonder_runtime.application.preferences import PreferenceService

FOREIGN = "ZULUFOREIGN"
OWN = "ALPHAOWN"


@pytest.fixture
def two_projects(monkeypatch, tmp_path):
    db_path = tmp_path / "mem.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db_path))
    project_a = tmp_path / "proj_a"
    project_b = tmp_path / "proj_b"
    project_a.mkdir()
    project_b.mkdir()
    a, b = str(project_a), str(project_b)
    conn = memory_store.connect(db_path)
    try:
        memory_store.upsert_preference(conn, "PREFG", "global", "kg", "global pref text")
        memory_store.upsert_preference(conn, "PREFA", "project:" + a, "ka", OWN + " pref")
        memory_store.upsert_preference(conn, "PREFABARE", a, "kab", OWN + " bare pref")
        memory_store.upsert_preference(conn, "PREFA_SLASH", "project:" + a.replace("\\", "/") + "/", "kas", OWN + " slash pref")
        memory_store.upsert_preference(conn, "PREFB", "project:" + b, "kb", FOREIGN + " pref")
        memory_store.upsert_preference(conn, "PREFBBARE", b, "kbb", FOREIGN + " bare pref")
        memory_store.upsert_preference(
            conn, "PREFNEAR", "project:" + a + "b", "kn", FOREIGN + " sibling pref",
        )
        memory_store.upsert_preference(
            conn, "PREFSUB", "project:" + a + "\\sub", "ks", FOREIGN + " child pref",
        )
        memory_store.upsert_preference(
            conn, "PREFB_DIS", "project:" + b, "kbd", FOREIGN + " disabled pref",
        )
        memory_store.set_preference_enabled(conn, "PREFB_DIS", False, scope="project:" + b)
        memory_store.upsert_preference(
            conn, "PREFA_DIS", "project:" + a, "kad", OWN + " disabled pref",
        )
        memory_store.set_preference_enabled(conn, "PREFA_DIS", False, scope="project:" + a)
    finally:
        conn.close()
    app = SimpleNamespace(
        preferences=PreferenceService(
            LegacyPreferenceRepository(lambda: memory_store.connect(db_path)),
            LegacyPreferenceCodec(),
            NullPreferenceEventSink(),
        )
    )
    return {"db_path": db_path, "a": a, "b": b, "app": app}


def _assert_no_foreign(out, ctx):
    assert FOREIGN not in out, out
    assert "proj_b" not in out, out
    assert ctx["b"] not in out, out


def test_preferences_status_lists_only_global_and_own_project(two_projects):
    ctx = two_projects
    out = preferences_status_scoped(ctx["app"], limit=200, bound_project=ctx["a"])
    _assert_no_foreign(out, ctx)
    assert "global pref text" in out, out
    assert OWN + " pref" in out, out
    assert OWN + " bare pref" in out, out
    assert OWN + " slash pref" in out, out
    assert OWN + " disabled pref" not in out, out


def test_preferences_status_include_disabled_stays_project_scoped(two_projects):
    ctx = two_projects
    out = preferences_status_scoped(
        ctx["app"], include_disabled=True, limit=200, bound_project=ctx["a"],
    )
    _assert_no_foreign(out, ctx)
    assert OWN + " disabled pref" in out, out
    assert FOREIGN + " disabled pref" not in out, out


def test_preferences_status_limit_counts_only_visible_rows(two_projects):
    """Foreign rows must not consume the limit and crowd out the own rows."""
    ctx = two_projects
    conn = memory_store.connect(ctx["db_path"])
    try:
        for index in range(30):
            memory_store.upsert_preference(
                conn, "PREFBX%d" % index, "project:" + ctx["b"], "kbx%d" % index,
                "%s filler %d" % (FOREIGN, index), confidence=0.99,
            )
    finally:
        conn.close()
    out = preferences_status_scoped(ctx["app"], limit=3, bound_project=ctx["a"])
    _assert_no_foreign(out, ctx)
    assert out.count("\n- ") == 3, out


def test_operator_preferences_status_is_unchanged(two_projects):
    ctx = two_projects
    out = preferences_status_scoped(ctx["app"], limit=200)
    for text in (FOREIGN + " pref", FOREIGN + " sibling pref", FOREIGN + " child pref", OWN + " pref"):
        assert text in out, out
    server_out = server.preferences_status(limit=200)
    for text in (FOREIGN + " pref", FOREIGN + " sibling pref", OWN + " pref"):
        assert text in server_out, server_out


def test_non_string_bound_project_fails_closed(two_projects):
    ctx = two_projects
    out = preferences_status_scoped(ctx["app"], bound_project=["bad"])  # type: ignore[arg-type]
    assert out.startswith("ERROR:"), out
    _assert_no_foreign(out, ctx)
    conn = memory_store.connect(ctx["db_path"])
    try:
        with pytest.raises(TypeError, match="bound_project"):
            memory_store.all_preferences(conn, bound_project=["bad"])
    finally:
        conn.close()
