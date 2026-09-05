"""Private disposable-host worker cleanup; never a request-supplied callback."""
from ..adapters.embedding_cache import close_current_thread as close_embeddings
from ..adapters.persistence.composition_store import close_current_thread as close_composition
from ..adapters.persistence.sqlite_factory import close_current_thread as close_cached
from ..adapters.persistence.owned_sqlite import OwnedSQLiteConnections
from ..application.ports.runtime_threads import RuntimeWorkerFactories, install_disposable_factories
from ..platform.runtime_threads import OwnedRuntimeThreads, install_disposable_owner


def install_disposable_thread_owner(owner):
    """One startup-only composition, before runtime stores or worker use."""
    if type(owner) is not OwnedRuntimeThreads:
        raise TypeError("exact disposable thread owner required")
    try:
        install_disposable_owner(owner)
        install_disposable_factories(RuntimeWorkerFactories(owner.thread, owner.pool))
    except BaseException:
        owner.stop_admissions()
        raise


class SQLiteThreadCleanup:
    def __init__(self, owner):
        if type(owner) is not OwnedSQLiteConnections:
            raise TypeError("exact disposable SQLite owner required")
        self._owner = owner

    def __call__(self):
        successful = True
        for close in (close_embeddings, close_composition, close_cached):
            try:
                close()
            except BaseException:
                successful = False
        self._owner.close_current_thread()
        return successful and self._owner.current_thread_closed()
