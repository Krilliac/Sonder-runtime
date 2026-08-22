"""Preference adapters (SPEC-5 WP11, relocated from legacy)."""
from __future__ import annotations

from sonder_runtime.platform import paths as sonder_paths
from ..application.ports.preferences import (
    ConnectionFactory,
)
from .preference_codec import PreferenceCodecAdapter


def _default_connection():
    import sonder_runtime.adapters.memory_store as memory_store

    return memory_store.connect(
        sonder_paths.memory_db_path(), check_same_thread=True
    )


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


LegacyPreferenceCodec = PreferenceCodecAdapter


class NullPreferenceEventSink:
    def changed(self, operation, count):
        return None
