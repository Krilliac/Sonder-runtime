"""The served route's ``project`` reaches routed work as a directory only
when the deployment already exposes that directory.

Measured 2026-09-03: on an authenticated deployment the field was namespaced
to an opaque id before the work router saw it, so the workbench agent ran
with no workspace; passing a client-chosen directory through unconditionally
would have made it the agent's base for relative paths anywhere, so the
pass-through is bounded by the configured file roots.
"""
from __future__ import annotations

import server
from sonder_runtime.interfaces.http import serve


def test_a_directory_inside_the_configured_roots_passes_through(monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    assert server.served_work_project(str(project)) == str(project.resolve())
    assert serve._work_project_for_request(str(project), "http-project-abc") == str(project.resolve())


def test_a_bare_namespace_keeps_the_namespaced_id(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    assert server.served_work_project("demo") == ""
    assert serve._work_project_for_request("demo", "http-project-abc") == "http-project-abc"


def test_a_directory_outside_the_roots_or_a_missing_one_is_ignored(monkeypatch, tmp_path):
    exposed = tmp_path / "exposed"
    exposed.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(exposed))

    assert server.served_work_project(str(elsewhere)) == ""
    assert server.served_work_project(str(exposed / "missing")) == ""
    assert serve._work_project_for_request(str(elsewhere), "http-project-abc") == "http-project-abc"


def test_empty_and_odd_values_are_ignored():
    assert server.served_work_project("") == ""
    assert server.served_work_project(None) == ""
    assert serve._work_project_for_request("", "http-project-abc") == "http-project-abc"
