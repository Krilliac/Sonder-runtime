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
        # Host stores may contain different certificates with the same subject.
        # Establish an untrusted baseline without loading any ambient trust.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            with context.wrap_socket(raw, server_hostname="10.77.0.2"):
                pytest.fail("private worker certificate was trusted without a supplied CA bundle")
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
    except ssl.SSLCertVerificationError:
        raise
    except OSError:
        pytest.skip("private worker TLS handshake is unavailable")


@pytest.fixture
def private_worker_tls(tmp_path, monkeypatch):
    """Exercise the live-worker probe through real TLS on local socket pairs."""
    import ipaddress
    import threading
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.x509.oid import NameOID

    def certificate(name, address):
        key = Ed25519PrivateKey.generate()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "private-worker")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]),
                critical=False,
            )
            .sign(key, algorithm=None)
        )
        cert_path = tmp_path / (name + ".pem")
        key_path = tmp_path / (name + ".key")
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        return cert_path, key_path

    certificates = {
        "matching": certificate("matching", "10.77.0.2"),
        "other": certificate("other", "10.77.0.2"),
        "wrong_hostname": certificate("wrong-hostname", "10.77.0.3"),
    }
    original_context = ssl.create_default_context
    original_socketpair = socket.socketpair
    ssl_error = ssl.SSLError
    connections = []
    servers = []
    server_errors = []

    def configure(server, bundle):
        cert_path, key_path = certificates[server]
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert_path), str(key_path))
        monkeypatch.setenv("SONDER_OLLAMA_CA_BUNDLE", str(certificates[bundle][0]))

        def ambient_context(*args, **kwargs):
            # Model a host that already trusts this private worker. Explicit
            # bundles still use the real verifying context and their own file.
            if not any(kwargs.get(key) for key in ("cafile", "capath", "cadata")):
                kwargs = {**kwargs, "cafile": str(cert_path)}
            return original_context(*args, **kwargs)

        def connect(address, timeout):
            assert address == ("10.77.0.2", 8443)
            client, raw = original_socketpair()
            client.settimeout(timeout)
            raw.settimeout(timeout)
            connections.append((client, raw))

            def handshake():
                with raw:
                    try:
                        with server_context.wrap_socket(raw, server_side=True):
                            pass
                    except (ssl_error, ConnectionAbortedError, ConnectionResetError):
                        pass  # Rejected TLS or immediate peer close, including Windows resets.
                    except OSError as error:
                        server_errors.append(error)

            thread = threading.Thread(target=handshake)
            servers.append(thread)
            thread.start()
            return client

        # Restrict the doubles to this module's probe; background runtime
        # callers retain the real socket and SSL modules.
        monkeypatch.setitem(globals(), "ssl", SimpleNamespace(
            SSLContext=ssl.SSLContext,
            PROTOCOL_TLS_CLIENT=ssl.PROTOCOL_TLS_CLIENT,
            TLSVersion=ssl.TLSVersion,
            SSLCertVerificationError=ssl.SSLCertVerificationError,
            create_default_context=ambient_context,
        ))
        monkeypatch.setitem(globals(), "socket", SimpleNamespace(create_connection=connect))

    yield configure, connections
    for thread in servers:
        thread.join(4)
    for client, raw in connections:
        client.close()
        raw.close()
    assert not any(thread.is_alive() for thread in servers)
    assert server_errors == []


@pytest.mark.parametrize("case", ["matching", "wrong_bundle", "wrong_hostname"])
def test_private_worker_probe_uses_only_supplied_trust(private_worker_tls, monkeypatch, case):
    configure, connections = private_worker_tls
    server = "wrong_hostname" if case == "wrong_hostname" else "matching"
    bundle = "other" if case == "wrong_bundle" else server
    configure(server, bundle)
    try:
        if case == "matching":
            test_private_ollama_certificate_requires_supplied_ca_bundle(monkeypatch)
        else:
            with pytest.raises(ssl.SSLCertVerificationError) as rejected:
                test_private_ollama_certificate_requires_supplied_ca_bundle(monkeypatch)
            if case == "wrong_hostname":
                assert "IP address mismatch" in rejected.value.verify_message
    except pytest.skip.Exception as error:
        pytest.fail("Local TLS verification was skipped: " + str(error))
    assert len(connections) == 2  # Untrusted baseline, then the supplied bundle.
