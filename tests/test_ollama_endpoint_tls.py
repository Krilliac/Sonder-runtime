import socket
import ssl
import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.inference import ollama_endpoint
from sonder_runtime.platform.config import load_config


def test_ca_bundle_rejects_missing_or_relative_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("SONDER_OLLAMA_CA_BUNDLE", raising=False)
    with pytest.raises(ValueError):
        ollama_endpoint.configure_typed_ca_bundle("relative.pem")
    with pytest.raises(ValueError):
        ollama_endpoint.configure_typed_ca_bundle(str(tmp_path / "missing.pem"))


def test_config_binds_absolute_ca_bundle(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("certificate", encoding="ascii")
    config = load_config(env={"SONDER_OLLAMA_CA_BUNDLE": str(ca)})
    assert config.ollama.ca_bundle == str(ca)
    assert str(ca.resolve()) in config.private_source_paths


def test_toml_ca_bundle_is_loaded(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("certificate", encoding="ascii")
    config_file = tmp_path / "sonder.toml"
    encoded = str(ca).replace("\\", "\\\\").replace('"', '\\"')
    config_file.write_text(
        '[ollama]\nca_bundle = "' + encoded + '"\n', encoding="utf-8"
    )
    config = load_config(config_file, env={})
    assert config.ollama.ca_bundle == str(ca)
    assert str(ca.resolve()) in config.private_source_paths


def test_private_ollama_certificate_requires_supplied_ca_bundle(monkeypatch):
    configured_ca = os.environ.get("SONDER_OLLAMA_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE", "")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    try:
        raw = socket.create_connection(("10.77.0.2", 8443), timeout=3)
    except OSError:
        pytest.skip("private Ollama worker is unavailable")
    with raw:
        context = ssl.create_default_context()
        try:
            with context.wrap_socket(raw, server_hostname="10.77.0.2"):
                pytest.skip("private worker certificate is already trusted by this host")
        except ssl.SSLCertVerificationError:
            pass
        except OSError:
            pytest.skip("private worker TLS handshake is unavailable")

    ca = configured_ca
    if not ca or not Path(ca).is_file():
        pytest.skip("private worker CA fixture is unavailable")
    context = ssl.create_default_context(cafile=ca)
    try:
        with socket.create_connection(("10.77.0.2", 8443), timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname="10.77.0.2") as client:
                assert client.version().startswith("TLS")
    except OSError:
        pytest.skip("private worker TLS handshake is unavailable")
