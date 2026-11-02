"""Kit exposure policy: which kit names a model-written script may reach."""
from __future__ import annotations

from sonder_runtime.application.meshgen.loop import MeshRequest, run
from sonder_runtime.domain.meshgen.kit_policy import analyse_kit
from sonder_runtime.domain.meshgen.prompts import generation_prompt, kit_api_digest, repair_hint
from sonder_runtime.domain.meshgen.script_safety import check_script

KIT = '''"""A kit."""
import os
import bpy
from pathlib import Path

REPO_ROOT = Path(__file__).parent
OUT = os.path.join("a", "b")
COL = "#ff00ff"
PALETTE = {"a": (1, 0, 0)}
SCALE = 2.0

def box(w, h):
    """A box."""
    bpy.ops.mesh.primitive_cube_add(size=w)

def lathe(profile):
    """A lathe."""
    return box(1, 2)

def export_it(path):
    bpy.ops.export_scene.gltf(filepath=path)

def run_prop(name):
    export_it(OUT)

def makes_dir():
    os.makedirs("x")

def uses_excluded_const():
    return OUT

def render_preview():
    bpy.ops.render.render(write_still=True)

def writes():
    with open("f", "w") as handle:
        handle.write("x")

def reset_scene():
    bpy.ops.wm.read_factory_settings()

class Part:
    def save(self):
        pass

def _private():
    pass
'''


def test_io_functions_and_transitive_users_are_excluded():
    allowed, excluded = analyse_kit(KIT)
    assert {"box", "lathe", "PALETTE", "SCALE", "reset_scene"} <= allowed  # reset_scene only resets Blender
    for name in ("export_it", "run_prop", "makes_dir", "uses_excluded_const", "render_preview", "writes",
                 "REPO_ROOT", "OUT", "COL"):
        assert name in excluded and name not in allowed, name
    assert not any(name.startswith("_") for name in allowed | excluded)


def test_an_unparseable_kit_exposes_nothing():
    assert analyse_kit("def (:") == (frozenset(), frozenset())


def test_allow_override_replaces_the_derived_list():
    allowed, excluded = analyse_kit(KIT + "\nMESHGEN_ALLOW = ['lathe', 'run_prop', 'not_defined']\n")
    assert allowed == {"lathe", "run_prop"}
    assert "box" in excluded and "MESHGEN_ALLOW" not in allowed | excluded


def test_exclude_override_removes_names():
    allowed, excluded = analyse_kit(KIT + "\nMESHGEN_EXCLUDE = ('lathe',)\n")
    assert "lathe" in excluded and "box" in allowed


def test_gate_rejects_excluded_kit_names_and_accepts_allowed_ones():
    allowed, _ = analyse_kit(KIT)

    def script(call):
        return "import bpy\nimport kit\n\ndef build():\n    %s\n" % call

    assert check_script(script("kit.box(1, 2)"), "kit", allowed) == []
    assert check_script(script("kit.reset_scene()"), "kit", allowed) == []
    for call in ("kit.run_prop('x')", "kit.export_it('p')", "kit.REPO_ROOT", "kit.render_preview()",
                 "getattr(kit, 'run_prop')()", "kit.__dict__"):
        assert check_script(script(call), "kit", allowed), call


def test_digest_and_hint_never_mention_excluded_names():
    allowed, excluded = analyse_kit(KIT)
    digest = kit_api_digest(KIT, allowed=allowed)
    assert "box(" in digest and "lathe(" in digest
    for name in excluded:
        assert name not in digest, name
    hint = repair_hint("NameError: name 'run_prop' is not defined", "kit", allowed, excluded)
    assert "harness exports and renders" in hint and "not available" in hint
    unqualified = repair_hint("NameError: name 'box' is not defined", "kit", allowed, excluded)
    assert "kit.box" in unqualified


def test_prompt_says_the_harness_exports_and_renders():
    prompt = generation_prompt("a cart", max_tris=3000, height_m=0.0, kit_module="kit", api_doc="api text")
    assert "harness exports and renders" in prompt


def test_loop_passes_excluded_names_into_the_repair_hint():
    allowed, excluded = analyse_kit(KIT)
    seen = []

    def generate(tier, prompt):
        seen.append(prompt)
        return "```python\nimport bpy\n\ndef build():\n    pass\n```"

    results = [{"ok": False, "error": "NameError: name 'run_prop' is not defined", "tail": []},
               {"ok": True, "tris": 5, "within_budget": True, "files": {"model": "/m.glb"}}]
    request = MeshRequest(brief="x", kit_module="kit", kit_names=allowed, kit_excluded=excluded,
                          candidates=1, rounds=2, vision=False)
    run(request, generate=generate, build=lambda i, r, c: results.pop(0), critique=lambda s: None,
        map_parallel=lambda fn, items: [fn(i) for i in items])
    assert "not available in this tool" in seen[1]


def test_system_and_popen_exclude_only_on_dangerous_receivers():
    source = (
        "import os\nimport bpy\n"
        "def shell():\n    os.system('x')\n"
        "def harmless(thing):\n    thing.system = 1\n    thing.popen()\n"
    )
    allowed, excluded = analyse_kit(source)
    assert "shell" in excluded and "harmless" in allowed


def test_a_colour_constant_is_not_a_callable_and_stays_usable_inside_the_kit():
    allowed, excluded = analyse_kit("COL = 'Col'\n\ndef paint_it():\n    return COL\n")
    assert "COL" in excluded and "paint_it" in allowed
