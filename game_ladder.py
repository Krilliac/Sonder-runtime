"""game_ladder — trilobite's "game gauntlet".

Escalating-difficulty ladder: for each level, trilobite generates a
complete, runnable game (code + procedurally-drawn assets), we ground it
by actually running it headless, and the ladder stops at the first level
that fails. Also feeds pass/fail back into trilobite's learning loop via
server.record_outcome, so the gauntlet doubles as training signal.

Stdlib only (+ py_compile/subprocess). No GPU/model calls at import time —
those only happen in the __main__ live driver.
"""
import os
import py_compile
import subprocess
import sys
import tempfile

import grounding

PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "Scripts", "python.exe")

_PYGAME_TEST_INSTRUCTION = (
    "Write a COMPLETE, runnable, single-file Python program. Return ONLY the code "
    "in one python code block. Use only the standard library and pygame; draw ALL "
    "visuals procedurally with pygame.draw/Surface — NO external files. It will be "
    "tested by running headless (dummy video driver) and terminating it after a "
    "few seconds, so just make sure it runs without errors from the start (don't "
    "require real input)."
)

_CONSOLE_TEST_INSTRUCTION = (
    "Write a COMPLETE, runnable, single-file Python program. Return ONLY the code "
    "in one python code block. Use only the standard library. It will be tested by "
    "running it with input piped from stdin and terminating after a few seconds; "
    "handle end-of-input gracefully (e.g. catch EOFError) or simply be runnable — "
    "just make sure it does not crash with an error."
)


def _console(n, name, desc):
    return {"n": n, "name": name, "kind": "console",
            "prompt": "%s %s" % (desc, _CONSOLE_TEST_INSTRUCTION)}


def _pygame(n, name, desc):
    return {"n": n, "name": name, "kind": "pygame",
            "prompt": "%s %s" % (desc, _PYGAME_TEST_INSTRUCTION)}


LEVELS = [
    _console(1, "number_guessing", "Build a number-guessing game: the program picks a "
             "secret random number in a range and the player guesses, getting "
             "higher/lower feedback until they win."),
    _console(2, "tic_tac_toe", "Build a tic-tac-toe game where the human player plays "
             "against a simple AI opponent (does not need to be unbeatable, just a "
             "reasonable move-picking heuristic)."),
    _pygame(3, "player_square", "Build a pygame program with a window that draws a "
            "background and a player square the user moves with the arrow keys."),
    _pygame(4, "pong", "Build a pygame Pong game: two paddles (one can be AI or "
            "player 2), a bouncing ball, a score for each side, and a win condition."),
    _pygame(5, "snake", "Build a pygame Snake game: a grid, food that spawns "
            "randomly, a snake that grows when it eats, and game over on "
            "self-collision or wall collision."),
    _pygame(6, "breakout", "Build a pygame Breakout/Arkanoid game: a paddle, a ball, "
            "a grid of bricks that break on collision, scoring, and a limited number "
            "of lives."),
    _pygame(7, "asteroids", "Build a pygame Asteroids game: a ship that rotates and "
            "thrusts, bullets the ship fires, asteroids that split into smaller "
            "asteroids when hit, and screen wrap-around at the edges."),
    _pygame(8, "space_shooter", "Build a pygame side-scrolling space shooter with "
            "procedurally-drawn sprite assets (draw the ship/enemies/bullets with "
            "pygame.draw shapes onto Surfaces): waves of enemies, a player hp bar, "
            "a score counter, and simple particle effects on explosions."),
    _pygame(9, "platformer", "Build a pygame platformer: gravity, jumping, solid "
            "platforms plus at least one moving platform, collectible items, and a "
            "small hand-designed level layout."),
    _pygame(10, "topdown_dungeon", "Build a pygame tile-based top-down game with a "
            "procedurally-generated map (e.g. random rooms/corridors or cellular "
            "automata), enemies with basic chase AI toward the player, and a "
            "minimap rendered in a corner of the screen."),
    _pygame(11, "multi_scene", "Build a pygame multi-scene game with a menu scene, a "
            "play scene, and a game-over scene (menu -> play -> game-over -> menu), "
            "a high score that is saved to and loaded from a local file, and a "
            "particle system used somewhere in the game (e.g. on scoring or death)."),
    _pygame(12, "tower_defense", "Build an ambitious pygame tower-defense-lite game: "
            "enemies pathfind along a route from spawn to goal (e.g. simple "
            "waypoint-following or grid pathfinding), the player places towers that "
            "shoot at enemies in range, there is a wave counter, and a lose "
            "condition when too many enemies reach the goal."),
]


# Exception types that are EXPECTED when we forcibly cut off input/time on a
# game we're just probing for real crashes — these do not count as failures.
REAL_CRASH_EXCEPTIONS = {"EOFError", "KeyboardInterrupt", "SystemExit", "BrokenPipeError"}
# Console games parse arbitrary piped input, so input-format errors (int(''), bad
# index) are artifacts of our generic feed, not game bugs.
_CONSOLE_EXPECTED = REAL_CRASH_EXCEPTIONS | {"ValueError", "IndexError", "OverflowError"}

# ~200 lines of generic input so console games' input() calls have something
# to consume before eventually hitting EOF.
_STDIN_FEED = (b"1\n2\n3\n5\n1\n1\n\n" * 30)


def detect_failure(stdout, stderr, returncode, timed_out=False, kind="pygame"):
    """Pure classifier: did this run count as a real crash, and why?

    A "crash" is a traceback whose final exception type is a genuine bug
    (NameError, TypeError, pygame.error, etc). EOFError/KeyboardInterrupt/
    SystemExit/BrokenPipeError are expected artifacts of us cutting off
    input/time and do NOT count as failures.

    Returns (failed: bool, reason: str).
    """
    stderr = stderr or ""
    stdout = stdout or ""
    has_traceback = "Traceback (most recent call last)" in stderr

    if timed_out and not has_traceback:
        return False, "ran (loop active, no crash)"

    if has_traceback:
        lines = [l for l in stderr.strip().splitlines() if l.strip()]
        last_line = lines[-1] if lines else ""
        exc_type = last_line.split(":", 1)[0].strip()
        expected = _CONSOLE_EXPECTED if kind == "console" else REAL_CRASH_EXCEPTIONS
        if exc_type in expected:
            return False, "ran (ended on %s, expected)" % exc_type
        return True, last_line

    if returncode == 0:
        return False, "ran clean"

    return False, "exited rc=%d, no crash" % returncode


def ground(code, kind, timeout=12):
    """Run generated game `code` headless and report pass/fail.

    Grounds on whether the game actually crashes (an unexpected traceback),
    not on any self-authored assertions inside the code.

    Returns (passed: bool, detail: str).
    """
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)

        try:
            py_compile.compile(path, doraise=True)
        except (py_compile.PyCompileError, SyntaxError) as e:
            msg = str(e).strip()
            last_line = msg.splitlines()[-1] if msg else "syntax error"
            return False, "SyntaxError: %s" % last_line

        env = dict(os.environ)
        env.update({
            "SDL_VIDEODRIVER": "dummy",
            "SDL_AUDIODRIVER": "dummy",
            "PYGAME_HIDE_SUPPORT_PROMPT": "1",
        })
        interp = PY if os.path.exists(PY) else sys.executable
        try:
            p = subprocess.run(
                [interp, path],
                env=env,
                input=_STDIN_FEED,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            err = e.stderr or b""
            if isinstance(err, str):
                err = err.encode("utf-8", errors="replace")
            err_text = err.decode("utf-8", errors="replace")
            out_text = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            failed, reason = detect_failure(out_text, err_text, None, timed_out=True, kind=kind)
            return (not failed), reason

        out_text = p.stdout.decode("utf-8", errors="replace") if isinstance(p.stdout, bytes) else p.stdout
        err_text = p.stderr.decode("utf-8", errors="replace") if isinstance(p.stderr, bytes) else p.stderr
        failed, reason = detect_failure(out_text, err_text, p.returncode, timed_out=False, kind=kind)
        return (not failed), reason
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run_ladder(gen_fn, start=1, max_levels=99, save_dir="games", record=None):
    """Run the gauntlet from `start`, generating+grounding each level in turn.

    Stops (and returns) at the first level that fails, or after the last
    level in LEVELS / max_levels, whichever comes first.
    """
    levels = [l for l in LEVELS if l["n"] >= start][:max_levels]
    last_n = start - 1
    for level in levels:
        n, name, kind, prompt = level["n"], level["name"], level["kind"], level["prompt"]
        raw = gen_fn(prompt)
        code = grounding.extract_code_block(raw)
        if not code:
            detail = "no code block"
            if record:
                record(level, False, code)
            print("LEVEL %d %s: FAIL — %s" % (n, name, detail))
            return {"reached": last_n, "failed_level": level, "detail": detail}

        level_dir = os.path.join(save_dir, "level_%02d_%s" % (n, name))
        os.makedirs(level_dir, exist_ok=True)
        with open(os.path.join(level_dir, "game.py"), "w", encoding="utf-8") as f:
            f.write(code)

        passed, detail = ground(code, kind)
        if record:
            record(level, passed, code)
        print("LEVEL %d %s: %s — %s" % (n, name, "PASS" if passed else "FAIL", detail))
        if not passed:
            return {"reached": last_n, "failed_level": level, "detail": detail}
        last_n = n

    return {"reached": last_n, "failed_level": None}


if __name__ == "__main__":
    # Live driver — needs Ollama/GPU. Do not run this in automated tests.
    import server

    _last_raw = {"text": None}

    def gen_fn(prompt):
        text = server.trilobite(prompt, num_predict=2048)
        _last_raw["text"] = text
        return text

    def record(level, passed, code):
        raw = _last_raw["text"]
        iid = server.parse_interaction_id(raw) if raw else None
        if iid:
            server.record_outcome(iid, "tests_passed" if passed else "failed")

    start = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    max_levels = int(sys.argv[2]) if len(sys.argv) > 2 else 99

    result = run_ladder(gen_fn, start=start, max_levels=max_levels, record=record)

    reached = result["reached"]
    failed_level = result["failed_level"]
    if failed_level:
        print("GAUNTLET: reached level %d, failed at %d (%s)" % (
            reached, failed_level["n"], result["detail"]))
    else:
        print("GAUNTLET: reached level %d, cleared the whole ladder" % reached)

    os.makedirs("games", exist_ok=True)
    with open(os.path.join("games", "progress.txt"), "w", encoding="utf-8") as f:
        f.write(str(reached))
