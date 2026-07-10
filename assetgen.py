"""Deterministic, stdlib-only procedural assets for arbitrary user requests.

The generator deliberately emits simple, documented formats that every language
surface in this repository can consume without package installation: PNG, PPM,
SVG, HTML, Markdown, CSV, PCM WAV, Wavefront OBJ/MTL, and JSON. Packs are useful
for branding, UI, documents, data prototypes, media, games, and other greenfield
work. Output stays under the local-llm workspace.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import random
import re
import struct
import wave
import zlib


DIMENSIONS = {"2d", "2.5d", "3d"}
THEMES = {
    "ember": ((16, 20, 28), (223, 74, 52), (255, 179, 71), (83, 35, 48)),
    "verdant": ((12, 24, 24), (54, 166, 111), (176, 232, 138), (39, 77, 68)),
    "arcane": ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91)),
    "frost": ((13, 24, 38), (69, 147, 203), (194, 235, 255), (39, 73, 103)),
}
ARTIFACT_KINDS = {
    "icon", "background", "tileset", "sprite_sheet", "texture", "preview",
    "vector", "diagram", "palette", "document", "data", "web",
    "sound", "music", "model", "scene",
}
OWNED_FILENAMES = {
    "background.png", "brief.md", "data.csv", "data.json", "diagram.svg",
    "hit.wav", "icon.png", "manifest.json", "materials.mtl", "models.obj",
    "palette.json", "pickup.wav", "preview.html", "preview.ppm", "request.json",
    "scene.json", "sprites.png", "texture.png", "theme.wav", "tiles.png",
    "vector.svg",
}
MAX_NAME = 48
MAX_IMAGE_SIDE = 512
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def workspace_root() -> str:
    return os.path.abspath(os.path.dirname(__file__))


def default_asset_root() -> str:
    return os.path.join(workspace_root(), "artifacts", "generated")


def _safe_slug(value: str, label: str = "name") -> str:
    slug = (value or "").strip().lower().replace(" ", "-")
    if not _SLUG.fullmatch(slug):
        raise ValueError(
            "%s must match %s and be at most %d characters" %
            (label, _SLUG.pattern, MAX_NAME)
        )
    return slug


def _inside_workspace(path: str) -> bool:
    root = os.path.realpath(workspace_root())
    try:
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except ValueError:
        return False


def resolve_pack_dir(name: str, output_dir: str = "") -> str:
    slug = _safe_slug(name)
    base = output_dir.strip() if output_dir else default_asset_root()
    if not os.path.isabs(base):
        base = os.path.join(workspace_root(), base)
    base = os.path.abspath(base)
    if not _inside_workspace(base):
        raise ValueError("asset output must stay inside workspace: %r" % output_dir)
    target = os.path.abspath(os.path.join(base, slug))
    if not _inside_workspace(target):
        raise ValueError("unsafe asset pack path")
    return target


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload +
        struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_png(path: str, width: int, height: int, rgba: bytes) -> None:
    width = max(1, min(int(width), MAX_IMAGE_SIDE))
    height = max(1, min(int(height), MAX_IMAGE_SIDE))
    expected = width * height * 4
    if len(rgba) != expected:
        raise ValueError("RGBA buffer is %d bytes; expected %d" % (len(rgba), expected))
    stride = width * 4
    rows = b"".join(
        b"\x00" + rgba[y * stride:(y + 1) * stride]
        for y in range(height)
    )
    payload = (
        b"\x89PNG\r\n\x1a\n" +
        _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
        _chunk(b"IDAT", zlib.compress(rows, 9)) +
        _chunk(b"IEND", b"")
    )
    with open(path, "wb") as handle:
        handle.write(payload)


class Canvas:
    def __init__(self, width: int, height: int, color=(0, 0, 0, 255)):
        self.width = max(1, min(int(width), MAX_IMAGE_SIDE))
        self.height = max(1, min(int(height), MAX_IMAGE_SIDE))
        self.pixels = bytearray(bytes(color) * (self.width * self.height))

    def pixel(self, x: int, y: int, color) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            index = (y * self.width + x) * 4
            self.pixels[index:index + 4] = bytes(color)

    def rect(self, x: int, y: int, width: int, height: int, color) -> None:
        for py in range(max(0, y), min(self.height, y + height)):
            for px in range(max(0, x), min(self.width, x + width)):
                self.pixel(px, py, color)

    def circle(self, cx: int, cy: int, radius: int, color) -> None:
        rr = radius * radius
        for y in range(cy - radius, cy + radius + 1):
            for x in range(cx - radius, cx + radius + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= rr:
                    self.pixel(x, y, color)

    def diamond(self, cx: int, cy: int, rx: int, ry: int, color) -> None:
        for y in range(cy - ry, cy + ry + 1):
            span = int(rx * (1.0 - abs(y - cy) / float(max(1, ry))))
            for x in range(cx - span, cx + span + 1):
                self.pixel(x, y, color)

    def line(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.pixel(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            twice = 2 * err
            if twice >= dy:
                err += dy
                x0 += sx
            if twice <= dx:
                err += dx
                y0 += sy

    def save_png(self, path: str) -> None:
        write_png(path, self.width, self.height, bytes(self.pixels))

    def save_ppm(self, path: str) -> None:
        with open(path, "wb") as handle:
            header = "P6\n%d %d\n255\n" % (self.width, self.height)
            handle.write(header.encode("ascii"))
            rgb = bytearray()
            for index in range(0, len(self.pixels), 4):
                rgb.extend(self.pixels[index:index + 3])
            handle.write(rgb)


def _rgba(rgb, alpha=255):
    return tuple(rgb) + (alpha,)


def _shade(rgb, amount):
    return tuple(max(0, min(255, int(value + amount))) for value in rgb)


def _background(path: str, palette, seed: int) -> None:
    rng = random.Random(seed)
    canvas = Canvas(256, 144)
    base, accent, bright, _ = palette
    for y in range(canvas.height):
        blend = y / float(canvas.height - 1)
        color = tuple(int(base[i] * (1 - blend) + accent[i] * blend * 0.35) for i in range(3))
        canvas.rect(0, y, canvas.width, 1, _rgba(color))
    for _ in range(55):
        x, y = rng.randrange(canvas.width), rng.randrange(4, 92)
        canvas.circle(x, y, rng.choice((1, 1, 2)), _rgba(bright, rng.randrange(120, 256)))
    canvas.save_png(path)


def _tiles(path: str, palette, seed: int, dimension: str) -> None:
    rng = random.Random(seed)
    canvas = Canvas(128, 32, _rgba(palette[0]))
    _, accent, bright, earth = palette
    for tile in range(4):
        ox = tile * 32
        base = _shade(earth if tile % 2 == 0 else accent, tile * 6 - 8)
        if dimension == "2.5d":
            canvas.diamond(ox + 16, 13, 15, 9, _rgba(base))
            canvas.diamond(ox + 16, 11, 14, 8, _rgba(_shade(base, 18)))
        else:
            canvas.rect(ox + 1, 1, 30, 30, _rgba(base))
            canvas.rect(ox + 2, 2, 28, 2, _rgba(_shade(base, 22)))
        for _ in range(18):
            x, y = ox + rng.randrange(4, 28), rng.randrange(5, 27)
            canvas.pixel(x, y, _rgba(bright if rng.random() < 0.15 else _shade(base, 12)))
    canvas.save_png(path)


def _sprites(path: str, palette, seed: int) -> None:
    rng = random.Random(seed)
    canvas = Canvas(128, 32, (0, 0, 0, 0))
    base, accent, bright, earth = palette
    for frame in range(4):
        ox = frame * 32
        bob = frame % 2
        canvas.circle(ox + 16, 8 + bob, 5, _rgba(bright))
        canvas.rect(ox + 10, 13 + bob, 12, 11, _rgba(accent))
        canvas.rect(ox + 8, 14 + bob, 3, 9, _rgba(_shade(accent, -18)))
        canvas.rect(ox + 22, 14 + bob, 3, 9, _rgba(_shade(accent, -18)))
        canvas.rect(ox + 11 + frame % 2, 24 + bob, 4, 6, _rgba(earth))
        canvas.rect(ox + 18 - frame % 2, 24 + bob, 4, 6, _rgba(earth))
        canvas.pixel(ox + 14, 7 + bob, _rgba(base))
        canvas.pixel(ox + 18, 7 + bob, _rgba(base))
        canvas.pixel(ox + rng.choice((12, 20)), 17 + bob, _rgba(bright))
    canvas.save_png(path)


def _texture(path: str, palette, seed: int) -> None:
    rng = random.Random(seed)
    canvas = Canvas(64, 64)
    base, accent, bright, earth = palette
    for y in range(64):
        for x in range(64):
            checker = ((x // 8) + (y // 8)) % 2
            source = accent if checker else earth
            jitter = rng.randrange(-10, 11)
            canvas.pixel(x, y, _rgba(_shade(source, jitter)))
    for x in range(0, 64, 8):
        canvas.line(x, 0, x, 63, _rgba(_shade(bright, -55)))
    for y in range(0, 64, 8):
        canvas.line(0, y, 63, y, _rgba(_shade(base, 15)))
    canvas.save_png(path)


def _icon(path: str, palette, seed: int) -> None:
    rng = random.Random(seed)
    canvas = Canvas(128, 128, _rgba(palette[0]))
    _, accent, bright, earth = palette
    canvas.circle(64, 64, 48, _rgba(earth))
    canvas.circle(64, 64, 43, _rgba(accent))
    points = [(64, 24), (75, 50), (104, 52), (82, 71), (89, 101),
              (64, 84), (39, 101), (46, 71), (24, 52), (53, 50)]
    for index in range(len(points)):
        canvas.line(*points[index], *points[(index + 1) % len(points)], _rgba(bright))
    canvas.circle(64, 64, 13, _rgba(bright))
    for _ in range(24):
        canvas.pixel(rng.randrange(22, 106), rng.randrange(22, 106), _rgba(_shade(bright, -35)))
    canvas.save_png(path)


def _preview(path: str, palette, dimension: str) -> None:
    canvas = Canvas(160, 90, _rgba(palette[0]))
    _, accent, bright, earth = palette
    horizon = 46
    canvas.rect(0, horizon, 160, 44, _rgba(earth))
    if dimension == "2d":
        for x in range(0, 160, 16):
            canvas.rect(x + 1, 58 + (x // 16 % 2) * 4, 14, 14, _rgba(accent))
        canvas.circle(80, 44, 8, _rgba(bright))
    elif dimension == "2.5d":
        for row in range(5):
            for col in range(7):
                canvas.diamond(38 + col * 14 + row * 7, 41 + row * 8, 8, 4,
                               _rgba(_shade(accent, (row + col) % 3 * 8)))
        canvas.circle(82, 34, 6, _rgba(bright))
    else:
        canvas.line(25, 78, 80, 18, _rgba(bright))
        canvas.line(135, 78, 80, 18, _rgba(bright))
        canvas.line(25, 78, 135, 78, _rgba(bright))
        canvas.line(53, 48, 107, 48, _rgba(accent))
        canvas.line(53, 48, 80, 78, _rgba(accent))
        canvas.line(107, 48, 80, 78, _rgba(accent))
    canvas.save_ppm(path)


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def _write_vector(path: str, palette, brief: str) -> None:
    base, accent, bright, earth = palette
    title = html.escape(brief[:96], quote=True)
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="%s">
  <rect width="512" height="512" rx="96" fill="%s"/>
  <circle cx="256" cy="256" r="178" fill="%s" stroke="%s" stroke-width="18"/>
  <path d="M256 92 302 207 426 214 330 292 360 414 256 346 152 414 182 292 86 214 210 207Z" fill="%s"/>
  <circle cx="256" cy="256" r="50" fill="%s"/>
  <title>%s</title>
</svg>
""" % (title, _hex(base), _hex(earth), _hex(accent), _hex(bright), _hex(accent), title)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(svg)


def _write_diagram(path: str, palette, brief: str) -> None:
    base, accent, bright, earth = palette
    title = html.escape(brief[:96], quote=True)
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 540" role="img" aria-label="%s">
  <defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="%s"/></marker></defs>
  <rect width="960" height="540" rx="28" fill="%s"/>
  <text x="48" y="70" fill="%s" font-family="Segoe UI, sans-serif" font-size="30">%s</text>
  <g font-family="Segoe UI, sans-serif" font-size="22" text-anchor="middle">
    <rect x="70" y="210" width="180" height="96" rx="20" fill="%s"/><text x="160" y="267" fill="white">Input</text>
    <rect x="390" y="120" width="180" height="96" rx="20" fill="%s"/><text x="480" y="177" fill="white">Create</text>
    <rect x="390" y="330" width="180" height="96" rx="20" fill="%s"/><text x="480" y="387" fill="white">Verify</text>
    <rect x="710" y="210" width="180" height="96" rx="20" fill="%s"/><text x="800" y="267" fill="white">Deliver</text>
  </g>
  <g stroke="%s" stroke-width="8" fill="none" marker-end="url(#arrow)"><path d="M250 242 C320 242 320 168 390 168"/><path d="M250 274 C320 274 320 378 390 378"/><path d="M570 168 C650 168 640 242 710 242"/><path d="M570 378 C650 378 640 274 710 274"/></g>
</svg>
""" % (
        title, _hex(bright), _hex(base), _hex(bright), title,
        _hex(accent), _hex(earth), _hex(accent), _hex(earth), _hex(bright),
    )
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(svg)


def _write_palette(path: str, palette, theme: str) -> None:
    names = ("canvas", "accent", "highlight", "surface")
    payload = {
        "schema": 1,
        "theme": theme,
        "colors": [
            {"name": name, "hex": _hex(rgb), "rgb": list(rgb)}
            for name, rgb in zip(names, palette)
        ],
    }
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_document(path: str, brief: str, dimension: str, theme: str, kinds) -> None:
    text = """# Generated artifact brief

## Request

%s

## Direction

- Theme: `%s`
- Spatial treatment: `%s`
- Deliverables: %s

## Provenance

Generated locally by Trilobite's deterministic standard-library artifact forge.
No downloaded or third-party assets are included. See `manifest.json` for hashes.
""" % (brief.strip(), theme, dimension, ", ".join(sorted(kinds)))
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_data(root: str, seed: int, brief: str) -> None:
    rng = random.Random(seed)
    rows = []
    value = 42.0
    for index in range(24):
        value = max(0.0, value + rng.uniform(-5.0, 7.5))
        rows.append({"index": index + 1, "value": round(value, 3), "group": "ABC"[index % 3]})
    payload = {"schema": 1, "brief": brief, "rows": rows}
    with open(os.path.join(root, "data.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(os.path.join(root, "data.csv"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("index,value,group\n")
        for row in rows:
            handle.write("%d,%.3f,%s\n" % (row["index"], row["value"], row["group"]))


def _write_web_preview(path: str, palette, brief: str) -> None:
    base, accent, bright, earth = palette
    title = html.escape(brief[:120], quote=True)
    document = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>
:root{--canvas:%s;--accent:%s;--highlight:%s;--surface:%s}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 20%% 0%%,var(--surface),var(--canvas) 58%%);color:#fff;font:16px/1.5 system-ui,sans-serif}
main{width:min(900px,92vw);padding:56px;border:1px solid color-mix(in srgb,var(--highlight),transparent 65%%);border-radius:28px;background:color-mix(in srgb,var(--canvas),transparent 12%%);box-shadow:0 30px 90px #0008}
.eyebrow{color:var(--highlight);letter-spacing:.16em;text-transform:uppercase}h1{font-size:clamp(2.4rem,8vw,5.4rem);line-height:.92;margin:.3em 0}.button{display:inline-block;margin-top:22px;padding:13px 20px;border-radius:999px;background:var(--accent);font-weight:700}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:42px}.grid div{height:86px;border-radius:16px;background:linear-gradient(135deg,var(--surface),var(--accent))}@media(max-width:600px){main{padding:30px}.grid{grid-template-columns:1fr}}
</style><main><div class="eyebrow">Trilobite concept</div><h1>%s</h1><p>A self-contained, dependency-free web mockup generated directly from the request.</p><span class="button">Explore concept</span><div class="grid"><div></div><div></div><div></div></div></main></html>
""" % (title, _hex(base), _hex(accent), _hex(bright), _hex(earth), title)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(document)


def write_wav(path: str, frequency: float, duration: float, seed: int,
              waveform: str = "sine") -> None:
    rate = 22050
    duration = max(0.05, min(float(duration), 3.0))
    frames = int(rate * duration)
    rng = random.Random(seed)
    data = bytearray()
    for index in range(frames):
        t = index / float(rate)
        attack = min(1.0, index / float(max(1, int(rate * 0.02))))
        release = min(1.0, (frames - index) / float(max(1, int(rate * 0.08))))
        envelope = attack * release
        if waveform == "noise":
            sample = rng.uniform(-1.0, 1.0) * math.exp(-8.0 * t / duration)
        elif waveform == "square":
            sample = 1.0 if math.sin(2 * math.pi * frequency * t) >= 0 else -1.0
        else:
            sample = math.sin(2 * math.pi * frequency * t)
            sample += 0.28 * math.sin(2 * math.pi * frequency * 2.01 * t)
        value = int(max(-1.0, min(1.0, sample * envelope * 0.35)) * 32767)
        data.extend(struct.pack("<h", value))
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(data))


def _write_models(root: str, palette) -> None:
    _, accent, bright, _ = palette
    mtl = (
        "newmtl hero\nKd %.4f %.4f %.4f\nmap_Kd texture.png\n\n"
        "newmtl crystal\nKd %.4f %.4f %.4f\n" % (
            accent[0] / 255, accent[1] / 255, accent[2] / 255,
            bright[0] / 255, bright[1] / 255, bright[2] / 255,
        )
    )
    obj = """mtllib materials.mtl
o hero
usemtl hero
v -0.5 0.0 -0.5
v 0.5 0.0 -0.5
v 0.5 0.0 0.5
v -0.5 0.0 0.5
v -0.5 1.0 -0.5
v 0.5 1.0 -0.5
v 0.5 1.0 0.5
v -0.5 1.0 0.5
f 1 2 3 4
f 5 8 7 6
f 1 5 6 2
f 2 6 7 3
f 3 7 8 4
f 5 1 4 8
o crystal
usemtl crystal
v 1.5 0.0 0.0
v 2.0 0.5 0.0
v 1.5 1.4 0.0
v 1.0 0.5 0.0
v 1.5 0.5 0.5
v 1.5 0.5 -0.5
f 9 10 13
f 10 11 13
f 11 12 13
f 12 9 13
f 10 9 14
f 11 10 14
f 12 11 14
f 9 12 14
"""
    with open(os.path.join(root, "materials.mtl"), "w", encoding="utf-8") as handle:
        handle.write(mtl)
    with open(os.path.join(root, "models.obj"), "w", encoding="utf-8") as handle:
        handle.write(obj)


def _scene(dimension: str, theme: str, seed: int) -> dict:
    rng = random.Random(seed)
    grid = []
    for y in range(9):
        row = []
        for x in range(14):
            edge = x in (0, 13) or y in (0, 8)
            row.append(1 if edge else (2 if rng.random() < 0.12 else 0))
        grid.append(row)
    entities = [
        {"id": "player", "type": "hero", "position": [3, 3, 0], "hp": 100},
        {"id": "enemy-1", "type": "enemy", "position": [9, 5, 0], "hp": 35},
        {"id": "pickup-1", "type": "crystal", "position": [7, 2, 0], "value": 10},
    ]
    return {
        "schema": 1,
        "dimension": dimension,
        "theme": theme,
        "seed": seed,
        "tile_size": [32, 32 if dimension == "2d" else 16],
        "map": grid,
        "entities": entities,
        "camera": {"projection": "perspective" if dimension == "3d" else dimension},
    }


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_request(brief: str, kinds: str = "auto", dimension: str = "auto",
                  theme: str = "auto", seed: int | None = None) -> dict:
    """Turn a free-form artifact request into deterministic generator settings."""
    text = (brief or "").strip()
    lowered = text.lower()
    if not text:
        raise ValueError("brief is required")
    if theme == "auto":
        if any(word in lowered for word in ("fire", "fiery", "ember", "hell", "lava", "warm", "red")):
            theme = "ember"
        elif any(word in lowered for word in ("forest", "nature", "leaf", "earth", "green")):
            theme = "verdant"
        elif any(word in lowered for word in ("ice", "frost", "snow", "water", "ocean", "blue")):
            theme = "frost"
        else:
            theme = "arcane"
    if dimension == "auto":
        if any(word in lowered for word in ("3d", "mesh", "model", "sculpt")):
            dimension = "3d"
        elif any(word in lowered for word in ("2.5d", "isometric", "iso ")):
            dimension = "2.5d"
        else:
            dimension = "2d"
    if kinds == "auto":
        selected = set()
        rules = {
            "icon": ("icon", "logo", "badge", "avatar", "emblem"),
            "background": ("background", "wallpaper", "backdrop", "skybox"),
            "tileset": ("tile", "map", "terrain", "floor"),
            "sprite_sheet": ("sprite", "character", "animation", "creature"),
            "texture": ("texture", "material", "surface", "skin"),
            "preview": ("preview", "mockup", "concept", "wireframe"),
            "vector": ("vector", "svg", "logo", "illustration", "emblem"),
            "diagram": ("diagram", "flowchart", "architecture", "infographic", "process"),
            "palette": ("palette", "colors", "colour", "brand", "theme", "style guide"),
            "document": ("document", "brief", "copy", "readme", "brochure", "content"),
            "data": ("data", "dataset", "csv", "table", "chart", "sample records"),
            "web": ("website", "web page", "landing page", "dashboard", "html", "web mockup"),
            "sound": ("sound", "sfx", "audio", "explosion", "laser", "voice"),
            "music": ("music", "song", "theme", "loop", "ambient"),
            "model": ("3d", "mesh", "model", "object", "prop"),
            "scene": ("scene", "level", "world", "layout", "map"),
        }
        for kind, words in rules.items():
            if any(word in lowered for word in words):
                selected.add(kind)
        if not selected:
            selected = set(ARTIFACT_KINDS)
    elif kinds.strip().lower() in ("all", "pack", "everything"):
        selected = set(ARTIFACT_KINDS)
    else:
        selected = {part.strip().lower() for part in kinds.split(",") if part.strip()}
        unknown = selected - ARTIFACT_KINDS
        if unknown:
            raise ValueError("unknown artifact kinds: %s" % ", ".join(sorted(unknown)))
    if seed is None:
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    return {
        "brief": text,
        "kinds": sorted(selected),
        "dimension": dimension,
        "theme": theme,
        "seed": int(seed),
    }


def generate_artifacts(name: str, brief: str, kinds: str = "auto",
                       dimension: str = "auto", theme: str = "auto",
                       seed: int | None = None, output_dir: str = "") -> dict:
    request = infer_request(brief, kinds, dimension, theme, seed)
    dimension = request["dimension"]
    theme = request["theme"]
    if dimension not in DIMENSIONS:
        raise ValueError("dimension must be one of: %s" % ", ".join(sorted(DIMENSIONS)))
    if theme not in THEMES:
        raise ValueError("theme must be one of: %s" % ", ".join(sorted(THEMES)))
    seed = max(-2147483648, min(int(request["seed"]), 2147483647))
    request["seed"] = seed
    root = resolve_pack_dir(name, output_dir)
    os.makedirs(root, exist_ok=True)
    for filename in OWNED_FILENAMES:
        stale = os.path.join(root, filename)
        if os.path.lexists(stale):
            if os.path.isdir(stale) and not os.path.islink(stale):
                raise ValueError("generated file path is unexpectedly a directory: %s" % filename)
            os.remove(stale)
    palette = THEMES[theme]
    selected = set(request["kinds"])
    if "icon" in selected:
        _icon(os.path.join(root, "icon.png"), palette, seed)
    if "background" in selected:
        _background(os.path.join(root, "background.png"), palette, seed + 1)
    if "tileset" in selected:
        _tiles(os.path.join(root, "tiles.png"), palette, seed + 2, dimension)
    if "sprite_sheet" in selected:
        _sprites(os.path.join(root, "sprites.png"), palette, seed + 3)
    if "texture" in selected or "model" in selected:
        _texture(os.path.join(root, "texture.png"), palette, seed + 4)
    if "preview" in selected:
        _preview(os.path.join(root, "preview.ppm"), palette, dimension)
    if "vector" in selected:
        _write_vector(os.path.join(root, "vector.svg"), palette, request["brief"])
    if "diagram" in selected:
        _write_diagram(os.path.join(root, "diagram.svg"), palette, request["brief"])
    if "palette" in selected:
        _write_palette(os.path.join(root, "palette.json"), palette, theme)
    if "document" in selected:
        _write_document(
            os.path.join(root, "brief.md"), request["brief"], dimension, theme, selected,
        )
    if "data" in selected:
        _write_data(root, seed + 8, request["brief"])
    if "web" in selected:
        _write_web_preview(os.path.join(root, "preview.html"), palette, request["brief"])
    if "sound" in selected:
        write_wav(os.path.join(root, "pickup.wav"), 660.0, 0.18, seed + 5, "sine")
        write_wav(os.path.join(root, "hit.wav"), 120.0, 0.22, seed + 6, "noise")
    if "music" in selected:
        write_wav(os.path.join(root, "theme.wav"), 110.0, 1.4, seed + 7, "square")
    if "model" in selected:
        _write_models(root, palette)
    if "scene" in selected:
        with open(os.path.join(root, "scene.json"), "w", encoding="utf-8") as handle:
            json.dump(_scene(dimension, theme, seed), handle, indent=2, sort_keys=True)
            handle.write("\n")
    with open(os.path.join(root, "request.json"), "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2, sort_keys=True)
        handle.write("\n")

    files = []
    for filename in sorted(os.listdir(root)):
        path = os.path.join(root, filename)
        if filename == "manifest.json" or filename not in OWNED_FILENAMES or not os.path.isfile(path):
            continue
        files.append({"path": filename, "bytes": os.path.getsize(path), "sha256": _hash_file(path)})
    manifest = {
        "schema": 2,
        "generator": "trilobite-artifact-forge-stdlib",
        "name": _safe_slug(name),
        "brief": request["brief"],
        "kinds": request["kinds"],
        "dimension": dimension,
        "theme": theme,
        "seed": seed,
        "files": files,
    }
    manifest_path = os.path.join(root, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    manifest.update({
        "root": root,
        "manifest": manifest_path,
        "total_bytes": sum(row["bytes"] for row in files),
    })
    return manifest


def generate_pack(name: str, dimension: str = "2d", theme: str = "arcane",
                  seed: int = 1337, output_dir: str = "") -> dict:
    return generate_artifacts(
        name=name,
        brief="complete %s game and general creative asset pack" % dimension,
        kinds="all",
        dimension=dimension,
        theme=theme,
        seed=seed,
        output_dir=output_dir,
    )


def verify_pack(path: str) -> dict:
    if not os.path.isabs(path):
        path = os.path.join(workspace_root(), path)
    root = os.path.abspath(path)
    if not _inside_workspace(root):
        raise ValueError("asset pack must stay inside workspace")
    manifest_path = os.path.join(root, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    failures = []
    seen = set()
    for row in manifest.get("files", []):
        filename = row.get("path", "")
        candidate = os.path.abspath(os.path.join(root, filename))
        if filename in seen:
            failures.append("duplicate manifest path: %r" % filename)
        elif not _inside_workspace(candidate) or os.path.dirname(candidate) != root:
            failures.append("unsafe manifest path: %r" % filename)
        elif not os.path.isfile(candidate):
            failures.append("missing: %s" % filename)
        else:
            if os.path.getsize(candidate) != row.get("bytes"):
                failures.append("size mismatch: %s" % filename)
            if _hash_file(candidate) != row.get("sha256"):
                failures.append("hash mismatch: %s" % filename)
        seen.add(filename)
    return {
        "ok": not failures,
        "root": root,
        "checked": len(manifest.get("files", [])),
        "failures": failures,
        "manifest": manifest,
    }


def format_pack(result: dict) -> str:
    lines = [
        "asset pack: %s" % result.get("name", "?"),
        "  dimension/theme: %s / %s" % (result.get("dimension"), result.get("theme")),
        "  files: %d | bytes: %d" % (len(result.get("files", [])), result.get("total_bytes", 0)),
        "  root: %s" % result.get("root", ""),
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--dimension", default="2d", choices=sorted(DIMENSIONS))
    parser.add_argument("--theme", default="arcane", choices=sorted(THEMES))
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()
    print(format_pack(generate_pack(
        args.name, args.dimension, args.theme, args.seed, args.output_dir,
    )))
