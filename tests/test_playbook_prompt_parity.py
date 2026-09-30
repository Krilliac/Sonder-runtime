"""Compare actual legacy prompt composition with origin/main, without a model."""
import ast
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.playbook_store import PlaybookStore
from sonder_runtime.application.memory.playbook_context import PlaybookContext, frame_owner_notes
from sonder_runtime.domain.prompt_composition import join_system_parts

ROOT = Path(__file__).resolve().parents[1]


def _load(source, parts):
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_build_system")
    namespace = {
        "_join_system_parts": join_system_parts,
        "_runtime_identity_block": lambda model, cloud, provider: f"IDENTITY:{model}:{cloud}:{provider}",
        "_read_system_context": lambda: parts,
        "_SYSTEM_CONTEXT": SimpleNamespace(parts=None),
        "_prompts": SimpleNamespace(render=lambda name: "TRACE"),
        "personas": SimpleNamespace(get=lambda name: "PERSONA:" + name),
        "_provider_bridge": SimpleNamespace(is_hosted=lambda provider: provider == "hosted"),
        "playbook_context": SimpleNamespace(frame_owner_notes=frame_owner_notes),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<prompt composition>", "exec"), namespace)
    return namespace["_build_system"]


@pytest.fixture(scope="module")
def sources():
    baseline = subprocess.run(
        ["git", "show", "origin/main:server.py"], cwd=ROOT, check=True,
        capture_output=True, encoding="utf-8",
    ).stdout
    return baseline, (ROOT / "server.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("system", ["", "Caller system", "Line one\nLine two é"])
@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("persona", ["", "coder"])
@pytest.mark.parametrize("cloud,provider", [(False, None), (False, "hosted"), (True, None)])
@pytest.mark.parametrize("proposed", [False, True])
def test_empty_or_proposed_only_prompts_are_byte_identical_to_origin(
    sources, tmp_path, system, trace, persona, cloud, provider, proposed,
):
    store = PlaybookStore(tmp_path)
    if proposed:
        store.note("builds", "procedure", "Compiler", "Use the verified build wrapper.", triggers=["build"])
    index = PlaybookContext(lambda: store).stable_index("session")
    assert index == ""
    old = _load(sources[0], ("PROFILE", "EMOTIONS", "GOAL"))
    new = _load(sources[1], ("PROFILE", "EMOTIONS", "GOAL", index))
    options = dict(model="test-model", cloud=cloud, provider=provider)
    assert new(system, trace, persona, **options).encode("utf-8") == old(system, trace, persona, **options).encode("utf-8")


def test_index_precedes_volatile_system_and_is_omitted_on_hosted_rungs(sources):
    index = "- [Builds](builds.md) — procedure — open when: build\n"
    build = _load(sources[1], ("PROFILE", "EMOTIONS", "GOAL", index))
    rendered = build("VOLATILE", False, "", model="local")
    assert rendered.index("PROFILE") < rendered.index("OWNER PLAYBOOK NOTES") < rendered.index("GOAL") < rendered.index("VOLATILE")
    assert "OWNER PLAYBOOK NOTES" not in build("VOLATILE", False, "", provider="hosted")


def test_index_is_stable_manifest_material_and_topic_documents_are_volatile():
    from sonder_runtime.application.context_manifests import ContextRecord, build_prefix_manifest
    index = ContextRecord("playbooks-index", "stable_instructions", "APPROVED INDEX", "playbooks", stable=True)
    first = ContextRecord("playbook-doc", "memories", "NOTE ONE", "playbooks")
    second = ContextRecord("playbook-doc", "memories", "NOTE TWO", "playbooks")
    assert build_prefix_manifest([index, first]).cache_key == build_prefix_manifest([index, second]).cache_key
