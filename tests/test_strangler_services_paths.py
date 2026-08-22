from __future__ import annotations

from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter as LegacyUnitOfWork
from sonder_runtime.platform import paths


def test_unit_of_work_uses_packaged_memory_path_boundary(monkeypatch, tmp_path):
    database = tmp_path / "canonical-memory.db"

    def canonical_memory_db_path() -> str:
        return str(database)

    monkeypatch.setattr(paths, "memory_db_path", canonical_memory_db_path)

    with LegacyUnitOfWork() as unit_of_work:
        assert unit_of_work._conn is not None
        assert database.exists()
