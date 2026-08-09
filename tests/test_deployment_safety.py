import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _text(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_legacy_serve_installer_is_loopback_only_and_keeps_key_out_of_unit():
    deploy = _text("deploy_sonder.sh")

    assert "Environment=SONDER_HOST=127.0.0.1" in deploy
    assert "Environment=SONDER_HOST=0.0.0.0" not in deploy
    assert "EnvironmentFile=/etc/sonder/sonder-local.env" in deploy
    assert "Environment=SONDER_API_KEY=" not in deploy
    assert "SERVER_IP=" not in deploy
    assert "Public URL:" not in deploy


def test_production_installer_uses_manifest_copy_and_validates_release_tag():
    installer = _text("packaging/install_sonder.sh")

    assert "copy_verified_payload" in installer
    assert "PACKAGE-MANIFEST.json" in installer
    assert "rsync " not in installer
    assert "rm -rf" not in installer
    assert '*[!A-Za-z0-9._-]*' in installer
    assert '${#VERSION_TAG}' in installer


def test_public_remote_examples_require_https_and_never_promote_direct_bind():
    readme = _text("README.md")
    client = _text("CLIENT.md")
    security = _text("SECURITY.md")

    combined = "\n".join((readme, client, security))
    assert "http://your-vps" not in combined
    assert "SONDER_HOST=0.0.0.0" not in readme
    assert "SONDER_HOST=0.0.0.0" not in client
    assert "https://sonder.example.com" in client
    assert "secure-remote-access.md" in combined


def test_app_remote_host_hint_requires_https():
    settings_screen = _text("app/lib/settings_screen.dart")

    assert "hintText: 'https://your-host.example'" in settings_screen
    assert "hintText: 'http://your-host" not in settings_screen
    assert "HTTPS is required off-device" in settings_screen

    api = _text("app/lib/api.dart")
    assert "e.g. https://sonder.example.com" in api
    assert "http://192.168.1.10:11435" not in api

    mobile = _text("MOBILE_HOST_CONTROL.md")
    assert "CI and tagged\nrelease builds keep Android cleartext disabled" in mobile
    assert "--allow-android-cleartext-for-development" in mobile
    assert "--allow-android-cleartext\n" not in mobile
    assert "http://HOST" not in mobile
    assert "http://192.168" not in mobile


def test_mobile_launcher_keeps_managed_runtime_loopback_and_documents_tls():
    launcher = _text("sonder_launcher.py")
    assert 'DEFAULT_SERVER_HOST = "127.0.0.1"' in launcher
    assert 'server_host=DEFAULT_SERVER_HOST' in launcher
    assert 'os.environ.get("SONDER_HOST", DEFAULT_SERVER_HOST)' in launcher
    assert "managed Sonder API host must be loopback" in launcher
    assert '"--allow-insecure-http-for-development"' in launcher
    assert "non-loopback launcher binding requires TLS" in launcher

    mobile = _text("MOBILE_HOST_CONTROL.md")
    assert "managed Sonder API remains loopback-only" in mobile
    assert "--cert C:\\path\\fullchain.pem --key C:\\path\\privkey.pem" in mobile
    assert "Never open or port-forward" in mobile
    assert "Open TCP ports `11435`" not in mobile
    assert "A non-loopback launcher refuses to start" in mobile

    autostart = _text("sonder-launcher-autostart.cmd")
    assert "if not defined SONDER_LAUNCHER_CERT" in autostart
    assert "if not defined SONDER_LAUNCHER_KEY" in autostart
    assert 'if not exist "%SONDER_LAUNCHER_CERT%"' in autostart
    assert 'if not exist "%SONDER_LAUNCHER_KEY%"' in autostart
    assert "--host 0.0.0.0" in autostart
    assert "--cert " not in autostart
    assert "--key " not in autostart


@pytest.mark.skipif(os.name != "nt", reason="Windows Startup batch integration")
def test_windows_launcher_autostart_fails_closed_and_keeps_secrets_out(tmp_path):
    startup = (
        tmp_path
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    startup.mkdir(parents=True)
    target = startup / "Sonder Launcher.cmd"
    script = ROOT / "sonder-launcher-autostart.cmd"
    environment = os.environ.copy()
    environment["APPDATA"] = str(tmp_path)
    environment["SONDER_LAUNCHER_TOKEN"] = "x" * 24
    environment.pop("SONDER_LAUNCHER_CERT", None)
    environment.pop("SONDER_LAUNCHER_KEY", None)

    missing = subprocess.run(
        ["cmd.exe", "/d", "/c", "call", str(script)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert not target.exists()

    environment["SONDER_LAUNCHER_CERT"] = str(tmp_path / "missing-cert.pem")
    environment["SONDER_LAUNCHER_KEY"] = str(tmp_path / "missing-key.pem")
    absent = subprocess.run(
        ["cmd.exe", "/d", "/c", "call", str(script)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert absent.returncode == 2
    assert not target.exists()

    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("test certificate", encoding="ascii")
    key.write_text("test key", encoding="ascii")
    environment["SONDER_LAUNCHER_CERT"] = str(cert)
    environment["SONDER_LAUNCHER_KEY"] = str(key)
    configured = subprocess.run(
        ["cmd.exe", "/d", "/c", "call", str(script)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert configured.returncode == 0
    startup_command = target.read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in startup_command
    assert environment["SONDER_LAUNCHER_TOKEN"] not in startup_command
    assert str(cert) not in startup_command
    assert str(key) not in startup_command


def test_public_docs_label_prerelease_and_do_not_offer_ungated_tagging():
    readme = _text("README.md")
    app_readme = _text("app/README.md")
    workflow = _text(".github/workflows/build-apps.yml")
    client = _text("CLIENT.md")

    assert "mutable prerelease snapshot" in readme
    assert "Android-prerelease" in readme
    assert "development identity" in app_readme
    assert "release-version-policy.md" in app_readme
    assert "git tag app-v1.0.0" not in app_readme
    assert "ordinary and manual runs do not publish or refresh it" in workflow
    assert "branches: [main]" in workflow
    assert "claude/mobile-desktop-app-gui-gzlhn5" not in workflow
    assert "README.md#quick-start" in client
    assert "README.md#why-sonder" in client
    assert "README.md#install--run" not in client
    assert "README.md#interfaces" not in client


def test_private_install_docs_match_signed_distribution_and_exact_revision():
    install = _text("docs/runbooks/install-server-private.md")
    assert "SPEC-4 signed bundles are" in install
    assert "Until the SPEC-4 signed distribution exists" not in install
    assert "VERSION_TAG=$(git rev-parse --verify HEAD)" in install
