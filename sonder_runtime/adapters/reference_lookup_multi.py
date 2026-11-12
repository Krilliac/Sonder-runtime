"""Reference lookup on the multi-model text lanes: ``ensemble_answer`` and ``consult``.

``server.py`` is a size-capped control-plane file, so each seam of these lanes
is one call into this module.

* One retrieval per question, shared.  Lookup applies to an ensemble (or a
  consult) when at least one member's answering model (for a tier bound to
  another provider, the model that provider serves) matches the applies
  rule; then every member is sent the same framed user turn (the same text
  and marker code), so the answers stay comparable.  The search terms come
  from the first matching member's model (temperature 0, 80 tokens, no system
  prompt, outside provider-attempt capture, never streamed).  The passage
  budget is computed on the smallest context window among the members.
* Every member answer is cleaned (display-safe text, copied marker lines
  removed) and citation-checked; so is the merged text.
* The synthesiser gets the raw question and the cleaned candidate answers
  only, plus one rule telling it to keep the ``[n]`` tags.  The consult judge
  runs with lookup switched off: it sees the raw question and the cleaned
  member answers, never passages or marker lines.
* One Sources block per call: before the ENSEMBLE footer, or after the consult
  verdict.  Any index or search failure answers bare with the visible
  ``Sources: none (reference lookup unavailable: <reason>)`` line.

Neither lane records an interaction, so nothing here marks one for learning;
the passages exist only inside the member requests.  Code-mode ensembles
(whose output is a bare source file, with nowhere to put Sources) get no
lookup.  When lookup does not apply every seam hands back exactly what the
caller had.

State and threads: a call's shared lookup is owned by the call that created
it and used on that call's thread; a consult hands it to its members through
a ContextVar that is reset when each member returns.  Nothing here executes
model output or calls a tool.
"""
from __future__ import annotations

import contextlib
import functools
from collections.abc import Callable, Iterator, Sequence
from contextvars import ContextVar
from typing import Any

import sonder_runtime.adapters.reference_lookup_lanes as _lanes
import sonder_runtime.adapters.reference_lookup_service as _service
from sonder_runtime.application.chat import reference_lookup as _lookup
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.session.provider_attempts import provider_attempt_scope
from sonder_runtime.domain.context_formatting import rough_token_count
from sonder_runtime.domain.reference_lookup.selection import passage_char_budget

CODE_MODE = "code"
SYNTHESIS_LABEL = "synthesis"
# The ensemble's own floor on a member's output cap.
MIN_MEMBER_NUM_PREDICT = 64

ContextFor = Callable[[str, str], Any]
Member = tuple[str, str]


def _applies(tier: Any, model: Any) -> bool:
    try:
        return bool(_service.applies(_lanes.answering_model(model, tier)).applies)
    except Exception:  # noqa: BLE001  # lookup is a help: a broken rule means no lookup
        return False


def _window(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return _lanes.ASSUMED_NUM_CTX
    return value


class SharedLookup:
    """One question's lookup, shared by every member of one ensemble or consult.

    ``members`` are the (tier, model) pairs that will answer; ``expand_member``
    is the first of them whose model matches the applies rule.  ``bundle`` is
    retrieved once, on the first member call; ``notes`` collects each cleaned
    reply's review under its tier (or ``synthesis``).  Owned by one call on
    one thread; not thread-safe.
    """

    def __init__(self, question: str, members: Sequence[Member], expand_member: Member) -> None:
        self.question = question
        self.members = tuple(members)
        self.expand_member = expand_member
        self.code = _lookup.new_code()
        self.bundle: _lookup.Bundle | None = None
        self.notes: list[tuple[str, _lookup.ReplyReview]] = []
        self.retrievals = 0
        self._windows: dict[Member, Any] = {}

    @property
    def grounded(self) -> bool:
        """True when the members were given passages."""
        return self.bundle is not None and self.bundle.applied and bool(self.bundle.passages)

    def num_ctx(self, ctx_for: ContextFor, tier: str, model: str) -> Any:
        """``ctx_for(tier, model)``, asked once per member; a failure is not remembered."""
        key = (tier, model)
        if key not in self._windows:
            self._windows[key] = ctx_for(tier, model)
        return self._windows[key]

    def _budget(self, ctx_for: ContextFor, num_predict: Any, prompt: Any) -> int:
        windows = []
        for tier, model in self.members:
            try:
                windows.append(_window(self.num_ctx(ctx_for, tier, model)))
            except Exception:  # noqa: BLE001, S112  # that member fails on its own turn, as before
                continue
        try:
            reserve = max(MIN_MEMBER_NUM_PREDICT, int(num_predict))
            return passage_char_budget(min(windows or [_lanes.ASSUMED_NUM_CTX]), reserve,
                                       rough_token_count(prompt))
        except Exception:  # noqa: BLE001  # an unreadable size means no room, never a failed call
            return 0

    def retrieve(self, make_tier_generate: Callable[..., Any], ctx_for: ContextFor,
                 num_predict: Any, prompt: Any) -> _lookup.Bundle:
        """The shared bundle, retrieved on the first call; never raises."""
        if self.bundle is not None:
            return self.bundle
        live = stream_sink.turn_stream()
        if live is not None:
            # Nothing raw, and never the search terms, reaches a live stream.
            live.claim()
        tier, model = self.expand_member

        def expand(text: str) -> Any:
            # Not a provider attempt of the turn, and never streamed.
            with provider_attempt_scope(None, None), stream_sink.armed(None):
                return make_tier_generate(
                    tier, model, "", _lanes.EXPAND_TEMPERATURE, _lanes.EXPAND_NUM_PREDICT,
                    self.num_ctx(ctx_for, tier, model),
                )(text)

        self.retrievals += 1
        self.bundle = _service.retrieve_for(
            self.question, expand, _service.DEFAULT_K, self._budget(ctx_for, num_predict, prompt),
        )
        return self.bundle

    def review(self, label: str, reply: str) -> _lookup.ReplyReview:
        bundle = self.bundle if self.bundle is not None else _lookup.Bundle(
            applied=False, reason=_lookup.LookupReason.NO_MATCH)
        review = _lookup.review_reply(bundle, reply)
        self.notes.append((label, review))
        return review

    def sources(self) -> str:
        """The one Sources block of the call (with every member's warnings), or ``""``."""
        if self.bundle is None:
            return ""
        try:
            cited = frozenset().union(*(review.cited for _label, review in self.notes))
            warnings = [f"{label}: {text}" for label, review in self.notes for text in review.warnings]
            return _lanes.bundle_sources_text(self.bundle, cited, warnings)
        except Exception:  # noqa: BLE001  # the answer stands without its Sources block
            return ""


_SHARED: ContextVar[SharedLookup | None] = ContextVar("sonder_reference_lookup_shared", default=None)
_OFF: ContextVar[bool] = ContextVar("sonder_reference_lookup_off", default=False)


@contextlib.contextmanager
def off() -> Iterator[None]:
    """Run the enclosed calls with no reference lookup (the consult judge)."""
    token = _OFF.set(True)
    try:
        yield
    finally:
        _OFF.reset(token)


def _shared_for(question: Any, members: Sequence[Any]) -> SharedLookup | None:
    text = question.strip() if isinstance(question, str) else ""
    pairs = tuple((str(tier), str(model)) for tier, model in members if model)
    matching = next((pair for pair in pairs if _applies(*pair)), None)
    if not text or matching is None:
        return None
    return SharedLookup(text, pairs, matching)


class EnsembleLookup:
    """The lookup seams of one ``ensemble_answer`` call; inert when ``shared`` is None.

    ``owner`` is False for a consult member: the consult prints Sources once,
    after its verdict.  Holds references only; one call, one thread.
    """

    def __init__(self, shared: SharedLookup | None, *, owner: bool,
                 make_tier_generate: Callable[..., Any], ctx_for: ContextFor,
                 num_predict: Any) -> None:
        self.shared = shared
        self.owner = owner
        self._make = make_tier_generate
        self._ctx_for = ctx_for
        self._num_predict = num_predict

    def num_ctx(self, tier: str, model: str) -> Any:
        """The member's context window: ``ctx_for(tier, model)``."""
        if self.shared is None:
            return self._ctx_for(tier, model)
        return self.shared.num_ctx(self._ctx_for, tier, model)

    def ask(self, tier: str, gen: Callable[..., Any], prompt: Any) -> Any:
        """``gen(prompt)``; under lookup the shared framed turn, and the cleaned reply."""
        shared = self.shared
        if shared is None:
            return gen(prompt)
        bundle = shared.retrieve(self._make, self._ctx_for, self._num_predict, prompt)
        raw: list[Any] = []

        def recording(framed: Any, *args: Any, **kwargs: Any) -> Any:
            reply = gen(framed, *args, **kwargs)
            raw.append(reply)
            return reply

        reply = _lookup.wrap_generate(recording, bundle, shared.code)(prompt)
        if not isinstance(reply, str):
            return reply
        review = shared.review(str(tier), raw[-1] if raw and isinstance(raw[-1], str) else reply)
        return reply if shared.grounded else review.text

    def synthesis(self, build: Callable[..., str]) -> Callable[..., str]:
        """The synthesis prompt builder, told to keep ``[n]`` tags when members had passages."""
        if self.shared is None or not self.shared.grounded:
            return build
        return functools.partial(build, keep_citations=True)

    def merged(self, text: Any) -> Any:
        """The merged answer, cleaned and citation-checked under lookup."""
        if self.shared is None or not isinstance(text, str):
            return text
        return self.shared.review(SYNTHESIS_LABEL, text).text

    def footer(self, footer: str, lead: str = "\n\n") -> str:
        """``footer`` with the call's Sources block before it (owner only)."""
        if self.shared is None or not self.owner:
            return footer
        sources = self.shared.sources()
        return lead + sources + "\n" + footer if sources else footer


def ensemble(prompt: Any, mode: Any, targets: Sequence[Any], num_predict: Any,
             make_tier_generate: Callable[..., Any], ctx_for: ContextFor) -> EnsembleLookup:
    """The lookup seams for one ``ensemble_answer`` call over ``targets`` [(tier, model)].

    ``prompt`` is the caller's raw question (searched); ``ctx_for(tier, model)``
    is the member's context window exactly as the ensemble computes it.
    """
    shared, owner = None, False
    try:
        if not _OFF.get() and str(mode or "").strip().lower() != CODE_MODE:
            shared = _SHARED.get()
            if shared is None:
                shared, owner = _shared_for(prompt, targets), True
    except Exception:  # noqa: BLE001  # fail open: the ensemble runs as before
        shared, owner = None, False
    return EnsembleLookup(shared, owner=owner, make_tier_generate=make_tier_generate,
                          ctx_for=ctx_for, num_predict=num_predict)


class ConsultLookup:
    """The lookup of one consult: its members share ``shared``; the judge runs without lookup."""

    def __init__(self, shared: SharedLookup) -> None:
        self.shared = shared

    def member(self, ask: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
        """``ask`` run with the shared lookup in force."""
        def run(prompt: Any, tier: Any) -> Any:
            token = _SHARED.set(self.shared)
            try:
                return ask(prompt, tier)
            finally:
                _SHARED.reset(token)

        return run

    def judge(self, ask: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
        """``ask`` run with lookup off, so the judge never sees passages or marker lines."""
        def run(prompt: Any, tier: Any) -> Any:
            with off():
                return ask(prompt, tier)

        return run

    def sources(self) -> str:
        return self.shared.sources()


def consult_scope(targets_for: Callable[[str], Any]) -> Callable[[Any, Sequence[Any]], ConsultLookup | None]:
    """The ``lookup`` hook of ``consult.consult``: ``hook(question, tiers)``.

    ``targets_for(tier)`` resolves a tier the way each consult member's
    ensemble does (``(targets, unknown)``).  The hook returns None, and the
    consult runs as before, when no member's model matches.
    """
    def hook(question: Any, tiers: Sequence[Any]) -> ConsultLookup | None:
        try:
            if _OFF.get() or _SHARED.get() is not None:
                return None
            members = []
            for tier in tiers:
                found = targets_for(tier)[0]
                if found:
                    members.append(found[0])
            shared = _shared_for(question, members)
        except Exception:  # noqa: BLE001  # fail open: the consult runs as before
            return None
        return ConsultLookup(shared) if shared is not None else None

    return hook


__all__ = [
    "CODE_MODE",
    "ConsultLookup",
    "EnsembleLookup",
    "SharedLookup",
    "consult_scope",
    "ensemble",
    "off",
]
