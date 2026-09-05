import pytest

from sonder_runtime.bootstrap.app import build_application
from sonder_runtime.bootstrap.child_storage import HostChildRepositoryFactory
from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationStorageFailure,
)
from sonder_runtime.platform.config import SonderConfig, ConfigError
from sonder_runtime.application.subagents.child_migration_activation import (
    issue_host_guard,
)
from sonder_runtime.application.subagents.child_migration import (
    MigrationUnsupported,
    MigrationRefused,
)
from sonder_runtime.bootstrap.child_migration_host import DisposableChildMigrationHost


def test_application_owns_exact_factory_repository_without_closing_unrelated_app(
    tmp_path,
):
    owned = SQLiteDurableContinuationRepository(tmp_path / "owned.db")
    unrelated = SQLiteDurableContinuationRepository(tmp_path / "unrelated.db")
    first = build_application(
        config=SonderConfig(),
        child_repository_factory=HostChildRepositoryFactory("sqlite", lambda: owned),
    )
    second = build_application(
        config=SonderConfig(),
        child_repository_factory=HostChildRepositoryFactory(
            "sqlite", lambda: unrelated
        ),
    )
    try:
        first.delegation_service()
        second.delegation_service()
        first.close_delegation(timeout=1)
        with pytest.raises(ContinuationStorageFailure):
            owned.get("missing")
        assert unrelated.get("missing") is None
    finally:
        first.close_providers(timeout=2)
        second.close_providers(timeout=2)


def test_factory_backend_mismatch_has_no_creation_effect(tmp_path):
    created = []
    application = build_application(
        config=SonderConfig(),
        child_repository_factory=HostChildRepositoryFactory(
            "postgresql", lambda: created.append(True)
        ),
    )
    try:
        with pytest.raises(ConfigError, match="conflicts"):
            application.delegation_service()
        assert created == []
    finally:
        application.close_providers(timeout=2)


def test_unregistered_issuer_and_unverified_host_cannot_mint_capability(tmp_path):
    with pytest.raises(MigrationUnsupported):
        issue_host_guard(object(), {})
    host = DisposableChildMigrationHost(tmp_path / "host", writable_roots=lambda: ())
    try:
        with pytest.raises(MigrationRefused, match="exact cutover"):
            issue_host_guard(host, {})
    finally:
        host.close()
    with pytest.raises(MigrationUnsupported):
        issue_host_guard(host, {})
