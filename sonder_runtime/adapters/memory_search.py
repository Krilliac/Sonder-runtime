"""Text search over local memory, optionally confined to one bound project.

The operator's MCP ``memory_search`` reads every project.  An agent the host
bound to a project directory must read only that project's records, so the
caller passes the host-selected root as ``bound_project`` (never a model
argument) and every project-carrying category is filtered to it:

* facts: ``facts.project`` names the bound project;
* sessions: ``sessions.project`` names it, and the searched/shown summary is
  that project's own summary (``session_project_summaries``), never the
  all-project summary, which can describe other projects' turns;
* interactions: ``interactions.project`` names it with explicit provenance
  (``project_explicit=1``), the same rule project-scoped recall applies to
  migrated rows;
* preferences: ``global`` plus the project's own scopes (``<project>`` and
  ``project:<project>``), the set a project-scoped chat turn already injects.

Lessons carry no project and are global memory.  A project-scoped chat turn
already injects them, so a bound agent keeps seeing them too.

A stored project value names the bound project when its lexically normalised
path equals the bound root's.  Matching is lexical on purpose: resolving a
stored string on the filesystem could touch a network share named by any
stored value.  A spelling that only a symlink resolution would equate is
therefore not matched, which hides the row rather than exposing another
project's.

Thread affinity: one call uses one caller-owned connection; the SQL function
registered for the bound scope lives on that connection only.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from typing import Any

from sonder_runtime.platform.logging import Redactor

_MATCH_FUNCTION = "sonder_bound_project"


def _normalized(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def bound_project_matcher(bound_project: str) -> Callable[[object], bool]:
    """Return ``value -> bool``: does a stored project value name the root?"""
    root = _normalized(bound_project)
    seen: dict[object, bool] = {}

    def matches(value: object) -> bool:
        cached = seen.get(value)
        if cached is None:
            cached = isinstance(value, str) and bool(value.strip()) and (
                _normalized(value) == root
            )
            seen[value] = cached
        return cached

    return matches


def search_memory(
    conn: sqlite3.Connection,
    store: Any,
    query: str,
    limit: int,
    *,
    bound_project: str = "",
) -> str:
    """Render the memory search report for ``query``.

    ``store`` is the live ``memory_store`` module (the server swaps it on a
    live reload).  ``bound_project`` empty means the unscoped operator view.
    """
    if bound_project and not isinstance(bound_project, str):
        # Fail closed: a bound scope must never degrade to the global view.
        raise TypeError("bound_project must be a project root path string")
    bound = bool(bound_project)
    like = "%%%s%%" % query.replace("%", r"\%").replace("_", r"\_")
    if bound:
        conn.create_function(
            _MATCH_FUNCTION, 1, bound_project_matcher(bound_project),
            deterministic=True,
        )
    redactor = Redactor()
    safe_query = redactor.redact(query)
    query_cf = query.casefold()
    query_contained_secret = safe_query != query

    def _redact(value: object) -> str:
        text = str(value or "")
        return redactor.redact(text) if text else ""

    lesson_ids = store.fts_search(conn, query, limit=limit)
    lessons: list[dict[str, Any]] = []
    for lesson_id in lesson_ids:
        text = store.get_lesson_text(conn, lesson_id)
        if text:
            safe_text = _redact(text)
            if query_contained_secret and query_cf not in safe_text.casefold():
                continue
            lessons.append({"id": lesson_id, "text": safe_text})
    current, instants = store.current_fact_params()  # expired facts stay out
    fact_scope = " AND %s(project)" % _MATCH_FUNCTION if bound else ""
    facts = []
    for row in conn.execute(
        "SELECT id, project, text FROM facts WHERE text LIKE ? ESCAPE '\\' AND "
        + current + fact_scope + " ORDER BY ts DESC, rowid DESC LIMIT ?",
        (like, *instants, limit),
    ).fetchall():
        item = dict(row)
        safe_text = _redact(item.get("text"))
        if query_cf and query_cf not in safe_text.casefold():
            continue
        item["text"] = safe_text
        facts.append(item)
    preference_scope = (
        " AND (scope='global' OR %s(scope) OR (substr(scope, 1, 8)='project:' "
        "AND %s(substr(scope, 9))))" % (_MATCH_FUNCTION, _MATCH_FUNCTION)
        if bound else ""
    )
    preferences = []
    for row in conn.execute(
        "SELECT id, scope, key, text, confidence, evidence_count FROM preferences "
        "WHERE text LIKE ? ESCAPE '\\' AND enabled=1" + preference_scope + " "
        "ORDER BY confidence DESC, evidence_count DESC, updated_ts DESC LIMIT ?",
        (like, limit),
    ).fetchall():
        item = dict(row)
        safe_text = _redact(item.get("text"))
        if query_cf and query_cf not in safe_text.casefold():
            continue
        item["text"] = safe_text
        preferences.append(item)
    if bound:
        raw_sessions = conn.execute(
            "SELECT s.session_id, s.title, ps.summary, s.project FROM sessions s "
            "LEFT JOIN session_project_summaries ps ON ps.session_id=s.session_id "
            "AND ps.project_key='project:' || s.project "
            "WHERE " + _MATCH_FUNCTION + "(s.project) AND "
            "(s.title LIKE ? ESCAPE '\\' OR ps.summary LIKE ? ESCAPE '\\') "
            "ORDER BY s.updated_ts DESC, s.rowid DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
    else:
        raw_sessions = conn.execute(
            "SELECT session_id, title, summary, project FROM sessions "
            "WHERE title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' "
            "ORDER BY updated_ts DESC, rowid DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
    sessions = []
    for row in raw_sessions:
        item = dict(row)
        safe_title = _redact(item.get("title"))
        safe_summary = _redact(item.get("summary"))
        if query_cf and (
            query_cf not in safe_title.casefold()
            and query_cf not in safe_summary.casefold()
        ):
            continue
        item["title"] = safe_title
        item["summary"] = safe_summary
        sessions.append(item)
    interaction_scope = (
        " AND %s(project) AND project_explicit=1" % _MATCH_FUNCTION if bound else ""
    )
    interactions = []
    for row in conn.execute(
        "SELECT id, task, response, tier, session_id, ts FROM interactions "
        "WHERE (task LIKE ? ESCAPE '\\' OR response LIKE ? ESCAPE '\\')"
        + interaction_scope + " ORDER BY ts DESC, rowid DESC LIMIT ?",
        (like, like, limit),
    ).fetchall():
        item = dict(row)
        safe_task = _redact(item.get("task"))
        safe_response = _redact(item.get("response"))
        if query_cf and (
            query_cf not in safe_task.casefold()
            and query_cf not in safe_response.casefold()
        ):
            continue
        item["task"] = safe_task
        item["response"] = safe_response
        interactions.append(item)

    lines = ["memory search: %r" % safe_query]
    lines.append("lessons (%d):" % len(lessons))
    lines.extend(
        "  - %s: %s" % (lesson["id"], lesson["text"][:220])
        for lesson in lessons
    )
    lines.append("facts (%d):" % len(facts))
    lines.extend("  - %s/%s: %s" % (f["project"], f["id"], f["text"][:220]) for f in facts)
    lines.append("preferences (%d):" % len(preferences))
    lines.extend("  - %s/%s: %s" % (
        p["scope"], p["id"], p["text"][:220],
    ) for p in preferences)
    lines.append("sessions (%d):" % len(sessions))
    lines.extend("  - %s [%s]: %s" % (
        s["session_id"], s.get("project") or "default",
        (s.get("title") or s.get("summary") or "(untitled)")[:220],
    ) for s in sessions)
    lines.append("interactions (%d):" % len(interactions))
    lines.extend("  - %s [%s]: %s" % (
        i["id"], i.get("tier") or "?",
        (i.get("task") or "")[:220],
    ) for i in interactions)
    return "\n".join(lines)


__all__ = ["bound_project_matcher", "search_memory"]
