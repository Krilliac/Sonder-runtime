"""Image-generation model discovery and inline chat image synthesis.

Routes image-generation requests from chat to the best available local or
explicitly configured image-generation model and returns a bounded inline
``data:image/...;base64,...`` Markdown response plus a saved file under
``<state_home>/generated-images/``.

Backend priority:
1. Explicitly configured OpenAI-compatible image endpoint (``SONDER_IMAGE_API_URL``)
   — loopback by default; non-loopback requires HTTPS.
2. Explicit image model (``model_override`` or ``SONDER_IMAGE_MODEL``) on the
   configured Ollama endpoint(s).
3. Auto-discovered local image-generation model advertised by Ollama ``/api/tags``
   (e.g. ``x/z-image-turbo``, ``x/flux2-klein``, ``*flux*``, ``*sdxl*``,
   ``*stable-diffusion*``, ``*sd3*``, ``*diffusion*``, ``*imagen*``), strictly
   excluding image-to-text vision models (``-vl``, ``llava``, ``moondream``, etc.).
4. Built-in instant local raster fallback via ``assetgen`` so chat still returns
   a real rendered PNG inline (< 1 s) even before a diffusion model is pulled.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import random
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import assetgen
from sonder_runtime.adapters.inference import ollama_endpoint
import sonder_runtime.platform.paths as runtime_paths

logger = logging.getLogger(__name__)

# Keep inline data URIs safely below Flutter's 2 MiB markdownDataImageMaxBytes.
MAX_INLINE_IMAGE_BYTES = 1_500_000
_MAX_HTTP_IMAGE_RESPONSE_BYTES = 24 * 1024 * 1024
_TAGS_PROBE_TIMEOUT_SECONDS = 1.8
_DEFAULT_GENERATE_TIMEOUT_SECONDS = 180.0

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


def _candidate_ollama_origins(ollama_endpoints: Sequence[str] | None = None) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        try:
            origin = ollama_endpoint.configured_origin(text)
        except Exception:
            return
        if origin not in seen:
            seen.add(origin)
            candidates.append(origin)

    if ollama_endpoints:
        for item in ollama_endpoints:
            _add(item)
    else:
        try:
            _add(ollama_endpoint.configured_origin())
        except Exception:
            pass
        env_workers = os.environ.get("SONDER_OLLAMA_WORKERS", "")
        for part in env_workers.split(","):
            _add(part)
    if not candidates:
        _add(ollama_endpoint.DEFAULT_HOST)
    return candidates


def _probe_ollama_tags(origin: str, timeout: float = _TAGS_PROBE_TIMEOUT_SECONDS) -> list[str]:
    url = f"{origin.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with ollama_endpoint.open_url(req, timeout=timeout) as resp:
            raw = resp.read(2 * 1024 * 1024)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for entry in models:
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("model") or "").strip()
            if name:
                names.append(name)
    return names


def list_discovered_image_models(
    *, ollama_endpoints: Sequence[str] | None = None, timeout: float = _TAGS_PROBE_TIMEOUT_SECONDS,
) -> list[tuple[str, str]]:
    """Return sorted ``(model_name, ollama_origin)`` pairs for installed image models."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for origin in _candidate_ollama_origins(ollama_endpoints):
        for name in _probe_ollama_tags(origin, timeout=timeout):
            if is_image_generation_model_name(name) and name.casefold() not in seen:
                seen.add(name.casefold())
                found.append((name, origin))
    found.sort(key=lambda pair: _preference_rank(pair[0]))
    return found


def _validate_image_api_url(raw: str) -> str:
    url = str(raw or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("SONDER_IMAGE_API_URL must be an http(s) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("SONDER_IMAGE_API_URL must not contain embedded credentials")
    if parsed.scheme == "http" and not ollama_endpoint.is_loopback(url):
        raise ValueError("Non-loopback SONDER_IMAGE_API_URL must use https")
    if parsed.path.rstrip("/").endswith("/images/generations"):
        return url
    base = url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/images/generations"
    return f"{base}/v1/images/generations"


def discover_image_backend(
    *,
    model_override: str = "",
    ollama_endpoints: Sequence[str] | None = None,
) -> ImageBackend:
    """Resolve the highest-priority available image-generation backend."""
    override = str(model_override or "").strip()
    env_api_url = os.environ.get("SONDER_IMAGE_API_URL", "").strip()
    env_model = os.environ.get("SONDER_IMAGE_MODEL", "").strip()
    env_api_key = os.environ.get("SONDER_IMAGE_API_KEY", "").strip()

    if env_api_url:
        try:
            endpoint = _validate_image_api_url(env_api_url)
            return ImageBackend(
                kind="openai_api",
                model=override or env_model or "default",
                endpoint=endpoint,
                api_key=env_api_key,
            )
        except ValueError as err:
            logger.warning("Ignoring invalid SONDER_IMAGE_API_URL: %s", err)

    discovered = list_discovered_image_models(ollama_endpoints=ollama_endpoints)

    if override and is_image_generation_model_name(override):
        for name, origin in discovered:
            if name.casefold() == override.casefold():
                return ImageBackend(kind="ollama", model=name, endpoint=origin)
        origins = _candidate_ollama_origins(ollama_endpoints)
        return ImageBackend(kind="ollama", model=override, endpoint=origins[0])

    if env_model:
        for name, origin in discovered:
            if name.casefold() == env_model.casefold():
                return ImageBackend(kind="ollama", model=name, endpoint=origin)
        origins = _candidate_ollama_origins(ollama_endpoints)
        return ImageBackend(kind="ollama", model=env_model, endpoint=origins[0])

    if discovered:
        best_model, best_origin = discovered[0]
        return ImageBackend(kind="ollama", model=best_model, endpoint=best_origin)

    return ImageBackend(kind="assetgen", model="sonder-assetgen")


def _decode_base64_image(raw: str) -> tuple[bytes, str]:
    text = str(raw or "").strip()
    mime = ""
    if text.startswith("data:") and "," in text:
        header, text = text.split(",", 1)
        m = re.match(r"data:(image/[a-zA-Z0-9.+-]+)", header, re.I)
        if m:
            mime = m.group(1).lower()
    data = base64.b64decode(text)
    if not mime:
        mime = _sniff_mime_type(data)
    return data, mime


def _sniff_mime_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "image/png"


def _sniff_dimensions(data: bytes) -> tuple[int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    if data.startswith(b"\xff\xd8\xff"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return int(w), int(h)
            if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
                i += 2
            else:
                if i + 4 > len(data):
                    break
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                if seg_len < 2:
                    break
                i += 2 + seg_len
    return 0, 0


def _fit_inline_budget(data: bytes, mime: str) -> tuple[bytes, str]:
    """Ensure image bytes fit safely under Flutter's inline data-URI limit."""
    if len(data) <= MAX_INLINE_IMAGE_BYTES:
        return data, mime
    try:
        from PIL import Image  # optional dependency
    except Exception:
        return data, mime
    try:
        img = Image.open(io.BytesIO(data))
        if max(img.size) > 1024:
            img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        out = buf.getvalue()
        if len(out) <= MAX_INLINE_IMAGE_BYTES:
            return out, "image/png"
        rgb = img.convert("RGB")
        for quality in (88, 78, 65):
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            if len(out) <= MAX_INLINE_IMAGE_BYTES:
                return out, "image/jpeg"
        return out, "image/jpeg"
    except Exception:
        return data, mime


def _call_ollama_image(backend: ImageBackend, prompt: str, *, timeout: float) -> tuple[bytes, str]:
    url = f"{backend.endpoint.rstrip('/')}/api/generate"
    body = json.dumps({
        "model": backend.model,
        "prompt": prompt,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with ollama_endpoint.open_url(req, timeout=timeout) as resp:
        raw = resp.read(_MAX_HTTP_IMAGE_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_HTTP_IMAGE_RESPONSE_BYTES:
        raise ValueError("image model response exceeded byte limit")
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("image model returned unexpected payload")
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    b64 = payload.get("image")
    if not b64:
        images = payload.get("images")
        if isinstance(images, list) and images:
            b64 = images[0]
    if not isinstance(b64, str) or not b64.strip():
        raise ValueError(
            f"model {backend.model!r} did not return image bytes (returned text only)"
        )
    return _decode_base64_image(b64)


def _call_openai_image_api(backend: ImageBackend, prompt: str, *, timeout: float) -> tuple[bytes, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if backend.api_key:
        headers["Authorization"] = f"Bearer {backend.api_key}"
    body = json.dumps({
        "model": backend.model,
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
    }).encode("utf-8")
    req = urllib.request.Request(backend.endpoint, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read(_MAX_HTTP_IMAGE_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_HTTP_IMAGE_RESPONSE_BYTES:
        raise ValueError("image API response exceeded byte limit")
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ValueError("image API returned no data items")
    b64 = items[0].get("b64_json")
    if not isinstance(b64, str) or not b64.strip():
        raise ValueError("image API response did not include b64_json")
    return _decode_base64_image(b64)


def _render_assetgen_png(prompt: str, out_path: Path) -> tuple[bytes, str]:
    """Render a crisp 512x320 procedural PNG using Sonder's stdlib assetgen Canvas."""
    is_random = bool(_RANDOM_PROMPT_RE.match(prompt.strip()))
    effective_brief = random.choice(_RANDOM_STYLES) if is_random else prompt
    seed = secrets.randbits(31) if is_random else int(
        hashlib.sha256(effective_brief.encode("utf-8")).hexdigest()[:8], 16
    )
    req = assetgen.infer_request(effective_brief, kinds="background", seed=seed)
    palette = assetgen.palette_from_brief(effective_brief, assetgen.THEMES[req["theme"]])
    base, accent, bright, trim = palette
    rng = random.Random(seed)
    w, h = 512, 320
    canvas = assetgen.Canvas(w, h)

    # Smooth atmospheric vertical gradient
    top_col = assetgen._shade(base, -18)
    mid_col = assetgen._shade(accent, -10)
    bot_col = assetgen._shade(base, 26)
    for y in range(h):
        t = y / float(h - 1)
        if t < 0.6:
            u = t / 0.6
            rgb = tuple(int(top_col[i] * (1.0 - u) + mid_col[i] * u) for i in range(3))
        else:
            u = (t - 0.6) / 0.4
            rgb = tuple(int(mid_col[i] * (1.0 - u) + bot_col[i] * u) for i in range(3))
        row_px = bytes(rgb + (255,)) * w
        start = y * w * 4
        canvas.pixels[start:start + w * 4] = row_px

    # Glowing sun / orb + concentric halo rings
    orb_x = rng.randint(w // 4, 3 * w // 4)
    orb_y = rng.randint(h // 5, h // 2)
    orb_r = rng.randint(36, 62)
    canvas.circle(orb_x, orb_y, orb_r + 22, assetgen._rgba(assetgen._shade(accent, 22)))
    canvas.circle(orb_x, orb_y, orb_r + 10, assetgen._rgba(assetgen._shade(bright, -16)))
    canvas.circle(orb_x, orb_y, orb_r, assetgen._rgba(bright))

    # Stars / particles in upper sky
    for _ in range(34):
        sx = rng.randint(8, w - 9)
        sy = rng.randint(8, int(h * 0.52))
        sr = 2 if rng.random() > 0.72 else 1
        canvas.circle(sx, sy, sr, assetgen._rgba(trim))

    # Layered geometric silhouettes / mountain & crystal ridges
    for layer, (y_base, amp, shade_delta) in enumerate(((212, 54, -22), (242, 38, -36), (272, 24, -48))):
        col = assetgen._rgba(assetgen._shade(base, shade_delta))
        peaks = [rng.randint(y_base - amp, y_base + 12) for _ in range(7)]
        seg_w = w / 6.0
        for x in range(w):
            idx = min(5, int(x / seg_w))
            frac = (x - idx * seg_w) / seg_w
            ridge_y = int(peaks[idx] * (1.0 - frac) + peaks[idx + 1] * frac)
            for y in range(max(0, ridge_y), h):
                canvas.pixel(x, y, col)
        # Subtle highlight crest on front-most ridge
        if layer == 1:
            accent_col = assetgen._rgba(assetgen._shade(accent, 18))
            for x in range(0, w, 2):
                idx = min(5, int(x / seg_w))
                frac = (x - idx * seg_w) / seg_w
                ridge_y = int(peaks[idx] * (1.0 - frac) + peaks[idx + 1] * frac)
                canvas.pixel(x, ridge_y, accent_col)

    # Geometric foreground accents (diamonds / monoliths)
    for _ in range(rng.randint(3, 6)):
        cx = rng.randint(40, w - 40)
        cy = rng.randint(int(h * 0.58), h - 28)
        rx = rng.randint(12, 26)
        ry = rng.randint(16, 34)
        canvas.diamond(cx, cy, rx, ry, assetgen._rgba(assetgen._shade(accent, rng.randint(-15, 25))))
        canvas.diamond(cx, cy, max(4, rx // 2), max(6, ry // 2), assetgen._rgba(trim))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save_png(str(out_path))
    return out_path.read_bytes(), "image/png"


def _slug_from_prompt(prompt: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", prompt.lower()).strip("-")
    return (cleaned[:36] or "image").strip("-") or "image"


def _clean_alt_text(prompt: str) -> str:
    text = re.sub(r"[\r\n\[\]()]+", " ", str(prompt or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 100:
        text = text[:97].rstrip() + "..."
    return text or "Generated image"


def _output_dir(state_home: Path | str | None) -> Path:
    if state_home is not None and str(state_home).strip():
        return Path(state_home) / "generated-images"
    return runtime_paths.default_home() / "generated-images"


def generate_chat_image(
    prompt: str,
    *,
    state_home: Path | str | None = None,
    model_override: str = "",
    ollama_endpoints: Sequence[str] | None = None,
    timeout: float = _DEFAULT_GENERATE_TIMEOUT_SECONDS,
) -> GeneratedImageResult:
    """Generate an image and format it as an inline Markdown ``data:`` reply."""
    clean_prompt = re.sub(r"\s+", " ", str(prompt or "").strip()) or "a random image"
    effective_prompt = (
        f"{random.choice(_RANDOM_STYLES)}, vivid lighting, rich detail"
        if _RANDOM_PROMPT_RE.match(clean_prompt)
        else clean_prompt
    )
    backend = discover_image_backend(
        model_override=model_override,
        ollama_endpoints=ollama_endpoints,
    )
    out_dir = _output_dir(state_home)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug_from_prompt(effective_prompt)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    nonce = secrets.token_hex(3)

    used_fallback = False
    fallback_note = ""
    raw_bytes: bytes
    mime: str
    active_kind = backend.kind
    active_model = backend.model

    if backend.kind == "openai_api":
        try:
            raw_bytes, mime = _call_openai_image_api(backend, effective_prompt, timeout=timeout)
        except Exception as err:
            logger.warning("OpenAI-compatible image API failed (%s); falling back to assetgen", err)
            used_fallback = True
            fallback_note = f"Image API `{backend.model}` failed ({type(err).__name__}); used local raster fallback."
            active_kind = "assetgen"
            active_model = "sonder-assetgen"
            raw_bytes, mime = _render_assetgen_png(effective_prompt, out_dir / f"{slug}-{stamp}-{nonce}.png")
    elif backend.kind == "ollama":
        try:
            raw_bytes, mime = _call_ollama_image(backend, effective_prompt, timeout=timeout)
        except Exception as err:
            logger.warning("Ollama image model %r failed (%s); falling back to assetgen", backend.model, err)
            used_fallback = True
            fallback_note = f"Ollama image model `{backend.model}` failed ({type(err).__name__}); used local raster fallback."
            active_kind = "assetgen"
            active_model = "sonder-assetgen"
            raw_bytes, mime = _render_assetgen_png(effective_prompt, out_dir / f"{slug}-{stamp}-{nonce}.png")
    else:
        used_fallback = True
        fallback_note = (
            "No local image-generation model was found on Ollama. "
            "Pull one (for example `ollama pull x/z-image-turbo`) or set "
            "`SONDER_IMAGE_MODEL` / `SONDER_IMAGE_API_URL` and image prompts "
            "will automatically route to it."
        )
        raw_bytes, mime = _render_assetgen_png(effective_prompt, out_dir / f"{slug}-{stamp}-{nonce}.png")

    raw_bytes, mime = _fit_inline_budget(raw_bytes, mime)
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
    saved_path = out_dir / f"{slug}-{stamp}-{nonce}.{ext}"
    if not saved_path.exists() or saved_path.read_bytes() != raw_bytes:
        saved_path.write_bytes(raw_bytes)

    width, height = _sniff_dimensions(raw_bytes)
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    alt = _clean_alt_text(effective_prompt)
    dims = f"{width}x{height}" if width > 0 and height > 0 else f"{len(raw_bytes):,} bytes"

    lines = [
        f"![{alt}](data:{mime};base64,{b64})",
        "",
        f"Generated via `{active_model}` ({dims}) · saved to `{saved_path}`.",
    ]
    if fallback_note:
        lines.append(fallback_note)

    return GeneratedImageResult(
        markdown="\n".join(lines),
        backend_kind=active_kind,
        model_id=active_model,
        mime_type=mime,
        width=width,
        height=height,
        byte_length=len(raw_bytes),
        saved_path=str(saved_path),
        used_fallback=used_fallback,
        fallback_note=fallback_note,
    )
