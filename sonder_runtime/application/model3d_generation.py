"""Pure contracts, prompt parsing, filename guards, and markdown formatting for 3D generation.

Stdlib only — no HTTP, filesystem, or Blender execution lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

MAX_3D_PROMPT_CHARS = 600
MAX_INLINE_PREVIEW_BYTES = 1_400_000  # well under Flutter's 2 MiB inline markdown cap
DEFAULT_3D_STEPS = 5
DEFAULT_3D_OCTREE_RESOLUTION = 256
DEFAULT_3D_FACE_COUNT = 40_000
HIGH_DETAIL_3D_STEPS = 10
HIGH_DETAIL_3D_OCTREE_RESOLUTION = 320
HIGH_DETAIL_3D_FACE_COUNT = 60_000
DEFAULT_3D_TIMEOUT_SECONDS = 300
CREATIVE_DEFAULT_3D_SUBJECT = "ornate brass astrolabe clockwork compass"

_SAFE_MODEL_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}\.(?:glb|obj|png)$")
_NOTEX_FLAG_RE = re.compile(
    r"(?:^|\s)(?:--no-?tex(?:ture)?|notex|untextured|clay(?:\s+only)?)\b",
    re.IGNORECASE,
)
_TEX_FLAG_RE = re.compile(r"(?:^|\s)(?:--tex(?:ture)?|textured)\b", re.IGNORECASE)
_KV_OPTION_KEYS = frozenset({"image", "steps", "octree", "faces", "seed", "format"})
_NL_3D_WRAPPER_RE = re.compile(
    r"^(?:(?:please|can\s+you|could\s+you|would\s+you|will\s+you)\s+)*"
    r"(?:generate|create|make|render|produce|design|build|sculpt|craft|give|model)"
    r"(?:\s+(?:me|us))?\s+"
    r"(?:(?:a|an|the|some|one)\s+)?"
    r"(?P<mod>(?:(?:highly|ultra|very|super|extremely|intricately)\s+)?(?:high[\s-]detail(?:ed)?|detailed|realistic|stylized|custom|cool|nice)\s+)*"
    r"(?:(?:3d|3-d|hunyuan\s*3d)\s+(?:model|mesh|asset|object|figurine|prop|sculpture|glb|character|item)s?"
    r"|hunyuan\s*3d|glb\s+(?:model|mesh|asset|file))\b"
    r"(?:\s+(?:of|for|about|showing|depicting))?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_HIGH_DETAIL_MODIFIER_RE = re.compile(
    r"\b(?:(?:highly|ultra|very|super|extremely|intricately)[\s-]+detailed|"
    r"high[\s-]+detail(?:ed)?|ultra[\s-]+detail(?:ed)?|fine[\s-]+detail(?:ed)?)\b",
    re.IGNORECASE,
)
_GENERIC_DETAIL_WORD_RE = re.compile(r"\bdetailed\b", re.IGNORECASE)
_DANGLING_INTENSIFIER_RE = re.compile(
    r"^(?:highly|ultra|very|super|extremely)\s+(?=(?:a|an|the|of|your|anything|whatever|something|surprise|random|up\s+to\s+you|dealer'?s\s+choice|you\s+(?:choose|pick)|pick\s+one)\b|$)",
    re.IGNORECASE,
)
_OPEN_ENDED_SUBJECT_RE = re.compile(
    r"^(?:(?:of|for|about)\s+)?"
    r"(?:your\s+(?:choosing|choice|pick|selection)|"
    r"whatever(?:\s+you\s+(?:want|like|prefer|choose|pick))?|"
    r"anything(?:\s+(?:you\s+(?:want|like|prefer|choose|pick)|cool|neat|awesome|interesting|random))?|"
    r"something(?:\s+(?:cool|neat|awesome|interesting|random|fun|nice|detailed))?|"
    r"surprise\s+me|up\s+to\s+you|dealer'?s\s+choice|you\s+(?:choose|pick|decide)|pick\s+one|random|"
    r"it|this|that|one|a\s+model|an?\s+object|3d\s+model)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Model3DBackend:
    """Resolved local or configured 3D generation backend."""

    kind: str  # "hunyuan3d" | "unavailable"
    endpoint: str
    model_id: str
    tex_enabled: bool = True
    t23d_enabled: bool = True
    detail: str = ""


@dataclass(frozen=True)
class Model3DRequestOptions:
    """Parsed user request options extracted from a /3d command or natural prompt."""

    prompt: str
    texture: bool | None = None
    image_path: str = ""
    steps: int | None = None
    octree_resolution: int | None = None
    face_count: int | None = None
    seed: int | None = None
    file_format: str = "glb"


@dataclass(frozen=True)
class GeneratedModel3DResult:
    """Result of one generated 3D model artifact + inline preview image."""

    markdown: str
    model_id: str
    backend_kind: str
    prompt: str
    model_path: str
    model_filename: str
    download_url: str
    preview_path: str = ""
    concept_path: str = ""
    model_bytes: int = 0
    tris: int = 0
    verts: int = 0
    materials: int = 0
    textured: bool = True
    elapsed_seconds: float = 0.0
    note: str = ""


def is_safe_model_filename(filename: str) -> bool:
    """Return True iff *filename* is a safe, flat generated-model basename."""
    raw = str(filename or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return False
    return bool(_SAFE_MODEL_FILENAME_RE.fullmatch(raw))


def slug_from_3d_prompt(prompt: str, fallback: str = "model3d") -> str:
    """Compact ASCII filename slug derived from a 3D prompt."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(prompt or "").lower()).strip("-")[:44].strip("-")
    return slug or fallback


def clean_alt_text(prompt: str, fallback: str = "Generated 3D model") -> str:
    text = re.sub(r"[\r\n\[\]\(\)`]+", " ", str(prompt or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return fallback
    return text[:96]


def _clamp_int(raw: str, low: int, high: int) -> int | None:
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return max(low, min(val, high))


def _extract_kv_options_linear(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Extract ``key=value`` / ``--key value`` tokens in linear O(n) time."""
    n = len(text)
    i = 0
    kept: list[str] = []
    found: list[tuple[str, str]] = []

    def _read_value(pos: int) -> tuple[str, int]:
        if pos >= n:
            return "", pos
        quote = text[pos]
        if quote in ('"', "'"):
            end = text.find(quote, pos + 1)
            if end > pos + 1:
                return text[pos + 1 : end], end + 1
            return "", pos
        end = pos
        while end < n and not text[end].isspace():
            end += 1
        return text[pos:end], end

    while i < n:
        if text[i].isspace():
            kept.append(text[i])
            i += 1
            continue
        tok_start = i
        key_start = i + 2 if text[i : i + 2] == "--" else i
        k = key_start
        while k < n and text[k].isalpha():
            k += 1
        key = text[key_start:k].lower()
        if key in _KV_OPTION_KEYS and (k == n or text[k] == "=" or text[k].isspace()):
            sep = k
            while sep < n and text[sep] in " \t":
                sep += 1
            has_eq = sep < n and text[sep] == "="
            if has_eq:
                val_pos = sep + 1
                while val_pos < n and text[val_pos] in " \t":
                    val_pos += 1
                val, next_i = _read_value(val_pos)
                if val:
                    found.append((key, val))
                    kept.append(" ")
                    i = next_i
                    continue
            elif sep > k and sep < n:
                val, next_i = _read_value(sep)
                valid_bare = (
                    (key in ("steps", "octree", "faces", "seed") and val.isdigit())
                    or (key == "format" and val.lower().lstrip(".") in ("glb", "obj"))
                    or (key == "image" and bool(val))
                )
                if val and valid_bare:
                    found.append((key, val))
                    kept.append(" ")
                    i = next_i
                    continue
        # Not an option: copy raw token (including quoted span if starting with quote)
        if text[tok_start] in ('"', "'"):
            end = text.find(text[tok_start], tok_start + 1)
            if end >= 0:
                kept.append(text[tok_start : end + 1])
                i = end + 1
                continue
        end = tok_start
        while end < n and not text[end].isspace():
            end += 1
        kept.append(text[tok_start:end])
        i = end
    return "".join(kept), found


def parse_model3d_prompt(raw: str) -> Model3DRequestOptions:
    """Extract inline options (``notex``, ``image=...``, ``steps=...``, etc.) from prompt text."""
    text = str(raw or "").strip()
    texture: bool | None = None
    image_path = ""
    steps: int | None = None
    octree: int | None = None
    faces: int | None = None
    seed: int | None = None
    file_format = "glb"

    if _NOTEX_FLAG_RE.search(text):
        texture = False
        text = _NOTEX_FLAG_RE.sub(" ", text)
    elif _TEX_FLAG_RE.search(text):
        texture = True
        text = _TEX_FLAG_RE.sub(" ", text)

    text, kv_pairs = _extract_kv_options_linear(text)
    for key, val in kv_pairs:
        if key == "image":
            image_path = val.strip()
        elif key == "steps":
            steps = _clamp_int(val, 1, 50)
        elif key == "octree":
            octree = _clamp_int(val, 64, 512)
        elif key == "faces":
            faces = _clamp_int(val, 1_000, 200_000)
        elif key == "seed":
            seed = _clamp_int(val, 0, 2_147_483_647)
        elif key == "format":
            candidate = val.strip().lower().lstrip(".")
            if candidate in ("glb", "obj"):
                file_format = candidate

    clean_prompt = re.sub(r"\s+", " ", text).strip(" ,;-")

    # Strip full natural-language wrappers (e.g. "generate a highly detailed 3d model of ...")
    high_detail_requested = bool(_HIGH_DETAIL_MODIFIER_RE.search(clean_prompt))
    wrapper_match = _NL_3D_WRAPPER_RE.match(clean_prompt)
    if wrapper_match:
        mod_text = (wrapper_match.group("mod") or "").strip()
        if _HIGH_DETAIL_MODIFIER_RE.search(mod_text) or _GENERIC_DETAIL_WORD_RE.search(mod_text):
            high_detail_requested = True
        clean_prompt = (wrapper_match.group("rest") or "").strip(" ,;:-.!?")

    # Strip quality modifiers and any dangling leading intensifiers left by upstream extractors
    # (e.g. "highly your choosing" -> "your choosing", "highly detailed dragon" -> "dragon").
    if _HIGH_DETAIL_MODIFIER_RE.search(clean_prompt):
        high_detail_requested = True
        clean_prompt = _HIGH_DETAIL_MODIFIER_RE.sub(" ", clean_prompt)
    if _DANGLING_INTENSIFIER_RE.search(clean_prompt):
        high_detail_requested = True
        clean_prompt = _DANGLING_INTENSIFIER_RE.sub("", clean_prompt)
    if _GENERIC_DETAIL_WORD_RE.search(clean_prompt):
        clean_prompt = _GENERIC_DETAIL_WORD_RE.sub(" ", clean_prompt)

    clean_prompt = re.sub(r"\s+", " ", clean_prompt).strip(" ,;:-.!?")
    # Collapse doubled articles left after stripping modifiers (e.g. "a  dragon" -> "a dragon")
    clean_prompt = re.sub(r"^(?:of|for)\s+", "", clean_prompt, flags=re.IGNORECASE).strip(" ,;:-.!?")

    if high_detail_requested:
        if steps is None:
            steps = HIGH_DETAIL_3D_STEPS
        if octree is None:
            octree = HIGH_DETAIL_3D_OCTREE_RESOLUTION
        if faces is None:
            faces = HIGH_DETAIL_3D_FACE_COUNT

    if not clean_prompt or _OPEN_ENDED_SUBJECT_RE.fullmatch(clean_prompt):
        clean_prompt = "image to 3d model" if image_path else CREATIVE_DEFAULT_3D_SUBJECT

    if len(clean_prompt) > MAX_3D_PROMPT_CHARS:
        clean_prompt = clean_prompt[:MAX_3D_PROMPT_CHARS].rstrip()
    return Model3DRequestOptions(
        prompt=clean_prompt,
        texture=texture,
        image_path=image_path,
        steps=steps,
        octree_resolution=octree,
        face_count=faces,
        seed=seed,
        file_format=file_format,
    )


def format_file_size(num_bytes: int) -> str:
    size = max(0, int(num_bytes or 0))
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def format_model3d_markdown(
    *,
    prompt: str,
    model_filename: str,
    download_url: str,
    model_path: str,
    model_bytes: int,
    preview_b64: str = "",
    preview_mime: str = "image/png",
    concept_b64: str = "",
    concept_mime: str = "image/png",
    model_id: str = "hunyuan3d-2",
    textured: bool = True,
    tris: int = 0,
    verts: int = 0,
    elapsed_seconds: float = 0.0,
    note: str = "",
) -> str:
    """Format an inline chat reply showing the rendered 3D preview and model file link."""
    alt = clean_alt_text(prompt, fallback="Generated 3D model")
    blocks: list[str] = []
    if preview_b64:
        blocks.append(f"![3D preview: {alt}](data:{preview_mime};base64,{preview_b64})")
    elif concept_b64:
        blocks.append(f"![Concept preview: {alt}](data:{concept_mime};base64,{concept_b64})")

    size_label = format_file_size(model_bytes)
    mode_label = "textured PBR" if textured else "untextured clay"
    stats_parts = [model_id, mode_label, size_label]
    if tris > 0:
        stats_parts.append(f"{tris:,} tris")
    if verts > 0:
        stats_parts.append(f"{verts:,} verts")
    if elapsed_seconds > 0:
        stats_parts.append(f"{elapsed_seconds:.1f}s")

    link_line = (
        f"**3D Model File:** [{model_filename} ({size_label})]({download_url})\n"
        f"**Saved to:** `{model_path}`\n"
        f"*{' | '.join(stats_parts)}*"
    )
    if note:
        link_line += f"\n*{note}*"
    blocks.append(link_line)
    return "\n\n".join(blocks)
