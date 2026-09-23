from __future__ import annotations

from scripts.aetherfall_walk_playtest import _verdict


def test_aetherfall_verdict_accepts_bounded_walk_report():
    report = {
        "concordSpawn": {}, "concordToHollow": {}, "pactSpawn": {}, "hollowWalk": {},
        "rim": {direction: {"pastExtent": False, "nan": False, "distLeft": 1.0, "returned": True} for direction in ("east", "west", "north", "south")},
    }
    assert _verdict(report) == (True, [])


def test_aetherfall_verdict_rejects_boundary_escape_and_missing_return():
    report = {
        "concordSpawn": {}, "concordToHollow": {}, "pactSpawn": {}, "hollowWalk": {},
        "rim": {"east": {"pastExtent": True, "nan": False, "returned": False}},
    }
    passed, errors = _verdict(report)
    assert not passed
    assert "rim/east crossed the world extent" in errors
    assert "rim/east did not return to the origin" in errors


def test_aetherfall_verdict_requires_every_rim_and_actual_arrival():
    report = {
        "concordSpawn": {}, "concordToHollow": {}, "pactSpawn": {}, "hollowWalk": {},
        "rim": {"south": {"pastExtent": False, "nan": False,
                           "distLeft": 52.23, "returned": True}},
    }
    passed, errors = _verdict(report)
    assert not passed
    assert "rim/east report is missing" in errors
    assert "rim/south did not reach the rim" in errors
