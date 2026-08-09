"""sonder — interactive terminal REPL for Sonder Runtime's local learning loop.

Boots straight into the real learning loop (server.sonder), the way `claude`
drops you into an interactive session. Slash-commands control trace/strict mode,
teach outcomes back, and surface stats/lessons. Stdlib only + server/memory_store.
"""
import os
import re
import sys
import time

import server
import sonder_runtime.adapters.memory_store as memory_store
import grounding
import code_runner
import training_tasks
import intents
import feedback
import personas
import live_reload
import debug_dump
import consult as consult_flow
import tier_router
import code_improve
import command_router
import project_scaffold

CURRENT_TOKEN = ""


class _Ansi:
    """Small dependency-free palette; automatically disappears when piped."""

    enabled = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
    reset = "\x1b[0m"
    teal = "\x1b[38;5;80m"
    cyan = "\x1b[38;5;117m"
    muted = "\x1b[38;5;245m"
    green = "\x1b[38;5;114m"
    amber = "\x1b[38;5;221m"
    red = "\x1b[38;5;210m"
    bold = "\x1b[1m"


def _paint(text, *styles):
    if not _Ansi.enabled:
        return str(text)
    return "".join(styles) + str(text) + _Ansi.reset


def _normalize_input_line(line):
    """Strip console framing, including a BOM from piped PowerShell input."""
    value = str(line or "")
    # Windows PowerShell 5.1 may send a UTF-8 BOM that Python's console codec
    # exposes either correctly as U+FEFF or as the three Latin-1 code points.
    for prefix in ("\ufeff", "\xef\xbb\xbf", "\xff\xfe", "\xfe\xff"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value.strip()


def _rule(char="─", width=56):
    return _paint(char * width, _Ansi.muted)


def _result_tag(ok):
    return _paint("PASS" if ok else "FAIL", _Ansi.green if ok else _Ansi.red, _Ansi.bold)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(text):
    """Printed width, ignoring colour escapes.

    Padding a boxed line by len() counts the escape bytes, so the right edge
    frays the moment any field inside is coloured -- and it shows only in a
    real terminal, never in piped output.
    """
    return len(_ANSI_RE.sub("", str(text)))


def _box_chars():
    """Box glyphs, degrading to ASCII rather than raising.

    A Windows console on a legacy code page cannot encode U+256D, and an
    unhandled UnicodeEncodeError here would take the whole REPL launch down
    with it. A decorative header must never be able to do that.
    """
    glyphs = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯",
              "h": "─", "v": "│", "dot": "◈"}
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(glyphs.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError, TypeError):
        return {"tl": "+", "tr": "+", "bl": "+", "br": "+",
                "h": "-", "v": "|", "dot": "*"}
    return glyphs


def _banner(rows, title="Sonder Runtime", subtitle="private AI runtime + orchestrator"):
    """A bordered header, sized to its content.

    `rows` is a list of (label, value, styles); the caller decides what is
    worth showing rather than this function knowing about the runtime.
    """
    box = _box_chars()
    label_width = max((len(label) for label, _value, _styles in rows), default=0)
    body = []
    for label, value, styles in rows:
        body.append((
            _paint((label + ":").ljust(label_width + 1), _Ansi.muted),
            _paint(value, *styles) if styles else str(value),
        ))
    head = "%s %s  %s" % (
        _paint(box["dot"], _Ansi.teal),
        _paint(title, _Ansi.teal, _Ansi.bold),
        _paint(subtitle, _Ansi.muted),
    )
    widest = max([_visible_len(head)]
                 + [_visible_len(a) + 1 + _visible_len(b) for a, b in body])
    inner = widest + 3

    def row(text):
        return "%s %s%s%s" % (
            _paint(box["v"], _Ansi.muted),
            text,
            " " * (inner - 2 - _visible_len(text)),
            _paint(box["v"], _Ansi.muted),
        )

    lines = [_paint(box["tl"] + box["h"] * (inner - 1) + box["tr"], _Ansi.muted)]
    lines.append(row(head))
    lines.append(row(""))
    for label, value in body:
        lines.append(row("%s %s" % (label, value)))
    lines.append(_paint(box["bl"] + box["h"] * (inner - 1) + box["br"], _Ansi.muted))
    return "\n".join(lines)


def _installed_models():
    """(name, size) for every model Ollama has locally, newest API shape first.

    Returns an empty list when Ollama is unreachable so callers can say so
    rather than printing an empty list that reads as "none installed".
    """
    try:
        payload = server._get("/api/tags")
    except Exception:
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    out = []
    for model in models:
        if not isinstance(model, dict):
            continue
        name = str(model.get("name") or model.get("model") or "").strip()
        if not name:
            continue
        size = model.get("size")
        try:
            pretty = "%.1f GB" % (float(size) / (1024 ** 3)) if size else ""
        except (TypeError, ValueError):
            pretty = ""
        out.append((name, pretty))
    return sorted(out)


def _home_relative(path):
    """Show ~ instead of the home prefix, the way a shell prompt does."""
    try:
        return "~" + os.sep + os.path.relpath(path, os.path.expanduser("~"))
    except (ValueError, OSError):
        return str(path)


def _startup_banner(strict, persona, project, tier=None):
    """The header shown on launch.

    Every value is read from the runtime rather than written here: the model
    comes from the live tier table and the endpoint from the listener, so the
    banner cannot claim a setup the process is not actually in. Each lookup is
    guarded, because a cosmetic header must never be the reason a REPL fails
    to start.
    """
    tier = tier or "code"
    try:
        model = str(server.TIERS.get(tier) or "unknown")
    except Exception:
        model = "unknown"
    try:
        import sonder_headless
        host, port = sonder_headless.DEFAULT_HOST, sonder_headless.DEFAULT_PORT
        live = sonder_headless.port_open(host, port)
        endpoint = "http://%s:%s" % (host, port)
    except Exception:
        endpoint, live = os.environ.get("SONDER_API", "http://127.0.0.1:11435"), False

    rows = [
        ("model", "%s  %s" % (model, _paint("(%s tier)" % tier, _Ansi.muted)),
         (_Ansi.cyan,)),
        ("endpoint", endpoint if live else
         "%s  %s" % (endpoint, _paint("(not listening)", _Ansi.amber)),
         (_Ansi.green,) if live else (_Ansi.muted,)),
        ("directory", _home_relative(os.getcwd()), ()),
        ("persona", str(persona), (_Ansi.cyan,)),
        ("project", str(project or "(none)"), ()),
    ]
    if strict:
        rows.append(("strict", "on  pinned to the sonder alias", (_Ansi.amber,)))
    hint = "%s  %s" % (
        _paint("type /help for commands", _Ansi.muted),
        _paint("or just start typing.", _Ansi.muted),
    )
    return "%s\n\n  %s\n" % (_banner(rows), hint)


def _execution_prompt(status=None):
    """Prompt suffix refreshed once per user turn, with no polling thread."""
    if status is None:
        try:
            status = server.execution_status_data()
        except Exception:
            status = None
    if not isinstance(status, dict) or not status.get("known"):
        return _paint("[lanes ? | agents ?]", _Ansi.amber)
    lanes = int(status.get("running_lanes") or 0)
    running = int(status.get("running_agents") or 0)
    queued = int(status.get("queued_agents") or 0)
    agents = str(running) if queued == 0 else "%s+%sq" % (running, queued)
    colour = _Ansi.green if lanes or running or queued else _Ansi.muted
    return _paint("[lanes %s | agents %s]" % (lanes, agents), colour)


def _watch_activity(poll_seconds=1.0):
    """Blocking feed tail; never interleaves with the normal input prompt."""
    last_seq = -1
    print("watching projected activity; Ctrl+C to return to the prompt")
    try:
        while True:
            feed = server.execution_feed_data()
            if not feed.get("known"):
                print("live execution feed: unknown (%s)" % feed.get("error", ""))
            else:
                events = [
                    row for row in (feed.get("events") or [])
                    if int(row.get("seq") or 0) > last_seq
                ]
                if events:
                    print(server.activity_tracker.format_execution_feed({
                        **feed, "events": events, "truncated": False,
                    }))
                    last_seq = max(int(row.get("seq") or 0) for row in events)
            time.sleep(max(0.25, min(5.0, float(poll_seconds))))
    except KeyboardInterrupt:
        print("\nactivity watch stopped")

HELP = """commands (slash forms are optional -- plain language works too, e.g.
"show me your stats", "which model should handle X", "read file foo.py"):
  /help              show this help
  /trace [on|off]    toggle trace mode (bare = on); shows retrieval + prompt
  /strict [on|off]   toggle strict mode (bare = on); pins to the sonder alias
  /persona [name]    show/set active persona (coder/explainer/reviewer/teacher)
  /model [name|tier] list installed models and tiers; switch either one
  /consult <question> ask 2 local tiers (+cloud when enabled) and compare answers
  /route <request>   suggest the tier best suited to a request, and why
  /refactor <file> <fn> [goal]  propose a guarded improvement to one function
  /scaffold <kind> <name> [root]  write a full project skeleton (cpp-msvc, csharp, rust, ...)
  /env [refresh]     show the host OS, shells, and installed toolchains
  /location [on|off] allow approximate IP location for "my area" weather answers
  /stats             show Sonder Runtime's learning stats
  /context           show context, session, and memory health meters
  /contextsize [N]   show/set requested context (8k..1m; native num_ctx is clamped)
  /compact           preview context compaction/rollover recommendations
  /commands [filter] list available commands by category, name, or risk
  /activity [watch]  show once, or poll projected new events until Ctrl+C
  /work <task>       execute a guarded tool-using workflow with checklist/report
  /autopilot ...     persistent plan/run/status/resume/pause/cancel autonomy
  /runtime ...       shared local model mappings and execution-lane tiers
  /hardware          detect RAM, GPU runtime, VRAM, and offload support
  /training ...      plan/start/status/deploy/rollback attended weight training
  /selfmod ...       inspect/plan/test/approve/deploy/rollback isolated improvements
  /mcp ...           audit/refresh atomic MCP source and tool convergence
  /learning          show grounded outcomes, lesson sources, and memory hygiene
  /report            show the latest grounded end report and action transcript
  /checklist [id]    show the current or selected persistent checklist
  /inventory [path]  summarize a guarded workspace with explicit scan budgets
  /tree [path]       list a guarded folder tree
  /search q|root|g   search text under a guarded root (optional glob)
  /programs [query]  find installed programs available to the workbench
  /scripts q|root    find runnable scripts under a guarded root
  /image <path>      inspect image metadata and dimensions
  /mkdir <path>      create a guarded directory
  /runprogram p|a|c  run a program with JSON args and optional cwd
  /runscript p|a|c   run a known script type with JSON args and optional cwd
  /dump [label]      dump this chat and debug info to a text file
  /todo ...          list/add/update visible task state
  /quality           audit lesson quality and duplicate rows
  /qualityfix [apply] dry-run or apply exact duplicate lesson cleanup
  /privacy [N]       review redacted path/credential-like lesson findings
  /privacyfix ...    dry-run or delete explicit flagged lesson IDs
  /embeddings ...    dry-run or refresh stale/missing local lesson vectors
  /emotion [cmd]     show/tune live tone vectors; try: /emotion tune warmer shorter
  /prefer [text]     show/teach preferences; /prefer forget <id-or-key>
  /improve           show the next system improvement checklist
  /master [mode] ... run orchestration: ask, inline, delegate, or fleet
  /agents            show live master/subagent activity
  /capacity [N]      show queued-agent ceiling and safe concurrent worker slots
  /agentcancel <id>  cooperatively cancel an agent/master prefix or all
  /agentretry <id>   explicitly retry persisted interrupted/failed master work
  /weather <place>   get sourced live conditions and a short forecast
  /asset <n> <brief> generate a general icon/audio/model/scene artifact pack
  /artifactcheck ... ground a file/pack: /artifactcheck <path> [| recipe]
  /forge [name]      build and run the dependency-free reference game suite
  /game ...          generate/test a game: /game cpp 3d name | concept
  /gamefleet ...     parallel game campaign: name | concept [| language | dimension]
  /register u p      create account (first account becomes admin)
  /login u p         login for admin/debug commands
  /whoami            show current account
  /admin             show admin status
  /accounts          list accounts (admin)
  /setaccount ...    admin account edits: user role= tier= dev_flags= banned=
  /debug             inspect safe debug state
  /cot               denied: hidden private chain-of-thought is not exposed
  /permissions [tool] show local permission rules or one matched rule
  /filepolicy        show file access roots and bypass controls
  /files [query]     find files under guarded roots
  /read <path>       read a guarded file
  /write <p> <text>  create a guarded file
  /append <p> <text> append to a guarded file
  /edit <p>|<old>|<new> replace text in a guarded file
  /delete <path>     dry-run delete; output shows required confirm string
  /lessons           show the 10 most recent distilled lessons
  /pass, /good       record the last answer as tests_passed
  /accept,/used      record the last answer as accepted/used
  /copied,/edited    record copy/edit passive learning signals
  /fail, /bad        record the last answer as failed
  /run [seconds]     execute the code block from the last response (default 8s)
  /runwindow [sec]   launch the last code block in a separate Windows console
  /runproject [sec]  execute file/path fenced blocks as a temp project
  /train, /learn [N] grounded practice: check N tasks and record lessons (default 3, max 500)
  /new               start a fresh conversation thread (forget this chat's history)
  /sessions          list past conversation threads
  /resume <id|title> continue a past thread by id or title prefix
  /project [name]    show/set the active project (scopes facts)
  /fact <text>       remember a durable fact for the active project
  /facts             list facts for the active project
  /exit, /quit, /q   leave
"""

TRAIN_DEFAULT_N = 3


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


TRAIN_MAX_N = max(1, _env_int("SONDER_TRAIN_MAX_N", 500))

LIVE_RELOAD_MODULES = [
    "server",
    "sonder_runtime.adapters.memory_store",
    "grounding",
    "training_tasks",
    "intents",
    "feedback",
    "personas",
    "emotion_vectors",
    "web_tools",
    "command_registry",
    "permission_rules",
    "debug_dump",
]


def _maybe_live_reload():
    global server, memory_store, grounding, training_tasks, intents, feedback, personas, debug_dump
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    server = modules.get("server", server)
    memory_store = modules.get(
        "sonder_runtime.adapters.memory_store", memory_store
    )
    grounding = modules.get("grounding", grounding)
    training_tasks = modules.get("training_tasks", training_tasks)
    intents = modules.get("intents", intents)
    feedback = modules.get("feedback", feedback)
    personas = modules.get("personas", personas)
    debug_dump = modules.get("debug_dump", debug_dump)


def _strip_footer(text):
    idx = text.find(server.FOOTER_PREFIX)
    if idx == -1:
        return text
    return text[:idx]


def _strip_trace(text):
    marker = "\n=== TRACE (how Sonder Runtime decided) ==="
    idx = (text or "").find(marker)
    if idx == -1:
        idx = (text or "").find("=== TRACE (how Sonder Runtime decided) ===")
    if idx == -1:
        return text or ""
    return (text or "")[:idx].rstrip()


def _answer_only(text):
    return _strip_trace(_strip_footer(text or "")).rstrip()


def _print_lessons():
    conn = server._open_db()
    try:
        lessons = memory_store.recent_lessons(conn, 10)
    finally:
        conn.close()
    if not lessons:
        print("(no lessons yet)")
        return
    for lesson in lessons:
        print("- %s" % lesson["text"])


def _on_off(arg, current):
    arg = (arg or "").strip().lower()
    if arg in ("", "on"):
        return True
    if arg == "off":
        return False
    print("usage: on|off (bare = on)")
    return current


def _parse_train_n(arg):
    arg = (arg or "").strip()
    if not arg:
        return TRAIN_DEFAULT_N
    try:
        n = int(arg)
    except ValueError:
        print("usage: /train [N]  (N must be an integer, default %d)" % TRAIN_DEFAULT_N)
        return None
    if n < 1:
        n = 1
    if n > TRAIN_MAX_N:
        n = TRAIN_MAX_N
    return n


def _parse_run_timeout(arg):
    arg = (arg or "").strip()
    if not arg:
        return grounding.DEFAULT_TIMEOUT
    try:
        value = int(arg)
    except ValueError:
        print("usage: /run [seconds]  (runs the previous fenced code block, not a filename or shell command)")
        return None
    return grounding.clamp_timeout(value)


def _run_train(n):
    tasks = training_tasks.sample(n)
    passed = 0
    lessons = 0
    for t in tasks:
        print("  %s %s" % (_paint("PRACTICE", _Ansi.teal, _Ansi.bold), t["name"]))
        # Practice runs are single-turn and must not pollute the user's chat thread.
        resp = server.sonder(t["prompt"], session="none")
        iid = server.parse_interaction_id(resp)
        code = grounding.extract_code_block(resp)
        ok = False
        if code:
            ok, _ = grounding.run_code(code, t["check"])
        signal = "tests_passed" if ok else "failed"
        passed += 1 if ok else 0
        if iid:
            msg = server.record_outcome(iid, signal)
            if "Distilled lesson" in msg:
                lessons += 1
            print("    %s  %s" % (_result_tag(ok), msg))
        else:
            print("    %s  (no interaction id)" % _result_tag(ok))
    print(_paint("practice complete", _Ansi.cyan, _Ansi.bold) +
          "  %d tasks · %d passed · %d failed · %d new lessons" % (
        len(tasks), passed, len(tasks) - passed, lessons))


def _print_sessions():
    conn = server._open_db()
    try:
        sessions = memory_store.list_sessions(conn, 20)
    finally:
        conn.close()
    if not sessions:
        print("(no past sessions)")
        return
    for s in sessions:
        print("  %s  [%d turns]  %s" % (
            s["session_id"], s["turn_count"], s.get("title") or "(untitled)"))


def _print_facts(project):
    conn = server._open_db()
    try:
        facts = memory_store.facts_for_project(conn, project)
    finally:
        conn.close()
    if not facts:
        print("(no facts for project '%s')" % project)
        return
    for f in facts:
        print("  - %s" % f["text"])


def main():
    global CURRENT_TOKEN
    trace = False
    strict = None  # None = env default
    location_consent = None  # None = env default (SONDER_LOCATION_CONSENT)
    persona = personas.DEFAULT
    last_iid = None
    last_response = None
    last_run_source = None
    # A fresh conversation thread per REPL launch; /new rerolls it, /resume switches it.
    session_id = memory_store.new_id()
    project = server.DEFAULT_PROJECT
    # None = whatever the runtime resolves by default; /model pins one.
    active_tier = None

    def apply_trace(val):
        nonlocal trace
        trace = val
        print("trace: %s" % ("on" if trace else "off"))

    def apply_strict(val):
        nonlocal strict
        strict = val
        print("strict: %s" % ("on" if strict else "off"))

    def do_persona(arg):
        nonlocal persona
        arg = (arg or "").strip()
        if not arg:
            print("persona: %s (available: %s)" % (persona, ", ".join(personas.names())))
            return
        persona = arg.lower()
        print("persona: %s" % persona)

    def do_model(arg):
        """Show what is installed and switch the model for the rest of the session.

        Switching rebinds the ACTIVE TIER's entry in server.TIERS rather than
        threading a model name through every call: the tier table is the single
        place the runtime resolves a model from, so a rebind is picked up by
        anything that asks -- including the identity block that tells the model
        what it is running as. Changing only this REPL's calls would leave that
        block naming the old model.
        """
        nonlocal active_tier
        arg = (arg or "").strip()
        tier = active_tier or "code"
        installed = _installed_models()

        if not arg:
            current = str(server.TIERS.get(tier) or "?")
            print("active tier: %s  ->  %s" % (
                _paint(tier, _Ansi.cyan), _paint(current, _Ansi.cyan, _Ansi.bold)))
            print()
            print(_paint("tiers", _Ansi.muted))
            for name in sorted(server.TIERS):
                mark = "*" if name == tier else " "
                print("  %s %-14s %s" % (mark, name, server.TIERS[name]))
            print()
            if installed:
                print(_paint("installed models (ollama)", _Ansi.muted))
                for name, size in installed:
                    mark = "*" if name == server.TIERS.get(tier) else " "
                    print("  %s %-40s %s" % (mark, name, size))
            else:
                print(_paint("installed models: (ollama did not answer)", _Ansi.amber))
            print()
            print(_paint("usage: /model <model-name>  |  /model <tier>", _Ansi.muted))
            return

        if arg in server.TIERS:
            active_tier = arg
            print("active tier: %s  ->  %s" % (arg, server.TIERS.get(arg)))
            return

        names = [name for name, _size in installed]
        if installed and arg not in names:
            # Refuse rather than rebind to something that will fail on the next
            # turn with an opaque ollama error. Suggest, because a near miss is
            # usually a tag typo (":7b" vs ":latest").
            near = [name for name in names if arg.split(":")[0] in name]
            print(_paint("no installed model named %r" % arg, _Ansi.red))
            if near:
                print("did you mean: %s" % ", ".join(near[:5]))
            else:
                print("run /model with no argument to list what is installed")
            return

        server.TIERS[tier] = arg
        print("%s tier -> %s" % (tier, _paint(arg, _Ansi.cyan, _Ansi.bold)))

    def do_run(timeout=grounding.DEFAULT_TIMEOUT):
        block = grounding.extract_runnable_code_block(last_run_source or last_response)
        if block is None:
            print("(no code block in the last response to run)")
            return
        result = code_runner.run_code(
            block["code"],
            language=block["language"],
            timeout=timeout,
        )
        print(code_runner.format_result(result))
        if result.get("ok"):
            print("[ran OK]")
        elif result.get("returncode") is None and result.get("error", "").startswith("timed out"):
            print("[timed out]")
        else:
            print("[exited with error]")

    def do_run_window(timeout=grounding.DEFAULT_TIMEOUT):
        block = grounding.extract_runnable_code_block(last_run_source or last_response)
        if block is None:
            print("(no code block in the last response to run)")
            return
        result = code_runner.run_code_window(
            block["code"],
            language=block["language"],
            timeout=timeout,
        )
        print(code_runner.format_window_result(result))
        print("[launched]" if result.get("ok") else "[launch failed]")

    def do_runproject(timeout=grounding.MAX_TIMEOUT):
        files = grounding.extract_project_files(last_run_source or last_response)
        if not files:
            print("(no file/path fenced project blocks in the last response)")
            return
        result = code_runner.run_project({"files": files}, timeout=timeout)
        print(code_runner.format_project_result(result))
        print("[ran OK]" if result.get("ok") else "[project failed]")

    def do_dump(label="repl"):
        conn = server._open_db()
        try:
            turns = memory_store.session_turns(conn, session_id)
        finally:
            conn.close()
        messages = []
        for turn in turns:
            messages.append({"role": "user", "content": turn.get("task") or ""})
            messages.append({"role": "assistant", "content": turn.get("response") or ""})
        sections = [
            ("session", session_id),
            ("project", project),
            ("trace", "on" if trace else "off"),
            ("strict", str(strict)),
            ("persona", persona),
            ("last interaction id", last_iid or "(none)"),
            ("last answer source", last_run_source or "(none)"),
            ("context", server.context_health(session=session_id, project=project)),
            ("quality", server.memory_quality_report(sample_limit=5)),
            ("agents", server.master_status(limit=20)),
            ("diagnostics", server.diagnostics()),
        ]
        path = debug_dump.write_dump(
            server.sonder_paths.default_home(),
            label=label or "repl",
            messages=messages,
            sections=sections,
        )
        print("dumped chat/debug log to %s" % path)

    # The next three helpers back both the slash commands (/consult, /route,
    # /refactor) AND their natural-language forms, so a request reaches the same
    # capability whether the user types the slash or just asks for it.
    def do_consult(question):
        question = (question or "").strip()
        if not question:
            print("usage: /consult <question>")
            return
        # Two local models plus a cloud model when cloud is enabled; the active
        # /model tier (if any) leads and judges. de-dup is handled inside
        # consult, so prepending a tier already in the default is safe.
        tiers = consult_flow.default_tiers()
        if active_tier and active_tier not in tiers:
            tiers = [active_tier] + tiers
        result = consult_flow.consult(question, tiers)
        for answer in result["answers"]:
            print("\n=== %s ===\n%s" % (answer["tier"], answer["text"]))
        verdict = consult_flow.verdict_line(result)
        colour = _Ansi.green if result["agree"] is True else _Ansi.amber
        print("\n" + _paint(verdict, colour, _Ansi.bold))

    def do_route(question):
        question = (question or "").strip()
        if not question:
            print("usage: /route <request>")
            return
        decision = tier_router.route(question, available_tiers=set(server.TIERS))
        print("kind:   %s" % _paint(decision["kind"], _Ansi.cyan))
        print("tier:   %s" % _paint(decision["tier"], _Ansi.cyan, _Ansi.bold))
        print("reason: %s" % _paint(decision["reason"], _Ansi.muted))

    def do_refactor(arg):
        parts = (arg or "").split(None, 2)
        if len(parts) < 2:
            print("usage: /refactor <file> <function> [objective]")
            return
        fpath, fname = parts[0], parts[1]
        objective = parts[2] if len(parts) > 2 else ""
        try:
            src = server.file_ops.read_file(fpath)
            src = src.get("text", "") if isinstance(src, dict) else str(src)
        except Exception as exc:
            print("could not read %s: %s" % (fpath, exc))
            return
        chosen = active_tier or tier_router.route(
            objective or "improve %s" % fname,
            available_tiers=set(server.TIERS))["tier"]
        print(_paint("asking %s to improve %s ..." % (chosen, fname), _Ansi.muted))
        res = code_improve.improve_function(
            src, fname,
            lambda p, t: server.ensemble_answer(p, tiers=t, mode="code"),
            tier=chosen, objective=objective)
        if not res["ok"]:
            print(_paint("no change: %s" % res["reason"], _Ansi.amber))
            return
        print(res["diff"] or "(no diff)")
        print(_paint("apply this change? [y/N] ", _Ansi.amber), end="")
        try:
            ans = _normalize_input_line(input()).lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans in ("y", "yes"):
            server.file_ops.write_file(fpath, res["edited"], mode="overwrite")
            print(_paint("applied to %s" % fpath, _Ansi.green))
        else:
            print(_paint("discarded", _Ansi.muted))

    print(_startup_banner(strict, persona, project, active_tier))

    while True:
        try:
            line = input(_paint("sonder", _Ansi.teal, _Ansi.bold) + " " +
                         _execution_prompt() + _paint(" > ", _Ansi.muted))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        # PowerShell 5.1 may prefix the first piped UTF-8 line with a BOM. Treat
        # it as transport framing so slash commands remain commands.
        line = _normalize_input_line(line)
        if not line:
            continue
        _maybe_live_reload()

        # Natural-language command resolution: "show me your stats" -> /stats,
        # "which model should handle X" -> /route X, "read file foo.py" ->
        # /read foo.py. The resolved slash line flows into the ordinary
        # dispatch below, so every command has exactly one implementation and
        # the slash form stays the precise way to invoke it. Unmatched turns
        # fall through untouched to feedback/intent/work/chat handling.
        if not line.startswith("/"):
            resolved = command_router.resolve(line)
            if resolved:
                print(_paint("(interpreted as: %s)" % resolved, _Ansi.muted))
                line = resolved

        if line.startswith("/"):
            parts = line.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "/help":
                print(HELP)
            elif cmd == "/trace":
                apply_trace(_on_off(arg, trace))
            elif cmd == "/strict":
                apply_strict(_on_off(arg, strict))
            elif cmd == "/persona":
                do_persona(arg)
            elif cmd == "/model":
                do_model(arg)
            elif cmd == "/consult":
                do_consult(arg)
            elif cmd == "/route":
                do_route(arg)
            elif cmd == "/refactor":
                do_refactor(arg)
            elif cmd in ("/env", "/environment"):
                print(server.environment_status(
                    refresh=(arg or "").strip().lower() == "refresh"))
            elif cmd == "/scaffold":
                parts = arg.split()
                if len(parts) < 2:
                    print("usage: /scaffold <kind> <name> [root]   kinds: %s"
                          % ", ".join(project_scaffold.kinds()))
                else:
                    kind, name = parts[0], parts[1]
                    root = parts[2] if len(parts) > 2 else name
                    print(server.scaffold_project(
                        kind=kind, name=name, root=root, apply=True))
            elif cmd == "/location":
                a = (arg or "").strip().lower()
                if a in ("on", "off"):
                    location_consent = a == "on"
                elif a:
                    print("usage: /location [on|off]")
                    continue
                effective = (
                    server._env_location_consent()
                    if location_consent is None else location_consent
                )
                print("approximate IP location: %s%s" % (
                    "on" if effective else "off",
                    " (env default)" if location_consent is None else "",
                ))
            elif cmd == "/stats":
                print(server.sonder_stats())
            elif cmd == "/context":
                print(server.context_health(session=session_id, project=project))
            elif cmd in ("/contextsize", "/ctxsize"):
                if arg.strip():
                    print(server.set_context_size(arg.strip()))
                else:
                    print(server.context_policy_status())
            elif cmd in ("/compact", "/compaction"):
                print(server.context_compaction_plan(session=session_id, project=project))
            elif cmd in ("/commands", "/cmds"):
                print(server.command_registry_list(arg.strip()))
            elif cmd == "/dump":
                do_dump(arg.strip() or "repl")
            elif cmd in ("/permissions", "/perms"):
                print(server.permission_policy(arg.strip()))
            elif cmd in ("/todo", "/task", "/tasks"):
                text = arg.strip()
                if not text or text.lower() in ("list", "ls"):
                    print(server.task_list(project=project))
                else:
                    action, _, rest = text.partition(" ")
                    action = action.lower()
                    if action in ("add", "create", "new"):
                        print(server.task_create(title=rest.strip(), project=project))
                    elif action in ("done", "complete", "finish"):
                        if rest.strip():
                            print(server.task_update(task_id=rest.strip(), status="done"))
                        else:
                            print("usage: /todo done <task-id>")
                    elif action in ("start", "doing"):
                        if rest.strip():
                            print(server.task_update(task_id=rest.strip(), status="in_progress"))
                        else:
                            print("usage: /todo start <task-id>")
                    elif action in ("block", "blocked"):
                        if rest.strip():
                            print(server.task_update(task_id=rest.strip(), status="blocked"))
                        else:
                            print("usage: /todo block <task-id>")
                    elif action in ("show", "view"):
                        if rest.strip():
                            print(server.task_show(rest.strip()))
                        else:
                            print("usage: /todo show <task-id>")
                    else:
                        print(
                            "usage: /todo [list] | /todo add <title> | /todo start <id> | "
                            "/todo done <id> | /todo block <id> | /todo show <id>"
                        )
            elif cmd == "/quality":
                print(server.memory_quality_report())
            elif cmd == "/qualityfix":
                print(server.memory_quality_repair(apply=(arg.strip().lower() == "apply")))
            elif cmd in ("/privacy", "/privacyreview", "/privacyfix", "/embeddings", "/embedfix"):
                print(server.control_command(line, session=session_id, project=project))
            elif cmd in ("/emotion", "/emotions", "/vectors", "/mood"):
                print(server.emotion_command(arg))
            elif cmd in ("/prefer", "/preference", "/preferences"):
                print(server.preference_command(arg))
            elif cmd in ("/improve", "/improvements"):
                print(server.system_improvement_report(session=session_id, project=project))
            elif cmd in ("/agents", "/masterstatus"):
                print(server.master_status())
            elif cmd in ("/capacity", "/agentcapacity"):
                print(server.control_command(line, session=session_id, project=project))
            elif cmd in ("/agentcancel", "/cancelagents"):
                print(server.control_command(line, session=session_id, project=project))
            elif cmd in ("/agentretry", "/retryagent"):
                print(server.control_command(line, session=session_id, project=project))
            elif cmd in ("/activity", "/tools"):
                if arg.strip().lower() in ("watch", "tail"):
                    _watch_activity()
                else:
                    print(server.activity_status())
            elif cmd in ("/autopilot", "/auto"):
                print(server.control_command(
                    line, session=session_id, project=project,
                ))
            elif cmd in (
                "/runtime", "/models", "/mcp", "/convergence",
                "/hardware", "/training", "/weighttraining",
                "/selfmod", "/selfmodify",
                "/learning", "/learnhealth", "/metrics",
                "/goal", "/goals", "/ensemble",
            ):
                print(server.control_command(
                    line, session=session_id, project=project,
                ))
            elif cmd in ("/weather", "/forecast"):
                print(server.control_command(
                    line, session=session_id, project=project,
                ))
            elif cmd in ("/work", "/agent"):
                if not arg.strip():
                    print("usage: /work <task>")
                else:
                    out = server.workbench_agent(
                        prompt=arg.strip(), project=project, max_steps=12,
                    )
                    last_response = out
                    last_run_source = _answer_only(out)
                    last_iid = None
                    print(out)
            elif cmd in (
                "/report", "/endreport", "/checklist", "/plan",
                "/inventory", "/workspace",
                "/tree", "/folders", "/search", "/grep",
                "/programs", "/programfind", "/scripts", "/scriptfind",
                "/image", "/inspectimage", "/mkdir", "/runprogram", "/runscript",
                "/artifactcheck", "/verifyartifact", "/groundartifact",
            ):
                print(server.control_command(
                    line, session=session_id, project=project,
                ))
            elif cmd in ("/asset", "/assets", "/assetgen", "/artifact"):
                parts = arg.strip().split(None, 1)
                if len(parts) != 2:
                    print("usage: /asset <name> <free-form brief>")
                else:
                    print(server.artifact_generate(name=parts[0], brief=parts[1]))
            elif cmd in ("/forge", "/gamesuite"):
                print(server.game_reference_suite(name=arg.strip() or "sonder-reference"))
            elif cmd in ("/game", "/gamegen"):
                parts = arg.strip().split(None, 2)
                if len(parts) != 3 or "|" not in parts[2]:
                    print("usage: /game <language> <2d|2.5d|3d> <name> | <concept>")
                else:
                    name, _, concept = parts[2].partition("|")
                    print(server.game_generate_and_test(
                        name=name.strip(), concept=concept.strip(),
                        language=parts[0], dimension=parts[1],
                    ))
            elif cmd in ("/gamefleet", "/gamecampaign"):
                campaign_args = server._parse_game_campaign_command(arg)
                if campaign_args is None:
                    print("usage: /gamefleet <name> | <concept> [| language | dimension]")
                else:
                    print(server.game_generation_campaign(**campaign_args))
            elif cmd == "/register":
                parts = arg.split(None, 1)
                if len(parts) != 2:
                    print("usage: /register <username> <password>")
                else:
                    print(server.admin_register(parts[0], parts[1]))
            elif cmd == "/login":
                parts = arg.split(None, 1)
                if len(parts) != 2:
                    print("usage: /login <username> <password>")
                else:
                    out = server.admin_login(parts[0], parts[1])
                    marker = "token: "
                    if marker in out and not out.startswith("ERROR:"):
                        CURRENT_TOKEN = out.split(marker, 1)[1].strip().splitlines()[0]
                    print(out)
            elif cmd == "/whoami":
                print(server.admin_whoami(CURRENT_TOKEN))
            elif cmd == "/admin":
                print(server.admin_status(CURRENT_TOKEN))
            elif cmd == "/accounts":
                print(server.admin_accounts(CURRENT_TOKEN))
            elif cmd == "/setaccount":
                parts = arg.split()
                if not parts:
                    print("usage: /setaccount <username> role=developer tier=pro dev_flags=x banned=false")
                else:
                    kv = {}
                    for item in parts[1:]:
                        if "=" in item:
                            k, v = item.split("=", 1)
                            kv[k] = v
                    print(server.admin_set_account(
                        token=CURRENT_TOKEN,
                        username=parts[0],
                        role=kv.get("role", ""),
                        tier=kv.get("tier", ""),
                        dev_flags=kv.get("dev_flags", ""),
                        banned=kv.get("banned", ""),
                    ))
            elif cmd in ("/debug", "/inspect"):
                print(server.debug_inspect(CURRENT_TOKEN))
            elif cmd in ("/cot", "/chainofthought", "/thoughts"):
                print(server.admin_private_chain_of_thought(CURRENT_TOKEN))
            elif cmd == "/filepolicy":
                print(server.file_policy(token=CURRENT_TOKEN))
            elif cmd in ("/files", "/find"):
                print(server.file_find(query=arg.strip() or "*", token=CURRENT_TOKEN))
            elif cmd == "/read":
                print(server.file_read(path=arg.strip(), token=CURRENT_TOKEN))
            elif cmd in ("/write", "/append"):
                parts = arg.split(None, 1)
                if len(parts) != 2:
                    print("usage: %s <path> <text>" % cmd)
                else:
                    print(server.file_write(
                        path=parts[0],
                        content=parts[1],
                        mode="append" if cmd == "/append" else "create",
                        token=CURRENT_TOKEN,
                    ))
            elif cmd == "/edit":
                pieces = arg.split("|", 2)
                if len(pieces) != 3:
                    print("usage: /edit <path>|<old>|<new>")
                else:
                    print(server.file_edit(
                        path=pieces[0].strip(),
                        old=pieces[1],
                        new=pieces[2],
                        token=CURRENT_TOKEN,
                    ))
            elif cmd == "/delete":
                print(server.file_delete(path=arg.strip(), dry_run=True, token=CURRENT_TOKEN))
            elif cmd == "/master":
                text = arg.strip()
                mode = "ask"
                task = text
                if text:
                    parts = text.split(None, 1)
                    mode_alias = {
                        "delagte": "delegate",
                        "delegte": "delegate",
                        "paralell": "parallel",
                        "inlne": "inline",
                        "workflow": "fleet",
                    }
                    requested_mode = mode_alias.get(parts[0].lower(), parts[0].lower())
                    if requested_mode in (
                        "ask", "inline", "master", "delegate",
                        "delegated", "agents", "parallel", "fleet", "swarm",
                        "fanout",
                    ):
                        mode = requested_mode
                        task = parts[1] if len(parts) > 1 else ""
                print(server.master_orchestrate(task=task, mode=mode))
            elif cmd == "/lessons":
                _print_lessons()
            elif cmd in ("/pass", "/good"):
                if last_iid:
                    print(server.record_outcome(last_iid, "tests_passed"))
                    last_iid = None
                else:
                    print("(nothing to record yet)")
            elif cmd in ("/accept", "/accepted", "/used", "/copied", "/edited"):
                if last_iid:
                    signal = {
                        "/accept": "accepted",
                        "/accepted": "accepted",
                        "/used": "used",
                        "/copied": "copied",
                        "/edited": "edited",
                    }[cmd]
                    print(server.record_outcome(last_iid, signal))
                    last_iid = None
                else:
                    print("(nothing to record yet)")
            elif cmd in ("/fail", "/bad"):
                if last_iid:
                    print(server.record_outcome(last_iid, "failed"))
                    last_iid = None
                else:
                    print("(nothing to record yet)")
            elif cmd == "/run":
                timeout = _parse_run_timeout(arg)
                if timeout is not None:
                    do_run(timeout)
            elif cmd in ("/runwindow", "/runnew", "/runconsole"):
                timeout = _parse_run_timeout(arg)
                if timeout is not None:
                    do_run_window(timeout)
            elif cmd == "/runproject":
                timeout = _parse_run_timeout(arg)
                if timeout is not None:
                    do_runproject(timeout)
            elif cmd in ("/train", "/learn"):
                n = _parse_train_n(arg)
                if n is not None:
                    _run_train(n)
            elif cmd == "/new":
                session_id = memory_store.new_id()
                last_iid = None
                last_response = None
                last_run_source = None
                print("started a new thread (%s)" % session_id)
            elif cmd == "/sessions":
                _print_sessions()
            elif cmd == "/resume":
                target = (arg or "").strip()
                if not target:
                    print("usage: /resume <session-id|title-prefix>")
                else:
                    conn = server._open_db()
                    try:
                        found = memory_store.find_session(conn, target)
                    finally:
                        conn.close()
                    if found:
                        session_id = found
                        last_iid = None
                        last_response = None
                        last_run_source = None
                        print("resumed thread %s" % session_id)
                    else:
                        print("no session matching '%s'" % target)
            elif cmd == "/project":
                a = (arg or "").strip()
                if not a:
                    print("project: %s" % project)
                else:
                    project = a
                    print("project: %s" % project)
            elif cmd == "/fact":
                a = (arg or "").strip()
                if not a:
                    print("usage: /fact <text>")
                else:
                    print(server.sonder_remember_fact(a, project=project))
            elif cmd == "/facts":
                _print_facts(project)
            elif cmd in ("/exit", "/quit", "/q"):
                break
            else:
                print("unknown command %s — try /help" % cmd)
            continue

        # Passive learning: if the previous turn is still pending an outcome,
        # check whether this line is plain feedback on it ("thanks, that
        # worked" / "no that's wrong") rather than a new task. Conservative
        # classifier — only fires on short, non-question/imperative turns.
        if last_iid:
            signal = feedback.classify_signal(line)
            if signal:
                server.record_outcome(last_iid, signal)
                last_iid = None
                print("(learned: %s recorded)" % signal)
                continue
            fb = feedback.classify_feedback(line)
            if fb == "positive":
                server.record_outcome(last_iid, "accepted")
                last_iid = None
                print("(learned: \U0001F44D recorded)")
                continue
            if fb == "negative":
                server.record_outcome(last_iid, "rejected")
                last_iid = None
                print("(learned: \U0001F44E recorded)")
                continue

        # Natural-language control intents ("strict on, show your reasoning",
        # "run it", "practice tasks") — conservative classifier, only fires on
        # short control-like turns. Applies the same toggles/actions as the
        # slash commands above and skips the model call for this turn.
        intent = intents.classify(line)
        if intent:
            if "trace" in intent:
                apply_trace(intent["trace"])
            if "strict" in intent:
                apply_strict(intent["strict"])
            if intent.get("run"):
                do_run()
            if "train" in intent:
                _run_train(intent["train"])
            continue

        # Concrete workspace requests run through the guarded agent so the
        # answer is backed by real inspection, file changes, validation, and a
        # persistent checklist instead of being a prose-only suggestion.
        if intents.classify_work(line):
            out = server.workbench_agent(
                prompt=line, tier="auto", max_steps=12, project=project,
            )
            last_iid = None
            last_response = out
            last_run_source = _answer_only(out)
            print(out)
            continue

        out = server.sonder(line, trace=trace, strict=strict, persona=persona,
                            session=session_id, project=project,
                            tier=active_tier or "",
                            location_consent=location_consent)
        if out.startswith("ERROR"):
            print(out)
            continue

        last_iid = server.parse_interaction_id(out)
        last_response = out
        last_run_source = _answer_only(out)
        cleaned = _strip_footer(out)
        print(cleaned)
        if last_iid:
            print("(/pass or /fail to teach Sonder Runtime)")


if __name__ == "__main__":
    main()
