"""Capture the pre-externalisation prompt text as a golden fixture.

Run ONCE on the commit before the prompts moved to ``prompts/*.md``
(base 32adc318). It evaluates each prompt expression straight from that
commit's source (by line number) with fixed sample values, so the fixture
holds the exact bytes the old code produced -- not a copy re-typed by hand.
After the move the line anchors no longer exist; the committed JSON is the
record, and ``tests/test_prompt_store_golden.py`` compares every rendered
default against it.

    python tests/fixtures/prompt_golden_capture.py > tests/fixtures/prompt_golden.json
"""
import ast
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

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

# (name, file, line, how, namespace)
#   how = "assign": the value of the assignment starting on that line
#         "arg0":   the first positional argument of the call on that line
SPECS = [
    ("trace_instructions", "server.py", 1453, "assign", {}),
    ("selfmod_editor", "server.py", 3124, "assign",
     {"run": RUN, "workspace": "C:/ws/run-1 $w"}),
    ("web_research_agent", "server.py", 16170, "assign", {}),
    ("claim_reviewer", "server.py", 18038, "arg0",
     {"vocabulary": ["text_search", "file_read_range", "directory_tree"]}),
    ("agent_hosted", "server.py", 20597, "assign", {}),
    ("agent_local", "server.py", 20612, "assign",
     {"_local_agent_brief": lambda scope: "", "project_scope": None}),
    ("autopilot_system", "server.py", 22441, "arg0", {"role": "planner"}),
    ("autopilot_planner", "server.py", 22482, "assign",
     {"run": RUN, "allowed": ["file_read", "text_search"], "initial_limit": 4, "max_tasks": 12}),
    ("autopilot_reviewer", "server.py", 22549, "assign",
     {"run": RUN, "issue": "adaptive checkpoint $1 {x}", "ledger": LEDGER, "json": json}),
    ("autopilot_worker", "server.py", 22666, "assign",
     {"run": RUN, "task": TASK, "prior": "prior evidence " + TRICKY}),
    ("execution_router_system", "server.py", 23162, "arg0", {}),
    ("execution_router", "server.py", 23175, "assign",
     {"project": "demo", "prompt": "Refactor " + TRICKY}),
    ("reflection_distill_system", "reflection.py", 9, "assign", {}),
    ("reflection_distill", "reflection.py", 75, "assign",
     {"signal": "pass", "task": "Task " + TRICKY, "response": "def f(): return '$x'"}),
    ("reflection_pitfall_system", "reflection.py", 203, "assign", {}),
    ("reflection_pitfall", "reflection.py", 221, "assign",
     {"task": "Task " + TRICKY, "response": "resp {1}", "detail": "E" * 1300}),
    ("grounded_extraction_system", "grounded_extraction.py", 65, "assign", {}),
    ("curriculum_task_generator", "self_curriculum.py", 14, "assign", {}),
    ("ollama_alias_system", "setup_alias.py", 33, "assign", {}),
    ("child_lane", "sonder_runtime/application/agents/interactive_lanes.py", 1692, "assign",
     {"lane": {"workspace_root": "C:/lanes/one $root", "allowed_tools": ["file_read", "run_tests"]}}),
]


def _expression(path, line, how):
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line:
            continue
        if how == "assign" and isinstance(node, (ast.Assign, ast.AnnAssign)):
            return node.value
        if how == "arg0" and isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            return node.value.args[0]
    raise SystemExit("no %s expression at %s:%d" % (how, path, line))


def main():
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    out = {"source_commit": head, "prompts": {}}
    for name, path, line, how, namespace in SPECS:
        expr = _expression(path, line, how)
        text = eval(compile(ast.Expression(expr), path, "eval"), {"__builtins__": __builtins__}, dict(namespace))
        out["prompts"][name] = {"source": "%s:%d" % (path, line), "text": text}
    sys.path.insert(0, str(ROOT))
    import personas
    for persona, text in personas.PERSONAS.items():
        out["prompts"]["personas/" + persona] = {"source": "personas.py:PERSONAS", "text": text}
    from sonder_runtime.domain.runtime_identity import runtime_identity_block
    out["runtime_identity"] = {
        "local": runtime_identity_block("qwen2.5-coder:7b $x"),
        "cloud": runtime_identity_block("gpt-oss:120b-cloud", cloud=True),
        "inference": runtime_identity_block("qwen3:14b", provider="sonder_inference"),
        "empty": runtime_identity_block(""),
    }
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
