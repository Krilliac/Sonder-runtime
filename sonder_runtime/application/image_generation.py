"""Pure image-generation contracts, model-name matching, and markdown stripping.

Stdlib-only application module: holds data models, inline base64 stripping
(used by context health and memory accounting), and pure model-name/prompt
helpers. All network, environment, Pillow, assetgen, and filesystem I/O live
in ``sonder_runtime.adapters.image_generation``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Keep inline data URIs safely below Flutter's 2 MiB markdownDataImageMaxBytes.
MAX_INLINE_IMAGE_BYTES = 1_500_000
_TARGET_GIF_BYTES = 950_000
_DEFAULT_GIF_FRAMES = 14
_DEFAULT_GIF_DURATION_MS = 90
_MAX_HTTP_IMAGE_RESPONSE_BYTES = 24 * 1024 * 1024
_TAGS_PROBE_TIMEOUT_SECONDS = 1.8
_DEFAULT_GENERATE_TIMEOUT_SECONDS = 180.0

_MARKDOWN_DATA_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+\)",
    re.I,
)
_BARE_DATA_IMAGE_RE = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
    re.I,
)


def strip_inline_data_images(text: str) -> str:
    """Replace inline base64 ``data:image/`` payloads with a compact placeholder.

    Inline data URIs can exceed 500 KB per generated image and must never be
    counted as LLM context tokens or replayed verbatim into text-LLM history.
    """
    if not text or "data:image/" not in text:
        return text or ""

    def _replace_md(match: re.Match[str]) -> str:
        alt = (match.group(1) or "").strip()
        return f"[Generated image: {alt}]" if alt else "[Generated image]"

    cleaned = _MARKDOWN_DATA_IMAGE_RE.sub(_replace_md, text)
    if "data:image/" in cleaned:
        cleaned = _BARE_DATA_IMAGE_RE.sub("[inline image]", cleaned)
    return cleaned


_IMAGE_MODEL_INCLUDE_RE = re.compile(
    r"(?:^|[/:_\-])("
    r"z-?image|flux(?:\d|[\-._]|$)|sdxl|stable[\-_ ]?diffusion|sd3(?:\.\d)?|"
    r"diffusion|imagen|dall[\-_ ]?e|gpt[\-_ ]?image|recraft|ideogram|"
    r"kandinsky|pixart|kolors|hunyuan[\-_ ]?image|qwen[\-_ ]?image|"
    r"chroma|aura[\-_ ]?flow|hidream|lumina[\-_ ]?image"
    r")",
    re.I,
)
_IMAGE_MODEL_EXCLUDE_RE = re.compile(
    r"(?:^|[/:_\-])("
    r"vl|vision|llava|bakllava|moondream|minicpm[\-_]?v|internvl|cogvlm|"
    r"ocr|embed|embedding|rerank|mesh|whisper|tts|stt|audio|speech|coder|code"
    r")(?:$|[/:_\-\d])",
    re.I,
)
_PREFERENCE_TOKENS = (
    "z-image-turbo",
    "z-image",
    "flux2-klein",
    "flux",
    "sdxl",
    "sd3",
    "stable-diffusion",
    "qwen-image",
    "hunyuan-image",
    "diffusion",
)
_RANDOM_PROMPT_RE = re.compile(
    r"^(?:a\s+|an\s+|some\s+)?(?:completely\s+|totally\s+)?random(?:\s+\w+){0,3}$",
    re.I,
)
_RANDOM_STYLES = (
    "cyberpunk neon skyline at twilight",
    "sunlit alpine lake with geometric reflection",
    "retro synthwave desert horizon with twin moons",
    "bioluminescent deep-ocean coral reef",
    "minimalist bauhaus abstract geometric composition",
    "warm autumn forest canopy with golden light shafts",
    "orbital space station above a ringed gas giant",
    "pastel watercolor lighthouse on a misty coast",
)
_GENERIC_GIF_PROMPT_RE = re.compile(
    r"^(?:a\s+|an\s+|some\s+)?(?:(?:completely|totally)\s+)?(?:random\s+)?"
    r"(?:(?:cool|nice|fun|pretty|cute|funny|awesome|looping|looped|animated|moving|smooth)\s+)*"
    r"gifs?$",
    re.I,
)
_RANDOM_GIF_STYLES = (
    "neon cyberpunk skyline with shimmering rain and glowing holographic signs",
    "bioluminescent deep-ocean jellyfish pulsing through starry abyssal water",
    "retro synthwave sunset with glowing twin moons over shimmering neon waves",
    "vivid aurora borealis ribbons dancing above a sunlit snow-capped alpine lake",
    "orbital space station drifting past a luminous ringed gas giant and nebula",
    "enchanted lantern-lit forest with floating golden fireflies at twilight",
    "crystalline prism lighthouse beaming warm light across misty ocean waves",
    "cosmic volcanic comet orbiting a radiant starfield with neon plasma trails",
)


@dataclass(frozen=True)
class ImageBackend:
    kind: str  # "openai_api" | "ollama" | "assetgen"
    model: str
    endpoint: str = ""
    api_key: str = ""


@dataclass(frozen=True)
class GeneratedImageResult:
    markdown: str
    backend_kind: str
    model_id: str
    mime_type: str
    width: int
    height: int
    byte_length: int
    saved_path: str
    used_fallback: bool
    fallback_note: str = ""
    frame_count: int = 1


ImageGenerationResult = GeneratedImageResult


def is_image_generation_model_name(name: str) -> bool:
    """Return True when ``name`` identifies an image-generation model."""
    raw = str(name or "").strip()
    if not raw:
        return False
    if _IMAGE_MODEL_EXCLUDE_RE.search(raw):
        return False
    return bool(_IMAGE_MODEL_INCLUDE_RE.search(raw))


def _preference_rank(name: str) -> tuple[int, str]:
    lowered = name.lower()
    for idx, token in enumerate(_PREFERENCE_TOKENS):
        if token in lowered:
            return (idx, lowered)
    return (len(_PREFERENCE_TOKENS), lowered)


def _slug_from_prompt(prompt: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", prompt.lower()).strip("-")
    return (cleaned[:36] or "image").strip("-") or "image"


def _clean_alt_text(prompt: str) -> str:
    text = re.sub(r"[\r\n\[\]()]+", " ", str(prompt or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 100:
        text = text[:97].rstrip() + "..."
    return text or "Generated image"


__all__ = [
    "GeneratedImageResult",
    "ImageBackend",
    "ImageGenerationResult",
    "MAX_INLINE_IMAGE_BYTES",
    "is_image_generation_model_name",
    "strip_inline_data_images",
]
