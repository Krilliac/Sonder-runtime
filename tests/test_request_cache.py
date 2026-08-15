"""Deterministic request/result cache for normal chat generation.

The cache may serve a stored response only when the whole turn is a pure
function of its bound identity: a local (never cloud) target, greedy decoding
(temperature 0), the non-learning non-augmented route, a request-owner scope,
and no streaming/structured/control surface.  Everything else is denied by
default.  Prompts and answers must never appear in metrics, receipts, or
stats: only bounded "hit"/"miss" style results are observable.
"""

from contextlib import contextmanager
import http.client
import json
import threading

import pytest

import request_cache
import server
import sonder_metrics
import sonder_serve as ts


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv("SONDER_REQUEST_CACHE", raising=False)
    monkeypatch.delenv("SONDER_REQUEST_CACHE_TTL", raising=False)
    monkeypatch.delenv("SONDER_REQUEST_CACHE_MAX", raising=False)
    monkeypatch.delenv("SONDER_SERVE_TEMPERATURE", raising=False)
    request_cache.reset_for_tests()
    yield
    request_cache.reset_for_tests()


def _eligible_kwargs(**overrides):
    kwargs = {
        "scope": "qc-owner",
        "cloud": False,
        "temperature": 0.0,
        "learning": False,
        "augmented": False,
        "remote_endpoint": False,
    }
    kwargs.update(overrides)
    return kwargs


def _key_kwargs(**overrides):
    kwargs = {
        "scope": "qc-owner",
        "model": "sonder:latest",
        "model_revision": "sha256:stable-revision",
        "tier": "general",
        "system": "system prompt",
        "prompt": "hello",
        "history": [{"role": "user", "content": "earlier"}],
        "options": {"temperature": 0.0, "num_predict": 1024, "num_ctx": 8192},
        "cloud_allowed": False,
    }
    kwargs.update(overrides)
    return kwargs


# --- module: enablement and eligibility -----------------------------------


def test_cache_is_opt_in_and_disabled_by_default():
    assert request_cache.enabled() is False
    assert request_cache.eligible(**_eligible_kwargs()) is False


def test_eligibility_denies_each_axis_independently(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    denied = (
        {"scope": ""},
        {"scope": None},
        {"cloud": True},
        {"temperature": 0.2},
        {"temperature": None},
        {"temperature": "hot"},
        {"learning": True},
        {"augmented": True},
        {"streaming": True},
        {"structured": True},
        {"remote_endpoint": True},
    )
    for overrides in denied:
        assert request_cache.eligible(**_eligible_kwargs(**overrides)) is False, overrides


def test_eligibility_default_denies_unasserted_endpoint_locality(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    kwargs = _eligible_kwargs()
    del kwargs["remote_endpoint"]
    # A caller that has not proven the serving endpoint is loopback is denied:
    # the axis defaults to remote, never to local.
    assert request_cache.eligible(**kwargs) is False


def test_eligibility_allows_deterministic_local_scoped_turn(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    assert request_cache.eligible(**_eligible_kwargs()) is True


# --- module: identity binding ---------------------------------------------


def test_identity_key_is_stable_and_opaque():
    first = request_cache.identity_key(**_key_kwargs())
    second = request_cache.identity_key(**_key_kwargs())
    assert first == second
    # Opaque digest: no prompt/system/model material leaks into the key.
    assert "hello" not in first
    assert "system prompt" not in first
    assert len(first) == 64


def test_identity_key_binds_every_component():
    base = request_cache.identity_key(**_key_kwargs())
    variants = (
        {"scope": "qc-other-owner"},
        {"model": "other:latest"},
        {"model_revision": "sha256:replacement-revision"},
        {"tier": "code"},
        {"system": "different system"},
        {"prompt": "hello there"},
        {"history": [{"role": "user", "content": "changed"}]},
        {"history": []},
        {"options": {"temperature": 0.0, "num_predict": 1024, "num_ctx": 4096}},
        {"cloud_allowed": True},
    )
    seen = {base}
    for overrides in variants:
        key = request_cache.identity_key(**_key_kwargs(**overrides))
        assert key not in seen, overrides
        seen.add(key)


# --- module: storage, TTL, capacity ---------------------------------------


def test_get_and_put_round_trip(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    key = request_cache.identity_key(**_key_kwargs())
    assert request_cache.get(key) is None
    assert request_cache.put(key, "the answer") is True
    assert request_cache.get(key) == "the answer"


def test_put_refuses_empty_error_and_non_text():
    key = request_cache.identity_key(**_key_kwargs())
    assert request_cache.put(key, "") is False
    assert request_cache.put(key, "   ") is False
    assert request_cache.put(key, "ERROR: model unavailable") is False
    assert request_cache.put(key, None) is False
    assert request_cache.put("", "answer") is False
    assert request_cache.get(key) is None


def test_entries_expire_after_ttl(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE_TTL", "60")
    clock = {"now": 1000.0}
    monkeypatch.setattr(request_cache, "_now", lambda: clock["now"])
    key = request_cache.identity_key(**_key_kwargs())
    request_cache.put(key, "answer")
    clock["now"] = 1059.0
    assert request_cache.get(key) == "answer"
    clock["now"] = 1061.0
    assert request_cache.get(key) is None


def test_capacity_is_bounded_lru(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE_MAX", "2")
    keys = [
        request_cache.identity_key(**_key_kwargs(prompt="p%d" % i))
        for i in range(3)
    ]
    request_cache.put(keys[0], "a0")
    request_cache.put(keys[1], "a1")
    # Refresh keys[0] so keys[1] is the least recently used entry.
    assert request_cache.get(keys[0]) == "a0"
    request_cache.put(keys[2], "a2")
    assert request_cache.get(keys[1]) is None
    assert request_cache.get(keys[0]) == "a0"
    assert request_cache.get(keys[2]) == "a2"


def test_stats_are_bounded_counts_only():
    key = request_cache.identity_key(**_key_kwargs())
    request_cache.put(key, "answer")
    request_cache.get(key)
    request_cache.get("missing-key")
    stats = request_cache.stats()
    assert set(map(type, stats.values())) == {int}
    assert "answer" not in json.dumps(stats)
    assert stats["entries"] == 1
    assert stats["hit"] >= 1
    assert stats["miss"] >= 1


# --- server: temperature knob ---------------------------------------------


def test_serve_temperature_default_and_clamping(monkeypatch):
    monkeypatch.delenv("SONDER_SERVE_TEMPERATURE", raising=False)
    assert server._serve_temperature() == pytest.approx(0.2)
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    assert server._serve_temperature() == 0.0
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "9")
    assert server._serve_temperature() == 2.0
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "-1")
    assert server._serve_temperature() == 0.0
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "warm")
    assert server._serve_temperature() == pytest.approx(0.2)
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "nan")
    assert server._serve_temperature() == pytest.approx(0.2)


# --- server: generation wiring --------------------------------------------


class _Connection:
    def close(self):
        return None


def _install_generation_fakes(monkeypatch, *, model, cloud, tier_label,
                              learn=False, responses=None):
    calls = []
    supplied = list(responses or [])

    # Pin the serving endpoint to loopback so these tests are independent of
    # the machine's OLLAMA_HOST; the remote-endpoint tests override this.
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "control_command", lambda *a, **k: None)
    monkeypatch.setattr(server, "_web_denial_guard", lambda *a, **k: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target", lambda *_a: (model, cloud, False, tier_label)
    )
    monkeypatch.setattr(server, "_build_system", lambda *a, **k: "sys")
    monkeypatch.setattr(server, "_should_learn", lambda *_a: learn)
    monkeypatch.setattr(
        server, "_cache_model_revision", lambda _model: "sha256:test-revision",
    )
    monkeypatch.setattr(server, "_open_db", _Connection)
    monkeypatch.setattr(server, "_resolve_project", lambda *_a: None)
    monkeypatch.setattr(server, "_capture_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_apply_code_gate",
        lambda response, interaction_id=None, regenerate=None: (response, False, False),
    )

    def make_generate(selected_model, system, temperature, num_predict, num_ctx,
                      cloud=False, timeout=None, cancel_check=None,
                      allow_cloud_fallback=True, **_kwargs):
        def generate(prompt, history=None):
            calls.append({"model": selected_model, "temperature": temperature})
            outcome = supplied.pop(0) if supplied else "generated answer"
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        generate.last_usage = {}
        return generate

    monkeypatch.setattr(server, "_make_generate", make_generate)

    def fake_answer(conn, prompt, selected_model, effective_system, temperature,
                    num_predict, num_ctx, session_id, project, history,
                    trace=False, tier="sonder", cloud=False, augment=True,
                    allow_cloud_fallback=True):
        calls.append({"model": selected_model, "temperature": temperature,
                      "learning": True})
        return "learned answer", None, {}

    monkeypatch.setattr(server, "_answer", fake_answer)
    return calls


def test_deterministic_non_learning_turn_is_served_from_cache(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )
    statuses = []

    first = server._answer_with_history_impl(
        "hello", [], tier="general",
        cache_scope="qc-owner", cache_observer=statuses.append,
    )
    second = server._answer_with_history_impl(
        "hello", [], tier="general",
        cache_scope="qc-owner", cache_observer=statuses.append,
    )

    assert len(calls) == 1
    assert first == second
    assert "generated answer" in first
    assert statuses == ["miss", "hit"]


def test_cache_key_changes_when_the_resolved_tag_revision_changes(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )
    revision = {"value": "sha256:before-pull"}
    monkeypatch.setattr(server, "_cache_model_revision", lambda _model: revision["value"])
    statuses = []

    server._answer_with_history_impl(
        "hello", [], tier="general", cache_scope="qc-owner",
        cache_observer=statuses.append,
    )
    revision["value"] = "sha256:after-pull"
    server._answer_with_history_impl(
        "hello", [], tier="general", cache_scope="qc-owner",
        cache_observer=statuses.append,
    )

    assert len(calls) == 2
    assert statuses == ["miss", "miss"]


def test_missing_model_revision_denies_cache_admission(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )
    monkeypatch.setattr(server, "_cache_model_revision", lambda _model: "")

    server._answer_with_history_impl("hello", [], tier="general", cache_scope="qc-owner")
    server._answer_with_history_impl("hello", [], tier="general", cache_scope="qc-owner")

    assert len(calls) == 2
    assert request_cache.stats()["entries"] == 0


def test_cache_model_revision_reads_the_live_catalog_digest(monkeypatch):
    monkeypatch.setattr(
        server,
        "discovered_model_records",
        lambda: (("local:latest", {"digest": "sha256:catalog-revision"}),),
    )
    assert server._cache_model_revision("local") == "sha256:catalog-revision"
    assert server._cache_model_revision("missing:latest") == ""


def test_cache_binds_request_owner_scope(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )

    server._answer_with_history_impl(
        "hello", [], tier="general", cache_scope="qc-owner-a",
    )
    server._answer_with_history_impl(
        "hello", [], tier="general", cache_scope="qc-owner-b",
    )

    # Different principals never share entries, even for identical prompts.
    assert len(calls) == 2


def test_cloud_target_is_never_cached(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="big-cloud:latest", cloud=True,
        tier_label="cloud-general",
    )
    statuses = []

    for _ in range(2):
        server._answer_with_history_impl(
            "hello", [], tier="cloud-general",
            cache_scope="qc-owner", cache_observer=statuses.append,
        )

    assert len(calls) == 2
    assert statuses == []
    assert request_cache.stats()["entries"] == 0


def test_remote_ollama_endpoint_is_never_cached(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )
    # A configured remote Ollama endpoint: the target is still cloud=False,
    # but the docs promise local-only replay, so admission must deny.
    monkeypatch.setattr(server, "BASE", "http://192.168.1.50:11434")
    statuses = []

    for _ in range(2):
        server._answer_with_history_impl(
            "hello", [], tier="general",
            cache_scope="qc-owner", cache_observer=statuses.append,
        )

    # Never consulted, never stored: both turns generate fresh.
    assert len(calls) == 2
    assert statuses == []
    stats = request_cache.stats()
    assert stats["entries"] == 0
    assert stats["store"] == 0
    assert stats["hit"] == 0
    assert stats["miss"] == 0


def test_remote_hostname_endpoint_is_never_cached(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )
    monkeypatch.setattr(server, "BASE", "http://ollama.lan:11434")

    for _ in range(2):
        server._answer_with_history_impl(
            "hello", [], tier="general", cache_scope="qc-owner",
        )

    assert len(calls) == 2
    assert request_cache.stats()["entries"] == 0


def test_loopback_endpoint_deterministic_turn_still_caches(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )
    # Explicit positive control for the endpoint axis: loopback in both its
    # spellings keeps the deterministic scoped turn cacheable.
    for origin in ("http://127.0.0.1:11434", "http://localhost:11434"):
        request_cache.reset_for_tests()
        del calls[:]
        monkeypatch.setattr(server, "BASE", origin)
        statuses = []

        for _ in range(2):
            server._answer_with_history_impl(
                "hello", [], tier="general",
                cache_scope="qc-owner", cache_observer=statuses.append,
            )

        assert len(calls) == 1, origin
        assert statuses == ["miss", "hit"], origin


def test_learning_route_is_never_cached(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="sonder",
        learn=True,
    )
    monkeypatch.setattr(server.memory_store, "touch_session", lambda *a: None)
    monkeypatch.setattr(server.memory_store, "get_interaction", lambda *a: {})
    statuses = []

    for _ in range(2):
        server._answer_with_history_impl(
            "hello", [], tier="sonder",
            cache_scope="qc-owner", cache_observer=statuses.append,
        )

    assert len(calls) == 2
    assert all(call.get("learning") for call in calls)
    assert statuses == []
    assert request_cache.stats()["entries"] == 0


def test_default_sampling_temperature_is_never_cached(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    # No SONDER_SERVE_TEMPERATURE: generation samples at the 0.2 default and
    # replaying one sample is not equivalent to fresh generation.
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )
    statuses = []

    for _ in range(2):
        server._answer_with_history_impl(
            "hello", [], tier="general",
            cache_scope="qc-owner", cache_observer=statuses.append,
        )

    assert len(calls) == 2
    assert statuses == []
    assert request_cache.stats()["entries"] == 0


def test_missing_scope_is_never_cached(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
    )

    for _ in range(2):
        server._answer_with_history_impl("hello", [], tier="general")

    assert len(calls) == 2
    assert request_cache.stats()["entries"] == 0


def test_failed_generation_is_never_stored(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    calls = _install_generation_fakes(
        monkeypatch, model="local:latest", cloud=False, tier_label="general",
        responses=[server.ModelCallError("timeout", "backend timed out"),
                   "recovered answer"],
    )

    first = server._answer_with_history_impl(
        "hello", [], tier="general", cache_scope="qc-owner",
    )
    assert request_cache.stats()["store"] == 0
    second = server._answer_with_history_impl(
        "hello", [], tier="general", cache_scope="qc-owner",
    )

    assert len(calls) == 2
    assert "recovered answer" in second
    assert "recovered answer" not in first


def test_answer_with_history_forwards_cache_arguments(monkeypatch):
    captured = {}

    def fake_impl(*args, **kwargs):
        captured.update(kwargs)
        return "answer"

    monkeypatch.setattr(server, "_answer_with_history_impl", fake_impl)
    observer = lambda status: None  # noqa: E731
    server.answer_with_history(
        "hello", [], cache_scope="qc-owner", cache_observer=observer,
    )

    assert captured["cache_scope"] == "qc-owner"
    assert captured["cache_observer"] is observer


# --- serve: scope derivation, receipt, metrics ----------------------------


def test_request_cache_scope_is_opaque_and_principal_bound():
    local = ts._request_cache_scope({})
    account = ts._request_cache_scope(
        {"account": {"username": "alice", "role": "user"}}
    )
    again = ts._request_cache_scope(
        {"account": {"username": "alice", "role": "user"}}
    )
    other = ts._request_cache_scope(
        {"account": {"username": "bob", "role": "user"}}
    )

    assert account == again
    assert len({local, account, other}) == 3
    assert "alice" not in account
    # Domain separation: never reuse another principal digest's namespace.
    assert not account.startswith(("fo-", "ro-"))


def test_run_prompt_reports_cache_status_and_metric(monkeypatch):
    captured = {}
    observed = []
    model_calls = []

    class _Metrics:
        def observe_model_call(self, **kwargs):
            model_calls.append(kwargs)

        def observe_request_cache(self, result):
            observed.append(result)

    def fake_answer(prompt, history, **kwargs):
        captured["cache_scope"] = kwargs.get("cache_scope")
        kwargs["cache_observer"]("hit")
        return "cached answer"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)
    result = ts._run_prompt(
        "hello", [], "general", return_result=True,
        metrics=_Metrics(), cache_scope="qc-owner",
    )

    assert captured["cache_scope"] == "qc-owner"
    assert result.cache == "hit"
    assert observed == ["hit"]
    assert model_calls == []


def test_run_prompt_ignores_unknown_cache_status(monkeypatch):
    observed = []

    class _Metrics:
        def observe_model_call(self, **kwargs):
            return None

        def observe_request_cache(self, result):
            observed.append(result)

    def fake_answer(prompt, history, **kwargs):
        kwargs["cache_observer"]("weird-status")
        return "answer"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)
    result = ts._run_prompt(
        "hello", [], "general", return_result=True,
        metrics=_Metrics(), cache_scope="qc-owner",
    )

    assert result.cache == ""
    assert observed == []


def test_metrics_request_cache_counter_is_noop_safe():
    metrics = sonder_metrics.MetricsRegistry(enabled=False)
    metrics.observe_request_cache("hit")
    metrics.observe_request_cache("miss")
    metrics.observe_request_cache("not-a-real-result")


# --- HTTP: scope threading and receipt ------------------------------------


@contextmanager
def _http_server(monkeypatch):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(port, method, path, body=None, headers=None):
    # A generous timeout: the first HTTP request of a test process pays
    # one-time lifecycle/database initialization that can exceed the 5s the
    # sibling serve harness uses.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    payload = response.read()
    result = response.status, dict(response.getheaders()), payload
    conn.close()
    return result


def _open_local_http(monkeypatch, fake_answer):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *a, **k: None)
    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)


def test_http_chat_threads_scope_and_exposes_bounded_receipt(monkeypatch):
    captured = {}

    def fake_answer(prompt, history, **kwargs):
        captured["cache_scope"] = kwargs.get("cache_scope")
        observer = kwargs.get("cache_observer")
        if observer is not None:
            observer("hit")
        return "answer"

    _open_local_http(monkeypatch, fake_answer)
    request = json.dumps({
        "model": "sonder", "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 200
    assert captured["cache_scope"]
    assert "hello" not in captured["cache_scope"]
    payload = json.loads(body)
    assert payload["sonder_receipt"]["cache"] == "hit"


def test_http_streaming_chat_never_offers_a_cache_scope(monkeypatch):
    captured = {}

    def fake_answer(prompt, history, **kwargs):
        captured["cache_scope"] = kwargs.get("cache_scope")
        return "answer"

    _open_local_http(monkeypatch, fake_answer)
    request = json.dumps({
        "model": "sonder", "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, _ = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    assert captured["cache_scope"] == ""
