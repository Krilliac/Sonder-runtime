"""The native PostgreSQL pair must recognize the installed binary's real version line."""

import pytest

from scripts.run_disposable_postgres_pair import _reviewed_postgres_version


@pytest.mark.parametrize("version", [
    "postgres (PostgreSQL) 18.6\n",
    "postgres (PostgreSQL) 18.7 (Ubuntu 18.7-1.pgdg24.04+1)\n",
])
def test_disposable_pair_accepts_reviewed_native_postgres(version):
    assert _reviewed_postgres_version(version)


@pytest.mark.parametrize("version", [
    "postgres (PostgreSQL) 18.5\n",
    "postgres (PostgreSQL) 17.20\n",
    "PostgreSQL 18.6\n",
    "postgres (PostgreSQL) 18.6\nunsafe extra line",
])
def test_disposable_pair_rejects_unreviewed_or_malformed_versions(version):
    assert not _reviewed_postgres_version(version)
