"""Diagnostic-only immediate report identities; never changes test assertions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import time

import pytest

_ROOT = Path(os.environ["TLS_CI_DIAGNOSTIC_DIR"])
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "controller")
_START = time.monotonic()
_COMPLETED: set[str] = set()
_FAILED: set[str] = set()
_STARTED: set[str] = set()
_WORKERS: set[str] = set()
_CAP = 5000


def _event(kind: str, **data: object) -> None:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    record = {
        "kind": kind,
        "worker": _WORKER,
        "pid": os.getpid(),
        "elapsed": round(time.monotonic() - _START, 6),
        "cpu_user": round(usage.ru_utime, 6),
        "cpu_system": round(usage.ru_stime, 6),
        **data,
    }
    _ROOT.mkdir(parents=True, exist_ok=True)
    with (_ROOT / f"events-{_WORKER}.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, sort_keys=True) + "\n")
        output.flush()
    if kind in {"failure", "collection_failure", "cap", "session_start", "session_finish", "worker_collection"}:
        print("TLS_CI_DIAGNOSTIC " + json.dumps(record, sort_keys=True), flush=True)


def pytest_sessionstart(session) -> None:
    _event(
        "session_start",
        xdist_numprocesses=session.config.getoption("numprocesses", default=None),
        diagnostic_only=True,
    )


def pytest_collection_finish(session) -> None:
    _event("collection_finish", collected=len(session.items))


def pytest_xdist_node_collection_finished(node, ids) -> None:
    """Persist the actual worker collection, not preceding JUnit arrival order."""
    _ROOT.mkdir(parents=True, exist_ok=True)
    worker = node.gateway.id
    _WORKERS.add(worker)
    with (_ROOT / f"collection-{worker}.json").open("w", encoding="utf-8") as output:
        json.dump(list(ids), output)
        output.write("\n")
    _event("worker_collection", target_worker=worker, collected=len(ids))


def pytest_runtest_logstart(nodeid, location) -> None:
    del location
    if _WORKER == "controller":
        _STARTED.add(nodeid)
        _event("test_start", nodeid=nodeid)


def pytest_collectreport(report) -> None:
    if report.failed:
        _event("collection_failure", nodeid=report.nodeid, outcome=report.outcome)


def pytest_runtest_logreport(report) -> None:
    if _WORKER != "controller":
        return
    node = getattr(report, "node", None)
    worker = getattr(getattr(node, "gateway", None), "id", None)
    _event(
        "report",
        target_worker=worker,
        nodeid=report.nodeid,
        phase=report.when,
        outcome=report.outcome,
        duration=round(report.duration, 6),
    )
    if report.failed:
        _FAILED.add(report.nodeid)
        _event(
            "failure",
            target_worker=worker,
            nodeid=report.nodeid,
            phase=report.when,
            outcome=report.outcome,
            named_failures=len(_FAILED),
        )
    if report.when == "teardown":
        _COMPLETED.add(report.nodeid)
    if len(_COMPLETED) >= _CAP:
        _event("cap", completed=len(_COMPLETED), limit=_CAP, named_failures=len(_FAILED))
        pytest.exit("DIAGNOSTIC_CAP: 5000 completed cases; this is not a gate", returncode=4)


def pytest_sessionfinish(session, exitstatus) -> None:
    del session
    _event(
        "session_finish",
        exitstatus=int(exitstatus),
        completed=len(_COMPLETED),
        started=len(_STARTED),
        unfinished_started=sorted(_STARTED - _COMPLETED),
        observed_workers=sorted(_WORKERS),
        named_failures=sorted(_FAILED),
        diagnostic_only=True,
    )
