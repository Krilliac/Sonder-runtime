"""The activity record must not call a failed tool run a success.

``server._agent_dispatch_observed`` derives ``ok`` as
``not str(observation).startswith("ERROR:")``. That is a statement about the
*dispatcher*, not about the work. Measured on this lineage, against a real
project holding one genuinely failing pytest::

    harness_tools.test_run(...)          -> {'ok': False, 'returncode': 1}
    rendered observation first line      -> 'test run (pytest)'
    str(observation).startswith("ERROR:")-> False
    activity_tracker tool_call event     -> ok=True          <-- inverted
    activity_tracker._public_event(...)  -> phase 'completed' <-- inverted

and ``ERROR:`` is emitted by these tools only when the tool never ran, so the
flag was inverted rather than merely noisy: on the agent path every failing
run was recorded as a pass.

The outcome feed was fixed separately (``_feed_grounded_outcome`` reads
``grounded_outcomes.rendered_verdict``), but that fix is scoped to
``grounded_outcomes.VERIFIERS`` and the same ``ok`` still reaches
``activity_tracker.record_tool_result``. Measured, 26 dispatchable tools
render their own verdict and **19 of them are not verifiers** -- ``git_merge``,
``git_commit``, ``apply_patch``, ``workspace_run``, ``script_run``, the whole
``dependency_*`` family -- so their failures were never covered at all::

    script_run on a script exiting 3
      rendered  ->   ok: False
      activity  ->   ok=True

Why this is scoped to a derived tool set rather than applied to every
observation -- measured, not argued: ``file_read`` of a 23-byte YAML whose
first line is ``ok: false`` makes ``grounded_outcomes.rendered_verdict``
return ``False``. A blanket read would let *file content* mark a successful
read as failed, which is the same defect pointing the other way, and the
content is caller-supplied. So the correction applies only to tools whose
rendered text this server itself produced, and
``test_rendered_verdict_tools_match_their_renderers`` re-derives that set from
the renderer call sites by AST so the list cannot drift.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import activity_tracker
import grounded_outcomes
import server


def _tool_call_events(response):
    return [
        event for event in (response.get("events") or [])
        if event.get("kind") == "tool_call"
    ]


def _observe(tool_name, args, *, project="", read_only=False):
    """Run the real agent path and return (observation, recorded ok)."""
    with activity_tracker.response_span("test", prompt="t", project=project):
        observation = server._agent_dispatch_observed(
            tool_name, args, read_only=read_only, project=project,
        )
        events = _tool_call_events(activity_tracker.current() or {})
        return observation, (events[-1].get("ok") if events else None)


def _failing_pytest_project(tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_thing.py").write_text(
        "def test_passes():\n"
        "    assert 1 == 1\n"
        "\n"
        "\n"
        "def test_genuinely_fails():\n"
        "    assert 2 + 2 == 5\n",
        encoding="utf-8",
    )
    return project


@pytest.mark.integration
def test_failing_pytest_is_not_recorded_as_ok(tmp_path):
    """The case from the docstring, end to end through the real path."""
    import harness_tools

    project = _failing_pytest_project(tmp_path)

    # Ground truth from the verifier itself, before the agent path sees it.
    truth = harness_tools.test_run(
        root=str(project), framework="pytest", extra_roots=str(project),
    )
    assert truth["ok"] is False, truth
    assert truth["returncode"] != 0, truth

    observation, recorded_ok = _observe(
        "test_run", {"root": str(project), "framework": "pytest"},
        project=str(project),
    )

    # The premise: this is exactly why the ERROR: heuristic missed it.
    assert not str(observation).startswith("ERROR:")
    assert recorded_ok is False, observation


@pytest.mark.integration
def test_failing_pytest_is_not_reported_as_a_completed_phase(tmp_path):
    """The public snapshot is a second surface reading the same flag."""
    project = _failing_pytest_project(tmp_path)

    with activity_tracker.response_span("test", prompt="t", project=str(project)):
        server._agent_dispatch_observed(
            "test_run", {"root": str(project), "framework": "pytest"},
            read_only=False, project=str(project),
        )
        events = _tool_call_events(activity_tracker.current() or {})
        public = activity_tracker._public_event(events[-1])
        transcript = activity_tracker.format_transcript(activity_tracker.current())

    assert public["phase"] == "failed"
    assert public["ok"] is False
    assert transcript.splitlines()[0].startswith("×")


def _rendered_failure(title, data):
    """Real rendered text, from the server's own renderer."""
    return server._format_run_result(title, data)


@pytest.mark.parametrize("tool_name, title", [
    ("git_merge", "git merge"),
    ("git_commit", "git commit"),
    ("apply_patch", "apply patch"),
    ("workspace_run", "workspace run"),
    ("dependency_add", "dependency add (pip)"),
])
def test_non_verifier_renderer_tools_are_covered_too(monkeypatch, tool_name, title):
    """The 19 renderer tools the VERIFIERS-scoped outcome fix never reached."""
    assert tool_name not in grounded_outcomes.VERIFIERS
    rendered = _rendered_failure(title, {
        "ok": False, "returncode": 1, "command": ["x"], "cwd": "",
        "timed_out": False, "stdout": "ok: true\n", "stderr": "",
    })
    assert not rendered.startswith("ERROR:")
    monkeypatch.setattr(server, "_agent_dispatch", lambda *a, **k: rendered)

    _observation, recorded_ok = _observe(tool_name, {})

    assert recorded_ok is False


def test_a_successful_renderer_tool_is_still_recorded_ok(monkeypatch):
    rendered = _rendered_failure("test run (pytest)", {
        "ok": True, "returncode": 0, "command": ["x"], "cwd": "",
        "timed_out": False, "stdout": "", "stderr": "",
    })
    monkeypatch.setattr(server, "_agent_dispatch", lambda *a, **k: rendered)

    _observation, recorded_ok = _observe("test_run", {})

    assert recorded_ok is True


def test_tool_content_cannot_flip_the_verdict(tmp_path, monkeypatch):
    """The over-reach guard: ``file_read`` content is not a verdict.

    Measured -- ``rendered_verdict`` on this observation returns False, so a
    blanket read would file a successful read as a failure.
    """
    root = tmp_path / "root"
    root.mkdir()
    config = root / "config.yaml"
    config.write_text("ok: false\nname: demo\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))

    observation, recorded_ok = _observe("file_read", {"path": str(config)})

    assert not str(observation).startswith("ERROR:"), observation
    assert grounded_outcomes.rendered_verdict(observation) is False
    assert recorded_ok is True


def test_a_tool_that_never_ran_is_still_recorded_as_failure(monkeypatch):
    monkeypatch.setattr(
        server, "_agent_dispatch", lambda *a, **k: "ERROR: no build system found",
    )

    _observation, recorded_ok = _observe("build_run", {})

    assert recorded_ok is False


def test_a_renderer_tool_with_no_rendered_verdict_keeps_the_dispatch_answer(monkeypatch):
    monkeypatch.setattr(server, "_agent_dispatch", lambda *a, **k: "build\n  notes: none\n")

    _observation, recorded_ok = _observe("build_run", {})

    assert recorded_ok is True


def _tools_calling(renderer):
    """Tool functions in ``server.py`` whose body calls ``renderer``.

    Read from the source by AST rather than from a hand-kept list, because a
    hand-kept list is exactly the thing that drifts.
    """
    tree = ast.parse(
        pathlib.Path(server.__file__).read_text(encoding="utf-8"), "server.py",
    )
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name == renderer:
                found.add(node.name)
                break
    return found


def _dispatchable():
    import tool_capabilities

    return set(tool_capabilities.dispatch_names(server._agent_dispatch))


def test_every_format_run_result_tool_is_covered():
    """A new tool using the main renderer must not quietly miss the read."""
    derived = _tools_calling("_format_run_result") & _dispatchable()

    assert derived, "AST derivation found nothing -- the guard is not measuring"
    assert derived <= server._RENDERED_VERDICT_TOOLS, sorted(
        derived - server._RENDERED_VERDICT_TOOLS
    )


def test_every_dispatchable_verifier_is_in_the_set():
    """A verifier whose verdict is not read is the original defect."""
    verifiers = {v for v in grounded_outcomes.VERIFIERS if v in _dispatchable()}

    assert verifiers, "no dispatchable verifiers found -- the guard is not measuring"
    assert verifiers <= server._RENDERED_VERDICT_TOOLS, sorted(
        verifiers - server._RENDERED_VERDICT_TOOLS
    )


def test_the_set_holds_no_tool_the_agent_cannot_call():
    unreachable = server._RENDERED_VERDICT_TOOLS - _dispatchable()

    assert not unreachable, sorted(unreachable)


def test_every_renderer_shape_is_readable():
    """Render one failing result through each renderer and read it back.

    This is the guard that would have caught the residual: four of these six
    shapes returned ``None`` from ``rendered_verdict``, so their FAIL was
    answered "no opinion" and the caller's inverted ``ok=True`` stood.
    """
    import artifact_grounding
    import code_runner
    import isolated_runner

    run = {"ok": False, "returncode": 1, "command": ["x"], "cwd": "",
           "timed_out": False, "stdout": "", "stderr": "boom"}
    code = {"ok": False, "returncode": 1, "language": "python", "cwd": "",
            "timeout": 10, "stdout": "", "stderr": "boom"}
    shapes = {
        "server._format_run_result": server._format_run_result("test run", run),
        "code_runner.format_result": code_runner.format_result(code),
        "code_runner.format_project_result": code_runner.format_project_result(
            {"ok": False, "files": ["main.py"], "timeout": 10, "steps": []}
        ),
        "isolated_runner.format_result": isolated_runner.format_result(
            {"ok": False, "returncode": 1, "runtime": "docker", "project": "p"}
        ),
        "artifact_grounding.format_result": artifact_grounding.format_result(
            {"ok": False, "recipe": "r", "checked_files": 1, "passed_checks": 0,
             "failed_checks": 1, "path": "p", "checks": []}
        ),
        # artifact_verify renders its verdict inline; this is that line.
        "server.artifact_verify": "artifact verification: FAIL\n  checked: 3\n",
    }

    unreadable = {
        name: grounded_outcomes.rendered_verdict(text)
        for name, text in shapes.items()
        if grounded_outcomes.rendered_verdict(text) is not False
    }

    assert not unreadable, unreadable


def test_a_passing_render_reads_back_as_a_pass():
    import code_runner

    assert grounded_outcomes.rendered_verdict("project status: ok\nfiles: a\n") is True
    assert grounded_outcomes.rendered_verdict("artifact grounding: PASS\n") is True
    assert grounded_outcomes.rendered_verdict("artifact verification: PASS\n") is True
    assert grounded_outcomes.rendered_verdict(
        code_runner.format_result({"ok": True, "returncode": 0, "language": "python",
                                   "cwd": "", "timeout": 10, "stdout": "", "stderr": ""})
    ) is True
