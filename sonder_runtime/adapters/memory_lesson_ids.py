"""Validation for bounded lesson-ID lists used by memory review tools."""
from __future__ import annotations

import json
import re


def _parse_lesson_ids(value):
    if isinstance(value, str):
        text = value.strip()
        if not text:
            values = []
        elif text.startswith("["):
            values = json.loads(text)
        else:
            values = [part for part in re.split(r"[\s,]+", text) if part]
    else:
        values = value
    if not isinstance(values, list):
        raise ValueError("lesson IDs must be a JSON list or comma-separated text")
    if len(values) > 50:
        raise ValueError("at most 50 lesson IDs can be reviewed at once")
    out = []
    for raw in values:
        lesson_id = str(raw or "").strip()
        if not lesson_id or len(lesson_id) > 128 or any(ord(ch) < 32 for ch in lesson_id):
            raise ValueError("invalid lesson ID")
        if lesson_id not in out:
            out.append(lesson_id)
    return out
