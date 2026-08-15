"""Per-response activity tracking for tools, files, and model work.

The tracker is intentionally small and dependency-free. Server paths open a
response span, then tools and file helpers record observable events into the
current thread's span. Status endpoints can also read active spans while work is
still running.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy


MAX_EVENTS = 80
MAX_ACTIVE = 20
MAX_EVENT_BLOCK = 4000
MAX_PUBLIC_PREVIEW = 1000
MAX_FEED_EVENTS = 20
MAX_FEED_BYTES = 64 * 1024
MAX_PUBLIC_EVENTS = 20
MAX_PUBLIC_RESPONSE_BYTES = 64 * 1024
MAX_EVENT_RING = 512
_NPU_PUBLIC_ENUMS = {
    "capability": frozenset({"routing", "embeddings"}),
    "reason": frozenset({
        "bundle_limit", "busy", "circuit_open", "cold", "crash",
        "dimension_mismatch", "hash_drift", "identity_mismatch", "internal",
        "invalid", "low_confidence", "malformed", "manifest_unhealthy",
        "no_manifest", "oversized", "ram_gate", "rss_limit",
        "simulated_provider", "space_mismatch", "space_unpinned", "spawn",
        "target_unattested", "timeout", "warming", "worker_error",
    }),
    "operation_mode": frozenset({"execution", "shadow"}),
    "fallback_handler": frozenset({"npu", "cpu", "ollama", "host"}),
    "handler_state": frozenset({"pending", "handled", "failed", "observed"}),
}
# Keep active response spans intact when this helper is refreshed at a request
# boundary. Function definitions may reload; live response ownership may not.
if "_LOCK" not in globals():
    _LOCK = threading.RLock()
if "_LOCAL" not in globals():
    _LOCAL = threading.local()
if "_IDS" not in globals():
    _IDS = itertools.count(1)
if "_ACTIVE" not in globals():
    _ACTIVE = {}
if "_LATEST" not in globals():
    _LATEST = None
if "_TOTAL_TOOL_CALLS" not in globals():
    _TOTAL_TOOL_CALLS = 0
if "_NEXT_EVENT_SEQUENCE" not in globals():
    _NEXT_EVENT_SEQUENCE = 1
if "_RUNTIME_ID" not in globals():
    _RUNTIME_ID = "rt-%s" % uuid.uuid4().hex[:12]
if "_EVENT_RING" not in globals():
    _EVENT_RING = []
if "_DROPPED_EVENTS" not in globals():
    _DROPPED_EVENTS = 0
# Model reasoning is kept OUT of the response span on purpose. Spans flow into
# snapshot() and from there into the sonder_activity field every client
# receives; reasoning is gated separately and must not ride along.
if "_REASONING" not in globals():
    _REASONING = {}
if "_REASONING_OWNERS" not in globals():
    # Authorization data, deliberately outside public response snapshots.
    _REASONING_OWNERS = {}
if "_LATEST_REASONING" not in globals():
    _LATEST_REASONING = None
if "_LATEST_REASONING_OWNER" not in globals():
    _LATEST_REASONING_OWNER = ""
# Live execution feed ownership. Owner keys are opaque principal hashes (or ""
# for the single trusted local operator domain) and, like reasoning owners,
# they live OUTSIDE the response object so no public projection can carry them.
if "_FEED_OWNERS" not in globals():
    _FEED_OWNERS = {}
if "_OWNER_RECENT" not in globals():
    _OWNER_RECENT = {}

MAX_REASONING_CHARS = 20000
MAX_FEED_OWNERS = 32
MAX_OWNER_FEED_ENTRIES = 12


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _short(value, limit=220):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + "..." if len(text) > limit else text


def _block(value, limit=MAX_EVENT_BLOCK):
    text = str(value or "").replace("\x00", "\\0").strip()
    return text[:limit] + "\n... (truncated)" if len(text) > limit else text


# These rules and npu_contract's (npu_contract.py:89-113) do the same job on the
# same shapes, and drifted: _BEARER_RE was added here first, then npu_contract
# alone was hardened. Measured against eight credential shapes this copy caught
# ZERO of them, and one of the eight came out looking sanitized while leaking:
# `token = "value with spaces"` became `token=<redacted> with spaces"`, because
# the value pattern stopped at the first space and never considered quotes.
#
# The AWS miss was the sharpest: '_' is a word character, so `\bsecret\b` has no
# boundary to match on either side of AWS_SECRET_ACCESS_KEY and the most standard
# secret environment variable there is passed through untouched. Wrapping the
# keyword in [a-z0-9_]* -- the way npu_contract does -- is what fixes that.
#
# Deliberately NARROWER than npu_contract in three places, because that module
# bounds a 200-char error string while this one carries the activity ledger's
# real command lines and stdout, which a caller reads to see what happened:
#   * an unquoted value ends at whitespace rather than at end-of-line, so
#     `--token abc --verbose` keeps its remaining flags;
#   * the separator is [ \t] only, so a bare `pwd` at end of line cannot swallow
#     the first word of the NEXT line;
#   * the JWT rule is anchored on the "ey" header ('{"' in base64url) so a dotted
#     identifier such as tests.test_activity.test_case is not read as a token.
# Accepted cost of the wider keyword: a tool that prints "tokens: 512" in its
# stdout has that count redacted, because "tokens" contains "token". Exempting
# numeric values would fix that and would equally un-redact "password: 1234", so
# it stays redacted. The ledger's OWN token counters are structured ints
# (format_transcript reads them from the span) and never pass through here.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z0-9_]*(?:password|passwd|pwd|token|secret|api[-_]?key|"
    r"access[-_]?key|authorization|credential)[a-z0-9_]*)"
    r"(?:[ \t]*[:=][ \t]*|[ \t]+)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_KEY_SHAPE_RE = re.compile(
    r"\b(?:(?:sk|gh[pousr])[-_][A-Za-z0-9_-]{12,}|"
    r"(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16})\b"
)
_JWT_RE = re.compile(r"\bey[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_URI_CREDENTIAL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _redact_text(value):
    try:
        text = str(value or "")
        # Bearer first: the assignment rule now matches `Authorization:` too, and it
        # consumes only up to the next space -- so running it first would eat the
        # word "Bearer" and leave the token itself behind, unmatchable.
        text = _BEARER_RE.sub("Bearer <redacted>", text)
        text = _URI_CREDENTIAL_RE.sub(r"\1<redacted>@", text)
        text = _SECRET_ASSIGNMENT_RE.sub(
            lambda match: "%s=<redacted>" % match.group(1), text,
        )
        text = _JWT_RE.sub("<redacted-token>", text)
        return _CONTROL_RE.sub("?", _KEY_SHAPE_RE.sub("<redacted-key>", text))
    except Exception:
        return "<redaction-failed>"


def _preview_descriptor(value=None, *, limit=MAX_PUBLIC_PREVIEW, available=True):
    """Return an explicit bounded/redacted preview contract.

    ``chars`` describes the original value while ``text`` is always the safe
    projection. Callers can therefore distinguish empty, unavailable,
    truncated, and redacted values without receiving the unbounded source.
    """
    if not available:
        return {
            "state": "unavailable", "text": "", "chars": None,
            "truncated": False, "redacted": False,
        }
    try:
        raw = str(value or "").replace("\x00", "\\0")
    except Exception:
        raw = "<redaction-failed>"
    safe = _redact_text(raw)
    bounded = max(80, min(MAX_EVENT_BLOCK, int(limit or MAX_PUBLIC_PREVIEW)))
    truncated = len(safe) > bounded
    return {
        "state": "available",
        "text": safe[:bounded] + ("\n... (truncated)" if truncated else ""),
        "chars": len(raw),
        "truncated": truncated,
        "redacted": safe != raw,
    }


def detail_enabled():
    return os.environ.get("SONDER_EXECUTION_FEED_DETAIL", "") == "1"


def _safe_path(value):
    text = _block(_redact_text(value), 1000)
    parts = [
        part for part in re.split(r"[\\/]+", text)
        if part not in ("", ".", "..")
    ]
    leaf = parts[-1] if parts else ""
    # ``C:foo`` is a drive-relative Windows path, not a harmless basename.
    if re.match(r"^[A-Za-z]:", leaf):
        leaf = leaf[2:]
    return leaf or (text if text and not re.search(r"[\\/:]", text) else "<path>")


# One vocabulary for "this NAME marks its VALUE as a secret", shared by the
# dict-key mask (`_safe_args`) and the argv mask (`_safe_command`). JSON and
# argv rendering split a keyword and its value into separate strings, so
# `_SECRET_ASSIGNMENT_RE` -- which needs both in one string -- can never see
# the pair; these name checks are the only line of defence there. They were
# two ad-hoc lists that had drifted narrower than the free-text regex:
# `{"pwd": "..."}` survived into the (detail-gated) ledger verbatim while
# `pwd=...` in prose was stripped. Keep this a superset of the regex's
# keyword alternation; a conformance test pins the correspondence.
_SENSITIVE_NAME_MARKERS = (
    "password", "passwd", "pwd", "token", "secret",
    "api_key", "api-key", "apikey", "access_key", "access-key", "accesskey",
    "authorization", "credential", "approval",
)


def _safe_command(value):
    if not value:
        return ""
    parsed = value
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = value
    # Agent activity renders argv as ``program ["--flag", "value"]`` so
    # humans can distinguish the executable from its arguments.  That shape
    # is neither a plain argv list nor ordinary free text: redacting it as
    # text misses a secret flag whose name and value are separate JSON items.
    # Reuse the list handling below while preserving the safe program prefix.
    prefix = ""
    if isinstance(parsed, str):
        # The program/path itself can contain ``[``.  Try every possible JSON
        # array start and accept only one that parses through the *end* of the
        # command text; the first bracket is not necessarily the argv array.
        for bracket, char in enumerate(parsed):
            if char != "[" or bracket == 0:
                continue
            candidate = parsed[bracket:].strip()
            try:
                argv = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(argv, list):
                prefix = parsed[:bracket].strip()
                parsed = argv
                break
    if not isinstance(parsed, (list, tuple)):
        return _block(_redact_text(parsed), 2400)
    rendered = []
    hide_next = False
    for item in parsed:
        text = str(item)
        lowered = text.lower().lstrip("-/")
        if hide_next:
            rendered.append("<redacted>")
            hide_next = False
            continue
        if lowered in _SENSITIVE_NAME_MARKERS:
            rendered.append(text)
            hide_next = True
            continue
        if any(lowered.startswith(name + "=") for name in _SENSITIVE_NAME_MARKERS):
            rendered.append(text.split("=", 1)[0] + "=<redacted>")
            continue
        rendered.append(_redact_text(text))
    command = json.dumps(rendered, ensure_ascii=False)
    if prefix:
        command = _redact_text(prefix) + " " + command
    return _block(command, 2400)


def _safe_args(value, depth=0):
    """Keep useful operation details without persisting secrets or bulk content."""
    if depth > 3:
        return "<nested>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if any(part in lowered for part in _SENSITIVE_NAME_MARKERS):
                out[name] = "<redacted>"
            elif lowered in {"content", "code", "files_json", "stdin"}:
                out[name] = _preview_descriptor(item)
            else:
                out[name] = _safe_args(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe_args(item, depth + 1) for item in list(value)[:64]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _block(_redact_text(value), 1000)


def _json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _safe_value_descriptor(raw, safe):
    safe_text = safe if isinstance(safe, str) else _json_text(safe)
    descriptor = _preview_descriptor(safe_text)
    raw_text = raw if isinstance(raw, str) else _json_text(raw)

    def flags(value):
        if isinstance(value, dict):
            own = bool(value.get("redacted")), bool(value.get("truncated"))
            nested = [flags(item) for item in value.values()]
        elif isinstance(value, (list, tuple)):
            own = False, False
            nested = [flags(item) for item in value]
        else:
            own = False, False
            nested = []
        return (
            own[0] or any(item[0] for item in nested),
            own[1] or any(item[1] for item in nested),
        )

    nested_redacted, nested_truncated = flags(safe)
    descriptor["redacted"] = (
        descriptor["redacted"]
        or nested_redacted
        or _redact_text(raw_text) != raw_text
    )
    descriptor["truncated"] = descriptor["truncated"] or nested_truncated
    return descriptor


ACTION_TITLES = {
    "workspace_inventory": "Inventoried Workspace",
    "directory_tree": "Listed Folder",
    "directory_create": "Created Folder",
    "file_find": "Searched Files",
    "file_read": "Read File",
    "file_read_range": "Read File Range",
    "file_write": "Wrote File",
    "file_edit": "Edited File",
    "file_delete": "Deleted File",
    "text_search": "Searched Text",
    "script_search": "Searched Scripts",
    "program_search": "Searched Programs",
    "workspace_run": "Ran Program",
    "script_run": "Ran Script",
    "run_code": "Ran Code",
    "run_project": "Ran Project",
    "image_inspect": "Viewed Image",
    "artifact_generate": "Generated Assets",
    "artifact_verify": "Verified Assets",
    "game_reference_suite": "Ran Game Suite",
    "game_generate_and_test": "Built and Tested Game",
    "game_generation_campaign": "Ran Game Fleet",
    "ground_artifact": "Verified Artifact",
    "checklist_create": "Created Checklist",
    "checklist_update": "Updated Checklist",
    "checklist_show": "Viewed Checklist",
    "master_capacity": "Checked Fleet Capacity",
    "master_cancel": "Cancelled Agent Fleet",
    "master_retry": "Retried Persisted Fleet",
    "memory_privacy_review": "Reviewed Memory Privacy",
    "memory_privacy_repair": "Cleaned Memory Privacy",
    "memory_embedding_backfill": "Backfilled Memory Embeddings",
    "web_search": "Searched Web",
    "web_fetch": "Fetched Web Page",
}


def action_title(tool_name):
    value = str(tool_name or "tool")
    return ACTION_TITLES.get(value, value.replace("_", " ").strip().title())


def _current():
    response_id = getattr(_LOCAL, "response_id", None)
    if not response_id:
        return None
    with _LOCK:
        return _ACTIVE.get(response_id)


def current_response_id():
    """Return the active response ID for propagation into a worker thread."""
    return getattr(_LOCAL, "response_id", None)


@contextlib.contextmanager
def bind_response(response_id):
    """Attach a worker thread to an existing response activity span."""
    previous = getattr(_LOCAL, "response_id", None)
    with _LOCK:
        response = _ACTIVE.get(response_id) if response_id else None
    if response is None:
        yield None
        return
    _LOCAL.response_id = response_id
    try:
        yield response
    finally:
        _LOCAL.response_id = previous


def _event(response, kind, **fields):
    global _NEXT_EVENT_SEQUENCE, _DROPPED_EVENTS
    elapsed_ms = int((time.time() - response["started_at"]) * 1000)
    event = {
        "seq": _NEXT_EVENT_SEQUENCE,
        "ts": _now(), "elapsed_ms": elapsed_ms, "kind": kind,
    }
    _NEXT_EVENT_SEQUENCE += 1
    event.update({k: v for k, v in fields.items() if v not in (None, "")})
    response["events"].append(event)
    if len(response["events"]) > MAX_EVENTS:
        del response["events"][:-MAX_EVENTS]
    response["last_event"] = event
    ring_event = _public_event(event, include_detail=True)
    ring_event["response_id"] = _short(response.get("id", ""), 80)
    ring_event["response_status"] = _short(response.get("status", "unknown"), 40)
    _EVENT_RING.append(ring_event)
    if len(_EVENT_RING) > MAX_EVENT_RING:
        dropped = len(_EVENT_RING) - MAX_EVENT_RING
        del _EVENT_RING[:dropped]
        _DROPPED_EVENTS += dropped
    return event


@contextlib.contextmanager
def response_span(
    label, prompt="", *, surface="", model="", session="", project="",
    reasoning_owner="", feed_owner="",
):
    """Track one user-visible response.

    Nested spans reuse the outer response so helpers can be composed safely.
    """
    existing = getattr(_LOCAL, "response_id", None)
    if existing:
        yield _current()
        return
    response_id = "r%06d" % next(_IDS)
    response = {
        "id": response_id,
        "label": _short(label, 80),
        "surface": _short(surface, 80),
        "model": _short(model, 80),
        "session": _short(session, 80),
        "project": _short(project, 80),
        "prompt": _short(prompt, 500),
        "status": "running",
        "started_ts": _now(),
        "started_at": time.time(),
        "elapsed_ms": 0,
        "tool_calls": 0,
        "model_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "file_creates": 0,
        "file_edits": 0,
        "file_deletes": 0,
        "lines_added": 0,
        "lines_edited": 0,
        "lines_deleted": 0,
        "files": [],
        "checklist": None,
        "result_summary": "",
        "events": [],
        "last_event": None,
    }
    with _LOCK:
        _ACTIVE[response_id] = response
        # Never add these opaque principals to ``response``: that object is
        # projected to public activity clients.
        _REASONING_OWNERS[response_id] = str(reasoning_owner or "")
        _FEED_OWNERS[response_id] = str(feed_owner or "")
        if len(_ACTIVE) > MAX_ACTIVE:
            oldest = sorted(_ACTIVE, key=lambda k: _ACTIVE[k]["started_at"])[:1]
            for key in oldest:
                if key != response_id:
                    _ACTIVE.pop(key, None)
    _LOCAL.response_id = response_id
    try:
        record_event("response_start", summary=_short(prompt, 180))
        yield response
        # A host policy can downgrade a model's apparent success (for example,
        # an agent that says "done" without the verification its lane makes
        # reachable).  Do not overwrite that observable verdict on normal
        # context-manager exit.
        if response.get("status") == "running":
            response["status"] = "complete"
    except Exception as exc:
        response["status"] = "error"
        record_event("response_error", summary=_short(exc, 180))
        raise
    finally:
        response["elapsed_ms"] = int((time.time() - response["started_at"]) * 1000)
        if response["status"] == "complete":
            record_event("response_complete", summary="%d tool call(s)" % response["tool_calls"])
        with _LOCK:
            global _LATEST, _LATEST_REASONING, _LATEST_REASONING_OWNER
            _ACTIVE.pop(response_id, None)
            _append_owner_entry(_FEED_OWNERS.pop(response_id, ""), response)
            _LATEST = deepcopy(response)
            # Promote alongside _LATEST so the pair always describes the same
            # turn, then drop the per-span buffer.
            _LATEST_REASONING = _collect_reasoning(response_id)
            _LATEST_REASONING_OWNER = _REASONING_OWNERS.pop(response_id, "")
            _REASONING.pop(response_id, None)
        _LOCAL.response_id = None


def record_event(kind, **fields):
    response = _current()
    if response is None:
        return None
    with _LOCK:
        return deepcopy(_event(response, kind, **fields))


def record_reasoning(text, *, model=""):
    """Attach one model reasoning segment to the current response.

    A turn can make several model calls, so segments accumulate in order. Stored
    outside the response span so it never reaches snapshot(); read it back with
    latest_reasoning(), which the HTTP layer gates before emitting.
    """
    if not isinstance(text, str) or not text.strip():
        return
    response_id = getattr(_LOCAL, "response_id", None)
    if not response_id:
        return
    with _LOCK:
        segments = _REASONING.setdefault(response_id, [])
        segments.append({"model": _short(model, 80), "text": text.strip()})


def _collect_reasoning(response_id):
    """Join a response's reasoning segments into one bounded record."""
    segments = _REASONING.get(response_id) or []
    if not segments:
        return None
    if len(segments) == 1:
        text = segments[0]["text"]
    else:
        text = "\n\n".join(
            "[%d/%d] %s" % (i, len(segments), seg["text"])
            for i, seg in enumerate(segments, 1)
        )
    truncated = len(text) > MAX_REASONING_CHARS
    if truncated:
        text = text[:MAX_REASONING_CHARS] + "\n... (reasoning truncated)"
    return {
        "text": text,
        "model": segments[-1]["model"],
        "segments": len(segments),
        "truncated": truncated,
    }


def latest_reasoning():
    """Reasoning from the most recently completed response, or None."""
    with _LOCK:
        return deepcopy(_LATEST_REASONING)


def current_reasoning():
    """Reasoning recorded so far for the span this thread is inside, or None.

    Callers that run inside their own span (the HTTP chat path does) must use
    this rather than latest_reasoning(): _LATEST_REASONING still describes the
    PREVIOUS turn until this span closes, so reading it here would attach one
    caller's reasoning to another caller's answer.
    """
    response_id = getattr(_LOCAL, "response_id", None)
    if not response_id:
        return None
    with _LOCK:
        return deepcopy(_collect_reasoning(response_id))


def reasoning_for_owner(owner=""):
    """Return current/latest reasoning only when it belongs to ``owner``."""
    wanted = str(owner or "")
    response_id = getattr(_LOCAL, "response_id", None)
    with _LOCK:
        if response_id and _REASONING_OWNERS.get(response_id, "") == wanted:
            record = _collect_reasoning(response_id)
            if record is not None:
                return deepcopy(record)
        if _LATEST_REASONING is not None and _LATEST_REASONING_OWNER == wanted:
            return deepcopy(_LATEST_REASONING)
    return None


def record_model_call(
    model="", prompt_chars=0, history_messages=0, ok=True, elapsed_ms=0,
    tokens_in=0, tokens_out=0, token_source="", request_preview=None,
    response_preview=None,
):
    response = _current()
    if response is None:
        return
    with _LOCK:
        response["model_calls"] += 1
        response["tokens_in"] += int(tokens_in or 0)
        response["tokens_out"] += int(tokens_out or 0)
        _event(
            response,
            "model_call",
            model=_short(model, 80),
            prompt_chars=int(prompt_chars or 0),
            history_messages=int(history_messages or 0),
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            token_source=_short(token_source, 40),
            request_preview=_preview_descriptor(
                request_preview, available=request_preview is not None,
            ),
            response_preview=_preview_descriptor(
                response_preview, available=response_preview is not None,
            ),
            ok=bool(ok),
            elapsed_ms=int(elapsed_ms or 0),
        )


def record_model_fanout(*, cloud_model_calls=0, tokens_in=0, tokens_out=0,
                        answered=0, failed=0, unknown=0, skipped=0, elapsed_ms=0):
    """Record a bounded aggregate for a durable model fanout.

    Local fanout calls execute in the response thread and are already captured
    individually by :func:`record_model_call`.  Cloud calls execute in worker
    threads, which deliberately do not inherit that response context, so add
    only their terminal attempted count here.  This preserves a truthful
    response-level total without manufacturing per-model prompt previews.
    """
    response = _current()
    if response is None:
        return
    cloud_model_calls = max(0, int(cloud_model_calls or 0))
    tokens_in = max(0, int(tokens_in or 0))
    tokens_out = max(0, int(tokens_out or 0))
    answered = max(0, int(answered or 0))
    failed = max(0, int(failed or 0))
    unknown = max(0, int(unknown or 0))
    skipped = max(0, int(skipped or 0))
    elapsed_ms = max(0, int(elapsed_ms or 0))
    with _LOCK:
        response["model_calls"] += cloud_model_calls
        response["tokens_in"] += tokens_in
        response["tokens_out"] += tokens_out
        _event(
            response,
            "model_fanout",
            cloud_model_calls=cloud_model_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            answered=answered,
            failed=failed,
            unknown=unknown,
            skipped=skipped,
            elapsed_ms=elapsed_ms,
            summary="%d answered, %d failed, %d unknown, %d skipped" % (
                answered, failed, unknown, skipped,
            ),
        )


def record_tool_call(name, args=None, *, ok=True, elapsed_ms=0, summary=""):
    response = _current()
    if response is None:
        return
    with _LOCK:
        global _TOTAL_TOOL_CALLS
        response["tool_calls"] += 1
        _TOTAL_TOOL_CALLS += 1
        safe_args = _safe_args(args or {})
        _event(
            response,
            "tool_call",
            tool=_short(name, 80),
            title=action_title(name),
            args=safe_args,
            args_preview=_safe_value_descriptor(args or {}, safe_args),
            ok=bool(ok),
            elapsed_ms=int(elapsed_ms or 0),
            summary=_short(_redact_text(summary), 220),
        )


def record_tool_result(
    name, args=None, *, ok=True, elapsed_ms=0, summary="", command="", output="",
):
    """Record a replay-friendly bounded tool event."""
    response = _current()
    if response is None:
        return
    with _LOCK:
        global _TOTAL_TOOL_CALLS
        response["tool_calls"] += 1
        _TOTAL_TOOL_CALLS += 1
        safe_args = _safe_args(args or {})
        safe_command = _safe_command(command)
        safe_output = _block(_redact_text(output), MAX_EVENT_BLOCK)
        result_source = output if output not in (None, "") else summary
        safe_result = _block(_redact_text(result_source), MAX_EVENT_BLOCK)
        _event(
            response,
            "tool_call",
            tool=_short(name, 80),
            title=action_title(name),
            args=safe_args,
            args_preview=_safe_value_descriptor(args or {}, safe_args),
            command=safe_command,
            command_preview=(
                _safe_value_descriptor(command, safe_command)
                if command not in (None, "") else _preview_descriptor(available=False)
            ),
            output=safe_output,
            result_preview=(
                _safe_value_descriptor(result_source, safe_result)
                if result_source not in (None, "") else _preview_descriptor(available=False)
            ),
            ok=bool(ok),
            elapsed_ms=int(elapsed_ms or 0),
            summary=_short(_redact_text(summary), 220),
        )


def set_checklist(checklist):
    response = _current()
    if response is None:
        return
    with _LOCK:
        response["checklist"] = deepcopy(checklist) if checklist else None
        _event(
            response,
            "checklist",
            title="Updated Checklist",
            summary=_short((checklist or {}).get("summary", ""), 220),
        )


def set_result_summary(summary):
    response = _current()
    if response is None:
        return
    with _LOCK:
        response["result_summary"] = _block(summary, 1000)


def set_response_status(status, summary=""):
    """Set a bounded host-observed terminal status for the active response.

    This is intentionally not a general model-facing status setter.  Callers
    use it only for a host-established outcome such as ``unverified``; the
    response span still owns ordinary completion and exceptions.
    """
    normalized = str(status or "").strip().lower()
    if normalized not in {"unverified", "incomplete"}:
        raise ValueError("unsupported response status")
    response = _current()
    if response is None:
        return
    with _LOCK:
        response["status"] = normalized
        _event(response, "response_%s" % normalized, summary=_short(summary, 180))


@contextlib.contextmanager
def tool_dispatch_context(tool_name=""):
    """Mark one tool dispatch; with a name, expose it as the running operation.

    The name feeds only the owner-scoped live feed's "Running ..." line: it is
    the tool's catalog name rendered through :func:`action_title`, never the
    tool's arguments. Restoring the previous marker keeps nested dispatches
    truthful about which operation is actually on the stack.
    """
    depth = int(getattr(_LOCAL, "tool_depth", 0) or 0)
    _LOCAL.tool_depth = depth + 1
    response = _current() if tool_name else None
    marker = None
    if response is not None:
        with _LOCK:
            marker = {
                "category": "tool",
                "name": action_title(tool_name),
                "started_at": time.time(),
            }
            # One response may own several concurrently executing fleet
            # workers.  A simple previous/current stack is only correct for
            # nesting in one worker: when worker A exits before worker B it
            # would clear B, and B would later resurrect A.  Keep the live
            # markers explicitly and expose the most recently entered active
            # one instead.  This stays private to the response record; the
            # feed projects only ``current_operation``.
            active = response.setdefault("_active_operations", [])
            active.append(marker)
            response["current_operation"] = marker
    try:
        yield
    finally:
        _LOCAL.tool_depth = depth
        if response is not None and marker is not None:
            with _LOCK:
                active = response.get("_active_operations") or []
                response["_active_operations"] = [
                    item for item in active if item is not marker
                ]
                if response["_active_operations"]:
                    response["current_operation"] = response["_active_operations"][-1]
                else:
                    response.pop("_active_operations", None)
                    response.pop("current_operation", None)


def inside_tool_call():
    return int(getattr(_LOCAL, "tool_depth", 0) or 0) > 0


def record_file_change(
    action,
    path,
    *,
    lines_added=0,
    lines_edited=0,
    lines_deleted=0,
    bytes_written=0,
    dry_run=False,
    summary="",
    preview=None,
    preview_kind="",
):
    response = _current()
    if response is None:
        return
    action = (action or "").lower()
    item = {
        "action": action,
        "path": _block(_redact_text(path), 1000),
        "lines_added": int(lines_added or 0),
        "lines_edited": int(lines_edited or 0),
        "lines_deleted": int(lines_deleted or 0),
        "bytes": int(bytes_written or 0),
        "dry_run": bool(dry_run),
        "summary": _short(_redact_text(summary), 160),
        "preview_kind": _short(preview_kind, 40),
        "preview": _preview_descriptor(preview, available=preview is not None),
    }
    with _LOCK:
        if not item["dry_run"]:
            if "create" in action:
                response["file_creates"] += 1
            elif "delete" in action:
                response["file_deletes"] += 1
            else:
                response["file_edits"] += 1
            response["lines_added"] += item["lines_added"]
            response["lines_edited"] += item["lines_edited"]
            response["lines_deleted"] += item["lines_deleted"]
        response["files"].append(item)
        if len(response["files"]) > 30:
            del response["files"][:-30]
        _event(response, "file_change", **item)


def snapshot():
    with _LOCK:
        active = [deepcopy(v) for v in sorted(_ACTIVE.values(), key=lambda r: r["started_at"])]
        now = time.time()
        for row in active:
            row["elapsed_ms"] = int((now - row.get("started_at", now)) * 1000)
        return {
            "active_count": len(active),
            "active": active,
            "latest": deepcopy(_LATEST),
            "total_tool_calls": _TOTAL_TOOL_CALLS,
            "event_ring": deepcopy(_EVENT_RING),
            "next_seq": _NEXT_EVENT_SEQUENCE,
            "dropped_events": _DROPPED_EVENTS,
        }


def _public_descriptor(value):
    if isinstance(value, dict) and value.get("state") == "disabled":
        return {
            "state": "disabled", "text": "", "chars": value.get("chars"),
            "truncated": bool(value.get("truncated")),
            "redacted": bool(value.get("redacted")),
        }
    if not isinstance(value, dict) or value.get("state") != "available":
        return _preview_descriptor(available=False)
    projected = _preview_descriptor(value.get("text", ""))
    chars = value.get("chars")
    projected["chars"] = int(chars) if isinstance(chars, int) and chars >= 0 else None
    projected["truncated"] = bool(value.get("truncated")) or projected["truncated"]
    projected["redacted"] = bool(value.get("redacted")) or projected["redacted"]
    return projected


def _disabled_descriptor(value=None):
    chars = None
    if isinstance(value, dict) and isinstance(value.get("chars"), int):
        chars = max(0, value["chars"])
    return {
        "state": "disabled", "text": "", "chars": chars,
        "truncated": bool(isinstance(value, dict) and value.get("truncated")),
        "redacted": bool(isinstance(value, dict) and value.get("redacted")),
    }


def _public_event(event, *, include_detail=False):
    event = event if isinstance(event, dict) else {}
    kind = _short(event.get("kind", "event"), 80)
    if kind == "response_start":
        phase = "started"
    elif kind == "file_change":
        phase = "previewed" if event.get("dry_run") else "applied"
    elif kind == "response_error" or event.get("ok") is False:
        phase = "failed"
    else:
        phase = "completed"
    out = {
        "ts": _short(event.get("ts", ""), 40),
        "elapsed_ms": max(0, int(event.get("elapsed_ms") or 0)),
        "kind": kind,
        "seq": max(0, int(event.get("seq") or 0)),
        "phase": phase,
    }
    is_npu = kind.startswith("npu_")
    if is_npu:
        if "ok" in event:
            out["ok"] = bool(event.get("ok"))
        for key, allowed in _NPU_PUBLIC_ENUMS.items():
            try:
                value = str(event.get(key) or "").strip().lower()
            except Exception:
                value = "unknown"
            out[key] = value if value in allowed else "unknown"
        # NPU observability is intentionally enum-only in both summary and
        # detail mode. A future provider error accidentally attached as a
        # path, preview, command, model, or summary must fail closed here.
        return out
    for key in ("tool", "title", "model", "action", "preview_kind"):
        if event.get(key) not in (None, ""):
            out[key] = _short(_redact_text(event.get(key)), 160)
    for key in (
        "prompt_chars", "history_messages", "tokens_in", "tokens_out",
        "cloud_model_calls", "answered", "failed", "unknown", "skipped",
        "lines_added", "lines_edited", "lines_deleted", "bytes",
    ):
        if key in event:
            try:
                out[key] = max(0, int(event.get(key) or 0))
            except (TypeError, ValueError):
                out[key] = 0
    for key in ("ok", "dry_run"):
        if key in event:
            out[key] = bool(event.get(key))
    if event.get("path") not in (None, ""):
        out["path"] = _safe_path(event.get("path"))
    if (
        include_detail
        and out["kind"] != "response_start"
        and event.get("summary") not in (None, "")
    ):
        out["summary"] = _block(_redact_text(event.get("summary")), 1000)
    if include_detail and "args" in event:
        out["args"] = _safe_args(event.get("args"))
    if include_detail and event.get("command") not in (None, ""):
        out["command"] = _safe_command(event.get("command"))
    if include_detail and event.get("output") not in (None, ""):
        out["output"] = _preview_descriptor(event.get("output"))["text"]
    for key in (
        "args_preview", "command_preview", "result_preview",
        "request_preview", "response_preview", "preview",
    ):
        if key in event:
            out[key] = (
                _public_descriptor(event.get(key))
                if include_detail else _disabled_descriptor(event.get(key))
            )
    return out


def _public_file(item, *, include_detail=False):
    item = item if isinstance(item, dict) else {}
    out = {
        "action": _short(item.get("action", "file"), 80),
        "path": _safe_path(item.get("path", "")),
        "lines_added": max(0, int(item.get("lines_added") or 0)),
        "lines_edited": max(0, int(item.get("lines_edited") or 0)),
        "lines_deleted": max(0, int(item.get("lines_deleted") or 0)),
        "bytes": max(0, int(item.get("bytes") or 0)),
        "dry_run": bool(item.get("dry_run")),
        "summary": (
            _short(_redact_text(item.get("summary", "")), 160)
            if include_detail else ""
        ),
        "preview_kind": _short(item.get("preview_kind", ""), 40),
        "preview": (
            _public_descriptor(item.get("preview"))
            if include_detail else _disabled_descriptor(item.get("preview"))
        ),
    }
    return out


def _public_response(response, *, include_detail=False):
    if not isinstance(response, dict):
        return None
    out = {}
    for key in ("id", "label", "surface", "model", "status", "started_ts"):
        out[key] = _short(_redact_text(response.get(key, "")), 160)
    for key in (
        "elapsed_ms", "tool_calls", "model_calls", "tokens_in", "tokens_out",
        "file_creates", "file_edits", "file_deletes", "lines_added",
        "lines_edited", "lines_deleted",
    ):
        out[key] = max(0, int(response.get(key) or 0))
    out["result_summary"] = (
        _block(_redact_text(response.get("result_summary", "")), 1000)
        if include_detail else ""
    )
    source_events = response.get("events") or []
    source_files = response.get("files") or []
    out["events"] = [
        _public_event(row, include_detail=include_detail)
        for row in source_events[-MAX_PUBLIC_EVENTS:]
    ]
    out["files"] = [
        _public_file(row, include_detail=include_detail)
        for row in source_files[-30:]
    ]
    checklist = response.get("checklist")
    if isinstance(checklist, dict):
        out["checklist"] = {
            "id": _short(checklist.get("id", ""), 100),
            "title": (
                _short(_redact_text(checklist.get("title", "")), 220)
                if include_detail else ""
            ),
            "status": _short(checklist.get("status", ""), 40),
            "summary": (
                _short(_redact_text(checklist.get("summary", "")), 220)
                if include_detail else ""
            ),
            "items": [
                {
                    "id": _short(row.get("id", ""), 100),
                    "title": (
                        _short(_redact_text(row.get("title", "")), 220)
                        if include_detail else ""
                    ),
                    "status": _short(row.get("status", ""), 40),
                }
                for row in (checklist.get("items") or [])[:80]
                if isinstance(row, dict)
            ],
        }
    else:
        out["checklist"] = None
    out["last_event"] = out["events"][-1] if out["events"] else None
    out["truncated"] = len(source_events) > len(out["events"]) or len(source_files) > len(out["files"])
    while len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_PUBLIC_RESPONSE_BYTES:
        out["truncated"] = True
        if out["events"]:
            out["events"].pop(0)
        elif out["files"]:
            out["files"].pop(0)
        elif (out.get("checklist") or {}).get("items"):
            out["checklist"]["items"].pop()
        else:
            out["result_summary"] = _short(out.get("result_summary", ""), 220)
            break
    out["last_event"] = out["events"][-1] if out["events"] else None
    return out


def public_response(response, *, include_detail=None):
    """Project one response without consulting the process-global snapshot.

    Request handlers that already own a completed span use this to avoid a
    concurrent response replacing ``latest`` between generation and encoding.
    """
    detail = detail_enabled() if include_detail is None else bool(include_detail)
    return _public_response(response, include_detail=detail)


def public_snapshot(source=None, *, include_detail=None):
    """App-safe activity shape compatible with the historical status payload."""
    source = snapshot() if source is None else source
    if not isinstance(source, dict):
        return None
    detail = detail_enabled() if include_detail is None else bool(include_detail)
    active = [
        projected for projected in (
            _public_response(row, include_detail=detail)
            for row in (source.get("active") or [])[-MAX_ACTIVE:]
        ) if projected is not None
    ]
    latest = _public_response(source.get("latest"), include_detail=detail)
    return {
        "active_count": max(0, int(source.get("active_count") or 0)),
        "active": active,
        "latest": latest,
        "total_tool_calls": max(0, int(source.get("total_tool_calls") or 0)),
        "projected": True,
        "detail_enabled": detail,
    }


def _feed_preview(value, *, available=True):
    if isinstance(value, dict) and "state" in value:
        return _public_descriptor(value)
    return _preview_descriptor(value, available=available)


def execution_feed(
    source=None,
    max_events=MAX_FEED_EVENTS,
    *,
    include_detail=None,
):
    """Flatten projected activity into the shared bounded execution feed."""
    raw_source = snapshot() if source is None else source
    detail = detail_enabled() if include_detail is None else bool(include_detail)
    public = public_snapshot(raw_source, include_detail=detail)
    if public is None:
        return {
            "schema_version": 1, "runtime_id": _RUNTIME_ID,
            "known": False, "active_responses": None, "events": [],
            "truncated": False, "redaction_applied": False,
            "oldest_seq": None, "next_seq": None, "dropped_events": None,
            "sequence_gap": None,
            "limits": {"events": MAX_FEED_EVENTS, "preview_chars": MAX_PUBLIC_PREVIEW},
            "error": "activity snapshot is unavailable",
        }
    ring = raw_source.get("event_ring") if isinstance(raw_source, dict) else None
    if isinstance(ring, list):
        responses = [
            {
                "id": _short(event.get("response_id", ""), 80),
                "status": _short(event.get("response_status", "unknown"), 40),
                "events": [_public_event(event, include_detail=detail)],
            }
            for event in ring
            if isinstance(event, dict)
        ]
    else:
        responses = list(public.get("active") or [])
        latest = public.get("latest")
        if latest and not any(row.get("id") == latest.get("id") for row in responses):
            responses.append(latest)
    rows = []
    for response in responses:
        for event in response.get("events") or []:
            row = {
                "response_id": response.get("id", ""),
                "response_status": response.get("status", "unknown"),
                "seq": max(0, int(event.get("seq") or 0)),
                "ts": event.get("ts", ""),
                "elapsed_ms": event.get("elapsed_ms", 0),
                "kind": event.get("kind", "event"),
                "phase": event.get("phase", "completed"),
            }
            kind = row["kind"]
            if kind == "model_call":
                row.update({
                    key: event.get(key) for key in (
                        "model", "prompt_chars", "history_messages", "tokens_in",
                        "tokens_out", "ok",
                    ) if key in event
                })
                row["request_preview"] = _feed_preview(event.get("request_preview"))
                row["response_preview"] = _feed_preview(event.get("response_preview"))
            elif kind == "model_fanout":
                # Fanout observability is intentionally scalar-only.  The
                # durable receipt is the owner-scoped place for model names,
                # answers, failure detail, and other sensitive material.
                row.update({
                    key: event.get(key) for key in (
                        "cloud_model_calls", "answered", "failed", "unknown", "skipped",
                        "tokens_in", "tokens_out",
                    ) if key in event
                })
            elif kind == "tool_call":
                row.update({
                    key: event.get(key) for key in ("tool", "title", "ok") if key in event
                })
                row["args_preview"] = _feed_preview(event.get("args_preview"))
                row["command_preview"] = _feed_preview(event.get("command_preview"))
                row["result_preview"] = _feed_preview(event.get("result_preview"))
            elif kind == "file_change":
                row.update({
                    key: event.get(key) for key in (
                        "action", "path", "lines_added", "lines_edited",
                        "lines_deleted", "bytes", "dry_run", "preview_kind",
                    ) if key in event
                })
                row["content_preview"] = _feed_preview(event.get("preview"))
            elif kind.startswith("npu_"):
                row.update({
                    key: event.get(key, "unknown") for key in (
                        "capability", "reason", "operation_mode",
                        "fallback_handler", "handler_state", "ok",
                    )
                    if key in event
                })
            else:
                summary = event.get("summary")
                row["summary_preview"] = _feed_preview(
                    summary, available=summary not in (None, ""),
                )
            rows.append(row)
    bounded = max(1, min(MAX_FEED_EVENTS, int(max_events or MAX_FEED_EVENTS)))
    selected = rows[-bounded:]
    descriptors = [
        value for row in selected for key, value in row.items()
        if key.endswith("_preview") and isinstance(value, dict)
    ]
    dropped_events = max(0, int(raw_source.get("dropped_events") or 0))
    next_seq = max(1, int(raw_source.get("next_seq") or _NEXT_EVENT_SEQUENCE))
    oldest_seq = (
        min((int(row.get("seq") or 0) for row in rows if int(row.get("seq") or 0) > 0), default=next_seq)
    )
    result = {
        "schema_version": 1,
        "runtime_id": _RUNTIME_ID,
        "known": True,
        "active_responses": public["active_count"],
        "events": selected,
        "truncated": (
            len(rows) > len(selected) or dropped_events > 0
            or any(row.get("truncated") for row in descriptors)
        ),
        "redaction_applied": any(row.get("redacted") for row in descriptors),
        "oldest_seq": oldest_seq,
        "next_seq": next_seq,
        "dropped_events": dropped_events,
        "sequence_gap": dropped_events,
        "limits": {"events": bounded, "preview_chars": MAX_PUBLIC_PREVIEW},
        "error": "",
    }
    result["bytes"] = 0
    while True:
        for _ in range(3):
            result["bytes"] = len(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        if result["bytes"] <= MAX_FEED_BYTES or not result["events"]:
            break
        result["events"].pop(0)
        result["truncated"] = True
    return result


# Feed states use the "Running ... / Ran ..." vocabulary: terminal span
# statuses map onto it, host-established verdicts pass through unchanged.
_FEED_STATES = {"complete": "completed", "error": "failed"}


def _operation_snapshot(response, now=None):
    """The one operation a live span is doing (running) or last did (ran).

    Deliberately category/name/state/elapsed only: the underlying events carry
    argument and output previews, and none of that belongs in the feed.
    """
    current = response.get("current_operation")
    if isinstance(current, dict):
        moment = time.time() if now is None else now
        elapsed = int((moment - float(current.get("started_at") or moment)) * 1000)
        return {
            "category": _short(current.get("category", "tool"), 20),
            "name": _short(_redact_text(current.get("name", "")), 80),
            "state": "running",
            "elapsed_ms": max(0, elapsed),
        }
    last = response.get("last_event")
    if isinstance(last, dict) and last.get("kind") in ("tool_call", "model_call"):
        name = last.get("title") or last.get("model") or last.get("tool") or ""
        return {
            "category": "tool" if last.get("kind") == "tool_call" else "model",
            "name": _short(_redact_text(name), 80),
            # Tool and model events overwrite the span-relative elapsed with
            # the operation's own duration, so this is "how long it ran".
            "state": "completed" if last.get("ok", True) else "failed",
            "elapsed_ms": max(0, int(last.get("elapsed_ms") or 0)),
        }
    return None


def _feed_entry(response, *, live):
    now = time.time()
    if live:
        elapsed = int((now - float(response.get("started_at") or now)) * 1000)
    else:
        elapsed = int(response.get("elapsed_ms") or 0)
    status = str(response.get("status") or "unknown")
    return {
        "id": _short(response.get("id", ""), 40),
        "category": "response",
        "name": _short(_redact_text(response.get("label", "")), 80),
        "surface": _short(response.get("surface", ""), 40),
        "state": _FEED_STATES.get(status, status),
        "started_ts": _short(response.get("started_ts", ""), 40),
        "elapsed_ms": max(0, elapsed),
        "model_calls": max(0, int(response.get("model_calls") or 0)),
        "tool_calls": max(0, int(response.get("tool_calls") or 0)),
        "summary": (
            "" if live
            else _short(_redact_text(response.get("result_summary", "")), 220)
        ),
        "operation": _operation_snapshot(response, now) if live else None,
    }


def _append_owner_entry(owner, response):
    """Retire a closing span into its owner's bounded recent-work bucket.

    Caller holds ``_LOCK``. Popping and re-inserting the bucket keeps
    ``_OWNER_RECENT`` ordered by last update, so exceeding ``MAX_FEED_OWNERS``
    evicts the least recently active principal, never the busiest one.
    """
    owner = str(owner or "")
    entries = _OWNER_RECENT.pop(owner, [])
    entries.append(_feed_entry(response, live=False))
    if len(entries) > MAX_OWNER_FEED_ENTRIES:
        del entries[:-MAX_OWNER_FEED_ENTRIES]
    _OWNER_RECENT[owner] = entries
    while len(_OWNER_RECENT) > MAX_FEED_OWNERS:
        _OWNER_RECENT.pop(next(iter(_OWNER_RECENT)), None)


def live_feed_for_owner(owner):
    """Bounded live execution feed for exactly one opaque owner principal.

    Owner "" is the local unowned domain (REPL, local MCP, and unauthenticated
    single-operator deployments); an authenticated HTTP caller always arrives
    with a nonempty principal hash, so the two domains can never observe each
    other. Entries carry operation category/name, state, elapsed time, and a
    redacted summary -- never prompts, arguments, paths, outputs, reasoning,
    or another owner's work.
    """
    wanted = str(owner or "")
    with _LOCK:
        active = [
            _feed_entry(response, live=True)
            for response in sorted(
                _ACTIVE.values(), key=lambda row: row["started_at"],
            )
            if _FEED_OWNERS.get(response["id"], "") == wanted
        ]
        recent = [dict(entry) for entry in reversed(_OWNER_RECENT.get(wanted, []))]
    return {
        "schema_version": 1,
        "known": True,
        "owner_scoped": True,
        "active": active,
        "recent": recent,
        "limits": {"active": MAX_ACTIVE, "recent": MAX_OWNER_FEED_ENTRIES},
    }


_TERMINAL_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)


def _terminal_safe(value):
    return _TERMINAL_CONTROL_RE.sub("?", str(value or ""))


def format_execution_feed(feed):
    if not isinstance(feed, dict) or not feed.get("known"):
        return "live execution feed: unknown"
    lines = [
        "live execution feed: %d event(s)%s" % (
            len(feed.get("events") or []),
            " (truncated)" if feed.get("truncated") else "",
        )
    ]
    if feed.get("oldest_seq") is not None and feed.get("next_seq") is not None:
        lines.append(
            "  seq: %s..%s | dropped before window: %s" % (
                feed.get("oldest_seq"), max(0, int(feed.get("next_seq") or 1) - 1),
                feed.get("dropped_events", 0),
            )
        )
    for event in (feed.get("events") or [])[-12:]:
        kind = event.get("kind", "event")
        if str(kind).startswith("npu_"):
            detail = "%s reason=%s mode=%s handler=%s/%s" % (
                event.get("capability", "unknown"),
                event.get("reason", "unknown"),
                event.get("operation_mode", "unknown"),
                event.get("fallback_handler", "unknown"),
                event.get("handler_state", "unknown"),
            )
        elif kind == "model_fanout":
            detail = "cloud calls=%s answered=%s failed=%s unknown=%s skipped=%s tok=%s/%s" % (
                event.get("cloud_model_calls", 0), event.get("answered", 0),
                event.get("failed", 0), event.get("unknown", 0), event.get("skipped", 0),
                event.get("tokens_in", 0), event.get("tokens_out", 0),
            )
        else:
            detail = event.get("tool") or event.get("model") or event.get("path") or ""
        lines.append("  +%sms %s %s" % (
            event.get("elapsed_ms", 0), _terminal_safe(kind), _terminal_safe(detail),
        ))
        preview = (
            event.get("response_preview") or event.get("result_preview")
            or event.get("content_preview") or event.get("summary_preview")
        )
        if isinstance(preview, dict):
            state = preview.get("state", "unavailable")
            text = preview.get("text", "")
            suffix = " [truncated]" if preview.get("truncated") else ""
            suffix += " [redacted]" if preview.get("redacted") else ""
            lines.append("    preview: %s%s" % (
                _terminal_safe(text if state == "available" else state), suffix,
            ))
    return "\n".join(lines)


def latest():
    with _LOCK:
        return deepcopy(_LATEST)


def current():
    response = _current()
    return deepcopy(response) if response else None


def _args_text(args):
    if not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def format_transcript(response=None, max_actions=30):
    response = response or _current() or latest()
    if not response:
        return "(no actions yet)"
    actions = [
        event for event in (response.get("events") or [])
        if event.get("kind") == "tool_call"
    ][-_bounded_action_count(max_actions):]
    if not actions:
        return "(no tool actions)"
    lines = []
    for event in actions:
        marker = "•" if event.get("ok", True) else "×"
        lines.append("%s %s" % (marker, event.get("title") or action_title(event.get("tool"))))
        command = event.get("command") or ""
        args_text = _args_text(event.get("args"))
        output = event.get("output") or event.get("summary") or ""
        detail = command or args_text
        if detail:
            detail_lines = detail.splitlines() or [detail]
            for line in detail_lines[:-1]:
                lines.append("  │ %s" % line)
            lines.append("  │ %s" % detail_lines[-1])
        if output:
            output_lines = output.splitlines() or [output]
            if len(output_lines) > 12:
                output_lines = output_lines[:10] + ["... (%d more lines)" % (len(output_lines) - 10)]
            for line in output_lines[:-1]:
                lines.append("  │ %s" % line)
            lines.append("  └ %s" % output_lines[-1])
        elif detail:
            lines[-1] = lines[-1].replace("  │ ", "  └ ", 1)
    return "\n".join(lines)


def _bounded_action_count(value):
    try:
        return max(1, min(80, int(value or 30)))
    except (TypeError, ValueError):
        return 30


def format_end_report(response=None, *, calibration_line=""):
    """Render the end report, optionally under a caller-supplied standing line.

    ``calibration_line`` is composed by the caller and passed in whole. This
    module is deliberately dependency-free and holds no database connection,
    so it cannot measure a standing itself and must not learn how: the tracker
    records what happened; what the measured record says about a claim is
    somebody else's fact. Defaulting to "" keeps the uncomposed report
    byte-identical for every existing caller.
    """
    response = response or _current() or latest()
    if not response:
        return "=== END REPORT ===\nresult: unavailable"
    checklist = response.get("checklist") or {}
    items = checklist.get("items") or []
    done = sum(1 for item in items if item.get("status") == "done")
    standing = str(calibration_line or "").strip()
    lines = [
        "=== END REPORT ===",
        "result: %s" % response.get("status", "unknown"),
        # Directly under the result it qualifies, not in a footer that a
        # reader quoting "result: complete" would never reach.
        *([standing] if standing else []),
        "elapsed: %sms | model calls: %s | tool calls: %s" % (
            response.get("elapsed_ms", 0), response.get("model_calls", 0),
            response.get("tool_calls", 0),
        ),
        "files: +%s ~%s -%s | lines: +%s ~%s -%s" % (
            response.get("file_creates", 0), response.get("file_edits", 0),
            response.get("file_deletes", 0), response.get("lines_added", 0),
            response.get("lines_edited", 0), response.get("lines_deleted", 0),
        ),
    ]
    if items:
        lines.append("checklist: %d/%d complete" % (done, len(items)))
        symbols = {"done": "[x]", "in_progress": "[~]", "blocked": "[!]"}
        for item in items:
            lines.append("  %s %s" % (symbols.get(item.get("status"), "[ ]"), item.get("title", "")))
    if response.get("result_summary"):
        lines.append("summary: %s" % response["result_summary"])
    files = response.get("files") or []
    if files:
        lines.append("changed paths:")
        for item in files[-12:]:
            lines.append("  - %s %s" % (item.get("action", "file"), item.get("path", "")))
    return "\n".join(lines)


def format_response(response=None):
    response = response or _current() or latest()
    if not response:
        return "activity: none yet"
    if response.get("status") == "running" and response.get("started_at"):
        response = deepcopy(response)
        response["elapsed_ms"] = int((time.time() - response["started_at"]) * 1000)
    lines = [
        "=== ACTIVITY (observable work) ===",
        "response: %(id)s %(status)s %(label)s in %(elapsed_ms)sms" % response,
        "model calls: %s   tool calls: %s   tokens in/out: %s/%s" % (
            response.get("model_calls", 0),
            response.get("tool_calls", 0),
            response.get("tokens_in", 0),
            response.get("tokens_out", 0),
        ),
        "files: +%s ~%s -%s   lines: +%s ~%s -%s" % (
            response.get("file_creates", 0),
            response.get("file_edits", 0),
            response.get("file_deletes", 0),
            response.get("lines_added", 0),
            response.get("lines_edited", 0),
            response.get("lines_deleted", 0),
        ),
    ]
    files = response.get("files") or []
    if files:
        lines.append("file changes:")
        for item in files[-8:]:
            suffix = " dry-run" if item.get("dry_run") else ""
            lines.append(
                "  %(action)s %(path)s  lines +%(lines_added)s ~%(lines_edited)s -%(lines_deleted)s%(suffix)s"
                % {**item, "suffix": suffix}
            )
    events = response.get("events") or []
    if events:
        lines.append("recent events:")
        for event in events[-10:]:
            detail = event.get("summary") or event.get("tool") or event.get("path") or event.get("model") or ""
            lines.append("  +%sms %s %s" % (event.get("elapsed_ms", 0), event.get("kind", ""), detail))
    transcript = format_transcript(response)
    if transcript != "(no tool actions)":
        lines.extend(["actions:", transcript])
    lines.append("=== END ACTIVITY ===")
    return "\n".join(lines)


def reset_for_tests():
    with _LOCK:
        global _LATEST, _TOTAL_TOOL_CALLS, _LATEST_REASONING, _LATEST_REASONING_OWNER
        global _NEXT_EVENT_SEQUENCE, _DROPPED_EVENTS
        _ACTIVE.clear()
        _LATEST = None
        _TOTAL_TOOL_CALLS = 0
        _EVENT_RING.clear()
        _NEXT_EVENT_SEQUENCE = 1
        _DROPPED_EVENTS = 0
        _REASONING.clear()
        _REASONING_OWNERS.clear()
        _FEED_OWNERS.clear()
        _OWNER_RECENT.clear()
        _LATEST_REASONING = None
        _LATEST_REASONING_OWNER = ""
    _LOCAL.response_id = None
