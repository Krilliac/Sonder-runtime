"""Vision-critique prompt and tolerant parsing of the reply.  Pure."""
from __future__ import annotations

import json

MAX_ISSUES = 8
_MAX_ISSUE_CHARS = 300


def critique_prompt(brief: str) -> str:
    return "\n".join([
        "This image is a 2x2 contact sheet of ONE 3D model, rendered flat-shaded with a light background. "
        "Top-left: front view. Top-right: right side view. Bottom-left: three-quarter view. "
        "Bottom-right: top view.",
        "",
        "The model is meant to be: %s" % (brief or "").strip(),
        "",
        "Before scoring, work through this checklist using all four views:",
        "1. ORIENTATION: for each part the brief names or implies, is it oriented as described? "
        "For example wheels stand upright with a horizontal axle (a wheel seen as a flat disc from the "
        "top, with a vertical axle, is lying flat and is WRONG), things rest on the ground plane (z=0), "
        "and the front faces the front view.",
        "2. ATTACHMENT: is every part attached to or touching the rest, rather than floating or "
        "detached with a gap visible in any view?",
        "3. Only then judge recognisability, missing parts and relative size.",
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
