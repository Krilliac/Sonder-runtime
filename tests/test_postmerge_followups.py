"""Follow-ups from the post-merge reviews: scanner secret coverage and mapped proxy peers."""
import ipaddress
from pathlib import Path

from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.interfaces.http.host_policy import forwarded_client_ip


def test_scanner_secret_check_covers_every_direct_read_credential_file():
    for name in sorted(file_ops.CREDENTIAL_READ_FILES):
        assert file_ops._is_secret_path(Path("proj") / name), name
    for name in (".env", ".env.local", ".pgpass", ".git-credentials", "_netrc"):
        assert file_ops._is_secret_path(Path("proj") / name), name


def test_scanner_directory_exclusions_cover_every_credential_directory():
    assert file_ops.CREDENTIAL_READ_DIRECTORIES <= file_ops.SENSITIVE_READ_DIRECTORIES


def test_ipv4_mapped_proxy_peer_matches_ipv4_trusted_network():
    networks = [ipaddress.ip_network("127.0.0.1/32")]
    resolved = forwarded_client_ip(
        "::ffff:127.0.0.1", "203.0.113.9", proxy_declared=True,
        trusted_networks=networks,
    )
    assert resolved == "203.0.113.9"


def test_untrusted_mapped_peer_still_cannot_choose_its_address():
    networks = [ipaddress.ip_network("127.0.0.1/32")]
    resolved = forwarded_client_ip(
        "::ffff:198.51.100.7", "127.0.0.1", proxy_declared=True,
        trusted_networks=networks,
    )
    assert resolved == "::ffff:198.51.100.7"
