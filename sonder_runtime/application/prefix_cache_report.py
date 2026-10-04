"""Join logical prefix-manifest decisions with provider-measured cache reuse.

``PrefixManifestCache`` says whether the *logical* system prefix of a request
matches one seen before (``hit``) or why not (``cold_start``,
``identity_changed``, ``version_changed``, ``prefix_changed``).  A provider
such as Ollama separately reports how many prompt tokens it actually reused
from its KV cache (``prompt_eval_cached_count``).  Neither alone explains a
slow first token: a logical hit can still be a provider miss (another prompt
evicted the slot), and a provider miss can be caused by one volatile section
near the top of the prompt.  This module binds the two into one record, e.g.
``prefix_changed[emotions], 2194/2657 cached``.

It also owns the local system-prompt section order.  Measured on a local
Ollama (see the change that introduced this module): any edit to a section
invalidates the provider cache for every token after it, so request-scoped
instructions sit before the volatile emotion-vector and active-goal sections.

Integration-neutral: no provider, transport, metrics, or environment access.
"""
from __future__ import annotations

import hashlib
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Mapping

from ..domain.prompt_composition import join_system_parts
from .context_manifests import ContextRecord, PrefixManifestCache

# Bump when the section order below changes so logical manifests report
# ``version_changed`` instead of an unexplained ``prefix_changed``.
LOCAL_SYSTEM_LAYOUT_VERSION = "local-system/3"
UNSTRUCTURED_LAYOUT_VERSION = "unstructured/1"
# Stable first, volatile last: a provider KV cache is a strict prefix match.
LOCAL_SYSTEM_SECTION_ORDER = ("identity", "profile", "playbook", "request", "emotions", "goal")
VOLATILE_SECTIONS = frozenset({"emotions", "goal"})
PREFIX_REASONS = frozenset({
    "hit", "cold_start", "identity_changed", "version_changed", "prefix_changed",
})


def _text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SystemSectionRegistry:
    """Bounded map from a composed system prompt to the sections it came from.

    The composed text is what reaches the model and every downstream caller;
    the sections are only needed later to explain a cache decision.  Keying by
    the text digest avoids widening any existing call signature.
    """

    def __init__(self, *, max_entries: int = 64) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self._max = max_entries
        self._values: OrderedDict[str, tuple[tuple[str, str], ...]] = OrderedDict()
        self._lock = RLock()

    def remember(self, text: str, sections) -> None:
        present = tuple((str(name), part) for name, part in sections if part)
        key = _text_key(text)
        with self._lock:
            self._values[key] = present
            self._values.move_to_end(key)
            while len(self._values) > self._max:
                self._values.popitem(last=False)

    def sections(self, text: str) -> tuple[tuple[str, str], ...] | None:
        with self._lock:
            return self._values.get(_text_key(text))


DEFAULT_SECTIONS = SystemSectionRegistry()


def compose_local_system(identity, profile, emotions, goal, request, *,
                         playbook="",
                         registry: SystemSectionRegistry | None = None) -> str:
    """Join the local system-prompt sections in cache-friendly order.

    The text of every section is unchanged; only its position is policy.
    """
    sections = (
        ("identity", identity), ("profile", profile), ("playbook", playbook), ("request", request),
        ("emotions", emotions), ("goal", goal),
    )
    text = join_system_parts(*(part for _name, part in sections))
    (registry or DEFAULT_SECTIONS).remember(text, sections)
    return text


@dataclass(frozen=True)
class PrefixCacheJoin:
    """One request's logical prefix decision next to the provider's reuse."""

    provider: str
    result: str
    reason: str
    changed_sections: tuple[str, ...]
    prompt_tokens: int | None
    cached_tokens: int | None

    @property
    def measured(self) -> bool:
        return self.prompt_tokens is not None and self.cached_tokens is not None

    @property
    def provider_reuse(self) -> str:
        if not self.measured:
            return "unmeasured"
        return "none" if self.cached_tokens == 0 else "partial"

    @property
    def cached_ratio(self) -> float | None:
        if not self.measured or not self.prompt_tokens:
            return None
        return self.cached_tokens / self.prompt_tokens

    def summary(self) -> str:
        reason = self.reason
        if self.changed_sections:
            reason += "[%s]" % ",".join(self.changed_sections)
        if not self.measured:
            return "%s, provider cache unmeasured" % reason
        return "%s, %d/%d cached" % (reason, self.cached_tokens, self.prompt_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "result": self.result, "reason": self.reason,
            "changed_sections": list(self.changed_sections),
            "prompt_tokens": self.prompt_tokens, "cached_tokens": self.cached_tokens,
            "provider_reuse": self.provider_reuse, "summary": self.summary(),
        }


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class ChatPrefixCacheMonitor:
    """Per (provider, model) logical manifests joined with provider counts."""

    def __init__(self, *, max_models: int = 16, history: int = 64,
                 sections: SystemSectionRegistry | None = None) -> None:
        if isinstance(max_models, bool) or not isinstance(max_models, int) or max_models < 1:
            raise ValueError("max_models must be a positive integer")
        self._max_models = max_models
        self._sections = sections or DEFAULT_SECTIONS
        self._models: OrderedDict[tuple[str, str], list] = OrderedDict()
        self._recent: deque[PrefixCacheJoin] = deque(maxlen=history)
        self._counts: Counter[tuple[str, str]] = Counter()
        self._lock = RLock()

    def _records(self, system_text: str):
        sections = self._sections.sections(system_text) if system_text else None
        if sections is None:
            named = (("system", system_text),) if system_text else ()
            version = UNSTRUCTURED_LAYOUT_VERSION
        else:
            named, version = sections, LOCAL_SYSTEM_LAYOUT_VERSION
        records = tuple(
            ContextRecord(
                item_id=name, section="%02d-%s" % (index, name), content=content,
                source="system_prompt", ordinal=index, stable=True,
            )
            for index, (name, content) in enumerate(named)
        )
        return records, version

    def observe(self, system_text: str, *, model: str, provider_id: str,
                telemetry: Any = None) -> PrefixCacheJoin:
        records, version = self._records(system_text or "")
        digests = {record.item_id: record.content_digest for record in records}
        order = [record.item_id for record in records]
        key = (str(provider_id or ""), str(model or ""))
        with self._lock:
            entry = self._models.get(key)
            if entry is None:
                entry = [PrefixManifestCache(max_entries=8), None]
                self._models[key] = entry
            self._models.move_to_end(key)
            while len(self._models) > self._max_models:
                self._models.popitem(last=False)
            _manifest, observation = entry[0].resolve_observed(
                records, version=version, model=key[1], provider_id=key[0],
            )
            previous: Mapping[str, str] | None = entry[1]
            entry[1] = digests
            changed: tuple[str, ...] = ()
            if previous is not None:
                names = order + [name for name in previous if name not in digests]
                changed = tuple(
                    name for name in names if previous.get(name) != digests.get(name)
                )
            prompt = _count(getattr(telemetry, "prompt_tokens", None))
            cached = _count(getattr(telemetry, "prompt_cached_tokens", None))
            if prompt is None or cached is None or cached > prompt:
                cached = None  # never manufacture a provider cache result
            join = PrefixCacheJoin(
                key[0], observation.result, observation.reason, changed, prompt, cached,
            )
            self._recent.append(join)
            self._counts[(join.reason, join.provider_reuse)] += 1
            return join

    def recent(self) -> tuple[PrefixCacheJoin, ...]:
        with self._lock:
            return tuple(self._recent)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {"%s/%s" % pair: n for pair, n in sorted(self._counts.items())}


DEFAULT_MONITOR = ChatPrefixCacheMonitor()


def prefill_payload(model: str, system: str, *, options: Mapping[str, Any],
                    keep_alive: Any) -> dict[str, Any]:
    """A one-token chat that leaves ``system`` in the provider's KV cache.

    ``options`` must be the ones the real turn will send (``num_ctx`` in
    particular): Ollama reloads the runner, discarding the cache, when the
    runner-shaping options differ.
    """
    return {
        "model": model,
        "messages": [{"role": "system", "content": system}],
        "stream": False,
        "keep_alive": keep_alive,
        "options": {**dict(options), "num_predict": 1},
    }


def prewarm_request(model: str, keep_alive: Any,
                    build_prefill: Callable[[], tuple[str, Mapping[str, Any]]]):
    """``(path, payload)`` for a prewarm: prefix prefill, else weight-only load.

    Building the prefill reads the profile, goal store and model metadata; any
    failure there degrades to the historical weight-only load, never to no
    prewarm at all.
    """
    try:
        system, options = build_prefill()
    except Exception:
        system, options = "", None
    if system and isinstance(options, Mapping):
        return "/api/chat", prefill_payload(model, system, options=options, keep_alive=keep_alive)
    return "/api/generate", {"model": model, "keep_alive": keep_alive}


__all__ = [
    "LOCAL_SYSTEM_LAYOUT_VERSION", "LOCAL_SYSTEM_SECTION_ORDER", "VOLATILE_SECTIONS",
    "PREFIX_REASONS", "SystemSectionRegistry", "DEFAULT_SECTIONS", "compose_local_system",
    "PrefixCacheJoin", "ChatPrefixCacheMonitor", "DEFAULT_MONITOR",
    "prefill_payload", "prewarm_request",
]
