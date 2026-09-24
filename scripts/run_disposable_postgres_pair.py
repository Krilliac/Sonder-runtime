"""Run the opt-in child conformance tests on one owned PostgreSQL 18 pair.

The repository tests still consume only a strict private binding, never an
ambient DSN. This harness creates that binding and starts/stops its own
primary and standby outside the checkout. No installed runtime is touched.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STANDBY_NAME = "lab_standby"


def _run(*argv: str | Path, timeout: int = 30, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        tuple(map(str, argv)), capture_output=True, text=True, timeout=timeout,
        env=env, check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{Path(argv[0]).name} failed ({completed.returncode}): "
            f"{completed.stderr[-2000:]}"
        )


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _private_write(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(content)


class DisposablePair:
    def __init__(self, pg_bin: Path, home: Path) -> None:
        self.pg_bin, self.home = pg_bin, home
        self.primary, self.standby = home / "primary", home / "standby"
        self.sockets, self.private = home / "socket", home / "private"
        self.primary_port, self.standby_port = _port(), _port()
        if self.primary_port == self.standby_port:
            self.standby_port = _port()
        self.application_password = secrets.token_urlsafe(32)
        self.replication_password = secrets.token_urlsafe(32)
        self.primary_live = self.standby_live = False

    def binary(self, name: str) -> Path:
        path = self.pg_bin / name
        if not path.is_file():
            raise RuntimeError(f"PostgreSQL 18 binary missing: {name}")
        return path

    def _pg_ctl(self, data: Path, *args: str, timeout: int = 30) -> None:
        _run(self.binary("pg_ctl"), "-D", data, *args, timeout=timeout)

    def _running(self, data: Path) -> bool:
        if not (data / "PG_VERSION").is_file():
            return False
        status = subprocess.run(
            [str(self.binary("pg_ctl")), "-D", str(data), "status"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if status.returncode not in (0, 3):
            raise RuntimeError("disposable PostgreSQL process state is unknown")
        return status.returncode == 0

    def start(self) -> Path:
        import psycopg
        from psycopg import sql

        from sonder_runtime.application.compute_fabric.artifact_spool import (
            PrivateDirectoryAnchor,
        )

        version = subprocess.run(
            [str(self.binary("postgres")), "--version"],
            capture_output=True, text=True, check=True,
        ).stdout
        match = re.search(r"PostgreSQL\s+(\d+)\.(\d+)", version)
        if match is None or int(match[1]) != 18 or int(match[2]) < 6:
            raise RuntimeError("disposable pair requires reviewed PostgreSQL 18.6 or newer")
        self.sockets.mkdir(mode=0o700)
        with PrivateDirectoryAnchor.open_base(self.private, require_new=True):
            pass
        _run(
            self.binary("initdb"), "-D", self.primary, "-U", getpass.getuser(),
            "--auth-local=trust", "--auth-host=scram-sha-256", "--no-sync",
        )
        with (self.primary / "postgresql.conf").open("a", encoding="utf-8") as config:
            config.write(
                f"listen_addresses = '127.0.0.1'\nport = {self.primary_port}\n"
                f"unix_socket_directories = '{self.sockets}'\n"
                "wal_level = replica\nmax_wal_senders = 4\n"
                "synchronous_standby_names = 'FIRST 1 (lab_standby)'\n"
                "synchronous_commit = local\n"
            )
        self._pg_ctl(self.primary, "-l", str(self.home / "primary.log"), "-w", "-t", "15", "start")
        self.primary_live = True

        with psycopg.connect(
            host=str(self.sockets), port=self.primary_port, dbname="postgres",
            user=getpass.getuser(), autocommit=True,
        ) as admin:
            admin.execute(sql.SQL("CREATE ROLE fixture LOGIN SUPERUSER PASSWORD {}").format(
                sql.Literal(self.application_password)
            ))
            admin.execute(sql.SQL("CREATE ROLE replicator LOGIN REPLICATION PASSWORD {}").format(
                sql.Literal(self.replication_password)
            ))
        passfile = self.private / "passfile"
        _private_write(
            passfile,
            f"127.0.0.1:{self.primary_port}:postgres:fixture:{self.application_password}\n"
            f"127.0.0.1:{self.primary_port}:replication:replicator:{self.replication_password}\n",
        )
        binding = self.private / "binding.json"
        _private_write(binding, json.dumps({
            "host": "127.0.0.1", "port": self.primary_port, "database": "postgres",
            "user": "fixture", "passfile": str(passfile), "sslmode": "disable",
        }))

        replication_env = dict(os.environ, PGPASSFILE=str(passfile))
        _run(
            self.binary("pg_basebackup"), "-D", self.standby, "-X", "stream",
            "-h", "127.0.0.1", "-p", str(self.primary_port), "-U", "replicator",
            "--no-password", timeout=60, env=replication_env,
        )
        (self.standby / "standby.signal").touch()
        with (self.standby / "postgresql.conf").open("a", encoding="utf-8") as config:
            config.write(
                f"port = {self.standby_port}\n"
                "synchronous_standby_names = ''\n"
                f"primary_conninfo = 'host=127.0.0.1 port={self.primary_port} "
                f"user=replicator passfile={passfile} "
                f"application_name={STANDBY_NAME} sslmode=disable'\n"
            )
        self.start_standby()
        return binding

    def start_standby(self) -> None:
        import psycopg

        self._pg_ctl(self.standby, "-l", str(self.home / "standby.log"), "-w", "-t", "15", "start")
        self.standby_live = True
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with psycopg.connect(
                host=str(self.sockets), port=self.primary_port, dbname="postgres",
                user=getpass.getuser(), autocommit=True,
            ) as admin:
                status = admin.execute(
                    "SELECT state,sync_state FROM pg_stat_replication "
                    "WHERE application_name=%s", (STANDBY_NAME,),
                ).fetchone()
            if status == ("streaming", "sync"):
                return
            time.sleep(.1)
        raise RuntimeError("disposable standby never became the synchronous streaming peer")

    def stop_standby(self) -> None:
        if self._running(self.standby):
            self._pg_ctl(self.standby, "-m", "immediate", "-w", "-t", "10", "stop")
        self.standby_live = self._running(self.standby)
        if self.standby_live:
            raise RuntimeError("disposable standby did not stop")

    def close(self) -> None:
        try:
            self.stop_standby()
        finally:
            if self._running(self.primary):
                self._pg_ctl(self.primary, "-m", "immediate", "-w", "-t", "10", "stop")
            self.primary_live = self._running(self.primary)
            if self.primary_live:
                raise RuntimeError("disposable primary did not stop")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, required=True)
    arguments = parser.parse_args()
    if os.name != "posix" or os.geteuid() == 0:
        parser.error("the disposable PostgreSQL pair needs an unprivileged POSIX owner")
    if any(key.upper().startswith("PG") for key in os.environ):
        parser.error("ambient PG* settings are not accepted by the private binding")
    if not (REPO / "tests/test_postgres_child_storage_integration.py").is_file():
        parser.error("repository conformance tests are unavailable")

    home = Path(tempfile.mkdtemp(prefix="sonder-owned-pg-pair-"))
    pair = DisposablePair(arguments.pg_bin.resolve(), home)
    try:
        binding = pair.start()
        import pytest

        from tests import test_postgres_child_storage_integration as integration

        integration.PAIR_CONTROL = (pair.stop_standby, pair.start_standby)
        os.environ["SONDER_TEST_CHILD_PG_BINDING"] = str(binding)
        os.environ["SONDER_TEST_DISPOSABLE_PG"] = "1"
        return int(pytest.main(["-q", str(REPO / "tests/test_postgres_child_storage_integration.py")]))
    finally:
        os.environ.pop("SONDER_TEST_CHILD_PG_BINDING", None)
        os.environ.pop("SONDER_TEST_DISPOSABLE_PG", None)
        pair.close()
        if not pair._running(pair.primary) and not pair._running(pair.standby):
            shutil.rmtree(home)


if __name__ == "__main__":
    raise SystemExit(main())
