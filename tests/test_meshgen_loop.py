"""The generate/build/critique loop with fake model, builder and critic."""
from __future__ import annotations

import json
import threading

from sonder_runtime.application.meshgen.loop import MeshRequest, in_process_recorder, run
from sonder_runtime.domain.meshgen.dsl.manifest import build_manifest

GOOD_CODE = "def build():\n    primitive('cube')\n"


def fence(code=GOOD_CODE):
    return "```python\n%s```" % code


def serial(fn, items):
    return [fn(item) for item in items]


class Rig:
    """Scripted collaborators; every call is recorded."""

    def __init__(self, replies=None, builds=None, critiques=None):
        self.replies = list(replies or [])
        self.builds = list(builds or [])
        self.critiques = list(critiques or [])
        self.prompts = []
        self.tiers = []
        self.lock = threading.Lock()

    def generate(self, tier, prompt):
        with self.lock:
            self.tiers.append(tier)
            self.prompts.append(prompt)
            return self.replies.pop(0) if self.replies else fence()

    def build(self, index, round_index, code, recording=None):
        with self.lock:
            result = self.builds.pop(0) if self.builds else ok_build()
        if isinstance(result, Exception):
            raise result
        return result

    def critique(self, sheet):
        with self.lock:
            return self.critiques.pop(0) if self.critiques else None

    def go(self, **kw):
        request = MeshRequest(brief="a cart", **kw)
        return run(request, generate=self.generate, build=self.build, critique=self.critique,
                   map_parallel=serial, record=in_process_recorder(request.manifest))


def ok_build(tris=100, within=True, sheet="/x/sheet.png"):
    return {"ok": True, "tris": tris, "within_budget": within, "files": {"sheet": sheet, "model": "/x/m.glb"}}


def verdict(score, *issues):
    return json.dumps({"score": score, "recognizable": True, "issues": list(issues)})


def test_repair_after_a_build_error_then_accept():
    rig = Rig(builds=[{"ok": False, "error": "NameError: x", "tail": ["File 1", "boom"]}, ok_build()],
              critiques=[verdict(9)])
    report = rig.go(candidates=1, rounds=3)
    statuses = [a["status"] for a in report["candidates"][0]["attempts"]]
    assert statuses == ["build_error", "accepted"]
    assert "NameError: x" in rig.prompts[1] and "def build" in rig.prompts[1]
    assert report["winner"]["score"] == 9 and report["ok"] is True and report["vision"] == "on"


def test_builder_exception_is_a_failed_build_not_a_crash():
    rig = Rig(builds=[RuntimeError("blender exploded"), ok_build()], critiques=[verdict(8)])
    report = rig.go(candidates=1, rounds=2)
    assert [a["status"] for a in report["candidates"][0]["attempts"]] == ["build_error", "accepted"]
    assert "blender exploded" in rig.prompts[1]


def test_repair_after_a_script_error_never_reaches_the_builder():
    bad = fence("import os\n\ndef build():\n    os.system('x')\n")
    rig = Rig(replies=[bad, fence()], critiques=[verdict(9)])
    built = []
    original = rig.build
    rig.build = lambda i, r, c, rec: built.append(c) or original(i, r, c, rec)
    report = rig.go(candidates=1, rounds=3)
    attempts = report["candidates"][0]["attempts"]
    assert [a["status"] for a in attempts] == ["script_error", "accepted"]
    assert attempts[0]["issues"] and "line 1" in attempts[0]["issues"][0]
    assert "could not be recorded" in rig.prompts[1] and "import os" in rig.prompts[1]
    assert len(built) == 1 and "os.system" not in built[0]


def test_budget_repair_asks_to_cut_detail():
    rig = Rig(builds=[ok_build(tris=9000, within=False), ok_build(tris=800)], critiques=[verdict(9)])
    report = rig.go(candidates=1, rounds=3, max_tris=1000)
    assert [a["status"] for a in report["candidates"][0]["attempts"]] == ["over_budget", "accepted"]
    assert "9000 triangles" in rig.prompts[1] and "budget is 1000" in rig.prompts[1]
    assert report["winner"]["tris"] == 800


def test_revise_after_a_low_score_and_stop_at_target():
    rig = Rig(critiques=[verdict(4, "wheels lie flat"), verdict(6, "cab floats"), verdict(8)])
    report = rig.go(candidates=1, rounds=5, target_score=8)
    attempts = report["candidates"][0]["attempts"]
    assert [a["status"] for a in attempts] == ["revise", "revise", "accepted"]
    assert "wheels lie flat" in rig.prompts[1] and "4/10" in rig.prompts[1]
    assert "cab floats" in rig.prompts[2]
    assert len(rig.prompts) == 3 and report["winner"]["score"] == 8


def test_rounds_exhausted_keeps_the_best_version_not_the_last():
    rig = Rig(builds=[ok_build(tris=100), ok_build(tris=100)], critiques=[verdict(7, "a"), verdict(3, "b")])
    report = rig.go(candidates=1, rounds=2, target_score=9)
    assert report["winner"]["round"] == 0 and report["winner"]["score"] == 7


def test_tier_rotation_across_candidates():
    rig = Rig(critiques=[verdict(9)] * 5)
    rig.go(candidates=5, rounds=1, tiers=("general", "code"))
    assert rig.tiers == ["general", "code", "general", "code", "general"]


def test_winner_is_the_best_ranked_candidate():
    # c0: in budget, score 6, 300 tris.  c1: over budget (never critiqued).  c2: score 6, 200 tris.
    builds = [ok_build(tris=300), ok_build(tris=9999, within=False), ok_build(tris=200)]
    rig = Rig(builds=builds, critiques=[verdict(6), verdict(6)])
    report = rig.go(candidates=3, rounds=1, target_score=9)
    assert report["winner"]["candidate"] == 2 and report["winner"]["tris"] == 200
    assert [c["index"] for c in report["candidates"]] == [0, 1, 2]


def test_a_higher_score_beats_fewer_triangles():
    builds = [ok_build(tris=100), ok_build(tris=900)]
    rig = Rig(builds=builds, critiques=[verdict(5), verdict(7)])
    report = rig.go(candidates=2, rounds=1, target_score=9)
    assert report["winner"]["candidate"] == 1 and report["winner"]["score"] == 7


def test_all_candidates_failing_reports_no_winner():
    rig = Rig(builds=[{"ok": False, "error": "bad"}] * 4)
    report = rig.go(candidates=2, rounds=2)
    assert report["ok"] is False and report["winner"] is None
    assert all(not c["built"] for c in report["candidates"])


def test_vision_unavailable_builds_once_and_says_so():
    rig = Rig(critiques=[None])
    report = rig.go(candidates=1, rounds=3)
    assert [a["status"] for a in report["candidates"][0]["attempts"]] == ["built"]
    assert report["vision"].startswith("unavailable") and report["winner"]["score"] is None


def test_vision_disabled_by_the_caller_never_calls_the_critic():
    rig = Rig(critiques=[verdict(1)])
    report = rig.go(candidates=1, rounds=3, vision=False)
    assert rig.critiques and report["vision"] == "unavailable: disabled by the caller"
    assert report["candidates"][0]["attempts"][0]["status"] == "built"


def test_unparseable_critique_stops_without_a_score():
    rig = Rig(critiques=["I cannot see any image"])
    report = rig.go(candidates=1, rounds=3)
    assert report["candidates"][0]["attempts"][0]["status"] == "built"
    assert report["winner"]["score"] is None


def test_generate_failure_retries_the_round_with_the_same_prompt():
    calls = {"n": 0}

    def flaky(tier, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("model down")
        return fence()

    rig = Rig(critiques=[verdict(9)])
    request = MeshRequest(brief="a cart", candidates=1, rounds=3)
    report = run(request, generate=flaky, build=rig.build, critique=rig.critique, map_parallel=serial,
                 record=in_process_recorder(request.manifest))
    attempts = report["candidates"][0]["attempts"]
    assert [a["status"] for a in attempts] == ["generate_error", "accepted"]
    assert "TimeoutError" in attempts[0]["error"]


def test_report_is_json_able_and_has_no_code_bodies():
    rig = Rig(critiques=[verdict(9)])
    report = rig.go(candidates=1, rounds=1)
    text = json.dumps(report)
    assert "def build" not in text and report["elapsed_s"] >= 0


def test_vision_is_not_reported_on_when_no_candidate_reached_the_critique():
    rig = Rig(builds=[{"ok": False, "error": "bad"}] * 2)
    report = rig.go(candidates=1, rounds=2)
    assert report["vision"] == "unavailable: no candidate reached the critique step"
    rig = Rig(builds=[ok_build(tris=9000, within=False)] * 2)
    assert rig.go(candidates=1, rounds=2, max_tris=10)["vision"].startswith("unavailable")


def test_the_recorder_receives_the_kit_manifest():
    bad = fence("import propkit\n\ndef build():\n    propkit.os.system('x')\n")
    good = fence("import propkit\n\ndef build():\n    propkit.wheel(1)\n")
    rig = Rig(replies=[bad, good], critiques=[verdict(9)])
    manifest = build_manifest("def wheel(r):\n    pass\n", "propkit")
    report = rig.go(candidates=1, rounds=2, kit_module="propkit", kit_names=frozenset({"wheel"}), manifest=manifest)
    assert [a["status"] for a in report["candidates"][0]["attempts"]] == ["script_error", "accepted"]


def test_the_build_receives_the_recording_and_print_output_reaches_the_repair_prompt():
    seen = []
    rig = Rig(replies=[fence("def build():\n    primitive('cube')\n    print('probe-line')\n"), fence()],
              builds=[{"ok": False, "error": "boom"}, ok_build()], critiques=[verdict(9)])
    original = rig.build

    def build(i, r, c, rec):
        seen.append(rec)
        return original(i, r, c, rec)

    rig.build = build
    rig.go(candidates=1, rounds=2)
    assert seen[0].ops[0]["op"] == "core.primitive" and seen[0].text.startswith("{")
    assert "probe-line" in rig.prompts[1]


def test_repeated_build_error_escalates_with_a_note_and_switches_tier():
    err = {"ok": False, "error": "NameError: name 'reset_scene' is not defined"}
    rig = Rig(builds=[err, err, ok_build()], critiques=[verdict(9)])
    request = MeshRequest(brief="a cart", candidates=1, rounds=3, tiers=("code", "general"),
                          kit_module="propkit", kit_names=frozenset({"reset_scene"}))
    report = run(request, generate=rig.generate, build=rig.build, critique=rig.critique, map_parallel=serial,
                 record=in_process_recorder(request.manifest))
    attempts = report["candidates"][0]["attempts"]
    assert [a["status"] for a in attempts] == ["build_error", "build_error", "accepted"]
    assert not attempts[0].get("stuck") and attempts[1]["stuck"] is True
    assert attempts[1]["escalated_to"] == "general"
    assert rig.tiers == ["code", "code", "general"]
    assert "Delete that call" in rig.prompts[1]              # NameError hint on the first repair
    assert "did not fix: NameError: name 'reset_scene'" in rig.prompts[2]
    assert "Change exactly that line" in rig.prompts[2]


def test_repeated_revision_with_the_same_top_issue_and_score_escalates():
    issue = "Front wheel is a flat disc in the top view"
    rig = Rig(builds=[ok_build(tris=2000), ok_build(tris=2014), ok_build(tris=2010)],
              critiques=[verdict(5, issue), verdict(5, "front wheel still a flat disc in top view"), verdict(8)])
    report = rig.go(candidates=1, rounds=3, tiers=("general", "code"))
    attempts = report["candidates"][0]["attempts"]
    assert [a["status"] for a in attempts] == ["revise", "revise", "accepted"]
    assert attempts[1]["stuck"] is True and attempts[1]["escalated_to"] == "code"
    assert rig.tiers == ["general", "general", "code"]
    assert "Orientation help" in rig.prompts[1]
    assert "did not fix: front wheel still a flat disc" in rig.prompts[2]
    assert "score stayed at 5" in rig.prompts[2]


def test_a_changed_score_or_issue_is_progress_not_stuck():
    rig = Rig(critiques=[verdict(4, "wheel lies flat"), verdict(6, "handle is too short"), verdict(8)])
    report = rig.go(candidates=1, rounds=3)
    assert not any(a.get("stuck") for a in report["candidates"][0]["attempts"])
    assert rig.tiers == ["general"] * 3


def test_single_tier_stuck_adds_the_note_without_switching():
    err = {"ok": False, "error": "ValueError: boom"}
    rig = Rig(builds=[err, err, err])
    report = rig.go(candidates=1, rounds=3, tiers=("general",))
    assert report["candidates"][0]["attempts"][1]["stuck"] is True
    assert "escalated_to" not in report["candidates"][0]["attempts"][1]
    assert rig.tiers == ["general"] * 3


def test_over_budget_repair_names_the_biggest_parts():
    big = ok_build(tris=9000, within=False)
    big["parts"] = [{"name": "wheel", "tris": 6000}, {"name": "tray", "tris": 2000}]
    rig = Rig(builds=[big, ok_build(tris=500)], critiques=[verdict(9)])
    rig.go(candidates=1, rounds=2, max_tris=1000)
    assert "wheel (6000 tris), tray (2000 tris)" in rig.prompts[1]


def test_a_stuck_candidate_moves_to_a_tier_it_has_not_used_and_the_winner_is_credited_with_it():
    same = {"ok": False, "error": "Same: boom", "tail": []}
    rig = Rig(builds=[dict(same), dict(same), ok_build()], critiques=[verdict(9)])
    report = rig.go(candidates=1, rounds=4, tiers=("a", "b", "c"))
    assert rig.tiers[:2] == ["a", "a"] and rig.tiers[2] == "b"
    attempts = report["candidates"][0]["attempts"]
    assert attempts[1]["stuck"] is True and attempts[1]["escalated_to"] == "b"
    assert report["winner"]["tier"] == "b"
    assert report["candidates"][0]["tiers_used"] == ["a", "b"]


def test_the_no_progress_note_sits_before_the_closing_instruction():
    same = {"ok": False, "error": "Same: boom", "tail": []}
    rig = Rig(builds=[dict(same), dict(same), ok_build()], critiques=[verdict(9)])
    rig.go(candidates=1, rounds=3, tiers=("general",))
    third = rig.prompts[2]
    assert third.rstrip().splitlines()[-1].startswith("Return ONLY")
    note = third.lower().rindex("no progress") if "no progress" in third.lower() else third.lower().rindex("same")
    assert 0 < note < third.rindex("Return ONLY")


# ---------------------------------------------------------------------------------------------- floating parts
def geometry(*entries):
    return {"parts": 3, "floating": list(entries), "height": 1.0, "dims": [1.0, 1.0, 1.0]}


LOAF = {"parts": ["loaf"], "part_count": 1, "lines": [34], "centre": [0, 0, 1], "gap": 0.12,
        "gap_vec": [0, 0, 0.12], "nearest": "plate"}
LOAF_ISSUE = "line 34: 'loaf' floats 0.12 m above the nearest part ('plate'); move it down to rest on it"


def floating_build(*entries):
    return dict(ok_build(), geometry=geometry(*(entries or (LOAF,))))


def test_a_floating_candidate_is_capped_at_six_even_when_the_critic_says_ten():
    rig = Rig(builds=[floating_build()], critiques=[verdict(10, "the lid is slightly crooked")])
    report = rig.go(candidates=1, rounds=1, target_score=8)
    attempt = report["candidates"][0]["attempts"][0]
    assert attempt["status"] == "revise" and attempt["score"] == 6
    assert report["winner"]["score"] == 6 and report["ok"] is True
    assert attempt["issues"] == [LOAF_ISSUE, "the lid is slightly crooked"]


def test_the_control_without_a_geometry_block_is_accepted_at_ten():
    rig = Rig(builds=[ok_build()], critiques=[verdict(10, "the lid is slightly crooked")])
    report = rig.go(candidates=1, rounds=1, target_score=8)
    attempt = report["candidates"][0]["attempts"][0]
    assert attempt["status"] == "accepted" and attempt["score"] == 10 and "geometry" not in attempt
    assert attempt["issues"] == ["the lid is slightly crooked"]


def test_a_geometry_block_with_nothing_floating_changes_nothing_but_is_recorded():
    clean = dict(ok_build(), geometry=geometry())
    rig = Rig(builds=[clean], critiques=[verdict(10)])
    report = rig.go(candidates=1, rounds=1, target_score=8)
    attempt = report["candidates"][0]["attempts"][0]
    assert attempt["status"] == "accepted" and attempt["score"] == 10
    assert attempt["geometry"] == geometry() and report["winner"]["geometry"] == geometry()


def test_the_measured_issues_come_first_in_the_revise_prompt_and_the_next_round_can_win():
    rig = Rig(builds=[floating_build(), dict(ok_build(), geometry=geometry())],
              critiques=[verdict(9, "the handle is too thin"), verdict(9)])
    report = rig.go(candidates=1, rounds=3, target_score=8)
    revise = rig.prompts[1]
    assert LOAF_ISSUE in revise and "the handle is too thin" in revise
    assert revise.index(LOAF_ISSUE) < revise.index("the handle is too thin")
    assert "Score: 6/10" in revise and "Score: 9/10" not in revise
    statuses = [a["status"] for a in report["candidates"][0]["attempts"]]
    assert statuses == ["revise", "accepted"] and report["winner"]["score"] == 9
    assert report["candidates"][0]["attempts"][0]["geometry"]["floating"][0]["parts"] == ["loaf"]


def test_a_floating_candidate_is_not_accepted_even_when_the_target_is_below_the_cap():
    rig = Rig(builds=[floating_build()], critiques=[verdict(10)])
    report = rig.go(candidates=1, rounds=1, target_score=5)
    assert report["candidates"][0]["attempts"][0]["status"] == "revise"


def test_a_floating_part_is_revised_even_when_no_critique_is_available():
    rig = Rig(builds=[floating_build(), ok_build()], critiques=[None])
    report = rig.go(candidates=1, rounds=2, target_score=8)
    attempts = report["candidates"][0]["attempts"]
    assert attempts[0]["status"] == "revise" and attempts[0]["score"] is None
    assert attempts[0]["issues"] == [LOAF_ISSUE] and LOAF_ISSUE in rig.prompts[1]
    assert attempts[1]["status"] == "built"


def test_a_floating_part_is_revised_with_vision_switched_off():
    rig = Rig(builds=[floating_build(), ok_build()])
    report = rig.go(candidates=1, rounds=2, vision=False)
    assert [a["status"] for a in report["candidates"][0]["attempts"]] == ["revise", "built"]
    assert LOAF_ISSUE in rig.prompts[1]


def test_the_same_floating_issue_twice_counts_as_no_progress():
    rig = Rig(builds=[floating_build(), floating_build(), ok_build()],
              critiques=[verdict(7), verdict(7), verdict(9)])
    report = rig.go(candidates=1, rounds=3, target_score=8)
    assert report["candidates"][0]["attempts"][1].get("stuck") is True
    assert "did not fix" in rig.prompts[2]


def test_malformed_geometry_is_ignored():
    for junk in ("x", 3, [], {"floating": "x"}, {"floating": [None, 3, {}]}):
        rig = Rig(builds=[dict(ok_build(), geometry=junk)], critiques=[verdict(10)])
        attempt = rig.go(candidates=1, rounds=1)["candidates"][0]["attempts"][0]
        assert attempt["status"] == "accepted" and attempt["score"] == 10


def test_the_winner_is_the_clean_version_even_when_the_floating_one_scored_higher():
    rig = Rig(builds=[floating_build(), ok_build(tris=300)], critiques=[verdict(10), verdict(5, "too plain")])
    report = rig.go(candidates=1, rounds=2, target_score=8)
    assert report["winner"]["score"] == 5 and "geometry" not in report["winner"]


# ---------------------------------------------------------------------------------------------- detached accountability
def detached_build(name="loaf", line=34, floating_entries=()):
    block = geometry(*floating_entries)
    block["detached"] = [{"name": name, "line": line}]
    return dict(ok_build(), geometry=block)


def test_detached_parts_reach_the_report_and_the_revise_prompt():
    rig = Rig(builds=[detached_build("wisp", 12), ok_build()], critiques=[verdict(5, "too plain"), verdict(9)])
    report = rig.go(candidates=1, rounds=2, target_score=8)
    attempt = report["candidates"][0]["attempts"][0]
    assert attempt["detached"] == [{"name": "wisp", "line": 12}] and attempt["status"] == "revise"
    assert "you marked 'wisp' (line 12) detached; only do that if the brief says it floats" in rig.prompts[1]
    assert "reopened" not in attempt


def test_a_detached_part_is_not_a_floating_one_when_it_never_floated():
    rig = Rig(builds=[detached_build("wisp", 12)], critiques=[verdict(10)])
    attempt = rig.go(candidates=1, rounds=1, target_score=8)["candidates"][0]["attempts"][0]
    assert attempt["status"] == "accepted" and attempt["score"] == 10


def test_detaching_a_part_that_floated_last_round_keeps_the_cap_and_blocks_acceptance():
    rig = Rig(builds=[floating_build(), detached_build("loaf", 34), detached_build("loaf", 34)],
              critiques=[verdict(7), verdict(10), verdict(10)])
    report = rig.go(candidates=1, rounds=3, target_score=8)
    attempts = report["candidates"][0]["attempts"]
    assert [a["status"] for a in attempts] == ["revise", "revise", "revise"]
    assert attempts[1]["score"] == 6 and attempts[2]["score"] == 6                  # the critic said 10 both times
    assert "'loaf' was floating in an earlier round and you now mark it detached" in attempts[1]["issues"][0]
    assert attempts[1]["reopened"] and report["winner"]["score"] == 6
    assert "was floating in an earlier round" in rig.prompts[2]


def test_the_line_moving_does_not_hide_it_the_name_still_matches():
    rig = Rig(builds=[floating_build(), detached_build("loaf", 90)], critiques=[verdict(7), verdict(10)])
    attempts = rig.go(candidates=1, rounds=2, target_score=8)["candidates"][0]["attempts"]
    assert attempts[1]["status"] == "revise" and attempts[1]["score"] == 6


def test_a_version_that_reopened_a_floating_part_ranks_below_a_clean_one():
    rig = Rig(builds=[floating_build(), detached_build("loaf", 34), ok_build(tris=500)],
              critiques=[verdict(7), verdict(10), verdict(4, "plain")])
    report = rig.go(candidates=1, rounds=3, target_score=8)
    assert report["winner"]["score"] == 4 and not report["winner"].get("reopened")


def test_the_measured_revise_prompt_without_a_reviewer_says_so():
    rig = Rig(builds=[floating_build(), ok_build()])
    rig.go(candidates=1, rounds=2, vision=False)
    assert "reviewer looked" not in rig.prompts[1] and "No reviewer scored it" in rig.prompts[1]
