"""A model pin resolves against the pool's routable members, not one member.

``sonder(model_override=X)`` used to read ``_get("/api/tags")``, which is one
pooled request answered by whichever member the pool picked (in production the
remote worker), so a model installed only on the other member was refused.
"""
from __future__ import annotations

import inspect
from urllib.error import URLError

import pytest

import server
from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool
from sonder_runtime.adapters.inference.pool_model_catalog import (
    PinStatus,
    PoolCatalogCache,
    resolve_model_pin,
)
from sonder_runtime.domain.fanout_policy import nonchat_reason
from sonder_runtime.domain.model_catalog import catalog_records, resolve_record

CHAT = ["completion"]


def _rec(name, caps=CHAT):
    return {"name": name, "capabilities": list(caps)}


def _endpoint_over(catalog):
    return lambda selector: resolve_record(selector, catalog_records(catalog))


class _FakePool:
    def __init__(self, union=None, *, enabled=True, error=None, origins=("a", "b")):
        self.enabled = enabled
        self.origins = origins
        self._union, self._error = union, error
        self.calls = []

    def catalog_union(self, fetch, *, routable_only=False):
        self.calls.append(routable_only)
        if self._error is not None:
            raise self._error
        return self._union


class _ForbiddenPool:
    """Pool whose union must never be read (disabled or absent)."""

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.origins = ()

    def catalog_union(self, *_a, **_k):
        pytest.fail("the pool-wide union must not be read here")


def _resolve(selector, *, endpoint=None, pool=None, cache=None):
    return resolve_model_pin(
        selector,
        endpoint_record=endpoint or _endpoint_over({"models": []}),
        pool=pool,
        fetch_member_tags=lambda origin: pytest.fail("fetching is the pool's job"),
        nonchat_reason=nonchat_reason,
        cache=cache or PoolCatalogCache(),
    )


# ---- unit level -----------------------------------------------------------

def test_endpoint_hit_resolves_without_loading_the_union():
    pool = _FakePool({"models": []})
    got = _resolve("here:7b", endpoint=_endpoint_over({"models": [_rec("here:7b")]}), pool=pool)
    assert got.status is PinStatus.RESOLVED and got.ok
    assert got.source == "endpoint" and got.model == "here:7b"
    assert pool.calls == []


def test_endpoint_miss_falls_back_to_the_pool_union_with_canonical_name():
    pool = _FakePool({"models": [_rec("Remote-Only:7B")]})
    got = _resolve("remote-only:7b", pool=pool)
    assert got.status is PinStatus.RESOLVED
    assert got.source == "pool"
    assert got.model == "Remote-Only:7B", "canonical catalog spelling is returned"
    assert pool.calls == [True], "the pin path asks for routable members only"


@pytest.mark.parametrize("pool", [None, _ForbiddenPool(enabled=False)])
def test_no_enabled_pool_never_loads_a_union_and_keeps_the_endpoint_verdict(pool):
    assert _resolve("x:1", pool=pool).status is PinStatus.NOT_INSTALLED
    emb = _endpoint_over({"models": [_rec("e:1", ["embedding"])]})
    assert _resolve("e:1", endpoint=emb, pool=pool).status is PinStatus.NOT_CHAT_CAPABLE
    ok = _endpoint_over({"models": [_rec("ok:1")]})
    assert _resolve("ok:1", endpoint=ok, pool=pool).source == "endpoint"


def test_present_nowhere_is_not_installed():
    pool = _FakePool({"models": [_rec("other:1")]})
    got = _resolve("ghost:1", pool=pool)
    assert got.status is PinStatus.NOT_INSTALLED and not got.ok and got.model is None


def test_embedding_only_record_is_not_chat_capable_via_pool_and_endpoint():
    pool = _FakePool({"models": [_rec("embed:1", ["embedding"])]})
    assert _resolve("embed:1", pool=pool).status is PinStatus.NOT_CHAT_CAPABLE
    ep = _endpoint_over({"models": [_rec("embed:1", ["embedding"])]})
    assert _resolve("embed:1", endpoint=ep, pool=None).status is PinStatus.NOT_CHAT_CAPABLE
    # With an enabled pool whose union lacks the model (or cannot be loaded),
    # the endpoint's NOT_CHAT_CAPABLE verdict is kept.
    empty = _FakePool({"models": []})
    assert _resolve("embed:1", endpoint=ep, pool=empty).status is PinStatus.NOT_CHAT_CAPABLE
    down = _FakePool(error=URLError("down"))
    assert _resolve("embed:1", endpoint=ep, pool=down).status is PinStatus.NOT_CHAT_CAPABLE


def test_endpoint_nonchat_hit_falls_through_to_a_chat_record_in_the_pool():
    """Pins current behaviour: a non-chat endpoint record does not stop the pool step."""
    ep = _endpoint_over({"models": [_rec("m:1", ["embedding"])]})
    pool = _FakePool({"models": [_rec("m:1", CHAT)]})
    got = _resolve("m:1", endpoint=ep, pool=pool)
    assert got.status is PinStatus.RESOLVED and got.source == "pool"


@pytest.mark.parametrize("error", [URLError("down"), OSError("reset"), ValueError("bad json")])
def test_transport_failures_loading_the_union_are_catalog_unavailable(error):
    got = _resolve("x:1", pool=_FakePool(error=error))
    assert got.status is PinStatus.CATALOG_UNAVAILABLE and not got.ok


def test_programming_errors_loading_the_union_propagate():
    with pytest.raises(TypeError):
        _resolve("x:1", pool=_FakePool(error=TypeError("bug")))


def test_endpoint_lookup_raising_is_a_miss_and_the_pool_is_still_consulted():
    def broken(_selector):
        raise RuntimeError("tags unreachable")

    pool = _FakePool({"models": [_rec("w:1")]})
    got = _resolve("w:1", endpoint=broken, pool=pool)
    assert got.status is PinStatus.RESOLVED and got.source == "pool"
    assert _resolve("w:1", endpoint=broken, pool=None).status is PinStatus.NOT_INSTALLED


# ---- PoolCatalogCache -----------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _loader(payloads):
    calls = []

    def load():
        calls.append(1)
        item = payloads[min(len(calls), len(payloads)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    return load, calls


def test_cache_reuses_within_ttl_and_reloads_after():
    clock = _Clock()
    cache = PoolCatalogCache(ttl_seconds=10, clock=clock)
    load, calls = _loader([{"models": [_rec("a")]}, {"models": [_rec("b")]}])
    first, fresh = cache.get(("o",), load)
    assert fresh
    clock.now += 9.9
    again, fresh = cache.get(("o",), load)
    assert again is first and not fresh and len(calls) == 1
    clock.now += 0.2
    second, fresh = cache.get(("o",), load)
    assert fresh
    assert len(calls) == 2 and second["models"][0]["name"] == "b"


def test_cache_does_not_store_failed_or_empty_loads():
    cache = PoolCatalogCache(ttl_seconds=10, clock=_Clock())
    load, calls = _loader([URLError("x"), {"models": []}, {"models": [_rec("a")]}])
    with pytest.raises(URLError):
        cache.get(("o",), load)
    assert cache.get(("o",), load)[0] == {"models": []}
    assert len(calls) == 2, "the failure was not cached"
    assert cache.get(("o",), load)[0]["models"]
    assert len(calls) == 3, "the empty union was not cached"
    assert cache.get(("o",), load)[0]["models"] and len(calls) == 3, "a good union is cached"


def test_cache_keys_by_origins_and_clear_empties_it():
    cache = PoolCatalogCache(ttl_seconds=10, clock=_Clock())
    one, _ = cache.get(("a", "b"), lambda: {"models": [_rec("one")]})
    two, _ = cache.get(("a", "c"), lambda: {"models": [_rec("two")]})
    assert one["models"][0]["name"] == "one" and two["models"][0]["name"] == "two"
    assert cache.get(("a", "b"), lambda: pytest.fail("cached"))[0]["models"][0]["name"] == "one"
    cache.clear()
    fresh, _ = cache.get(("a", "b"), lambda: {"models": [_rec("fresh")]})
    assert fresh["models"][0]["name"] == "fresh"


# ---- selector normalisation ----------------------------------------------

def test_selector_is_case_insensitive_and_exact_on_the_tag_in_both_paths():
    catalog = {"models": [_rec("Model-X:7B"), _rec("tagless:latest")]}
    ep = _endpoint_over(catalog)
    pool = _FakePool(catalog)
    for selector in ("model-x:7b", "MODEL-X:7B", "  Model-X:7B  "):
        via_endpoint = _resolve(selector, endpoint=ep, pool=None)
        via_pool = _resolve(selector, pool=pool)
        assert via_endpoint.model == via_pool.model == "Model-X:7B"
        assert (via_endpoint.source, via_pool.source) == ("endpoint", "pool")
    # No tag is inferred except ``:latest``: a bare name whose only record has
    # another tag stays unresolved. The pool path behaves identically.
    assert _resolve("model-x", endpoint=ep, pool=None).status is PinStatus.NOT_INSTALLED
    assert _resolve("model-x", pool=pool).status is PinStatus.NOT_INSTALLED
    assert _resolve("tagless:latest", pool=pool).status is PinStatus.RESOLVED
    assert _resolve("", pool=pool).status is PinStatus.NOT_INSTALLED


def test_untagged_selector_resolves_to_its_latest_record_in_both_paths():
    catalog = {"models": [_rec("Tagless:latest"), _rec("Model-X:7B")]}
    ep = _endpoint_over(catalog)
    pool = _FakePool(catalog)
    for selector in ("tagless", "TAGLESS", "  Tagless  "):
        via_endpoint = _resolve(selector, endpoint=ep, pool=None)
        via_pool = _resolve(selector, pool=pool)
        # The catalog's own name comes back, never the selector as typed.
        assert via_endpoint.model == via_pool.model == "Tagless:latest"
        assert (via_endpoint.source, via_pool.source) == ("endpoint", "pool")


def test_untagged_selector_prefers_an_exact_record_over_latest():
    for rows in ([_rec("both"), _rec("both:latest")], [_rec("both:latest"), _rec("both")]):
        records = catalog_records({"models": rows})
        assert resolve_record("both", records)[0] == "both"
        assert resolve_record("both:latest", records)[0] == "both:latest"


@pytest.mark.parametrize("selector, names, expected", [
    # A tagged selector never matches an untagged record, or another tag.
    ("bare:latest", ["bare"], None),
    ("bare:7b", ["bare:latest"], None),
    ("bare:", ["bare:latest"], None),
    ("bare:7b", ["bare:7b:latest"], None),
    ("bare@sha256:0123", ["bare@sha256:0123:latest", "bare:latest"], None),
    # Only the model part decides "untagged": a registry port is not a tag.
    ("reg.example:5000/team/model", ["reg.example:5000/team/model:latest"],
     "reg.example:5000/team/model:latest"),
    ("hf.co/owner/repo", ["hf.co/owner/repo:latest"], "hf.co/owner/repo:latest"),
    ("hf.co/owner/repo", ["hf.co/owner/repo:Q8_0"], None),
    # No prefix, suffix or partial matching rides in with the alias.
    ("bar", ["bare:latest", "foobar:latest", "ns/bar:latest"], None),
    ("ns/bar", ["bar:latest"], None),
    (":latest", ["x:latest"], None),
])
def test_latest_alias_edges(selector, names, expected):
    found = resolve_record(selector, catalog_records({"models": [_rec(n) for n in names]}))
    assert (found[0] if found else None) == expected


# ---- server level ---------------------------------------------------------

LOCAL = "http://127.0.0.1:11434"
WORKER = "https://worker.example:8443"
LOCAL_ONLY = "local-only:7b"
WORKER_ONLY = "worker-only:7b"
TAGS = {
    LOCAL: {"models": [_rec("shared:1"), _rec(LOCAL_ONLY), _rec("embed-local:1", ["embedding"]),
                       _rec("local-latest:latest")]},
    WORKER: {"models": [_rec("shared:1"), _rec(WORKER_ONLY), _rec("worker-latest:latest")]},
}
REFUSAL = "model pin '%s' is unavailable or is not chat-capable."


class _Conn:
    def close(self):
        pass


class _Rig:
    pass


@pytest.fixture
def rig(monkeypatch):
    pool = OllamaWorkerPool(
        LOCAL, (WORKER,), allow_remote=True,
        capability_prober=lambda origin: {"models": [r["name"] for r in TAGS[origin]["models"]]},
    )
    pool.refresh_capabilities()
    r = _Rig()
    r.pool, r.member_reads, r.chats, r.fail, r.timeouts = pool, [], [], set(), []

    r.pooled_read_answered_by = WORKER  # production: the worker answers /api/tags

    def get(path):
        return TAGS[r.pooled_read_answered_by]

    r.payloads = {}

    def member_tags(origin, timeout=15):
        r.member_reads.append(origin)
        if origin in r.payloads:
            return r.payloads[origin]
        r.timeouts.append(timeout)
        if origin in r.fail:
            raise URLError("member down")
        return TAGS[origin]

    def answer(_conn, _prompt, model, *_a, **_k):
        origin = pool.request(lambda o: o, model=model, idempotent=True)
        r.chats.append((model, origin))
        return "answered by %s" % origin, None, {}

    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_POOL_PIN_CATALOG", PoolCatalogCache())
    monkeypatch.setattr(server, "_get", get)
    monkeypatch.setattr(server, "_pool_member_tags", member_tags)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_route_chat_web", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_serve_target",
                        lambda _t, _s: ("code-default:7b", False, True, "code"))
    monkeypatch.setattr(server, "_internal_generate_for_route", lambda *_a: None)
    monkeypatch.setattr(server, "_build_system", lambda *_a, **_k: "system")
    monkeypatch.setattr(server, "_open_db", lambda: _Conn())
    monkeypatch.setattr(server, "_answer", answer)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    return r


def _pin(model):
    return server._sonder_impl_serialized(
        "hello", session="none", project="none", tier="code", model_override=model,
    )


def test_local_only_model_resolves_and_chat_goes_to_the_local_member(rig):
    """THE PRODUCTION CASE: the pooled /api/tags read is the worker's catalog."""
    assert _pin(LOCAL_ONLY) == "answered by %s" % LOCAL
    assert rig.chats == [(LOCAL_ONLY, LOCAL)]


def test_worker_only_model_resolves_and_chat_goes_to_the_worker(rig):
    assert _pin(WORKER_ONLY) == "answered by %s" % WORKER
    assert rig.chats == [(WORKER_ONLY, WORKER)]


def test_worker_only_model_resolves_when_the_local_member_answers_the_pooled_read(rig):
    """Mirror of the production case: the pooled catalog read hit the local member."""
    rig.pooled_read_answered_by = LOCAL
    assert _pin(WORKER_ONLY) == "answered by %s" % WORKER
    assert rig.chats == [(WORKER_ONLY, WORKER)]


def test_bare_pin_reaches_the_latest_record_on_either_member(rig):
    """A pin typed without a tag is sent as the catalog's ``:latest`` name."""
    assert _pin("local-latest") == "answered by %s" % LOCAL
    assert _pin("Worker-Latest") == "answered by %s" % WORKER
    assert rig.chats == [("local-latest:latest", LOCAL), ("worker-latest:latest", WORKER)]
    assert _pin("shared") == REFUSAL % "shared"  # only ``shared:1`` exists: no tag is guessed


def test_model_on_neither_member_returns_the_unchanged_refusal_and_sends_no_chat(rig):
    assert _pin("ghost:1") == REFUSAL % "ghost:1"
    assert rig.chats == []


def test_embedding_only_local_model_is_refused_with_the_same_text(rig):
    assert _pin("embed-local:1") == REFUSAL % "embed-local:1"
    assert rig.chats == []


def test_a_failed_worker_read_does_not_block_a_local_only_pin(rig):
    """catalog_union skips an unreachable member, so the primary's models still resolve."""
    rig.fail.add(WORKER)
    assert _pin(LOCAL_ONLY) == "answered by %s" % LOCAL
    assert WORKER in rig.member_reads, "the worker was attempted and tolerated"


def test_every_member_failing_refuses_the_pin(rig):
    rig.fail.update({LOCAL, WORKER})
    assert _pin(LOCAL_ONLY) == REFUSAL % LOCAL_ONLY
    assert rig.chats == []


def test_endpoint_hit_never_reads_member_catalogs(rig):
    assert _pin("shared:1").startswith("answered by")
    assert rig.member_reads == []


def test_a_member_in_cooldown_is_not_consulted_for_a_pin(rig):
    # The pooled read answers with the worker's catalog (lacks LOCAL_ONLY); the
    # only holder is the local member, which is in cooldown, so the pin is refused.
    local = next(s for s in rig.pool._states if s.endpoint.origin == LOCAL)
    local.cooldown_until = rig.pool._clock() + 1000.0
    assert _pin(LOCAL_ONLY) == REFUSAL % LOCAL_ONLY
    assert LOCAL not in rig.member_reads
    assert rig.chats == []


def test_pool_disabled_output_is_unchanged_and_the_union_is_never_read(rig, monkeypatch):
    single = OllamaWorkerPool(LOCAL)
    assert not single.enabled
    monkeypatch.setattr(server, "OLLAMA_POOL", single)
    monkeypatch.setattr(server, "_get", lambda path: TAGS[LOCAL])
    monkeypatch.setattr(single, "catalog_union",
                        lambda *a, **k: pytest.fail("union read with pool disabled"))
    monkeypatch.setattr(server, "_answer",
                        lambda _c, _p, model, *_a, **_k: ("answered %s" % model, None, {}))
    assert _pin(LOCAL_ONLY) == "answered %s" % LOCAL_ONLY
    assert _pin(WORKER_ONLY) == REFUSAL % WORKER_ONLY
    assert rig.member_reads == []


def test_pool_member_tags_default_timeout_is_still_15_and_pin_path_uses_its_own(rig):
    assert inspect.signature(server._pool_member_tags).parameters["timeout"].default == 15
    assert _pin(LOCAL_ONLY).startswith("answered by")
    assert rig.timeouts and set(rig.timeouts) == {server._PIN_CATALOG_TIMEOUT_SECONDS}
    assert server._PIN_CATALOG_TIMEOUT_SECONDS < 15


def test_real_pool_member_tags_forwards_its_timeout_to_open_url(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def open_url(request, *, timeout, allow_remote=None):
        seen.update(url=request.full_url, timeout=timeout)
        return _Resp()

    pool = OllamaWorkerPool(LOCAL)
    monkeypatch.setattr(pool, "open_url", open_url)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_read_ollama_response_bytes", lambda _r: b'{"models": []}')
    assert server._pool_member_tags(LOCAL) == {"models": []}
    assert seen == {"url": LOCAL + "/api/tags", "timeout": 15}
    server._pool_member_tags(LOCAL, timeout=3)
    assert seen["timeout"] == 3


def test_serve_target_is_not_widened_to_the_pool_union(monkeypatch):
    """Deliberate scope limit: only the model_override pin consults the union."""
    pool = OllamaWorkerPool(LOCAL, (WORKER,), allow_remote=True)
    reads = []
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_POOL_PIN_CATALOG", PoolCatalogCache())
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_get", lambda path: TAGS[LOCAL])
    monkeypatch.setattr(
        server, "_pool_member_tags",
        lambda origin, timeout=15: reads.append(origin) or TAGS[origin],
    )
    assert server._serve_target(LOCAL_ONLY, None)[0] == LOCAL_ONLY  # endpoint hit still works
    model, _cloud, _augment, label = server._serve_target(WORKER_ONLY, None)
    assert (model, label) == (None, None), "worker-only names stay unknown to _serve_target"
    assert reads == []


# ---- review fixes: malformed catalogs, stale-cache refusals ----------------

@pytest.mark.parametrize("bad", [["x"], "x", {"models": "x"}])
def test_malformed_worker_catalog_still_resolves_a_local_only_pin(rig, bad):
    rig.payloads[WORKER] = bad
    assert _pin(LOCAL_ONLY) == "answered by %s" % LOCAL
    assert rig.chats == [(LOCAL_ONLY, LOCAL)]


@pytest.mark.parametrize("bad", [["x"], "x", {"models": "x"}])
def test_malformed_catalog_on_every_member_returns_the_unchanged_refusal(rig, bad):
    rig.payloads.update({LOCAL: bad, WORKER: bad})
    assert _pin(LOCAL_ONLY) == REFUSAL % LOCAL_ONLY
    assert rig.chats == []


class _SeqPool(_FakePool):
    """FakePool whose union/error can be changed between calls."""


def _warm(cache, pool, selector="warm:1"):
    pool._union = {"models": [_rec(selector)]}
    got = _resolve(selector, pool=pool, cache=cache)
    assert got.status is PinStatus.RESOLVED
    return got


def test_stale_cached_union_without_the_model_is_reloaded_once_and_resolves():
    cache, pool = PoolCatalogCache(ttl_seconds=100, clock=_Clock()), _SeqPool()
    _warm(cache, pool)
    assert pool.calls == [True]
    pool._union = {"models": [_rec("warm:1"), _rec("recovered:1")]}
    got = _resolve("recovered:1", pool=pool, cache=cache)
    assert got.status is PinStatus.RESOLVED and got.source == "pool"
    assert len(pool.calls) == 2, "exactly one reload"


def test_cached_refusal_reloads_exactly_once_per_resolution_then_refuses():
    cache, pool = PoolCatalogCache(ttl_seconds=100, clock=_Clock()), _SeqPool()
    _warm(cache, pool)
    before = len(pool.calls)
    assert _resolve("ghost:1", pool=pool, cache=cache).status is PinStatus.NOT_INSTALLED
    assert len(pool.calls) == before + 1


def test_fresh_refusal_does_not_reload_a_second_time():
    cache, pool = PoolCatalogCache(ttl_seconds=100, clock=_Clock()), _SeqPool({"models": [_rec("a:1")]})
    assert _resolve("ghost:1", pool=pool, cache=cache).status is PinStatus.NOT_INSTALLED
    assert len(pool.calls) == 1


def test_resolved_from_cache_does_not_reload():
    cache, pool = PoolCatalogCache(ttl_seconds=100, clock=_Clock()), _SeqPool()
    _warm(cache, pool)
    assert _resolve("warm:1", pool=pool, cache=cache).status is PinStatus.RESOLVED
    assert len(pool.calls) == 1


def test_stale_nonchat_cached_record_is_reloaded_and_can_resolve():
    cache, pool = PoolCatalogCache(ttl_seconds=100, clock=_Clock()), _SeqPool()
    pool._union = {"models": [_rec("m:1", ["embedding"])]}
    assert _resolve("m:1", pool=pool, cache=cache).status is PinStatus.NOT_CHAT_CAPABLE
    pool._union = {"models": [_rec("m:1", CHAT)]}
    assert _resolve("m:1", pool=pool, cache=cache).status is PinStatus.RESOLVED


def test_reload_transport_failure_after_clean_miss_is_catalog_unavailable():
    cache, pool = PoolCatalogCache(ttl_seconds=100, clock=_Clock()), _SeqPool()
    _warm(cache, pool)
    pool._error = URLError("down")
    assert _resolve("ghost:1", pool=pool, cache=cache).status is PinStatus.CATALOG_UNAVAILABLE


def test_reload_transport_failure_after_endpoint_nonchat_keeps_not_chat_capable():
    cache, pool = PoolCatalogCache(ttl_seconds=100, clock=_Clock()), _SeqPool()
    _warm(cache, pool)
    pool._error = URLError("down")
    ep = _endpoint_over({"models": [_rec("e:1", ["embedding"])]})
    got = _resolve("e:1", endpoint=ep, pool=pool, cache=cache)
    assert got.status is PinStatus.NOT_CHAT_CAPABLE


def test_refresh_stores_nonempty_results_and_skips_empty_ones():
    cache = PoolCatalogCache(ttl_seconds=10, clock=_Clock())
    assert cache.refresh(("k",), lambda: {"models": []}) == {"models": []}
    assert cache.get(("k",), lambda: {"models": [_rec("a")]})[1] is True, "empty was not cached"
    assert cache.refresh(("k",), lambda: {"models": [_rec("b")]})["models"][0]["name"] == "b"
    union, fresh = cache.get(("k",), lambda: pytest.fail("cached"))
    assert not fresh and union["models"][0]["name"] == "b"


def test_refresh_prunes_expired_entries_so_distinct_keys_stay_bounded():
    clock = _Clock()
    cache = PoolCatalogCache(ttl_seconds=10, clock=clock)
    for i in range(200):
        cache.refresh(("origin", i), lambda: {"models": [_rec("a")]})
        clock.now += 11
    assert len(cache._entries) <= 2
    for i in range(5):  # unexpired distinct keys all stay
        cache.refresh(("live", i), lambda: {"models": [_rec("a")]})
    assert len(cache._entries) >= 5


def test_member_recovered_from_cooldown_within_the_ttl_is_pinned_without_waiting(rig):
    """Server level, real pool: a refusal must not outlive the cooldown that caused it."""
    local = next(s for s in rig.pool._states if s.endpoint.origin == LOCAL)
    local.cooldown_until = rig.pool._clock() + 1000.0
    assert _pin(LOCAL_ONLY) == REFUSAL % LOCAL_ONLY
    assert LOCAL not in rig.member_reads
    local.cooldown_until = 0.0
    assert _pin(LOCAL_ONLY) == "answered by %s" % LOCAL
    assert rig.chats == [(LOCAL_ONLY, LOCAL)]
    assert LOCAL in rig.member_reads


@pytest.mark.parametrize("bad_name", [["x"], 5, {"k": "v"}])
def test_non_string_row_names_from_a_worker_never_escape_the_pin_path(rig, bad_name):
    rig.payloads[WORKER] = {"models": [{"name": bad_name}]}
    assert _pin("ghost:1") == REFUSAL % "ghost:1"
    assert rig.chats == []
    assert _pin(LOCAL_ONLY) == "answered by %s" % LOCAL
    assert rig.chats == [(LOCAL_ONLY, LOCAL)]


# ---- text lanes resolve by name against the pool (chat_target) -------------
#
# ``chat_target`` resolves an unknown name again with ``chat_only=True``, and
# that call falls back to the pool-wide union.  The default ``_serve_target``
# call (lanes that run or apply output) is never widened.

from types import SimpleNamespace  # noqa: E402

from sonder_runtime.adapters import tool_lane_guard as guard  # noqa: E402
from sonder_runtime.adapters.model_bootstrap import LegacyModelBootstrapAdapter  # noqa: E402
from sonder_runtime.adapters.model_transport import ModelCallError  # noqa: E402
from tests.support.tool_lane import BLOCKED, DENIED, DENIED_SMALL  # noqa: E402

WORKER_EMBED = "embed-worker:1"
LOCAL_DENIED, WORKER_DENIED = DENIED, DENIED_SMALL  # both on the default deny list
TEXT_TAGS = {
    LOCAL: {"models": [_rec("shared:1"), _rec(LOCAL_ONLY), _rec(LOCAL_DENIED)]},
    WORKER: {"models": [_rec("shared:1"), _rec("Worker-Only:7B"), _rec(WORKER_DENIED),
                        _rec(WORKER_EMBED, ["embedding"])]},
}


@pytest.fixture
def text_rig(monkeypatch, tmp_path):
    """Real ``_serve_target`` and pool; the pooled /api/tags read is this host's catalog."""
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setattr(server, "TIERS", dict(server.TIERS))
    monkeypatch.setattr(server, "_RUNTIME_POLICY", server._RUNTIME_POLICY)
    server._refresh_runtime_policy(create=False)
    pool = OllamaWorkerPool(
        LOCAL, (WORKER,), allow_remote=True,
        capability_prober=lambda origin: {"models": [r["name"] for r in TEXT_TAGS[origin]["models"]]},
    )
    pool.refresh_capabilities()
    r = SimpleNamespace(pool=pool, reads=[], asked=[])
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_POOL_PIN_CATALOG", PoolCatalogCache())
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_get", lambda path: TEXT_TAGS[LOCAL])
    monkeypatch.setattr(
        server, "_pool_member_tags",
        lambda origin, timeout=15: r.reads.append(origin) or TEXT_TAGS[origin],
    )

    def make(tier, model, *_a, **_k):
        def generate(prompt, history=None, **_kw):
            origin = pool.request(lambda o: o, model=model, idempotent=True)
            r.asked.append((model, origin))
            return "answer from %s" % model

        return generate

    monkeypatch.setattr(server, "_make_tier_generate", make)
    monkeypatch.setattr(server, "_post", lambda *a, **k: {})
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 4096)
    return r


def test_ensemble_targets_return_a_local_only_and_a_worker_only_member(text_rig):
    """THE LIVE DEFECT: the worker-only name was reported unknown."""
    targets, unknown = server._ensemble_targets("%s,worker-only:7b" % LOCAL_ONLY)
    assert unknown == []
    assert targets == [(LOCAL_ONLY, LOCAL_ONLY), ("worker-only:7b", "Worker-Only:7B")]


def test_ensemble_answer_asks_both_members_each_on_its_own_host(text_rig):
    out = server.ensemble_answer("q", tiers="%s,WORKER-ONLY:7B" % LOCAL_ONLY)
    assert text_rig.asked[:2] == [(LOCAL_ONLY, LOCAL), ("Worker-Only:7B", WORKER)]
    assert "2 models answered" in out


def test_worker_only_model_resolves_in_a_text_lane_under_its_catalog_name(text_rig):
    assert guard.chat_target(server._serve_target, "WORKER-ONLY:7B", None) == (
        "Worker-Only:7B", False, False, "model:Worker-Only:7B")
    assert text_rig.reads, "an unknown name reads the pool union"


def test_lanes_that_run_output_do_not_gain_a_worker_only_name(text_rig):
    model, _cloud, _augment, label = server._serve_target("worker-only:7b", None)
    assert (model, label) == (None, None)
    target = LegacyModelBootstrapAdapter(SimpleNamespace(_serve_target=server._serve_target)) \
        .resolve_target("worker-only:7b")
    assert (target.model, target.tier_label) == (None, None)
    assert text_rig.reads == []


@pytest.mark.parametrize("name", [LOCAL_DENIED, WORKER_DENIED])
def test_a_denied_model_behaves_the_same_wherever_it_is_installed(text_rig, name):
    # Text lanes answer it by name...
    assert guard.chat_target(server._serve_target, name, None) == (name, False, False, "model:" + name)
    assert server._ensemble_targets(name) == ([(name, name)], [])
    # ...and every lane that runs output refuses it, by name, before resolving.
    assert guard.selector_refusal(name, server.TIERS, server._RUNTIME_POLICY) == BLOCKED % name
    assert guard.denied(name, "model:" + name, server._RUNTIME_POLICY)  # the chat code gate
    assert server._serve_target(name, None)[0] is None
    gate = server._gate_answer_code(
        "```python\nprint(1)\n```", iid=None, prompt="p", history=[], model=name, system="",
        temperature=0.2, num_predict=64, num_ctx=0, cloud=False, tier_label="model:" + name)
    assert gate[1:] == (None, False, {}), "a blocked model's reply is never run"


def test_a_name_installed_nowhere_stays_unknown_with_unchanged_errors(text_rig):
    assert guard.chat_target(server._serve_target, "ghost:1", None)[3] is None
    assert server._ensemble_targets("ghost:1") == ([], ["ghost:1"])
    assert server.ensemble_answer("q", tiers="ghost:1") == \
        "ERROR: no bound local tiers to poll (unknown: ghost:1)."
    with pytest.raises(ModelCallError) as raised:
        server.structured_answer_with_history("q", [], {"type": "object"}, tier="ghost:1")
    assert raised.value.detail == "unknown model 'ghost:1'. Valid: sonder, %s." % server._valid_tier_names()
    assert text_rig.asked == []


def test_an_embedding_only_worker_model_is_still_not_a_chat_target(text_rig):
    assert guard.chat_target(server._serve_target, WORKER_EMBED, None)[3] is None
    assert server._ensemble_targets(WORKER_EMBED) == ([], [WORKER_EMBED])
    assert text_rig.reads, "the union was consulted and its record refused"


def test_a_failing_pool_lookup_leaves_a_text_lane_name_unknown(text_rig, monkeypatch):
    """Like the endpoint read beside it: an error is a miss, never an exception."""
    def boom(_selector):
        raise RuntimeError("pool lookup broke")

    monkeypatch.setattr(server, "_resolve_model_pin", boom)
    assert server._serve_target(WORKER_ONLY, None, chat_only=True)[3] is None
