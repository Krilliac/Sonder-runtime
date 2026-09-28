"""Every externalised prompt renders byte-for-byte what the old code emitted.

``tests/fixtures/prompt_golden.json`` was captured from the hard-coded
prompts on the commit before they moved to ``prompts/*.md`` (see
``tests/fixtures/prompt_golden_capture.py``), using fixed sample values that
include ``$``, ``{}``, ``%`` and backslashes. Here the shipped defaults are
rendered with the fields each call site derives from those same samples.
"""
import json
import pathlib

import pytest

from sonder_runtime.adapters import prompt_store

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "prompt_golden.json"
GOLDEN = json.loads(FIXTURE.read_text(encoding="utf-8"))

TRICKY = "a $dollar {brace} %s 100% \\ back"
RUN = {
    "objective": "Fix the parser " + TRICKY,
    "evidence": ["log line 1", "trace $2"],
    "criteria": ["tests pass", "no {regressions}"],
    "files": ["parser.py", "tests/test_parser.py"],
    "project": "demo-project",
    "policy": "workspace",
    "allow_web": True,
    "adaptive": True,
    "max_replans": 2,
    "failures": 1,
    "max_failures": 3,
    "plan": [{"id": "task-00"}, {"id": "task-01"}],
    "max_tasks": 12,
    "checkpoints": 1,
    "replans": 0,
}
TASK = {"id": "task-01", "kind": "implement", "title": "Edit $x", "instruction": "Change {y} 50%"}
LEDGER = [{"id": "task-00", "status": "done", "result": "ok $1"}]

# The fields each call site passes, derived from the capture's sample values.
FIELDS = {
    "selfmod_editor": dict(
        objective=RUN["objective"], evidence="; ".join(RUN["evidence"]),
        criteria="; ".join(RUN["criteria"]), files=", ".join(RUN["files"]),
        workspace="C:/ws/run-1 $w",
    ),
    "claim_reviewer": dict(tools=", ".join(["text_search", "file_read_range", "directory_tree"])),
    "agent_local": dict(host_brief=""),
    "autopilot_system": dict(role="planner"),
    "autopilot_planner": dict(
        objective=RUN["objective"], project=RUN["project"], policy=RUN["policy"],
        web="on", adaptive="on", initial_limit=4, max_tasks=12,
        max_replans=RUN["max_replans"], tools=", ".join(["file_read", "text_search"]),
    ),
    "autopilot_reviewer": dict(
        objective=RUN["objective"], issue="adaptive checkpoint $1 {x}",
        failures=RUN["failures"], max_failures=RUN["max_failures"],
        task_count=len(RUN["plan"]), max_tasks=RUN["max_tasks"],
        checkpoints=RUN["checkpoints"], replans=RUN["replans"],
        max_replans=RUN["max_replans"], ledger=json.dumps(LEDGER, ensure_ascii=False),
    ),
    "autopilot_worker": dict(
        objective=RUN["objective"], task_id=TASK["id"], kind=TASK["kind"],
        title=TASK["title"], instruction=TASK["instruction"],
        criteria="\n".join("- " + item for item in RUN["criteria"]),
        prior="prior evidence " + TRICKY,
    ),
    "execution_router": dict(project="demo", request=("Refactor " + TRICKY)[:12000]),
    "reflection_distill": dict(
        signal="pass", task="Task " + TRICKY, response="def f(): return '$x'",
    ),
    "reflection_pitfall": dict(
        task="Task " + TRICKY, response="resp {1}", error=("E" * 1300)[:1200],
    ),
    "child_lane": dict(
        workspace_root="C:/lanes/one $root", tools=", ".join(["file_read", "run_tests"]),
    ),
}


@pytest.fixture(autouse=True)
def _no_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_PROMPTS_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    prompt_store.reload()


def test_fixture_covers_the_whole_catalog():
    captured = set(GOLDEN["prompts"]) | {"runtime_identity"}
    assert captured == set(prompt_store.CATALOG)


@pytest.mark.parametrize("name", sorted(GOLDEN["prompts"]))
def test_default_renders_the_pre_change_text(name):
    expected = GOLDEN["prompts"][name]["text"]
    loaded = prompt_store.load(name)
    assert loaded.source == "default"
    assert prompt_store.render(name, **FIELDS.get(name, {})) == expected


def test_template_fields_match_the_catalog():
    for name, spec in prompt_store.CATALOG.items():
        if name == "runtime_identity":
            continue
        assert set(FIELDS.get(name, {})) == set(spec.fields), name


@pytest.mark.parametrize("variant,args", [
    ("local", ("qwen2.5-coder:7b $x",)),
    ("cloud", ("gpt-oss:120b-cloud", True)),
    ("inference", ("qwen3:14b", False, "sonder_inference")),
    ("empty", ("",)),
])
def test_runtime_identity_matches_pre_change_text(variant, args):
    assert prompt_store.runtime_identity_block(*args) == GOLDEN["runtime_identity"][variant]
