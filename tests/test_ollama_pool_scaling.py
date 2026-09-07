"""Static-roster capacity and bounded-observability contracts for Ollama."""
from __future__ import annotations

import json
import threading

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    configure_typed_workers,
    from_environment,
    parse_worker_origins,
    reset_typed_workers,
)


def _roster(total: int) -> tuple[str, tuple[str, ...]]:
    """Return deterministic loopback origins without opening a socket."""
    assert total >= 1
    return (
        "http://127.0.0.1:11434",
        tuple(
            "http://127.0.0.1:%d" % (12_000 + offset)
            for offset in range(total - 1)
        ),
    )


@pytest.mark.parametrize("maximum", [16, 64, 256])
def test_constructor_uses_its_configured_primary_inclusive_roster_limit(maximum):
    primary, workers = _roster(maximum)

    pool = OllamaWorkerPool(primary, workers, max_workers=maximum)

    assert pool.origins == (primary, *workers)
    assert pool.status()["worker_count"] == maximum
    assert pool.status()["configured_worker_limit"] == maximum
    with pytest.raises(ValueError, match="at most %d Ollama workers" % maximum):
        OllamaWorkerPool(
            primary,
            (*workers, "http://127.0.0.1:20000"),
            max_workers=maximum,
        )


@pytest.mark.parametrize("maximum", [16, 64, 256])
def test_parser_consumes_configured_primary_inclusive_limit(maximum):
    _, workers = _roster(maximum)

    assert parse_worker_origins(
        ",".join(workers), max_workers=maximum,
    ) == workers
    with pytest.raises(ValueError, match="at most %d additional" % (maximum - 1)):
        parse_worker_origins(
            ",".join((*workers, "http://127.0.0.1:20000")),
            max_workers=maximum,
        )


@pytest.mark.parametrize("maximum", [64, 256])
def test_explicit_environment_owns_larger_roster_and_never_reads_process_state(
    monkeypatch, maximum,
):
    primary, workers = _roster(maximum)
    monkeypatch.setenv("SONDER_OLLAMA_POOL_MAX_WORKERS", "1")
    environment = {
        "SONDER_OLLAMA_POOL_MAX_WORKERS": str(maximum),
        "SONDER_OLLAMA_WORKERS": ",".join(workers),
        "SONDER_OLLAMA_WORKER_MAX_INFLIGHT": "3",
        "SONDER_OLLAMA_WORKER_QUEUE_DEPTH": "7",
        "SONDER_OLLAMA_WORKER_PROBE_PARALLELISM": "2",
        "SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE": "5",
        "SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE": "2",
    }

    pool = from_environment(primary, environment)
    status = pool.status()

    assert pool.origins == (primary, *workers)
    assert status["worker_count"] == maximum
    assert status["configured_worker_limit"] == maximum
    assert {worker["capacity"] for worker in status["workers"]} == {3}
    assert status["queue"] == {"waiting": 0, "limit": 7, "scope": "global"}
    assert status["probe_parallelism"] == 2
    assert status["probe_batch_size"] == 5
    assert status["status_page_size"] == 2
    assert len(status["workers"]) == 2

    with pytest.raises(
        ValueError, match="at most %d additional" % (maximum - 1),
    ):
        from_environment(
            primary,
            {
                **environment,
                "SONDER_OLLAMA_WORKERS": ",".join(
                    (*workers, "http://127.0.0.1:20000")
                ),
            },
        )


def test_typed_configuration_controls_static_roster_limits_without_environment(
    monkeypatch,
):
    primary, workers = _roster(64)
    monkeypatch.setenv("SONDER_OLLAMA_POOL_MAX_WORKERS", "1")
    monkeypatch.setenv("SONDER_OLLAMA_WORKER_PROBE_PARALLELISM", "1")
    monkeypatch.setenv("SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE", "1")
    monkeypatch.setenv("SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE", "1")
    try:
        configure_typed_workers(
            workers,
            allow_remote=False,
            max_workers=64,
            capability_probe_parallelism=2,
            capability_probe_batch_size=5,
            status_page_size=2,
        )

        status = from_environment(primary).status()

        assert status["worker_count"] == 64
        assert status["configured_worker_limit"] == 64
        assert status["probe_parallelism"] == 2
        assert status["probe_batch_size"] == 5
        assert status["status_page_size"] == 2
    finally:
        reset_typed_workers()


@pytest.mark.parametrize("maximum", [0, 257])
def test_direct_environment_rejects_out_of_range_static_roster_limits(maximum):
    with pytest.raises(ValueError, match="SONDER_OLLAMA_POOL_MAX_WORKERS"):
        from_environment(
            "http://127.0.0.1:11434",
            {"SONDER_OLLAMA_POOL_MAX_WORKERS": str(maximum)},
        )


def test_canonical_duplicates_are_rejected_at_pool_and_typed_boundaries():
    primary = "http://127.0.0.1:11434"
    with pytest.raises(ValueError, match="duplicates primary"):
        OllamaWorkerPool(primary, ("http://127.0.0.1:11434/",))
    with pytest.raises(ValueError, match="duplicate canonical"):
        OllamaWorkerPool(
            primary,
            ("http://127.0.0.2:11434", "http://127.0.0.2:11434/"),
        )
    with pytest.raises(ValueError, match="duplicate canonical"):
        configure_typed_workers(
            ("http://127.0.0.2:11434", "http://127.0.0.2:11434/"),
            allow_remote=False,
        )

    with pytest.raises(ValueError, match="duplicates primary"):
        from_environment(
            primary,
            {"SONDER_OLLAMA_WORKERS": "http://127.0.0.1:11434/"},
        )


def test_refresh_selects_fair_bounded_stale_batches():
    primary, workers = _roster(7)
    calls: list[str] = []
    pool = OllamaWorkerPool(
        primary,
        workers,
        max_workers=7,
        capability_probe_parallelism=1,
        capability_probe_batch_size=2,
        capability_prober=lambda origin: calls.append(origin) or {"models": ()},
    )

    pool.refresh_capabilities()
    assert calls == [primary, workers[0]]
    pool.refresh_capabilities()
    assert calls == [primary, workers[0], workers[1], workers[2]]
    pool.refresh_capabilities()
    assert calls == [
        primary, workers[0], workers[1], workers[2], workers[3], workers[4],
    ]
    pool.refresh_capabilities()
    assert calls == [primary, *workers]


def test_refresh_never_starts_more_than_configured_parallel_probes():
    primary, workers = _roster(5)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    calls: list[str] = []

    def probe(origin: str):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(origin)
            if len(calls) == 2:
                entered.set()
        assert release.wait(2)
        with lock:
            active -= 1
        return {"models": ()}

    pool = OllamaWorkerPool(
        primary,
        workers,
        max_workers=5,
        capability_probe_parallelism=2,
        capability_probe_batch_size=4,
        capability_prober=probe,
    )
    refresh = threading.Thread(target=pool.refresh_capabilities)
    refresh.start()
    assert entered.wait(2)
    release.set()
    refresh.join(2)

    assert not refresh.is_alive()
    assert len(calls) == 4
    assert peak == 2


@pytest.mark.parametrize("exit_path", ["no_prober", "contended", "fresh", "probed"])
def test_refresh_never_materializes_worker_snapshots(monkeypatch, exit_path):
    primary, workers = _roster(64)
    probes = []
    pool = OllamaWorkerPool(
        primary, workers, max_workers=64,
        capability_probe_parallelism=1, capability_probe_batch_size=2,
        capability_prober=None if exit_path == "no_prober" else (
            lambda origin: probes.append(origin) or {"models": ()}
        ),
    )
    if exit_path == "fresh":
        # A wholly fresh roster should do no probe or presentation work.
        monkeypatch.setattr(pool, "_capabilities_stale", lambda *_: False)
    if exit_path == "contended":
        assert pool._probe_lock.acquire(blocking=False)
    monkeypatch.setattr(pool, "_snapshot", lambda *_: pytest.fail("snapshot during refresh"))
    try:
        assert pool.refresh_capabilities() is None
    finally:
        if exit_path == "contended":
            pool._probe_lock.release()
    assert len(probes) == (2 if exit_path == "probed" else 0)


def test_status_pages_are_bounded_safe_and_report_active_limits():
    primary, workers = _roster(3)
    models = tuple("model-%04d-%s" % (index, "x" * 180) for index in range(2_048))

    pool = OllamaWorkerPool(
        primary,
        workers,
        max_workers=3,
        capability_probe_parallelism=1,
        capability_probe_batch_size=3,
        status_page_size=2,
        capability_prober=lambda _origin: {"models": models},
    )
    pool.refresh_capabilities()

    first = pool.status()
    assert first["schema_version"] == 2
    assert first["roster_generation"] == 1
    assert first["configured_worker_limit"] == 3
    assert first["worker_count"] == 3
    assert first["page_size"] == 2
    assert len(first["workers"]) == 2
    assert first["complete"] is False
    assert first["omitted_worker_count"] == 1
    assert first["next_cursor"]
    assert first["next_cursor"] != "2"
    assert first["serialized_bytes"] <= 65_536
    assert len(json.dumps(first).encode("utf-8")) == first["serialized_bytes"]
    for worker in first["workers"]:
        assert "models" not in worker
        assert "last_error" not in worker
        assert worker["model_count"] == 2_048
        assert len(worker["model_preview"]) == 8
        assert all(len(model) <= 128 for model in worker["model_preview"])
        assert worker["error_category"] == "none"

    second = pool.status(cursor=first["next_cursor"])
    assert second["complete"] is True
    assert second["omitted_worker_count"] == 0
    assert len(second["workers"]) == 1
    assert second["workers"][0]["worker_id"] not in {
        worker["worker_id"] for worker in first["workers"]
    }
    assert second["serialized_bytes"] <= 65_536
    assert len(json.dumps(second).encode("utf-8")) == second["serialized_bytes"]

    with pytest.raises(ValueError, match="cursor"):
        pool.status(cursor="not-a-valid-cursor")
    with pytest.raises(ValueError, match="page size"):
        pool.status(page_size=129)


def test_status_never_starts_a_capability_probe():
    calls: list[str] = []
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        capability_prober=lambda origin: calls.append(origin) or {"models": ()},
    )

    status = pool.status()

    assert calls == []
    assert status["workers"][0]["state"] == "unknown"


def test_status_stops_at_a_complete_record_before_the_byte_ceiling():
    primary, workers = _roster(128)
    models = tuple("model-%04d-%s" % (index, "x" * 180) for index in range(2_048))
    pool = OllamaWorkerPool(
        primary,
        workers,
        max_workers=128,
        capability_probe_parallelism=8,
        capability_probe_batch_size=128,
        status_page_size=128,
        capability_prober=lambda _origin: {"models": models},
    )
    pool.refresh_capabilities()

    page = pool.status()

    assert 0 < len(page["workers"]) < page["worker_count"]
    assert page["complete"] is False
    assert page["omitted_worker_count"] == (
        page["worker_count"] - len(page["workers"])
    )
    assert len(json.dumps(page).encode("utf-8")) == page["serialized_bytes"]
    assert page["serialized_bytes"] <= 65_536
    lines = pool.operator_status_lines()
    assert len(lines) == len(page["workers"]) + 2
    assert "omitted by the configured status page limit" in lines[-1]


@pytest.mark.parametrize("first,second", [
    ("https://Worker.Example:11434", "https://worker.example.:11434/"),
    ("https://[2001:db8::1]:11434", "https://[2001:0db8:0:0:0:0:0:1]:11434/"),
    ("http://[::1]:11434", "http://[0:0:0:0:0:0:0:1]:11434"),
])
@pytest.mark.parametrize("route", ["direct", "environment", "typed", "parser"])
def test_equivalent_origins_rejected_before_scheduler_creation(monkeypatch, first, second, route):
    import sonder_runtime.adapters.inference.ollama_pool as module

    monkeypatch.setattr(module, "_WorkerState", lambda *a, **k: pytest.fail("scheduler created"))
    with pytest.raises(ValueError, match="duplicate"):
        if route == "direct":
            OllamaWorkerPool(first, (second,), allow_remote=True)
        elif route == "environment":
            from_environment(first, {
                "SONDER_ALLOW_REMOTE_OLLAMA": "1", "SONDER_OLLAMA_WORKERS": second,
            })
        elif route == "typed":
            configure_typed_workers((first, second), allow_remote=True)
        else:
            parse_worker_origins(first + "," + second)


def test_status_builds_only_the_requested_page(monkeypatch):
    primary, workers = _roster(64)
    pool = OllamaWorkerPool(primary, workers, max_workers=64)
    first = pool.status(page_size=1)
    record = pool._status_worker_record
    visited = []

    def build(snapshot):
        visited.append(snapshot.origin)
        return record(snapshot)

    monkeypatch.setattr(pool, "_status_worker_record", build)
    page = pool.status(cursor=first["next_cursor"], page_size=1)
    assert visited == [workers[0]]
    assert page["worker_count"] == 64
    assert len(page["workers"]) == 1


def test_summary_never_builds_worker_or_model_details(monkeypatch):
    primary, workers = _roster(64)
    pool = OllamaWorkerPool(primary, workers, max_workers=64)
    monkeypatch.setattr(pool, "snapshots", lambda: pytest.fail("whole-roster snapshots"))
    monkeypatch.setattr(pool, "_status_worker_record", lambda _: pytest.fail("worker detail"))
    summary = pool.summary()
    assert summary["worker_count"] == 64
    assert summary["refresh_state"] == "not_refreshed"
    assert summary["refresh_age_seconds"] is None
    assert summary["eligible_worker_count"] == 0
    assert not {"workers", "origin", "tls_verification", "models"} & summary.keys()


def test_admin_page_cursor_rejects_other_principal_tampering_and_generation():
    primary, workers = _roster(3)
    pool = OllamaWorkerPool(primary, workers)
    page = pool.status(page_size=1, principal="admin-a")
    cursor = page["next_cursor"]
    for wrong in (cursor, cursor[:-3] + "xyz"):
        with pytest.raises(ValueError, match="cursor"):
            pool.status(cursor=wrong, principal="admin-b")
    pool._roster_generation += 1
    with pytest.raises(ValueError, match="cursor"):
        pool.status(cursor=cursor, principal="admin-a")


@pytest.mark.parametrize("route", ["direct", "environment", "typed"])
def test_trusted_cidr_never_relaxes_remote_https(route):
    with pytest.raises(ValueError, match="https"):
        if route == "direct":
            OllamaWorkerPool("http://127.0.0.1:11434", ("http://10.77.0.2:11434",),
                             allow_remote=True, trusted_origins=("10.77.0.0/24",))
        elif route == "environment":
            from_environment("http://127.0.0.1:11434", {
                "SONDER_ALLOW_REMOTE_OLLAMA": "1", "SONDER_TRUSTED_ORIGINS": "10.77.0.0/24",
                "SONDER_OLLAMA_WORKERS": "http://10.77.0.2:11434",
            })
        else:
            configure_typed_workers(("http://10.77.0.2:11434",), allow_remote=True,
                                    trusted_origins=("10.77.0.0/24",))


def test_admin_page_omits_unsafe_provider_model_and_version_text():
    from sonder_runtime.platform.logging import Redactor
    pool = OllamaWorkerPool("http://127.0.0.1:11434", redactor=Redactor(secret_values=("private-token-value",)),
                           capability_prober=lambda _: {
                               "version": "response body password=secret",
                               "models": ("safe:latest", "private-token-value", "C:/private/workspace/model", "<body>prompt secret</body>"),
                           })
    pool.refresh_capabilities()
    page = pool.status()
    assert page["workers"][0]["model_count"] == 4
    assert page["workers"][0]["model_preview"] == ["safe:latest"]
    assert page["workers"][0]["version"] == "unknown"
    assert "private" not in json.dumps(page)
    assert "secret" not in json.dumps(page)
