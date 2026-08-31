# Tier-Aware Local Provider Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one Sonder application graph route `fast`/`general` generation through loopback Prism/llama.cpp while keeping `code`/`reasoning` and embeddings on Ollama.

**Architecture:** Parse and freeze provider bindings at bootstrap, compose the already-existing Ollama and OpenAI-compatible gateways behind a small `ProviderDispatchGateway`, and carry the same per-tier provider mapping into `ModelRoute`. A uniform binding returns the original direct gateway; mixed bindings use the dispatcher and never fail over between providers.

**Tech Stack:** Python 3.12, frozen dataclasses, `MappingProxyType`, existing `ModelGateway` protocol, pytest 9, PowerShell on Windows 11.

**Spec:** `docs/superpowers/specs/2026-08-31-tier-provider-dispatch-design.md`

## Global Constraints

- Preserve `SONDER_MODEL_BACKEND=ollama` as the default and all existing OpenAI-compatible aliases.
- Unset tier and embedding bindings inherit `SONDER_MODEL_BACKEND`; the mixed production profile explicitly sets `SONDER_EMBEDDING_PROVIDER=ollama`.
- Keep endpoints, credentials, model content, output, and filesystem paths out of the provider status projection.
- Never retry or fail over from one provider to another inside the dispatcher.
- Leave endpoint consent, deadlines, transport retries, telemetry, and response validation inside the existing provider adapters.
- Do not modify runtime-policy JSON, Ollama model storage, GGUF files, or the model manifests in this repository change.
- Add no third-party dependency.
- Use a unique pytest `--basetemp` on this machine because stale global numbered-temp cleanup is known to hang after otherwise-complete runs.
- Preserve unrelated worktree changes and stage only paths named by each task.

---

### Task 1: Parse and Freeze Provider Bindings

**Files:**
- Create: `sonder_runtime/bootstrap/provider_bindings.py`
- Create: `tests/test_provider_bindings.py`

**Interfaces:**
- Consumes: environment-style `Mapping[str, str]` values.
- Produces: `normalize_provider(value: str) -> str`, `ProviderBindings.uniform(provider: str) -> ProviderBindings`, `provider_bindings_from_env(env: Mapping[str, str] | None = None) -> ProviderBindings`, `ProviderBindings.required_providers: frozenset[str]`, and `ProviderBindings.status_projection() -> dict[str, object]`.

- [ ] **Step 1: Write the failing configuration tests**

```python
from __future__ import annotations

import pytest

from sonder_runtime.bootstrap.provider_bindings import (
    PROVIDER_TIERS,
    ProviderBindings,
    normalize_provider,
    provider_bindings_from_env,
)


def test_aliases_and_unset_bindings_inherit_global_backend():
    bindings = provider_bindings_from_env({"SONDER_MODEL_BACKEND": " llamacpp "})

    assert bindings.default_generation_provider == "openai_compatible"
    assert dict(bindings.tier_providers) == {
        tier: "openai_compatible" for tier in PROVIDER_TIERS
    }
    assert bindings.embedding_provider == "openai_compatible"
    assert bindings.required_providers == frozenset({"openai_compatible"})


def test_mixed_profile_has_explicit_ollama_embeddings_and_content_free_status():
    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "ollama",
        "SONDER_FAST_PROVIDER": "openai-compatible",
        "SONDER_GENERAL_PROVIDER": "vllm",
        "SONDER_EMBEDDING_PROVIDER": "ollama",
        "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:18080",
        "SONDER_OPENAI_API_KEY": "must-not-appear",
    })

    assert bindings.tier_providers["fast"] == "openai_compatible"
    assert bindings.tier_providers["general"] == "openai_compatible"
    assert bindings.tier_providers["code"] == "ollama"
    assert bindings.embedding_provider == "ollama"
    assert bindings.required_providers == frozenset({"ollama", "openai_compatible"})
    assert bindings.status_projection() == {
        "default_generation_provider": "ollama",
        "tier_providers": {
            "fast": "openai_compatible",
            "general": "openai_compatible",
            "code": "ollama",
            "reasoning": "ollama",
            "vision": "ollama",
        },
        "embedding_provider": "ollama",
    }


@pytest.mark.parametrize("value", ["cloud", "unknown", "openai_compatible_typo"])
def test_unknown_nonblank_provider_fails_closed(value):
    with pytest.raises(ValueError, match="unknown model provider"):
        normalize_provider(value)


def test_uniform_constructor_normalizes_alias():
    bindings = ProviderBindings.uniform("openai")
    assert bindings.required_providers == frozenset({"openai_compatible"})
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-bindings-red-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_provider_bindings.py
```

Expected: collection fails because `sonder_runtime.bootstrap.provider_bindings` does not exist.

- [ ] **Step 3: Implement the immutable binding parser**

Create `sonder_runtime/bootstrap/provider_bindings.py` with:

```python
"""Validated, content-free provider bindings for model-gateway composition."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

PROVIDER_TIERS = ("fast", "general", "code", "reasoning", "vision")
_TIER_ENV = {
    "fast": "SONDER_FAST_PROVIDER",
    "general": "SONDER_GENERAL_PROVIDER",
    "code": "SONDER_CODE_PROVIDER",
    "reasoning": "SONDER_REASONING_PROVIDER",
    "vision": "SONDER_VISION_PROVIDER",
}
_ALIASES = {
    "ollama": "ollama",
    "openai": "openai_compatible",
    "openai-compatible": "openai_compatible",
    "openai_compatible": "openai_compatible",
    "llamacpp": "openai_compatible",
    "vllm": "openai_compatible",
}


def normalize_provider(value: str) -> str:
    normalized = str(value or "").strip().lower()
    try:
        return _ALIASES[normalized]
    except KeyError as exc:
        raise ValueError("unknown model provider %r" % value) from exc


@dataclass(frozen=True)
class ProviderBindings:
    default_generation_provider: str
    tier_providers: Mapping[str, str]
    embedding_provider: str

    def __post_init__(self) -> None:
        default = normalize_provider(self.default_generation_provider)
        embedding = normalize_provider(self.embedding_provider)
        tiers = {str(k): normalize_provider(v) for k, v in self.tier_providers.items()}
        if set(tiers) != set(PROVIDER_TIERS):
            raise ValueError("provider bindings must define exactly %r" % (PROVIDER_TIERS,))
        object.__setattr__(self, "default_generation_provider", default)
        object.__setattr__(self, "tier_providers", MappingProxyType(tiers))
        object.__setattr__(self, "embedding_provider", embedding)

    @classmethod
    def uniform(cls, provider: str) -> "ProviderBindings":
        normalized = normalize_provider(provider)
        return cls(
            default_generation_provider=normalized,
            tier_providers={tier: normalized for tier in PROVIDER_TIERS},
            embedding_provider=normalized,
        )

    @property
    def required_providers(self) -> frozenset[str]:
        return frozenset((*self.tier_providers.values(), self.embedding_provider))

    def status_projection(self) -> dict[str, object]:
        return {
            "default_generation_provider": self.default_generation_provider,
            "tier_providers": dict(self.tier_providers),
            "embedding_provider": self.embedding_provider,
        }


def provider_bindings_from_env(
    env: Mapping[str, str] | None = None,
) -> ProviderBindings:
    source = os.environ if env is None else env
    default = normalize_provider(source.get("SONDER_MODEL_BACKEND", "ollama") or "ollama")
    tiers = {
        tier: normalize_provider(source.get(variable, "") or default)
        for tier, variable in _TIER_ENV.items()
    }
    embedding = normalize_provider(source.get("SONDER_EMBEDDING_PROVIDER", "") or default)
    return ProviderBindings(default, tiers, embedding)
```

- [ ] **Step 4: Run the configuration tests and verify GREEN**

Run the command from Step 2 with a new unique `--basetemp`.

Expected: `6 passed` and exit code 0.

- [ ] **Step 5: Commit the configuration boundary**

```powershell
git add -- sonder_runtime/bootstrap/provider_bindings.py tests/test_provider_bindings.py
git commit -m "feat: add immutable provider bindings"
```

---

### Task 2: Dispatch Generation and Embeddings Without Failover

**Files:**
- Create: `sonder_runtime/adapters/provider_dispatch/__init__.py`
- Create: `sonder_runtime/adapters/provider_dispatch/gateway.py`
- Create: `tests/test_provider_dispatch_gateway.py`

**Interfaces:**
- Consumes: `Mapping[str, ModelGateway]`, `Mapping[str, str]` tier bindings, one embedding provider name, `ModelRequest`, and `OperationContext`.
- Produces: `ProviderDispatchGateway.generate(request, context) -> ModelResponse` and `ProviderDispatchGateway.embed(texts, context) -> Sequence[Embedding]`.

- [ ] **Step 1: Write failing behavior tests for branch selection and no failover**

```python
from __future__ import annotations

import pytest

from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import Embedding, ModelRequest, ModelResponse
from sonder_runtime.domain.common.errors import DependencyUnavailable, InvalidInput


class RecordingGateway:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.generated = []
        self.embedded = []

    def generate(self, request, context):
        self.generated.append((request, context))
        if self.fail:
            raise DependencyUnavailable(self.name + " unavailable")
        return ModelResponse(text=self.name, model=self.name, tier=request.tier)

    def embed(self, texts, context):
        values = tuple(texts)
        self.embedded.append((values, context))
        return [Embedding(vector=(float(i + 1),), model=self.name) for i in range(len(values))]


def _context():
    return local_owner_context(correlation_id="provider-dispatch-test")


def _gateway(ollama, prism):
    return ProviderDispatchGateway(
        providers={"ollama": ollama, "openai_compatible": prism},
        tier_providers={
            "fast": "openai_compatible",
            "general": "openai_compatible",
            "code": "ollama",
            "reasoning": "ollama",
            "vision": "ollama",
        },
        embedding_provider="ollama",
    )


@pytest.mark.parametrize(
    ("tier", "expected"),
    [("fast", "prism"), ("general", "prism"), ("code", "ollama"), ("reasoning", "ollama")],
)
def test_generation_uses_exact_tier_binding_and_forwards_identity(tier, expected):
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = _gateway(ollama, prism)
    request = ModelRequest(prompt="hello", tier=tier)
    context = _context()

    response = gateway.generate(request, context)

    assert response.text == expected
    selected = prism if expected == "prism" else ollama
    assert selected.generated == [(request, context)]
    assert (ollama if selected is prism else prism).generated == []


def test_embeddings_ignore_generation_tiers_and_use_embedding_binding():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = _gateway(ollama, prism)
    context = _context()

    result = gateway.embed(["a", "b"], context)

    assert [row.model for row in result] == ["ollama", "ollama"]
    assert ollama.embedded == [(('a', 'b'), context)]
    assert prism.embedded == []


def test_provider_failure_is_propagated_without_second_provider_call():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism", fail=True)
    gateway = _gateway(ollama, prism)

    with pytest.raises(DependencyUnavailable, match="prism unavailable"):
        gateway.generate(ModelRequest(prompt="hello", tier="fast"), _context())

    assert len(prism.generated) == 1
    assert ollama.generated == []


def test_unknown_tier_and_missing_provider_fail_closed():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = _gateway(ollama, prism)
    with pytest.raises(InvalidInput, match="no provider binding"):
        gateway.generate(ModelRequest(prompt="hello", tier="oracle"), _context())

    with pytest.raises(InvalidInput, match="missing provider gateways"):
        ProviderDispatchGateway(
            providers={"ollama": ollama},
            tier_providers={"fast": "openai_compatible"},
            embedding_provider="ollama",
        )
```

- [ ] **Step 2: Run dispatcher tests and verify RED**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-dispatch-red-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_provider_dispatch_gateway.py
```

Expected: collection fails because the provider-dispatch adapter does not exist.

- [ ] **Step 3: Implement the minimal dispatcher**

Create `sonder_runtime/adapters/provider_dispatch/__init__.py`:

```python
"""Tier-aware composition over concrete model-provider gateways."""
from .gateway import ProviderDispatchGateway

__all__ = ["ProviderDispatchGateway"]
```

Create `sonder_runtime/adapters/provider_dispatch/gateway.py`:

```python
"""Select exactly one configured ModelGateway for each request."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ...application.context import OperationContext
from ...application.ports.model_gateway import Embedding, ModelGateway, ModelRequest, ModelResponse
from ...domain.common.errors import InvalidInput


class ProviderDispatchGateway:
    def __init__(
        self,
        *,
        providers: Mapping[str, ModelGateway],
        tier_providers: Mapping[str, str],
        embedding_provider: str,
    ) -> None:
        provider_map = dict(providers)
        tier_map = dict(tier_providers)
        required = set(tier_map.values()) | {embedding_provider}
        missing = sorted(required - set(provider_map))
        if missing:
            raise InvalidInput("missing provider gateways: %s" % ", ".join(missing))
        self._providers = MappingProxyType(provider_map)
        self._tier_providers = MappingProxyType(tier_map)
        self._embedding_provider = embedding_provider

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        provider = self._tier_providers.get(request.tier)
        if provider is None:
            raise InvalidInput("no provider binding for tier %r" % request.tier)
        return self._providers[provider].generate(request, context)

    def embed(
        self, texts: Sequence[str], context: OperationContext
    ) -> Sequence[Embedding]:
        return self._providers[self._embedding_provider].embed(texts, context)
```

- [ ] **Step 4: Run dispatcher tests and verify GREEN**

Run the command from Step 2 with a new unique `--basetemp`.

Expected: `7 passed` and exit code 0.

- [ ] **Step 5: Commit the dispatcher**

```powershell
git add -- sonder_runtime/adapters/provider_dispatch/__init__.py sonder_runtime/adapters/provider_dispatch/gateway.py tests/test_provider_dispatch_gateway.py
git commit -m "feat: dispatch model calls by provider binding"
```

---

### Task 3: Build Only the Required Provider Gateways

**Files:**
- Create: `sonder_runtime/bootstrap/model_gateways.py`
- Create: `tests/test_model_gateway_factory.py`

**Interfaces:**
- Consumes: `ProviderBindings` and optional `Mapping[str, Callable[[], ModelGateway]]` factories.
- Produces: `build_model_gateway(bindings, provider_factories=None) -> ModelGateway`.

- [ ] **Step 1: Write failing factory tests**

```python
from __future__ import annotations

from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.bootstrap.model_gateways import build_model_gateway
from sonder_runtime.bootstrap.provider_bindings import ProviderBindings


class MarkerGateway:
    pass


def test_uniform_binding_returns_direct_gateway_and_builds_only_one_provider():
    calls = []
    ollama = MarkerGateway()
    gateway = build_model_gateway(
        ProviderBindings.uniform("ollama"),
        {
            "ollama": lambda: calls.append("ollama") or ollama,
            "openai_compatible": lambda: calls.append("openai_compatible") or MarkerGateway(),
        },
    )
    assert gateway is ollama
    assert calls == ["ollama"]


def test_mixed_binding_builds_dispatcher_and_only_referenced_providers():
    bindings = ProviderBindings(
        default_generation_provider="ollama",
        tier_providers={
            "fast": "openai_compatible",
            "general": "openai_compatible",
            "code": "ollama",
            "reasoning": "ollama",
            "vision": "ollama",
        },
        embedding_provider="ollama",
    )
    calls = []
    gateway = build_model_gateway(
        bindings,
        {
            "ollama": lambda: calls.append("ollama") or MarkerGateway(),
            "openai_compatible": lambda: calls.append("openai_compatible") or MarkerGateway(),
            "unused": lambda: calls.append("unused") or MarkerGateway(),
        },
    )
    assert isinstance(gateway, ProviderDispatchGateway)
    assert calls == ["ollama", "openai_compatible"]
```

- [ ] **Step 2: Run factory tests and verify RED**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-factory-red-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_model_gateway_factory.py
```

Expected: collection fails because `sonder_runtime.bootstrap.model_gateways` does not exist.

- [ ] **Step 3: Implement the shared composition factory**

Create `sonder_runtime/bootstrap/model_gateways.py`:

```python
"""Shared provider-gateway construction for both composition roots."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from ..adapters.provider_dispatch.gateway import ProviderDispatchGateway
from ..application.ports.model_gateway import ModelGateway
from .provider_bindings import ProviderBindings

ProviderFactory = Callable[[], ModelGateway]


def _default_factories() -> dict[str, ProviderFactory]:
    from ..adapters.ollama.gateway import OllamaGateway
    from ..adapters.openai_compat.gateway import OpenAICompatibleGateway

    return {
        "ollama": OllamaGateway,
        "openai_compatible": OpenAICompatibleGateway,
    }


def build_model_gateway(
    bindings: ProviderBindings,
    provider_factories: Mapping[str, ProviderFactory] | None = None,
) -> ModelGateway:
    factories = dict(_default_factories() if provider_factories is None else provider_factories)
    missing = sorted(bindings.required_providers - set(factories))
    if missing:
        raise ValueError("missing provider factories: %s" % ", ".join(missing))
    gateways = {
        provider: factories[provider]() for provider in sorted(bindings.required_providers)
    }
    if len(gateways) == 1:
        return next(iter(gateways.values()))
    return ProviderDispatchGateway(
        providers=gateways,
        tier_providers=bindings.tier_providers,
        embedding_provider=bindings.embedding_provider,
    )
```

- [ ] **Step 4: Run factory and dispatcher tests and verify GREEN**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-factory-green-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_model_gateway_factory.py tests/test_provider_dispatch_gateway.py
```

Expected: `9 passed` and exit code 0.

- [ ] **Step 5: Commit the factory**

```powershell
git add -- sonder_runtime/bootstrap/model_gateways.py tests/test_model_gateway_factory.py
git commit -m "feat: compose required model providers"
```

---

### Task 4: Carry Per-Tier Provider Identity Through Route Planning

**Files:**
- Modify: `sonder_runtime/domain/routing/route_planner.py:61-65,126-143`
- Modify: `tests/test_spec5_route_planner.py:25-50,105-118,133-143`

**Interfaces:**
- Consumes: `AvailableModels.tier_providers: Mapping[str, str]` with `AvailableModels.provider` retained as a compatibility default.
- Produces: `AvailableModels.provider_for(tier: str) -> str` and `RoutePlanner.from_policy(policy, provider="ollama", tier_providers=None) -> AvailableModels`.

- [ ] **Step 1: Add a failing route-identity test**

Add to `tests/test_spec5_route_planner.py`:

```python
def test_selected_tier_uses_its_provider_binding(planner, policy):
    available = AvailableModels(
        tier_models={
            "fast": "bonsai",
            "general": "bonsai",
            "code": "qwen",
            "reasoning": "qwen",
            "vision": "",
        },
        provider="ollama",
        tier_providers={
            "fast": "openai_compatible",
            "general": "openai_compatible",
            "code": "ollama",
            "reasoning": "ollama",
        },
    )

    route = planner.select(
        RoutingRequest(lane="router", prompt="hello"), policy, available
    )

    assert route.provider == available.provider_for(route.tier)
    assert route.provider in {"ollama", "openai_compatible"}
```

Extend `TestFromPolicy`:

```python
def test_from_policy_preserves_per_tier_providers(self, policy):
    available = RoutePlanner.from_policy(
        policy,
        provider="ollama",
        tier_providers={"fast": "openai_compatible"},
    )
    assert available.provider_for("fast") == "openai_compatible"
    assert available.provider_for("code") == "ollama"
```

- [ ] **Step 2: Run the two new tests and verify RED**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-route-red-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_spec5_route_planner.py -k "selected_tier_uses_its_provider_binding or from_policy_preserves_per_tier_providers"
```

Expected: failures report that `AvailableModels` does not accept `tier_providers` and lacks `provider_for`.

- [ ] **Step 3: Implement per-tier route provider lookup**

Change `AvailableModels` to:

```python
@dataclass(frozen=True)
class AvailableModels:
    tier_models: dict[str, str] = field(default_factory=dict)
    provider: str = "ollama"
    tier_providers: dict[str, str] = field(default_factory=dict)

    @property
    def available_tiers(self) -> frozenset[str]:
        return frozenset(t for t, model in self.tier_models.items() if model)

    def provider_for(self, tier: str) -> str:
        return self.tier_providers.get(tier, self.provider)
```

Set the route field with:

```python
provider=available.provider_for(tier),
```

Replace `from_policy` with:

```python
@staticmethod
def from_policy(
    policy: dict,
    provider: str = "ollama",
    tier_providers: dict[str, str] | None = None,
) -> AvailableModels:
    models = policy.get("local_models", {})
    return AvailableModels(
        tier_models={tier: models.get(tier, "") for tier in LOCAL_TIERS},
        provider=provider,
        tier_providers=dict(tier_providers or {}),
    )
```

- [ ] **Step 4: Run the complete route-planner module and verify GREEN**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-route-green-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_spec5_route_planner.py
```

Expected: `16 passed` and exit code 0.

- [ ] **Step 5: Commit routing metadata**

```powershell
git add -- sonder_runtime/domain/routing/route_planner.py tests/test_spec5_route_planner.py
git commit -m "feat: record provider per routed tier"
```

---

### Task 5: Use One Binding Source in Both Composition Roots

**Files:**
- Modify: `sonder_runtime/bootstrap/app.py:58-94,109-142`
- Modify: `sonder_runtime/bootstrap/container.py:21-65`
- Modify: `sonder_runtime/bootstrap/main.py:41-46`
- Modify: `tests/test_openai_compat_gateway.py:196-210`
- Modify: `tests/production/test_composition_root.py:36-50`
- Create: `tests/test_provider_composition.py`

**Interfaces:**
- Consumes: `provider_bindings_from_env()`, `ProviderBindings.uniform()`, and `build_model_gateway()` from Tasks 1 and 3.
- Produces: `Application.provider_bindings: ProviderBindings`, `RuntimeConfig.provider_bindings: ProviderBindings | None`, and `Runtime.provider_bindings: ProviderBindings`.

- [ ] **Step 1: Write failing composition tests**

Create `tests/test_provider_composition.py`:

```python
from __future__ import annotations

from sonder_runtime.adapters.ollama.gateway import OllamaGateway
from sonder_runtime.adapters.openai_compat.gateway import OpenAICompatibleGateway
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.bootstrap.capabilities import RuntimeCapabilities
from sonder_runtime.bootstrap.container import build_runtime
from sonder_runtime.bootstrap.main import build_config_from_env


_PROVIDER_ENV = (
    "SONDER_MODEL_BACKEND",
    "SONDER_FAST_PROVIDER",
    "SONDER_GENERAL_PROVIDER",
    "SONDER_CODE_PROVIDER",
    "SONDER_REASONING_PROVIDER",
    "SONDER_VISION_PROVIDER",
    "SONDER_EMBEDDING_PROVIDER",
)


def _clear(monkeypatch):
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


def test_legacy_default_and_global_openai_graphs_stay_direct(monkeypatch):
    _clear(monkeypatch)
    bootstrap_app.reset_for_tests()
    assert isinstance(bootstrap_app.build_application().model_gateway, OllamaGateway)

    monkeypatch.setenv("SONDER_MODEL_BACKEND", "openai")
    bootstrap_app.reset_for_tests()
    assert isinstance(
        bootstrap_app.build_application().model_gateway, OpenAICompatibleGateway
    )
    bootstrap_app.reset_for_tests()


def test_mixed_graph_exposes_content_free_bindings(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SONDER_FAST_PROVIDER", "llamacpp")
    monkeypatch.setenv("SONDER_GENERAL_PROVIDER", "llamacpp")
    monkeypatch.setenv("SONDER_EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("SONDER_OPENAI_API_KEY", "must-not-appear")
    bootstrap_app.reset_for_tests()

    application = bootstrap_app.build_application()

    assert isinstance(application.model_gateway, ProviderDispatchGateway)
    projection = application.provider_bindings.status_projection()
    assert projection["tier_providers"]["fast"] == "openai_compatible"
    assert projection["tier_providers"]["code"] == "ollama"
    assert projection["embedding_provider"] == "ollama"
    assert "127.0.0.1" not in repr(projection)
    assert "must-not-appear" not in repr(projection)
    bootstrap_app.reset_for_tests()


def test_spec5_config_freezes_same_bindings_used_by_runtime(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SONDER_FAST_PROVIDER", "openai-compatible")
    config = build_config_from_env("workstation-local")
    runtime = build_runtime(config, RuntimeCapabilities())

    assert runtime.provider_bindings is config.provider_bindings
    assert runtime.provider_bindings.tier_providers["fast"] == "openai_compatible"
    assert isinstance(runtime.model_gateway, ProviderDispatchGateway)
```

- [ ] **Step 2: Run composition tests and verify RED**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-composition-red-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_provider_composition.py
```

Expected: failures report that application/runtime graphs do not expose `provider_bindings` and mixed configuration still returns a direct gateway.

- [ ] **Step 3: Update the legacy application composition root**

In `sonder_runtime/bootstrap/app.py`, import:

```python
from .model_gateways import build_model_gateway
from .provider_bindings import ProviderBindings, provider_bindings_from_env
```

Add the field:

```python
provider_bindings: ProviderBindings
```

Replace `_build_model_gateway` with:

```python
def _build_model_components() -> tuple[ProviderBindings, ModelGateway]:
    bindings = provider_bindings_from_env()
    return bindings, build_model_gateway(bindings)
```

Inside `build_application`, use:

```python
bindings, gateway = _build_model_components()
```

and pass:

```python
provider_bindings=bindings,
model_gateway=gateway,
```

- [ ] **Step 4: Update the SPEC-5 config and composition root**

In `sonder_runtime/bootstrap/container.py`, import `ProviderBindings` and `build_model_gateway`, then extend the dataclasses:

```python
@dataclass(frozen=True)
class RuntimeConfig:
    profile: str = "workstation-local"
    model_backend: str = "ollama"
    sonder_home: str = ""
    provider_bindings: ProviderBindings | None = None


@dataclass(frozen=True)
class Runtime:
    config: RuntimeConfig
    capabilities: RuntimeCapabilities
    provider_bindings: ProviderBindings
    model_gateway: ModelGateway
    events: EventSink
    clock: Clock
```

Replace backend branching in `build_runtime` with:

```python
bindings = config.provider_bindings or ProviderBindings.uniform(config.model_backend)
gateway = build_model_gateway(bindings)
```

and pass `provider_bindings=bindings` to `Runtime`.

In `sonder_runtime/bootstrap/main.py`, update `build_config_from_env`:

```python
def build_config_from_env(profile: str) -> RuntimeConfig:
    bindings = provider_bindings_from_env()
    return RuntimeConfig(
        profile=profile,
        model_backend=bindings.default_generation_provider,
        sonder_home=os.environ.get("SONDER_HOME", ""),
        provider_bindings=bindings,
    )
```

- [ ] **Step 5: Preserve and extend existing graph-selection coverage**

In `tests/test_openai_compat_gateway.py`, clear every environment name in `_PROVIDER_ENV` before asserting the default and global OpenAI-compatible direct gateways. Keep the assertions against the concrete `OllamaGateway` and `OpenAICompatibleGateway` types.

In `tests/production/test_composition_root.py`, add:

```python
def test_provider_status_projection_is_bounded_and_content_free(monkeypatch):
    monkeypatch.setenv("SONDER_FAST_PROVIDER", "openai-compatible")
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://127.0.0.1:18080/private")
    monkeypatch.setenv("SONDER_OPENAI_API_KEY", "secret-value")
    bootstrap_app.reset_for_tests()
    projection = bootstrap_app.build_application().provider_bindings.status_projection()
    rendered = repr(projection)
    assert set(projection) == {
        "default_generation_provider", "tier_providers", "embedding_provider"
    }
    assert "private" not in rendered
    assert "secret-value" not in rendered
    bootstrap_app.reset_for_tests()
```

- [ ] **Step 6: Run composition and legacy gateway tests and verify GREEN**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-composition-green-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_provider_composition.py tests/test_openai_compat_gateway.py tests/production/test_composition_root.py
```

Expected: all selected tests pass with exit code 0.

- [ ] **Step 7: Commit both composition roots**

```powershell
git add -- sonder_runtime/bootstrap/app.py sonder_runtime/bootstrap/container.py sonder_runtime/bootstrap/main.py tests/test_provider_composition.py tests/test_openai_compat_gateway.py tests/production/test_composition_root.py
git commit -m "feat: compose tier-aware local providers"
```

---

### Task 6: Document Configuration and Verify the Repository

**Files:**
- Modify: `docs/wiki/18-model-catalog.md`
- Verify: all files changed in Tasks 1-5

**Interfaces:**
- Consumes: the final environment-variable contract and status projection.
- Produces: operator instructions for the production Bonsai/Prism plus Qwen/Ollama split.

- [ ] **Step 1: Add the operator configuration section**

Append this section to `docs/wiki/18-model-catalog.md`:

````markdown
## Tier-aware local providers

Sonder can keep its standard Ollama lifecycle while routing selected generation tiers to a loopback OpenAI-compatible server such as Prism/llama.cpp. Unset provider bindings inherit `SONDER_MODEL_BACKEND`, so existing single-provider installations do not change.

For the production Bonsai/Qwen split:

```text
SONDER_MODEL_BACKEND=ollama
SONDER_FAST_PROVIDER=openai-compatible
SONDER_GENERAL_PROVIDER=openai-compatible
SONDER_CODE_PROVIDER=ollama
SONDER_REASONING_PROVIDER=ollama
SONDER_EMBEDDING_PROVIDER=ollama
SONDER_OPENAI_BASE_URL=http://127.0.0.1:18080
SONDER_OPENAI_MODEL=sonder-bonsai
```

The dispatcher makes one provider call and never falls through to another provider. Non-loopback OpenAI-compatible endpoints still require explicit cloud consent, and remote Ollama still requires explicit remote-Ollama consent.
````

- [ ] **Step 2: Run formatting and architecture checks**

Run:

```powershell
git diff --check
& .\venv\Scripts\python.exe scripts/check_architecture.py
& .\venv\Scripts\python.exe scripts/check_error_signals.py
```

Expected: all three commands exit 0.

- [ ] **Step 3: Run the targeted provider/routing suite**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-provider-targeted-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt tests/test_provider_bindings.py tests/test_provider_dispatch_gateway.py tests/test_model_gateway_factory.py tests/test_provider_composition.py tests/test_openai_compat_gateway.py tests/test_model_gateway_conformance.py tests/test_spec5_route_planner.py tests/production/test_composition_root.py tests/test_runtime_policy.py tests/test_inference_telemetry.py
```

Expected: exit code 0 with no failures.

- [ ] **Step 4: Run the complete non-network test suite**

Run:

```powershell
$bt = Join-Path ([IO.Path]::GetTempPath()) ("sonder-provider-full-" + [guid]::NewGuid().ToString("N"))
& .\venv\Scripts\python.exe -m pytest -q --basetemp=$bt
```

Expected: exit code 0; tests marked `network` or `model` remain skipped unless explicitly enabled.

- [ ] **Step 5: Review the final diff against the eight acceptance criteria**

Run:

```powershell
git status --short
git diff --stat HEAD~4..HEAD
git diff HEAD~4..HEAD -- sonder_runtime tests docs/wiki/18-model-catalog.md
```

Confirm the diff contains configuration parsing, exact dispatch, per-tier route identity, both composition roots, bounded provider status, tests, and operator docs; confirm it contains no endpoint probe, provider failover, runtime-policy schema edit, model-file operation, or new dependency.

- [ ] **Step 6: Commit operator documentation**

```powershell
git add -- docs/wiki/18-model-catalog.md
git commit -m "docs: configure tier-aware local providers"
```

---

### Task 7: Prove the Live Prism/Ollama Split After Runtime Installation

**Files:**
- Read: `<workspace>/outputs/sonder-model-manifest.json`
- Read: `<workspace>/outputs/sonder-runtime-manifest.json`
- Write evidence: `<workspace>/outputs/sonder-mixed-provider-smoke.json`

**Interfaces:**
- Consumes: verified Prism server executable, verified Bonsai GGUF, live local Ollama, and the implemented provider bindings.
- Produces: one bounded JSON evidence record proving `fast` used Prism, `code` used Ollama, and embeddings used Ollama.

- [ ] **Step 1: Require completed model/runtime evidence before starting a server**

Read both manifests and require:

```text
fast-general-bonsai.status = verified
main-5070-ti.status = verified
embedding.status = verified
deployment.status = verified
smoke.status = passed
```

Refuse the live mixed-provider smoke if any field differs.

- [ ] **Step 2: Start only the verified Prism server on loopback**

Launch the runtime-manifest server executable with the manifest-verified Bonsai path, `--host 127.0.0.1`, `--port 18080`, `--ctx-size 4096`, `--n-gpu-layers 99`, and `--alias sonder-bonsai`. Record the exact process ID and creation time, wait for `/health`, and refuse non-loopback binding.

- [ ] **Step 3: Run one request through each production lane**

Set these variables only in the smoke process:

```text
SONDER_MODEL_BACKEND=ollama
SONDER_FAST_PROVIDER=openai-compatible
SONDER_GENERAL_PROVIDER=openai-compatible
SONDER_CODE_PROVIDER=ollama
SONDER_REASONING_PROVIDER=ollama
SONDER_EMBEDDING_PROVIDER=ollama
SONDER_OPENAI_BASE_URL=http://127.0.0.1:18080
SONDER_OPENAI_MODEL=sonder-bonsai
```

Build one application graph, issue bounded exact-marker prompts to `fast` and `code`, and call `application.model_gateway.embed(["SONDER_EMBED_SMOKE"], context)`. Record response model/tier, token counts, telemetry presence, embedding model, embedding dimension, provider projection, and timestamps. Do not record prompts, generated text beyond exact marker booleans, vectors, endpoint URLs, credentials, or paths.

- [ ] **Step 4: Stop the exact Prism process and verify evidence**

Stop only the process whose ID and creation time match Step 2. Verify the evidence JSON states:

```text
fast.provider = openai_compatible
fast.markerMatched = true
code.provider = ollama
code.markerMatched = true
embedding.provider = ollama
embedding.dimension = 768
```

Then rerun the final model-library audit so the completion report references both the Prism-only smoke and the mixed-provider smoke.
