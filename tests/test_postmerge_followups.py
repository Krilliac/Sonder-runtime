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


def test_existing_state_home_stores_are_tightened_once(tmp_path):
    import os
    import stat
    import pytest
    from sonder_runtime.platform import private_files
    if not private_files.supported():
        pytest.skip("POSIX modes only")
    home = tmp_path / "home"
    home.mkdir()
    stores = [home / "child-sessions.db", home / "child-sessions.db-wal", home / "audit.jsonl"]
    other = home / "notes.txt"
    for path in stores + [other]:
        path.write_text("x")
        os.chmod(path, 0o644)
    link = home / "linked.db"
    link.symlink_to(other)
    assert private_files.tighten_existing_stores(home) == len(stores)
    for path in stores:
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    # Unrelated files and symlink targets are left alone.
    assert stat.S_IMODE(os.lstat(other).st_mode) == 0o644
    # Second call in the same process is a no-op.
    os.chmod(stores[0], 0o644)
    assert private_files.tighten_existing_stores(home) == 0
