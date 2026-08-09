"""Provider-neutral classification of model context-overflow failures.

A model gateway sees one thing when a prompt does not fit: a failed request
carrying a vendor-worded error body. Different servers word it differently
("context length exceeded", "maximum context length is 8192 tokens", "prompt is
too long"), attach different HTTP statuses, and sometimes attach a *misleading*
status - a proxy in front of a model server will happily return 429 for a
request that actually overflowed. Reacting to the status alone therefore either
misses real overflows or retries things that will never succeed.

This module answers exactly one question, from the error text alone:

    is this failure a context overflow that a smaller prompt could fix?

Design rules, all of which the tests pin:

* **Bounded work.** The candidate text is normalized inside a hard
  ``NORMALIZE_MAX_BYTES`` UTF-8 budget before any matching runs, so a hostile or
  merely enormous error body cannot turn classification into a scanning cost.
  Every regex is anchor-free but length-bounded - no unbounded ``.*``.
* **Conservative.** The default answer is "not an overflow". Positive evidence
  must be an exact known phrase or a bounded pattern; nothing is inferred.
* **Typed.** Callers get a :class:`ContextOverflowMatch`, not a bare bool, so a
  retry decision and an activity record can both cite the same evidence.
* **Status is supporting evidence only.** A status never makes a match and
  never breaks one. It is carried on the result for logging.
* **Explicit negative controls.** Failures that merely *look* adjacent - a
  request body rejected by a proxy, a device out of memory, a rate limit, a
  missing model - are recognised by name so they can be reported as
  deliberately-not-overflow rather than as "unknown".

Clean-room: written against the observable wire behaviour of OpenAI-compatible
and Ollama/llama.cpp style error bodies. No vendor source is reproduced here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Hard ceiling on the text examined. Error bodies upstream are already clipped,
# but this module must be safe to call on anything, so it re-bounds its own
# input rather than trusting the caller's clipping.
NORMALIZE_MAX_BYTES = 4096

# Machine-stable reasons. Callers branch on these, so they are part of the API.
REASON_NONE = ""
REASON_CONTEXT_PHRASE = "context_overflow_phrase"
REASON_CONTEXT_LIMIT = "context_overflow_limit"

CONTROL_NONE = ""
CONTROL_BODY_TOO_LARGE = "body_too_large"
CONTROL_DEVICE_OUT_OF_MEMORY = "device_out_of_memory"
CONTROL_RATE_LIMIT = "rate_limit"
CONTROL_MODEL_MISSING = "model_missing"

# Controls that veto a retry outright. Compacting the prompt cannot fix any of
# them: a proxy-rejected body is a transport limit, a device out of memory is an
# allocator failure, and a missing model will stay missing at any prompt size.
_VETO_CONTROLS = frozenset({
    CONTROL_BODY_TOO_LARGE,
    CONTROL_DEVICE_OUT_OF_MEMORY,
    CONTROL_MODEL_MISSING,
})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ContextOverflowMatch:
    """Typed, conservative verdict for one failed model call.

    ``overflow`` is the only field a retry gate should branch on. The rest exist
    so the decision can be explained: ``reason`` says which family of evidence
    fired, ``evidence`` is the bounded phrase that matched, ``control`` names the
    negative control that fired (whether or not it vetoed), and ``status`` is the
    HTTP status carried along purely as supporting context.
    """

    overflow: bool
    reason: str = REASON_NONE
    evidence: str = ""
    control: str = CONTROL_NONE
    status: int | None = None
    truncated: bool = False

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.overflow

    @property
    def vetoed(self) -> bool:
        """True when positive evidence was present but a control overrode it."""
        return bool(self.reason) and not self.overflow


def normalize(value, *, max_bytes: int = NORMALIZE_MAX_BYTES) -> tuple[str, bool]:
    """Fold arbitrary error text into a bounded, match-friendly form.

    Returns ``(normalized, truncated)``. The input is stringified, clipped to
    ``max_bytes`` UTF-8 bytes on a codepoint boundary, lowercased, and reduced to
    single-space-separated alphanumeric runs. That last step is what makes one
    phrase table cover ``context_length_exceeded``, ``Context Length Exceeded``,
    and ``"context length exceeded."`` without a pattern per spelling.
    """
    if value is None:
        return "", False
    text = value if isinstance(value, str) else str(value)
    encoded = text.encode("utf-8", errors="replace")
    truncated = len(encoded) > max_bytes
    if truncated:
        # errors="ignore" drops a codepoint split by the byte cut rather than
        # emitting a replacement char that could break an adjacent phrase.
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")
    folded = _NON_ALNUM.sub(" ", text.casefold()).strip()
    return folded, truncated


# Exact phrases, written in normalized form (lowercase, single spaces). Each one
# is a statement that the *prompt did not fit*, not merely that something was
# large. Anything vaguer belongs in the bounded patterns below or nowhere.
_OVERFLOW_PHRASES: tuple[str, ...] = (
    "context length exceeded",
    "context window exceeded",
    "context size exceeded",
    "exceeds context length",
    "exceeds context window",
    "exceeds the context window",
    "exceeds the models context length",
    "exceed context window size",
    "input length exceeds context",
    "prompt is too long",
    "prompt too long",
    "prompt exceeds context",
    "input is too long for the context",
    "too many tokens in the prompt",
    "token limit exceeded",
    "reduce the length of the messages",
    "reduce the length of the prompt",
    "please reduce the length of your prompt",
    "requested tokens exceed context window",
    "number of tokens to keep from the initial prompt is greater than the context",
    "trying to keep the first tokens when context is full",
)

# Bounded patterns for the "maximum context length is N tokens" family, which no
# fixed phrase can cover because the number and the surrounding wording vary.
# Every quantifier is explicitly bounded so match cost stays linear in the
# already-bounded input.
_OVERFLOW_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "max context window of 4096 was exceeded"
    re.compile(
        r"\b(?:maximum|max) context (?:length|window|size)\b"
        r"[a-z ]{0,16}\b\d{1,9}\b[a-z ]{0,24}\b(?:exceeded|overflowed)\b"
    ),
    # "12000 tokens exceeds the 8192 token context"
    re.compile(r"\b\d{1,9} token[s]?\b[a-z0-9 ]{0,40}\b(?:exceed|exceeds|greater than|larger than)\b[a-z0-9 ]{0,40}\bcontext\b"),
    # llama.cpp style: "n_ctx (4096) is less than n_tokens (5000)"
    re.compile(r"\bn ctx\b[a-z0-9 ]{0,24}\b(?:less than|smaller than|exceeded)\b"),
)

# Numeric provider forms are only positive when the requested count is
# demonstrably larger than the advertised limit. Merely mentioning a maximum
# context size in an unrelated error is not enough to spend another request.
_LIMIT_THEN_REQUEST = re.compile(
    r"\b(?:maximum|max) context (?:length|window|size)\b[a-z ]{0,16}"
    r"\b(?P<limit>\d{1,9})\b(?: token[s]?)?[a-z ]{0,40}"
    r"\b(?:(?:you |your messages )?request(?:ed|s|ing)?|"
    r"your messages resulted in)\b[a-z ]{0,12}"
    r"\b(?P<requested>\d{1,9})\b"
)
_REQUEST_THEN_LIMIT = re.compile(
    r"\brequest(?:ed|s|ing)?\b[a-z ]{0,12}\b(?P<requested>\d{1,9})\b"
    r"(?: token[s]?)?[a-z ]{0,40}\b(?:model )?context\b[a-z ]{0,16}"
    r"\b(?P<limit>\d{1,9})\b"
)

# Error bodies sometimes quote or negate the phrase while explaining another
# failure. Normalization deliberately removes punctuation, so recognize the
# bounded semantic markers instead of treating echoed prose as positive proof.
_NEGATED_OR_META = (
    re.compile(
        r"\b(?:not|never|without)\b[a-z0-9 ]{0,24}"
        r"\b(?:context (?:length|window|size) exceeded|prompt (?:is )?too long)\b"
    ),
    re.compile(
        r"\b(?:context (?:length|window|size) exceeded|prompt (?:is )?too long)\b"
        r"[a-z0-9 ]{0,12}\b(?:false|not|never)\b"
    ),
    re.compile(
        r"\b(?:example|literal|quoted|phrase)\b[a-z0-9 ]{0,24}"
        r"\b(?:context (?:length|window|size) exceeded|prompt (?:is )?too long)\b"
    ),
)

# Negative controls. Each entry is (control name, normalized phrases). These are
# recognised so the gateway can say "deliberately not an overflow" instead of
# "unclassified", and so three of them can veto a retry.
_CONTROLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CONTROL_BODY_TOO_LARGE, (
        "request entity too large",
        "payload too large",
        "request body too large",
        "body too large",
        "http request body exceeded",
        "content too large",
    )),
    (CONTROL_DEVICE_OUT_OF_MEMORY, (
        "cuda out of memory",
        "hip out of memory",
        "out of memory",
        "cudamalloc failed",
        "cublas status alloc failed",
        "failed to allocate compute buffer",
        "unable to allocate backend buffer",
        "insufficient vram",
        "not enough vram",
    )),
    (CONTROL_RATE_LIMIT, (
        "rate limit",
        "rate limited",
        "too many requests",
        "quota exceeded",
        "requests per minute",
    )),
    (CONTROL_MODEL_MISSING, (
        "model not found",
        "no such model",
        "model does not exist",
        "unknown model",
        "try pulling it first",
        "pull the model first",
    )),
)


def _first_phrase(text: str, phrases) -> str:
    for phrase in phrases:
        if phrase in text:
            return phrase
    return ""


def _first_control(text: str) -> str:
    for control, phrases in _CONTROLS:
        if _first_phrase(text, phrases):
            return control
    return CONTROL_NONE


def _numeric_limit_evidence(text: str) -> str:
    for pattern in (_LIMIT_THEN_REQUEST, _REQUEST_THEN_LIMIT):
        found = pattern.search(text)
        if not found:
            continue
        try:
            requested = int(found.group("requested"))
            limit = int(found.group("limit"))
        except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
            continue
        if requested > limit:
            return found.group(0)
    return ""


def classify(detail, *, status=None) -> ContextOverflowMatch:
    """Decide whether one failed model call overflowed the context window.

    ``detail`` is the error text as the gateway captured it; ``status`` is the
    HTTP status if there was one. The status is recorded but never consulted:
    a body that says the context was exceeded is an overflow even when it
    arrives under a misleading 429, and a 400 with no overflow wording is not an
    overflow just because 400 is a common way to report one.
    """
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        code = None

    text, truncated = normalize(detail)
    if not text:
        return ContextOverflowMatch(False, status=code, truncated=truncated)

    control = _first_control(text)

    if any(pattern.search(text) for pattern in _NEGATED_OR_META):
        return ContextOverflowMatch(
            False, control=control, status=code, truncated=truncated,
        )

    evidence = _first_phrase(text, _OVERFLOW_PHRASES)
    reason = REASON_CONTEXT_PHRASE if evidence else REASON_NONE
    if not evidence:
        evidence = _numeric_limit_evidence(text)
        reason = REASON_CONTEXT_LIMIT if evidence else REASON_NONE
    if not evidence:
        for pattern in _OVERFLOW_PATTERNS:
            found = pattern.search(text)
            if found:
                evidence = found.group(0)
                reason = REASON_CONTEXT_LIMIT
                break

    overflow = bool(evidence) and control not in _VETO_CONTROLS
    return ContextOverflowMatch(
        overflow=overflow,
        reason=reason,
        evidence=evidence[:200],
        control=control,
        status=code,
        truncated=truncated,
    )


# --- bounded compaction -----------------------------------------------------
#
# The retry that follows a positive classification reuses the runtime's existing
# compaction discipline rather than inventing a second one: keep the system
# preamble and the newest turns, drop the oldest ones, and say so in-band. That
# is the same shape `server._session_history_messages` already applies to a
# session's live-turn window (older turns are folded away, a short system note
# stands in for them). No model call is made here, so nothing about this path can
# grow unbounded, and no message content is rewritten - messages are kept whole
# or dropped whole.

# Wording of the in-band note. It states only what happened; it never stands in
# for the dropped content, because inventing a summary without a model call
# would be fabrication.
COMPACTION_NOTE = (
    "Earlier turns in this conversation were dropped to fit the model's "
    "context window. Answer from the turns that remain."
)

_COMPACTION_NOTE_MARK = "were dropped to fit the model s context window"


def _is_system(message) -> bool:
    return isinstance(message, dict) and str(message.get("role", "")).strip().casefold() == "system"


def _role(message) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("role", "")).strip().casefold()


def compact_messages(messages, *, keep_recent: int = 0):
    """Drop the oldest droppable turns from a chat message list.

    Returns a new list, or ``None`` when compaction would change nothing - which
    is the signal not to retry. Nothing is dropped from the leading system
    preamble (it carries the instructions) or from the final message (it carries
    the actual request), and no message body is truncated: a request that is one
    oversized user turn cannot be made to fit without silently corrupting it, so
    it is reported as uncompactable instead.

    ``keep_recent`` optionally pins a minimum number of trailing history messages
    to preserve; the default keeps half of them.
    """
    if not isinstance(messages, (list, tuple)):
        return None
    items = [m for m in messages]
    if len(items) < 3:
        return None

    head = 0
    while head < len(items) and _is_system(items[head]):
        head += 1
    prefix = items[:head]
    # The final message is the live request and is always preserved.
    body = items[head:-1]
    tail = items[-1:]
    if len(body) < 2:
        return None

    # Never re-compact a payload that already carries the note: the retry budget
    # is exactly one, and a second pass would start eating real turns.
    for message in prefix:
        content = message.get("content") if isinstance(message, dict) else None
        folded, _ = normalize(content)
        if _COMPACTION_NOTE_MARK in folded:
            return None

    try:
        minimum_keep = max(0, int(keep_recent or 0))
    except (TypeError, ValueError, OverflowError):
        return None

    # Cut only immediately before a user message, which is the start of a
    # complete conversation turn. Cutting an arbitrary message can retain a
    # tool result without its assistant tool call (or an assistant response
    # without its user request), producing an invalid retry payload. If no
    # complete historical turn can be retained, dropping all history is safer
    # than keeping an orphaned fragment.
    target_drop = max(1, len(body) // 2)
    boundaries = [
        index for index in range(1, len(body))
        if _role(body[index]) == "user" and len(body) - index >= minimum_keep
    ]
    at_or_after = [index for index in boundaries if index >= target_drop]
    if at_or_after:
        dropped = at_or_after[0]
    elif boundaries:
        dropped = boundaries[-1]
    elif minimum_keep == 0:
        dropped = len(body)
    else:
        return None
    if dropped < 1:
        return None

    note = {"role": "system", "content": COMPACTION_NOTE}
    return list(prefix) + [note] + list(body[dropped:]) + list(tail)
