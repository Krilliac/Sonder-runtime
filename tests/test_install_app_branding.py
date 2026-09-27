"""scripts/install_app_branding.py against real and minimal Flutter trees."""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from scripts import configure_flutter_networking as networking
from scripts import install_app_branding as branding
from scripts import release_artifacts

ROOT = Path(__file__).resolve().parents[1]
BRAND = branding.BRAND_DIR
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_header(path: Path) -> tuple[int, int, int]:
    """(width, height, colour type) from a PNG's IHDR chunk."""
    data = path.read_bytes()[:26]
    assert data[:8] == PNG_SIGNATURE, path
    assert data[12:16] == b"IHDR", path
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data[25]


def _flutter() -> str | None:
    found = shutil.which("flutter")
    if found:
        return found
    root = os.environ.get("FLUTTER_ROOT")
    if root and (Path(root) / "bin" / "flutter").is_file():
        return str(Path(root) / "bin" / "flutter")
    return None


# -- the committed renders ---------------------------------------------------


def test_display_name_is_one_value_across_build_scripts():
    # Both scripts run after flutter create; if they disagreed, whichever ran
    # last would win and the other's tests would lie.
    assert branding.DISPLAY_NAME == "Sonder"
    assert networking.PRODUCT_NAME == branding.DISPLAY_NAME
    # macOS names the bundle after PRODUCT_NAME; release integrity checks it.
    assert release_artifacts.MACOS_APP_BUNDLE == branding.DISPLAY_NAME + ".app"


@pytest.mark.parametrize(
    ("relative", "size"),
    [
        *[
            (f"android/res/mipmap-{d}/{layer}", round(base * f))
            for d, f in {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}.items()
            for layer, base in (
                ("ic_launcher.png", 48),
                ("ic_launcher_foreground.png", 108),
                ("ic_launcher_monochrome.png", 108),
            )
        ],
        *[(f"macos/AppIcon.appiconset/app_icon_{px}.png", px)
          for px in (16, 32, 64, 128, 256, 512, 1024)],
        ("linux/sonder.png", 512),
        ("web/favicon.png", 32),
        ("web/icons/Icon-192.png", 192),
        ("web/icons/Icon-512.png", 512),
        ("web/icons/Icon-maskable-192.png", 192),
        ("web/icons/Icon-maskable-512.png", 512),
    ],
)
def test_committed_icon_has_its_platform_size(relative, size):
    width, height, _ = _png_header(BRAND / relative)
    assert (width, height) == (size, size)


def test_ios_icons_are_opaque_at_their_declared_sizes():
    for name in branding.IOS_ICONS:
        match = re.match(r"Icon-App-([\d.]+)x[\d.]+@(\d)x\.png", name)
        expected = round(float(match.group(1)) * int(match.group(2)))
        width, height, colour_type = _png_header(BRAND / "ios/AppIcon.appiconset" / name)
        assert (width, height) == (expected, expected), name
        # App Store Connect rejects an app icon with an alpha channel.
        assert colour_type == 2, name


def test_windows_icon_carries_every_shell_size():
    data = (BRAND / "windows/app_icon.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = []
    for index in range(count):
        width, height = data[6 + 16 * index], data[7 + 16 * index]
        sizes.append((width or 256, height or 256))
    assert sorted(sizes) == [(px, px) for px in (16, 24, 32, 48, 64, 128, 256)]


def test_android_adaptive_icon_declares_all_three_layers():
    xml = (BRAND / "android/res/mipmap-anydpi-v26/ic_launcher.xml").read_text()
    assert '<background android:drawable="@color/ic_launcher_background"/>' in xml
    assert '<foreground android:drawable="@mipmap/ic_launcher_foreground"/>' in xml
    assert '<monochrome android:drawable="@mipmap/ic_launcher_monochrome"/>' in xml
    colours = (BRAND / "android/res/values/ic_launcher_background.xml").read_text()
    assert '<color name="ic_launcher_background">#15163A</color>' in colours


# -- a real flutter create tree ----------------------------------------------


@pytest.fixture(scope="module")
def created_app(tmp_path_factory):
    flutter = _flutter()
    if flutter is None:
        pytest.skip("needs the Flutter SDK (flutter on PATH or FLUTTER_ROOT)")
    app = tmp_path_factory.mktemp("flutter_app")
    # The Flutter tool generates every platform's template on any host; the
    # windows/macos/ios trees are the same files CI's native runners get.
    result = subprocess.run(
        [flutter, "create", "--no-pub", "--org", "com.sonder.runtime",
         "--project-name", "sonder_runtime",
         "--platforms=android,ios,linux,macos,web,windows", str(app)],
        capture_output=True, text=True, timeout=600, check=False,
        env={**os.environ, "CI": "true"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return app


def test_brands_every_flutter_create_platform_idempotently(created_app, tmp_path):
    app = tmp_path / "app"
    shutil.copytree(created_app, app)
    template_icon = (app / "android/app/src/main/res/mipmap-mdpi/ic_launcher.png").read_bytes()
    assert template_icon != (BRAND / "android/res/mipmap-mdpi/ic_launcher.png").read_bytes()

    changed = branding.install(app)

    assert changed
    res = app / "android/app/src/main/res"
    for density in branding.ANDROID_DENSITIES:
        for layer in ("ic_launcher.png", "ic_launcher_foreground.png", "ic_launcher_monochrome.png"):
            assert (res / f"mipmap-{density}" / layer).read_bytes() == (
                BRAND / f"android/res/mipmap-{density}" / layer
            ).read_bytes()
    assert (res / "mipmap-anydpi-v26/ic_launcher.xml").is_file()
    manifest = (app / "android/app/src/main/AndroidManifest.xml").read_text()
    assert 'android:label="Sonder"' in manifest
    assert 'android:icon="@mipmap/ic_launcher"' in manifest

    for name in branding.IOS_ICONS:
        assert (app / "ios/Runner/Assets.xcassets/AppIcon.appiconset" / name).read_bytes() == (
            BRAND / "ios/AppIcon.appiconset" / name
        ).read_bytes()
    ios_plist = plistlib.loads((app / "ios/Runner/Info.plist").read_bytes())
    assert ios_plist["CFBundleDisplayName"] == "Sonder"

    for name in branding.MACOS_ICONS:
        assert (app / "macos/Runner/Assets.xcassets/AppIcon.appiconset" / name).read_bytes() == (
            BRAND / "macos/AppIcon.appiconset" / name
        ).read_bytes()
    app_info = (app / "macos/Runner/Configs/AppInfo.xcconfig").read_text()
    assert re.search(r"(?m)^PRODUCT_NAME = Sonder$", app_info)

    runner = (app / "linux/runner/my_application.cc").read_text()
    assert 'gtk_header_bar_set_title(header_bar, "Sonder")' in runner
    assert 'gtk_window_set_title(window, "Sonder")' in runner
    assert runner.count(branding.LINUX_ICON_MARKER) == 1
    assert "gtk_window_set_icon_from_file(window, icon_path, nullptr)" in runner
    cmake = (app / "linux/CMakeLists.txt").read_text()
    assert cmake.count(branding.LINUX_CMAKE_MARKER) == 1
    assert (app / "linux/runner/resources/sonder.png").read_bytes() == (
        BRAND / "linux/sonder.png"
    ).read_bytes()

    assert (app / "windows/runner/resources/app_icon.ico").read_bytes() == (
        BRAND / "windows/app_icon.ico"
    ).read_bytes()
    assert 'window.Create(L"Sonder"' in (app / "windows/runner/main.cpp").read_text()
    rc = (app / "windows/runner/Runner.rc").read_text()
    assert 'VALUE "FileDescription", "Sonder" "\\0"' in rc
    assert 'VALUE "ProductName", "Sonder" "\\0"' in rc

    for name in branding.WEB_ICONS:
        assert (app / "web" / name).read_bytes() == (BRAND / "web" / name).read_bytes()
    web_manifest = json.loads((app / "web/manifest.json").read_text())
    assert web_manifest["name"] == web_manifest["short_name"] == "Sonder"
    assert web_manifest["theme_color"] == "#15163A"
    assert [icon["src"] for icon in web_manifest["icons"]] == [
        "icons/Icon-192.png", "icons/Icon-512.png",
        "icons/Icon-maskable-192.png", "icons/Icon-maskable-512.png",
    ]
    index = (app / "web/index.html").read_text()
    assert "<title>Sonder</title>" in index
    assert '<meta name="apple-mobile-web-app-title" content="Sonder">' in index

    # Idempotent, and the identity script that runs next agrees on the name,
    # so a later rebrand pass finds nothing to change.
    assert branding.install(app) == []
    networking.configure(app)
    assert branding.install(app) == []
    assert runner == (app / "linux/runner/my_application.cc").read_text()


def test_cli_brands_only_the_requested_platform(created_app, tmp_path, capsys):
    app = tmp_path / "app"
    shutil.copytree(created_app / "web", app / "web")
    shutil.copytree(created_app / "android", app / "android")
    assert "<title>sonder_runtime</title>" in (app / "web/index.html").read_text()

    assert branding.main([str(app), "--platforms", "web"]) == 0

    assert "index.html" in capsys.readouterr().out
    assert "<title>Sonder</title>" in (app / "web/index.html").read_text()
    # Android was generated but not requested, so it keeps the template.
    assert 'android:label="sonder_runtime"' in (
        app / "android/app/src/main/AndroidManifest.xml"
    ).read_text()


# -- fail loudly -------------------------------------------------------------


def test_missing_template_icon_fails_instead_of_shipping_the_default(created_app, tmp_path, capsys):
    app = tmp_path / "app"
    shutil.copytree(created_app / "android", app / "android")
    (app / "android/app/src/main/res/mipmap-xhdpi/ic_launcher.png").unlink()

    assert branding.main([str(app), "--platforms", "android"]) == 1

    err = capsys.readouterr().err
    assert "Flutter template icon is missing" in err
    assert "mipmap-xhdpi" in err


@pytest.mark.parametrize(
    ("relative", "needle", "replacement", "message"),
    [
        ("windows/runner/main.cpp", 'window.Create(L"', "window.Make(L\"", "window.Create title"),
        ("linux/runner/my_application.cc", "gtk_window_set_title(", "gtk_window_title(", "window title"),
        ("macos/Runner/Configs/AppInfo.xcconfig", "PRODUCT_NAME", "APP_NAME", "PRODUCT_NAME"),
        ("web/index.html", "<title>", "<name>", "<title>"),
        ("android/app/src/main/AndroidManifest.xml", "android:label=", "android:name2=", "android:label"),
    ],
)
def test_template_drift_in_an_edited_file_fails(created_app, tmp_path, relative, needle,
                                                replacement, message):
    platform = relative.split("/")[0]
    app = tmp_path / "app"
    shutil.copytree(created_app / platform, app / platform)
    target = app / relative
    text = target.read_text()
    assert needle in text
    target.write_text(text.replace(needle, replacement))

    with pytest.raises(branding.BrandingError, match=re.escape(message)):
        branding.install(app, [platform])


def test_missing_platform_folder_and_empty_tree_fail(tmp_path):
    with pytest.raises(branding.BrandingError, match="platform folder is missing"):
        branding.install(tmp_path, ["windows"])
    with pytest.raises(branding.BrandingError, match="no flutter create platform folders"):
        branding.install(tmp_path)
    with pytest.raises(branding.BrandingError, match="unknown platform"):
        branding.install(tmp_path, ["fuchsia"])


def test_missing_windows_resource_file_fails_without_flutter(tmp_path):
    runner = tmp_path / "windows/runner"
    (runner / "resources").mkdir(parents=True)
    (runner / "resources/app_icon.ico").write_bytes(b"template")
    (runner / "main.cpp").write_text('if (!window.Create(L"sonder_runtime", origin, size)) {\n')

    with pytest.raises(branding.BrandingError, match="Windows Runner.rc is missing"):
        branding.install(tmp_path, ["windows"])


# -- wired into every build --------------------------------------------------


def test_every_flutter_create_in_ci_is_followed_by_branding():
    lines = (ROOT / ".github/workflows/build-apps.yml").read_text(encoding="utf-8").splitlines()
    creates = [i for i, line in enumerate(lines) if "flutter create " in line and "run:" in line]
    assert len(creates) == 6
    for index in creates:
        platform = re.search(r"--platforms=(\w+)", lines[index]).group(1)
        assert lines[index + 1].strip() == "- name: Install Sonder app branding"
        assert lines[index + 2].strip() == (
            f"run: python ../scripts/install_app_branding.py . --platforms {platform}"
        )
    workflow = "\n".join(lines)
    assert "Sonder Runtime.app" not in workflow
    assert '"build/macos/Build/Products/Release/Sonder.app/Contents/Resources/local-system"' in workflow


def test_local_builder_brands_after_flutter_create():
    text = (ROOT / "scripts/build_flutter_local.ps1").read_text(encoding="utf-8")
    create = text.index("create --org com.sonder.runtime")
    brand = text.index("install_app_branding.py")
    configure = text.index("configure_flutter_networking.py")
    assert create < brand < configure
