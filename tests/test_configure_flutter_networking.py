import pytest

from scripts.configure_flutter_networking import configure


def test_configures_generated_android_and_apple_projects(tmp_path):
    manifest = tmp_path / "android/app/src/main/AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <application android:label="old_name" />\n</manifest>\n',
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
    assert 'android:label="Sonder Runtime"' in manifest.read_text(encoding="utf-8")
    assert "NSLocalNetworkUsageDescription" in plist.read_text(encoding="utf-8")
    assert "Sonder Runtime" in plist.read_text(encoding="utf-8")


def test_configures_exact_native_identity_and_desktop_executable(tmp_path):
    manifest = tmp_path / "android/app/src/main/AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '<manifest><application android:label="sonder_runtime" /></manifest>',
        encoding="utf-8",
    )
    gradle = tmp_path / "android/app/build.gradle.kts"
    gradle.parent.mkdir(parents=True, exist_ok=True)
    gradle.write_text(
        'android {\n  namespace = "com.example.sonder_runtime"\n'
        '  defaultConfig { applicationId = "com.example.sonder_runtime" }\n}\n',
        encoding="utf-8",
    )
    activity = (
        tmp_path
        / "android/app/src/main/kotlin/com/example/sonder_runtime/MainActivity.kt"
    )
    activity.parent.mkdir(parents=True)
    activity.write_text(
        "package com.example.sonder_runtime\n\nclass MainActivity\n",
        encoding="utf-8",
    )
    linux_cmake = tmp_path / "linux/CMakeLists.txt"
    linux_cmake.parent.mkdir(parents=True)
    linux_cmake.write_text('set(BINARY_NAME "sonder_runtime")\n', encoding="utf-8")
    windows_cmake = tmp_path / "windows/CMakeLists.txt"
    windows_cmake.parent.mkdir(parents=True)
    windows_cmake.write_text(
        'set(BINARY_NAME "sonder_runtime")\n', encoding="utf-8"
    )
    windows_main = tmp_path / "windows/runner/main.cpp"
    windows_main.parent.mkdir(parents=True)
    windows_main.write_text(
        'window.Create(L"sonder_runtime", origin, size);\n', encoding="utf-8"
    )
    windows_resources = tmp_path / "windows/runner/Runner.rc"
    windows_resources.write_text(
        'VALUE "FileDescription", "sonder_runtime"\n'
        'VALUE "InternalName", "sonder_runtime"\n'
        'VALUE "OriginalFilename", "sonder_runtime.exe"\n'
        'VALUE "ProductName", "sonder_runtime"\n',
        encoding="utf-8",
    )
    linux_runner = tmp_path / "linux/runner/my_application.cc"
    linux_runner.parent.mkdir(parents=True)
    linux_runner.write_text(
        'g_application_new("com.example.sonder_runtime", flags);\n'
        'gtk_window_set_title(window, "sonder_runtime");\n',
        encoding="utf-8",
    )
    apple_project = tmp_path / "macos/Runner.xcodeproj/project.pbxproj"
    apple_project.parent.mkdir(parents=True)
    apple_project.write_text(
        "PRODUCT_BUNDLE_IDENTIFIER = com.example.sonderRuntime;\n"
        "PRODUCT_BUNDLE_IDENTIFIER = com.example.sonderRuntime.RunnerTests;\n",
        encoding="utf-8",
    )
    app_info = tmp_path / "macos/Runner/Configs/AppInfo.xcconfig"
    app_info.parent.mkdir(parents=True)
    app_info.write_text(
        "PRODUCT_NAME = sonder_runtime\n"
        "PRODUCT_BUNDLE_IDENTIFIER = com.example.sonderRuntime\n",
        encoding="utf-8",
    )

    configure(tmp_path)

    assert 'namespace = "com.sonder.runtime"' in gradle.read_text(encoding="utf-8")
    assert 'applicationId = "com.sonder.runtime"' in gradle.read_text(
        encoding="utf-8"
    )
    assert activity.read_text(encoding="utf-8").startswith(
        "package com.sonder.runtime\n"
    )
    assert 'set(BINARY_NAME "sonder")' in linux_cmake.read_text(encoding="utf-8")
    assert 'set(BINARY_NAME "sonder")' in windows_cmake.read_text(
        encoding="utf-8"
    )
    assert 'window.Create(L"Sonder Runtime"' in windows_main.read_text(
        encoding="utf-8"
    )
    resource_text = windows_resources.read_text(encoding="utf-8")
    assert 'VALUE "FileDescription", "Sonder Runtime"' in resource_text
    assert 'VALUE "OriginalFilename", "sonder.exe"' in resource_text
    linux_text = linux_runner.read_text(encoding="utf-8")
    assert 'g_application_new("com.sonder.runtime"' in linux_text
    assert 'gtk_window_set_title(window, "Sonder Runtime")' in linux_text
    project_text = apple_project.read_text(encoding="utf-8")
    assert "PRODUCT_BUNDLE_IDENTIFIER = com.sonder.runtime;" in project_text
    assert (
        "PRODUCT_BUNDLE_IDENTIFIER = com.sonder.runtime.RunnerTests;"
        in project_text
    )
    assert "PRODUCT_NAME = Sonder Runtime" in app_info.read_text(encoding="utf-8")
    assert "PRODUCT_BUNDLE_IDENTIFIER = com.sonder.runtime" in app_info.read_text(
        encoding="utf-8"
    )
    assert "EXECUTABLE_NAME = sonder" in app_info.read_text(encoding="utf-8")


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
