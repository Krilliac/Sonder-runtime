"""Configure generated Flutter platform projects for Trilobite networking.

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
    "Trilobite connects to and controls the server you configure on your local network."
)


def configure_android(app_root: Path, allow_cleartext: bool = False) -> bool:
    manifest = app_root / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    if not manifest.exists():
        return False
    text = manifest.read_text(encoding="utf-8")
    value = "true" if allow_cleartext else "false"
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
    manifest.write_text(text, encoding="utf-8")
    return True


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
        if "NSLocalNetworkUsageDescription" in payload:
            continue
        payload["NSLocalNetworkUsageDescription"] = LOCAL_NETWORK_DESCRIPTION
        plist.write_bytes(plistlib.dumps(payload, sort_keys=False))
        changed.append(plist)
    return changed


def configure(app_root: Path, allow_android_cleartext: bool = False) -> list[Path]:
    root = Path(app_root).resolve()
    changed = []
    if configure_android(root, allow_android_cleartext):
        changed.append(root / "android" / "app" / "src" / "main" / "AndroidManifest.xml")
    changed.extend(configure_apple(root))
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
