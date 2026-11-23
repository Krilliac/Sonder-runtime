"""Image-generation model discovery and inline chat image/GIF synthesis adapter.

Handles environment configuration, loopback/HTTPS image API requests, Ollama
image-model discovery, Pillow/assetgen rendering, and persistence under
``<state_home>/generated-images/``. Pure contracts, model-name matching, and
inline-image stripping live in ``sonder_runtime.application.image_generation``.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Sequence

from sonder_runtime.adapters.inference import ollama_endpoint
from sonder_runtime.adapters.repl_services import assetgen
from sonder_runtime.application.image_generation import (
    GeneratedImageResult,
    ImageBackend,
    ImageGenerationResult,
    MAX_INLINE_IMAGE_BYTES,
    _DEFAULT_GENERATE_TIMEOUT_SECONDS,
    _DEFAULT_GIF_DURATION_MS,
    _DEFAULT_GIF_FRAMES,
    _GENERIC_GIF_PROMPT_RE,
    _IMAGE_MODEL_EXCLUDE_RE,
    _IMAGE_MODEL_INCLUDE_RE,
    _MAX_HTTP_IMAGE_RESPONSE_BYTES,
    _PREFERENCE_TOKENS,
    _RANDOM_GIF_STYLES,
    _RANDOM_PROMPT_RE,
    _RANDOM_STYLES,
    _TAGS_PROBE_TIMEOUT_SECONDS,
    _TARGET_GIF_BYTES,
    _clean_alt_text,
    _preference_rank,
    _slug_from_prompt,
    is_image_generation_model_name,
    strip_inline_data_images,
)
import sonder_runtime.platform.paths as runtime_paths

logger = logging.getLogger(__name__)


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
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return int(w), int(h)
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


def extract_gif_frame_payloads(data: bytes) -> list[bytes]:
    """Parse a GIF87a/GIF89a byte stream and return one payload blob per frame."""
    if not data.startswith((b"GIF87a", b"GIF89a")) or len(data) < 13:
        return []
    pos = 13
    packed_lsd = data[10]
    if packed_lsd & 0x80:
        gct_size = 3 * (1 << ((packed_lsd & 0x07) + 1))
        pos += gct_size
    frames: list[bytes] = []
    n = len(data)
    while pos < n:
        marker = data[pos]
        pos += 1
        if marker == 0x3B:  # Trailer
            break
        if marker == 0x21:  # Extension block
            if pos >= n:
                break
            pos += 1  # Extension label
            while pos < n:
                sub_len = data[pos]
                pos += 1
                if sub_len == 0:
                    break
                pos += sub_len
        elif marker == 0x2C:  # Image Descriptor
            if pos + 9 > n:
                break
            start = pos
            packed_img = data[pos + 8]
            pos += 9
            if packed_img & 0x80:
                lct_size = 3 * (1 << ((packed_img & 0x07) + 1))
                pos += lct_size
            if pos >= n:
                break
            pos += 1  # LZW minimum code size
            while pos < n:
                sub_len = data[pos]
                pos += 1
                if sub_len == 0:
                    break
                pos += sub_len
            frames.append(data[start:min(pos, n)])
        else:
            break
    return frames


def count_gif_frames(data: bytes) -> int:
    """Return the number of image frames inside a GIF87a/GIF89a byte stream."""
    return len(extract_gif_frame_payloads(data))


def _gif_lzw_encode(pixels: bytes, min_code_size: int) -> bytes:
    """Spec-compliant GIF LZW encoder with dynamic code width (up to 12 bits)."""
    clear_code = 1 << min_code_size
    eoi_code = clear_code + 1
    code_size = min_code_size + 1
    next_code = eoi_code + 1
    max_code = 1 << code_size
    table: dict[int, int] = {}
    out = bytearray()
    acc = 0
    bits = 0

    acc |= clear_code << bits
    bits += code_size
    while bits >= 8:
        out.append(acc & 0xFF)
        acc >>= 8
        bits -= 8

    if pixels:
        prefix = pixels[0]
        for b in pixels[1:]:
            key = (prefix << 8) | b
            found = table.get(key)
            if found is not None:
                prefix = found
            else:
                acc |= prefix << bits
                bits += code_size
                while bits >= 8:
                    out.append(acc & 0xFF)
                    acc >>= 8
                    bits -= 8
                if next_code < 4096:
                    table[key] = next_code
                    next_code += 1
                    if next_code > max_code and code_size < 12:
                        code_size += 1
                        max_code = 1 << code_size
                else:
                    acc |= clear_code << bits
                    bits += code_size
                    while bits >= 8:
                        out.append(acc & 0xFF)
                        acc >>= 8
                        bits -= 8
                    table.clear()
                    code_size = min_code_size + 1
                    next_code = eoi_code + 1
                    max_code = 1 << code_size
                prefix = b
        acc |= prefix << bits
        bits += code_size
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8

    acc |= eoi_code << bits
    bits += code_size
    while bits >= 8:
        out.append(acc & 0xFF)
        acc >>= 8
        bits -= 8
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def _gif_subblocks(data: bytes) -> bytes:
    out = bytearray()
    for offset in range(0, len(data), 255):
        chunk = data[offset:offset + 255]
        out.append(len(chunk))
        out.extend(chunk)
    out.append(0)
    return bytes(out)


def _encode_indexed_gif89a(
    frames: Sequence[bytes],
    palette: Sequence[tuple[int, int, int]],
    width: int,
    height: int,
    *,
    duration_ms: int = _DEFAULT_GIF_DURATION_MS,
) -> bytes:
    """Encode indexed-color frames as a looping GIF89a animation."""
    if not frames:
        raise ValueError("at least one frame is required to encode a GIF")
    color_bits = max(2, min(8, (max(2, len(palette)) - 1).bit_length()))
    table_len = 1 << color_bits
    padded_palette = list(palette[:table_len])
    while len(padded_palette) < table_len:
        padded_palette.append((0, 0, 0))

    delay_cs = max(2, int(round(duration_ms / 10.0)))
    out = bytearray(b"GIF89a")
    packed_lsd = 0x80 | (7 << 4) | (color_bits - 1)
    out.extend(struct.pack("<HHBBB", width, height, packed_lsd, 0, 0))
    for r, g, b in padded_palette:
        out.extend((max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))))
    # NETSCAPE2.0 infinite loop application extension
    out.extend(b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00")
    for frame in frames:
        out.extend(b"\x21\xF9\x04\x04")
        out.extend(struct.pack("<H", delay_cs))
        out.extend(b"\x00\x00")
        out.extend(b"\x2C")
        out.extend(struct.pack("<HHHHB", 0, 0, width, height, 0))
        out.append(color_bits)
        out.extend(_gif_subblocks(_gif_lzw_encode(frame, color_bits)))
    out.append(0x3B)
    return bytes(out)


def _mix_rgb(left: tuple[int, int, int], right: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    u = max(0.0, min(1.0, float(t)))
    return (
        max(0, min(255, int(round(left[0] * (1.0 - u) + right[0] * u)))),
        max(0, min(255, int(round(left[1] * (1.0 - u) + right[1] * u)))),
        max(0, min(255, int(round(left[2] * (1.0 - u) + right[2] * u)))),
    )


def _target_gif_size(src_w: int, src_h: int, max_side: int = 384, min_side: int = 256) -> tuple[int, int]:
    w = max(1, int(src_w))
    h = max(1, int(src_h))
    longest = max(w, h)
    if longest < min_side:
        scale = min_side / float(longest)
    elif longest > max_side:
        scale = max_side / float(longest)
    else:
        scale = 1.0
    out_w = max(64, min(max_side, int(round(w * scale))))
    out_h = max(64, min(max_side, int(round(h * scale))))
    # Keep even dimensions for clean raster alignment
    out_w = max(64, out_w & ~1)
    out_h = max(64, out_h & ~1)
    return out_w, out_h


def _animate_keyframe_pillow(
    keyframe_bytes: bytes,
    *,
    frame_count: int = _DEFAULT_GIF_FRAMES,
    duration_ms: int = _DEFAULT_GIF_DURATION_MS,
) -> bytes | None:
    """Turn a single keyframe image into a smooth looping GIF89a using Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageEnhance
    except Exception:
        return None

    try:
        src = Image.open(io.BytesIO(keyframe_bytes)).convert("RGB")
    except Exception:
        return None

    out_w, out_h = _target_gif_size(src.width, src.height, max_side=384, min_side=256)
    # Work from a slightly upsampled master when the source is smaller than output
    work = src
    if work.width < out_w or work.height < out_h:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        work = work.resize((max(out_w, work.width), max(out_h, work.height)), resample)

    resample = getattr(Image, "Resampling", Image).LANCZOS
    rgb_frames: list = []
    # Pre-build a soft radial glow tile for subtle luminous atmospheric shimmer
    glow_size = max(48, min(out_w, out_h) // 2)
    glow_mask = Image.new("L", (glow_size, glow_size), 0)
    glow_draw = ImageDraw.Draw(glow_mask)
    half = glow_size / 2.0
    for step in range(8, 0, -1):
        frac = step / 8.0
        r = max(1, int(half * frac))
        alpha = int(28 * ((1.0 - frac) ** 1.35 + 0.12))
        glow_draw.ellipse(
            (int(half - r), int(half - r), int(half + r), int(half + r)),
            fill=min(255, alpha),
        )

    w_src, h_src = work.size
    for i in range(frame_count):
        theta = 2.0 * math.pi * i / float(frame_count)
        # Seamless periodic forward-backward Ken Burns zoom (1.04 -> 1.12 -> 1.04)
        zoom = 1.04 + 0.08 * (0.5 - 0.5 * math.cos(theta))
        crop_w = max(1.0, w_src / zoom)
        crop_h = max(1.0, h_src / zoom)
        max_dx = max(0.0, w_src - crop_w)
        max_dy = max(0.0, h_src - crop_h)
        pan_x = 0.5 + 0.42 * math.sin(theta)
        pan_y = 0.5 + 0.35 * math.sin(theta * 2.0) * 0.5
        x0 = max(0.0, min(max_dx, max_dx * pan_x))
        y0 = max(0.0, min(max_dy, max_dy * pan_y))
        frame = work.crop((x0, y0, x0 + crop_w, y0 + crop_h)).resize((out_w, out_h), resample)

        # Subtle periodic luminous breathing + color warmth
        brightness = 1.0 + 0.055 * math.sin(theta)
        color_boost = 1.0 + 0.08 * math.cos(theta)
        frame = ImageEnhance.Brightness(frame).enhance(brightness)
        frame = ImageEnhance.Color(frame).enhance(color_boost)

        # Subtle orbiting luminous highlight sheen (seamless over 2*pi)
        gx = int(out_w * (0.5 + 0.30 * math.cos(theta)) - glow_size / 2.0)
        gy = int(out_h * (0.45 + 0.22 * math.sin(theta)) - glow_size / 2.0)
        tint = (
            min(255, int(235 + 20 * math.sin(theta))),
            min(255, int(230 + 20 * math.cos(theta))),
            255,
        )
        sheen = Image.new("RGB", (glow_size, glow_size), tint)
        frame.paste(sheen, (gx, gy), glow_mask)
        rgb_frames.append(frame)

    return _encode_pillow_frames_under_budget(rgb_frames, duration_ms=duration_ms)


def _encode_pillow_frames_under_budget(
    rgb_frames: Sequence,
    *,
    duration_ms: int = _DEFAULT_GIF_DURATION_MS,
) -> bytes:
    """Quantize and encode Pillow RGB frames as a looping GIF89a under byte budget."""
    from PIL import Image

    resample = getattr(Image, "Resampling", Image).LANCZOS
    dither_fs = getattr(Image, "Dither", Image).FLOYDSTEINBERG
    dither_none = getattr(Image, "Dither", Image).NONE
    mediancut = getattr(getattr(Image, "Quantize", None), "MEDIANCUT", 0)

    def _encode_attempt(frames_in: Sequence, colors: int, dither_mode) -> bytes:
        n = len(frames_in)
        sample_indices = sorted({0, n // 3, (2 * n) // 3})
        w, h = frames_in[0].size
        sheet = Image.new("RGB", (w * len(sample_indices), h))
        for idx, frame_idx in enumerate(sample_indices):
            sheet.paste(frames_in[frame_idx], (idx * w, 0))
        palette_img = sheet.quantize(colors=colors, method=mediancut, dither=dither_none)
        pal_frames = [
            fr.quantize(palette=palette_img, dither=dither_mode)
            for fr in frames_in
        ]
        buf = io.BytesIO()
        pal_frames[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=pal_frames[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
            disposal=1,
        )
        return buf.getvalue()

    ladders = (
        (1.0, 256, dither_fs),
        (1.0, 192, dither_none),
        (0.84, 160, dither_none),
        (0.68, 128, dither_none),
        (0.54, 96, dither_none),
    )
    best: bytes | None = None
    base_w, base_h = rgb_frames[0].size
    for scale, colors, dither_mode in ladders:
        if scale < 0.999:
            sw = max(64, int(round(base_w * scale)) & ~1)
            sh = max(64, int(round(base_h * scale)) & ~1)
            candidate_frames = [fr.resize((sw, sh), resample) for fr in rgb_frames]
        else:
            candidate_frames = rgb_frames
        encoded = _encode_attempt(candidate_frames, colors, dither_mode)
        if best is None or len(encoded) < len(best):
            best = encoded
        if len(encoded) <= _TARGET_GIF_BYTES:
            return encoded
    return best if best is not None else b""


def _decode_png_rgb(data: bytes) -> tuple[int, int, list[bytes]] | None:
    """Pure-Python 8-bit non-interlaced PNG decoder returning ``(w, h, rgb_rows)``."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    n = len(data)
    w = h = bit_depth = color_type = interlace = 0
    plte = b""
    idat = bytearray()
    while pos + 8 <= n:
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        pos += 8
        if pos + length + 4 > n:
            return None
        payload = data[pos:pos + length]
        pos += length + 4
        if ctype == b"IHDR":
            if length < 13:
                return None
            w, h, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(">IIBBBBB", payload[:13])
        elif ctype == b"PLTE":
            plte = payload
        elif ctype == b"IDAT":
            idat.extend(payload)
        elif ctype == b"IEND":
            break
    if (
        bit_depth != 8
        or interlace != 0
        or color_type not in (0, 2, 3, 4, 6)
        or w <= 0
        or h <= 0
        or w * h > 4096 * 4096
    ):
        return None
    if color_type == 3 and len(plte) < 3:
        return None
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    stride = w * bpp
    expected = (stride + 1) * h
    if expected > 64 * 1024 * 1024:
        return None
    try:
        decomp = zlib.decompressobj()
        raw = decomp.decompress(bytes(idat), expected + 1)
    except Exception:
        return None
    if len(raw) < expected:
        return None

    prev = bytearray(stride)
    rows: list[bytes] = []
    idx = 0
    for _ in range(h):
        ftype = raw[idx]
        idx += 1
        cur = bytearray(raw[idx:idx + stride])
        idx += stride
        if ftype == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (cur[i] + pr) & 0xFF
        elif ftype != 0:
            return None
        prev = cur

        if color_type == 2:
            rows.append(bytes(cur))
        elif color_type == 6:
            rgb = bytearray(w * 3)
            rgb[0::3] = cur[0::4]
            rgb[1::3] = cur[1::4]
            rgb[2::3] = cur[2::4]
            rows.append(bytes(rgb))
        elif color_type == 0:
            rgb = bytearray(w * 3)
            rgb[0::3] = cur
            rgb[1::3] = cur
            rgb[2::3] = cur
            rows.append(bytes(rgb))
        elif color_type == 4:
            g = cur[0::2]
            rgb = bytearray(w * 3)
            rgb[0::3] = g
            rgb[1::3] = g
            rgb[2::3] = g
            rows.append(bytes(rgb))
        else:  # color_type == 3 indexed PLTE
            max_idx = (len(plte) // 3) - 1
            rgb = bytearray(w * 3)
            out_pos = 0
            for pal_i in cur:
                p_off = min(pal_i, max_idx) * 3
                rgb[out_pos:out_pos + 3] = plte[p_off:p_off + 3]
                out_pos += 3
            rows.append(bytes(rgb))
    return w, h, rows


def _animate_keyframe_pure_png(
    keyframe_bytes: bytes,
    *,
    frame_count: int = _DEFAULT_GIF_FRAMES,
    duration_ms: int = _DEFAULT_GIF_DURATION_MS,
) -> bytes | None:
    """Pure-Python PNG keyframe animator (used when Pillow is unavailable)."""
    decoded = _decode_png_rgb(keyframe_bytes)
    if decoded is None:
        return None
    src_w, src_h, rows = decoded

    # 6x6x6 RGB web-cube (216 colors) + 40 luminous highlight / neutral ramp colors
    levels = (0, 51, 102, 153, 204, 255)
    palette: list[tuple[int, int, int]] = [
        (r, g, b) for r in levels for g in levels for b in levels
    ]
    for i in range(40):
        v = int(round(255 * i / 39.0))
        palette.append((v, v, min(255, v + 8)))

    n_frames = min(frame_count, 12)
    for max_side in (288, 224, 176):
        out_w, out_h = _target_gif_size(src_w, src_h, max_side=max_side, min_side=160)
        indexed_frames: list[bytes] = []
        for f in range(n_frames):
            theta = 2.0 * math.pi * f / float(n_frames)
            zoom = 1.04 + 0.08 * (0.5 - 0.5 * math.cos(theta))
            crop_w = max(1, int(round(src_w / zoom)))
            crop_h = max(1, int(round(src_h / zoom)))
            x0 = max(0, min(src_w - crop_w, int((src_w - crop_w) * (0.5 + 0.42 * math.sin(theta)))))
            y0 = max(0, min(src_h - crop_h, int((src_h - crop_h) * (0.5 + 0.35 * math.sin(theta * 2.0) * 0.5))))

            br = 1.0 + 0.06 * math.sin(theta)
            bg = 1.0 + 0.04 * math.sin(theta + 1.0)
            bb = 1.0 + 0.06 * math.cos(theta)
            lr = [min(5, (min(255, int(v * br)) * 5 + 127) // 255) * 36 for v in range(256)]
            lg = [min(5, (min(255, int(v * bg)) * 5 + 127) // 255) * 6 for v in range(256)]
            lb = [min(5, (min(255, int(v * bb)) * 5 + 127) // 255) for v in range(256)]
            xs = [min(src_w - 1, x0 + (x * crop_w) // out_w) * 3 for x in range(out_w)]

            frame = bytearray(out_w * out_h)
            pos = 0
            for y in range(out_h):
                sy = min(src_h - 1, y0 + (y * crop_h) // out_h)
                row = rows[sy]
                for sx in xs:
                    frame[pos] = lr[row[sx]] + lg[row[sx + 1]] + lb[row[sx + 2]]
                    pos += 1

            # Subtle orbiting luminous glint so motion stays crisp even on flat areas
            cx = int(out_w * (0.5 + 0.30 * math.cos(theta)))
            cy = int(out_h * (0.45 + 0.22 * math.sin(theta)))
            rad = max(8, min(out_w, out_h) // 14)
            r2 = rad * rad
            for dy in range(-rad, rad + 1):
                py = cy + dy
                if 0 <= py < out_h:
                    row_off = py * out_w
                    dy2 = dy * dy
                    for dx in range(-rad, rad + 1):
                        px = cx + dx
                        if 0 <= px < out_w:
                            d2 = dx * dx + dy2
                            if d2 <= r2:
                                boost = 1.0 - (d2 / float(r2))
                                if boost > 0.55:
                                    frame[row_off + px] = 216 + min(39, 28 + int(boost * 11))
            indexed_frames.append(bytes(frame))

        encoded = _encode_indexed_gif89a(
            indexed_frames,
            palette,
            out_w,
            out_h,
            duration_ms=duration_ms,
        )
        if len(encoded) <= _TARGET_GIF_BYTES or max_side == 176:
            return encoded
    return None


def _procedural_gif_theme(prompt: str) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    lowered = prompt.lower()
    if any(tok in lowered for tok in ("cyberpunk", "neon", "synthwave", "vaporwave", "hologram", "laser")):
        base_pal = ((14, 10, 34), (236, 46, 152), (56, 238, 255), (86, 36, 132))
    elif any(tok in lowered for tok in ("ocean", "wave", "water", "jellyfish", "coral", "sea", "aurora", "frost", "ice", "snow", "arctic")):
        base_pal = ((8, 22, 42), (34, 178, 214), (168, 250, 240), (24, 72, 108))
    elif any(tok in lowered for tok in ("fire", "lava", "volcano", "sunset", "ember", "dragon", "desert", "mars", "solar", "flame")):
        base_pal = ((24, 12, 22), (234, 84, 48), (255, 206, 92), (104, 34, 52))
    elif any(tok in lowered for tok in ("forest", "jungle", "emerald", "nature", "firefl", "lantern", "moss", "leaf")):
        base_pal = ((10, 24, 20), (46, 176, 114), (240, 232, 126), (34, 76, 58))
    elif any(tok in lowered for tok in ("pastel", "sakura", "dream", "cloud", "watercolor", "cotton", "rainbow")):
        base_pal = ((36, 26, 58), (232, 134, 178), (255, 226, 186), (118, 92, 168))
    else:
        req = assetgen.infer_request(prompt, kinds="background", seed=1337)
        base_pal = assetgen.THEMES[req["theme"]]
    return assetgen.palette_from_brief(prompt, base_pal)


def _procedural_gif_palette(prompt: str) -> list[tuple[int, int, int]]:
    """Build a 64-color smooth gradient + neon + orb palette for procedural GIFs."""
    base, accent, bright, trim = _procedural_gif_theme(prompt)
    deep_sky = _mix_rgb(base, (4, 6, 16), 0.45)
    mid_sky = _mix_rgb(base, trim, 0.55)
    horizon_glow = _mix_rgb(accent, trim, 0.35)
    hot_white = _mix_rgb(bright, (255, 255, 255), 0.72)

    palette: list[tuple[int, int, int]] = []
    # 0..23: 24-step twilight sky gradient
    for i in range(24):
        t = i / 23.0
        if t < 0.55:
            palette.append(_mix_rgb(deep_sky, mid_sky, t / 0.55))
        else:
            palette.append(_mix_rgb(mid_sky, horizon_glow, (t - 0.55) / 0.45))
    # 24..35: 12-step neon / aurora ribbon ramp
    for i in range(12):
        t = i / 11.0
        if t < 0.5:
            palette.append(_mix_rgb(trim, accent, t * 2.0))
        else:
            palette.append(_mix_rgb(accent, bright, (t - 0.5) * 2.0))
    # 36..47: 12-step celestial orb / corona ramp
    for i in range(12):
        t = i / 11.0
        if t < 0.65:
            palette.append(_mix_rgb(accent, bright, t / 0.65))
        else:
            palette.append(_mix_rgb(bright, hot_white, (t - 0.65) / 0.35))
    # 48..57: 10-step layered ridge / silhouette / crystal ramp
    for i in range(10):
        t = i / 9.0
        palette.append(_mix_rgb(_mix_rgb(base, (0, 0, 0), 0.55), trim, t))
    # 58..63: 6-step water shimmer / star / sparkle accents
    for i in range(6):
        t = i / 5.0
        palette.append(_mix_rgb(deep_sky, hot_white, 0.25 + 0.75 * t))
    return palette


def _draw_indexed_circle(
    buf: bytearray, width: int, height: int, cx: int, cy: int, radius: int, color: int,
) -> None:
    r2 = radius * radius
    y_min = max(0, cy - radius)
    y_max = min(height - 1, cy + radius)
    for y in range(y_min, y_max + 1):
        dy = y - cy
        dx_max = int(math.isqrt(max(0, r2 - dy * dy)))
        x0 = max(0, cx - dx_max)
        x1 = min(width, cx + dx_max + 1)
        if x1 > x0:
            row_off = y * width
            buf[row_off + x0:row_off + x1] = bytes([color]) * (x1 - x0)


def _draw_indexed_diamond(
    buf: bytearray, width: int, height: int, cx: int, cy: int, rx: int, ry: int, color: int,
) -> None:
    if rx <= 0 or ry <= 0:
        return
    for dy in range(-ry, ry + 1):
        py = cy + dy
        if 0 <= py < height:
            span = int(rx * (1.0 - abs(dy) / float(ry)))
            x0 = max(0, cx - span)
            x1 = min(width, cx + span + 1)
            if x1 > x0:
                row_off = py * width
                buf[row_off + x0:row_off + x1] = bytes([color]) * (x1 - x0)


def _render_procedural_gif(
    prompt: str,
    *,
    frame_count: int = _DEFAULT_GIF_FRAMES,
    duration_ms: int = _DEFAULT_GIF_DURATION_MS,
    width: int = 320,
    height: int = 200,
    is_random: bool = False,
) -> bytes:
    """Synthesize a smooth, looping multi-frame procedural GIF89a in pure Python."""
    seed = secrets.randbits(31) if is_random else int(
        hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], 16
    )
    rng = random.Random(seed)
    palette = _procedural_gif_palette(prompt)
    horizon = int(height * 0.64)

    # Twinkling stars across the upper sky
    stars = [
        (rng.randint(4, width - 5), rng.randint(4, horizon - 14), rng.uniform(0.0, 2.0 * math.pi), 1 if rng.random() > 0.78 else 0)
        for _ in range(44)
    ]
    # Seamless tileable ridge profile for gentle parallax drift
    ridge_a = [
        int(horizon - 18 - 18 * math.sin(2.0 * math.pi * x / width * 2.0 + seed % 7) - 9 * math.cos(2.0 * math.pi * x / width * 5.0))
        for x in range(width)
    ]
    ridge_b = [
        int(horizon - 6 - 11 * math.sin(2.0 * math.pi * x / width * 3.0 + 1.3) - 6 * math.cos(2.0 * math.pi * x / width * 7.0))
        for x in range(width)
    ]
    # Floating crystal diamonds
    crystals = [
        (
            rng.randint(28, width - 29),
            rng.randint(int(height * 0.68), height - 22),
            rng.randint(7, 13),
            rng.randint(10, 18),
            rng.uniform(0.0, 2.0 * math.pi),
        )
        for _ in range(5)
    ]

    base_orb_x = width // 2 + rng.randint(-36, 36)
    base_orb_y = int(height * 0.34)
    orb_r = rng.randint(22, 30)

    frames: list[bytes] = []
    for f in range(frame_count):
        theta = 2.0 * math.pi * f / float(frame_count)
        buf = bytearray(width * height)

        # 1. Breathing sky gradient (0..23)
        sky_shift = int(round(1.5 * math.sin(theta)))
        for y in range(horizon):
            c = max(0, min(23, (y * 23) // max(1, horizon - 1) + sky_shift))
            buf[y * width:(y + 1) * width] = bytes([c]) * width

        # 2. Flowing twin aurora / neon plasma ribbons
        for x in range(width):
            nx = x / float(width)
            y1 = int(height * 0.22 + 14.0 * math.sin(nx * 2.0 * math.pi * 2.0 + theta) + 6.0 * math.cos(nx * 2.0 * math.pi * 3.0 - theta))
            y2 = int(height * 0.36 + 11.0 * math.cos(nx * 2.0 * math.pi * 1.5 - theta) + 5.0 * math.sin(nx * 2.0 * math.pi * 4.0 + theta))
            for ribbon_y, base_c, thick in ((y1, 25, 9), (y2, 29, 7)):
                for dy in range(-thick, thick + 1):
                    py = ribbon_y + dy
                    if 0 <= py < horizon:
                        intensity = 1.0 - abs(dy) / float(thick + 1)
                        c = min(35, base_c + int(intensity * 6))
                        buf[py * width + x] = c

        # 3. Twinkling starfield
        for sx, sy, s_phase, big in stars:
            twinkle = 0.5 + 0.5 * math.sin(theta + s_phase)
            c = 60 + int(twinkle * 3)
            buf[sy * width + sx] = c
            if big and twinkle > 0.55:
                for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    px, py = sx + ox, sy + oy
                    if 0 <= px < width and 0 <= py < horizon:
                        buf[py * width + px] = max(59, c - 1)

        # 4. Orbiting celestial orb + pulsing concentric halo corona + satellite moon
        orb_x = int(base_orb_x + 22.0 * math.sin(theta))
        orb_y = int(base_orb_y + 9.0 * math.sin(theta * 2.0))
        pulse = int(round(4.0 * math.sin(theta)))
        _draw_indexed_circle(buf, width, height, orb_x, orb_y, orb_r + 18 + pulse, 37)
        _draw_indexed_circle(buf, width, height, orb_x, orb_y, orb_r + 10 + (pulse // 2), 40)
        _draw_indexed_circle(buf, width, height, orb_x, orb_y, orb_r + 4, 43)
        _draw_indexed_circle(buf, width, height, orb_x, orb_y, orb_r, 46)
        _draw_indexed_circle(buf, width, height, orb_x - orb_r // 3, orb_y - orb_r // 3, max(4, orb_r // 3), 47)

        # Orbiting satellite + glowing trail
        for trail_step in range(3, -1, -1):
            t_angle = theta - trail_step * 0.24
            sat_x = int(orb_x + (orb_r + 28) * math.cos(t_angle))
            sat_y = int(orb_y + (orb_r * 0.55 + 10) * math.sin(t_angle))
            sat_r = 5 if trail_step == 0 else max(2, 4 - trail_step)
            sat_c = 47 if trail_step == 0 else (35 - trail_step * 3)
            _draw_indexed_circle(buf, width, height, sat_x, sat_y, sat_r, sat_c)

        # 5. Parallax layered silhouette ridges
        shift_a = int(round((f / float(frame_count)) * width * 0.08))
        shift_b = int(round((f / float(frame_count)) * width * 0.16))
        for x in range(width):
            ya = max(0, min(horizon, ridge_a[(x + shift_a) % width]))
            for y in range(ya, horizon):
                buf[y * width + x] = 52
            yb = max(0, min(horizon, ridge_b[(x + shift_b) % width]))
            for y in range(yb, horizon):
                buf[y * width + x] = 49
            if 0 <= yb < horizon:
                buf[yb * width + x] = 31

        # 6. Animated neon horizon & shimmering reflective waves (horizon..height)
        for y in range(horizon, height):
            depth = (y - horizon) / float(max(1, height - horizon - 1))
            base_water = 48 + int((1.0 - depth) * 6)
            buf[y * width:(y + 1) * width] = bytes([base_water]) * width

            # Horizontal perspective neon wave band
            wave_val = math.sin(depth * 18.0 - theta * 2.0)
            if wave_val > 0.62:
                band_c = 28 + int((1.0 - depth) * 6)
                buf[y * width:(y + 1) * width] = bytes([band_c]) * width

            # Shimmering reflection column beneath the orbiting orb
            refl_half = max(6, int((orb_r * (0.65 + depth * 1.15)) + 6.0 * math.sin(y * 0.45 + theta * 2.0)))
            refl_cx = int(orb_x + 7.0 * math.sin(y * 0.35 - theta * 2.0))
            rx0 = max(0, refl_cx - refl_half)
            rx1 = min(width, refl_cx + refl_half)
            if rx1 > rx0:
                shimmer = 0.5 + 0.5 * math.sin(y * 0.7 - theta * 2.0)
                refl_c = 42 + int(shimmer * 5) if (y - horizon) % 2 == 0 else 33
                buf[y * width + rx0:y * width + rx1] = bytes([refl_c]) * (rx1 - rx0)

        # Glowing horizon seam line
        buf[horizon * width:(horizon + 1) * width] = bytes([34]) * width

        # 7. Floating pulsing geometric crystals in foreground
        for cx, cy, rx, ry, c_phase in crystals:
            bob_y = int(cy + 5.0 * math.sin(theta + c_phase))
            glow_c = 30 + int((0.5 + 0.5 * math.cos(theta + c_phase)) * 5)
            _draw_indexed_diamond(buf, width, height, cx, bob_y, rx, ry, 54)
            _draw_indexed_diamond(buf, width, height, cx, bob_y, max(3, rx // 2), max(4, ry // 2), glow_c)

        frames.append(bytes(buf))

    return _encode_indexed_gif89a(frames, palette, width, height, duration_ms=duration_ms)


def generate_chat_gif(
    prompt: str,
    *,
    state_home: Path | str | None = None,
    model_override: str = "",
    ollama_endpoints: Sequence[str] | None = None,
    timeout: float = _DEFAULT_GENERATE_TIMEOUT_SECONDS,
    frame_count: int = _DEFAULT_GIF_FRAMES,
    duration_ms: int = _DEFAULT_GIF_DURATION_MS,
) -> GeneratedImageResult:
    """Synthesize a looping animated GIF89a and return an inline Markdown reply."""
    clean_prompt = re.sub(r"\s+", " ", str(prompt or "").strip()) or "a random animated gif"
    is_generic = bool(
        _GENERIC_GIF_PROMPT_RE.match(clean_prompt) or _RANDOM_PROMPT_RE.match(clean_prompt)
    )
    effective_prompt = (
        f"{random.choice(_RANDOM_GIF_STYLES)}, vivid lighting, rich detail"
        if is_generic
        else clean_prompt
    )
    clamped_frames = max(8, min(int(frame_count), 24))
    clamped_duration = max(40, min(int(duration_ms), 250))

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
    active_kind = backend.kind
    active_model = backend.model
    gif_bytes: bytes | None = None

    if backend.kind in ("openai_api", "ollama"):
        try:
            if backend.kind == "openai_api":
                keyframe_bytes, _keyframe_mime = _call_openai_image_api(
                    backend, effective_prompt, timeout=timeout,
                )
            else:
                keyframe_bytes, _keyframe_mime = _call_ollama_image(
                    backend, effective_prompt, timeout=timeout,
                )
            gif_bytes = _animate_keyframe_pillow(
                keyframe_bytes,
                frame_count=clamped_frames,
                duration_ms=clamped_duration,
            )
            if gif_bytes is None:
                gif_bytes = _animate_keyframe_pure_png(
                    keyframe_bytes,
                    frame_count=clamped_frames,
                    duration_ms=clamped_duration,
                )
            if gif_bytes is None:
                used_fallback = True
                fallback_note = (
                    f"Keyframe from `{backend.model}` could not be decoded without Pillow; "
                    "used procedural animated GIF fallback."
                )
                active_kind = "assetgen"
                active_model = "sonder-gifgen"
        except Exception as err:
            logger.warning("GIF keyframe backend %r failed (%s); falling back to procedural GIF", backend.model, err)
            used_fallback = True
            fallback_note = (
                f"Image backend `{backend.model}` failed ({type(err).__name__}); "
                "used procedural animated GIF fallback."
            )
            active_kind = "assetgen"
            active_model = "sonder-gifgen"
    else:
        used_fallback = True
        active_kind = "assetgen"
        active_model = "sonder-gifgen"
        fallback_note = (
            "No local image-generation model was found. "
            "Set `SONDER_IMAGE_API_URL` or pull an Ollama image model "
            "(for example `ollama pull x/z-image-turbo`) to animate diffusion keyframes."
        )

    if gif_bytes is None:
        gif_bytes = _render_procedural_gif(
            effective_prompt,
            frame_count=clamped_frames,
            duration_ms=clamped_duration,
            is_random=is_generic,
        )

    # Guarantee GIF strictly fits below MAX_INLINE_IMAGE_BYTES while staying animated GIF89a
    if len(gif_bytes) > MAX_INLINE_IMAGE_BYTES:
        gif_bytes = _render_procedural_gif(
            effective_prompt,
            frame_count=min(clamped_frames, 12),
            duration_ms=clamped_duration,
            width=256,
            height=160,
            is_random=is_generic,
        )

    saved_path = out_dir / f"{slug}-{stamp}-{nonce}.gif"
    saved_path.write_bytes(gif_bytes)

    width, height = _sniff_dimensions(gif_bytes)
    actual_frames = count_gif_frames(gif_bytes)
    b64 = base64.b64encode(gif_bytes).decode("ascii")
    alt = _clean_alt_text(effective_prompt)
    dims = f"{width}x{height}" if width > 0 and height > 0 else f"{len(gif_bytes):,} bytes"

    lines = [
        f"![{alt}](data:image/gif;base64,{b64})",
        "",
        f"Generated animated GIF via `{active_model}` ({dims}, {actual_frames} frames) · saved to `{saved_path}`.",
    ]
    if fallback_note:
        lines.append(fallback_note)

    return GeneratedImageResult(
        markdown="\n".join(lines),
        backend_kind=active_kind,
        model_id=active_model,
        mime_type="image/gif",
        width=width,
        height=height,
        byte_length=len(gif_bytes),
        saved_path=str(saved_path),
        used_fallback=used_fallback,
        fallback_note=fallback_note,
        frame_count=actual_frames,
    )
