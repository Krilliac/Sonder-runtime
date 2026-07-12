"""Configure generated Flutter projects for Sonder Runtime identity/networking.

The repository intentionally does not commit Flutter's generated native trees.
Run this after ``flutter create``. Cleartext Android LAN access is an explicit
build-time choice because HTTPS is preferable whenever bearer tokens are used.
"""
from __future__ import annotations

import argparse
import plistlib
import re
from pathlib import Path


LOCAL_NETWORK_DESCRIPTION = (
    "Sonder Runtime connects to and controls the server you configure on your local network."
)
PRODUCT_NAME = "Sonder Runtime"
APPLICATION_ID = "com.sonder.runtime"
EXECUTABLE_NAME = "sonder"


def _write_text(path: Path, text: str) -> bool:
    current = path.read_text(encoding="utf-8")
    if current == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def configure_android(app_root: Path, allow_cleartext: bool = False) -> list[Path]:
    manifest = app_root / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    if not manifest.exists():
        return []
    changed = []
    text = manifest.read_text(encoding="utf-8")
    value = "true" if allow_cleartext else "false"
    if "android:label=" in text:
        text = re.sub(
            r'android:label="[^"]*"',
            'android:label="%s"' % PRODUCT_NAME,
            text,
            count=1,
        )
    else:
        if "<application" not in text:
            raise ValueError("generated Android manifest has no application element")
        text = text.replace(
            "<application",
            '<application\n        android:label="%s"' % PRODUCT_NAME,
            1,
        )
    if "android:usesCleartextTraffic=" in text:
        text = re.sub(
            r'android:usesCleartextTraffic="(?:true|false)"',
            'android:usesCleartextTraffic="%s"' % value,
            text,
            count=1,
        )
    else:
        if "<application" not in text:
            raise ValueError("generated Android manifest has no application element")
        text = text.replace(
            "<application",
            '<application\n        android:usesCleartextTraffic="%s"' % value,
            1,
        )
    if _write_text(manifest, text):
        changed.append(manifest)

    for relative in ("android/app/build.gradle.kts", "android/app/build.gradle"):
        gradle = app_root / relative
        if not gradle.exists():
            continue
        text = gradle.read_text(encoding="utf-8")
        if not re.search(r"(?:namespace|applicationId)\s*(?:=\s*)?\"", text):
            raise ValueError("generated Android Gradle file has no app identity")
        text = re.sub(
            r'namespace\s*=\s*"[^"]+"',
            'namespace = "%s"' % APPLICATION_ID,
            text,
        )
        text = re.sub(
            r'applicationId\s*=\s*"[^"]+"',
            'applicationId = "%s"' % APPLICATION_ID,
            text,
        )
        text = re.sub(
            r'namespace\s+"[^"]+"',
            'namespace "%s"' % APPLICATION_ID,
            text,
        )
        text = re.sub(
            r'applicationId\s+"[^"]+"',
            'applicationId "%s"' % APPLICATION_ID,
            text,
        )
        if _write_text(gradle, text):
            changed.append(gradle)

    kotlin_root = app_root / "android" / "app" / "src" / "main" / "kotlin"
    java_root = app_root / "android" / "app" / "src" / "main" / "java"
    activities = []
    if kotlin_root.exists():
        activities.extend(kotlin_root.rglob("MainActivity.kt"))
    if java_root.exists():
        activities.extend(java_root.rglob("MainActivity.java"))
    for activity in activities:
        text = activity.read_text(encoding="utf-8")
        terminator = ";" if activity.suffix == ".java" else ""
        updated, count = re.subn(
            r"(?m)^package\s+[A-Za-z0-9_.]+;?\s*$",
            "package %s%s" % (APPLICATION_ID, terminator),
            text,
            count=1,
        )
        if count != 1:
            raise ValueError("generated Android MainActivity has no package")
        if _write_text(activity, updated):
            changed.append(activity)
    return changed


def configure_apple(app_root: Path) -> list[Path]:
    changed = []
    for relative in ("ios/Runner/Info.plist", "macos/Runner/Info.plist"):
        plist = app_root / relative
        if not plist.exists():
            continue
        try:
            payload = plistlib.loads(plist.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            raise ValueError("generated Apple plist is invalid: %s" % exc) from exc
        if not isinstance(payload, dict):
            raise ValueError("generated Apple plist has no root dictionary")
        payload["NSLocalNetworkUsageDescription"] = LOCAL_NETWORK_DESCRIPTION
        payload["CFBundleDisplayName"] = PRODUCT_NAME
        payload["CFBundleName"] = PRODUCT_NAME
        plist.write_bytes(plistlib.dumps(payload, sort_keys=False))
        changed.append(plist)

    for project in (
        app_root / "ios" / "Runner.xcodeproj" / "project.pbxproj",
        app_root / "macos" / "Runner.xcodeproj" / "project.pbxproj",
    ):
        if not project.exists():
            continue
        text = project.read_text(encoding="utf-8")

        def bundle_id(match):
            current = match.group(1)
            suffix = ".RunnerTests" if "RunnerTests" in current else ""
            return "PRODUCT_BUNDLE_IDENTIFIER = %s%s;" % (APPLICATION_ID, suffix)

        text = re.sub(
            r"PRODUCT_BUNDLE_IDENTIFIER\s*=\s*([^;]+);",
            bundle_id,
            text,
        )
        if _write_text(project, text):
            changed.append(project)

    app_info = app_root / "macos" / "Runner" / "Configs" / "AppInfo.xcconfig"
    if app_info.exists():
        text = app_info.read_text(encoding="utf-8")
        text = re.sub(
            r"(?m)^PRODUCT_NAME\s*=.*$", "PRODUCT_NAME = %s" % PRODUCT_NAME, text
        )
        text = re.sub(
            r"(?m)^PRODUCT_BUNDLE_IDENTIFIER\s*=.*$",
            "PRODUCT_BUNDLE_IDENTIFIER = %s" % APPLICATION_ID,
            text,
        )
        if re.search(r"(?m)^EXECUTABLE_NAME\s*=", text):
            text = re.sub(
                r"(?m)^EXECUTABLE_NAME\s*=.*$",
                "EXECUTABLE_NAME = %s" % EXECUTABLE_NAME,
                text,
            )
        else:
            text = text.rstrip() + "\nEXECUTABLE_NAME = %s\n" % EXECUTABLE_NAME
        if _write_text(app_info, text):
            changed.append(app_info)
    return changed


def configure_desktop(app_root: Path) -> list[Path]:
    changed = []
    for cmake in (
        app_root / "linux" / "CMakeLists.txt",
        app_root / "windows" / "CMakeLists.txt",
    ):
        if not cmake.exists():
            continue
        text = cmake.read_text(encoding="utf-8")
        text = re.sub(
            r'set\(BINARY_NAME\s+"[^"]+"\)',
            'set(BINARY_NAME "%s")' % EXECUTABLE_NAME,
            text,
        )
        if _write_text(cmake, text):
            changed.append(cmake)

    linux_runner = app_root / "linux" / "runner" / "my_application.cc"
    if linux_runner.exists():
        text = linux_runner.read_text(encoding="utf-8")
        text = re.sub(
            r'g_application_new\("[^"]+"',
            'g_application_new("%s"' % APPLICATION_ID,
            text,
        )
        text = re.sub(
            r'(gtk_(?:window|header_bar)_set_title\([^,]+,\s*)"[^"]+"',
            r'\1"%s"' % PRODUCT_NAME,
            text,
        )
        if _write_text(linux_runner, text):
            changed.append(linux_runner)

    windows_main = app_root / "windows" / "runner" / "main.cpp"
    if windows_main.exists():
        text = windows_main.read_text(encoding="utf-8")
        text = re.sub(
            r'window\.Create\(L"[^"]+"',
            'window.Create(L"%s"' % PRODUCT_NAME,
            text,
        )
        if _write_text(windows_main, text):
            changed.append(windows_main)

    windows_resources = app_root / "windows" / "runner" / "Runner.rc"
    if windows_resources.exists():
        text = windows_resources.read_text(encoding="utf-8")
        replacements = {
            "FileDescription": PRODUCT_NAME,
            "InternalName": EXECUTABLE_NAME,
            "OriginalFilename": "%s.exe" % EXECUTABLE_NAME,
            "ProductName": PRODUCT_NAME,
        }
        for field, value in replacements.items():
            text = re.sub(
                r'(VALUE\s+"%s",\s*)"[^"]*"' % field,
                r'\1"%s"' % value,
                text,
            )
        if _write_text(windows_resources, text):
            changed.append(windows_resources)
    return changed


def configure(app_root: Path, allow_android_cleartext: bool = False) -> list[Path]:
    root = Path(app_root).resolve()
    changed = []
    changed.extend(configure_android(root, allow_android_cleartext))
    changed.extend(configure_apple(root))
    changed.extend(configure_desktop(root))
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_root", nargs="?", default="app")
    parser.add_argument(
        "--allow-android-cleartext",
        action="store_true",
        help="Allow user-configured http:// LAN endpoints in the Android build.",
    )
    args = parser.parse_args(argv)
    for path in configure(Path(args.app_root), args.allow_android_cleartext):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
