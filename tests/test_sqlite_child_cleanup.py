from threading import Event, Thread

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationStorageFailure,
)


def test_shutdown_retains_live_owned_connection_and_permanently_denies_admission(
    tmp_path,
):
    repository = SQLiteDurableContinuationRepository(tmp_path / "owned.db")
    opened, release = Event(), Event()

    def transaction():
        with repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            opened.set()
            assert release.wait(5)

    thread = Thread(target=transaction)
    thread.start()
    assert opened.wait(2)
    try:
        repository.stop_admissions()
        assert not repository.close(runners_stopped=True, timeout=0.01)
        with pytest.raises(ContinuationStorageFailure, match="closed"):
            repository.get("absent")
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert repository.close(runners_stopped=True, timeout=1)
    with pytest.raises(ContinuationStorageFailure, match="closed"):
        repository.get("absent")


def test_failed_connection_creation_releases_owned_slot(tmp_path, monkeypatch):
    repository = SQLiteDurableContinuationRepository(tmp_path / "owned.db")
    import sonder_runtime.adapters.persistence.durable_continuation as module

    def fail(*args, **kwargs):
        raise OSError("fixture connection refusal")

    monkeypatch.setattr(module.sqlite3, "connect", fail)
    with pytest.raises(OSError):
        repository.get("absent")
    assert repository.close(runners_stopped=True, timeout=0)
