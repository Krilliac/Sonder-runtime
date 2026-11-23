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
DEFAULT_3D_TIMEOUT_SECONDS = 300

_SAFE_MODEL_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}\.(?:glb|obj|png)$")
_NOTEX_FLAG_RE = re.compile(
    r"(?:^|\s)(?:--no-?tex(?:ture)?|notex|untextured|clay(?:\s+only)?)\b",
    re.IGNORECASE,
)
_TEX_FLAG_RE = re.compile(r"(?:^|\s)(?:--tex(?:ture)?|textured)\b", re.IGNORECASE)
_KV_OPTION_RE = re.compile(
    r"(?:^|\s)(?:--)?(?P<key>image|steps|octree|faces|seed|format)\s*(?:=|\s+)\s*"
    r"(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
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

    def _consume_kv(match: re.Match[str]) -> str:
        nonlocal image_path, steps, octree, faces, seed, file_format
        key = match.group("key").lower()
        val = match.group(2) or match.group(3) or match.group(4) or ""
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
        return " "

    text = _KV_OPTION_RE.sub(_consume_kv, text)
    clean_prompt = re.sub(r"\s+", " ", text).strip(" ,;-")
    if len(clean_prompt) > MAX_3D_PROMPT_CHARS:
        clean_prompt = clean_prompt[:MAX_3D_PROMPT_CHARS].rstrip()
    return Model3DRequestOptions(
        prompt=clean_prompt or ("image to 3d model" if image_path else "a stylized 3d object"),
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
