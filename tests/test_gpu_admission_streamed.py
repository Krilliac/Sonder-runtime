"""STREAMED footprints, the bounded footprint window, and non-Ollama admission."""
from __future__ import annotations

import contextlib
import contextvars
import sqlite3

import pytest

from sonder_runtime.adapters.inference import gpu_admission as adapter
from sonder_runtime.adapters.inference import gpu_admission_store as store_module
from sonder_runtime.adapters.inference.gpu_admission_store import GpuAdmissionStore
from sonder_runtime.domain import gpu_admission as domain
from sonder_runtime.domain.common.errors import (
    CapacityExceeded, DependencyUnavailable, InternalFailure, SonderError)
from sonder_runtime.platform.gpu_admission_config import GpuAdmissionConfig
from tests.support.gpu_admission_harness import GIB, MODEL, FakeOllama, chat_body

ORIGIN = "http://127.0.0.1:11434"
BACKEND = "http://127.0.0.1:8081"
DAY = 86400.0


# -- the STREAMED estimate ------------------------------------------------------------
def _small_info() -> dict[str, object]:
    return {
        "general.architecture": "demo",
        "demo.block_count": 4, "demo.embedding_length": 1000,
        "demo.attention.head_count": 10, "demo.attention.head_count_kv": 2,
        "demo.attention.key_length": 100, "demo.attention.value_length": 100,
        "demo.expert_count": 10, "demo.expert_used_count": 2,
        "demo.expert_feed_forward_length": 500, "demo.vocab_size": 1000,
    }


def test_expert_model_shape_reads_the_gguf_metadata():
    shape = domain.expert_model_shape(_small_info())
    assert shape is not None
    assert (shape.block_count, shape.embedding_length, shape.head_count_kv) == (4, 1000, 2)
    assert (shape.expert_count, shape.expert_used_count, shape.expert_ff_length) == (10, 2, 500)
    assert domain.kv_bytes_per_token(shape) == 3200  # 4 blocks * 2 kv heads * 200 * 2 bytes


def test_a_sibling_block_never_stands_in_for_a_named_architecture():
    info = dict(_small_info(), **{"clip.vision.attention.head_count": 99})
    del info["demo.attention.head_count"]
    assert domain.expert_model_shape(info) is None  # the clip key is not the model's head count
    nameless = {key: value for key, value in _small_info().items() if key != "general.architecture"}
    assert domain.expert_model_shape(nameless) is not None  # no name: the suffix match applies


def test_a_dense_or_unreadable_model_has_no_expert_shape():
    dense = dict(_small_info(), **{"demo.expert_count": 0})
    assert domain.expert_model_shape(dense) is None
    assert domain.expert_model_shape({}) is None
    assert domain.expert_model_shape(None) is None


def test_streamed_estimate_from_metadata_is_dense_kv_workspace_cache_and_reserve():
    # experts 4*10*3*1000*500 = 60_000_000 params; attention 4*1000*(2*10*100 + 2*200) = 9_600_000;
    # embeddings 2*1000*1000 = 2_000_000; router 4*1000*10 = 40_000 -> dense 11_640_000.
    # At 1 byte per parameter the file is 71_640_000: dense 11_640_000, experts 60_000_000.
    shape = domain.expert_model_shape(_small_info())
    policy = domain.GpuAdmissionPolicy(host_overhead_bytes=500_000, streamed_workspace_bytes=1_000_000)
    got = domain.estimate_streamed_footprint(
        file_size_bytes=71_640_000, num_ctx=1000,
        placement=domain.StreamedPlacement(expert_cache_bytes=20_000_000),
        shape=shape, policy=policy, digest="d")
    assert got is not None
    # 11_640_000 dense + 3_200_000 KV (3200 B/token * 1000) + 1_000_000 workspace
    # + 20_000_000 cache + 500_000 reserve
    assert got.working_set_bytes == got.private_bytes == 36_340_000
    assert (got.source, got.digest, got.num_ctx) == (domain.FootprintSource.STREAMED, "d", 1000)


def test_the_expert_cache_is_clamped_to_the_experts_that_exist():
    shape = domain.expert_model_shape(_small_info())
    policy = domain.GpuAdmissionPolicy(host_overhead_bytes=0, streamed_workspace_bytes=0)
    got = domain.estimate_streamed_footprint(
        file_size_bytes=71_640_000, num_ctx=0,
        placement=domain.StreamedPlacement(expert_cache_bytes=10**12, kv_bytes_per_token=0),
        shape=shape, policy=policy)
    assert got is not None and got.working_set_bytes == 71_640_000  # dense + all experts


def test_a_declared_dense_size_and_kv_rate_need_no_metadata():
    # 120 GiB file, 6 GiB dense, 98_304 B/token * 32_768 = 3 GiB KV, 1 GiB workspace,
    # 16 GiB cache, 1 GiB reserve -> 27 GiB, against 120 * 1.25 + 1 = 151 GiB unstreamed.
    policy = domain.GpuAdmissionPolicy()
    placement = domain.StreamedPlacement(
        expert_cache_bytes=16 * GIB, dense_bytes=6 * GIB, kv_bytes_per_token=98_304)
    got = domain.estimate_streamed_footprint(
        file_size_bytes=120 * GIB, num_ctx=32_768, placement=placement, shape=None, policy=policy)
    assert got is not None and got.working_set_bytes == 27 * GIB
    ordinary = domain.estimate_host_footprint(
        120 * GIB, factor_pct=policy.unmeasured_host_factor_pct,
        overhead_bytes=policy.host_overhead_bytes)
    assert ordinary.working_set_bytes == 151 * GIB


def test_state_bytes_are_added_once_and_do_not_scale_with_the_window():
    policy = domain.GpuAdmissionPolicy(host_overhead_bytes=0, streamed_workspace_bytes=0)
    placement = domain.StreamedPlacement(
        expert_cache_bytes=0, dense_bytes=100, kv_bytes_per_token=10, state_bytes=7)
    for ctx, expected in ((1, 117), (5, 157)):
        got = domain.estimate_streamed_footprint(
            file_size_bytes=1000, num_ctx=ctx, placement=placement, shape=None, policy=policy)
        assert got is not None and got.working_set_bytes == expected


def test_an_undecidable_streamed_estimate_is_none_so_the_ordinary_one_applies():
    policy = domain.GpuAdmissionPolicy()
    cache_only = domain.StreamedPlacement(expert_cache_bytes=GIB)
    assert domain.estimate_streamed_footprint(
        file_size_bytes=GIB, num_ctx=1, placement=cache_only, shape=None, policy=policy) is None
    no_kv = domain.StreamedPlacement(expert_cache_bytes=GIB, dense_bytes=GIB)
    assert domain.estimate_streamed_footprint(
        file_size_bytes=GIB, num_ctx=1, placement=no_kv, shape=None, policy=policy) is None


def test_placement_rejects_negative_numbers():
    with pytest.raises(ValueError):
        domain.StreamedPlacement(expert_cache_bytes=-1)
    with pytest.raises(ValueError):
        domain.StreamedPlacement(expert_cache_bytes=0, dense_bytes=-1)


def test_a_streamed_footprint_beats_a_measured_one_and_the_ordinary_estimate():
    policy = domain.GpuAdmissionPolicy()
    measured = domain.Footprint(90 * GIB, 90 * GIB, domain.FootprintSource.MEASURED, "d", 4096)
    streamed = domain.Footprint(27 * GIB, 27 * GIB, domain.FootprintSource.STREAMED, "d", 4096)
    chosen = domain.select_footprint(
        measured_exact=measured, measured_other=[], file_size_bytes=120 * GIB,
        requested_ctx=4096, policy=policy, streamed=streamed)
    assert chosen is streamed
    unchanged = domain.select_footprint(
        measured_exact=measured, measured_other=[], file_size_bytes=120 * GIB,
        requested_ctx=4096, policy=policy)
    assert unchanged is measured


# -- the bounded footprint window ----------------------------------------------------
class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _store(tmp_path, clock):
    return GpuAdmissionStore(tmp_path / "gpu.sqlite3", clock=clock)


def _numbers(store, digest="d", ctx=4096):
    rows = {r.num_ctx: r for r in store.footprints_for(digest)}
    return (rows[ctx].working_set_bytes, rows[ctx].private_bytes) if ctx in rows else None


def test_window_percentile_is_nearest_rank():
    assert store_module.window_percentile([5], 90) == 5
    assert store_module.window_percentile(list(range(1, 11)), 90) == 9
    assert store_module.window_percentile(list(range(1, 11)), 100) == 10
    assert store_module.window_percentile([3, 1, 2], 90) == 3


def test_an_outlier_ages_out_of_a_full_window(tmp_path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    store.record_footprint("d", 4096, 1000, 1000)  # the outlier, e.g. an mmapped giant
    assert _numbers(store) == (1000, 1000)  # a lone sample is all there is
    for _ in range(store_module.FOOTPRINT_WINDOW):
        clock.now += 60
        store.record_footprint("d", 4096, 10, 10)
    assert _numbers(store) == (10, 10)


def test_a_percentile_trims_one_outlier_inside_a_full_window_but_not_a_short_one(tmp_path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    store.record_footprint("d", 4096, 1000, 1000)
    for _ in range(8):
        store.record_footprint("d", 4096, 10, 10)
    assert _numbers(store) == (1000, 1000)  # nine samples: the percentile is still the maximum
    for _ in range(store_module.FOOTPRINT_WINDOW - 9):
        store.record_footprint("d", 4096, 10, 10)
    # sixteen samples, rank ceil(0.9 * 16) = 15: the one outlier is the sixteenth value
    assert _numbers(store) == (10, 10)


def test_the_window_keeps_only_the_newest_samples(tmp_path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    for value in range(1, 20):
        clock.now += 1
        store.record_footprint("d", 4096, value, value)
    with sqlite3.connect(store.path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM gpu_footprint_samples").fetchone()[0]
        kept = sorted(row[0] for row in connection.execute(
            "SELECT working_set FROM gpu_footprint_samples"))
    assert count == store_module.FOOTPRINT_WINDOW
    assert kept == list(range(4, 20))  # exactly the newest sixteen


def test_an_old_sample_stands_until_a_newer_one_replaces_it(tmp_path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    store.record_footprint("d", 4096, 500, 500)
    assert _numbers(store) == (500, 500)
    clock.now += store_module.FOOTPRINT_MAX_AGE_SECONDS + 1
    assert _numbers(store) == (500, 500)  # nothing newer exists, so the last window stands
    store.record_footprint("d", 4096, 20, 30)
    assert _numbers(store) == (20, 30)


def test_a_recent_sample_after_old_ones_replaces_the_value_unlike_keep_the_maximum(tmp_path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    store.record_footprint("d", 4096, 900, 900)
    clock.now += 8 * DAY
    store.record_footprint("d", 4096, 40, 40)
    assert _numbers(store) == (40, 40)


def _seed_old_store(path, rows):
    """A store as the keep-the-maximum build left it: no sample table, no marker."""
    old = GpuAdmissionStore(path)
    old.verify()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE gpu_footprint_samples")
        connection.execute("DELETE FROM gpu_meta WHERE key=?", (store_module._SAMPLES_MIGRATED,))
        for digest, ctx, ws, private, at in rows:
            connection.execute(
                "INSERT INTO gpu_footprints(digest,num_ctx,working_set,private_bytes,measured_at) "
                "VALUES(?,?,?,?,?)", (digest, ctx, ws, private, at))


def test_old_max_rows_migrate_to_one_legacy_sample_and_keep_their_value(tmp_path):
    clock = _Clock()
    path = tmp_path / "gpu.sqlite3"
    _seed_old_store(path, [("d", 4096, 700, 800, clock.now - DAY)])
    store = GpuAdmissionStore(path, clock=clock)
    assert _numbers(store) == (700, 800)
    assert store.footprints_for("d")[0].measured_at == clock.now - DAY
    # a second open does not import again
    again = GpuAdmissionStore(path, clock=clock)
    again.verify()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gpu_footprint_samples").fetchone()[0] == 1


def test_a_poisoned_legacy_maximum_is_retired_by_fresh_samples(tmp_path):
    clock = _Clock()
    path = tmp_path / "gpu.sqlite3"
    _seed_old_store(path, [("d", 4096, 10**12, 10**12, clock.now - DAY)])
    store = GpuAdmissionStore(path, clock=clock)
    for _ in range(store_module.FOOTPRINT_LEGACY_MIN_FRESH):
        assert _numbers(store) == (10**12, 10**12)  # still the only evidence
        clock.now += 60
        store.record_footprint("d", 4096, 50, 60)
    assert _numbers(store) == (50, 60)


def test_a_legacy_row_older_than_the_maximum_age_is_kept_on_read(tmp_path):
    clock = _Clock()
    path = tmp_path / "gpu.sqlite3"
    _seed_old_store(path, [("d", 4096, 5, 5, clock.now - 30 * DAY)])
    assert _numbers(GpuAdmissionStore(path, clock=clock)) == (5, 5)


def test_the_aggregate_column_is_kept_current_for_an_older_build(tmp_path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    store.record_footprint("d", 4096, 10, 20)
    store.record_footprint("d", 4096, 30, 5)
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT working_set,private_bytes FROM gpu_footprints WHERE digest='d'").fetchone()
    assert row == (30, 20)


# -- service wiring ----------------------------------------------------------------------
def _reading(available):
    return domain.HostMemoryReading(
        available_physical_bytes=available, total_physical_bytes=64 * GIB,
        commit_limit_bytes=96 * GIB, commit_available_bytes=64 * GIB, commit_enforced=True)


def _service(tmp_path, fake, *, available, streamed=None, show=None, store=None, **config):
    values = {"mode": "enforce", "store_path": str(tmp_path / "gpu-admission.sqlite3"),
              "poll_interval_ms": 20, "on_host_floor": "refuse"}
    values.update(config)
    return adapter.GpuAdmission(
        GpuAdmissionConfig(**values), host_reader=lambda: _reading(available),
        ps_reader=fake.ps, tags_reader=fake.tags, memory_reader=lambda _key: None,
        show_reader=show or (lambda _origin, _model: None), background=lambda work: work(),
        streamed=streamed, store=store)


_GIANT = domain.StreamedPlacement(
    expert_cache_bytes=16 * GIB, dense_bytes=6 * GIB, kv_bytes_per_token=98_304)


def test_a_giant_ollama_model_is_refused_by_the_ordinary_estimate(tmp_path):
    fake = FakeOllama(file_size=120 * GIB)
    service = _service(tmp_path, fake, available=40 * GIB)
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        with service.admit("/api/chat", chat_body(32_768), origin=ORIGIN):
            pass
    assert caught.value.refusal.reason is domain.RefusalReason.HOST_RAM_FLOOR
    assert caught.value.refusal.footprint.source is domain.FootprintSource.ESTIMATED


def test_a_declared_streamed_ollama_model_is_admitted_on_its_resident_size(tmp_path):
    fake = FakeOllama(file_size=120 * GIB)
    service = _service(tmp_path, fake, available=40 * GIB, streamed={MODEL: _GIANT})
    with service.admit("/api/chat", chat_body(32_768), origin=ORIGIN) as ticket:
        assert ticket.lane is domain.AdmissionLane.INFERENCE


def test_a_streamed_model_is_still_refused_when_even_its_resident_size_does_not_fit(tmp_path):
    fake = FakeOllama(file_size=120 * GIB)
    service = _service(tmp_path, fake, available=30 * GIB, streamed={MODEL: _GIANT})
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        with service.admit("/api/chat", chat_body(32_768), origin=ORIGIN):
            pass
    refusal = caught.value.refusal
    assert refusal.footprint.source is domain.FootprintSource.STREAMED
    assert refusal.footprint.working_set_bytes == 27 * GIB  # 30 - 27 = 3 GiB < the 4 GiB floor


def test_streamed_sizing_reads_expert_metadata_from_api_show_once(tmp_path):
    calls = []

    def show(origin, model):
        calls.append((origin, model))
        return {"model_info": _small_info()}

    fake = FakeOllama(file_size=71_640_000)
    policy_free = domain.StreamedPlacement(expert_cache_bytes=20_000_000)
    service = _service(tmp_path, fake, available=64 * GIB, streamed={MODEL: policy_free}, show=show,
                       host_overhead_mb=0)
    for _ in range(2):
        with service.admit("/api/chat", chat_body(1000), origin=ORIGIN):
            pass
        fake.loaded.clear()
    assert len(calls) == 1
    footprint = service._streamed_footprint(
        adapter._origin_without_userinfo(ORIGIN), adapter.model_key(MODEL),
        next(iter(service._read_tags(ORIGIN).values())).digest, 71_640_000, 1000)
    # dense 11_640_000 + KV 3_200_000 + 1 GiB default workspace + 20_000_000 cache + 0 reserve
    assert footprint is not None
    assert footprint.working_set_bytes == 11_640_000 + 3_200_000 + GIB + 20_000_000


def test_an_unreadable_api_show_is_not_asked_again_for_a_while(tmp_path):
    calls = []

    def show(origin, model):
        calls.append((origin, model))
        return None

    fake = FakeOllama(file_size=120 * GIB)
    service = _service(tmp_path, fake, available=64 * GIB, streamed={MODEL: _GIANT}, show=show)
    for _ in range(2):
        with service.admit("/api/chat", chat_body(32_768), origin=ORIGIN):
            pass
        fake.loaded.clear()
    assert len(calls) == 1


def test_an_idle_measured_model_that_fit_is_still_admitted_after_eight_days(tmp_path):
    clock = _Clock()
    store = GpuAdmissionStore(tmp_path / "measured.sqlite3", clock=clock)
    fake = FakeOllama(file_size=20 * GIB)
    service = _service(tmp_path, fake, available=26 * GIB, store=store)
    digest = next(iter(service._read_tags(ORIGIN).values())).digest
    store.record_footprint(digest, 4096, 18 * GIB, 18 * GIB)
    clock.now += 8 * DAY
    # the measurement is the only evidence; the 26 GiB estimate would be refused
    with service.admit("/api/chat", chat_body(4096), origin=ORIGIN) as ticket:
        assert ticket.lane is domain.AdmissionLane.INFERENCE


def test_a_declared_model_without_enough_metadata_falls_back_to_the_ordinary_estimate(tmp_path):
    fake = FakeOllama(file_size=120 * GIB)
    service = _service(tmp_path, fake, available=40 * GIB,
                       streamed={MODEL: domain.StreamedPlacement(expert_cache_bytes=GIB)})
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        with service.admit("/api/chat", chat_body(4096), origin=ORIGIN):
            pass
    assert caught.value.refusal.footprint.source is domain.FootprintSource.ESTIMATED


def test_a_streamed_model_never_has_its_runner_working_set_recorded(tmp_path):
    fake = FakeOllama(file_size=120 * GIB)
    service = _service(tmp_path, fake, available=64 * GIB, streamed={MODEL: _GIANT})
    service._memory_reader = lambda _key: (100 * GIB, 100 * GIB)
    with service.admit("/api/chat", chat_body(32_768), origin=ORIGIN):
        pass
    digest = next(iter(service._read_tags(ORIGIN).values())).digest
    assert service._the_store().footprints_for(digest) == ()


# -- non-Ollama local backends -----------------------------------------------------------
def _declared(working_set):
    return domain.Footprint(working_set, working_set, domain.FootprintSource.STREAMED, "decl", 8192)


def test_a_loopback_backend_with_a_declared_footprint_is_refused_when_it_does_not_fit(tmp_path):
    service = _service(tmp_path, FakeOllama(), available=20 * GIB)
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB),
                                   resident=lambda: False):
            pytest.fail("must not be admitted")
    refusal = caught.value.refusal
    assert refusal.reason is domain.RefusalReason.HOST_RAM_FLOOR  # 20 - 18 = 2 GiB < 4 GiB floor
    assert refusal.footprint.source is domain.FootprintSource.STREAMED


def test_a_loopback_backend_that_fits_is_admitted_and_takes_a_gpu_slot(tmp_path):
    service = _service(tmp_path, FakeOllama(), available=30 * GIB)
    with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB)) as ticket:
        assert ticket.lane is domain.AdmissionLane.INFERENCE
        assert ticket.payload is None and not ticket.rewritten
        assert [lease.model_key for lease in service._the_store().leases()] == ["giant-moe"]
    assert service._the_store().leases() == ()


def test_a_resident_backend_skips_the_host_check(tmp_path):
    service = _service(tmp_path, FakeOllama(), available=1 * GIB)
    with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB),
                               resident=lambda: True):
        pass


def test_an_idle_loaded_backend_is_not_charged_again(tmp_path):
    clock = _Clock(100.0)
    service = _service(tmp_path, FakeOllama(), available=30 * GIB)
    service._clock = clock
    with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB)):
        pass
    # memory is now used by that model and nothing says it was unloaded
    service._host_reader = lambda: _reading(1 * GIB)
    clock.now += 3600.0
    with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB)):
        pass


def test_a_restarted_service_does_not_charge_a_loaded_backend(tmp_path):
    first = _service(tmp_path, FakeOllama(), available=30 * GIB)
    with first.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB)):
        pass
    second = _service(tmp_path, FakeOllama(), available=1 * GIB)  # same store, no memory of the first
    with second.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB)):
        assert [lease.model_key for lease in second._the_store().leases()] == ["giant-moe"]
    assert second._the_store().leases() == ()


def test_a_residency_probe_that_raises_counts_as_unknown(tmp_path):
    service = _service(tmp_path, FakeOllama(), available=1 * GIB)

    def probe():
        raise RuntimeError("backend down")

    with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB),
                               resident=probe):
        pass
    assert service._the_store().leases() == ()


def test_a_backend_request_that_fails_inside_the_block_releases_its_slot(tmp_path):
    service = _service(tmp_path, FakeOllama(), available=30 * GIB)
    with pytest.raises(RuntimeError):
        with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB)):
            raise RuntimeError("backend down")
    assert service._the_store().leases() == ()


def test_a_remote_backend_and_mode_off_pass_through(tmp_path):
    service = _service(tmp_path, FakeOllama(), available=1 * GIB)
    with service.admit_backend("m", origin="http://10.1.2.3:8081", footprint=_declared(99 * GIB)) as ticket:
        assert ticket.lane is None
    off = _service(tmp_path, FakeOllama(), available=1 * GIB, mode="off")
    with off.admit_backend("m", origin=BACKEND, footprint=_declared(99 * GIB)) as ticket:
        assert ticket.lane is None


def test_observe_mode_never_blocks_a_backend(tmp_path):
    service = _service(tmp_path, FakeOllama(), available=1 * GIB, mode="observe")
    with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB)) as ticket:
        assert ticket.lane is domain.AdmissionLane.INFERENCE


def test_the_queue_mode_waits_then_refuses_with_the_backend_footprint(tmp_path):
    service = _service(tmp_path, FakeOllama(), available=20 * GIB, on_host_floor="queue",
                       max_queue_wait_seconds=1, host_recheck_interval_ms=100)
    service._the_store()  # open and migrate first, so the 0.3 s budget is not spent on disk setup
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        with service.admit_backend("giant-moe", origin=BACKEND, footprint=_declared(18 * GIB),
                                   deadline=service._clock() + 0.3, resident=lambda: False):
            pass
    assert caught.value.refusal.reason is domain.RefusalReason.HOST_RAM_FLOOR
    assert service._counters["queued"] >= 1  # it waited and rechecked before giving up
    assert service._the_store().leases() == ()


# -- a request that names no window ---------------------------------------------------------
def test_a_streamed_estimate_without_a_window_uses_the_declared_then_the_model_window():
    policy = domain.GpuAdmissionPolicy(host_overhead_bytes=0, streamed_workspace_bytes=0)
    info = dict(_small_info(), **{"demo.context_length": 8000})
    shape = domain.expert_model_shape(info)
    assert shape is not None and shape.context_length == 8000

    def estimate(placement, shape_):
        got = domain.estimate_streamed_footprint(
            file_size_bytes=71_640_000, num_ctx=0, placement=placement, shape=shape_, policy=policy)
        assert got is not None
        return got

    bare = domain.StreamedPlacement(expert_cache_bytes=0)
    from_model = estimate(bare, shape)
    assert from_model.num_ctx == 8000
    assert from_model.working_set_bytes == 11_640_000 + 3200 * 8000
    declared = estimate(domain.StreamedPlacement(expert_cache_bytes=0, num_ctx=2000), shape)
    assert declared.num_ctx == 2000 and declared.working_set_bytes == 11_640_000 + 3200 * 2000
    nothing = estimate(bare, domain.expert_model_shape(_small_info()))
    assert nothing.num_ctx == 4096 and nothing.working_set_bytes == 11_640_000 + 3200 * 4096


def test_an_ollama_request_with_no_window_is_charged_kv_for_the_declared_window(tmp_path):
    fake = FakeOllama(file_size=120 * GIB)
    placement = domain.StreamedPlacement(
        expert_cache_bytes=16 * GIB, dense_bytes=6 * GIB, kv_bytes_per_token=98_304, num_ctx=32_768)
    service = _service(tmp_path, fake, available=20 * GIB, streamed={MODEL: placement})
    body = dict(chat_body(1), options={})
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        with service.admit("/api/chat", body, origin=ORIGIN):
            pass
    footprint = caught.value.refusal.footprint
    assert footprint.source is domain.FootprintSource.STREAMED and footprint.num_ctx == 32_768
    assert footprint.working_set_bytes >= 6 * GIB + 98_304 * 32_768 + 16 * GIB


# -- the openai-compatible binding -----------------------------------------------------------
_BACKEND_PLACEMENT = domain.StreamedPlacement(
    expert_cache_bytes=16 * GIB, dense_bytes=6 * GIB, kv_bytes_per_token=98_304, num_ctx=8192)


def _gateway(calls, base_url=BACKEND):
    from sonder_runtime.adapters.inference.openai_compat_gateway import (
        OpenAICompatibleConfig, OpenAICompatibleGateway)

    def transport(url, payload, headers, timeout):
        calls.append(payload["model"])
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    return OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url, model="giant-moe"), transport=transport)


def _generate(gateway, *, cloud=False):
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.model_gateway import ModelRequest

    context = local_owner_context(
        correlation_id="req_moe", cloud_allowed=cloud, timeout_seconds=30.0)
    return gateway.generate(ModelRequest(prompt="hi", tier="code"), context)


@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch):
    for name in ("SONDER_GPU_STREAMED_MODELS", "SONDER_GPU_SLOTS"):
        monkeypatch.delenv(name, raising=False)
    adapter.reset_typed_gpu_admission()
    adapter.set_process_service(None)
    yield
    adapter.reset_typed_gpu_admission()
    adapter.set_process_service(None)


class _StubService:
    """A process service whose backend gate refuses with a chosen reason."""

    def __init__(self, reason, detail="does not fit", retry_after=7.0):
        self.refusal = domain.Refused(reason, detail, retry_after_seconds=retry_after)

    def declared_backend_footprint(self, model):
        return _declared(18 * GIB)

    @contextlib.contextmanager
    def admit_backend(self, model, **_kwargs):
        raise adapter.GpuAdmissionRefused(self.refusal)
        yield  # unreachable: makes this a generator


def test_the_openai_binding_maps_a_refusal_to_a_domain_error(monkeypatch):
    stub = _StubService(domain.RefusalReason.HOST_RAM_FLOOR)
    monkeypatch.setattr(adapter, "_process_service", stub)
    calls = []
    with pytest.raises(CapacityExceeded) as caught:
        _generate(_gateway(calls))
    error = caught.value
    assert isinstance(error, SonderError)
    assert error.reason == "host_ram_floor" and error.retry_after == 7.0
    assert type(error.__cause__) is adapter.GpuAdmissionRefused
    assert calls == []  # refused before the backend was contacted


def test_a_store_failure_is_dependency_unavailable(monkeypatch):
    stub = _StubService(domain.RefusalReason.STORE_UNAVAILABLE, detail="cannot open C:/secret/gpu.db")
    monkeypatch.setattr(adapter, "_process_service", stub)
    calls = []
    with pytest.raises(DependencyUnavailable) as caught:
        _generate(_gateway(calls))
    assert "secret" not in str(caught.value)  # the store error text can hold a path
    assert type(caught.value.__cause__) is adapter.GpuAdmissionRefused
    assert calls == []


def test_a_queue_timeout_through_the_real_service_is_a_domain_error(tmp_path, monkeypatch):
    from sonder_runtime.adapters.inference.openai_compat_gateway import BackendAdmissionRefused

    service = _service(tmp_path, FakeOllama(), available=60 * GIB, max_queue_wait_seconds=1,
                       streamed={"giant-moe": _BACKEND_PLACEMENT})
    monkeypatch.setattr(adapter, "_process_service", service)
    calls = []
    with service.admit_backend("other", origin=BACKEND, footprint=_declared(GIB)):
        # a fresh context: the held ticket of the outer block would otherwise pass this through
        with pytest.raises(BackendAdmissionRefused) as caught:
            contextvars.Context().run(_generate, _gateway(calls))
        error = caught.value
        assert isinstance(error, SonderError) and error.reason == "queue_timeout"
        assert isinstance(error.__cause__, adapter.GpuAdmissionRefused)
        assert error.retry_after == error.__cause__.retry_after_seconds
        assert [lease.model_key for lease in service._the_store().leases()] == ["other"]
    assert calls == []


def test_a_remote_endpoint_ignores_a_bad_admission_variable(monkeypatch):
    for name, value in (("SONDER_GPU_STREAMED_MODELS", "not json"), ("SONDER_GPU_SLOTS", "abc")):
        monkeypatch.setenv(name, value)
        adapter.set_process_service(None)
        calls = []
        gateway = _gateway(calls, base_url="https://api.example.com/v1")
        assert _generate(gateway, cloud=True).text == "ok"
        assert calls == ["giant-moe"]
        assert adapter._process_service is None
        monkeypatch.delenv(name)


def test_a_bad_streamed_variable_is_rejected_at_configure_time(monkeypatch):
    monkeypatch.setenv("SONDER_GPU_STREAMED_MODELS", "not json")
    with pytest.raises(ValueError, match="SONDER_GPU_STREAMED_MODELS"):
        adapter.configure_typed_gpu_admission(GpuAdmissionConfig(mode="off"))


def test_configured_placements_reach_the_process_service(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SONDER_GPU_STREAMED_MODELS",
        '{"giant-moe": {"expert_cache_bytes": 1000, "dense_bytes": 10, "kv_bytes_per_token": 5}}')
    adapter.configure_typed_gpu_admission(
        GpuAdmissionConfig(mode="enforce", store_path=str(tmp_path / "gpu.sqlite3")))
    # the process never re-reads the environment once configured
    monkeypatch.setenv("SONDER_GPU_STREAMED_MODELS", "not json")
    assert adapter.process_service().declared_backend_footprint("giant-moe") is not None


def test_an_undeclared_loopback_endpoint_never_builds_the_service(monkeypatch):
    monkeypatch.setenv("SONDER_GPU_SLOTS", "abc")
    calls = []
    assert _generate(_gateway(calls)).text == "ok"
    assert calls == ["giant-moe"]
    assert adapter._process_service is None


def test_a_loopback_endpoint_with_a_bad_variable_fails_with_a_domain_error(monkeypatch):
    monkeypatch.setenv("SONDER_GPU_STREAMED_MODELS", "not json")
    calls = []
    with pytest.raises(InternalFailure) as caught:
        _generate(_gateway(calls))
    assert "SONDER_GPU_STREAMED_MODELS" in str(caught.value)
    assert calls == []


def test_the_openai_binding_admits_a_declared_model_of_unknown_residency(tmp_path, monkeypatch):
    service = _service(tmp_path, FakeOllama(), available=20 * GIB,
                       streamed={"giant-moe": _BACKEND_PLACEMENT})
    monkeypatch.setattr(adapter, "_process_service", service)
    calls = []
    assert _generate(_gateway(calls)).text == "ok"
    assert calls == ["giant-moe"]
    assert service._the_store().leases() == ()


def test_the_openai_binding_admits_a_declared_model_that_fits_and_releases_its_slot(tmp_path, monkeypatch):
    service = _service(tmp_path, FakeOllama(), available=60 * GIB,
                       streamed={"giant-moe": _BACKEND_PLACEMENT})
    monkeypatch.setattr(adapter, "_process_service", service)
    calls = []
    assert _generate(_gateway(calls)).text == "ok"
    assert calls == ["giant-moe"]
    assert service._the_store().leases() == ()


def test_the_openai_binding_does_not_gate_an_undeclared_model(tmp_path, monkeypatch):
    service = _service(tmp_path, FakeOllama(), available=1 * GIB)
    monkeypatch.setattr(adapter, "_process_service", service)
    calls = []
    assert _generate(_gateway(calls)).text == "ok"
    assert calls == ["giant-moe"]


def test_placements_are_read_from_the_environment():
    text = '{"Giant-MoE": {"expert_cache_bytes": 5, "dense_bytes": 6, "kv_bytes_per_token": 7, "num_ctx": 8}}'
    got = adapter.streamed_placements_from_environment({adapter.STREAMED_MODELS_ENV: text})
    assert got == {"Giant-MoE": domain.StreamedPlacement(5, 6, 7, 0, 8)}
    assert adapter.streamed_placements_from_environment({}) == {}
    for bad in ("not json", "[]", '{"m": {}}', '{"m": {"expert_cache_bytes": 1, "bogus": 2}}',
                '{"m": {"expert_cache_bytes": -1}}'):
        with pytest.raises(ValueError):
            adapter.streamed_placements_from_environment({adapter.STREAMED_MODELS_ENV: bad})


# -- liveness across the admission wait --------------------------------------------------------
class _Token:
    def __init__(self):
        self.cancelled = False

    def wait(self, timeout=None):
        return self.cancelled


def _generate_while_slot_is_held(service, gateway, *, token, timeout, hold_seconds, on_release):
    """Run one request while another holder owns the single slot; release it after a delay."""
    import threading
    import time

    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.model_gateway import ModelRequest

    ready, release = threading.Event(), threading.Event()

    def holder():
        with service.admit_backend("other", origin=BACKEND, footprint=_declared(GIB)):
            ready.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert ready.wait(10)

    def free_the_slot():
        time.sleep(hold_seconds)
        on_release()
        release.set()

    freer = threading.Thread(target=free_the_slot)
    freer.start()
    context = local_owner_context(
        correlation_id="req_moe", timeout_seconds=timeout, cancellation=token)
    try:
        return gateway.generate(ModelRequest(prompt="hi", tier="code"), context)
    finally:
        freer.join()
        thread.join()


def test_a_request_cancelled_while_queued_is_not_sent(tmp_path, monkeypatch):
    from sonder_runtime.domain.common.errors import Cancelled

    service = _service(tmp_path, FakeOllama(), available=60 * GIB, max_queue_wait_seconds=10,
                       streamed={"giant-moe": _BACKEND_PLACEMENT})
    monkeypatch.setattr(adapter, "_process_service", service)
    calls, token = [], _Token()

    def cancel():
        token.cancelled = True

    with pytest.raises(Cancelled):
        _generate_while_slot_is_held(
            service, _gateway(calls), token=token, timeout=30.0, hold_seconds=0.4,
            on_release=cancel)
    assert calls == []
    assert service._the_store().leases() == ()


def test_a_request_that_expires_while_queued_is_not_sent(tmp_path, monkeypatch):
    import time

    from sonder_runtime.domain.common.errors import DeadlineExceeded

    class _SlowGrant(_StubService):
        """Grants the slot only after the request's deadline has passed."""

        @contextlib.contextmanager
        def admit_backend(self, model, **_kwargs):
            time.sleep(0.5)
            yield

    monkeypatch.setattr(adapter, "_process_service", _SlowGrant(None))
    calls = []
    with pytest.raises(DeadlineExceeded):
        _generate_with_timeout(_gateway(calls), 0.2)
    assert calls == []


def _generate_with_timeout(gateway, seconds):
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.model_gateway import ModelRequest

    context = local_owner_context(correlation_id="req_moe", timeout_seconds=seconds)
    return gateway.generate(ModelRequest(prompt="hi", tier="code"), context)


def test_the_transport_timeout_is_what_is_left_after_the_wait(tmp_path, monkeypatch):
    from sonder_runtime.adapters.inference.openai_compat_gateway import (
        OpenAICompatibleConfig, OpenAICompatibleGateway)

    service = _service(tmp_path, FakeOllama(), available=60 * GIB, max_queue_wait_seconds=10,
                       streamed={"giant-moe": _BACKEND_PLACEMENT})
    monkeypatch.setattr(adapter, "_process_service", service)
    seen = []

    def transport(url, payload, headers, timeout):
        seen.append(timeout)
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig(BACKEND, model="giant-moe"), transport=transport)
    _generate_while_slot_is_held(
        service, gateway, token=_Token(), timeout=5.0, hold_seconds=0.6, on_release=lambda: None)
    assert len(seen) == 1 and seen[0] < 5.0 - 0.4


def test_a_streamed_placement_error_names_the_variable_and_model():
    text = '{"giant-moe": {"expert_cache_bytes": -1}}'
    with pytest.raises(ValueError, match="SONDER_GPU_STREAMED_MODELS.*giant-moe"):
        adapter.streamed_placements_from_environment({adapter.STREAMED_MODELS_ENV: text})
