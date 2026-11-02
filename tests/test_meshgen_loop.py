"""The generate/build/critique loop with fake model, builder and critic."""
from __future__ import annotations

import json
import threading

from sonder_runtime.application.meshgen.loop import MeshRequest, run

GOOD_CODE = "import bpy\n\ndef build():\n    pass\n"


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

    def build(self, index, round_index, code):
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
                   map_parallel=serial)


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


def test_repair_after_a_safety_rejection_never_reaches_the_builder():
    bad = fence("import os\n\ndef build():\n    os.system('x')\n")
    rig = Rig(replies=[bad, fence()], critiques=[verdict(9)])
    built = []
    original = rig.build
    rig.build = lambda i, r, c: built.append(c) or original(i, r, c)
    report = rig.go(candidates=1, rounds=3)
    attempts = report["candidates"][0]["attempts"]
    assert [a["status"] for a in attempts] == ["rejected", "accepted"]
    assert attempts[0]["issues"] and "line 1" in attempts[0]["issues"][0]
    assert "safety check rejected" in rig.prompts[1] and "import os" in rig.prompts[1]
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
    report = run(request, generate=flaky, build=rig.build, critique=rig.critique, map_parallel=serial)
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


def test_the_safety_gate_receives_the_kit_names():
    kit_code = fence("import bpy\nimport propkit\n\ndef build():\n    propkit.os.system('x')\n")
    rig = Rig(replies=[kit_code, fence("import bpy\nimport propkit\n\ndef build():\n    propkit.wheel(1)\n")],
              critiques=[verdict(9)])
    report = rig.go(candidates=1, rounds=2, kit_module="propkit", kit_names=frozenset({"wheel"}))
    assert [a["status"] for a in report["candidates"][0]["attempts"]] == ["rejected", "accepted"]


def test_repeated_build_error_escalates_with_a_note_and_switches_tier():
    err = {"ok": False, "error": "NameError: name 'reset_scene' is not defined"}
    rig = Rig(builds=[err, err, ok_build()], critiques=[verdict(9)])
    request = MeshRequest(brief="a cart", candidates=1, rounds=3, tiers=("code", "general"),
                          kit_module="propkit", kit_names=frozenset({"reset_scene"}))
    report = run(request, generate=rig.generate, build=rig.build, critique=rig.critique, map_parallel=serial)
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
