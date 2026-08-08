from pathlib import Path


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
