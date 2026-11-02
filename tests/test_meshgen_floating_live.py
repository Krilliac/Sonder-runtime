"""The floating-part check in real headless Blender, through the actual runner (and the sandbox where the platform has one).

Skipped unless SONDER_BLENDER points at blender.  Every "none floating" case has a floating twin in the same file
whose assertion is non-empty, so a check that silently stopped measuring would fail the twin.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.meshgen import blender_runner
from sonder_runtime.domain.meshgen.critique import geometry_issues
from sonder_runtime.domain.meshgen.dsl.recorder import compile_script

BLENDER = os.environ.get("SONDER_BLENDER", "")

pytestmark = pytest.mark.skipif(not BLENDER or not os.path.isfile(BLENDER),
                                reason="SONDER_BLENDER is not set to a Blender executable")

# a 1 m cube standing on the ground (top at z = 1); the ball has radius 0.2, so its lowest point is z - 0.2
BASE = '''def build():
    box = primitive("cube", "Box", size=1.0, loc=(0, 0, 0.5))
'''


def build(tmp_path, source, max_tris=5000, **kw):
    recording = compile_script(source)
    tmp_path.mkdir(parents=True, exist_ok=True)
    ops = tmp_path / "ops.json"
    ops.write_text(recording.text, encoding="utf-8")
    kw.setdefault("render", False)
    result = blender_runner.build_candidate(
        BLENDER, str(ops), str(tmp_path / "out"), name="thing", max_tris=max_tris, height_m=kw.pop("height_m", 0),
        timeout=240, **kw)
    assert result.get("ok"), result
    return result


def floating_of(result):
    assert "geometry" in result, "the harness returned no geometry block: %r" % sorted(result)
    return result["geometry"]["floating"]


def test_a_ball_hovering_above_a_box_gives_one_floating_entry_with_its_line(tmp_path):
    source = BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(0, 0, 1.5))\n'
    result = build(tmp_path, source)
    entries = floating_of(result)
    assert len(entries) == 1                                              # non-empty: a no-op check fails here
    entry = entries[0]
    assert entry["parts"] == ["Ball"] and entry["lines"] == [3] and entry["nearest"] == "Box"
    assert abs(entry["gap"] - 0.3) < 0.01 and abs(entry["gap_vec"][2] - 0.3) < 0.01
    assert abs(entry["gap_vec"][0]) < 0.01 and abs(entry["gap_vec"][1]) < 0.01
    assert all(abs(a - b) < 0.01 for a, b in zip(entry["centre"], [0.0, 0.0, 1.5], strict=True))
    block = result["geometry"]
    assert block["parts"] == 2 and abs(block["height"] - 1.7) < 0.01 and len(block["dims"]) == 3
    assert geometry_issues(block)[0].startswith("line 3: 'Ball' floats 0.30 m above the nearest part ('Box')")


def test_the_same_ball_resting_on_the_box_gives_none(tmp_path):
    touching = BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(0, 0, 1.15))\n'
    result = build(tmp_path, touching)
    assert floating_of(result) == [] and result["geometry"]["parts"] == 2    # measured, and found connected
    # a gap under the tolerance (1.5% of a 1.4 m prop is 0.021 m) still touches ...
    assert floating_of(build(tmp_path / "near", BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, '
                             'loc=(0, 0, 1.215))\n')) == []
    # ... and a gap over it does not (twin of the two cases above)
    far = floating_of(build(tmp_path / "far", BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, '
                            'loc=(0, 0, 1.26))\n'))
    assert len(far) == 1 and abs(far[0]["gap"] - 0.06) < 0.01


def test_detached_leaves_the_ball_out_of_the_report(tmp_path):
    source = BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(0, 0, 1.5))\n    detached(ball)\n'
    result = build(tmp_path, source)
    assert floating_of(result) == [] and result["geometry"]["parts"] == 2
    # the control: without the detached() line the same build reports it
    assert len(floating_of(build(tmp_path / "plain", source.replace("    detached(ball)\n", "")))) == 1


def test_detached_excludes_only_the_marked_part(tmp_path):
    source = (BASE + '    mote = primitive("uv_sphere", "Mote", radius=0.1, loc=(0.6, 0, 1.4))\n'
              '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(-0.6, 0, 1.5))\n    detached(mote)\n')
    entries = floating_of(build(tmp_path, source))
    assert [entry["parts"] for entry in entries] == [["Ball"]] and entries[0]["lines"] == [4]


def test_a_detached_part_does_not_carry_a_neighbour_to_the_ground(tmp_path):
    # the orb touches the box; the charm hangs from the orb. The orb is detached, so the charm is not grounded by it.
    source = (BASE + '    orb = primitive("uv_sphere", "Orb", radius=0.2, loc=(0, 0, 1.1))\n'
              '    charm = primitive("uv_sphere", "Charm", radius=0.1, loc=(0, 0, 1.38))\n    detached(orb)\n')
    entries = floating_of(build(tmp_path, source))
    assert [entry["parts"] for entry in entries] == [["Charm"]]


def test_a_chain_of_touching_parts_is_grounded_and_a_raised_top_is_not(tmp_path):
    chain = (BASE + '    plate = primitive("cube", "Plate", size=0.5, loc=(0, 0, 1.2))\n'
             '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(0, 0, 1.6))\n')
    assert floating_of(build(tmp_path, chain)) == []                         # ball -> plate -> box -> ground
    bench = '''def build():
    leg_a = primitive("cube", "LegA", size=0.2, loc=(-0.5, 0, 0.1))
    leg_b = primitive("cube", "LegB", size=0.2, loc=(0.5, 0, 0.1))
    top = primitive("cube", "Top", size=1.4, scale=(1, 0.5, 0.1), loc=(0, 0, 0.5))
'''
    entries = floating_of(build(tmp_path / "bench", bench))
    assert len(entries) == 1 and entries[0]["parts"] == ["Top"] and entries[0]["lines"] == [4]
    assert entries[0]["gap_vec"][2] > 0.1                                    # it floats above the legs
    seated = bench.replace("loc=(0, 0, 0.5))", "loc=(0, 0, 0.27))")
    assert floating_of(build(tmp_path / "seated", seated)) == []


def test_parts_that_touch_each_other_but_not_the_ground_are_one_entry(tmp_path):
    source = BASE + '''    a = primitive("uv_sphere", "A", radius=0.2, loc=(0, 0, 1.6))
    b = primitive("uv_sphere", "B", radius=0.2, loc=(0.3, 0, 1.6))
'''
    entries = floating_of(build(tmp_path, source))
    assert len(entries) == 1 and sorted(entries[0]["parts"]) == ["A", "B"] and entries[0]["part_count"] == 2
    assert entries[0]["lines"] == [3, 4]


def test_a_join_is_reported_at_the_line_of_the_join(tmp_path):
    source = BASE + '''    a = primitive("uv_sphere", "A", radius=0.2, loc=(0, 0, 1.6))
    b = primitive("uv_sphere", "B", radius=0.2, loc=(0.3, 0, 1.6))
    both = join([a, b], "Pair")
'''
    entries = floating_of(build(tmp_path, source))
    assert len(entries) == 1 and entries[0]["parts"] == ["Pair (piece 1)", "Pair (piece 2)"] and entries[0]["lines"] == [5]


def test_the_tolerance_scales_with_the_height_of_the_prop(tmp_path):
    # a 10 m cube with a ball 0.1 m above it: 1.5% of the 12 m prop is 0.18 m, so it touches ...
    tall = '''def build():
    box = primitive("cube", "Box", size=10.0, loc=(0, 0, 5))
    ball = primitive("uv_sphere", "Ball", radius=1.0, loc=(0, 0, %s))
'''
    assert floating_of(build(tmp_path, tall % "11.1")) == []
    # ... and 0.5 m above it does not
    assert len(floating_of(build(tmp_path / "gap", tall % "11.5"))) == 1


def test_a_build_rescaled_by_height_m_is_measured_in_its_final_size(tmp_path):
    source = BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(0, 0, 1.5))\n'
    entry = floating_of(build(tmp_path, source, height_m=3.4))[0]             # the 1.7 m prop doubled
    assert abs(entry["gap"] - 0.6) < 0.02 and abs(entry["centre"][2] - 3.0) < 0.02


def test_no_more_than_twenty_floating_entries_are_reported(tmp_path):
    lines = ["def build():", '    ground = primitive("cube", "Ground", size=1.0, loc=(0, 0, 0.5))']
    lines += ['    p%d = primitive("cube", "P%d", size=0.1, loc=(%d, 0, 3))' % (i, i, i * 2) for i in range(1, 26)]
    result = build(tmp_path, "\n".join(lines) + "\n")
    assert len(floating_of(result)) == 20 and result["geometry"]["parts"] == 26


def test_a_prop_with_nothing_floating_and_one_part_has_an_empty_report(tmp_path):
    result = build(tmp_path, BASE)
    assert result["geometry"]["floating"] == [] and result["geometry"]["parts"] == 1


def test_the_replay_refuses_detached_on_something_that_is_not_a_part(tmp_path):
    def doc(*ops):
        full = [{"id": i, "line": i + 1, "op": name, "args": args, "kwargs": {}} for i, (name, args) in enumerate(ops, 1)]
        return json.dumps({"version": 1, "kit": "", "seed": 0, "ops": full, "log": []})

    def run(text, name):
        ops = tmp_path / (name + ".json")
        ops.write_text(text, encoding="utf-8")
        return blender_runner.build_candidate(BLENDER, str(ops), str(tmp_path / name), name="thing", max_tris=100,
                                              height_m=0, timeout=240, render=False)

    literal = run(doc(("core.primitive", ["cube"]), ("core.detached", ["Cube"])), "literal")
    assert literal["ok"] is False and "detached" in literal["error"]           # the grammar refuses a non-reference
    deleted = run(doc(("core.primitive", ["cube"]), ("core.delete", [{"$ref": 1}]),
                      ("core.primitive", ["cube"]), ("core.detached", [{"$ref": 2}])), "deleted")
    assert deleted["ok"] is False and "line 5" in deleted["error"] and "detached" in deleted["error"]
    assert run(doc(("core.primitive", ["cube"]), ("core.detached", [{"$ref": 1}])), "fine")["ok"] is True


def test_the_result_carries_the_geometry_block_and_no_error(tmp_path):
    result = build(tmp_path, BASE)
    assert "geometry_error" not in result
    assert Path(result["files"]["model"]).is_file()


# ---------------------------------------------------------------------------------------------- flush contacts
# Layouts where parts rest on each other with no vertex of either part on the other: a face, a line or a patch.
# Each is built touching (lift 0) and lifted by 0.05 m, more than three times the tolerance of a prop this size.
FLUSH = {
    "crossed planks": ('''def build():
    a = primitive("cube", "A", size=1.0, loc=(0, 0, 0.05), scale=(2.0, 0.1, 0.1))
    b = primitive("cube", "B", size=1.0, loc=(0, 0, 0.15 + %s), scale=(0.1, 2.0, 0.1))
''', "B"),
    "log across a stump": ('''def build():
    stump = primitive("cube", "Stump", size=0.4, loc=(0, 0, 0.2))
    log = primitive("cylinder", "Log", radius=0.2, depth=2.0, loc=(0, 0, 0.6 + %s), rot=(0, 90, 0))
''', "Log"),
    "board on two rails": ('''def build():
    r1 = primitive("cube", "RailL", size=1.0, loc=(-0.8, 0, 0.2), scale=(0.1, 0.6, 0.4))
    r2 = primitive("cube", "RailR", size=1.0, loc=(0.8, 0, 0.2), scale=(0.1, 0.6, 0.4))
    board = primitive("cube", "Board", size=1.0, loc=(0, 0, 0.425 + %s), scale=(2.0, 0.4, 0.05))
''', "Board"),
    "box resting on its edge": ('''def build():
    a = primitive("cube", "A", size=1.0, loc=(0, 0, 0.5))
    b = primitive("cube", "B", size=0.5, loc=(0, 0, 1.35355339 + %s), rot=(45, 0, 0), scale=(4, 1, 1))
''', "B"),
}


@pytest.mark.parametrize("layout", sorted(FLUSH))
def test_parts_resting_flush_on_each_other_are_not_floating(tmp_path, layout):
    template, mover = FLUSH[layout]
    resting = build(tmp_path, template % "0.0")
    assert floating_of(resting) == [] and resting["geometry"]["parts"] >= 2
    # the twin: the same layout lifted by 0.05 m must report the part that moved
    lifted = floating_of(build(tmp_path / "lifted", template % "0.05"))
    assert [entry["parts"] for entry in lifted] == [[mover]]
    assert 0.03 < lifted[0]["gap"] < 0.07


def test_a_true_floater_at_a_gap_of_35_centimetres_is_still_caught(tmp_path):
    source = BASE + '    b = primitive("cube", "B", size=0.5, loc=(0, 0, 1.6))\n'
    entry = floating_of(build(tmp_path, source))[0]
    assert entry["parts"] == ["B"] and abs(entry["gap"] - 0.35) < 0.01 and entry["gap_vec"][2] > 0.3


def test_a_part_wholly_inside_another_is_connected_to_it(tmp_path):
    inside = '''def build():
    shell = primitive("cube", "Shell", size=2.0, loc=(0, 0, 1.0))
    core = primitive("cube", "Core", size=0.5, loc=(0, 0, %s))
'''
    assert floating_of(build(tmp_path, inside % "1.0")) == []
    assert [e["parts"] for e in floating_of(build(tmp_path / "out", inside % "3.0"))] == [["Core"]]


def test_the_nearest_part_named_is_one_that_stands_on_the_ground_not_another_floater(tmp_path):
    source = BASE + '''    low = primitive("uv_sphere", "Low", radius=0.2, loc=(0, 0, 1.5))
    high = primitive("uv_sphere", "High", radius=0.2, loc=(0, 0, 2.15))
'''
    entries = {entry["parts"][0]: entry for entry in floating_of(build(tmp_path, source))}
    assert sorted(entries) == ["High", "Low"]
    assert entries["Low"]["nearest"] == "Box" and abs(entries["Low"]["gap"] - 0.3) < 0.01
    assert entries["High"]["nearest"] == "Box" and abs(entries["High"]["gap"] - 0.95) < 0.02   # not Low, 0.25 away


def test_the_detached_parts_are_listed_in_the_geometry_block(tmp_path):
    source = BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(0, 0, 1.5))\n    detached(ball)\n'
    assert build(tmp_path, source)["geometry"]["detached"] == [{"name": "Ball", "line": 3, "box": [-0.2, -0.2, 1.3, 0.2, 0.2, 1.7]}]
    assert build(tmp_path / "none", BASE)["geometry"]["detached"] == []


def test_an_over_budget_build_is_not_measured(tmp_path):
    source = BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(0, 0, 1.5))\n'
    result = build(tmp_path, source, max_tris=100)
    assert result["within_budget"] is False and "geometry" not in result and "geometry_error" not in result
    assert "geometry" in build(tmp_path / "fits", source)       # the control: within budget it is measured


def test_a_pile_of_overlapping_parts_skips_the_check_instead_of_running_away(tmp_path):
    lines = ["def build():"] + ['    primitive("cube", "C%d", size=0.1, loc=(0, 0, %f))' % (i, i * 1e-5) for i in range(700)]
    result = build(tmp_path, "\n".join(lines) + "\n", max_tris=100000)
    assert "geometry" not in result and result["geometry_error"] == "skipped: too many parts"
    small = build(tmp_path / "few", "\n".join(lines[:30]) + "\n", max_tris=100000)
    assert small["geometry"]["parts"] == 29 and "geometry_error" not in small


COPY_KIT = '''"""A trusted test kit that copies objects."""

def box(name, size, loc):
    import bpy
    bpy.ops.mesh.primitive_cube_add(size=size, location=loc)
    obj = bpy.context.active_object
    obj.name = name
    return obj

def clone(obj, name, loc):
    import bpy
    copy = obj.copy()
    copy.data = obj.data.copy()
    copy.name = name
    copy.location = loc
    bpy.context.scene.collection.objects.link(copy)
    return copy
'''


def test_a_copy_made_by_a_kit_gets_its_own_line_and_is_not_detached_because_its_source_was(tmp_path):
    from sonder_runtime.domain.meshgen.dsl.manifest import build_manifest

    kit = tmp_path / "kit" / "copykit.py"
    kit.parent.mkdir()
    kit.write_text(COPY_KIT, encoding="utf-8")
    source = ("import copykit\ndef build():\n    a = copykit.box('A', 1.0, (0, 0, 0.5))\n"
              "    b = copykit.box('B', 0.2, (0, 0, 3.0))\n    detached(b)\n    c = copykit.clone(b, 'C', (2.0, 0, 3.0))\n")
    manifest = build_manifest(kit.read_text(encoding="utf-8"), "copykit")
    recording = compile_script(source, manifest)
    ops = tmp_path / "ops.json"
    ops.write_text(recording.text, encoding="utf-8")
    result = blender_runner.build_candidate(BLENDER, str(ops), str(tmp_path / "out"), name="thing", max_tris=5000,
                                            height_m=0, kit_path=str(kit), kit_allowed=sorted(manifest.exposed),
                                            timeout=240, render=False)
    assert result["ok"], result
    entries = floating_of(result)
    assert [(e["parts"], e["lines"]) for e in entries] == [(["C"], [6])]      # B is detached, its copy C is not


# ---------------------------------------------------------------------------------------------- islands, open meshes, reopening
TABLE = '''def build():
    leg = primitive("cube", "Leg", size=1.0, loc=(0, 0, 0.4), scale=(0.1, 0.1, 0.8))
    top = primitive("cube", "Top", size=1.0, loc=(0, 0, %s), scale=(1, 1, 0.05))
%s'''


def test_a_join_does_not_hide_a_floating_piece_behind_a_grounded_one(tmp_path):
    joined = TABLE % ("1.2", '    table = join([leg, top], "Table")\n')
    entries = floating_of(build(tmp_path, joined))
    assert len(entries) == 1 and entries[0]["lines"] == [4]                       # the line of the join
    assert entries[0]["parts"] == ["Table (piece 2)"] and entries[0]["nearest"] == "Table (piece 1)"
    assert entries[0]["gap_vec"][2] > 0.3
    # the control: the same join with the top seated on the leg is one connected whole
    seated = TABLE % ("0.825", '    table = join([leg, top], "Table")\n')
    assert floating_of(build(tmp_path / "seated", seated)) == []


def test_a_part_with_a_single_island_keeps_its_plain_name(tmp_path):
    result = build(tmp_path, BASE + '    ball = primitive("uv_sphere", "Ball", radius=0.2, loc=(0, 0, 1.5))\n')
    assert floating_of(result)[0]["parts"] == ["Ball"]


TRAY = '''def build():
    v = [(-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0), (-0.5, -0.5, 0.6), (0.5, -0.5, 0.6),
         (0.5, 0.5, 0.6), (-0.5, 0.5, 0.6)]
    f = [(0, 3, 2, 1), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    tray = mesh("Tray", v, f)
    apple = primitive("cube", "Apple", size=0.1, loc=(%s, 0.0, 0.3))
'''


def test_a_part_inside_an_open_tray_is_not_connected_by_the_inside_test(tmp_path):
    entries = floating_of(build(tmp_path, TRAY % "0.3"))
    assert [entry["parts"] for entry in entries] == [["Apple"]] and entries[0]["nearest"] == "Tray"
    # the control: an apple resting against the wall touches it, and the closed shell test above still holds
    assert floating_of(build(tmp_path / "touching", TRAY % "0.45")) == []


def round_geometry(tmp_path, source):
    return build(tmp_path, source)["geometry"]


def remembered(tmp_path):
    from sonder_runtime.domain.meshgen.critique import remember_floating

    memory = {"boxes": []}
    first = round_geometry(tmp_path / "round0", TABLE % ("1.2", ""))
    assert len(first["floating"]) == 1 and first["floating"][0]["box"]            # the floater and where it was
    remember_floating(memory, first)
    return memory


def test_detaching_a_floater_after_renaming_it_and_moving_its_line_is_still_reopened(tmp_path):
    from sonder_runtime.domain.meshgen.critique import reopened_issues

    memory = remembered(tmp_path)
    renamed = '''def build():
    # a sturdy table, honest

    leg = primitive("cube", "Leg", size=1.0, loc=(0, 0, 0.4), scale=(0.1, 0.1, 0.8))
    slab = primitive("cube", "Tabletop", size=1.0, loc=(0, 0, 1.2), scale=(1, 1, 0.05))
    detached(slab)
'''
    geometry = round_geometry(tmp_path / "renamed", renamed)
    assert geometry["floating"] == [] and geometry["detached"][0]["name"] == "Tabletop"
    assert geometry["detached"][0]["line"] == 5
    assert reopened_issues(geometry, memory)
    # the control: a mote hovering elsewhere is a legitimate detached part
    far = TABLE % ("0.825", '    mote = primitive("uv_sphere", "Mote", radius=0.05, loc=(0, 0, 2.5))\n    detached(mote)\n')
    assert reopened_issues(round_geometry(tmp_path / "far", far), memory) == []


def test_detaching_a_floater_after_splitting_it_into_two_parts_is_still_reopened(tmp_path):
    from sonder_runtime.domain.meshgen.critique import reopened_issues

    memory = remembered(tmp_path)
    split = '''def build():
    leg = primitive("cube", "Leg", size=1.0, loc=(0, 0, 0.4), scale=(0.1, 0.1, 0.8))
    left = primitive("cube", "HalfL", size=1.0, loc=(-0.25, 0, 1.2), scale=(0.5, 1, 0.05))
    right = primitive("cube", "HalfR", size=1.0, loc=(0.25, 0, 1.2), scale=(0.5, 1, 0.05))
    detached(left)
    detached(right)
'''
    geometry = round_geometry(tmp_path / "split", split)
    assert geometry["floating"] == [] and len(geometry["detached"]) == 2
    assert len(reopened_issues(geometry, memory)) == 2


def test_detaching_a_floater_after_joining_it_into_a_new_part_is_still_reopened(tmp_path):
    from sonder_runtime.domain.meshgen.critique import reopened_issues

    memory = remembered(tmp_path)
    joined = '''def build():
    leg = primitive("cube", "Leg", size=1.0, loc=(0, 0, 0.4), scale=(0.1, 0.1, 0.8))
    top = primitive("cube", "Top", size=1.0, loc=(0, 0, 1.2), scale=(1, 1, 0.05))
    knob = primitive("uv_sphere", "Knob", radius=0.05, loc=(0, 0, 1.25))
    lid = join([top, knob], "Lid")
    detached(lid)
'''
    geometry = round_geometry(tmp_path / "joined", joined)
    assert geometry["floating"] == [] and geometry["detached"][0]["name"] == "Lid"
    assert reopened_issues(geometry, memory)


def test_a_floater_joined_into_a_part_of_sixty_five_pieces_is_still_reported(tmp_path):
    lines = ["def build():"]
    lines += ['    c%d = primitive("cube", "C%d", size=0.2, loc=(%f, %f, 0.1))' % (i, i, (i % 8) * 0.5, (i // 8) * 0.5)
              for i in range(64)]
    lines += ['    hat = primitive("cube", "Hat", size=0.2, loc=(0, 0, 1.0))',
              '    pile = join([%s, hat], "Pile")' % ", ".join("c%d" % i for i in range(64))]
    entries = floating_of(build(tmp_path, chr(10).join(lines) + chr(10), max_tris=5000))
    assert len(entries) == 1 and entries[0]["lines"] == [67] and entries[0]["parts"][0].startswith("Pile (piece")
    assert entries[0]["gap"] > 0.5                                       # the hat, 0.8 m above the grounded cubes
