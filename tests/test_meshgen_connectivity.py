"""The floating-part check without Blender: tolerance, pruning, union-find, report entries, issue wording."""
from __future__ import annotations

from sonder_runtime.adapters.meshgen import blender_runner as runner
from sonder_runtime.domain.meshgen import connectivity as cx
from sonder_runtime.domain.meshgen.critique import FLOATING_SCORE_CAP, cap_score, geometry_issues


def box(x0, z0, x1, z1):
    return (x0, 0.0, z0, x1, 1.0, z1)


def floating(count, touching, grounded, detached=None):
    return cx.floating_components(count, touching, grounded, detached or [False] * count)


# ---------------------------------------------------------------------------------------------- tolerance
def test_tolerance_is_a_one_centimetre_floor_or_one_and_a_half_percent_of_the_height():
    assert cx.tolerance(0.5) == 0.01          # 1.5% of 0.5 m is 0.0075 m: the floor wins
    assert cx.tolerance(0.0) == 0.01
    assert cx.tolerance(2.0) == 0.03          # 1.5% of 2 m
    assert abs(cx.tolerance(10.0) - 0.15) < 1e-12
    # exactly where the two meet: 1.5% of 2/3 m is 0.01 m
    assert abs(cx.tolerance(2.0 / 3.0) - 0.01) < 1e-12
    for bad in (None, "tall", float("nan"), float("inf"), -3.0):
        assert cx.tolerance(bad) == 0.01


def test_a_part_is_grounded_up_to_and_including_the_tolerance():
    assert cx.is_grounded(0.0, 0.01)
    assert cx.is_grounded(-0.2, 0.01)
    assert cx.is_grounded(0.01, 0.01)
    assert not cx.is_grounded(0.0100001, 0.01)


def test_sample_indices_are_capped_even_and_deterministic():
    assert cx.sample_indices(0) == [] and cx.sample_indices(5, 0) == []
    assert cx.sample_indices(10) == list(range(10))
    assert cx.sample_indices(256) == list(range(256))
    big = cx.sample_indices(8000)
    assert len(big) == 256 and big == sorted(set(big)) and big[0] == 0 and big[-1] > 7900
    assert big == cx.sample_indices(8000)


# ---------------------------------------------------------------------------------------------- pruning
def test_box_distance_is_zero_when_boxes_overlap_and_the_gap_otherwise():
    assert cx.box_distance(box(0, 0, 1, 1), box(0.5, 0.5, 2, 2)) == 0.0
    assert cx.box_distance(box(0, 0, 1, 1), box(1, 1, 2, 2)) == 0.0          # touching faces
    assert abs(cx.box_distance(box(0, 0, 1, 1), box(1, 1.5, 2, 2)) - 0.5) < 1e-12
    assert abs(cx.box_distance((0, 0, 0, 1, 1, 1), (2, 2, 0, 3, 3, 1)) - 2 ** 0.5) < 1e-12


def test_candidate_pairs_keep_boxes_within_the_tolerance_and_drop_the_rest():
    near = [box(0, 0, 1, 1), box(1.01, 0, 2, 1)]                    # 0.01 apart in X
    assert cx.candidate_pairs(near, 0.01) == [(0, 1)]               # a gap equal to the tolerance stays a candidate
    assert cx.candidate_pairs(near, 0.0099) == []                   # just under it is pruned
    far_in_z = [box(0, 0, 1, 1), box(0, 1.5, 1, 2)]
    assert cx.candidate_pairs(far_in_z, 0.1) == []
    assert cx.candidate_pairs(far_in_z, 0.5) == [(0, 1)]


def test_candidate_pairs_skip_parts_without_a_box_and_return_ordered_index_pairs():
    boxes = [box(5, 0, 6, 1), None, box(0, 0, 1, 1), box(0.5, 0, 5.2, 1)]
    assert cx.candidate_pairs(boxes, 0.0) == [(0, 3), (2, 3)]


# ---------------------------------------------------------------------------------------------- union-find
def test_a_touching_chain_standing_on_the_ground_has_nothing_floating():
    assert floating(4, [(0, 1), (1, 2), (2, 3)], [True, False, False, False]) == []


def test_grounded_through_a_chain_in_any_edge_order_and_direction():
    edges = [(3, 2), (1, 2), (0, 1)]
    assert floating(4, edges, [True, False, False, False]) == []
    assert floating(4, edges, [False, False, False, True]) == []


def test_an_island_with_no_grounded_part_floats_and_lists_all_its_members():
    result = floating(5, [(0, 1), (2, 3), (3, 4)], [True, False, False, False, False])
    assert result == [[2, 3, 4]]


def test_two_islands_are_reported_separately_in_first_member_order():
    assert floating(4, [], [True, False, False, False]) == [[1], [2], [3]]
    assert floating(5, [(3, 4), (1, 2)], [True, False, False, False, False]) == [[1, 2], [3, 4]]


def test_one_grounded_member_grounds_the_whole_component():
    assert floating(3, [(0, 1), (1, 2)], [False, False, True]) == []


def test_a_detached_part_is_never_reported_and_never_bridges():
    # part 1 is a mote hovering between 0 (grounded) and 2: it must not carry 2 to the ground
    assert floating(3, [(0, 1), (1, 2)], [True, False, False], [False, True, False]) == [[2]]
    # a detached part alone floating is fine
    assert floating(2, [], [True, False], [False, True]) == []
    # a detached part that is grounded does not ground its neighbour either
    assert floating(2, [(0, 1)], [True, False], [True, False]) == [[1]]


def test_degenerate_inputs():
    assert floating(0, [], []) == []
    assert floating(1, [], [False]) == [[0]]
    assert floating(1, [], [True]) == []
    assert floating(2, [(0, 7), (-1, 1)], [True, False]) == [[1]]       # out-of-range edges are ignored


# ---------------------------------------------------------------------------------------------- report shape
def test_a_floating_entry_is_bounded_and_rounded():
    names = ["part%02d_" % i + "x" * 60 for i in range(30)]
    entry = cx.floating_entry(names, [5, 5, 0, -3, 2] + list(range(100, 120)), (1.123456, 0, 0), 0.123456,
                              (0, 0, 0.123456), "n" * 80)
    assert len(entry["parts"]) == cx.MAX_NAMES and all(len(n) <= cx.MAX_NAME_CHARS for n in entry["parts"])
    assert entry["part_count"] == 30
    assert entry["lines"][:3] == [2, 5, 100] and len(entry["lines"]) == cx.MAX_NAMES
    assert entry["centre"] == [1.1235, 0.0, 0.0] and entry["gap"] == 0.1235
    assert len(entry["nearest"]) == cx.MAX_NAME_CHARS
    assert cx.floating_entry(["a"], [], (0, 0, 0), 1, (0, 0, 1), None)["nearest"] is None


def test_the_geometry_block_keeps_at_most_twenty_floating_entries():
    entries = [cx.floating_entry(["p%d" % i], [i + 1], (0, 0, 1), 0.5, (0, 0, 0.5), None) for i in range(35)]
    block = cx.geometry_block(40, entries, 1.23456, (1, 2, 1.23456))
    assert block["parts"] == 40 and len(block["floating"]) == 20 and cx.MAX_FLOATING == 20
    assert block["height"] == 1.2346 and block["dims"] == [1.0, 2.0, 1.2346]


# ---------------------------------------------------------------------------------------------- issue wording
def entry(parts=("loaf",), lines=(34,), gap=0.12, vec=(0, 0, 0.12), nearest="plate", count=None):
    return {"parts": list(parts), "part_count": count or len(parts), "lines": list(lines), "centre": [0, 0, 1],
            "gap": gap, "gap_vec": list(vec), "nearest": nearest}


def issues_for(*entries):
    return geometry_issues({"floating": list(entries)})


def test_the_issue_names_the_line_the_part_the_gap_and_the_fix():
    assert issues_for(entry()) == [
        "line 34: 'loaf' floats 0.12 m above the nearest part ('plate'); move it down to rest on it"]


def test_direction_words_follow_the_gap_vector():
    def say(vec):
        return issues_for(entry(vec=vec, gap=0.2))[0]

    assert "above the nearest part" in say((0.01, -0.02, 0.2)) and "move it down" in say((0, 0, 0.2))
    assert "below the nearest part" in say((0, 0, -0.2)) and "move it up" in say((0, 0, -0.2))
    assert "to the right (+X) of" in say((0.2, 0.05, 0.01)) and "left (toward -X)" in say((0.2, 0, 0))
    assert "to the left (-X) of" in say((-0.2, 0, 0)) and "right (toward +X)" in say((-0.2, 0, 0))
    assert "behind (+Y)" in say((0, 0.2, 0)) and "forward (toward -Y)" in say((0, 0.2, 0))
    assert "in front (-Y) of" in say((0, -0.2, 0)) and "back (toward +Y)" in say((0, -0.2, 0))


def test_the_ground_is_the_target_when_there_is_no_other_part():
    text = issues_for(entry(nearest=None, gap=0.3, vec=(0, 0, 0.3)))[0]
    assert text == "line 34: 'loaf' floats 0.30 m above the ground; move it down to stand on the ground (z=0)"


def test_a_group_of_parts_is_named_once_with_a_count_and_lines():
    text = issues_for(entry(parts=("a", "b", "c", "d", "e"), lines=(7, 9), count=5))[0]
    assert text.startswith("lines 7, 9: 'a', 'b', 'c' and 2 more parts float 0.12 m above")
    assert "move them down" in text


def test_a_missing_line_and_a_zero_vector_still_give_a_usable_issue():
    text = issues_for(entry(lines=(), vec=(0, 0, 0), gap=0.004))[0]
    assert text.startswith("'loaf' floats 0.004 m away from the nearest part ('plate')") and "touches it" in text


def test_issues_are_empty_without_a_floating_part_or_a_geometry_block():
    assert geometry_issues(None) == [] and geometry_issues({}) == [] and geometry_issues({"floating": []}) == []
    assert geometry_issues({"floating": "x"}) == [] and geometry_issues({"floating": [3, {}, {"parts": []}]}) == []
    assert geometry_issues("nope") == []


def test_issue_count_and_length_are_bounded():
    many = [entry(parts=("p%d" % i,), lines=(i + 1,)) for i in range(20)]
    assert len(geometry_issues({"floating": many})) == 8
    assert all(len(text) <= 300 for text in geometry_issues({"floating": [entry(parts=("n" * 500,))]}))


def test_the_score_is_capped_only_when_something_floats():
    assert FLOATING_SCORE_CAP == 6
    assert cap_score(10, ["x"]) == 6 and cap_score(6, ["x"]) == 6 and cap_score(3, ["x"]) == 3
    assert cap_score(10, []) == 10 and cap_score(None, ["x"]) is None


# ---------------------------------------------------------------------------------------------- result file
def test_the_runner_keeps_a_well_formed_geometry_block_and_bounds_it():
    first = dict(entry(parts=["a"] * 12), part_count=12, lines=[3, True, -1, "x", 4])
    block = {"parts": 3, "height": 1.5, "dims": [1, 2, 1.5], "floating": [first] + [entry()] * 40}
    clean = runner.sanitize_result({"ok": True, "geometry": block})["geometry"]
    assert clean["parts"] == 3 and clean["height"] == 1.5 and clean["dims"] == [1.0, 2.0, 1.5]
    assert len(clean["floating"]) == 20
    kept = clean["floating"][0]
    assert len(kept["parts"]) == 8 and kept["part_count"] == 12 and kept["lines"] == [3, 4]
    assert geometry_issues(clean)


def test_the_runner_drops_a_geometry_block_that_is_not_one_and_neutralises_bad_numbers():
    for junk in (None, 3, "x", [], {"floating": "x"}, {"parts": 1}):
        assert "geometry" not in runner.sanitize_result({"ok": True, "geometry": junk})
    bad = {"parts": True, "height": float("nan"), "dims": "x",
           "floating": [entry(gap=float("inf"), vec=("a", None, 1e9)), "junk"]}
    clean = runner.sanitize_result({"ok": True, "geometry": bad})["geometry"]
    assert clean["parts"] == 0 and clean["height"] == 0.0 and clean["dims"] == [0.0, 0.0, 0.0]
    assert len(clean["floating"]) == 1 and clean["floating"][0]["gap"] == 0.0
    assert clean["floating"][0]["gap_vec"] == [0.0, 0.0, 1e9]
