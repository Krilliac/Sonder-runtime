"""trilobite — interactive terminal REPL for the local self-improving coding assistant.

Boots straight into the real learning loop (server.trilobite), the way `claude`
drops you into an interactive session. Slash-commands control trace/strict mode,
teach outcomes back, and surface stats/lessons. Stdlib only + server/memory_store.
"""
import os
import sys

import server
import memory_store
import grounding
import code_runner
import training_tasks
import intents
import feedback
import personas
import live_reload
import debug_dump

CURRENT_TOKEN = ""

BANNER = """trilobite - fully local self-improving coder
type /help for commands, or just start typing to ask trilobite something.
"""

HELP = """commands:
  /help              show this help
  /trace [on|off]    toggle trace mode (bare = on); shows retrieval + prompt
  /strict [on|off]   toggle strict mode (bare = on); pins to the trilobite alias
  /persona [name]    show/set active persona (coder/explainer/reviewer/teacher)
  /stats             show trilobite's learning stats
  /context           show context, session, and memory health meters
  /contextsize [N]   show/set requested context (8k..1m; native num_ctx is clamped)
  /compact           preview context compaction/rollover recommendations
  /commands [filter] list available commands by category, name, or risk
  /dump [label]      dump this chat and debug info to a text file
  /todo ...          list/add/update visible task state
  /quality           audit lesson quality and duplicate rows
  /qualityfix [apply] dry-run or apply exact duplicate lesson cleanup
  /improve           show the next system improvement checklist
  /master [mode] ... run master orchestration: ask, inline, or delegate
  /agents            show live master/subagent activity
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
  /train, /learn [N] grounded self-learning: practice N tasks (default 3, max 500)
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


TRAIN_MAX_N = max(1, _env_int("TRILOBITE_TRAIN_MAX_N", 500))

LIVE_RELOAD_MODULES = [
    "server",
    "memory_store",
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
    memory_store = modules.get("memory_store", memory_store)
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
    marker = "\n=== TRACE (how trilobite decided) ==="
    idx = (text or "").find(marker)
    if idx == -1:
        idx = (text or "").find("=== TRACE (how trilobite decided) ===")
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
    for l in lessons:
        print("- %s" % l["text"])


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
        print("  training: %s ..." % t["name"])
        # Training runs are single-turn and must not pollute the user's chat thread.
        resp = server.trilobite(t["prompt"], session="none")
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
            print("    -> %s  %s" % ("PASS" if ok else "FAIL", msg))
        else:
            print("    -> %s (no id)" % ("PASS" if ok else "FAIL"))
    print("trained on %d tasks: %d passed, %d failed, %d new lessons" % (
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
    persona = personas.DEFAULT
    last_iid = None
    last_response = None
    last_run_source = None
    # A fresh conversation thread per REPL launch; /new rerolls it, /resume switches it.
    session_id = memory_store.new_id()
    project = server.DEFAULT_PROJECT

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
            server.trilobite_paths.default_home(),
            label=label or "repl",
            messages=messages,
            sections=sections,
        )
        print("dumped chat/debug log to %s" % path)

    print(BANNER)

    while True:
        try:
            line = input("trilobite> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        line = line.strip()
        if not line:
            continue
        _maybe_live_reload()

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
            elif cmd == "/stats":
                print(server.trilobite_stats())
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
            elif cmd in ("/improve", "/improvements"):
                print(server.system_improvement_report(session=session_id, project=project))
            elif cmd in ("/agents", "/masterstatus"):
                print(server.master_status())
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
                    if parts[0].lower() in (
                        "ask", "inline", "master", "delegate",
                        "delegated", "agents", "parallel",
                    ):
                        mode = parts[0].lower()
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
                    print(server.trilobite_remember_fact(a, project=project))
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
        # "run it", "train yourself") — conservative classifier, only fires on
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

        out = server.trilobite(line, trace=trace, strict=strict, persona=persona,
                               session=session_id, project=project)
        if out.startswith("ERROR"):
            print(out)
            continue

        last_iid = server.parse_interaction_id(out)
        last_response = out
        last_run_source = _answer_only(out)
        cleaned = _strip_footer(out)
        print(cleaned)
        if last_iid:
            print("(/pass or /fail to teach trilobite)")


if __name__ == "__main__":
    main()
