"""Compose exactly one host-configured child aggregate backend."""

import os

from ..platform.child_storage_config import child_storage_errors
from ..platform.config import ConfigError


def compose_child_repository(config):
    errors = child_storage_errors(config)
    if (
        config.child_storage.backend == "postgresql"
        and os.environ.get("SONDER_CHILD_SESSIONS_DB", "").strip()
    ):
        errors.append(
            "PostgreSQL child storage conflicts with SONDER_CHILD_SESSIONS_DB"
        )
    if errors:
        raise ConfigError(errors)
    if config.child_storage.backend == "sqlite":
        from ..platform.paths import state_path
        from ..adapters.persistence.durable_continuation import (
            SQLiteDurableContinuationRepository,
        )

        return SQLiteDurableContinuationRepository(
            state_path("child-sessions.db", "SONDER_CHILD_SESSIONS_DB")
        )
    from ..adapters.persistence.postgres_binding import PostgresPrivateBinding
    from ..adapters.persistence.postgres_continuation import (
        PostgreSQLDurableContinuationRepository,
    )
    from ..adapters.filesystem.file_ops import allowed_roots

    def roots():
        return (
            tuple(allowed_roots())
            + tuple(config.state.workspace_roots)
            + ((config.state.home,) if config.state.home else ())
        )

    binding = PostgresPrivateBinding(
        config.child_storage.binding_file, writable_roots=roots
    )
    try:
        return PostgreSQLDurableContinuationRepository(config.child_storage, binding)
    except Exception:
        binding.close()
        raise
