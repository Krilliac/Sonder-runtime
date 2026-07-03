"""trilobite — interactive terminal REPL for the local self-improving coding assistant.

Boots straight into the real learning loop (server.trilobite), the way `claude`
drops you into an interactive session. Slash-commands control trace/strict mode,
teach outcomes back, and surface stats/lessons. Stdlib only + server/memory_store.
"""
import sys

import server
import memory_store
import grounding
import training_tasks
import intents
import feedback

BANNER = """trilobite - fully local self-improving coder
type /help for commands, or just start typing to ask trilobite something.
"""

HELP = """commands:
  /help              show this help
  /trace [on|off]    toggle trace mode (bare = on); shows retrieval + prompt
  /strict [on|off]   toggle strict mode (bare = on); pins to the trilobite alias
  /stats             show trilobite's learning stats
  /lessons           show the 10 most recent distilled lessons
  /pass, /good       record the last answer as tests_passed
  /fail, /bad        record the last answer as failed
  /run               actually execute the code block from the last response
  /train [N]         grounded self-learning: practice N tasks (default 3, max 10)
  /exit, /quit, /q   leave
"""

TRAIN_DEFAULT_N = 3
TRAIN_MAX_N = 10


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


def _run_train(n):
    tasks = training_tasks.sample(n)
    passed = 0
    lessons = 0
    for t in tasks:
        print("  training: %s ..." % t["name"])
        resp = server.trilobite(t["prompt"])
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


def main():
    trace = False
    strict = None  # None = env default
    last_iid = None
    last_response = None

    def apply_trace(val):
        nonlocal trace
        trace = val
        print("trace: %s" % ("on" if trace else "off"))

    def apply_strict(val):
        nonlocal strict
        strict = val
        print("strict: %s" % ("on" if strict else "off"))

    def do_run():
        code = grounding.extract_code_block(last_response)
        if code is None:
            print("(no code block in the last response to run)")
            return
        ok, out = grounding.run_code(code)
        if out:
            print(out)
        print("[ran OK]" if ok else "[exited with error]")

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
            elif cmd == "/stats":
                print(server.trilobite_stats())
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
                do_run()
            elif cmd == "/train":
                n = _parse_train_n(arg)
                if n is not None:
                    _run_train(n)
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

        out = server.trilobite(line, trace=trace, strict=strict)
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
