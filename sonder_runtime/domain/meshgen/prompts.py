"""Prompt construction and code extraction for the mesh generation loop.

The wording is generic (no game or engine specifics): the caller's brief and
an optional kit API digest carry everything project-specific.  Pure.
"""
from __future__ import annotations

import ast
import re

_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_OPEN_FENCE = re.compile(r"```[ \t]*(?:python|py)[ \t]*\r?\n(.*)\Z", re.DOTALL | re.IGNORECASE)
_MAX_ERROR_CHARS = 3000
_MAX_CODE_CHARS = 24_000


def kit_api_digest(kit_source: str, max_chars: int = 6000) -> str:
    """Public top-level signatures plus the first docstring line of a kit module."""
    try:
        tree = ast.parse(kit_source or "")
    except (SyntaxError, ValueError):
        return ""
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name.startswith("_"):
                continue
            head = "def %s(%s)" % (node.name, ast.unparse(node.args))
            if node.returns is not None:
                head += " -> " + ast.unparse(node.returns)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            head = "class %s" % node.name
        else:
            continue
        doc = (ast.get_docstring(node) or "").strip().splitlines()
        if doc and doc[0].strip():
            head += "  # " + doc[0].strip()
        lines.append(head)
    text = "\n".join(lines)
    limit = max(0, int(max_chars))
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut + "\n# ... (truncated)"


def generation_prompt(brief: str, *, max_tris: int, height_m: float,
                      kit_module: str | None, api_doc: str) -> str:
    height = (
        "Overall height is about %.2f metres." % height_m if height_m and height_m > 0
        else "Pick a sensible real-world size (the harness can rescale later)."
    )
    if kit_module:
        rotation = ("- bpy rotations are in radians (use math.radians for degrees) unless the kit "
                    "API below says otherwise.")
        colour = ("- Colour each part with the provided helper `paint(obj, \"#RRGGBB\")` (already "
                  "defined; do not import it) unless the kit API below provides its own colouring.")
    else:
        rotation = "- bpy rotations are in radians (use math.radians for degrees)."
        colour = ("- Colour each part with the provided helper `paint(obj, \"#RRGGBB\")` (already "
                  "defined; do not import it).")
    parts = [
        "You write a Blender Python script that builds a 3D model of the subject below.",
        "",
        "SUBJECT: %s" % (brief or "").strip(),
        "",
        "Rules:",
        "- Blender 4/5 Python API (bpy, bmesh, mathutils, math, random). Units are metres; Z is up; "
        "the front of the object faces -Y.",
        rotation,
        "- Low poly: stay under %d triangles in total. Prefer few segments on cylinders and spheres "
        "(8 to 16) and skip detail nobody would see." % int(max_tris),
        "- %s" % height,
        "- Make each distinct part a separate mesh object; parts must touch or overlap so the model "
        "is one connected whole, with every part in its correct position and orientation "
        "(for example wheels stand upright on their axle, not lying flat).",
        "- Nothing animated, no lights or cameras, no materials or textures.",
        "- Define `def build():` which creates the objects in the current scene and returns nothing. "
        "Do not call build() yourself. Do not use files, the network, or names starting with an underscore.",
        colour,
        "- Output ONLY one ```python code block and nothing else.",
    ]
    if kit_module:
        parts += [
            "",
            "A helper kit is available. Start the script with `import %s` and use it where it helps. "
            "Its public API:" % kit_module,
            "```",
            (api_doc or "").strip() or "(no API digest available)",
            "```",
        ]
    return "\n".join(parts)


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def _code_block(code: str) -> str:
    return "```python\n%s\n```" % _clip(code, _MAX_CODE_CHARS)


def repair_prompt(base_prompt: str, code: str, failure: str) -> str:
    return "\n".join([
        base_prompt, "",
        "Your previous script failed. Fix it and return the complete corrected script.",
        "", "Previous script:", _code_block(code),
        "", "Failure:", _clip(failure, _MAX_ERROR_CHARS),
        "", "Return ONLY one ```python code block.",
    ])


def revise_prompt(base_prompt: str, code: str, critique: dict) -> str:
    score = critique.get("score")
    issues = [str(item) for item in (critique.get("issues") or [])]
    listing = "\n".join("- " + item for item in issues) or (
        "- (no specific issues listed; improve overall recognisability)")
    return "\n".join([
        base_prompt, "",
        "Your script built, and a reviewer looked at renders from the front, right side, "
        "three-quarter view and top. Score: %s/10." % ("?" if score is None else score),
        "", "Previous script:", _code_block(code),
        "", "Issues to fix:", listing,
        "", "Return the complete revised script, fixing these issues while keeping what already works. "
        "Return ONLY one ```python code block.",
    ])


def extract_code(text: str) -> str:
    """Largest ```python block; otherwise the largest fenced block; else the text."""
    body = str(text or "")
    blocks = [(lang.lower(), code) for lang, code in _FENCE.findall(body)]
    python = [code for lang, code in blocks if lang in ("python", "py", "python3")]
    pool = python or [code for lang, code in blocks if lang == ""]
    if pool:
        return max(pool, key=len).strip("\r\n") + "\n"
    opened = _OPEN_FENCE.search(body)
    if opened:
        return opened.group(1).strip("\r\n") + "\n"
    return body.strip("\r\n") + "\n" if body.strip() else ""
