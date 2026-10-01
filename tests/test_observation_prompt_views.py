"""A7 preserves the just-read file tail and shares the worker's prompt prefix."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sonder_runtime.adapters import prompt_store
from sonder_runtime.domain import prompt_templates
from sonder_runtime.domain.agents.observation_prompt import fit_sectioned_text


ROOT = Path(__file__).resolve().parents[1]


def _observation_view():
    """Execute the actual host helper without composing services or a live model."""
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    names = {"_AGENT_MODEL_OBSERVATION_CHARS", "_AGENT_SECTIONED_OBSERVATION_PREFIXES"}
    nodes = [node for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name == "_agent_model_observation_view"
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)
    )]
    namespace = {
        "_CONTEXT_PACK_SECTION_PREFIX": "=== FILE:",
        "_fit_sectioned_agent_text": fit_sectioned_text,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), namespace)
    return namespace["_agent_model_observation_view"]


@pytest.mark.parametrize("tool", ["file_read", "file_read_range", "workspace_run"])
def test_large_observation_keeps_head_tail_and_explicit_omission(tool):
    # AL-F5: a 12k read silently lost the end of the file under the 6k head slice.
    text = "H" * 5000 + "M" * 4000 + "T" * 3000
    shown = _observation_view()(tool, text)
    marker = "...[4000 chars omitted; use file_read offset/limit or output_digest]..."
    assert shown == text[:5000] + marker + text[-3000:]
    assert text[-2000:] in shown


@pytest.mark.parametrize("size", [0, 1, 6000, 7999, 8000])
def test_small_observation_is_verbatim(size):
    text = "x" * size
    assert _observation_view()("file_read", text) == text


def test_sectioned_context_pack_still_fits_each_section():
    text = "=== FILE: first.py\n" + "a" * 7000 + "\n=== FILE: last.py\n" + "b" * 7000
    shown = _observation_view()("context_pack", text)
    assert len(shown) <= 8000
    assert "=== FILE: first.py" in shown and "=== FILE: last.py" in shown
    assert "host clipped this section" in shown


def _render_default(name, **fields):
    template = prompt_templates.normalize((ROOT / "prompts" / (name + ".md")).read_text(encoding="utf-8"))
    values = {field: "test $literal {value}" for field in prompt_store.CATALOG[name].fields}
    values.update(fields)
    return prompt_templates.render(template, values)


@pytest.mark.parametrize("name", ["autopilot_system", "autopilot_planner", "autopilot_reviewer"])
def test_role_templates_share_worker_static_first_line(name):
    worker = _render_default("agent_local", host_brief="").rstrip()
    rendered = _render_default(name)
    assert rendered.splitlines()[0] == worker


def test_role_identity_is_only_at_system_tail():
    planner = _render_default("autopilot_system", role="planner")
    reviewer = _render_default("autopilot_system", role="reviewer")
    assert planner.rsplit("\n", 1)[0] == reviewer.rsplit("\n", 1)[0]
    assert planner.endswith("Role: planner")
    assert reviewer.endswith("Role: reviewer")


def test_reviewer_explicitly_requires_json_object_only():
    assert "reply with the JSON object only" in _render_default("autopilot_reviewer")
