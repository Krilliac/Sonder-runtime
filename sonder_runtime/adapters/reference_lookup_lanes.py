"""Reference lookup on the single-model chat lanes.

``server.py`` is a size-capped control-plane file, so each seam of the MCP
chat tool (by tier and by ``model_override``) and of the HTTP chat route
(learning and by-name) calls one function here:

* ``lookup_turn(model)``: whether automatic lookup applies to the model of the
  rung about to answer.  The decision is made once per rung and remembered for
  the rest of that rung's seams.
* ``system_for(model, system, caller_system="")``: the system prompt for that
  rung.  A lookup model is sent none of Sonder's own (persona, playbook, trace
  instructions, tool guidance), so its Modelfile house rules stay in force; a
  system message the caller supplied explicitly is kept and sent as is.  The
  MCP chat tool passes its ``system`` argument; the HTTP route has no caller
  system message to pass (``_history_from_messages`` drops ``system`` role
  messages on every turn, lookup or not).
* ``chat_generate(make_generate, question, model, system, ...)``: the generate
  callable for the rung.  For a lookup model it retrieves passages once per
  turn (asking the answering model for search terms first) and frames the user
  turn through ``wrap_generate``, so the stored task stays the raw question and
  callers store the cleaned reply.  Only a lane that passes ``text_only=True``
  gets lookup; any other lane is called through unchanged.
* ``mark_learning_skip(conn, interaction_id)``: marks a turn that was answered
  with passages so lesson distillation and the training export skip it.
* ``with_sources(text)`` (MCP) and ``finish_http(content, receipt)`` (HTTP):
  the Sources block for the text returned to the caller, added after storage
  and session capture so it is never stored.  The HTTP receipt entry also says
  that the runtime system prompt was not sent.
* ``replaced(text)``: the turn's reply was replaced by another text (the
  web-denial guard), so the lookup state is dropped and that text gets no
  Sources.  ``take_turn()`` / ``restore_turn(record)`` let a caller that runs
  several candidate turns (Spanda sampling) keep each candidate's state and
  put back the one whose reply it sends.

For any other model each function hands back exactly what the caller had: the
same ``make_generate(...)`` call, the same system prompt, the same text.

A rung bound to a non-Ollama provider is judged by the model that provider
serves (``answering_model``, the same reading the tool-lane guard uses), not
by the rung's Ollama name; an unreadable provider configuration means no
lookup.

State and threads: a turn's lookup state lives in a ContextVar, so every
thread and every copied context has its own; a turn never sees another
thread's passages.  The state object is owned by the turn that created it and
is only mutated by that turn.  Nothing here executes model output or calls a
tool, and no index or search failure reaches the caller: the turn proceeds as
a bare answer that says why.
"""
from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Callable, Collection, Iterator, Mapping, MutableMapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import sonder_runtime.adapters.memory_store as _memory_store
import sonder_runtime.adapters.reference_lookup_service as _service
import sonder_runtime.adapters.reference_lookup_state as _state
import sonder_runtime.adapters.tool_lane_guard as _guard
from sonder_runtime.application.chat import provider_bridge as _provider_bridge
from sonder_runtime.application.chat import reference_lookup as _lookup
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.session.provider_attempts import provider_attempt_scope
from sonder_runtime.domain.context_formatting import rough_token_count
from sonder_runtime.domain.reference_lookup.answer_check import (
    format_sources,
    format_unavailable,
)
from sonder_runtime.domain.reference_lookup.sanitize import one_line, sanitize
from sonder_runtime.domain.reference_lookup.selection import passage_char_budget

_logger = logging.getLogger("sonder.reference_lookup")

# The search-terms call: deterministic, small, no system prompt, no model thinking.
EXPAND_TEMPERATURE = 0.0
EXPAND_NUM_PREDICT = 80
# Context window assumed for the budget when the call passes none (a hosted or
# bridged rung that uses its provider's default window).
ASSUMED_NUM_CTX = 4096
WARNING_PREFIX = "Warning: "
LEARNING_SKIP_REASON = _memory_store.LEARNING_SKIP_REFERENCE_LOOKUP
RECEIPT_KEY = "reference_lookup"
# The receipt says a lookup turn was sent none of the runtime's system prompt.
RECEIPT_SYSTEM_PROMPT_KEY = "runtime_system_prompt_sent"

_FAILED = frozenset({_lookup.LookupReason.INDEX_UNAVAILABLE, _lookup.LookupReason.SEARCH_FAILED})
_NONE_TEXT = {
    _lookup.LookupReason.NO_MATCH: _lookup.NO_MATCH_WARNING,
    _lookup.LookupReason.NO_ROOM: _lookup.NO_ROOM_WARNING,
    _lookup.LookupReason.EMPTY_QUESTION: "the question was empty",
    _lookup.LookupReason.DISABLED: "reference lookup is off",
}


@dataclass
class TurnLookup:
    """Lookup state of one rung of one chat turn.

    ``bound`` is set once a generate callable was built for the rung; a bound
    record is never reused, so passages cannot carry over to another rung or
    turn.  ``caller_system`` is the system message the caller supplied, the
    only one a lookup model is sent.  ``bundle`` and ``review`` are filled in
    when the model is called.
    """

    model: str
    applies: bool
    bound: bool = False
    tier: str = ""
    caller_system: str = ""
    bundle: _lookup.Bundle | None = None
    review: _lookup.ReplyReview | None = None

    @property
    def grounded(self) -> bool:
        """True when the model was given passages."""
        return self.bundle is not None and self.bundle.applied and bool(self.bundle.passages)


_TURN: ContextVar[TurnLookup | None] = ContextVar("sonder_reference_lookup_turn", default=None)


def answering_model(model: Any, tier: Any = None) -> str:
    """The model that answers ``model``'s call: on a tier bound to another provider, the one it serves.

    ``tier`` is the tier label; without one the rung bound on this thread (if
    any) is used.  ``""`` when the provider configuration cannot be read.
    """
    name = str(model or "")
    if tier is None:
        rung = _provider_bridge.active_rung()
        if rung is None:
            return name
        tier = rung.tier
    try:
        served = _guard.served_model(tier)
    except Exception:  # noqa: BLE001  # an unknown served model gets no lookup
        return ""
    return served[1] if served is not None else name


def _record(model: Any) -> TurnLookup:
    """The current rung's record for ``model``: the unbound one in force, else a new one.

    A record is reused only for the same model name and the same binding (the
    bound provider tier, or none for an Ollama rung), because the decision
    depends on both.
    """
    name = str(model or "")
    rung = _provider_bridge.active_rung()
    tier = str(rung.tier) if rung is not None else ""
    current = _TURN.get()
    if current is not None and not current.bound and current.model == name and current.tier == tier:
        return current
    try:
        applies = bool(_service.applies(answering_model(name)).applies)
    except Exception:  # noqa: BLE001  # lookup is a help: a broken rule means no lookup
        applies = False
    record = TurnLookup(name, applies, tier=tier)
    _TURN.set(record)
    return record


def _take() -> TurnLookup | None:
    record = _TURN.get()
    _TURN.set(None)
    return record


@contextlib.contextmanager
def own_turn() -> Iterator[None]:
    """Run one chat turn with lookup state of its own.

    On entry the enclosing turn's state (if any) is set aside and the turn
    starts with none; on exit, normal or by an exception, the enclosing state
    is put back and whatever the turn left is dropped.  A turn run inside
    another (``/train`` practice inside an HTTP turn, a loop action) can
    therefore neither take nor leave the enclosing turn's lookup state.
    """
    token = _TURN.set(None)
    try:
        yield
    finally:
        _TURN.reset(token)


def turn_scoped(function: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate a chat turn's implementation so it runs under ``own_turn``."""
    @functools.wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        with own_turn():
            return function(*args, **kwargs)

    return run


def reset_turn() -> None:
    """Forget any lookup state before a new turn starts."""
    _TURN.set(None)


def take_turn() -> TurnLookup | None:
    """The current lookup state, removed from this context (``restore_turn`` puts it back)."""
    return _take()


def restore_turn(record: TurnLookup | None) -> None:
    """Make ``record`` (from ``take_turn``) the current lookup state again."""
    _TURN.set(record if isinstance(record, TurnLookup) else None)


def replaced(text: Any) -> Any:
    """``text`` unchanged, with the turn's lookup state dropped: a replacement reply has no Sources."""
    _TURN.set(None)
    return text


def lookup_turn(model: Any) -> bool:
    """Whether automatic lookup applies to the rung answering with ``model``."""
    return _record(model).applies


def system_for(model: Any, system: Any, caller_system: Any = "") -> Any:
    """``system`` unchanged, or for a lookup model only ``caller_system``.

    ``system`` is Sonder's composed system prompt; ``caller_system`` is the part
    the caller supplied explicitly.  A lookup model is sent the caller's text as
    is and none of Sonder's own, so its Modelfile rules stay in force.
    """
    record = _record(model)
    if not record.applies:
        return system
    record.caller_system = caller_system if isinstance(caller_system, str) else ""
    return record.caller_system


class LookupGenerate:
    """A generate callable that answers one lookup turn.

    Owns nothing but references: ``inner`` is the rung's generate callable
    (built with only the caller's system message), ``record`` the turn's state.  Usage and
    response metadata are read through to ``inner`` live, and the provider
    ceiling (``num_predict_override``) is written to it, so callers that read
    or set those attributes see the real generator.  Not thread-safe: one turn
    calls it on one thread.
    """

    def __init__(self, inner: Callable[..., Any], record: TurnLookup, question: str,
                 expander: Callable[[], Callable[..., Any]], *, num_ctx: Any,
                 num_predict: Any) -> None:
        self._inner = inner
        self._record = record
        self._question = question
        self._expander = expander
        self._num_ctx = num_ctx
        self._num_predict = num_predict

    @property
    def num_predict_override(self) -> Any:
        return getattr(self._inner, "num_predict_override", None)

    @num_predict_override.setter
    def num_predict_override(self, value: Any) -> None:
        self._inner.num_predict_override = value

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def _expand(self, prompt: str) -> str:
        # Not a provider attempt of the turn, and never streamed to the client.
        with provider_attempt_scope(None, None), stream_sink.armed(None):
            return self._expander()(prompt)

    def _budget(self, prompt: Any, history: Any) -> int:
        """Passage characters that fit: the caller's system message, history and prompt are counted."""
        try:
            num_ctx = self._num_ctx
            if isinstance(num_ctx, bool) or not isinstance(num_ctx, int) or num_ctx <= 0:
                num_ctx = ASSUMED_NUM_CTX
            used = rough_token_count(self._record.caller_system) + rough_token_count(prompt) + sum(
                rough_token_count(message.get("content"))
                for message in (history or ()) if isinstance(message, Mapping)
            )
            return passage_char_budget(num_ctx, int(self._num_predict or 0), used)
        except Exception:  # noqa: BLE001  # an unreadable size means no room, never a failed turn
            return 0

    def __call__(self, prompt: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._respond(prompt, *args, **kwargs)
        except BaseException:
            # A failed or refused call leaves no lookup state behind: the turn
            # has no reply to carry Sources, and no later turn may pick it up.
            if _TURN.get() is self._record:
                _TURN.set(None)
            raise

    def _respond(self, prompt: Any, *args: Any, **kwargs: Any) -> Any:
        live = stream_sink.turn_stream()
        if live is not None:
            # Claim the turn's stream before any model call: nothing raw (and
            # never the search terms) is forwarded; the cleaned text goes whole.
            live.claim()
        history = args[0] if args else kwargs.get("history")
        bundle = _service.retrieve_for(
            self._question, self._expand, _service.DEFAULT_K, self._budget(prompt, history),
        )
        self._record.bundle = bundle
        raw: list[Any] = []

        def recording(framed: Any, *call_args: Any, **call_kwargs: Any) -> Any:
            reply = self._inner(framed, *call_args, **call_kwargs)
            raw.append(reply)
            return reply

        reply = _lookup.wrap_generate(recording, bundle, _lookup.new_code())(prompt, *args, **kwargs)
        if not isinstance(reply, str):
            return reply
        review = _lookup.review_reply(bundle, raw[-1] if raw and isinstance(raw[-1], str) else reply)
        self._record.review = review
        # A framed turn returns wrap_generate's cleaned reply; a bare one is
        # cleaned here, so a lookup model's reply is always display-safe.
        return reply if self._record.grounded else review.text


# Options that only make sense for a call that thinks.
_THINKING_ONLY_OPTIONS = frozenset({"reasoning_continuation", "reasoning_total_tokens"})


def _thinking_was_needed(exc: BaseException) -> bool:
    """True when a no-thinking call failed because of the option itself.

    Either the reply came back empty (the model thought anyway: its route
    dropped or ignored the option) or the request was refused (HTTP 400: the
    model rejects the option and this route does not retry without it).
    """
    kind = getattr(exc, "kind", None)
    return kind == "empty_response" or (kind == "http" and getattr(exc, "status", None) == 400)


def terms_generate(make: Callable[..., Any], *target: Any, **options: Any) -> Any:
    """``make(*target, "", ...)`` for the search-terms call, with model thinking off.

    ``target`` is ``(model, num_ctx)``, after the tier for a tier-bound factory.
    The reply is one line of query words, so reasoning only delays it and can use
    up the reply budget.  Never worse than the call as it was:

    * a hosted model's provider owns thinking and the factory refuses the option
      for it (``ValueError``): that target gets the plain call;
    * a call that comes back empty or refused because of the option (see
      ``_thinking_was_needed``) is asked again once, as the plain call.
    """
    *head, num_ctx = target
    args = (*head, "", EXPAND_TEMPERATURE, EXPAND_NUM_PREDICT, num_ctx)
    plain_options = {**options, "text_only": True}
    quick_options = {key: value for key, value in plain_options.items()
                     if key not in _THINKING_ONLY_OPTIONS}
    try:
        quick = make(*args, **{**quick_options, "think": False})
    except ValueError:
        return make(*args, **plain_options)

    def generate(prompt: Any, *call_args: Any, **call_kwargs: Any) -> Any:
        try:
            return quick(prompt, *call_args, **call_kwargs)
        except Exception as exc:  # noqa: BLE001  # re-raised unless the option itself was the cause
            if not _thinking_was_needed(exc):
                raise
        return make(*args, **plain_options)(prompt, *call_args, **call_kwargs)

    return generate


def chat_generate(make_generate: Callable[..., Any], question: Any, model: Any, system: Any,
                  temperature: Any, num_predict: Any, num_ctx: Any, **options: Any) -> Any:
    """``make_generate(model, system, temperature, num_predict, num_ctx, **options)``,
    wrapped for reference lookup when it applies to ``model``.

    Lookup runs only on a lane that declared itself text-only
    (``text_only=True`` in ``options``).  Any other lane gets the plain call:
    no lookup record, no retrieval and no search-terms call, so nothing is
    sent ahead of a call the generation check is about to refuse.
    """
    if options.get("text_only") is not True:
        return make_generate(model, system, temperature, num_predict, num_ctx, **options)
    record = _record(model)
    record.bound = True
    if not record.applies:
        return make_generate(model, system, temperature, num_predict, num_ctx, **options)
    # Only the caller's own system message (kept by ``system_for``), never Sonder's.
    # ``options`` (including the lane's ``text_only``) reach the framed call as given.
    inner = make_generate(model, record.caller_system, temperature, num_predict, num_ctx, **options)
    expand_options = {key: value for key, value in options.items() if key != "text_only"}

    def expander() -> Callable[..., Any]:
        # The search-terms reply is only parsed into query words: a text call.
        return terms_generate(make_generate, model, num_ctx, **expand_options)

    return LookupGenerate(inner, record, question if isinstance(question, str) else "", expander,
                          num_ctx=num_ctx, num_predict=num_predict)


def mark_learning_skip(conn: Any, interaction_id: Any) -> bool:
    """Mark the current turn's interaction for distillation and export to skip, if it had passages."""
    record = _TURN.get()
    if record is None or not record.grounded or not interaction_id:
        return False
    try:
        return bool(_memory_store.exclude_interaction_from_learning(conn, interaction_id, LEARNING_SKIP_REASON))
    except Exception as exc:  # noqa: BLE001  # the answer stands; the miss is logged and kept for status
        detail = sanitize(f"learning skip marker not stored: {type(exc).__name__}: {exc}")
        _state.set_last_error(detail)
        _logger.warning("reference lookup: %s", detail)
        return False


def bundle_sources_text(bundle: _lookup.Bundle, cited: Collection[int] = (),
                        warnings: Sequence[str] = ()) -> str:
    """The Sources block for ``bundle``: its passages (``cited`` ones starred) or
    why there are none, then the bundle's warnings and ``warnings``."""
    shown = ""
    if bundle.applied and bundle.passages:
        head = format_sources(bundle.passages, cited)
    elif bundle.reason in _FAILED:
        head = format_unavailable(bundle.reason_text)
    else:
        shown = _NONE_TEXT.get(bundle.reason, bundle.reason_text)
        head = f"Sources: none ({one_line(shown)})"
    lines = [*bundle.warnings, *warnings]
    return "\n".join([head, *(WARNING_PREFIX + one_line(text) for text in lines if text and text != shown)])


def sources_text(record: TurnLookup) -> str:
    """The Sources block (and any warnings) for a lookup turn's returned text."""
    bundle = record.bundle
    if bundle is None:
        return ""
    review = record.review
    return bundle_sources_text(bundle, review.cited if review is not None else (),
                               review.warnings if review is not None else ())


def receipt_entry(record: TurnLookup) -> dict[str, Any]:
    """The receipt's ``reference_lookup`` object for a lookup turn.

    A lookup model is never sent the runtime's system prompt (only a system
    message the caller supplied), grounded or not, and the entry says so.
    """
    bundle = record.bundle
    if record.grounded and bundle is not None:
        entry: dict[str, Any] = {"applied": True, "passages": len(bundle.passages)}
    else:
        entry = {"applied": False, "reason": bundle.reason_text if bundle is not None else ""}
    entry[RECEIPT_SYSTEM_PROMPT_KEY] = False
    return entry


def _finished(record: TurnLookup | None) -> bool:
    return record is not None and record.applies and record.bundle is not None


def with_sources(text: Any) -> Any:
    """The MCP reply with the turn's Sources block appended (call after storage and capture)."""
    record = _take()
    if not _finished(record) or not isinstance(text, str):
        return text
    try:
        return text + "\n\n" + sources_text(record)
    except Exception:  # noqa: BLE001
        return text


def finish_http(content: Any, receipt: Any) -> Any:
    """The HTTP reply with the Sources block, and the receipt's ``reference_lookup`` entry."""
    record = _take()
    if not _finished(record) or not isinstance(content, str):
        return content
    try:
        if isinstance(receipt, MutableMapping):
            receipt[RECEIPT_KEY] = receipt_entry(record)
        return content + "\n\n" + sources_text(record)
    except Exception:  # noqa: BLE001
        return content


__all__ = [
    "ASSUMED_NUM_CTX",
    "EXPAND_NUM_PREDICT",
    "EXPAND_TEMPERATURE",
    "LEARNING_SKIP_REASON",
    "RECEIPT_KEY",
    "RECEIPT_SYSTEM_PROMPT_KEY",
    "LookupGenerate",
    "TurnLookup",
    "answering_model",
    "bundle_sources_text",
    "chat_generate",
    "finish_http",
    "lookup_turn",
    "mark_learning_skip",
    "own_turn",
    "receipt_entry",
    "replaced",
    "reset_turn",
    "restore_turn",
    "sources_text",
    "system_for",
    "take_turn",
    "terms_generate",
    "turn_scoped",
    "with_sources",
]
