"""Legacy storage and text adapters for typed preference use cases."""
from __future__ import annotations

import importlib

from ...application.ports.preferences import (
    ConnectionFactory,
    PreferenceModuleProvider,
)


def _default_connection():
    import sonder_runtime.adapters.memory_store as memory_store
    import sonder_paths

    return memory_store.connect(
        sonder_paths.memory_db_path(), check_same_thread=True
    )


def _default_preference_module():
    return importlib.import_module("preference_learning")


class LegacyPreferenceRepository:
    def __init__(self, connection_factory: ConnectionFactory | None = None) -> None:
        self._connection_factory = connection_factory or _default_connection

    def upsert_and_list(
        self, *, scope, key, text, confidence, limit
    ):
        import sonder_runtime.adapters.memory_store as memory_store

        connection = self._connection_factory()
        try:
            memory_store.upsert_preference(
                connection,
                memory_store.new_id(),
                scope,
                key,
                text,
                confidence=confidence,
            )
            return memory_store.preferences_for_scope(
                connection, scope, limit=limit
            )
        finally:
            connection.close()

    def list(self, *, limit, include_disabled):
        import sonder_runtime.adapters.memory_store as memory_store

        connection = self._connection_factory()
        try:
            return memory_store.all_preferences(
                connection,
                limit=limit,
                include_disabled=include_disabled,
            )
        finally:
            connection.close()

    def set_enabled(self, target, *, enabled, scope):
        import sonder_runtime.adapters.memory_store as memory_store

        connection = self._connection_factory()
        try:
            return memory_store.set_preference_enabled(
                connection, target, enabled, scope=scope
            )
        finally:
            connection.close()


class LegacyPreferenceCodec:
    def __init__(
        self, module_provider: PreferenceModuleProvider | None = None
    ) -> None:
        self._module_provider = module_provider or _default_preference_module

    def extract(self, text):
        return self._module_provider().extract_preferences(text)

    def normalize(self, text):
        return self._module_provider().normalize_preference(text)

    def key(self, text):
        return self._module_provider().preference_key(text)

    def is_stable(self, text, source_text=None):
        return self._module_provider().is_stable_preference(text, source_text)

    def format(self, rows):
        return self._module_provider().format_preferences(rows)


class NullPreferenceEventSink:
    """Preserve the legacy surface, which emitted no preference activity."""

    def changed(self, operation, count):
        return None
