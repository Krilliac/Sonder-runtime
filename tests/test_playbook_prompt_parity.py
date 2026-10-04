"""Prompt parity: with no approved playbook the system prompt is unchanged.

``LEGACY_BUILD_SYSTEM`` freezes the intended prefix feature's local section
order (``3be563c4``) independently using join_system_parts; local-system/3
adds the framed owner playbook section,
with current main's hosted-provider fence retained. The current function is extracted from
``server.py`` by AST and run against the same stubs, so no model, disk state or
git ref is involved and the result cannot depend on which checkout ran it.

If a change to the prompt layout is intentional, update the frozen copy in the
same commit: this test exists so that playbooks alone never move prompt bytes.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.playbook_store import PlaybookStore
from sonder_runtime.application.memory.playbook_context import PlaybookContext, frame_owner_notes
from sonder_runtime.domain.prompt_composition import join_system_parts
from sonder_runtime.application.prefix_cache_report import compose_local_system

ROOT = Path(__file__).resolve().parents[1]

LEGACY_BUILD_SYSTEM = r'''
def _build_system(system, trace, persona, model="", cloud=False, provider=None):
    """Compose the effective system prompt from a base `system`, optional trace
    instruction, optional persona, editable profile, and emotion vectors.

    `model`/`cloud` describe the target the caller resolved for THIS request;
    they are threaded to the runtime identity block. Callers that genuinely do
    not know the target omit them, and the identity block is then left out
    rather than guessing one.

    A hosted model receives only request-scoped instructions and the
    non-sensitive runtime identity. Personas, the editable profile, emotion
    vectors, and active goal are disk-backed local control-plane context;
    enabling a cloud tier consents to that request's messages, not to silently
    exporting those instructions on every cloud turn. Hosted agents already
    follow this same boundary. Keeping it here covers ordinary chat and
    structured output too.
    """
    effective_system = system
    if trace:
        trace_text = _prompts.render("trace_instructions")
        effective_system = "%s\n\n%s" % (system, trace_text) if system else trace_text
    if cloud or _provider_bridge.is_hosted(provider):  # hosted: no local profile/goal
        return _join_system_parts(
            _runtime_identity_block(model, cloud, provider), effective_system,
        )
    if persona and persona.strip():
        persona_prompt = personas.get(persona)
        effective_system = (
            "%s\n\n%s" % (persona_prompt, effective_system) if effective_system else persona_prompt
        )
    # Outside a pinned turn this is an ordinary fresh read, so a single-build
    # caller behaves exactly as before.
    parts = getattr(_SYSTEM_CONTEXT, "parts", None)
    profile, emotions, goal_block = parts or _read_system_context()
    return _join_system_parts(
        _runtime_identity_block(model, cloud, provider), profile, effective_system,
        emotions, goal_block,
    )
'''


def _function(source):
    tree = ast.parse(source)
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_build_system")


def _load(function, parts):
    namespace = {
        "_join_system_parts": join_system_parts,
        "_runtime_identity_block": lambda model, cloud, provider: f"IDENTITY:{model}:{cloud}:{provider}",
        "_read_system_context": lambda: parts,
        "_SYSTEM_CONTEXT": SimpleNamespace(parts=None),
        "_prompts": SimpleNamespace(render=lambda name: "TRACE"),
        "personas": SimpleNamespace(get=lambda name: "PERSONA:" + name),
        "_provider_bridge": SimpleNamespace(is_hosted=lambda provider: provider == "hosted"),
        "playbook_context": SimpleNamespace(frame_owner_notes=frame_owner_notes),
        "_prefix_cache": SimpleNamespace(compose_local_system=compose_local_system),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<prompt composition>", "exec"), namespace)
    return namespace["_build_system"]


@pytest.fixture(scope="module")
def sources():
    return _function(LEGACY_BUILD_SYSTEM), _function((ROOT / "server.py").read_text(encoding="utf-8"))


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
    assert rendered.index("PROFILE") < rendered.index("OWNER PLAYBOOK NOTES") < rendered.index("VOLATILE") < rendered.index("GOAL")
    assert "OWNER PLAYBOOK NOTES" not in build("VOLATILE", False, "", provider="hosted")


def test_index_is_stable_manifest_material_and_topic_documents_are_volatile():
    from sonder_runtime.application.context_manifests import ContextRecord, build_prefix_manifest
    index = ContextRecord("playbooks-index", "stable_instructions", "APPROVED INDEX", "playbooks", stable=True)
    first = ContextRecord("playbook-doc", "memories", "NOTE ONE", "playbooks")
    second = ContextRecord("playbook-doc", "memories", "NOTE TWO", "playbooks")
    assert build_prefix_manifest([index, first]).cache_key == build_prefix_manifest([index, second]).cache_key
