"""
local-llm MCP server
---------------------
Bridges Claude Code to a local Ollama instance running on the RTX 4050 (6 GB VRAM).

Design goals:
  * Claude decides WHEN to offload (tools are opt-in), so VRAM is idle until used.
  * Tiered models: only one sits in VRAM at a time; short keep_alive frees it fast.
  * Zero third-party HTTP deps (stdlib urllib) -> only `mcp` is required.

Tiers (escalation ladder, cheapest first):
  LOCAL  (private, free, offline, runs on the 6 GB 4050):
    fast        -> qwen2.5:3b            (~2 GB, fully GPU-resident, snappy)
    code        -> qwen2.5-coder:7b      (~4.7 GB Q4, strong coding model)
    general     -> qwen2.5:7b-instruct   (~4.7 GB Q4, general text grunt-work)
  CLOUD  (Ollama-hosted, huge, metered; prompt leaves this machine):
    cloud-code  -> qwen3-coder:480b-cloud (frontier coding, no local VRAM cost)
    cloud-general -> gpt-oss:120b-cloud   (heavy reasoning over text)
"""

import json
import os
import re
import urllib.request
import urllib.error

import memory_store
import orchestrator
import retriever
import reward
import reflection
import embeddings
import personas
import recall
import summarizer

from mcp.server.fastmcp import FastMCP

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").replace("http://", "")
BASE = f"http://{OLLAMA_HOST}"
# How long a model stays in VRAM after its last call. Short = frees GPU quickly.
KEEP_ALIVE = os.environ.get("LOCAL_LLM_KEEP_ALIVE", "2m")
TIMEOUT = int(os.environ.get("LOCAL_LLM_TIMEOUT", "300"))

TIERS = {
    "fast": os.environ.get("LOCAL_LLM_FAST", "qwen2.5:3b"),
    "code": os.environ.get("LOCAL_LLM_CODE", "qwen2.5-coder:7b"),
    "general": os.environ.get("LOCAL_LLM_GENERAL", "qwen2.5:7b-instruct"),
    "cloud-code": os.environ.get("LOCAL_LLM_CLOUD_CODE", "qwen3-coder:480b-cloud"),
    "cloud-general": os.environ.get("LOCAL_LLM_CLOUD_GENERAL", "gpt-oss:120b-cloud"),
}
# Tiers whose ":...-cloud" model runs on Ollama's servers (data leaves the machine).
CLOUD_TIERS = {"cloud-code", "cloud-general"}

# Which offload tiers feed the learning loop (capture + distill lessons). A stronger
# paid/cloud model makes an excellent *teacher*: its grounded good outcomes become
# lessons and fine-tuning data the local student retrieves later. Local 'code' stays
# in by default; cloud tiers now do too. Override machine-wide with e.g.
# TRILOBITE_LEARN_TIERS="code" (local only) or "code,cloud-code,cloud-general,general".
LEARN_TIERS = {
    t.strip()
    for t in os.environ.get(
        "TRILOBITE_LEARN_TIERS", "code,cloud-code,cloud-general"
    ).split(",")
    if t.strip()
}

# strict=True pins trilobite to the fine-tuned alias only (errors if missing) instead
# of silently falling back to the base coder model. Env default lets ops flip this
# machine-wide without touching call sites.
_STRICT_DEFAULT = os.environ.get("TRILOBITE_STRICT", "").strip().lower() in ("1", "true", "yes", "on")

# Conversation memory is ON by default: a call with no explicit session threads the
# shared DEFAULT_SESSION so follow-ups are remembered. Pass session="none" to opt out
# (single-turn), or a distinct id to isolate a thread. Same idea for project facts.
DEFAULT_SESSION = os.environ.get("TRILOBITE_DEFAULT_SESSION", "default")
DEFAULT_PROJECT = os.environ.get("TRILOBITE_DEFAULT_PROJECT", "default")
# Sessioned calls get a roomier context (fits easily on the 6 GB 4050) and keep the
# last MAX_TURNS turns live; older turns are rolled into a summary.
SESSION_NUM_CTX = int(os.environ.get("LOCAL_LLM_SESSION_NUM_CTX", "8192"))
MAX_TURNS = int(os.environ.get("TRILOBITE_MAX_TURNS", "12"))

_DB_PATH = os.path.join(os.path.dirname(__file__), "memory.db")

FOOTER_PREFIX = "\n\n[interaction_id: "
_FOOTER_RE = re.compile(r"\[interaction_id: ([0-9a-f]+)\]\s*$")


def _open_db():
    return memory_store.connect(_DB_PATH, check_same_thread=True)


def with_footer(text, interaction_id):
    return "%s%s%s]" % (text, FOOTER_PREFIX, interaction_id)


def parse_interaction_id(text):
    m = _FOOTER_RE.search(text or "")
    return m.group(1) if m else None


TRACE_SYSTEM = (
    "Before giving your answer, output a section titled '## Reasoning' where you "
    "think step by step: restate the task in your own words, note constraints and "
    "edge cases, and explain your approach and any tradeoffs. Then output a section "
    "titled '## Answer' with the final solution."
)


def _format_trace(model, tier, params, trace):
    lessons = trace.get("lessons", [])
    lines = [
        "",
        "=== TRACE (how trilobite decided) ===",
        "model: %s   tier: %s" % (model, tier),
        "generation params: %r" % (params,),
        "lessons retrieved: %d" % len(lessons),
    ]
    for l in lessons:
        lines.append("   - %s" % l)
    lines.append("--- exact prompt sent to the model ---")
    lines.append(trace.get("augmented_prompt", ""))
    lines.append("=== END TRACE ===")
    return "\n".join(lines)


def _should_learn(tier, learn):
    # A tier feeds the learning loop when it is in LEARN_TIERS (env-configurable) and
    # the caller didn't opt out with learn=False. Defaults: local 'code' plus the
    # cloud tiers (teacher distillation); 'fast'/'general' stay mechanical.
    return bool(learn) and tier in LEARN_TIERS


def resolve_trilobite_model(strict=False):
    try:
        tags = [m.get("name", "") for m in _get("/api/tags").get("models", [])]
    except Exception:
        tags = []
    if any(t.split(":")[0] == "trilobite" for t in tags):
        return "trilobite"
    return None if strict else TIERS["code"]


def _make_generate(model, system, temperature, num_predict, num_ctx, cloud=False):
    """Build a generate(prompt, history) closure for `model`.

    cloud=True targets an Ollama-hosted model: keep_alive and num_ctx are omitted
    (they're VRAM/local-context knobs the remote tier doesn't take), matching how the
    non-learning cloud path posts.
    """
    def gen(prompt, history=None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        options = {"temperature": temperature, "num_predict": num_predict}
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options}
        if not cloud:
            options["num_ctx"] = num_ctx
            payload["keep_alive"] = KEEP_ALIVE
        out = _post("/api/chat", payload)
        return out.get("message", {}).get("content", "")
    return gen


def _no_retrieve(conn, task):
    """Retrieve hook that injects nothing — used for 'teacher' (clean) generation so a
    strong model answers at full strength without local-lesson augmentation, while its
    output is still captured for grounding + distillation."""
    return []


def _generate_text(prompt, tier="fast", system="", temperature=0.2,
                   num_predict=256, num_ctx=2048):
    model = TIERS.get(tier, TIERS["fast"])
    return _make_generate(model, system, temperature, num_predict, num_ctx)(prompt)


def _resolve_session(session):
    """"" -> DEFAULT_SESSION (memory on by default); "none" -> None (single turn)."""
    s = (session or "").strip()
    if s == "":
        return DEFAULT_SESSION
    if s.lower() == "none":
        return None
    return s


def _resolve_project(project):
    """Same convention as sessions: "" -> DEFAULT_PROJECT, "none" -> None."""
    p = (project or "").strip()
    if p == "":
        return DEFAULT_PROJECT
    if p.lower() == "none":
        return None
    return p


def _build_system(system, trace, persona):
    """Compose the effective system prompt from a base `system`, optional trace
    instruction, and optional persona (persona goes first)."""
    effective_system = system
    if trace:
        effective_system = "%s\n\n%s" % (system, TRACE_SYSTEM) if system else TRACE_SYSTEM
    if persona and persona.strip():
        persona_prompt = personas.get(persona)
        effective_system = (
            "%s\n\n%s" % (persona_prompt, effective_system) if effective_system else persona_prompt
        )
    return effective_system


def _resolve_model_and_system(system, trace, strict, persona):
    """Shared prep for the trilobite tool and the serve layer.

    Returns (model, effective_system); model is None if the strict alias is missing.
    """
    strict_eff = _STRICT_DEFAULT if strict is None else strict
    model = resolve_trilobite_model(strict_eff)
    if model is None:
        return None, None
    return model, _build_system(system, trace, persona)


def _serve_target(tier, strict):
    """Resolve a serve/app request's OpenAI `model` field to a concrete target.

    Returns (model, cloud, augment, tier_label):
      - model:      the Ollama model to generate with (None if a strict alias is
                    missing, or tier_label is None for an unknown name)
      - cloud:      True if it runs on Ollama's servers (payload omits VRAM knobs)
      - augment:    inject facts/lessons/recall? Only the local student ('code'/
                    trilobite) does; any other model answers clean (teacher mode)
      - tier_label: what to record on the interaction (None => unknown model)

    Default / "" / "trilobite" / "local" => the local self-improving student.
    Any TIERS key (e.g. "cloud-code", "general") selects that model directly, so a
    single server can drive many models — pick per request.
    """
    t = (tier or "").strip().lower()
    if t in ("", "trilobite", "local"):
        strict_eff = _STRICT_DEFAULT if strict is None else strict
        return resolve_trilobite_model(strict_eff), False, True, "trilobite"
    if t in TIERS:
        return TIERS[t], t in CLOUD_TIERS, t == "code", t
    return None, False, True, None


def _session_history_messages(conn, session_id, max_turns):
    """Build the prior-turn chat messages for a session, summarizing overflow.

    Turns older than the last `max_turns` are folded (once) into sessions.summary via
    the fast tier; the summary is prepended as a system message so nothing is lost.
    Summarization is best-effort: if it fails, we simply send the live turns.
    """
    turns = memory_store.session_turns(conn, session_id)
    sess = memory_store.get_session(conn, session_id) or {}
    summary = sess.get("summary")
    summarized_through = sess.get("summarized_through")

    if max_turns and len(turns) > max_turns:
        live = turns[-max_turns:]
        window_start = len(turns) - len(live)
        marker_idx = -1
        if summarized_through:
            for i, t in enumerate(turns):
                if t["id"] == summarized_through:
                    marker_idx = i
                    break
        new_overflow = turns[marker_idx + 1:window_start]
        if new_overflow:
            pairs = [(t["task"], t["response"]) for t in new_overflow]
            try:
                summary = summarizer.summarize(summary, pairs, _generate_text)
                memory_store.update_session_summary(
                    conn, session_id, summary, new_overflow[-1]["id"]
                )
            except urllib.error.URLError:
                pass  # keep prior summary; live turns still carry recent context
    else:
        live = turns

    msgs = []
    if summary:
        msgs.append({"role": "system",
                     "content": "Earlier in this conversation:\n%s" % summary})
    for t in live:
        msgs.append({"role": "user", "content": t["task"]})
        msgs.append({"role": "assistant", "content": t["response"]})
    return msgs


def _maybe_title(conn, session_id, first_prompt):
    """Give a brand-new session a short title (best-effort, never fatal)."""
    sess = memory_store.get_session(conn, session_id) or {}
    if sess.get("title"):
        return
    try:
        title = summarizer.make_title(first_prompt, _generate_text)
    except urllib.error.URLError:
        title = (first_prompt or "").strip()[:40]
    memory_store.set_session_title(conn, session_id, title)


def _answer(conn, prompt, model, effective_system, temperature, num_predict,
            num_ctx, session_id, project, history, trace=False,
            tier="trilobite", cloud=False, augment=True):
    """Core answer path shared by the tool and serve: (optionally) augment
    (facts/lessons/recall), generate with `history`, capture. Returns
    (response, interaction_id, trace_ctx).

    tier      -> recorded on the interaction (so training data knows its source).
    cloud     -> generate against an Ollama-hosted model (omit VRAM knobs).
    augment   -> False runs 'teacher' mode: no lesson/fact/recall injection (the model
                 answers clean), but the turn is still captured (with its task
                 embedding) so record_outcome can ground and distill it.
    """
    gen = _make_generate(model, effective_system, temperature, num_predict, num_ctx,
                         cloud=cloud)
    qv = embeddings.embed(prompt)
    blob = embeddings.to_blob(qv) if qv else None
    if augment:
        recalls = recall.recall(conn, prompt, qv=qv, exclude_session=session_id)
        facts = None
        if project:
            facts = [f["text"] for f in memory_store.facts_for_project(conn, project)]
        retrieve_fn = retriever.retrieve
    else:
        recalls = None
        facts = None
        retrieve_fn = _no_retrieve
    if trace:
        resp, iid, tctx = orchestrator.run_with_learning_traced(
            conn, prompt, tier, gen, retrieve_fn=retrieve_fn, history=history,
            recalls=recalls, facts=facts, session_id=session_id, task_embedding=blob,
        )
        return resp, iid, tctx
    resp, iid = orchestrator.run_with_learning(
        conn, prompt, tier, gen, retrieve_fn=retrieve_fn, history=history,
        recalls=recalls, facts=facts, session_id=session_id, task_embedding=blob,
    )
    return resp, iid, None


mcp = FastMCP("local-llm")


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


@mcp.tool()
def offload(
    prompt: str,
    tier: str = "fast",
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 4096,
    learn: bool = True,
) -> str:
    """Offload a self-contained subtask to a local-GPU or Ollama-cloud model.

    Local tiers (fast/code/general) run privately on the 6 GB 4050. The learning tiers
    (TRILOBITE_LEARN_TIERS, default local 'code' + both cloud tiers) participate in the
    lesson loop: with learn=True (default) the call is captured and the response ends
    with a '[interaction_id: <id>]' footer you can pass to record_outcome once you know
    whether it compiled / passed tests, so a good outcome distills a lesson. The local
    'code' tier is also memory-augmented (student); cloud tiers answer CLEAN (teacher)
    but are still captured — so a paid frontier model's grounded wins become lessons and
    fine-tuning data for the local model. 'fast'/'general' (mechanical work) and
    learn=False run the plain path: no capture, no footer, just text.

    Tiers: fast=3B (default), code=7B coder, general=7B instruct,
    cloud-code / cloud-general (METERED, prompt leaves this machine).
    Give a FULLY self-contained prompt (the model can't see this chat or your files).
    """
    model = TIERS.get(tier)
    if model is None:
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, ", ".join(TIERS))

    # Only the local 'code' tier (with learn not disabled) takes the learning path.
    if not _should_learn(tier, learn):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options = {"temperature": temperature, "num_predict": num_predict}
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options}
        if tier not in CLOUD_TIERS:
            payload["keep_alive"] = KEEP_ALIVE
            options["num_ctx"] = num_ctx
        try:
            out = _post("/api/chat", payload)
        except urllib.error.URLError as e:
            return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                    "running? (the tray app / `ollama serve`)" % (BASE, e))
        msg = out.get("message", {}).get("content", "")
        return msg if msg else "(empty response) raw=%s" % json.dumps(out)[:500]

    # Learning path. Local tiers are answered by the trilobite student model/alias and
    # augmented with lessons (consistent with the trilobite tool). Cloud tiers act as a
    # 'teacher': the actual cloud model answers CLEAN (no augmentation), and its grounded
    # good outcomes are still captured + distilled into lessons for the local student.
    retrieve_kwargs = {}
    if tier in CLOUD_TIERS:
        gen = _make_generate(model, system, temperature, num_predict, num_ctx, cloud=True)
        retrieve_kwargs["retrieve_fn"] = _no_retrieve
    else:
        learning_model = resolve_trilobite_model(_STRICT_DEFAULT)
        if learning_model is None:
            return ("ERROR: trilobite model/alias not found. Run setup_alias.py, or call "
                    "with strict=False to fall back to the base coder.")
        gen = _make_generate(learning_model, system, temperature, num_predict, num_ctx)
    conn = _open_db()
    try:
        response, iid = orchestrator.run_with_learning(
            conn, prompt, tier, gen, **retrieve_kwargs)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()
    return with_footer(response, iid)


@mcp.tool()
def trilobite(
    prompt: str,
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 4096,
    trace: bool = False,
    strict: bool = None,
    persona: str = "",
    session: str = "",
    project: str = "",
) -> str:
    """Ask 'trilobite', the local self-improving coding model, for help.

    This is the interactive front door to the same learning loop the fleet uses:
    the prompt is augmented with project facts, lessons distilled from past work, and
    similar past solutions, answered locally on the 4050, captured, and returned with
    a '[interaction_id: <id>]' footer. After you learn how it went, call
    record_outcome(<id>, "tests_passed" | "accepted" | "compiled" | "rejected" |
    "failed") so trilobite gets better over time. trilobite is local-only and always
    uses the local coder model/alias — it never routes to another tier or the cloud;
    use offload for that. Defaults to the 7B coder base model, or the 'trilobite'
    Ollama alias if it exists.

    CONVERSATION MEMORY IS ON BY DEFAULT. Successive calls remember each other: with
    no `session`, the shared "default" thread is used, so follow-ups have context.
    Pass a distinct `session` id to keep an isolated thread (recommended: one id per
    conversation), or session="none" for a one-off single-turn answer. Threads persist
    in memory.db across restarts; older turns are auto-summarized to stay in the local
    context window (the most recent turns are kept verbatim). Use trilobite_sessions()
    to list threads.

    `project` scopes durable facts (see trilobite_remember_fact); those facts are
    always injected. No project -> the "default" project; project="none" -> no facts.

    trace=True instructs the model to externalize its step-by-step reasoning
    ('## Reasoning' then '## Answer'), and appends a TRACE block showing the SYSTEM's
    actual decision context (retrieved lessons, exact augmented prompt, model/params).

    strict=True (or env TRILOBITE_STRICT=1) pins this call to the fine-tuned
    'trilobite' alias only, erroring if it isn't installed instead of falling back.

    persona selects one of personas.names() (e.g. "explainer", "reviewer", "teacher")
    to steer tone; its system prompt is prepended ahead of `system`/trace instructions.
    """
    model, effective_system = _resolve_model_and_system(system, trace, strict, persona)
    if model is None:
        return ("ERROR: trilobite model/alias not found. Run setup_alias.py, or call "
                "with strict=False to fall back to the base coder.")

    session_id = _resolve_session(session)
    project_id = _resolve_project(project)
    # Sessioned threads get the roomier context window; honor a larger explicit num_ctx.
    num_ctx_eff = max(num_ctx, SESSION_NUM_CTX) if session_id else num_ctx

    conn = _open_db()
    try:
        history = None
        is_first = False
        if session_id:
            is_first = memory_store.session_turn_count(conn, session_id) == 0
            memory_store.touch_session(conn, session_id, project_id)
            history = _session_history_messages(conn, session_id, MAX_TURNS)
        response, iid, trace_ctx = _answer(
            conn, prompt, model, effective_system, temperature, num_predict,
            num_ctx_eff, session_id, project_id, history, trace=trace,
        )
        if session_id and is_first:
            _maybe_title(conn, session_id, prompt)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()

    if trace:
        params = {"temperature": temperature, "num_predict": num_predict, "num_ctx": num_ctx_eff}
        trace_block = _format_trace(model, "trilobite", params, trace_ctx)
        # Footer must stay LAST so parse_interaction_id's $-anchored regex still finds it.
        return with_footer(response + trace_block, iid)
    return with_footer(response, iid)


def answer_with_history(prompt, history, trace=False, strict=None, tier=None):
    """Answer a turn using caller-supplied prior `history` (list of {role, content}).

    For the OpenAI-compatible serve layer, where the chat UI owns the conversation:
    history comes from the request, not the DB, so no DB session is threaded.

    `tier` maps the request's OpenAI `model` field to a target (see _serve_target):
    default/"trilobite" is the local self-improving student (augmented with facts +
    lessons); any other tier (e.g. a paid cloud model) answers CLEAN as a teacher. The
    turn is always captured so record_outcome can ground it and distill lessons — so
    the app learns from whatever model you point it at. Returns the reply (with footer).
    """
    model, cloud, augment, tier_label = _serve_target(tier, strict)
    if tier_label is None:
        return "ERROR: unknown model '%s'. Valid: trilobite, %s." % (
            tier, ", ".join(TIERS))
    if model is None:
        return ("ERROR: trilobite model/alias not found. Run setup_alias.py, or call "
                "with strict=False to fall back to the base coder.")
    effective_system = _build_system("", trace, "")
    project = DEFAULT_PROJECT if augment else None
    conn = _open_db()
    try:
        response, iid, trace_ctx = _answer(
            conn, prompt, model, effective_system, 0.2, 1024, SESSION_NUM_CTX,
            None, project, history or None, trace=trace,
            tier=tier_label, cloud=cloud, augment=augment,
        )
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()
    if trace:
        params = {"temperature": 0.2, "num_predict": 1024, "num_ctx": SESSION_NUM_CTX}
        trace_block = _format_trace(model, "trilobite", params, trace_ctx)
        return with_footer(response + trace_block, iid)
    return with_footer(response, iid)


@mcp.tool()
def record_outcome(interaction_id: str, signal: str) -> str:
    """Feed a real-world outcome back into trilobite's learning loop.

    Call this after a trilobite/offload response once you know how it went.
    signal is one of: tests_passed, accepted, compiled, rejected, failed.
    A good outcome triggers a distilled 'lesson' that future prompts will retrieve.
    Pass the id from the '[interaction_id: <id>]' footer of the response.
    """
    if signal not in reward.VALID_SIGNALS:
        return "ERROR: unknown signal '%s'. Valid: %s." % (
            signal, ", ".join(sorted(reward.VALID_SIGNALS)))
    conn = _open_db()
    try:
        inter = memory_store.get_interaction(conn, interaction_id)
        if inter is None:
            return "ERROR: no interaction '%s' (already expired or wrong id)." % interaction_id
        r = reward.score(signal)
        memory_store.record_outcome_row(conn, interaction_id, signal, r)
        lesson_id = None
        if reward.is_good(signal):
            try:
                lesson_id = reflection.maybe_add_lesson(
                    conn, interaction_id, inter["task"], inter["response"], signal,
                    offload_fn=_generate_text, embed_fn=embeddings.embed,
                )
            except urllib.error.URLError:
                lesson_id = None
    finally:
        conn.close()
    msg = "Recorded '%s' (reward %+.2f) for %s." % (signal, r, interaction_id)
    if lesson_id:
        msg += " Distilled lesson %s." % lesson_id
    return msg


@mcp.tool()
def trilobite_stats() -> str:
    """Report what trilobite has learned so far.

    Read-only observability into the learning loop's SQLite memory: how many
    interactions have been logged, how outcomes break down by signal, and the
    most recently distilled lessons. Makes no model call and needs no Ollama —
    it only reads memory.db, so it works even if the Ollama server is down.
    """
    conn = _open_db()
    try:
        n_interactions = memory_store.count_interactions(conn)
        signal_counts = memory_store.outcome_signal_counts(conn)
        lessons = memory_store.recent_lessons(conn, limit=5)
        n_lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    finally:
        conn.close()
    n_outcomes = sum(signal_counts.values())
    signals_line = (
        ", ".join("%s=%d" % (sig, n) for sig, n in sorted(signal_counts.items()))
        if signal_counts else "(none yet)"
    )
    lines = [
        "trilobite learning stats",
        "  lessons: %d" % n_lessons,
        "  interactions: %d | outcomes: %d" % (n_interactions, n_outcomes),
        "  outcomes by signal: %s" % signals_line,
    ]
    if lessons:
        lines.append("  recent lessons:")
        for l in lessons:
            lines.append("    - %s" % l["text"])
    else:
        lines.append("  recent lessons: (none yet)")
    return "\n".join(lines)


@mcp.tool()
def trilobite_sessions(limit: int = 20) -> str:
    """List trilobite conversation threads, most recently used first.

    Each line shows the session id (pass it as `session` to trilobite to resume),
    its auto-generated title, live turn count, and last-updated time. Read-only.
    """
    conn = _open_db()
    try:
        sessions = memory_store.list_sessions(conn, limit=limit)
    finally:
        conn.close()
    if not sessions:
        return "no conversation sessions yet."
    lines = ["trilobite sessions (most recent first):"]
    for s in sessions:
        lines.append("  %s  [%d turns]  %s  (updated %s)" % (
            s["session_id"], s["turn_count"], s.get("title") or "(untitled)",
            s.get("updated_ts") or "?",
        ))
    return "\n".join(lines)


@mcp.tool()
def trilobite_remember_fact(text: str, project: str = "") -> str:
    """Store a durable fact trilobite should ALWAYS know for a project.

    Unlike lessons (earned from good outcomes), facts are asserted directly and are
    injected into every trilobite call for that project — a mini project brief the
    model carries itself (toolchain, conventions, key paths, gotchas). No `project`
    stores it under the "default" project. Use trilobite(..., project="<name>") to
    scope which facts apply to a call.
    """
    text = (text or "").strip()
    if not text:
        return "ERROR: empty fact."
    project_id = _resolve_project(project) or DEFAULT_PROJECT
    conn = _open_db()
    try:
        emb = embeddings.embed(text)
        blob = embeddings.to_blob(emb) if emb else None
        fact_id = memory_store.new_id()
        memory_store.add_fact(conn, fact_id, project_id, text, blob)
        n = memory_store.count_facts(conn, project_id)
    finally:
        conn.close()
    return "Remembered fact for project '%s' (%d total). id=%s" % (project_id, n, fact_id)


@mcp.tool()
def status() -> str:
    """Report local-LLM state: which models are installed, and which are currently in VRAM.

    Use this to check whether the GPU is busy before offloading, or to confirm models pulled.
    """
    try:
        tags = _get("/api/tags").get("models", [])
        ps = _get("/api/ps").get("models", [])
    except urllib.error.URLError as e:
        return f"ERROR contacting Ollama at {BASE}: {e}"

    installed = sorted(m.get("name", "?") for m in tags)
    loaded = [
        f"{m.get('name')} (VRAM ~{round(m.get('size_vram', 0)/1e9, 1)} GB)" for m in ps
    ]
    tier_lines = [
        f"  {k}={v}" + ("  [CLOUD — leaves machine]" if k in CLOUD_TIERS else "  [local GPU]")
        for k, v in TIERS.items()
    ]
    lines = [
        f"Ollama @ {BASE}",
        "Tiers:",
        *tier_lines,
        f"Installed/registered models: {', '.join(installed) if installed else '(none)'}",
        f"In VRAM now: {', '.join(loaded) if loaded else '(none — GPU idle)'}",
        f"local keep_alive: {KEEP_ALIVE}",
    ]
    return "\n".join(lines)


@mcp.tool()
def unload(tier: str = "all") -> str:
    """Immediately free GPU VRAM by unloading a model (or all of them).

    Args:
        tier: "all" (default), or one of "fast", "code", "general".
    """
    if tier == "all":
        # Only local tiers occupy VRAM; cloud tiers run remote.
        targets = [v for k, v in TIERS.items() if k not in CLOUD_TIERS]
    elif tier in CLOUD_TIERS:
        return f"'{tier}' is a cloud tier — it uses no local VRAM, nothing to unload."
    else:
        targets = [TIERS.get(tier)]
    if None in targets:
        return f"ERROR: unknown tier '{tier}'. Valid: all, {', '.join(TIERS)}."
    freed = []
    for model in targets:
        try:
            _post("/api/generate", {"model": model, "keep_alive": 0})
            freed.append(model)
        except urllib.error.URLError:
            pass
    return f"Unload requested for: {', '.join(freed) if freed else '(none)'}."


if __name__ == "__main__":
    mcp.run()
