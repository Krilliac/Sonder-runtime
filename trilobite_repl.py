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
  /quality           audit lesson quality and duplicate rows
  /qualityfix [apply] dry-run or apply exact duplicate lesson cleanup
  /lessons           show the 10 most recent distilled lessons
  /pass, /good       record the last answer as tests_passed
  /fail, /bad        record the last answer as failed
  /run [seconds]     execute the code block from the last response (default 8s)
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
]


def _maybe_live_reload():
    global server, memory_store, grounding, training_tasks, intents, feedback, personas
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    server = modules.get("server", server)
    memory_store = modules.get("memory_store", memory_store)
    grounding = modules.get("grounding", grounding)
    training_tasks = modules.get("training_tasks", training_tasks)
    intents = modules.get("intents", intents)
    feedback = modules.get("feedback", feedback)
    personas = modules.get("personas", personas)


def _strip_footer(text):
    idx = text.find(server.FOOTER_PREFIX)
    if idx == -1:
        return text
    return text[:idx]


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
    trace = False
    strict = None  # None = env default
    persona = personas.DEFAULT
    last_iid = None
    last_response = None
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
        block = grounding.extract_runnable_code_block(last_response)
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

    def do_runproject(timeout=grounding.MAX_TIMEOUT):
        files = grounding.extract_project_files(last_response)
        if not files:
            print("(no file/path fenced project blocks in the last response)")
            return
        result = code_runner.run_project({"files": files}, timeout=timeout)
        print(code_runner.format_project_result(result))
        print("[ran OK]" if result.get("ok") else "[project failed]")

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
            elif cmd == "/quality":
                print(server.memory_quality_report())
            elif cmd == "/qualityfix":
                print(server.memory_quality_repair(apply=(arg.strip().lower() == "apply")))
            elif cmd == "/lessons":
                _print_lessons()
            elif cmd in ("/pass", "/good"):
                if last_iid:
                    print(server.record_outcome(last_iid, "tests_passed"))
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
        cleaned = _strip_footer(out)
        print(cleaned)
        if last_iid:
            print("(/pass or /fail to teach trilobite)")


if __name__ == "__main__":
    main()
