"""Vision-critique prompt and tolerant parsing of the reply.  Pure."""
from __future__ import annotations

import json
import re

from .connectivity import boxes_near, tolerance

MAX_ISSUES = 8
_MAX_ISSUE_CHARS = 300
MAX_REMEMBERED_BOXES = 200
# words that mean the brief itself asks for something that floats (whole words, any case)
_FLOAT_WORDS = re.compile(
    r"\b(?:float|floating|floats|hover|hovering|levitat\w*|orbs?|motes?|wisps?|sparks?|fireflies|firefly|drifting|"
    r"suspended)\b", re.IGNORECASE)
FLOATING_SCORE_CAP = 6          # a candidate with a measured floating part never scores above this
MAX_GEOMETRY_ISSUES = 8
# (axis, sign of the gap vector) -> (where the floating part sits relative to what it should touch, how to move it).
# The front of the model faces -Y, so -Y is "in front" and +Y is "behind".
_DIRECTIONS = {
    (2, 1): ("above", "down"), (2, -1): ("below", "up"),
    (0, 1): ("to the right (+X) of", "left (toward -X)"), (0, -1): ("to the left (-X) of", "right (toward +X)"),
    (1, 1): ("behind (+Y)", "forward (toward -Y)"), (1, -1): ("in front (-Y) of", "back (toward +Y)"),
}


def critique_prompt(brief: str, max_tris: int | None = None) -> str:
    detail = (
        "4. DETAIL: the model may use up to about %d triangles. If it looks blocky or low-effort for that "
        "budget (plain boxes, visibly faceted round parts, no bevels, no smooth shading where it belongs), "
        "add an issue that names the part and the upgrade (this alone does not cap the score at 5)."
        % int(max_tris) if max_tris else
        "4. DETAIL: if it looks blocky or low-effort (plain boxes, visibly faceted round parts, no "
        "bevels), add an issue that names the part and the upgrade (this alone does not cap the score at 5)."
    )
    return "\n".join([
        "This image is a 2x2 contact sheet of ONE 3D model, rendered flat-shaded with a light background. "
        "Top-left: front view. Top-right: right side view. Bottom-left: three-quarter view. "
        "Bottom-right: top view.",
        "",
        "The model is meant to be: %s" % (brief or "").strip(),
        "",
        "Scene conventions you judge against: Z is up, the ground is z=0, the front of the object faces "
        "-Y (the front view). A wheel is upright when its AXLE IS HORIZONTAL: it then shows as a full "
        "circle from the side and as a thin bar from the top. A wheel that shows as a flat disc from the top "
        "has a vertical axle and is lying flat. For cart, barrow and vehicle wheels the axle must run along X "
        "(side to side, perpendicular to the direction of travel), so the fix is rot=(0, 90, 0), a rotation of "
        "90 degrees about Y (parts built around the Z axis, such as cylinders and lathes, lie flat until "
        "rotated); never tell the author to rotate such a wheel about X, which puts the axle along Y, "
        "sideways. Other wheels (a ship's or steering wheel, a waterwheel, a valve) follow how the brief mounts "
        "them. The fix is NOT to make the axle vertical.",
        "",
        "Before scoring, work through this checklist using all four views:",
        "1. ORIENTATION: for each part the brief names or implies, is it oriented as described? "
        "For example wheels upright with a horizontal axle, things resting on the ground plane (z=0), "
        "and the front facing the front view.",
        "2. ATTACHMENT: is every part attached to or touching the rest, rather than floating or "
        "detached with a gap visible in any view?",
        "3. Only then judge recognisability, missing parts and relative size.",
        detail,
        "Any wrong orientation or detached part caps the score at 5 or lower, and it MUST be the first "
        "entry in issues.",
        "",
        'Reply with ONLY a JSON object: {"score": <integer 0-10>, "recognizable": <true|false>, '
        '"issues": [<up to 8 short concrete fixes that name the part and the direction, '
        'e.g. "rear wheels lie flat; rotate 90 degrees so they stand upright on the axle">]}.',
        "10 means a clean, correct, recognisable model. Give an empty issues list only when nothing needs fixing.",
    ])


def _quoted(names, count) -> str:
    shown = ", ".join("'%s'" % name for name in names[:3])
    rest = max(int(count), len(names)) - min(len(names), 3)
    return shown + (" and %d more parts" % rest if rest > 0 else "")


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _names(entry) -> list:
    return [str(name) for name in (entry.get("parts") if isinstance(entry.get("parts"), list) else [])
            if isinstance(name, str) and name.strip()]


def floating_entries(geometry) -> list:
    """The well-formed entries of a ``geometry`` block's ``floating`` list: dicts that name at least one part.

    The one definition of "something floats" for the issue text, the ranking and the loop, so they cannot disagree."""
    entries = geometry.get("floating") if isinstance(geometry, dict) else None
    return [entry for entry in (entries if isinstance(entries, list) else [])
            if isinstance(entry, dict) and _names(entry)]


def brief_allows_floating(brief) -> bool:
    """True when the brief names something that floats (float, hover, orb, mote, wisp, spark, firefly, ...)."""
    return bool(_FLOAT_WORDS.search(str(brief or "")))


def _box(value):
    ok = (isinstance(value, (list, tuple)) and len(value) == 6
          and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value))
    if not ok:
        return None
    box = tuple(float(v) for v in value)
    return box if all(box[k] <= box[k + 3] for k in range(3)) else None


def _volume(box) -> float:
    return max(0.0, box[3] - box[0]) * max(0.0, box[4] - box[1]) * max(0.0, box[5] - box[2])


def _detached_items(geometry) -> list:
    found = geometry.get("detached") if isinstance(geometry, dict) else None
    return [item for item in (found if isinstance(found, list) else [])
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()]


def detached_parts(geometry) -> list:
    """``(name, line)`` of every part the script marked ``detached(...)``, from a ``geometry`` block."""
    return [(item["name"], item["line"] if isinstance(item.get("line"), int) and item["line"] > 0 else 0)
            for item in _detached_items(geometry)]


def remember_floating(memory, geometry) -> None:
    """Remember where the floating components of ``geometry`` are (their boxes), in a candidate's ``memory``."""
    for entry in floating_entries(geometry):
        box = _box(entry.get("box"))
        if box is not None and len(memory.setdefault("boxes", [])) < MAX_REMEMBERED_BOXES:
            memory["boxes"].append(box)


def detached_notes(geometry, skip=()) -> list:
    """One reminder per detached part for the revise prompt: detach only what the brief says floats."""
    return ["you marked '%s'%s detached; only do that if the brief says it floats (motes, wisps, orbs), otherwise "
            "attach it to the model" % (name, " (line %d)" % line if line else "")
            for name, line in detached_parts(geometry)[:MAX_GEOMETRY_ISSUES] if name not in skip]


def reopened_note(name) -> str:
    """The advisory for a reopened part when the brief does ask for floating things."""
    return ("you marked '%s' detached; keep that only if '%s' is one of the floating things the brief asks for"
            % (name, name))


def reopened_parts(geometry, memory) -> list:
    """``(name, line)`` of detached parts that sit where a floating component was reported in an earlier round.

    Matched by place only: the part's bounding box overlaps, or lies within the tolerance of, a remembered floating
    box.  Names and line numbers are not used (lines are recycled between rewrites and names collide), and a
    detached box bigger than the whole prop is ignored (a cover-everything box would otherwise match anything).
    Renaming, moving to another line, splitting or joining a floater does not change where it is."""
    found = []
    remembered = memory.get("boxes", [])
    tol = tolerance(geometry.get("height") if isinstance(geometry, dict) else None)
    dims = geometry.get("dims") if isinstance(geometry, dict) else None
    whole = None
    if isinstance(dims, list) and len(dims) == 3 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                         for v in dims):
        whole = dims[0] * dims[1] * dims[2]
    for item in _detached_items(geometry)[:MAX_GEOMETRY_ISSUES]:
        box = _box(item.get("box"))
        if box is None or (whole is not None and _volume(box) > whole * 1.001 + 1e-9):
            continue
        if any(boxes_near(box, old, tol) for old in remembered):
            found.append((item["name"], item["line"] if isinstance(item.get("line"), int) and item["line"] > 0 else 0))
    return found


def reopened_issue(name, line) -> str:
    """The issue for a reopened part when the brief mentions nothing that floats (the host cannot know more)."""
    return (("line %d: " % line if line else "") + "'%s' was floating in an earlier round and is now marked detached; "
            "the brief mentions nothing that floats, so attach it to the prop instead" % name)


def reopened_issues(geometry, memory) -> list:
    """``reopened_issue`` for every part in ``reopened_parts``."""
    return [reopened_issue(name, line) for name, line in reopened_parts(geometry, memory)]


def geometry_issues(geometry) -> list:
    """One concrete, deterministic issue per floating component of a harness ``geometry`` block (else ``[]``).

    Reads ``floating`` entries (``parts``, ``part_count``, ``lines``, ``gap``, ``gap_vec`` from the nearest other
    part to the floating one, ``nearest`` part name or ``None`` for the ground).  Pure and tolerant: anything
    that is not a well-formed entry is skipped."""
    issues = []
    for entry in floating_entries(geometry)[:MAX_GEOMETRY_ISSUES]:
        names = _names(entry)
        count = entry.get("part_count") if isinstance(entry.get("part_count"), int) else len(names)
        lines = [line for line in (entry.get("lines") or []) if isinstance(line, int) and line > 0][:3]
        where = ("line%s %s: " % ("s" if len(lines) > 1 else "", ", ".join(str(line) for line in lines))) if lines else ""
        nearest = entry.get("nearest")
        ground = not (isinstance(nearest, str) and nearest)
        target = "the ground" if ground else "the nearest part ('%s')" % nearest
        gap = _number(entry.get("gap"))
        vector = entry.get("gap_vec") if isinstance(entry.get("gap_vec"), list) and len(entry["gap_vec"]) == 3 else []
        values = [_number(item) for item in vector]
        plural = max(int(count), len(names)) > 1
        verb, pronoun = ("float", "them") if plural else ("floats", "it")
        distance = "%.3f" % gap if gap < 0.01 else "%.2f" % gap
        head = "%s%s %s %s m" % (where, _quoted(names, count), verb, distance)
        if values and max(abs(item) for item in values) > 1e-9:
            axis = 2 if ground else max(range(3), key=lambda k: abs(values[k]))
            side, move = _DIRECTIONS[(axis, 1 if values[axis] >= 0 else -1)]
            end = "stand on the ground (z=0)" if ground else ("rest on it" if axis == 2 else "touch it")
            text = "%s %s %s; move %s %s to %s" % (head, side, target, pronoun, move, end)
        else:
            text = "%s away from %s; move %s until %s" % (
                head, target, pronoun, "it touches it" if not plural else "they touch it")
        if plural:
            text += " (together they touch each other but nothing that stands on the ground)"
        if gap < 3 * tolerance(geometry.get("height") if isinstance(geometry, dict) else None):
            text += " (the gap may be from low-poly faceting; overlap the parts slightly)"
        issues.append(text[:_MAX_ISSUE_CHARS])
    return issues


def cap_score(score, floating_issues):
    """The critic's score, capped at ``FLOATING_SCORE_CAP`` while any floating part was measured."""
    if score is None or not floating_issues:
        return score
    return min(score, FLOATING_SCORE_CAP)


def _first_object(text: str):
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value, _end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            return value
        index = text.find("{", index + 1)
    return None


def parse_critique(text: str) -> dict:
    raw = str(text or "")
    failure = {"score": None, "issues": [], "raw": raw[:400]}
    data = _first_object(raw)
    if data is None:
        return failure
    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float, str)):
        return failure
    try:
        score = int(round(float(score)))
    except (ValueError, OverflowError):
        return failure
    score = max(0, min(10, score))
    issues_raw = data.get("issues")
    if isinstance(issues_raw, str):
        issues_raw = [issues_raw]
    if not isinstance(issues_raw, list):
        issues_raw = []
    issues = [str(item).strip()[:_MAX_ISSUE_CHARS] for item in issues_raw
              if str(item).strip()][:MAX_ISSUES]
    recognizable = data.get("recognizable")
    return {
        "score": score,
        "recognizable": bool(recognizable) if recognizable is not None else score >= 5,
        "issues": issues,
    }
