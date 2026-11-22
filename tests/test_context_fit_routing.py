"""Launch-profile metadata, context-length routing and the per-tier Ollama window.

Covers: ``/v1/models`` parsing (absent fields included), the per-endpoint TTL
cache, the context-fit decision (fits -> unchanged; too long -> long-context
tier; too long without one -> a clear error), the per-tier ``num_ctx``
passthrough, the ``routing.long_context_tier`` policy field, and parity: a
turn that fits, or whose model's context is unknown, routes exactly as before
and triggers no lookup.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters.inference import launch_profile_cache
from sonder_runtime.adapters.inference import overflow_route
from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.routing import context_fit
from sonder_runtime.application.routing import long_context_overflow as overflow
from sonder_runtime.application.routing import tier_escalation
from sonder_runtime.domain import inference_profiles
from sonder_runtime.domain.runtime_policy import rules
from sonder_runtime.platform import tier_model_options

INFERENCE_ENV = (
    "SONDER_MODEL_BACKEND", "SONDER_FAST_PROVIDER", "SONDER_GENERAL_PROVIDER",
    "SONDER_CODE_PROVIDER", "SONDER_REASONING_PROVIDER", "SONDER_VISION_PROVIDER",
    "SONDER_INFERENCE_TIER_MODELS", "SONDER_INFERENCE_MODEL", "SONDER_INFERENCE_BASE_URL",
    "SONDER_INFERENCE_READY_FILE", "SONDER_LONG_CONTEXT_TIER", "SONDER_OLLAMA_TIER_OPTIONS",
    "SONDER_LONG_CONTEXT_OVERFLOW", "SONDER_LONG_CONTEXT_MODEL", "SONDER_LONG_CONTEXT_THRESHOLD",
    "SONDER_INFERENCE_PROFILE_TTL_SECONDS", "SONDER_CAPABILITY_ROUTING",
)

# The entry shape Sonder Inference's ``serve --profile`` publishes on
# ``/v1/models`` (Inference docs/integration/launch-profiles.md, "/v1/models").
PROFILE_100K = {
    "id": "qwen3.8-27b-q3-100k", "object": "model", "owned_by": "sonder-inference",
    "sonder": {
        "backend": "llamaserver", "default": True, "synthetic": False,
        "profile": {
            "name": "qwen3.8-27b-q3-100k", "backend": "llamaserver", "context_length": 100096,
            "parallel": 1, "cache_type_k": "q8_0", "cache_type_v": "q5_1", "flash_attn": "on",
            "capabilities": ["chat", "streaming"], "upstream_capabilities": ["tools"],
            "estimated_vram_mib": 15437, "vram_budget_mib": 15600, "served": True,
        },
    },
}
# A flat spelling (fields directly on the ``sonder`` extension object).
PROFILE_100K_FLAT = {
    "id": "qwen3.8-27b-q3-100k", "object": "model",
    "sonder": {
        "backend": "llamaserver", "default": True, "profile": "qwen3.8-27b-q3-100k",
        "context_length": 100096, "capabilities": ["chat", "streaming"],
    },
}
PROFILE_32K = {
    "id": "qwen3.8-27b-q3-32k", "object": "model",
    "sonder": {"backend": "llamaserver", "context_length": 32768, "capabilities": ["chat"]},
}
# An Inference from before launch profiles: only the original extension fields.
OLD_ENTRY = {"id": "mock:tiny", "object": "model", "owned_by": "sonder-inference",
             "sonder": {"backend": "mock", "default": True, "synthetic": True}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in INFERENCE_ENV:
        monkeypatch.delenv(name, raising=False)
    launch_profile_cache.shared_cache().clear()
    yield
    launch_profile_cache.shared_cache().clear()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeModels:
    """GET seam: health is ready; /v1/models answers from ``status``/``document``."""

    def __init__(self, document=None, status=200):
        self.document = document
        self.status = status
        self.gets: list[str] = []

    def get(self, url, headers, timeout):
        self.gets.append(url)
        if url.endswith("/v1/sonder/health"):
            return 200, json.dumps({
                "status": "ready", "api_version": 1, "version": "0.2.0", "synthetic": False,
                "models": [{"id": "qwen3.8-27b-q3-100k", "backend": "llamaserver", "default": True}],
            }).encode()
        if url.endswith("/v1/models"):
            assert timeout <= 2.0
            if isinstance(self.document, BaseException):
                raise self.document
            return self.status, json.dumps(self.document).encode()
        if "/v1/sonder/identity" in url:
            return 404, b"{}"
        raise AssertionError(url)

    def models_gets(self):
        return [url for url in self.gets if url.endswith("/v1/models")]


def _gateway(fake, *, clock=None, cache=None, **settings):
    clock = clock or Clock()
    return SonderInferenceGateway(
        SonderInferenceConfig(**settings), get_transport=fake.get,
        transport=lambda *a, **k: pytest.fail("no generation expected"),
        monotonic=clock, profile_cache=cache or launch_profile_cache.ProfileCache(clock),
    )


# -- /v1/models parsing ---------------------------------------------------------

@pytest.mark.parametrize("entry", [PROFILE_100K, PROFILE_100K_FLAT])
def test_profile_fields_are_read_from_the_sonder_extension(entry):
    models = inference_profiles.parse_launch_profile_models({"object": "list", "data": [entry]})
    profile = models["qwen3.8-27b-q3-100k"]
    assert profile.context_length == 100096
    assert profile.capabilities == ("chat", "streaming")
    assert profile.profile == "qwen3.8-27b-q3-100k"
    assert profile.backend == "llamaserver" and profile.default is True


def test_profile_object_wins_and_reports_the_running_context():
    """auto_fit shrank the served context: Inference reports the running value."""
    entry = {"id": "p", "sonder": {
        "backend": "llamaserver", "context_length": 999,
        "profile": {"name": "p-100k", "context_length": 81920,
                    "configured_context_length": 100096, "capabilities": []},
    }}
    profile = inference_profiles.parse_launch_profile_entry(entry)
    assert profile.context_length == 81920
    assert profile.capabilities == ()  # reported, and empty -- not unknown
    assert profile.profile == "p-100k"
    # A profile with a null context length (the model's training context)
    # falls back to the extension, then to unknown.
    bare = inference_profiles.parse_launch_profile_entry(
        {"id": "q", "sonder": {"profile": {"name": "q", "context_length": None}}})
    assert bare.context_length is None and bare.capabilities is None


def test_absent_profile_fields_are_unknown_not_invented():
    models = inference_profiles.parse_launch_profile_models({"data": [OLD_ENTRY]})
    profile = models["mock:tiny"]
    assert (profile.context_length, profile.capabilities, profile.profile) == (None, None, None)
    assert profile.backend == "mock" and profile.default is True


@pytest.mark.parametrize("entry, expected", [
    ({"id": "a", "context_length": 8192}, 8192),                       # top level
    ({"id": "a", "sonder": {"context_tokens": 4096}}, 4096),           # alias
    ({"id": "a", "sonder": {"n_ctx": 2048.0}}, 2048),                  # integral float
    ({"id": "a", "sonder": {"context_length": True}}, None),           # bool is not a number
    ({"id": "a", "sonder": {"context_length": -1}}, None),
    ({"id": "a", "sonder": {"context_length": "100k"}}, None),
    ({"id": "a", "sonder": {"context_length": 0}}, None),
    ({"id": "a", "sonder": {"context_length": 2 ** 40}}, None),        # implausible
])
def test_context_length_spellings_and_invalid_values(entry, expected):
    assert inference_profiles.parse_launch_profile_entry(entry).context_length == expected


def test_profile_name_capability_normalisation_and_bad_entries():
    entry = {"id": "m", "sonder": {"profile": {"name": "long"}, "capabilities": ["Chat", "chat", 3, ""]}}
    profile = inference_profiles.parse_launch_profile_entry(entry)
    assert profile.profile == "long" and profile.capabilities == ("chat",)
    assert inference_profiles.parse_launch_profile_entry({"object": "model"}) is None
    assert inference_profiles.parse_launch_profile_entry("x") is None
    assert inference_profiles.parse_launch_profile_models({"object": "list"}) is None
    assert inference_profiles.parse_launch_profile_models([PROFILE_100K]) is None
    assert inference_profiles.parse_launch_profile_models({"data": [PROFILE_100K, "x", {}]}) == {
        "qwen3.8-27b-q3-100k": inference_profiles.parse_launch_profile_entry(PROFILE_100K),
    }


def test_lookup_resolves_default_profile_name_and_case():
    catalog = inference_profiles.LaunchProfileCatalog(
        "http://127.0.0.1:11437", inference_profiles.CATALOG_OK,
        models=inference_profiles.parse_launch_profile_models({"data": [PROFILE_32K, PROFILE_100K]}),
    )
    assert catalog.lookup("default").context_length == 100096
    assert catalog.lookup("QWEN3.8-27B-Q3-32K").context_length == 32768
    assert catalog.lookup("missing") is None and catalog.lookup("") is None


# -- fetch and TTL cache ----------------------------------------------------------

def test_catalog_is_cached_for_the_ttl_then_refetched():
    clock = Clock()
    fake = FakeModels({"data": [PROFILE_100K]})
    gateway = _gateway(fake, clock=clock)
    assert gateway.model_profiles().state == inference_profiles.CATALOG_OK
    clock.now += 59.0
    assert gateway.model_profiles().lookup("default").context_length == 100096
    assert len(fake.models_gets()) == 1
    clock.now += 2.0
    gateway.model_profiles()
    assert len(fake.models_gets()) == 2
    gateway.model_profiles(refresh=True)
    assert len(fake.models_gets()) == 3


def test_ttl_is_configurable_and_invalid_values_keep_the_default():
    assert launch_profile_cache.profile_ttl_seconds({}) == 60.0
    assert launch_profile_cache.profile_ttl_seconds(
        {"SONDER_INFERENCE_PROFILE_TTL_SECONDS": "5"}) == 5.0
    for bad in ("x", "-1", "nan", "7200"):
        assert launch_profile_cache.profile_ttl_seconds(
            {"SONDER_INFERENCE_PROFILE_TTL_SECONDS": bad}) == 60.0


@pytest.mark.parametrize("status, document, state", [
    (404, {"error": {"code": "not_found"}}, inference_profiles.CATALOG_ABSENT),
    (200, {"object": "list"}, inference_profiles.CATALOG_ABSENT),
    (500, {"error": {"code": "internal"}}, inference_profiles.CATALOG_ERROR),
])
def test_older_or_failing_inference_is_absent_or_error_and_cached(status, document, state):
    clock = Clock()
    fake = FakeModels(document, status=status)
    gateway = _gateway(fake, clock=clock)
    catalog = gateway.model_profiles()
    assert catalog.state == state and catalog.lookup("default") is None
    gateway.model_profiles()
    assert len(fake.models_gets()) == 1  # a missing endpoint is also cached


def test_unreachable_models_endpoint_is_an_error_never_raised():
    fake = FakeModels(urllib.error.URLError(ConnectionRefusedError(111, "refused")))
    catalog = _gateway(fake).model_profiles()
    assert catalog.state == inference_profiles.CATALOG_ERROR
    assert "refused" in catalog.detail or "reachable" in catalog.detail


def test_remote_endpoint_without_consent_is_never_contacted():
    fake = FakeModels({"data": [PROFILE_100K]})
    catalog = _gateway(fake, base_url="https://gpu.example").model_profiles()
    assert catalog.state == inference_profiles.CATALOG_ERROR and fake.gets == []


def test_tier_context_reports_each_bound_profile_in_provider_status():
    fake = FakeModels({"data": [PROFILE_100K, PROFILE_32K]})
    gateway = _gateway(fake, tier_models={"code": "qwen3.8-27b-q3-32k"})
    document = gateway.tier_context()
    assert document["state"] == "ok"
    assert document["tiers"]["code"] == {
        "model": "qwen3.8-27b-q3-32k", "profile": None, "context_length": 32768,
        "capabilities": ["chat"], "source": "inference_profile",
    }
    assert document["tiers"]["general"]["context_length"] == 100096  # "default" -> default entry
    status = gateway.provider_status()["sonder_inference"]
    assert status["tier_context"] == document
    assert len(fake.models_gets()) == 1  # status reused the cached catalog


# -- context-fit decision -----------------------------------------------------------

def _window(tier, context, model="m", provider="ollama"):
    return context_fit.TierWindow(
        tier, model, provider, context,
        context_fit.SOURCE_MODEL if context is not None else context_fit.SOURCE_UNKNOWN,
    )


def _no_window(tier):
    raise AssertionError("long tier must not be looked up")


def test_fits_or_unknown_routes_unchanged():
    assert context_fit.decide(1000, _window("code", 8192), long_context_tier="reasoning",
                              window_for=_no_window) is None
    assert context_fit.decide(8192, _window("code", 8192), long_context_tier="reasoning",
                              window_for=_no_window) is None
    assert context_fit.decide(10**6, _window("code", None), long_context_tier="reasoning",
                              window_for=_no_window) is None


def test_too_long_routes_to_the_long_context_tier_and_says_why():
    decision = context_fit.decide(
        40_000, _window("code", 32768, "dense-32k"), long_context_tier="reasoning",
        window_for=lambda tier: _window(tier, 100096, "qwen-100k", "sonder_inference"),
    )
    assert decision.switched and decision.target.tier == "reasoning"
    assert decision.notice() == (
        "context fit: routed to long-context tier reasoning (qwen-100k) -- request ~40k "
        "tokens > 32.8k context of dense-32k on tier code"
    )
    assert decision.receipt()["status"] == "switched"
    assert decision.telemetry()["reason_code"] == "context_exceeds_model"


def test_too_long_without_a_long_tier_is_a_clear_error():
    decision = context_fit.decide(40_000, _window("code", 32768, "dense-32k"),
                                  long_context_tier="", window_for=_no_window)
    assert decision.status == "rejected"
    text = decision.error_text()
    assert "about 40000 tokens" in text and "32768-token context" in text
    assert "nothing was truncated" in text and "long_context_tier" in text


def test_long_tier_that_is_too_small_unbound_failing_evidence_or_same_is_rejected():
    start = _window("code", 32768)
    small = context_fit.decide(40_000, start, long_context_tier="reasoning",
                               window_for=lambda tier: _window(tier, 32768))
    assert small.status == "rejected" and "does not hold it either" in small.reason
    unbound = context_fit.decide(40_000, start, long_context_tier="reasoning",
                                 window_for=lambda tier: _window(tier, None, model=""))
    assert unbound.status == "rejected" and "no model bound" in unbound.reason
    same = context_fit.decide(40_000, start, long_context_tier="code", window_for=_no_window)
    assert same.status == "rejected"
    evidence = context_fit.decide(40_000, start, long_context_tier="reasoning",
                                  window_for=lambda tier: _window(tier, 100000),
                                  evidence_failure=lambda target: "recent failure")
    assert evidence.status == "rejected" and "capability evidence" in evidence.reason
    broken = context_fit.decide(40_000, start, long_context_tier="reasoning",
                                window_for=lambda tier: (_ for _ in ()).throw(RuntimeError()))
    assert broken.status == "rejected" and "could not be resolved" in broken.reason
    unknown_target = context_fit.decide(40_000, start, long_context_tier="reasoning",
                                        window_for=lambda tier: _window(tier, None))
    assert unknown_target.switched  # the operator named it for long context


# -- chat-loop wiring (overflow_route -> context_fit_route) ----------------------------

class Host:
    """The attributes the chat loop's routing reads from ``server``."""

    def __init__(self, *, tiers=None, contexts=None, providers=None):
        self.TIERS = dict(tiers or {"fast": "small:3b", "code": "dense-32k",
                                    "general": "dense-32k", "reasoning": "moe-128k"})
        self.contexts = dict(contexts or {})
        self.providers = dict(providers or {})
        self.metadata_calls: list[str] = []
        self._RUNTIME_POLICY = runtime_policy.default_policy(env={})
        self.OLLAMA_POOL = None
        self._MODEL_CONTEXT_CACHE = {}

    def _refresh_runtime_policy(self, create=True):
        return self._RUNTIME_POLICY

    def _bridge_provider_for_tier(self, tier, cloud=False):
        return None if cloud else self.providers.get(tier)

    def _model_context_metadata(self, model):
        self.metadata_calls.append(model)
        return self.contexts.get(model), None

    def resolve_discovered_model_record(self, model):
        return False


def _plan(tier="code", model="dense-32k"):
    return tier_escalation.single(tier_escalation.Rung(tier=tier, model=model))


SHORT = [{"role": "user", "content": "x" * 4000}]           # ~1k tokens
LONG = [{"role": "user", "content": "x" * 160_000}]        # ~40k tokens


def test_short_turn_does_no_lookup_and_keeps_the_plan():
    host = Host(contexts={"dense-32k": 32768})
    plan = _plan()
    result = overflow_route.plan(host, "hello", SHORT, plan)
    assert result == (plan, None) and result[0] is plan
    assert host.metadata_calls == []


def test_short_turn_on_an_inference_tier_never_fetches_profiles(monkeypatch):
    host = Host(providers={"code": "sonder_inference"})
    monkeypatch.setattr(SonderInferenceGateway, "model_profiles",
                        lambda *a, **k: pytest.fail("no /v1/models fetch on a short turn"))
    plan = _plan()
    assert overflow_route.plan(host, "hello", SHORT, plan) == (plan, None)


def test_long_turn_that_fits_keeps_the_plan_and_reads_only_the_cached_metadata():
    host = Host(contexts={"dense-32k": 131072})
    plan = _plan()
    assert overflow_route.plan(host, "hello", LONG, plan) == (plan, None)
    # The same read the turn's automatic window makes next (server caches it).
    assert host.metadata_calls == ["dense-32k"]


def test_long_turn_with_unknown_context_keeps_the_plan():
    host = Host(contexts={})
    plan = _plan()
    assert overflow_route.plan(host, "hello", LONG, plan) == (plan, None)


def test_pinned_turn_never_probes_ollama_metadata():
    host = Host(contexts={"dense-32k": 8192})
    plan = _plan()
    assert overflow_route.plan(host, "hello", LONG, plan, context_size="8k") == (plan, None)
    assert host.metadata_calls == []
    host._MODEL_CONTEXT_CACHE["dense-32k"] = (0.0, 8192, None)
    _plan_after, decision = overflow_route.plan(host, "hello", LONG, plan, context_size="8k")
    assert decision.status == "rejected" and host.metadata_calls == []


def test_long_turn_reroutes_to_the_long_context_tier(monkeypatch):
    monkeypatch.setenv("SONDER_LONG_CONTEXT_TIER", "reasoning")
    host = Host(contexts={"dense-32k": 32768, "moe-128k": 131072})
    new_plan, decision = overflow_route.plan(host, "hello", LONG, _plan())
    assert decision.switched
    assert new_plan.rungs == (tier_escalation.Rung(
        tier="reasoning", model="moe-128k", route=context_fit.ROUTE),)
    # The rerouted Ollama rung's window is raised to hold the prompt.
    window = overflow_route.rung_started(new_plan.start, decision, 16384, None)
    assert 40_000 + 1024 <= window <= 131072
    with overflow.notice_scope() as notes:
        overflow_route.finished(decision, new_plan.start)
    receipt = overflow.receipt_entry(notes)
    assert receipt["status"] == "switched" and receipt["to_tier"] == "reasoning"
    assert receipt["notice"].startswith("context fit: routed to long-context tier reasoning")


def test_policy_long_context_tier_is_used_when_the_environment_is_unset():
    host = Host(contexts={"dense-32k": 32768, "moe-128k": 131072})
    host._RUNTIME_POLICY = {**host._RUNTIME_POLICY,
                            "routing": {**host._RUNTIME_POLICY["routing"],
                                        "long_context_tier": "reasoning"}}
    new_plan, decision = overflow_route.plan(host, "hello", LONG, _plan())
    assert decision.switched and new_plan.start.tier == "reasoning"


def test_long_turn_without_a_long_tier_fails_before_any_model_call():
    host = Host(contexts={"dense-32k": 32768})
    plan = _plan()
    new_plan, decision = overflow_route.plan(host, "hello", LONG, plan)
    assert new_plan is plan and decision.status == "rejected"
    with pytest.raises(ModelCallError) as raised:
        overflow_route.rung_started(plan.start, decision, 16384, None)
    assert raised.value.kind == "configuration" and raised.value.status == 400
    assert "nothing was truncated" in raised.value.detail


def test_inference_tier_uses_the_profile_context(monkeypatch):
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS",
                       "code=qwen3.8-27b-q3-32k,reasoning=qwen3.8-27b-q3-100k")
    monkeypatch.setenv("SONDER_LONG_CONTEXT_TIER", "reasoning")
    fake = FakeModels({"data": [PROFILE_32K, PROFILE_100K]})
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.sonder_inference_gateway.direct_get_transport",
        fake.get,
    )
    host = Host(providers={"code": "sonder_inference", "reasoning": "sonder_inference"})
    new_plan, decision = overflow_route.plan(host, "hello", LONG, _plan())
    assert decision.switched and decision.start.context_tokens == 32768
    assert decision.target.context_tokens == 100096 and decision.target.source == "inference_profile"
    assert new_plan.start.tier == "reasoning"
    assert len(fake.models_gets()) == 1  # one cached fetch serves both tiers
    # A non-Ollama rung has no window to size.
    assert overflow_route.rung_started(new_plan.start, decision, None, None) is None


def test_cloud_start_rung_is_never_checked():
    host = Host(contexts={"dense-32k": 1024})
    plan = tier_escalation.single(tier_escalation.Rung(tier="cloud-general", model="x:cloud", cloud=True))
    assert overflow_route.plan(host, "hello", LONG, plan) == (plan, None)
    assert host.metadata_calls == []


def test_exact_model_pin_is_never_rerouted(monkeypatch):
    monkeypatch.setenv("SONDER_LONG_CONTEXT_TIER", "reasoning")
    host = Host(contexts={"pinned:7b": 8192})
    plan = tier_escalation.single(tier_escalation.Rung(tier="pinned:7b", model="pinned:7b"))
    new_plan, decision = overflow_route.plan(host, "hello", LONG, plan)
    assert new_plan is plan and decision.status == "rejected"


def test_switched_overflow_takes_precedence(monkeypatch):
    monkeypatch.setenv("SONDER_LONG_CONTEXT_OVERFLOW", "1")
    monkeypatch.setenv("SONDER_LONG_CONTEXT_MODEL", "moe-128k")
    host = Host(contexts={"dense-32k": 1024})
    host.resolve_discovered_model_record = lambda model: True
    _new_plan, decision = overflow_route.plan(host, "hello", LONG, _plan())
    assert isinstance(decision, overflow.Decision) and decision.switched
    assert host.metadata_calls == []


def test_routing_failure_inside_the_lookup_never_fails_the_turn():
    host = Host()
    host._model_context_metadata = lambda model: (_ for _ in ()).throw(RuntimeError("x"))
    plan = _plan()
    assert overflow_route.plan(host, "hello", LONG, plan) == (plan, None)


# -- parity with the behaviour before context-fit routing -----------------------------

@pytest.mark.parametrize("history", [None, [], SHORT, LONG])
@pytest.mark.parametrize("req_ctx, pinned", [(16384, None), (None, None), (0, None), (8192, 8192)])
def test_rung_started_is_unchanged_without_configuration(history, req_ctx, pinned):
    host = Host(contexts={"dense-32k": 131072})
    plan, decision = overflow_route.plan(host, "hello", history, _plan())
    assert decision is None
    assert overflow_route.rung_started(plan.start, decision, req_ctx, pinned) == req_ctx


# -- per-tier Ollama window -------------------------------------------------------------

def test_tier_options_parse_and_validate():
    options, error = tier_model_options.parse('{"reasoning": {"num_ctx": 65536}, "code": {}}')
    assert error == "" and options == {"reasoning": {"num_ctx": 65536}}
    for bad in ('[1]', '{"cloud": {"num_ctx": 4096}}', '{"code": {"num_gpu": 1}}',
                '{"code": {"num_ctx": 100}}', '{"code": {"num_ctx": 2000000}}',
                '{"code": {"num_ctx": "4096"}}', '{"code": {"num_ctx": true}}',
                '{"code": 4096}', 'nope'):
        assert tier_model_options.parse(bad)[0] == {}
        assert tier_model_options.parse(bad)[1]
    assert tier_model_options.TIERS == rules.LOCAL_TIERS


def test_tier_num_ctx_applies_unless_pinned_cloud_or_non_ollama(monkeypatch):
    monkeypatch.setenv("SONDER_OLLAMA_TIER_OPTIONS", '{"code": {"num_ctx": 65536}}')
    rung = tier_escalation.Rung(tier="code", model="dense-32k")
    assert overflow_route.rung_started(rung, None, 16384, None) == 65536
    assert overflow_route.rung_started(rung, None, 8192, 8192) == 8192       # pinned wins
    assert overflow_route.rung_started(rung, None, None, None) is None       # non-Ollama rung
    assert overflow_route.rung_started(rung, None, 0, None) == 0             # cloud
    other = tier_escalation.Rung(tier="general", model="dense-32k")
    assert overflow_route.rung_started(other, None, 16384, None) == 16384


def test_invalid_tier_options_change_nothing(monkeypatch):
    monkeypatch.setenv("SONDER_OLLAMA_TIER_OPTIONS", '{"code": {"num_ctx": -1}}')
    rung = tier_escalation.Rung(tier="code", model="dense-32k")
    assert overflow_route.rung_started(rung, None, 16384, None) == 16384


def test_tier_num_ctx_is_the_window_the_fit_check_uses(monkeypatch):
    monkeypatch.setenv("SONDER_OLLAMA_TIER_OPTIONS", '{"code": {"num_ctx": 16384}}')
    host = Host(contexts={"dense-32k": 131072})
    _plan_after, decision = overflow_route.plan(host, "hello", LONG, _plan())
    assert decision.status == "rejected"
    assert decision.start.source == context_fit.SOURCE_TIER_OPTION and host.metadata_calls == []


def test_ollama_gateway_sends_the_tier_num_ctx(monkeypatch):
    monkeypatch.setenv("SONDER_OLLAMA_TIER_OPTIONS", '{"code": {"num_ctx": 65536}}')
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.model_gateway import ModelRequest
    from sonder_runtime.application.ports.model_target import ModelTarget

    seen = {}

    def factory(model, system, temperature, num_predict, num_ctx, **kwargs):
        def gen(prompt, history=None):
            seen["num_ctx"] = num_ctx
            return "ok"
        return gen

    target = ModelTarget(model="dense-32k", cloud=False, tier_label="code")
    gateway = OllamaGateway(
        generate_factory=factory, target_resolver=lambda tier, strict: target,
        session_num_ctx=8192,
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.ollama_gateway._enforce_local_endpoint",
        lambda *a, **k: None, raising=False,
    )
    context = local_owner_context(correlation_id="t1")
    gateway.generate(ModelRequest(prompt="hi", tier="code"), context)
    assert seen["num_ctx"] == 65536
    gateway.generate(ModelRequest(prompt="hi", tier="code", options={"num_ctx": 4096}), context)
    assert seen["num_ctx"] == 4096  # an explicit request option wins
    monkeypatch.delenv("SONDER_OLLAMA_TIER_OPTIONS")
    gateway.generate(ModelRequest(prompt="hi", tier="code"), context)
    assert seen["num_ctx"] == 8192


# -- policy ---------------------------------------------------------------------------

@pytest.fixture
def policy_file(monkeypatch, tmp_path):
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    return path


def test_default_policy_file_is_unchanged_by_the_new_field(policy_file):
    policy = runtime_policy.load(create=True)
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert set(on_disk["routing"]) == set(rules.ROUTING_LANES)
    assert "long_context_tier" not in policy["routing"]
    assert runtime_policy.long_context_tier(policy, env={}) == {"tier": "", "source": "", "error": ""}


def test_long_context_tier_is_set_validated_cleared_and_env_overridable(policy_file):
    policy = runtime_policy.update(routing={"long_context_tier": "reasoning"})
    assert policy["routing"]["long_context_tier"] == "reasoning"
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert on_disk["routing"]["long_context_tier"] == "reasoning"
    assert runtime_policy.route_tier("chat", policy) == "general"
    for bad in ("cloud-general", "chat", "qwen3:32b"):
        with pytest.raises(ValueError, match="long_context_tier"):
            runtime_policy.update(routing={"long_context_tier": bad})
    assert runtime_policy.long_context_tier(policy, env={"SONDER_LONG_CONTEXT_TIER": "code"}) == {
        "tier": "code", "source": "environment", "error": ""}
    ignored = runtime_policy.long_context_tier(policy, env={"SONDER_LONG_CONTEXT_TIER": "gpt"})
    assert ignored["tier"] == "reasoning" and ignored["error"]
    # An unrelated update keeps the tier; an empty value clears it.
    kept = runtime_policy.update(routing={"chat": "code"})
    assert kept["routing"]["long_context_tier"] == "reasoning"
    cleared = runtime_policy.update(routing={"long_context_tier": ""})
    assert "long_context_tier" not in cleared["routing"]
    assert "context fit: long-context tier (unset" in runtime_policy.format_policy(cleared)


def test_unknown_routing_keys_are_still_refused(policy_file):
    with pytest.raises(ValueError, match="unknown routing lane"):
        runtime_policy.update(routing={"long_context": "reasoning"})


def test_policy_status_lists_tier_context_from_caches_only(policy_file, monkeypatch):
    monkeypatch.setenv("SONDER_CODE_PROVIDER", "sonder_inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "code=qwen3.8-27b-q3-100k")
    monkeypatch.setenv("SONDER_OLLAMA_TIER_OPTIONS", '{"general": {"num_ctx": 32768}}')
    monkeypatch.setenv("SONDER_LONG_CONTEXT_TIER", "code")
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.sonder_inference_gateway.direct_get_transport",
        lambda *a, **k: pytest.fail("runtime_policy_status must not open a socket"),
    )
    policy = runtime_policy.load(create=True)
    general = policy["local_models"]["general"]
    text = runtime_policy.format_policy(policy)
    assert "context fit: long-context tier code [from environment]" in text
    assert "code: sonder_inference qwen3.8-27b-q3-100k, context unknown" in text
    assert "general: ollama %s, context 32768 tokens (tier num_ctx)" % general in text
    # Once status or doctor has read the profile, the cached value is shown.
    fake = FakeModels({"data": [PROFILE_100K]})
    SonderInferenceGateway(get_transport=fake.get).model_profiles()
    text = runtime_policy.format_policy(policy)
    assert ("code: sonder_inference qwen3.8-27b-q3-100k, context 100096 tokens "
            "(profile qwen3.8-27b-q3-100k), capabilities chat, streaming") in text
