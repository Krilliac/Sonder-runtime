"""Prompt building, code extraction, critique parsing and candidate ranking."""
from __future__ import annotations

from sonder_runtime.domain.meshgen.critique import critique_prompt, parse_critique
from sonder_runtime.domain.meshgen.prompts import (
    extract_code, generation_prompt, kit_api_digest, repair_prompt, revise_prompt,
)
from sonder_runtime.domain.meshgen.selection import pick_best, rank_key


def test_extract_code_picks_the_largest_python_block():
    text = ("Here:\n```python\nx = 1\n```\nand better:\n```python\nimport bpy\n\ndef build():\n    pass\n```\n"
            "```bash\nrm -rf everything_and_a_very_long_command_line_to_be_large\n```")
    code = extract_code(text)
    assert "def build" in code and "rm -rf" not in code


def test_extract_code_falls_back_to_plain_fence_unterminated_and_whole_text():
    assert extract_code("```\ndef build():\n    pass\n```").startswith("def build")
    assert extract_code("```python\ndef build():\n    pass\n").startswith("def build")
    assert extract_code("def build():\n    pass").startswith("def build")
    assert extract_code("") == ""


def test_kit_api_digest_lists_public_defs_only():
    kit = '''"""module"""
import math

def wheel(radius: float, n=8) -> object:
    """Make a wheel.

    More detail.
    """

def _hidden():
    """no"""

class Part:
    """A part."""

CONSTANT = 3
'''
    digest = kit_api_digest(kit)
    assert "def wheel(radius: float, n=8) -> object  # Make a wheel." in digest
    assert "class Part  # A part." in digest
    assert "_hidden" not in digest and "CONSTANT" not in digest
    assert kit_api_digest("def (broken") == ""


def test_kit_api_digest_truncates():
    kit = "\n".join("def f%d(a, b):\n    '''doc'''" % i for i in range(200))
    digest = kit_api_digest(kit, max_chars=200)
    assert len(digest) < 260 and digest.endswith("(truncated)")


def test_generation_prompt_is_generic_and_states_the_contract():
    prompt = generation_prompt("a handcart", max_tris=1500, height_m=1.2, kit_module=None, api_doc="")
    for needle in ("a handcart", "1500", "1.20 metres", "Z is up", "-Y", "def build():", 'paint(obj, "#RRGGBB")',
                   "ONLY one ```python"):
        assert needle in prompt
    assert "import propkit" not in prompt and "helper kit" not in prompt.lower()


def test_generation_prompt_with_kit_includes_api_and_import_instruction():
    prompt = generation_prompt("a crate", max_tris=500, height_m=0, kit_module="propkit",
                               api_doc="def wheel(r)")
    assert "import propkit" in prompt and "def wheel(r)" in prompt
    assert "sensible real-world size" in prompt


def test_repair_and_revise_prompts_carry_failure_code_and_issues():
    base = "BASE PROMPT"
    repaired = repair_prompt(base, "print('old')", "NameError: boom")
    assert base in repaired and "print('old')" in repaired and "NameError: boom" in repaired
    revised = revise_prompt(base, "print('old')", {"score": 4, "issues": ["wheels lie flat", "cab floats"]})
    assert "print('old')" in revised and "4/10" in revised
    assert "- wheels lie flat" in revised and "- cab floats" in revised


def test_critique_prompt_describes_the_sheet_and_json_shape():
    prompt = critique_prompt("a handcart")
    assert "a handcart" in prompt and "front" in prompt and "top" in prompt and '"score"' in prompt


def test_parse_critique_reads_json_embedded_in_prose():
    parsed = parse_critique('Sure! {"score": 7, "recognizable": true, "issues": ["cab floats"]} hope that helps')
    assert parsed == {"score": 7, "recognizable": True, "issues": ["cab floats"]}


def test_parse_critique_clamps_and_limits():
    high = parse_critique('{"score": 99, "issues": %s}' % str(["i%d" % n for n in range(20)]).replace("'", '"'))
    assert high["score"] == 10 and len(high["issues"]) == 8
    assert parse_critique('{"score": -3, "issues": []}')["score"] == 0
    assert parse_critique('{"score": "6.4", "issues": "one"}') ["issues"] == ["one"]


def test_parse_critique_tolerates_garbage():
    for text in ("no json here", "{broken", '{"issues": ["x"]}', '{"score": null}', '{"score": true}', "", None):
        parsed = parse_critique(text)
        assert parsed["score"] is None and parsed["issues"] == [] and "raw" in parsed
    assert len(parse_critique("x" * 1000)["raw"]) == 400


def test_rank_key_orders_built_budget_score_then_tris():
    failed = {"built": False}
    over = {"built": True, "within_budget": False, "score": 10, "tris": 9000}
    low = {"built": True, "within_budget": True, "score": 3, "tris": 100}
    high = {"built": True, "within_budget": True, "score": 8, "tris": 900}
    lean = {"built": True, "within_budget": True, "score": 8, "tris": 400}
    unscored = {"built": True, "within_budget": True, "score": None, "tris": 10}
    assert rank_key(failed) < rank_key(over) < rank_key(unscored) < rank_key(low) < rank_key(high) < rank_key(lean)
    assert pick_best([failed, over, low, high, lean]) is lean
    assert pick_best([high, dict(high)]) is high
    assert pick_best([]) is None


def test_critique_prompt_has_an_orientation_and_attachment_checklist_before_scoring():
    prompt = critique_prompt("a wheelbarrow")
    assert prompt.index("ORIENTATION") < prompt.index("Reply with ONLY a JSON object")
    assert prompt.index("ATTACHMENT") < prompt.index("Reply with ONLY a JSON object")
    assert "lying flat" in prompt and "axle" in prompt and "z=0" in prompt
    assert "caps the score at 5" in prompt and "first" in prompt and "issues" in prompt
    # the JSON output contract is unchanged
    assert '"score": <integer 0-10>' in prompt and '"recognizable"' in prompt and '"issues"' in prompt


def test_generation_prompt_pushes_the_budget_and_gives_segment_guidance():
    low = generation_prompt("a crate", max_tris=400, height_m=0, kit_module=None, api_doc="")
    high = generation_prompt("a crate", max_tris=12000, height_m=0, kit_module=None, api_doc="")
    assert "target to USE" in high and "7200 to 10800 triangles" in high
    assert "about 24 segments" in high and "about 10 segments" in low
    for needle in ("BEVEL", "SUBSURF", "shade_smooth", "rot=(0, 90, 0)", "already empty"):
        assert needle in high


def test_generation_prompt_tells_kit_users_to_qualify_calls_and_skip_reset_scene():
    prompt = generation_prompt("a crate", max_tris=1000, height_m=0, kit_module="propkit", api_doc="def f()")
    assert "propkit.name(" in prompt and "do not call reset_scene()" in prompt


def test_repair_hint_qualifies_kit_names_and_drops_reset_scene():
    from sonder_runtime.domain.meshgen.prompts import repair_hint
    err = "NameError: name 'lathe' is not defined"
    assert "propkit.lathe(" in repair_hint(err, "propkit", {"lathe"})
    assert "Delete that call" in repair_hint("NameError: name 'reset_scene' is not defined", "propkit", {"reset_scene"})
    assert "is not defined" in repair_hint("NameError: name 'zzz' is not defined", "propkit", {"lathe"})
    assert repair_hint("ValueError: x", "propkit", {"lathe"}) == ""
    prompt = repair_prompt("BASE", "code", err, hint=repair_hint(err, "propkit", {"lathe"}))
    assert "How to fix it:" in prompt and "propkit.lathe(" in prompt


def test_revise_prompt_turns_orientation_issues_into_rotation_instructions():
    flat = revise_prompt("B", "code", {"score": 5, "issues": ["Front wheel lies flat in the top view"]})
    assert "rot=(0, 90, 0)" in flat and "z=0" in flat and "degrees" in flat
    other = revise_prompt("B", "code", {"score": 5, "issues": ["handle is too short"]})
    assert "Orientation help" not in other
    stuck = revise_prompt("B", "code", {"score": 5, "issues": ["x"]}, stuck="DID NOT FIX IT")
    assert "DID NOT FIX IT" in stuck


def test_critique_prompt_states_conventions_and_asks_for_a_detail_check():
    prompt = critique_prompt("a wheelbarrow", max_tris=8000)
    assert "Z is up" in prompt and "-Y" in prompt and "AXLE IS HORIZONTAL" in prompt
    assert "NOT to make the axle vertical" in prompt
    assert "DETAIL" in prompt and "8000 triangles" in prompt
    assert "DETAIL" in critique_prompt("x")


def test_generation_prompt_describes_the_recorded_dialect_not_bpy():
    prompt = generation_prompt("a crate", max_tris=1000, height_m=0, kit_module="propkit", api_doc="def f()")
    for needle in ("RECORDED", "DEGREES", "def build()", "primitive(", "paint_faces(", "modifier(", "handle",
                   "no bpy", "lambda p: p.center.z"):
        assert needle in prompt, needle
    assert "bpy.ops" not in prompt and "rotation_euler" not in prompt


def test_repair_hint_covers_the_recorder_messages():
    from sonder_runtime.domain.meshgen.prompts import repair_hint
    handle = "line 3: the result of core.primitive(...) can only be passed to other kit/core functions; measurements"
    assert "opaque handles" in repair_hint(handle, "propkit", set())
    assert "no bpy" in repair_hint("line 1: import bpy: bpy is not available; use the kit", "propkit", set())
    assert "propkit.name(" in repair_hint("line 2: os.x is not available in build scripts", "propkit", set())
