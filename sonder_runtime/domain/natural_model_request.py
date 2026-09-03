"""Pure grammar for explicit natural-language model and fanout requests.

Only imperative, whole-turn forms are recognized: ``use model X: ...``,
``ask all local models to ...``, the reviewed fanout profiles and the small
code-plus-reasoning ensemble. The grammar never inspects retrieved files, web
pages or model output, so untrusted text cannot spend local compute or cloud
budget. Catalog resolution and the transport error type stay with the caller
and are injected as callables. Moved from ``server.py`` in the WP1
Two-Hundred-Ninety-Ninth Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import re

# Selection profiles deliberately describe a small, host-defined set of
# target classes.  They are *not* a filtering language: accepting arbitrary
# tag, provider, or capability selectors here would let prompt-derived text
# widen an expensive fanout beyond the reviewed catalog policy.
#
# "healthy" means the model has no active fanout health cooldown.  Unknown
# models remain eligible so a newly discovered chat model is not silently
# starved of its first probe; non-chat targets are always excluded by the
# fanout selection gates.
FANOUT_SELECTION_PROFILES = {
    "healthy-local-chat": "local",
    "healthy-cloud-chat": "cloud",
    "healthy-chat": "all",
    # A deliberate no-load profile for an interactive machine: only models
    # that Ollama already reports as resident may be selected.  This stays
    # local and still passes the normal chat-capability/health gates below.
    "loaded-local-chat": "local",
}

UNKNOWN_FANOUT_PROFILE_MESSAGE = (
    "unknown fanout profile; use healthy-local-chat, healthy-cloud-chat, "
    "healthy-chat, or loaded-local-chat"
)


def fanout_profile_scope(profile):
    """Return a reviewed profile's scope, rejecting arbitrary selectors.

    Returns ``(scope, None)`` for a reviewed profile, ``(None, None)`` for an
    empty selector and ``(None, message)`` for an unknown one. The caller owns
    turning the message into its transport error.
    """
    name = str(profile or "").strip().lower()
    if not name:
        return None, None
    scope = FANOUT_SELECTION_PROFILES.get(name)
    if scope is None:
        return None, UNKNOWN_FANOUT_PROFILE_MESSAGE
    return scope, None


INTERPRETER_LIKE_MODEL_SELECTOR_PREFIXES = frozenset({
    # Bare ``run <runtime>:<version> ...`` is substantially more likely to be
    # an execution/work request than an intent to select a model.  Explicit
    # ``model <tag>`` forms remain the unambiguous opt-in for catalog models
    # that happen to share one of these names.
    "bash", "bun", "cargo", "cmd", "deno", "dotnet", "go", "java",
    "node", "nodejs", "perl", "php", "powershell", "pwsh", "python",
    "ruby", "sh",
})


def is_interpreter_like_bare_model_selector(selector):
    """Whether a tagged selector is more naturally a command name.

    Natural-language routing must not reinterpret ordinary work such as
    ``run python:3.12: reproduce this`` as a request to select a model.  This
    applies only to colon tags in bare-selector grammar.  An untagged
    catalog model genuinely named ``python`` remains selectable via the
    ordinary ``python model to`` phrasing; explicit ``model <tag>`` and
    ``using model <tag>`` forms remain intentional opt-ins for any tag.
    """
    value = str(selector or "")
    prefix, separator, _suffix = value.partition(":")
    return bool(separator) and prefix.casefold() in INTERPRETER_LIKE_MODEL_SELECTOR_PREFIXES


def natural_model_request(text, *, profile_scope, bare_tagged_request):
    """Recognize explicit user requests for a model or bounded model fanout.

    This intentionally recognizes only imperative, whole-turn forms.  It does
    not inspect retrieved files, web pages, or model output, preventing those
    untrusted inputs from spending local compute or cloud budget.

    ``profile_scope(profile)`` returns ``(scope, error)`` for a reviewed fanout
    profile and ``bare_tagged_request(selector, prompt)`` resolves a terse
    ``<name>:<tag>`` selector against the live catalog, returning a request
    or ``None``. Both are injected so this grammar stays free of transport
    and catalog concerns.
    """
    value = str(text or "").strip()
    ensemble = re.match(
        # A small, named local ensemble is useful for an explicit second
        # opinion without turning broad prose about "reasoning" into an
        # execution request. Keep the same imperative whole-turn and prompt
        # delimiter contract as model fanout. Compiler-feedback repair is a
        # separate, explicitly parameterized codegen_build_loop tool: it must
        # know the approved root, files, and build command and cannot safely
        # be inferred from free-form chat.
        r"^(?:ask|run|try|query|use)\s+(?:a\s+|the\s+)?(?:code\s+(?:and|\+)\s+reasoning|reasoning\s+(?:and|\+)\s+code)\s+(?:models?|ensemble)\s*(?::|to\s+answer\b:?|answer\b:?|to\b|for\s+)\s*(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if ensemble:
        return {
            "kind": "ensemble", "tiers": "code,reasoning",
            "prompt": ensemble.group(1).strip(),
        }
    profiled_fanout = re.match(
        # Keep this whole-turn syntax as constrained as the existing all-model
        # grammar.  In particular, no trailing selector or embedded prose may
        # become a profile request.
        r"^(?:ask|run|try|query)\s+(?:(?:all|every)\s+)?((?:healthy\s+(?:local|cloud)?|loaded\s+local)\s*chat)\s+models?\s*(?::|to\s+answer\b:?|answer\b:?|to\b|for\s+)\s*(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if profiled_fanout:
        profile = "-".join(profiled_fanout.group(1).lower().split())
        scope, error = profile_scope(profile)
        if error is None:
            return {
                "kind": "fanout", "scope": scope, "profile": profile,
                "prompt": profiled_fanout.group(2).strip(),
            }
    sonder_cloud_fanout = re.match(
        # Preserve the same whole-turn imperative/delimiter boundary as the
        # general fanout grammar below.  This covers the user-facing runtime
        # name without treating a retrieved mention of Sonder as authority to
        # spend local/cloud compute.
        r"^(?:ask|run|try|query)\s+(?:all|every)\s+(?:the\s+)?sonder\s+models?\s*(?:and|\+)\s+cloud(?:\s+models?)?\b\s*(?::|to\s+answer\b:?|answer\b:?|to\b|for\s+)\s*(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if sonder_cloud_fanout:
        return {"kind": "fanout", "scope": "all", "prompt": sonder_cloud_fanout.group(1).strip()}
    fanout = re.match(
        # Keep this an imperative whole-turn grammar: it is deliberately not
        # a classifier over retrieved prose.  ``available`` describes the
        # catalog while local/cloud selects its bounded scope.
        r"^(?:ask|run|try|query)\s+(?:all|every)\s+(?:of\s+)?(?:the\s+|my\s+)?(?:(?:currently\s+)?available\s+)?(?:(?:(local|cloud|local\s+(?:and|\+)\s+cloud|cloud\s+(?:and|\+)\s+local)\s+)?models?|local\s+models?\s+(?:and|\+)\s+cloud\s+models?|cloud\s+models?\s+(?:and|\+)\s+local\s+models?)(?:\s+(?:currently\s+)?available)?\b\s*(?::|to\s+answer\b:?|answer\b:?|to\b|for\s+)\s*(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if fanout:
        scope = (fanout.group(1) or "all").lower()
        if "local" in scope and "cloud" in scope:
            scope = "all"
        return {"kind": "fanout", "scope": scope, "prompt": fanout.group(2).strip()}
    single = re.match(
        # A model tag commonly contains a colon (for example ``phi4:latest``).
        # Requiring whitespace after the prompt separator makes the final
        # ``: `` unambiguous without turning ordinary prose into a request.
        r"^(?:use|run|ask|try|query)\s+model\s+([A-Za-z0-9][A-Za-z0-9._:/-]*)\s*:\s+(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if single:
        return {"kind": "model", "model": single.group(1).strip(), "prompt": single.group(2).strip()}
    named_tag = re.match(
        # A bare tag is accepted only when it contains an internal tag colon;
        # arbitrary "run word: ..." prose must not become a model request.
        r"^(?:use|run|ask|try|query)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._/-]*)\s*:\s+(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if named_tag:
        selector = named_tag.group(1).strip()
        request = bare_tagged_request(selector, named_tag.group(2))
        if request is None:
            return None
        return request
    named_tag_to = re.match(
        # A tagged selector plus an explicit ``to`` is as unambiguous as the
        # existing ``with/using <tag> to`` form. Keep the internal colon so
        # ordinary ``run thing to ...`` prose cannot become model routing.
        r"^(?:use|run|ask|try|query)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._/-]*)\s+to\s+(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if named_tag_to:
        selector = named_tag_to.group(1).strip()
        # Unlike the ``model <tag>`` forms, this is deliberately terse enough
        # to resemble ordinary version-tagged work (for example
        # ``ubuntu:24.04 to reproduce ...``). Only consume it after an exact
        # live-catalog match; an unavailable/unknown tag stays ordinary prose
        # rather than losing its work instruction to an unknown-tier error.
        request = bare_tagged_request(selector, named_tag_to.group(2))
        if request is None:
            return None
        return request
    using_model = re.match(
        # This provides an explicit natural-language counterpart to the
        # established ``use model X: prompt`` form without attempting to
        # infer a model from arbitrary prose.  Both the ``using model`` cue
        # and a prompt delimiter are required; the selector is still checked
        # against the live catalog downstream.
        r"^(?:use|run|ask|try|query)\s+(?:with|using)\s+model\s+([A-Za-z0-9][A-Za-z0-9._:/-]*)\s*(?::\s+|to\s+)(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if using_model:
        return {
            "kind": "model",
            "model": using_model.group(1).strip(),
            "prompt": using_model.group(2).strip(),
        }
    using_tag = re.match(
        # An internal tag colon keeps ordinary prose out of this routing path.
        # ``with/using <tag> to/for`` is natural speech, but remains bounded:
        # it requires a tag-shaped selector, an explicit delimiter, and the
        # exact selector is still resolved against the live catalog downstream.
        r"^(?:use|run|ask|try|query)\s+(?:with|using)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._/-]*)(?:\s+model\s*(?::\s+|to\s+|for\s+)|\s*(?::\s+|to\s+|for\s+))(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if using_tag:
        selector = using_tag.group(1).strip()
        # These are command/interpreter names first and model tags only by
        # coincidence.  Let ordinary work such as ``run using python:3.12 to
        # reproduce this`` reach the normal agent path; a user who really
        # means a model can use the unambiguous ``using model <tag>`` form.
        request = bare_tagged_request(selector, using_tag.group(2))
        if request is None:
            return None
        return request
    # A colon in a model tag is common, which is why the legacy form above
    # requires ``: ``.  This alternate has a constrained selector and an
    # explicit ``to`` delimiter, so it remains unambiguous and cannot make
    # arbitrary prose a routing request.
    single_to = re.match(
        r"^(?:use|run|ask|try|query)\s+model\s+([A-Za-z0-9][A-Za-z0-9._:/-]*)\s+to\s+(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if single_to:
        return {"kind": "model", "model": single_to.group(1).strip(), "prompt": single_to.group(2).strip()}
    named_model_to = re.match(
        # Natural phrasing commonly puts ``model`` after the name.  Keep the
        # same constrained selector and whole-turn delimiter as ``model X to``
        # above; _serve_target still resolves only a live catalog entry.
        r"^(?:use|run|ask|try|query)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9._:/-]*)\s+model\s+to\s+(.+)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if named_model_to:
        selector = named_model_to.group(1).strip()
        # "the best model" and similar preference language is not a concrete
        # selector.  Preserve it as an ordinary request rather than consuming
        # the wrapper and producing an unknown-tier error.  Exact model names
        # remain validated downstream against the live catalog.
        if selector.casefold() in {
            "best", "better", "fastest", "quickest", "cheapest",
            "strongest", "smartest", "largest", "smallest", "biggest",
            "appropriate", "available", "default", "preferred",
            "recommended", "right", "local", "cloud",
        }:
            return None
        if is_interpreter_like_bare_model_selector(selector):
            return None
        return {
            "kind": "model",
            "model": selector,
            "prompt": named_model_to.group(2).strip(),
        }
    return None
