import pytest

from scripts.configure_flutter_networking import configure


def test_configures_generated_android_and_apple_projects(tmp_path):
    manifest = tmp_path / "android/app/src/main/AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <application android:label="trilobite" />\n</manifest>\n',
        encoding="utf-8",
    )
    plist = tmp_path / "ios/Runner/Info.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict></dict></plist>\n',
        encoding="utf-8",
    )

    changed = configure(tmp_path, allow_android_cleartext=True)

    assert manifest in changed
    assert 'android:usesCleartextTraffic="true"' in manifest.read_text(encoding="utf-8")
    assert "NSLocalNetworkUsageDescription" in plist.read_text(encoding="utf-8")


def test_network_configuration_is_repeatable(tmp_path):
    manifest = tmp_path / "android/app/src/main/AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <application android:usesCleartextTraffic="true" />\n</manifest>\n',
        encoding="utf-8",
    )
    configure(tmp_path, allow_android_cleartext=False)
    configure(tmp_path, allow_android_cleartext=False)
    text = manifest.read_text(encoding="utf-8")
    assert text.count("android:usesCleartextTraffic") == 1
    assert 'android:usesCleartextTraffic="false"' in text


def test_malformed_generated_project_fails_closed(tmp_path):
    manifest = tmp_path / "android/app/src/main/AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("<manifest />", encoding="utf-8")
    with pytest.raises(ValueError, match="application"):
        configure(tmp_path)
