import json

import pytest

from sonder_runtime.adapters.persistence.postgres_binding import PostgresPrivateBinding
from sonder_runtime.application.compute_fabric.artifact_spool import (
    PrivateDirectoryAnchor,
)
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationStorageFailure,
)
from sonder_runtime.platform.child_storage_config import ChildStorageConfig


def bundle(tmp_path, **changes):
    root = tmp_path / "private"
    with PrivateDirectoryAnchor.open_base(root):
        pass
    password = root / "passfile"
    password.write_text(
        "127.0.0.1:5432:postgres:fixture:fixture-only-password\n", encoding="utf-8"
    )
    if __import__("os").name != "nt":
        password.chmod(0o600)
    value = dict(
        host="127.0.0.1",
        port=5432,
        database="postgres",
        user="fixture",
        passfile=str(password),
        sslmode="disable",
    )
    value.update(changes)
    path = root / "binding.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    if __import__("os").name != "nt":
        path.chmod(0o600)
    return path


def test_fixed_scram_binding_disables_ambient_client_cert_auth(tmp_path):
    binding = PostgresPrivateBinding(bundle(tmp_path), writable_roots=lambda: ())
    try:
        kwargs = binding.connection_kwargs(ChildStorageConfig())
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["sslcertmode"] == "disable"
        assert kwargs["require_auth"] == "scram-sha-256"
        assert "password" not in kwargs and "service" not in kwargs
    finally:
        binding.close()


@pytest.mark.parametrize("variable", ["pgservice", "PgPaSsFiLe", "pGoPtIoNs"])
def test_binding_rejects_case_preserved_windows_libpq_aliases(
    tmp_path, monkeypatch, variable
):
    import os

    binding = PostgresPrivateBinding(bundle(tmp_path), writable_roots=lambda: ())
    try:
        # Preserve the spelling as a native Windows environment enumeration may.
        # os._Environ uppercases keys itself, so setenv alone misses this case.
        monkeypatch.setattr(os, "environ", {variable: "fixture-untrusted"})
        with pytest.raises(ContinuationStorageFailure):
            binding.validate()
    finally:
        binding.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "192.0.2.1"},
        {"host": "localhost"},
        {"host": "host-one,host-two"},
        {"port": True},
        {"options": "-c role=other"},
        {"password": "inline-secret-fixture"},
        {"sslkey": "ambient"},
        {"database": "postgres\x00other"},
    ],
)
def test_binding_rejects_unreviewed_authority_or_plaintext_remote(tmp_path, changes):
    with pytest.raises(ContinuationStorageFailure):
        PostgresPrivateBinding(bundle(tmp_path, **changes), writable_roots=lambda: ())


def test_binding_duplicate_fields_fail_before_connection(tmp_path):
    path = bundle(tmp_path)
    text = path.read_text()
    path.write_text(text[:-1] + ',"host":"192.0.2.1"}', encoding="utf-8")
    with pytest.raises(ContinuationStorageFailure):
        PostgresPrivateBinding(path, writable_roots=lambda: ())


def test_binding_revalidates_current_roots_files_and_libpq_environment(
    tmp_path, monkeypatch
):
    path = bundle(tmp_path)
    roots = []
    binding = PostgresPrivateBinding(path, writable_roots=lambda: roots)
    try:
        roots.append(path.parent)
        with pytest.raises(ContinuationStorageFailure):
            binding.validate()
        roots.clear()
        monkeypatch.setenv("PGSERVICE", "fixture-untrusted-service")
        with pytest.raises(ContinuationStorageFailure):
            binding.validate()
        monkeypatch.delenv("PGSERVICE")
        path.write_text(path.read_text() + " ", encoding="utf-8")
        with pytest.raises(ContinuationStorageFailure):
            binding.validate()
    finally:
        binding.close()
