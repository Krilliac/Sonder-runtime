"""Install Sonder's icons and display name into ``flutter create`` output.

The repository does not commit Flutter's generated platform folders, so every
build regenerates them with the default Flutter icon and the raw
``sonder_runtime`` name. Run this right after ``flutter create``::

    python ../scripts/install_app_branding.py . --platforms android

It copies the committed renders in ``app/assets/brand/`` (see
``scripts/generate_app_icons.py``) over the template icons and sets the
display name to ``Sonder`` for each requested platform. It is idempotent, and
it fails loudly (exit 1, nothing half-explained) when a file it expects from
the Flutter template is missing: a silent skip would ship the default icon.
"""
from __future__ import annotations

import argparse
import json
import plistlib
import re
import shutil
import sys
from pathlib import Path

DISPLAY_NAME = "Sonder"
DESCRIPTION = "Sonder: private, local AI orchestration."
THEME_COLOR = "#15163A"
BRAND_DIR = Path(__file__).resolve().parents[1] / "app" / "assets" / "brand"
PLATFORMS = ("android", "ios", "linux", "macos", "web", "windows")

ANDROID_DENSITIES = ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi")
IOS_ICONS = (
    "Icon-App-20x20@1x.png", "Icon-App-20x20@2x.png", "Icon-App-20x20@3x.png",
    "Icon-App-29x29@1x.png", "Icon-App-29x29@2x.png", "Icon-App-29x29@3x.png",
    "Icon-App-40x40@1x.png", "Icon-App-40x40@2x.png", "Icon-App-40x40@3x.png",
    "Icon-App-60x60@2x.png", "Icon-App-60x60@3x.png",
    "Icon-App-76x76@1x.png", "Icon-App-76x76@2x.png",
    "Icon-App-83.5x83.5@2x.png", "Icon-App-1024x1024@1x.png",
)
MACOS_ICONS = tuple(f"app_icon_{px}.png" for px in (16, 32, 64, 128, 256, 512, 1024))
WEB_ICONS = (
    "favicon.png", "icons/Icon-192.png", "icons/Icon-512.png",
    "icons/Icon-maskable-192.png", "icons/Icon-maskable-512.png",
)
LINUX_ICON = "sonder.png"
LINUX_ICON_MARKER = "// Sonder branding: window icon"
LINUX_ICON_CODE = f"""
  {LINUX_ICON_MARKER} (installed next to flutter_assets by CMake).
  {{
    g_autofree gchar* exe_path = g_file_read_link("/proc/self/exe", nullptr);
    if (exe_path != nullptr) {{
      g_autofree gchar* exe_dir = g_path_get_dirname(exe_path);
      g_autofree gchar* icon_path =
          g_build_filename(exe_dir, "data", "{LINUX_ICON}", nullptr);
      gtk_window_set_icon_from_file(window, icon_path, nullptr);
    }}
  }}
"""
LINUX_CMAKE_MARKER = "# Sonder branding: window icon"
LINUX_CMAKE_RULE = f"""
{LINUX_CMAKE_MARKER} read by runner/my_application.cc.
install(FILES "${{CMAKE_CURRENT_SOURCE_DIR}}/runner/resources/{LINUX_ICON}"
  DESTINATION "${{INSTALL_BUNDLE_DATA_DIR}}" COMPONENT Runtime)
"""


class BrandingError(RuntimeError):
    """A generated platform tree lacks something the branding step needs."""


def _require(path: Path, what: str) -> Path:
    if not path.is_file():
        raise BrandingError(f"{what} is missing: {path}")
    return path


def _copy(source: Path, target: Path, changed: list[Path]) -> None:
    _require(source, "brand asset")
    if target.is_file() and target.read_bytes() == source.read_bytes():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    changed.append(target)


def _replace_icon(source: Path, target: Path, changed: list[Path]) -> None:
    """Overwrite a template icon; its absence means the template changed."""
    _require(target, "Flutter template icon")
    _copy(source, target, changed)


def _write(path: Path, text: str, changed: list[Path]) -> None:
    if path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
        changed.append(path)


def _sub_required(pattern: str, replacement: str, text: str, path: Path, what: str,
                  *, count: int = 0, flags: int = 0) -> str:
    updated, hits = re.subn(pattern, replacement, text, count=count, flags=flags)
    if not hits:
        raise BrandingError(f"{what} not found in {path}")
    return updated


def brand_android(app_root: Path, brand: Path, changed: list[Path]) -> None:
    main = app_root / "android" / "app" / "src" / "main"
    manifest = _require(main / "AndroidManifest.xml", "Android manifest")
    text = manifest.read_text(encoding="utf-8")
    text = _sub_required(r'android:label="[^"]*"', f'android:label="{DISPLAY_NAME}"',
                         text, manifest, "android:label", count=1)
    text = _sub_required(r'android:icon="[^"]*"', 'android:icon="@mipmap/ic_launcher"',
                         text, manifest, "android:icon", count=1)
    _write(manifest, text, changed)

    res = main / "res"
    source = brand / "android" / "res"
    for density in ANDROID_DENSITIES:
        folder = f"mipmap-{density}"
        _replace_icon(source / folder / "ic_launcher.png", res / folder / "ic_launcher.png",
                      changed)
        for layer in ("ic_launcher_foreground.png", "ic_launcher_monochrome.png"):
            _copy(source / folder / layer, res / folder / layer, changed)
    for extra in ("mipmap-anydpi-v26/ic_launcher.xml", "values/ic_launcher_background.xml"):
        _copy(source / extra, res / extra, changed)


def _set_plist_names(plist: Path, changed: list[Path]) -> None:
    _require(plist, "Info.plist")
    original = plist.read_bytes()
    try:
        payload = plistlib.loads(original)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise BrandingError(f"Info.plist is invalid: {plist}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BrandingError(f"Info.plist has no root dictionary: {plist}")
    if payload.get("CFBundleDisplayName") == DISPLAY_NAME and payload.get("CFBundleName") == DISPLAY_NAME:
        return
    payload["CFBundleDisplayName"] = DISPLAY_NAME
    payload["CFBundleName"] = DISPLAY_NAME
    plist.write_bytes(plistlib.dumps(payload, sort_keys=False))
    changed.append(plist)


def brand_ios(app_root: Path, brand: Path, changed: list[Path]) -> None:
    runner = app_root / "ios" / "Runner"
    iconset = runner / "Assets.xcassets" / "AppIcon.appiconset"
    for name in IOS_ICONS:
        _replace_icon(brand / "ios" / "AppIcon.appiconset" / name, iconset / name, changed)
    _set_plist_names(runner / "Info.plist", changed)


def brand_macos(app_root: Path, brand: Path, changed: list[Path]) -> None:
    runner = app_root / "macos" / "Runner"
    iconset = runner / "Assets.xcassets" / "AppIcon.appiconset"
    for name in MACOS_ICONS:
        _replace_icon(brand / "macos" / "AppIcon.appiconset" / name, iconset / name, changed)
    app_info = _require(runner / "Configs" / "AppInfo.xcconfig", "macOS AppInfo.xcconfig")
    text = _sub_required(r"(?m)^PRODUCT_NAME\s*=.*$", f"PRODUCT_NAME = {DISPLAY_NAME}",
                         app_info.read_text(encoding="utf-8"), app_info, "PRODUCT_NAME")
    _write(app_info, text, changed)


def brand_linux(app_root: Path, brand: Path, changed: list[Path]) -> None:
    linux = app_root / "linux"
    runner = _require(linux / "runner" / "my_application.cc", "Linux runner")
    text = runner.read_text(encoding="utf-8")
    text = _sub_required(r'(gtk_header_bar_set_title\([^,]+,\s*)"[^"]*"',
                         rf'\1"{DISPLAY_NAME}"', text, runner, "header bar title")
    text = _sub_required(r'(gtk_window_set_title\([^,]+,\s*)"[^"]*"',
                         rf'\1"{DISPLAY_NAME}"', text, runner, "window title")
    if LINUX_ICON_MARKER not in text:
        anchor = re.search(r"(?m)^\s*gtk_window_set_default_size\(window,[^\n]*\n", text)
        if anchor is None:
            raise BrandingError(f"gtk_window_set_default_size not found in {runner}")
        text = text[:anchor.end()] + LINUX_ICON_CODE + text[anchor.end():]
    _write(runner, text, changed)

    cmake = _require(linux / "CMakeLists.txt", "Linux CMakeLists.txt")
    text = cmake.read_text(encoding="utf-8")
    if "INSTALL_BUNDLE_DATA_DIR" not in text:
        raise BrandingError(f"INSTALL_BUNDLE_DATA_DIR not found in {cmake}")
    if LINUX_CMAKE_MARKER not in text:
        text = text.rstrip("\n") + "\n" + LINUX_CMAKE_RULE
    _write(cmake, text, changed)
    _copy(brand / "linux" / LINUX_ICON, linux / "runner" / "resources" / LINUX_ICON, changed)


def brand_windows(app_root: Path, brand: Path, changed: list[Path]) -> None:
    runner = app_root / "windows" / "runner"
    _replace_icon(brand / "windows" / "app_icon.ico", runner / "resources" / "app_icon.ico",
                  changed)
    main = _require(runner / "main.cpp", "Windows main.cpp")
    text = _sub_required(r'window\.Create\(L"[^"]*"', f'window.Create(L"{DISPLAY_NAME}"',
                         main.read_text(encoding="utf-8"), main, "window.Create title")
    _write(main, text, changed)
    resources = _require(runner / "Runner.rc", "Windows Runner.rc")
    text = resources.read_text(encoding="utf-8")
    if "resources\\\\app_icon.ico" not in text:
        raise BrandingError(f"app_icon.ico resource not found in {resources}")
    for field in ("FileDescription", "ProductName"):
        text = _sub_required(rf'(VALUE\s+"{field}",\s*)"[^"]*"', rf'\1"{DISPLAY_NAME}"',
                             text, resources, f"{field} value")
    _write(resources, text, changed)


def brand_web(app_root: Path, brand: Path, changed: list[Path]) -> None:
    web = app_root / "web"
    for name in WEB_ICONS:
        _replace_icon(brand / "web" / name, web / name, changed)
    manifest = _require(web / "manifest.json", "web manifest")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BrandingError(f"web manifest is invalid: {manifest}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BrandingError(f"web manifest has no root object: {manifest}")
    payload.update(
        name=DISPLAY_NAME,
        short_name=DISPLAY_NAME,
        description=DESCRIPTION,
        background_color=THEME_COLOR,
        theme_color=THEME_COLOR,
    )
    _write(manifest, json.dumps(payload, indent=4) + "\n", changed)

    index = _require(web / "index.html", "web index.html")
    text = index.read_text(encoding="utf-8")
    text = _sub_required(r"<title>[^<]*</title>", f"<title>{DISPLAY_NAME}</title>",
                         text, index, "<title>")
    text = _sub_required(r'(<meta name="apple-mobile-web-app-title" content=)"[^"]*"',
                         rf'\1"{DISPLAY_NAME}"', text, index, "apple-mobile-web-app-title")
    text = _sub_required(r'(<meta name="description" content=)"[^"]*"',
                         rf'\1"{DESCRIPTION}"', text, index, "meta description")
    _write(index, text, changed)


BRANDERS = {
    "android": brand_android,
    "ios": brand_ios,
    "linux": brand_linux,
    "macos": brand_macos,
    "web": brand_web,
    "windows": brand_windows,
}


def install(app_root: Path, platforms: list[str] | None = None,
            brand_dir: Path = BRAND_DIR) -> list[Path]:
    """Brand each platform folder; return the files that changed."""
    root = Path(app_root).resolve()
    if platforms is None:
        platforms = [name for name in PLATFORMS if (root / name).is_dir()]
        if not platforms:
            raise BrandingError(f"no flutter create platform folders under {root}")
    changed: list[Path] = []
    for name in platforms:
        if name not in BRANDERS:
            raise BrandingError(f"unknown platform: {name}")
        if not (root / name).is_dir():
            raise BrandingError(f"platform folder is missing (run flutter create first): {root / name}")
        BRANDERS[name](root, Path(brand_dir), changed)
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app_root", nargs="?", default="app")
    parser.add_argument(
        "--platforms",
        help="comma-separated platforms to brand (default: every generated folder)",
    )
    args = parser.parse_args(argv)
    platforms = None
    if args.platforms:
        platforms = [item.strip() for item in args.platforms.split(",") if item.strip()]
    try:
        changed = install(Path(args.app_root), platforms)
    except BrandingError as exc:
        print(f"install_app_branding: error: {exc}", file=sys.stderr)
        return 1
    for path in changed:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
