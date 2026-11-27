"""Regression tests for pooled model tool-capability detection and bounded probing."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import threading
import time
from types import SimpleNamespace

from sonder_runtime.adapters.inference import gpu_admission, ollama_pool
from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    _default_capability_prober,
)
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    PrivateWorkerSpec,
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.bootstrap import chat_tools_wiring

PRIMARY = "http://127.0.0.1:11434"
WORKER_A = "https://192.0.2.10:8443"
MOE_MODEL = "qwen3.5:35b-a3b"
PLAIN_MODEL = "gemma2:2b"


class _JsonResponse(io.BytesIO):
    def __init__(self, payload: dict) -> None:
        super().__init__(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _make_pool_transport(
    models_by_origin: dict[str, list[dict]],
    show_by_model: dict[str, dict | Exception | object],
):
    """Build a fake ``open_url`` recording SI ``/v1/models`` listing & detail calls."""
    calls: dict[str, list[tuple[str, str]]] = {"tags": [], "show": []}
    lock = threading.Lock()

    def fake_open_url(request, **kwargs):
        url = request.full_url
        origin = url.rsplit("/v1/", 1)[0]
        if url.endswith("/v1/sonder/health"):
            return _JsonResponse({"version": "0.12.6"})
        if url.endswith("/v1/models"):
            rows_raw = list(models_by_origin.get(origin, ()))
            is_show = float(kwargs.get("timeout", 0.0) or 0.0) >= 5.0
            if not is_show:
                with lock:
                    calls["tags"].append((origin, ""))
                return _JsonResponse({
                    "object": "list",
                    "data": [
                        {**row, "id": row.get("id") or row.get("name", "")}
                        for row in rows_raw
                    ],
                })
            enriched = []
            for row in rows_raw:
                model = str(row.get("name") or row.get("id") or "")
                with lock:
                    calls["show"].append((origin, model))
                outcome = show_by_model.get(model)
                if callable(outcome):
                    outcome = outcome(origin, model)
                if isinstance(outcome, BaseException):
                    raise outcome
                merged = {**row, "id": row.get("id") or model}
                if isinstance(outcome, dict):
                    merged.update(outcome)
                enriched.append(merged)
            return _JsonResponse({"object": "list", "data": enriched})
        raise AssertionError(f"unexpected URL {url}")

    return fake_open_url, calls


def _stub_permission_policy(mode: str = "auto") -> SimpleNamespace:
    return SimpleNamespace(
        current_mode=lambda: mode,
        approval_ledger=lambda: None,
    )


def _stub_handler() -> SimpleNamespace:
    return SimpleNamespace(
        _early_stream=SimpleNamespace(
            completion_id="iid-pool-tools",
            client_gone=False,
            lock=threading.Lock(),
        ),
    )


def _stub_server_for_turns(pool: object, *, gateway: object | None = None, serve_target=None) -> SimpleNamespace:
    attrs: dict[str, object] = {
        "OLLAMA_POOL": pool,
        "mcp": None,
        "_agent_dispatch": lambda *_a, **_k: "ok",
        "_AGENT_SYSTEM_OPERATOR_TOOLS": (),
        "_ACCOUNT_SCOPED_AGENT_BLOCKED_TOOLS": (),
        "served_work_project": lambda _p: "",
        "code_runner": SimpleNamespace(container_enabled=lambda: False),
        "_APP_GRAPH": SimpleNamespace(model_gateway=gateway) if gateway is not None else None,
    }
    if serve_target is not None:
        attrs["_serve_target"] = serve_target
    return SimpleNamespace(**attrs)


# ---------------------------------------------------------------------------
# 1. Unprobed pool snapshots + sonder_inference gateway tool_capable_models
# ---------------------------------------------------------------------------


def test_unprobed_pool_uses_gateway_tool_capable_models_and_normalizes_latest(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    monkeypatch.setenv("SONDER_CHAT_TOOLS", "1")
    empty_pool = SimpleNamespace(
        enabled=True,
        snapshots=lambda: (
            SimpleNamespace(healthy=True, supports_tools={}),
            SimpleNamespace(healthy=True, supports_tools={}),
        ),
    )
    gateway = SimpleNamespace(
        unified_worker_status=lambda **_kw: {
            "tool_capable_models": [MOE_MODEL, "sonder:latest"],
            "workers": [],
        },
    )
    server = _stub_server_for_turns(empty_pool, gateway=gateway)

    assert chat_tools_wiring._model_supports_tools(server, MOE_MODEL) is True
    assert chat_tools_wiring._model_supports_tools(server, "sonder") is True
    assert chat_tools_wiring._model_supports_tools(server, "SONDER:latest") is True
    assert chat_tools_wiring._model_supports_tools(server, "unknown:7b") is False

    turn = chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=_stub_permission_policy("auto"),
        handler=_stub_handler(),
        chunk_fn=lambda *_a, **_k: "",
        model=MOE_MODEL,
        project="",
        stream=False,
        authorized_tools=True,
    )
    assert turn is not None


def test_unprobed_pool_finds_gateway_inside_nested_providers_and_direct_attr(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    empty_pool = SimpleNamespace(
        enabled=True,
        snapshots=lambda: (SimpleNamespace(healthy=True, supports_tools={}),),
    )
    leaf_gw = SimpleNamespace(tool_capable_models=(MOE_MODEL,))
    wrapped_gw = SimpleNamespace(
        _providers={"sonder_inference": SimpleNamespace(_inner=leaf_gw)},
    )
    server = SimpleNamespace(
        OLLAMA_POOL=empty_pool,
        _APP_GRAPH=SimpleNamespace(model_gateway=wrapped_gw),
    )
    assert chat_tools_wiring._model_supports_tools(server, MOE_MODEL) is True


def test_gateway_explicit_false_stays_fail_closed(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    empty_pool = SimpleNamespace(
        enabled=True,
        snapshots=lambda: (SimpleNamespace(healthy=True, supports_tools={}),),
    )
    gateway = SimpleNamespace(
        unified_worker_status=lambda **_kw: {
            "tool_capable_models": [],
            "workers": [
                {"healthy": True, "supports_tools": {PLAIN_MODEL: False}},
            ],
        },
    )
    records_called = [False]

    def fail_if_called():
        records_called[0] = True
        return [(PLAIN_MODEL, {"capabilities": ["completion", "tools"]})]

    server = SimpleNamespace(
        OLLAMA_POOL=empty_pool,
        _APP_GRAPH=SimpleNamespace(model_gateway=gateway),
        _runtime_installed_model_records=fail_if_called,
        _runtime_model_has_capability=lambda *_a, **_k: True,
    )
    assert chat_tools_wiring._model_supports_tools(server, PLAIN_MODEL) is False
    assert records_called[0] is False


# ---------------------------------------------------------------------------
# 2. Explicit supports_tools=False stays fail-closed; unhealthy workers ignored
# ---------------------------------------------------------------------------


def test_explicit_false_in_pool_snapshot_stays_fail_closed(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    monkeypatch.setenv("SONDER_CHAT_TOOLS", "1")
    pool = SimpleNamespace(
        enabled=True,
        snapshots=lambda: (
            SimpleNamespace(healthy=True, supports_tools={"nomic-embed-text:latest": False}),
        ),
    )
    # Even if another surface claims tool support, an explicit False from a healthy pool worker stays fail-closed.
    gateway = SimpleNamespace(
        unified_worker_status=lambda **_kw: {
            "tool_capable_models": ["nomic-embed-text:latest"],
            "workers": [],
        },
    )
    server = _stub_server_for_turns(pool, gateway=gateway)

    assert chat_tools_wiring._model_supports_tools(server, "nomic-embed-text:latest") is False
    assert chat_tools_wiring._model_supports_tools(server, "nomic-embed-text") is False

    turn = chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=_stub_permission_policy("auto"),
        handler=_stub_handler(),
        chunk_fn=lambda *_a, **_k: "",
        model="nomic-embed-text",
        project="",
        stream=False,
        authorized_tools=True,
    )
    assert turn is None

    mcp_turn = chat_tools_wiring.maybe_open_mcp_chat_tool_turn(
        server=server,
        permission_policy=_stub_permission_policy("auto"),
        model="nomic-embed-text:latest",
        cloud=False,
        project="",
    )
    assert mcp_turn is None


def test_unhealthy_worker_neither_enables_nor_poisons_tool_support(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    # Unhealthy worker claiming True must not enable tools.
    pool_unhealthy_true = SimpleNamespace(
        enabled=True,
        snapshots=lambda: (
            SimpleNamespace(healthy=False, supports_tools={MOE_MODEL: True}),
            SimpleNamespace(healthy=True, supports_tools={}),
        ),
    )
    server_a = SimpleNamespace(OLLAMA_POOL=pool_unhealthy_true, _APP_GRAPH=None)
    assert chat_tools_wiring._model_supports_tools(server_a, MOE_MODEL) is False

    # Unhealthy worker claiming False must not block gateway/healthy worker.
    pool_unhealthy_false = SimpleNamespace(
        enabled=True,
        snapshots=lambda: (
            SimpleNamespace(healthy=False, supports_tools={MOE_MODEL: False}),
            SimpleNamespace(healthy=True, supports_tools={}),
        ),
    )
    gw = SimpleNamespace(
        unified_worker_status=lambda **_kw: {"tool_capable_models": [MOE_MODEL], "workers": []},
    )
    server_b = SimpleNamespace(
        OLLAMA_POOL=pool_unhealthy_false,
        _APP_GRAPH=SimpleNamespace(model_gateway=gw),
    )
    assert chat_tools_wiring._model_supports_tools(server_b, MOE_MODEL) is True


# ---------------------------------------------------------------------------
# 3. Cold-start OllamaWorkerPool bounded cached capability probe
# ---------------------------------------------------------------------------


def test_cold_start_pooled_model_probes_and_caches_supports_tools(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    monkeypatch.setenv("SONDER_CHAT_TOOLS", "1")
    fake_open, calls = _make_pool_transport(
        models_by_origin={
            PRIMARY: [{"name": "sonder:latest", "digest": "sha256:p1"}],
            WORKER_A: [
                {"name": MOE_MODEL, "digest": "sha256:w1"},
                {"name": PLAIN_MODEL, "digest": "sha256:w2"},
            ],
        },
        show_by_model={
            "sonder:latest": {"capabilities": ["completion", "tools"]},
            MOE_MODEL: {"capabilities": ["completion", "tools", "thinking"]},
            PLAIN_MODEL: {"capabilities": ["completion"]},
        },
    )
    prober = _default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake_open)
    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=prober,
    )
    assert pool.enabled is True
    # Before any probe, snapshots have empty supports_tools (the cold-start state).
    assert all(snap.supports_tools == {} for snap in pool.snapshots())

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:11437"),
        get_transport=lambda *_a, **_k: (200, b"{}"),
        transport=lambda *_a, **_k: {},
        ollama_pool=pool,
    )
    server = _stub_server_for_turns(pool, gateway=gw)

    # First turn: triggers bounded refresh + /api/show, returns True, enables ChatToolTurn!
    assert chat_tools_wiring._model_supports_tools(server, MOE_MODEL) is True
    prober.wait_show_fill(5.0)

    # Snapshots, pool_capacity_summary, and gateway unified_worker_status are now warm.
    assert pool.model_supports_tools(MOE_MODEL, probe=False) is True
    assert MOE_MODEL in pool.pool_capacity_summary()["tool_capable_models"]
    assert MOE_MODEL in gw.unified_worker_status()["tool_capable_models"]

    # Non-tool model is cached as explicit False (fail-closed).
    assert chat_tools_wiring._model_supports_tools(server, PLAIN_MODEL) is False
    assert pool.model_supports_tools(PLAIN_MODEL, probe=False) is False

    # Bare name matches ':latest' installed on PRIMARY.
    assert chat_tools_wiring._model_supports_tools(server, "sonder") is True

    # Follow-up checks perform zero additional /api/show or /api/tags calls.
    tags_after = len(calls["tags"])
    show_after = len(calls["show"])
    assert chat_tools_wiring._model_supports_tools(server, MOE_MODEL) is True
    assert chat_tools_wiring._model_supports_tools(server, PLAIN_MODEL) is False
    assert chat_tools_wiring._model_supports_tools(server, "sonder") is True
    assert len(calls["tags"]) == tags_after
    assert len(calls["show"]) == show_after

    # End-to-end HTTP ChatToolTurn succeeds for MOE_MODEL and returns None for PLAIN_MODEL.
    turn_ok = chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=_stub_permission_policy("auto"),
        handler=_stub_handler(),
        chunk_fn=lambda *_a, **_k: "",
        model=MOE_MODEL,
        project="",
        stream=False,
        authorized_tools=True,
    )
    assert turn_ok is not None
    turn_no = chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=_stub_permission_policy("auto"),
        handler=_stub_handler(),
        chunk_fn=lambda *_a, **_k: "",
        model=PLAIN_MODEL,
        project="",
        stream=False,
        authorized_tools=True,
    )
    assert turn_no is None


def test_background_show_fill_hydrates_pool_snapshots_without_ttl_wait():
    """Background /api/show completion immediately updates pool.snapshots()."""
    fake_open, _calls = _make_pool_transport(
        models_by_origin={
            PRIMARY: [{"name": "sonder:latest", "digest": "sha256:p1"}],
            WORKER_A: [
                {"name": MOE_MODEL, "digest": "sha256:w1"},
                {"name": PLAIN_MODEL, "digest": "sha256:w2"},
            ],
        },
        show_by_model={
            "sonder:latest": {"capabilities": ["completion", "tools"]},
            MOE_MODEL: {"capabilities": ["completion", "tools"]},
            PLAIN_MODEL: {"capabilities": ["completion"]},
        },
    )
    prober = _default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake_open)
    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=prober,
        capability_ttl_seconds=300.0,
    )
    pool.refresh_capabilities(force=True)
    prober.wait_show_fill(5.0)

    # Without a second refresh_capabilities() or waiting 300s, snapshots are hydrated!
    assert pool.model_supports_tools(MOE_MODEL, probe=False) is True
    assert pool.model_supports_tools("sonder", probe=False) is True
    assert pool.model_supports_tools(PLAIN_MODEL, probe=False) is False
    snap_by_origin = {snap.origin: snap for snap in pool.snapshots()}
    assert snap_by_origin[WORKER_A].supports_tools[MOE_MODEL] is True
    assert snap_by_origin[WORKER_A].supports_tools[PLAIN_MODEL] is False


def test_model_beyond_first_show_fill_batch_still_probes_on_demand(monkeypatch):
    """When a worker has >8 models, requesting model #12 probes it on demand."""
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    names = [f"m{i:02d}:latest" for i in range(ollama_pool._SHOW_FETCHES_PER_PROBE + 4)]
    target = names[-1]
    fake_open, calls = _make_pool_transport(
        models_by_origin={
            PRIMARY: [{"name": "base:latest", "digest": "sha256:base"}],
            WORKER_A: [{"name": n, "digest": "sha256:" + n} for n in names],
        },
        show_by_model={
            "base:latest": {"capabilities": ["completion"]},
            **{n: {"capabilities": ["completion", "tools"]} for n in names},
        },
    )
    prober = _default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake_open)
    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=prober,
    )
    pool.refresh_capabilities(force=True)
    prober.wait_show_fill(5.0)

    # Target was past the first _SHOW_FETCHES_PER_PROBE batch, so still unprobed.
    assert pool.model_supports_tools(target, probe=False) is None

    server = _stub_server_for_turns(pool)
    assert chat_tools_wiring._model_supports_tools(server, target) is True
    assert pool.model_supports_tools(target, probe=False) is True
    assert (WORKER_A, target) in calls["show"]


# ---------------------------------------------------------------------------
# 4. Bounded timeout, backoff, non-blocking warm-up, concurrency & GPU gate
# ---------------------------------------------------------------------------


def test_hanging_show_probe_is_bounded_and_cooled_down(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    release = threading.Event()
    show_attempts = [0]

    def hang_show(_origin, _model):
        show_attempts[0] += 1
        release.wait(5.0)
        return {"capabilities": ["completion", "tools"]}

    fake_open, _calls = _make_pool_transport(
        models_by_origin={
            PRIMARY: [{"name": "base:latest", "digest": "sha256:b"}],
            WORKER_A: [{"name": MOE_MODEL, "digest": "sha256:m"}],
        },
        show_by_model={
            "base:latest": {"capabilities": ["completion"]},
            MOE_MODEL: hang_show,
        },
    )
    prober = _default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake_open)
    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=prober,
        capability_probe_timeout_seconds=1.0,
    )
    # Pre-populate /api/tags without blocking on hang_show.
    pool.refresh_capabilities(force=True)
    # Wait briefly for background filler to enter hang_show or probe directly with a tight bound.
    try:
        started = time.monotonic()
        verdict = pool.model_supports_tools(MOE_MODEL, probe=True, probe_timeout=0.1)
        elapsed = time.monotonic() - started
        assert verdict is None
        assert elapsed < 0.6

        # Immediate retry hits cooldown and returns in <0.05s without another stall.
        started_retry = time.monotonic()
        verdict_retry = pool.model_supports_tools(MOE_MODEL, probe=True, probe_timeout=0.5)
        elapsed_retry = time.monotonic() - started_retry
        assert verdict_retry is None
        assert elapsed_retry < 0.1

        # Circuit breaker was NOT tripped by a slow /api/show probe.
        snap_by_origin = {snap.origin: snap for snap in pool.snapshots()}
        assert snap_by_origin[WORKER_A].healthy is True
        assert snap_by_origin[WORKER_A].consecutive_failures == 0
    finally:
        release.set()
        prober.wait_show_fill(5.0)


def test_non_blocking_probe_warms_snapshot_asynchronously():
    show_ready = threading.Event()

    def slow_show(_origin, _model):
        show_ready.wait(2.0)
        return {"capabilities": ["completion", "tools"]}

    fake_open, _calls = _make_pool_transport(
        models_by_origin={
            PRIMARY: [{"name": "base:latest", "digest": "sha256:b"}],
            WORKER_A: [{"name": MOE_MODEL, "digest": "sha256:m"}],
        },
        show_by_model={
            "base:latest": {"capabilities": ["completion"]},
            MOE_MODEL: slow_show,
        },
    )
    prober = _default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake_open)
    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=prober,
    )
    started = time.monotonic()
    assert pool.model_supports_tools(MOE_MODEL, probe=True, blocking=False) is None
    assert time.monotonic() - started < 0.1

    show_ready.set()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if pool.model_supports_tools(MOE_MODEL, probe=False) is True:
            break
        time.sleep(0.02)
    assert pool.model_supports_tools(MOE_MODEL, probe=False) is True
    prober.wait_show_fill(5.0)


def test_concurrent_cold_probes_coalesce_and_both_succeed():
    gate = threading.Event()
    entered = threading.Event()
    show_count = [0]
    count_lock = threading.Lock()

    def counted_show(_origin, _model):
        with count_lock:
            show_count[0] += 1
        entered.set()
        gate.wait(2.0)
        return {"capabilities": ["completion", "tools"]}

    fake_open, _calls = _make_pool_transport(
        models_by_origin={
            PRIMARY: [{"name": "base:latest", "digest": "sha256:b"}],
            WORKER_A: [{"name": MOE_MODEL, "digest": "sha256:m"}],
        },
        show_by_model={
            "base:latest": {"capabilities": ["completion"]},
            MOE_MODEL: counted_show,
        },
    )
    prober = _default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake_open)
    # Use a prober whose initial refresh already populates tags without starting background show fill
    # so we deterministically test two concurrent callers entering targeted show probe.
    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=lambda origin: {
            "protocol": "ollama-http-v1",
            "version": "0.12.6",
            "models": ["base:latest"] if origin == PRIMARY else [MOE_MODEL],
        },
    )
    pool.refresh_capabilities(force=True)
    # Swap in prober with targeted show after tags are cached.
    pool._capability_prober = prober
    pool._bind_capability_prober()

    results: list[bool | None] = []
    errors: list[BaseException] = []

    def worker_call():
        try:
            results.append(pool.model_supports_tools(MOE_MODEL, probe=True, probe_timeout=2.0))
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker_call)
    t2 = threading.Thread(target=worker_call)
    t1.start()
    assert entered.wait(2.0)
    t2.start()
    time.sleep(0.05)
    gate.set()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert not errors
    assert results == [True, True]
    assert show_count[0] == 1


def test_gpu_admission_probe_deferral_returns_none_without_faulting_worker():
    class _DeferringAdmission:
        mode = "enforce"

        def acquire_probe(self, _path: str, *, origin: str = ""):
            raise gpu_admission.GpuAdmissionRefused(
                gpu_admission.Refused(
                    reason=gpu_admission.RefusalReason.PROBE_BUSY,
                    detail="probe deferred",
                )
            )

    fake_open, calls = _make_pool_transport(
        models_by_origin={
            PRIMARY: [{"name": MOE_MODEL, "digest": "sha256:m"}],
            WORKER_A: [{"name": "other:latest", "digest": "sha256:o"}],
        },
        show_by_model={MOE_MODEL: {"capabilities": ["completion", "tools"]}},
    )
    prober = _default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake_open)
    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=lambda origin: {
            "protocol": "ollama-http-v1",
            "version": "0.12.6",
            "models": [MOE_MODEL] if origin == PRIMARY else ["other:latest"],
        },
    )
    pool.refresh_capabilities(force=True)
    pool._capability_prober = prober
    pool.gpu_admission = _DeferringAdmission()
    pool._bind_capability_prober()

    assert pool.model_supports_tools(MOE_MODEL, probe=True, probe_timeout=1.0) is None
    assert calls["show"] == []
    snap_primary = next(s for s in pool.snapshots() if s.origin == PRIMARY)
    assert snap_primary.healthy is True
    assert snap_primary.consecutive_failures == 0


# ---------------------------------------------------------------------------
# 5. Tier resolution, overrides, stub pools, single-host fallback & membership
# ---------------------------------------------------------------------------


def test_tier_alias_resolution_and_env_override(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    pool = SimpleNamespace(
        enabled=True,
        snapshots=lambda: (
            SimpleNamespace(healthy=True, supports_tools={MOE_MODEL: True}),
        ),
    )
    server = _stub_server_for_turns(
        pool,
        serve_target=lambda raw, _strict=False: (MOE_MODEL, False, True, "code") if raw == "code" else (None, False, False, None),
    )
    assert chat_tools_wiring._model_supports_tools(server, "code") is True

    # SONDER_CHAT_TOOL_MODELS override normalizes ':latest' and case.
    monkeypatch.setenv("SONDER_CHAT_TOOL_MODELS", "CustomTool:latest")
    empty_server = SimpleNamespace(
        OLLAMA_POOL=SimpleNamespace(enabled=True, snapshots=lambda: ()),
        _APP_GRAPH=None,
    )
    assert chat_tools_wiring._model_supports_tools(empty_server, "customtool") is True


def test_stub_pool_with_refresh_capabilities_populates_supports_tools(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    warmed = [False]

    def snapshots():
        if warmed[0]:
            return (SimpleNamespace(healthy=True, supports_tools={MOE_MODEL: True}),)
        return (SimpleNamespace(healthy=True, supports_tools={}),)

    def refresh():
        warmed[0] = True

    stub_pool = SimpleNamespace(
        enabled=True,
        snapshots=snapshots,
        refresh_capabilities=refresh,
    )
    server = SimpleNamespace(OLLAMA_POOL=stub_pool, _APP_GRAPH=None)
    assert chat_tools_wiring._model_supports_tools(server, MOE_MODEL) is True
    assert warmed[0] is True


def test_single_host_disabled_pool_does_not_probe_pool_and_uses_runtime_fallback(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    probe_called = [False]

    def fail_if_probed(origin: str):
        probe_called[0] = True
        raise AssertionError(f"should not probe disabled pool: {origin}")

    pool = OllamaWorkerPool(PRIMARY, capability_prober=fail_if_probed)
    assert pool.enabled is False

    server = SimpleNamespace(
        OLLAMA_POOL=pool,
        _APP_GRAPH=None,
        _runtime_installed_model_records=lambda: ((MOE_MODEL, {"capabilities": ["completion", "tools"]}),),
        _runtime_model_has_capability=lambda m, cap, recs: any(
            name == m and cap in rec.get("capabilities", ()) for name, rec in recs
        ),
    )
    assert chat_tools_wiring._model_supports_tools(server, MOE_MODEL) is True
    assert probe_called[0] is False


def test_probationary_membership_worker_is_not_probed_by_model_supports_tools():
    probed_origins: list[str] = []

    def tracking_prober(origin: str):
        probed_origins.append(origin)
        return {
            "protocol": "ollama-http-v1",
            "version": "0.12.6",
            "models": [MOE_MODEL],
            "supports_tools": {MOE_MODEL: True},
        }

    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=tracking_prober,
    )
    pool.configure_membership(
        cluster_id="cluster-1",
        issuer_id="issuer-1",
        clock=lambda: datetime.now(timezone.utc),
    )
    # WORKER_A is in membership probation; only static loopback PRIMARY may be probed.
    pool.model_supports_tools(MOE_MODEL, probe=True, probe_timeout=1.0)
    assert WORKER_A not in probed_origins


def test_failing_show_probe_backs_off_and_stays_fail_closed(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    fake_open, calls = _make_pool_transport(
        models_by_origin={
            PRIMARY: [{"name": "base:latest", "digest": "sha256:b"}],
            WORKER_A: [{"name": MOE_MODEL, "digest": "sha256:m"}],
        },
        show_by_model={
            "base:latest": {"capabilities": ["completion"]},
            MOE_MODEL: TimeoutError("timed out"),
        },
    )
    prober = _default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake_open)
    pool = OllamaWorkerPool(
        PRIMARY,
        (WORKER_A,),
        allow_remote=True,
        capability_prober=prober,
    )
    server = _stub_server_for_turns(pool)

    assert chat_tools_wiring._model_supports_tools(server, MOE_MODEL) is False
    prober.wait_show_fill(5.0)
    attempts_after_first = sum(1 for _o, m in calls["show"] if m == MOE_MODEL)
    assert attempts_after_first == 1

    # Backoff prevents hammering /api/show on subsequent turns.
    assert chat_tools_wiring._model_supports_tools(server, MOE_MODEL) is False
    assert sum(1 for _o, m in calls["show"] if m == MOE_MODEL) == attempts_after_first


def test_gateway_tool_support_skips_remote_worker_health_http_probes(monkeypatch, tmp_path):
    """_model_supports_tools must pass gateway_rows=[] so remote workers are never HTTP-probed."""
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    ca_file = tmp_path / "worker-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_TEST_WORKER_TOKEN", "x" * 32)

    health_calls: list[str] = []

    def fail_on_get(url: str, _headers: dict, _timeout: float):
        health_calls.append(url)
        raise AssertionError(f"unexpected synchronous gateway HTTP GET: {url}")

    gw = SonderInferenceGateway(
        SonderInferenceConfig(
            base_url="http://127.0.0.1:11437",
            allow_remote=True,
            workers=(
                PrivateWorkerSpec(
                    url="https://192.0.2.20:8443",
                    ca_bundle=str(ca_file),
                    token_env="SONDER_TEST_WORKER_TOKEN",
                ),
            ),
        ),
        get_transport=fail_on_get,
        transport=lambda *_a, **_k: {},
    )

    # Case 1: pool.enabled=True with unprobed snapshots -> zero remote worker health HTTP calls.
    enabled_pool = SimpleNamespace(
        enabled=True,
        has_capability_prober=False,
        snapshots=lambda: (SimpleNamespace(healthy=True, supports_tools={}),),
    )
    server_enabled = _stub_server_for_turns(enabled_pool, gateway=gw)
    assert chat_tools_wiring._model_supports_tools(server_enabled, MOE_MODEL) is False
    assert health_calls == []

    # Case 2: pool.enabled=False -> still zero remote worker health HTTP calls.
    disabled_pool = SimpleNamespace(
        enabled=False,
        snapshots=lambda: (SimpleNamespace(healthy=True, supports_tools={}),),
    )
    server_disabled = _stub_server_for_turns(disabled_pool, gateway=gw)
    assert chat_tools_wiring._model_supports_tools(server_disabled, MOE_MODEL) is False
    assert health_calls == []

