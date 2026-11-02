"""Vision-critique prompt and tolerant parsing of the reply.  Pure."""
from __future__ import annotations

import json

MAX_ISSUES = 8
_MAX_ISSUE_CHARS = 300


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
        "has a vertical axle and is lying flat; the fix is to rotate it 90 degrees about X or Y (parts "
        "built around the Z axis, such as cylinders and lathes, lie flat until rotated), NOT to make the "
        "axle vertical.",
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
