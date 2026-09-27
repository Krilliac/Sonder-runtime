"""Render Sonder's app icons from the S mark into ``app/assets/brand/``.

Developer tool, not a build step: the rendered PNG/ICO files are committed and
``scripts/install_app_branding.py`` copies them into the platform folders that
``flutter create`` generates. Rerun this only when the mark or palette changes::

    python scripts/generate_app_icons.py            # needs playwright + pillow

Chromium (via Playwright) rasterises the SVG compositions so the arcs in the
mark match the published banners exactly; Pillow packs the Windows ``.ico``.

Compositions (all share one geometry: the split S path below, on a 420 grid):

* ``tile``     rounded navy square with the gradient S (desktop, web favicon,
               Android legacy launcher). Sizes <= 48 px use a larger glyph so
               the counters stay open at 16 px.
* ``full``     full-bleed navy square (iOS, which masks its own corners, and
               web maskable icons, whose safe zone is the central 80 % circle).
* ``macos``    Big Sur grid: 824/1024 rounded rectangle with a transparent
               margin and a soft drop shadow.
* ``adaptive`` Android adaptive foreground: transparent 108 dp canvas, glyph
               inside the 66 dp safe circle (the background layer is the
               ``ic_launcher_background`` colour resource).
* ``mono``     Android 13 themed-icon monochrome layer (white S, same box).
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "app" / "assets" / "brand"

# The mark, verbatim from the brand source (420 x 420 grid).
MARK_PATH = (
    "M382 32H151.95A113.95 107.5 0 0 0 151.95 247H164.65L222.37 173H151.95"
    "A39.95 33.5 0 0 1 151.95 106H324.28Z"
    "M38 388H268.05A113.95 107.5 0 0 0 268.05 173H255.35L197.63 247H268.05"
    "A39.95 33.5 0 0 1 268.05 314H95.72Z"
)
MARK_BOX = (38.0, 32.0, 382.0, 388.0)  # x0, y0, x1, y1 of the path
GRADIENT = ("#b56cf7", "#8a85fb", "#4fa6ff")
NAVY_TOP = "#16173d"
NAVY_BOTTOM = "#0b0c22"
NAVY = "#15163a"  # flat background (Android adaptive layer, web theme)
GLOW = "#7d6cff"

ANDROID_DENSITIES = {"mdpi": 1.0, "hdpi": 1.5, "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0}
IOS_ICONS = {
    "Icon-App-20x20@1x.png": 20, "Icon-App-20x20@2x.png": 40, "Icon-App-20x20@3x.png": 60,
    "Icon-App-29x29@1x.png": 29, "Icon-App-29x29@2x.png": 58, "Icon-App-29x29@3x.png": 87,
    "Icon-App-40x40@1x.png": 40, "Icon-App-40x40@2x.png": 80, "Icon-App-40x40@3x.png": 120,
    "Icon-App-60x60@2x.png": 120, "Icon-App-60x60@3x.png": 180,
    "Icon-App-76x76@1x.png": 76, "Icon-App-76x76@2x.png": 152,
    "Icon-App-83.5x83.5@2x.png": 167, "Icon-App-1024x1024@1x.png": 1024,
}
MACOS_SIZES = (16, 32, 64, 128, 256, 512, 1024)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
CANVAS = 1024


def _glyph(cx: float, cy: float, height: float, fill: str) -> str:
    """The S path scaled so its box is ``height`` tall, centred on (cx, cy)."""
    x0, y0, x1, y1 = MARK_BOX
    scale = height / (y1 - y0)
    tx = cx - (x0 + x1) / 2 * scale
    ty = cy - (y0 + y1) / 2 * scale
    return (
        f'<path fill="{fill}" transform="translate({tx:.4f} {ty:.4f}) '
        f'scale({scale:.6f})" d="{MARK_PATH}"/>'
    )


def _gradient_def() -> str:
    """Diagonal brand gradient across the mark's own box.

    ``userSpaceOnUse`` resolves in the path's coordinate system, which already
    includes the glyph's scale transform, so the ends are in 420-grid units.
    """
    x0, y0, x1, y1 = MARK_BOX
    stops = "".join(
        f'<stop offset="{offset}" stop-color="{color}"/>'
        for offset, color in zip(("0", ".5", "1"), GRADIENT)
    )
    return (
        f'<linearGradient id="sg" gradientUnits="userSpaceOnUse" '
        f'x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}">{stops}</linearGradient>'
    )


def _background_defs() -> str:
    return (
        f'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{NAVY_TOP}"/>'
        f'<stop offset="1" stop-color="{NAVY_BOTTOM}"/></linearGradient>'
        f'<radialGradient id="gl" cx=".5" cy=".5" r=".62">'
        f'<stop offset="0" stop-color="{GLOW}" stop-opacity=".26"/>'
        f'<stop offset="1" stop-color="{GLOW}" stop-opacity="0"/></radialGradient>'
    )


def compose(kind: str, size: int, render_px: int | None = None) -> str:
    """SVG source for one composition designed for ``size`` px (square).

    ``render_px`` draws that same design on a larger canvas (supersampling).
    """
    s = float(size)
    c = s / 2
    if kind == "tile":
        glyph_h = s * (0.66 if size <= 48 else 0.58)
        radius = s * 0.22
        body = (
            f'<rect width="{s}" height="{s}" rx="{radius:.3f}" fill="url(#bg)"/>'
            f'<rect width="{s}" height="{s}" rx="{radius:.3f}" fill="url(#gl)"/>'
        )
        defs = _background_defs() + _gradient_def()
        glyph = _glyph(c, c, glyph_h, "url(#sg)")
    elif kind == "full":
        glyph_h = s * 0.52
        body = (
            f'<rect width="{s}" height="{s}" fill="url(#bg)"/>'
            f'<rect width="{s}" height="{s}" fill="url(#gl)"/>'
        )
        defs = _background_defs() + _gradient_def()
        glyph = _glyph(c, c, glyph_h, "url(#sg)")
    elif kind == "macos":
        inset = s * 100 / 1024
        side = s - 2 * inset
        radius = side * 0.225
        glyph_h = side * 0.54
        shadow = (
            '<filter id="sh" x="-20%" y="-20%" width="140%" height="140%">'
            f'<feDropShadow dx="0" dy="{s * 0.01:.3f}" stdDeviation="{s * 0.012:.3f}" '
            'flood-color="#000" flood-opacity=".35"/></filter>'
        )
        body = (
            f'<rect x="{inset:.3f}" y="{inset:.3f}" width="{side:.3f}" height="{side:.3f}" '
            f'rx="{radius:.3f}" fill="url(#bg)" filter="url(#sh)"/>'
            f'<rect x="{inset:.3f}" y="{inset:.3f}" width="{side:.3f}" height="{side:.3f}" '
            f'rx="{radius:.3f}" fill="url(#gl)"/>'
        )
        defs = _background_defs() + shadow + _gradient_def()
        glyph = _glyph(c, c, glyph_h, "url(#sg)")
    elif kind in ("adaptive", "mono"):
        # 108 dp canvas; the box diagonal (40 dp tall -> ~56 dp) stays well
        # inside the 66 dp safe circle every launcher mask preserves.
        glyph_h = s * 40 / 108
        body = ""
        if kind == "adaptive":
            defs = _gradient_def()
            glyph = _glyph(c, c, glyph_h, "url(#sg)")
        else:
            defs = ""
            glyph = _glyph(c, c, glyph_h, "#ffffff")
    elif kind == "mark":
        glyph_h = s * 0.86
        body = ""
        defs = _gradient_def()
        glyph = _glyph(c, c, glyph_h, "url(#sg)")
    else:
        raise ValueError(f"unknown icon composition: {kind}")
    out = render_px or size
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{out}" height="{out}" '
        f'viewBox="0 0 {s} {s}"><defs>{defs}</defs>{body}{glyph}</svg>'
    )


def _plan() -> dict[str, tuple[str, int]]:
    """Relative output path -> (composition, pixel size)."""
    plan: dict[str, tuple[str, int]] = {}
    for density, factor in ANDROID_DENSITIES.items():
        base = f"android/res/mipmap-{density}"
        plan[f"{base}/ic_launcher.png"] = ("tile", round(48 * factor))
        plan[f"{base}/ic_launcher_foreground.png"] = ("adaptive", round(108 * factor))
        plan[f"{base}/ic_launcher_monochrome.png"] = ("mono", round(108 * factor))
    for name, px in IOS_ICONS.items():
        plan[f"ios/AppIcon.appiconset/{name}"] = ("full", px)
    for px in MACOS_SIZES:
        plan[f"macos/AppIcon.appiconset/app_icon_{px}.png"] = ("macos", px)
    plan["linux/sonder.png"] = ("tile", 512)
    plan["web/favicon.png"] = ("tile", 32)
    plan["web/icons/Icon-192.png"] = ("tile", 192)
    plan["web/icons/Icon-512.png"] = ("tile", 512)
    plan["web/icons/Icon-maskable-192.png"] = ("full", 192)
    plan["web/icons/Icon-maskable-512.png"] = ("full", 512)
    return plan


ADAPTIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background"/>
    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>
    <monochrome android:drawable="@mipmap/ic_launcher_monochrome"/>
</adaptive-icon>
"""

BACKGROUND_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="ic_launcher_background">{NAVY.upper()}</color>
</resources>
"""


def render(out_dir: Path = BRAND_DIR, chromium: str | None = None) -> list[Path]:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    written: list[Path] = []
    plan = _plan()
    ico_frames: dict[int, Image.Image] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium)
        # One fixed canvas: headless Chromium resizes small viewports
        # unreliably, and supersampling then downscaling gives cleaner edges.
        page = browser.new_page(
            device_scale_factor=1, viewport={"width": CANVAS, "height": CANVAS}
        )

        def shoot(kind: str, px: int) -> Image.Image:
            page.set_content(
                "<html><body style='margin:0;background:transparent'>"
                + compose(kind, px, render_px=CANVAS)
                + "</body></html>"
            )
            image = Image.open(io.BytesIO(page.screenshot(omit_background=True)))
            image = image.convert("RGBA")
            if px != CANVAS:
                image = image.resize((px, px), Image.LANCZOS)
            return image

        for relative, (kind, px) in sorted(plan.items()):
            image = shoot(kind, px)
            target = out_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "full":
                # App Store and iOS reject icons with an alpha channel.
                image = image.convert("RGB")
            image.save(target, optimize=True)
            written.append(target)
        for px in ICO_SIZES:
            ico_frames[px] = shoot("tile", px)
        browser.close()

    frames = [ico_frames[px] for px in ICO_SIZES]
    ico = out_dir / "windows" / "app_icon.ico"
    ico.parent.mkdir(parents=True, exist_ok=True)
    # Pillow stores each explicitly appended frame at its own native size.
    frames[-1].save(
        ico, format="ICO", sizes=[(px, px) for px in ICO_SIZES], append_images=frames[:-1]
    )
    written.append(ico)

    anydpi = out_dir / "android" / "res" / "mipmap-anydpi-v26" / "ic_launcher.xml"
    anydpi.parent.mkdir(parents=True, exist_ok=True)
    anydpi.write_text(ADAPTIVE_XML, encoding="utf-8")
    colors = out_dir / "android" / "res" / "values" / "ic_launcher_background.xml"
    colors.parent.mkdir(parents=True, exist_ok=True)
    colors.write_text(BACKGROUND_XML, encoding="utf-8")
    source = out_dir / "sonder-mark.svg"
    source.write_text(compose("mark", 420) + "\n", encoding="utf-8")
    written.extend([anydpi, colors, source])
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=BRAND_DIR)
    parser.add_argument("--chromium", help="Chromium executable (default: Playwright's)")
    parser.add_argument("--print-svg", metavar="KIND:SIZE",
                        help="print one composition's SVG instead of rendering")
    args = parser.parse_args(argv)
    if args.print_svg:
        kind, _, size = args.print_svg.partition(":")
        print(compose(kind, int(size or 512)))
        return 0
    for path in render(args.out, args.chromium):
        print(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
