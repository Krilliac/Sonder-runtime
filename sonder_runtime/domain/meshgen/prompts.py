"""Prompt construction and code extraction for the mesh generation loop.

The wording is generic (no game or engine specifics): the caller's brief and
an optional kit API digest carry everything project-specific.  The model writes a script in the
recorded dialect (``dsl``): kit and core calls only, rotations in degrees.  Pure.
"""
from __future__ import annotations

import ast
import re

from .dsl.core_api import DIALECT_RULES, core_api_digest

_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_OPEN_FENCE = re.compile(r"```[ \t]*(?:python|py)[ \t]*\r?\n(.*)\Z", re.DOTALL | re.IGNORECASE)
_MAX_ERROR_CHARS = 3000
_MAX_CODE_CHARS = 24_000
_NAME_ERROR = re.compile(r"NameError: name '([A-Za-z_][A-Za-z0-9_]*)' is not defined")
_HANDLE_USE = "can only be passed to other kit/core functions"
_ORIENTATION_WORDS = ("flat", "upright", "axle", "lying", "lies", "orient", "rotate", "rotation", "tilt",
                      "sideways", "vertical", "horizontal", "facing")


def _segments_for(max_tris) -> int:
    """Rough segment count for round parts, scaled to the triangle budget."""
    budget = int(max_tris)
    for limit, segments in ((500, 10), (2000, 12), (6000, 16), (20000, 24)):
        if budget < limit:
            return segments
    return 32


def kit_api_digest(kit_source: str, max_chars: int = 6000, allowed=None) -> str:
    """Public top-level signatures plus the first docstring line of a kit module.

    With ``allowed`` (the names the safety policy exposes) every other name is omitted, so the
    model is never shown a function it may not call.
    """
    try:
        tree = ast.parse(kit_source or "")
    except (SyntaxError, ValueError):
        return ""
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name.startswith("_") or (allowed is not None and node.name not in allowed):
                continue
            head = "def %s(%s)" % (node.name, ast.unparse(node.args))
            if node.returns is not None:
                head += " -> " + ast.unparse(node.returns)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_") or (allowed is not None and node.name not in allowed):
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
    parts = [
        "You write a short Python build script for a 3D model of the subject below. The script is "
        "RECORDED, not run in Blender: it can only call the kit and core functions listed here.",
        "",
        "SUBJECT: %s" % (brief or "").strip(),
        "",
        "Rules:",
        DIALECT_RULES.strip(),
        "- Units are metres; Z is up; the front of the object faces -Y.",
        "- Budget: %d triangles in total, counted after modifiers. Treat it as a target to USE, not a "
        "ceiling to hide under: aim for roughly %d to %d triangles and spend them where they read "
        "(bevels on hard edges, about %d segments around wheels, barrels and other round parts so they "
        "read round, smooth shading on organic parts) while skipping detail nobody would see. Modifiers "
        "are baked automatically." % (
            int(max_tris), int(max_tris * 0.6), int(max_tris * 0.9), _segments_for(max_tris)),
        "- %s" % height,
        "- Cylinders, cones, tori and lathes are created around the Z axis, so they lie flat. To stand a "
        "wheel upright with its axle along X, rotate it 90 degrees about Y (rot=(0, 90, 0)); "
        "for an axle along Y, rotate 90 degrees about X (rot=(90, 0, 0)). Check every wheel, barrel and door this way.",
        "- The scene is already empty when the script is replayed. The harness exports and renders the "
        "result for you: never write export, render, save or preview code.",
        "- Make each distinct part its own object; parts must touch or overlap so the model "
        "is one connected whole, with every part in its correct position and orientation "
        "(for example wheels stand upright on their axle, not lying flat).",
        "- Nothing animated, no lights or cameras, no textures.",
        "- Colour each part with `paint(obj, \"#RRGGBB\")` or `paint_faces(obj, lambda p: ..., \"#RRGGBB\")`, "
        "or with the kit's own colour arguments.",
        "- Output ONLY one ```python code block and nothing else.",
        "",
        "Core functions (always available, plain names, no import):",
        "```",
        core_api_digest(),
        "```",
    ]
    if kit_module:
        parts += [
            "",
            "A helper kit is available. Start the script with `import %s` and use it where it helps. "
            "Always call its functions qualified (`%s.name(...)`, never a bare `name(...)`). The scene is "
            "already cleared, so do not call reset_scene(). Rotations passed to the kit are in degrees too. "
            "Its public API:" % (kit_module, kit_module),
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


def repair_hint(failure: str, kit_module: str | None, kit_names, kit_excluded=()) -> str:
    """A concrete extra instruction for failures the model keeps repeating (else empty)."""
    text = str(failure or "")
    if _HANDLE_USE in text:
        return ("Results of kit/core calls are opaque handles: only pass them to other kit/core functions "
                "(paint, transform, join, a list of parts). Do not index, unpack, add, compare or read "
                "attributes of them. Compute positions from the sizes you pass in instead of measuring.")
    if "bpy is not available" in text:
        return ("There is no bpy here. Build with the kit and core functions in the prompt only "
                "(primitive, mesh, modifier, paint, transform, join).")
    if "is not available in build scripts" in text:
        return ("That name is not part of the build dialect. Use only the listed kit and core functions, "
                "and call kit functions qualified, for example `%s.name(...)`." % (kit_module or "kit"))
    match = _NAME_ERROR.search(text)
    if not match:
        return ""
    name = match.group(1)
    if name == "reset_scene":
        return ("`reset_scene` is not defined here and is not needed: the scene is already empty when "
                "build() runs. Delete that call.")
    if name in set(kit_excluded or ()) or (kit_module and name.startswith(kit_module + ".")):
        return ("`%s` is not available in this tool; the harness exports and renders for you. Delete "
                "that call and only build geometry." % name)
    if kit_module and name in set(kit_names or ()):
        return ("`%s` is a function of the kit module, not a builtin. Call it as `%s.%s(...)` "
                "after `import %s`." % (name, kit_module, name, kit_module))
    return ("`%s` is not defined. Define it before use, import it from an allowed module, or remove "
            "the call." % name)


def orientation_guidance(issues) -> str:
    """Turn orientation complaints into concrete rotation instructions (else empty)."""
    text = " ".join(str(item).lower() for item in (issues or []))
    if not any(word in text for word in _ORIENTATION_WORDS):
        return ""
    return ("Orientation help: cylinders, cones, tori and lathes are created around the Z axis, so they lie "
            "flat. To stand a wheel upright with its axle along X, set rot=(0, 90, 0) "
            "(rotate 90 degrees about Y); for an axle along Y use rot=(90, 0, 0). Rotations are in degrees. "
            "When a kit function revolves about Z, rotate the returned object the same way, and move it so "
            "the rim touches the ground (z=0) again. A wheel seen as a flat disc from the TOP is lying "
            "flat; seen from the SIDE it should be a full circle.")


def repair_prompt(base_prompt: str, code: str, failure: str, hint: str = "", stuck: str = "") -> str:
    lines = [
        base_prompt, "",
        "Your previous script failed. Fix it and return the complete corrected script.",
        "", "Previous script:", _code_block(code),
        "", "Failure:", _clip(failure, _MAX_ERROR_CHARS),
    ]
    if hint:
        lines += ["", "How to fix it: " + hint]
    if stuck:
        lines += ["", stuck]
    lines += ["", "Return ONLY one ```python code block."]
    return "\n".join(lines)


def revise_prompt(base_prompt: str, code: str, critique: dict, stuck: str = "") -> str:
    score = critique.get("score")
    issues = [str(item) for item in (critique.get("issues") or [])]
    listing = "\n".join("- " + item for item in issues) or (
        "- (no specific issues listed; improve overall recognisability)")
    lines = [
        base_prompt, "",
        "Your script built, and a reviewer looked at renders from the front, right side, "
        "three-quarter view and top. Score: %s/10." % ("?" if score is None else score),
        "", "Previous script:", _code_block(code),
        "", "Issues to fix:", listing,
    ]
    guidance = orientation_guidance(issues)
    if guidance:
        lines += ["", guidance]
    if stuck:
        lines += ["", stuck]
    lines += ["", "Return the complete revised script, fixing these issues while keeping what already works. "
              "Return ONLY one ```python code block."]
    return "\n".join(lines)


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
