"""sonder — interactive terminal REPL for Sonder Runtime's local learning loop.

Boots straight into the injected legacy runtime's learning loop, the way
`claude` drops you into an interactive session. Slash-commands control
trace/strict mode, teach outcomes back, and surface stats/lessons. The legacy
runtime is an explicit composition dependency; this interface never discovers
it at import time.
"""

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread
import json
import getpass
import hashlib
import inspect
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager, redirect_stdout

from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.application import foreground_turns
from sonder_runtime.domain.runtime_model_configuration import OPTIONAL_LOCAL_TIERS
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.platform import paths as server_paths
import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
from sonder_runtime.adapters.observability.repl_formatting import (
    elapsed_label as _elapsed_label,
)
from sonder_runtime.adapters.observability.response_formatting import (
    _strip_activity_block as _strip_activity,
)
from sonder_runtime.adapters.observability import repl_machine_output as _machine_output
from sonder_runtime.adapters.observability.error_hint_formatting import (
    error_hint as _error_hint,
)
from sonder_runtime.adapters.observability.session_list_formatting import (
    format_sessions as _format_sessions,
)
from sonder_runtime.adapters.observability.session_replay_formatting import (
    REPLAY_DEFAULT_TURNS as _REPLAY_DEFAULT_TURNS,
    format_session_replay as _format_session_replay,
)
import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.adapters.execution_tools import code_runner, grounding
from sonder_runtime.adapters.content_services import intents, training_tasks
from sonder_runtime.adapters.repl_services import personas, web_intents
from sonder_runtime.adapters.content_services import feedback
from sonder_runtime.adapters.web import live_reload
from sonder_runtime.adapters.web import listener_probe
from sonder_runtime.platform import debug_dump
from sonder_runtime.adapters.repl_services import (
    code_improve, consult as consult_flow, tier_router,
)
from sonder_runtime.interfaces.repl import command_router
from sonder_runtime.interfaces.repl import style as S
from sonder_runtime.application.ports import repl_notices
from sonder_runtime.adapters.command_catalog import command_catalog
from sonder_runtime.adapters.security.permission_policy import permission_policy
from sonder_runtime.adapters.repl_services import project_scaffold
from sonder_runtime.adapters.optional_slash_menu import load_optional_slash_menu
from sonder_runtime.interfaces.repl.facades import (
    ContextHealthFacade,
    ExecutionStatusFacade,
    InstalledModel,
    ModelSelectionFacade,
    PermissionModeFacade,
    RecoveryPostureFacade,
)
from sonder_runtime.interfaces.repl.facades.developer_tools import (
    TEST_ACTIONS as _TEST_ACTIONS,
    TEST_USAGE as _TEST_USAGE,
    poll_test_result as _poll_test_result,
    render_digest_command as _render_digest_command,
    render_test_followup as _render_test_followup,
    render_tools_command as _render_tools_command,
    start_test_command as _start_test_command,
)
from sonder_runtime.application.context import local_owner_context as _local_owner_context
from sonder_runtime.interfaces.repl.facades.debug_tools import (
    crash_command as _render_crash_command,
    profile_command as _render_profile_command,
)

# Optional: the live filtering "/" menu. Absent or unusable (piped stdin,
# non-Windows, dumb terminal) the REPL falls back to plain input().
slash_menu = load_optional_slash_menu()

CURRENT_TOKEN = ""
REPL_HISTORY_LIMIT = 200


class _JsonLinesWriter:
    """Turn stdout into a stable JSONL event stream for ``repl --json``.

    The legacy REPL has many presentation call sites.  Adapting the stream at
    the interface boundary keeps their execution and permission behavior
    unchanged while guaranteeing that every stdout line is one parseable JSON
    object.  Input is deliberately never echoed: prompts can contain private
    repository context and credentials are already excluded from REPL history.
    """

    schema = "sonder.repl-output.v1"

    def __init__(self, stream):
        self._stream = stream
        self._buffer = ""
        self._seq = 0
        self.encoding = getattr(stream, "encoding", None) or "utf-8"

    def isatty(self):
        return False

    def writable(self):
        return True

    def write(self, value):
        text = str(value or "")
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line.rstrip("\r"))
        return len(text)

    def _emit(self, text, event="output"):
        self._seq += 1
        payload = {
            "schema": self.schema,
            "seq": self._seq,
            "event": event,
            "text": str(text),
        }
        self._stream.write(json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ) + "\n")

    def emit_event(self, event, text):
        """Emit ``text`` line by line under a named event (``error``)."""
        if self._buffer:
            self._emit(self._buffer.rstrip("\r"))
            self._buffer = ""
        for line in str(text or "").split("\n"):
            self._emit(line.rstrip("\r"), event=event)

    def flush(self):
        self._stream.flush()

    def close(self):
        if self._buffer:
            self._emit(self._buffer.rstrip("\r"))
            self._buffer = ""
        self._stream.flush()


def run_jsonl(stream=None):
    """Run the normal REPL with machine-readable stdout and no terminal UI.

    stderr remains stderr, as conventional command-line tools expect.  This
    mode does not add retries, persist prompts, or reinterpret commands; it is
    only a deterministic presentation adapter around the existing loop.
    """
    _reconfigure_streams()
    writer = _JsonLinesWriter(stream or sys.stdout)
    previous_caps = S._CACHED
    legacy_ndjson = os.environ.pop("SONDER_REPL_NDJSON", None)
    # Machine output is never styled: no colour, no motion, plain words.
    S.set_caps(S.Caps(color="none", glyphs="unicode", plain=False))
    try:
        with redirect_stdout(writer):
            main(machine_output=True)
    finally:
        if legacy_ndjson is not None:
            os.environ["SONDER_REPL_NDJSON"] = legacy_ndjson
        writer.close()
        S._CACHED = previous_caps


class _LegacyRuntimeProxy:
    """Late-bound view of the explicitly configured legacy runtime.

    Keeping the existing command branches pointed at one proxy makes the
    migration mechanical without giving the interface an import-time escape
    hatch. Missing configuration is an explicit dependency failure rather
    than an attempted module lookup or a partially working REPL.
    """

    def __getattr__(self, name):
        runtime = _legacy_runtime
        if runtime is None:
            raise DependencyUnavailable(
                "REPL requires an injected legacy runtime; call "
                "configure_legacy_runtime(runtime) first"
            )
        try:
            return getattr(runtime, name)
        except AttributeError as exc:
            raise DependencyUnavailable(
                "injected REPL runtime does not provide %s" % name
            ) from exc

    def __setattr__(self, name, value):
        """Patch the injected runtime instead of shadowing it on the proxy.

        Besides making the proxy transparent to existing REPL code, this keeps
        direct unit-test monkeypatches isolated: teardown restores the member
        on the configured runtime rather than leaving a stale proxy attribute
        that would bypass a later injection or fail-closed check.
        """
        runtime = _legacy_runtime
        if runtime is None:
            raise DependencyUnavailable(
                "REPL requires an injected legacy runtime; call "
                "configure_legacy_runtime(runtime) first"
            )
        setattr(runtime, name, value)


_legacy_runtime = None
server = _LegacyRuntimeProxy()


def configure_legacy_runtime(runtime):
    """Inject the runtime used by all REPL commands.

    The caller owns composition and lifetime. ``None`` is rejected so a
    failed bootstrap cannot silently reset a previously valid runtime.
    No module discovery or dynamic import is performed here.
    """
    if runtime is None:
        raise DependencyUnavailable(
            "cannot configure the REPL with an empty legacy runtime"
        )
    global _legacy_runtime
    _legacy_runtime = runtime
    return runtime

# The raw composer history is deliberately process-local, but that is not a
# reason to retain credentials for the lifetime of the terminal.  Ctrl+R
# redraws entries in clear text, which makes a successfully typed `/login`
# password visible again to anyone at the console.  These names cover the two
# native commands whose positional arguments are credentials; the assignment
# pattern covers the catalogue commands that accept an explicitly supplied
# bearer token.
_HISTORY_SECRET_COMMANDS = frozenset(("/login", "/register"))
_HISTORY_SECRET_ASSIGNMENT = re.compile(
    r"(?:^|[\s,;])(?:api[_-]?key|authorization|credential|password|passwd|"
    r"secret|token)\s*(?:=|:)\s*\S+",
    re.IGNORECASE,
)


# --- terminal chrome ------------------------------------------------------
#
# Every colour, glyph and width decision comes from ``style`` (spec 2.1-2.10):
# capabilities are detected once at REPL start (``_init_terminal``) and the
# helpers below only translate the REPL's call sites into style roles.  With
# colour ``none`` nothing here emits an ESC byte; with ASCII glyphs the chrome
# is pure ASCII.

_ROLE_ALIASES = {
    "teal": "accent", "cyan": "info", "green": "success", "amber": "warning",
    "red": "danger", "violet": "warning", "text2": "muted", "bold": "strong",
}


def _paint(text, *roles):
    """Paint ``text`` with style roles (``accent``, ``muted``, ...)."""
    return S.s(text, *(_ROLE_ALIASES.get(r, r) for r in roles if r))


def _cols():
    """The terminal's real width (floor 20, no cap)."""
    return S.cols()


def _reconfigure_streams():
    """Never let an unencodable character end the session (P0-1).

    A console on a legacy code page (cp1252, ascii) cannot encode ``·`` or a
    model's emoji; ``errors="replace"`` prints ``?`` instead of raising
    ``UnicodeEncodeError`` out of ``print``.  Streams without ``reconfigure``
    (test doubles, the JSONL writer) are left alone.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError, TypeError):
                pass


def _init_terminal():
    """Detect terminal capabilities once for this REPL session.

    On Windows the composer's ``enable_vt`` probe switches the console into
    VT mode; if that fails, colour is off, links are off and glyphs are
    ASCII, instead of ``←[38;5;..m`` litter on a legacy conhost.
    """
    _reconfigure_streams()
    vt_enable = getattr(slash_menu, "enable_vt", None) if slash_menu is not None else None
    try:
        return S.caps(env=os.environ, stream=sys.stdout, platform=os.name,
                      vt_enable=vt_enable if callable(vt_enable) else None,
                      refresh=True)
    except Exception:
        return S.set_caps(S.Caps())


class _NdjsonCommandWriter(_JsonLinesWriter):
    """``SONDER_REPL_NDJSON=1`` on a pipe: every stdout line is JSON.

    Turn results keep their ``sonder.repl-turn.v1`` line, written raw; every
    other line (command output, notices) becomes a ``sonder.repl-output.v1``
    ``output`` event, so a consumer never meets a line it cannot parse.
    """

    def _emit(self, text, event="output"):
        self._seq += 1
        payload = {"schema": self.schema, "seq": self._seq, "event": event,
                   "text": str(text)}
        self._stream.write(json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"),
        ) + "\n")

    def raw_line(self, line):
        if self._buffer:
            self._emit(self._buffer.rstrip("\r"))
            self._buffer = ""
        self._stream.write(str(line).rstrip("\n") + "\n")
        self._stream.flush()


def _read_input(prompt, *, history=None, composer=False, argument_completer=None,
                refresh_frame=None):
    """Prompt for a line, optionally in the raw terminal composer frame."""
    if slash_menu is not None:
        try:
            if slash_menu.available():
                # Keep ordinary input() as the universal fallback.  The
                # framed composer is raw-terminal presentation only, never a
                # second input protocol or a source of changed prompt text.
                extra = {}
                if composer and refresh_frame is not None:
                    # Shift+Tab goes through the same audited tool /mode
                    # uses, then the frame redraws with the new mode.
                    extra = {"mode_cycle": _cycle_mode, "refresh_frame": refresh_frame}
                return slash_menu.read_line(
                    "" if composer else prompt,
                    history=history, frame=prompt if composer else "",
                    frame_style="",
                    argument_completer=argument_completer,
                    fallback_prompt=prompt,
                    **extra,
                )
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            pass  # a menu problem must never cost the user their prompt
    return input(prompt)


def _cycle_mode():
    """Shift+Tab: advance the mode through the ``permission_mode`` tool.

    Same authority and the same audit record as typing ``/mode <next>``: a
    person at the console pressed the key.
    """
    order = ("plan", "manual", "acceptEdits", "auto")
    snapshot = _permission_mode_snapshot() or {}
    current = str(snapshot.get("mode") or "manual")
    wanted = order[(order.index(current) + 1) % len(order)] if current in order else "manual"
    return _mode_command(wanted)


# --- POSIX line editing (P0-5) --------------------------------------------
#
# Without readline, cooked-mode input() hands arrow keys and Shift+Tab to the
# REPL as ``^[[A`` / ``^[[Z`` bytes, which then went to the model as a turn.
# readline gives history, editing and Tab completion; the stripping in
# ``_normalize_input_line`` is the backstop for everything readline is not.

_READLINE = None


def _readline_completer(text, state):
    try:
        import readline
        line = readline.get_line_buffer()
    except Exception:
        line = text
    if not line.startswith("/") or " " in line.strip():
        return None
    try:
        matches = [c.name for c in command_catalog.complete(line.strip(), limit=40)]
    except Exception:
        matches = []
    matches = [m for m in matches if m.startswith(line.strip())]
    return matches[state] if state < len(matches) else None


def _setup_readline(history):
    """Install readline on a POSIX terminal; returns the module or None."""
    global _READLINE
    if os.name == "nt" or _composer_available():
        return None
    if not (_console_has_operator() and _stdout_is_interactive()):
        return None
    try:
        import readline
    except Exception:
        return None
    try:
        readline.set_completer(_readline_completer)
        readline.set_completer_delims(" \t\n")
        if "libedit" in str(getattr(readline, "__doc__", "") or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
            # A terminal that must not receive escapes (NO_COLOR, TERM=dumb)
            # gets none from readline either: no meta-mode or paste toggles.
            # Plain mode also skips the paste toggle, whose trailing bare
            # carriage return would redraw the line (P1-11).
            caps = S.caps()
            if caps.color == "none" or caps.plain:
                readline.parse_and_bind("set enable-meta-key off")
                readline.parse_and_bind("set enable-bracketed-paste off")
            else:
                readline.parse_and_bind("set enable-bracketed-paste on")
        readline.clear_history()
        for entry in history or ():
            if "\n" not in entry:
                readline.add_history(entry)
    except Exception:
        return None
    _READLINE = readline
    return readline


def _readline_forget_last():
    """Drop the line readline just recorded (an approval answer, a login)."""
    if _READLINE is None:
        return
    try:
        length = _READLINE.get_current_history_length()
        if length:
            _READLINE.remove_history_item(length - 1)
    except Exception:
        pass


def _readline_prompt(text):
    """Mark escapes as zero-width so readline measures the prompt right."""
    if _READLINE is None or "\x1b" not in text:
        return text
    return re.sub(r"(\x1b\[[0-9;]*m)", "\x01\\1\x02", text)


# --- persisted history (P2-5) ---------------------------------------------

_HISTORY_FILE_NAME = "repl_history"


def _history_path():
    try:
        return os.path.join(str(server_paths.default_home()), _HISTORY_FILE_NAME)
    except Exception:
        return ""


def _load_history(path=None):
    """Lines from the persisted history, oldest first, capped at the limit."""
    path = path if path is not None else _history_path()
    if not path:
        return []
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip("\n") for line in handle]
    except OSError:
        return []
    return [line for line in lines if _history_safe(line)][-REPL_HISTORY_LIMIT:]


def _save_history(entries, path=None):
    """Write history 0600, newest ``REPL_HISTORY_LIMIT`` single-line entries."""
    path = path if path is not None else _history_path()
    if not path:
        return False
    keep = [e for e in entries if _history_safe(e) and "\n" not in e][-REPL_HISTORY_LIMIT:]
    try:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            os.write(fd, "".join(e + "\n" for e in keep).encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


# ``_confirm`` pushes a slash command typed as an approval answer here, so
# the main loop's history (and readline's) can recall it with Up.
_HISTORY_SINK = None


def _clear_terminal_scrollback(stream=None):
    """Erase the interactive terminal screen and its scrollback buffer.

    This intentionally affects presentation only: conversation/session state,
    command history, and the currently selected model remain intact.  It is
    shared by the native ``/clear`` command and matches the Ctrl+L behaviour
    in :mod:`slash_menu`.
    """
    target = stream or sys.stdout
    csi = getattr(slash_menu, "CSI", "\x1b[")
    try:
        clear = getattr(slash_menu, "clear_terminal_presentation", None)
        if callable(clear):
            clear(target)
        else:
            target.write(csi + "3J" + csi + "2J" + csi + "H")
            target.flush()
    except (AttributeError, OSError):
        # A piped or closed stdout must not terminate the REPL merely because
        # a presentation-only command was requested.
        return False
    return True


# The gate's own controls are never gated by it. `permission_mode` is risk
# `ask`, which `plan` denies -- so gating it would trap whoever is at the
# keyboard in `plan` with no console way back out. A human typing the command
# is the authority the gate exists to serve; the agent path deliberately gets
# no such exemption (a model must not be able to lift its own restraint, and
# `_agent_dispatch` cannot reach this tool at all).
#
# Aliased, not restated: the MCP protocol entry point exempts the same names
# for the same reason, and two hand-kept copies of a security-relevant set is
# how one of them silently stops matching the other.
# Compatibility alias retained for callers that inspect the console surface.
# The provider owns the lookup; the interface does not import the legacy
# permission_modes module or consult the set during dispatch.
GATE_EXEMPT_TOOLS = permission_policy.gate_control_tools()


def _console_has_operator():
    """Whether a person is actually at the console to answer a prompt.

    This is the value ``permission_modes.decide(interactive=...)`` is asking
    for -- it means "is somebody present to answer", not "is this the console
    module". Passing a hardcoded ``True`` from here was simply the wrong
    argument for `sonder < script.txt` or `echo /stats | sonder`, where nobody
    is present and ``input()`` does not ask anybody anything: it reads the
    next line of the script.

    Deliberately narrower than ``slash_menu.available()``, which also consults
    ``TERM=dumb`` and ``SONDER_NO_MENU``. Those decide whether the raw composer
    can be *drawn*; ``NO_COLOR`` only removes styling and deliberately keeps
    the composer's keyboard behavior. None of these is evidence about whether
    a person is there, and letting them quietly change what the permission gate
    enforces would be its own defect.
    """
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        # A stream that cannot answer is not evidence of an operator.
        return False


def _stdout_is_interactive():
    """Whether presentation can safely add terminal-only chrome.

    An operator may type at a terminal while redirecting stdout to a file.
    That is still interactive for permission prompts, but result decoration
    belongs only on an actual terminal so redirected output stays script-safe.
    """
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


class _Approval(str):
    """An approval question: the full text as a string, plus its layout.

    It is a ``str`` so everything that already treats the question as text
    (tests, logs) keeps working; ``_confirm`` reads the extra attributes to
    draw the spec 2.8 prompt and to require ``yes`` for ``[danger]``.
    """

    def __new__(cls, command, risk, summary, reason):
        word = command_catalog.risk_word(risk) if hasattr(command_catalog, "risk_word") else ""
        text = "approve %s %s\n%s\n%s" % (command, word, summary, reason)
        value = super().__new__(cls, text)
        value.command = command
        value.risk = risk
        value.word = word
        value.summary = summary
        value.reason = reason
        value.danger = risk == "dangerous"
        return value

    def lines(self, width):
        c = S.caps()
        role = "danger" if self.danger else "warning"
        head = "%s approve  %s" % (S.g("ask", c), S.safe_text(self.command))
        word = self.word
        room = width - 1 - S.cell_width(word) - 1
        if S.cell_width(head) > room:
            head = S.truncate(head, max(8, room), c)
        pad = " " * max(1, width - 1 - S.cell_width(head) - S.cell_width(word))
        out = [S.s(head, "strong", c=c) + pad + S.s(word, role, c=c) if word
               else S.s(head, "strong", c=c)]
        for text in (self.summary, self.reason):
            if text:
                out.extend(S.wrap(S.safe_text(text), width - 1, indent="  ",
                                  hanging="  "))
        return out

    def prompt(self):
        return "  type 'yes' to run: " if self.danger else "  run it? [y/N] "


def _flush_typeahead():
    """Discard keys typed before the question was shown (P0-4).

    A line typed ahead while a turn ran (``/env``, a stray ``y``) must never
    be read as the answer to a prompt it was not written for.
    """
    stream = getattr(sys, "stdin", None)
    try:
        if stream is None or not stream.isatty():
            return
    except Exception:
        return
    if os.name == "nt":
        try:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
        except Exception:
            pass
        return
    try:
        import termios
        termios.tcflush(stream.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


# The approval answer that was a slash command, for the skip notice.
_DIVERTED_ANSWER = ""


def _confirm(question):
    """Ask a y/N question, defaulting to no. Anything but an explicit yes is no.

    Every non-answer -- EOF on piped stdin, a closed console, Ctrl-C -- is a
    "no". A permission prompt that a missing terminal turns into a "yes" is
    worse than no prompt, because it looks like it asked.

    With no operator present it does not read *at all*. Treating EOF as "no"
    only ever covered the last line of a piped script; every earlier line
    returns a real string, so the read succeeded and silently ate the next
    command. Callers must not reach here without an operator anyway -- see
    ``_gate_tools`` -- so this is the safety net rather than the seam: a
    function whose whole job is asking a person has no business reading a
    line nobody typed.

    Pending input is flushed first, so type-ahead never answers.  ``y``,
    ``yes``, ``n``, ``no`` and an empty line are answers; anything else asks
    again, and three invalid answers are a no.  An answer that starts with
    ``/`` is a no: that command was not run, and it is pushed to history so
    Up recalls it.  A ``[danger]`` question needs the whole word ``yes``.
    """
    global _DIVERTED_ANSWER
    _DIVERTED_ANSWER = ""
    if not _console_has_operator():
        return False
    danger = bool(getattr(question, "danger", False))
    if isinstance(question, _Approval):
        for line in question.lines(_cols()):
            print(line)
        prompt = question.prompt()
    else:
        prompt = "%s [y/N] " % question
    _flush_typeahead()
    for _attempt in range(3):
        try:
            answer = input(_readline_prompt(prompt))
        except (EOFError, OSError, KeyboardInterrupt):
            return False
        _readline_forget_last()
        value = str(answer or "").strip()
        lowered = value.lower()
        if value.startswith("/"):
            _DIVERTED_ANSWER = value
            if _HISTORY_SINK is not None:
                try:
                    _HISTORY_SINK(value)
                except Exception:
                    pass
            return False
        if lowered in ("", "n", "no"):
            return False
        if lowered == "yes" or (lowered == "y" and not danger):
            return True
        if danger and lowered == "y":
            prompt = "  type the whole word 'yes' to run: "
        else:
            prompt = "  please answer y or n [y/N] "
    return False


def _risk_summary(label):
    """The catalog's one-line summary for a command, or ""."""
    try:
        command = command_catalog.by_name(label.split(None, 1)[0])
    except Exception:
        command = None
    return str(getattr(command, "summary", "") or "")


# Most-to-least severe, so a command that fronts several tools is described by
# the worst thing it can do rather than by whichever branch ast.walk saw first.
_RISK_ORDER = ("dangerous", "execution", "mutation", "ask", "safe")
_RISK_RANK = {risk: index for index, risk in enumerate(_RISK_ORDER)}


def _severity(risk):
    """Rank a risk class, treating an unrecognised one as the most severe.

    A class this list has not heard of is by definition unclassified, and the
    rest of the gate fails closed on ignorance rather than open. Ranking it
    with a plain ``_RISK_ORDER.index`` instead would raise ``ValueError``
    inside the gate -- turning "a new risk class was added" into a crashed
    REPL loop, which is a worse answer than either allowing or refusing.
    """
    return _RISK_RANK.get(risk, -1)


def _gate_tools(tools, label, command_line=None):
    """Strictest decision across ``tools``; returns ``(may_run, refusal_text)``.

    The console is the one surface that *can* have a human attached, so ``ask``
    means actually asking rather than degrading to allow the way a direct MCP
    call does -- but only when one actually is. ``deny`` prints why and runs
    nothing, whoever is or is not watching.

    ``interactive`` is therefore ``_console_has_operator()`` and not a
    hardcoded ``True``. A piped session (`sonder < script.txt`) has nobody to
    ask, so it is answered exactly like every other non-interactive caller:
    file changes, host programs and destructive tools are refused with the
    remedies named, ask-class tools proceed on the record, and a ``deny`` rule
    and ``plan`` still refuse. Asking anyway was worse than useless --
    ``input()`` read the next line of the script as the answer, so one unseen
    prompt both denied the command and
    swallowed the one after it.

    A named command can front several tools (``/todo`` reaches everything from
    ``task_list`` to ``task_delete``), and which one runs depends on an
    argument this gate does not parse. ``_named_command_gate`` narrows the
    read forms the argument grammar recognises first (a bare ``/todo`` lists;
    ``/todo list`` is a read); whatever the argument could not rule out
    arrives here, and this gate decides on the *strictest* member: a deny
    anywhere refuses, otherwise the highest-risk ``ask`` is the one the
    operator is asked about, once. Rounding the other way -- gating a command
    that can delete at the risk of its most harmless sibling -- would be
    under-enforcement, which is the failure this whole change exists to fix.
    """
    interactive = _console_has_operator()
    worst = None
    for tool in tools:
        # The exemption is not applied here any more: this asks for a decision
        # *for a person at a console*, and `permission_modes` owns which
        # exemptions that kind of caller carries. Four surfaces kept their own
        # copy of the check and the fifth was written without it.
        decision = permission_policy.decide_for_caller(
            tool, interactive=interactive, gate_control_exempt=True, surface="repl",
        )
        if decision is None:
            continue
        if decision.action == permission_policy.deny_action():
            return False, "refused %s: %s (mode: %s)" % (
                label, decision.reason, permission_policy.mode_label(decision.mode),
            )
        if decision.action != permission_policy.ask_action():
            continue
        if worst is None or _severity(decision.risk) < _severity(worst.risk):
            worst = decision
    if worst is None:
        return True, ""
    question = _Approval(
        str(command_line or label), worst.risk, _risk_summary(label), worst.reason,
    )
    if _confirm(question):
        return True, ""
    return False, "skipped %s" % label


# Autopilot actions that create a run or steer one.  Only these carry the
# console's owner: status/resume/pause/cancel stay unscoped exactly as before,
# so the console still sees and controls every local run, including legacy
# unowned ones.
_AUTOPILOT_OWNER_ACTIONS = frozenset({"run", "start", "plan", "steer", "clarify"})


def _repl_console_owner():
    """A stable opaque owner for runs this OS user starts from the console.

    Steering is owner-scoped and fails closed for unowned runs, and the
    console used to create every run unowned, so ``/autopilot steer`` and
    ``clarify`` were advertised but always refused.  The owner is a digest of
    the OS user and the Sonder state home: stable across console restarts,
    distinct from the ``ta-`` account scopes the served API derives, and never
    a name a remote caller can present.
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = str(getattr(os, "getuid", lambda: "")())
    material = "repl-console-autopilot-owner\0%s\0%s" % (
        user, os.path.realpath(str(server_paths.default_home())),
    )
    return "rc-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _repl_autopilot_owner(cmd, arg):
    """The owner to pass for one ``/autopilot`` or ``/mission`` line, or None."""
    words = str(arg or "").split(None, 1)
    action = words[0].lower() if words else ""
    command = str(cmd or "").lower()
    if command == "/mission":
        return _repl_console_owner() if action == "start" else None
    return _repl_console_owner() if action in _AUTOPILOT_OWNER_ACTIONS else None


def _help_policy_note(topic):
    """The standing permission rules that refuse a command, for ``/help <cmd>``.

    The catalog grades a command by what its branch can do (``/delete`` is a
    hard-coded dry run, so ``safe``), while the gate also applies the rule
    set, where the shipped ``file_delete`` deny refuses it in every mode.
    ``/help`` used to show only the grade, so it advertised a command the
    gate always refuses.  This names the refusing rule next to the grade.
    The rule is deliberately not relaxed for the dry run: an explicit deny
    outranks every call site.  Read-only; records no decision.
    """
    name = str(topic or "").strip().split(None, 1)
    if not name:
        return ""
    cmd = "/" + name[0].lstrip("/").lower()
    try:
        tools = command_catalog.console_tools().get(cmd, ())
    except command_catalog.CatalogUnavailable:
        return ""
    notes = []
    for tool in tools:
        try:
            decision = permission_policy.decide_for_caller(
                tool, interactive=_console_has_operator(),
                gate_control_exempt=True, surface="repl", record=False,
            )
        except Exception:
            continue
        if (
            decision is not None
            and decision.action == permission_policy.deny_action()
            and getattr(decision, "source", "") == "rule"
        ):
            notes.append("  policy:   refused -- %s" % decision.reason)
    return ("\n" + "\n".join(dict.fromkeys(notes))) if notes else ""


def _permission_gate(tool):
    """Gate one tool dispatched as ``/<tool_name>`` through _run_catalogued."""
    return _gate_tools((tool,), "/" + tool)


def _named_command_gate(cmd, argument=""):
    """Gate a hand-written console branch (``/write``, ``/delete``, ``/mkdir``).

    ``_run_catalogued`` is only the *fallback* path: roughly fifty named
    branches in ``main`` -- and another twenty-five that ``main`` forwards to
    ``server.control_command`` -- call their tool directly and never reach it.
    Left ungated, ``/write x hi`` wrote a file in ``plan`` mode while
    ``/file_write`` was refused, which is the same "policy that reports one
    thing and enforces another" this change exists to remove, reintroduced at
    a new site.

    So the gate sits at ONE choke point, the top of the slash chain, and reads
    which tools a branch can invoke out of the source
    (``command_catalog.console_tools``) rather than out of a hand-kept table
    that would go stale the first time someone adds a branch. Commands that
    front no tool (``/help``, ``/exit``, ``/trace``) are absent from that map
    and are not gated. Named branches and ``_run_catalogued`` are disjoint by
    construction -- a command is handled by a branch or by the fallback, never
    both -- so nothing is prompted for twice.

    A catalog that cannot read the tool registry refuses here rather than
    returning an empty map. An empty map made `_gate_tools(())` answer
    "allowed" for every command it covers, so the gate did not break -- it
    turned off, silently, for the life of the process. Fail closed on
    ignorance: a gate that cannot tell what a command runs must not let it run.
    """
    try:
        tools = command_catalog.console_tools().get(cmd, ())
    except command_catalog.CatalogUnavailable as exc:
        return False, "refused %s: %s" % (cmd, exc)
    if cmd in ("/lanes", "/recover"):
        # LaneConsoleFacade separates reads from effects and gates effects with
        # their prepared principal/root/payload. A coarse gate here would consume
        # a one-shot approval before that exact command reaches its own gate.
        # Recovery likewise uses its original prepared attachment/verification
        # identities and real approval ledger inside the managed host boundary.
        expected = "agent_lane" if cmd == "/lanes" else "workspace_run"
        if set(tools) != {expected}:
            return False, "refused %s: scoped command catalog is unavailable" % cmd
        return True, ""
    # Workspace creation is implemented by a nested REPL helper, so it is not
    # visible to the catalog's top-level branch scanner.  Keep its filesystem
    # mutation in the same single choke-point gate as every other command.
    if cmd in ("/workspace-create", "/workspacecreate"):
        tools = tuple(tools) + ("directory_create",)
    # A command that fronts several tools is graded by its strictest member,
    # but the recognised read forms (``/selfmod status``, ``/todo list``) reach
    # only the member the argument names; narrowing before the gate keeps a
    # read from being prompted for -- or, piped, refused for -- a write it
    # cannot perform.
    tools = command_catalog.narrow_branch_tools(cmd, argument, tools)
    return _gate_tools(tools, cmd, ("%s %s" % (cmd, argument or "")).strip())


def _mode_command(argument):
    """`/mode` -- show every mode, switch to one, or explain one.

    Delegates to the `permission_mode` tool rather than reimplementing it, so
    the console and the MCP surface cannot drift apart and a mode change made
    from here is still recorded like any other tool call. It stays a REPL
    branch (not a catalogued dispatch) so it can never be refused by the gate
    it controls -- see GATE_EXEMPT_TOOLS.
    """
    wanted = str(argument or "").strip()
    explain = False
    for flag in ("--explain", "explain"):
        if wanted.endswith(flag):
            wanted = wanted[: -len(flag)].strip()
            explain = True
            break
    # The console is the attended surface: a person typed this, so it may
    # raise autonomy (unattended callers may only lower it).
    with permission_policy.attended_mode_change():
        return server.permission_mode(mode=wanted, explain=explain)


def _run_catalogued(line, cmd):
    """Run any registered MCP tool typed as /<tool_name>, or explain the miss.

    Every tool is catalogued, so this is what makes the whole surface -- not
    just the branches written out above -- reachable from the console.

    This is also where the permission gate applies to the console: the tool is
    resolved first (so an unknown command still gets its suggestions rather
    than a confusing refusal), then `permission_modes.decide` runs before the
    handler is ever called.
    """
    try:
        parsed = command_catalog.parse_invocation(line)
    except ValueError as exc:
        return str(exc)
    except command_catalog.CatalogUnavailable as exc:
        # Resolving the command is itself a catalog read. Refuse rather than
        # dispatch something the gate could not have classified.
        return "refused %s: %s" % (cmd, exc)
    if parsed:
        tool, kwargs = parsed
        handler = getattr(server, tool, None)
        if callable(handler):
            # A successful /login is the REPL's authenticated session. Thread
            # it through catalogue-dispatched tools that explicitly accept a
            # token, while preserving an explicit command argument if present.
            # This keeps durable fanout status/recovery usable in an account
            # deployment without making users paste session tokens into input.
            try:
                accepts_token = "token" in inspect.signature(handler).parameters
            except (TypeError, ValueError):
                accepts_token = False
            if accepts_token:
                kwargs.setdefault("token", CURRENT_TOKEN)
            may_run, refusal = _permission_gate(tool)
            if not may_run:
                return refusal
            try:
                return str(handler(**kwargs))
            except TypeError as exc:
                return "%s: %s\n%s" % (
                    cmd, exc, command_catalog.help_command(cmd),
                )
            except Exception as exc:
                return "%s failed: %s" % (cmd, exc)
    # One line, suggestion first (spec P2-1), fitted to the terminal.
    # On a terminal the notice prefix ("? unknown  ") costs three more cells.
    return command_catalog.unknown_command(
        cmd, width=_cols() - (3 if _stdout_is_interactive() else 0),
        sep=" %s " % S.g("sep"), ellipsis=S.g("ellipsis"),
    )


def _format_route_explanation(report):
    """Render ``command_router.explain()`` for the console, one fact per line.

    The report is evidence from the resolver itself, so this only formats --
    it never re-derives or second-guesses what would resolve.
    """
    lines = ["turn:      %s" % report["input"]]
    detail = report.get("detail") or {}
    if report["resolved"]:
        lines.append("resolved:  %s" % report["resolved"])
    else:
        lines.append(
            "resolved:  nothing -- the turn goes to ordinary chat/work handling"
        )
    source = report["source"]
    if source == "rule":
        lines.append("stage:     hand-written rule %s" % detail.get("index"))
    elif source == "tier":
        lines.append("stage:     tier intent (/%s)" % detail.get("command"))
    elif source == "structured":
        lines.append("stage:     explicit \"use the <name> tool\" form")
    elif source == "catalog":
        lines.append(
            "stage:     generic catalog match (%s)" % detail.get("command", "")
        )
    elif source == "slash":
        lines.append("stage:     already a slash line; the router never sees these")
    elif source == "empty":
        lines.append("stage:     empty turn")
    else:
        lines.append(
            "stage:     no match (%s)"
            % detail.get("reason", "no stage claimed the turn")
        )
        if detail.get("candidates"):
            lines.append("tied:      %s" % ", ".join(detail["candidates"]))
        if detail.get("leftover"):
            lines.append("leftover:  %s" % ", ".join(detail["leftover"]))
        if detail.get("command"):
            lines.append("nearest:   %s" % detail["command"])
    return "\n".join(lines)


# CSI (``ESC [ ... final``), SS3 (``ESC O x``), and a bare ESC + one char.
# Cooked-mode input() without readline hands arrow keys (``^[[A``),
# Shift+Tab (``^[[Z``) and bracketed-paste markers (``^[[200~``) to the REPL
# as text; before this they became a 60-90 s model turn (P0-5).
_INPUT_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1bO.|\x9b[0-?]*[ -/]*[@-~]|\x1b.?")


def _normalize_input_line(line):
    """Strip console framing: a BOM from piped PowerShell, and key escapes."""
    value = str(line or "")
    # Windows PowerShell 5.1 may send a UTF-8 BOM that Python's console codec
    # exposes either correctly as U+FEFF or as the three Latin-1 code points.
    for prefix in ("﻿", "\xef\xbb\xbf", "\xff\xfe", "\xfe\xff"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if "\x1b" in value or "\x9b" in value:
        value = _INPUT_ESCAPE.sub("", value)
    return value.strip()


def _history_safe(line):
    """Whether a submitted REPL line may be retained for Ctrl+R recall.

    This is intentionally a deny-only privacy boundary: it never rewrites a
    command into something that could be recalled and rerun with changed
    semantics.  Sensitive turns are simply absent from the in-memory history;
    normal command and chat recall remain unchanged.
    """
    value = str(line or "").strip()
    command = value.split(None, 1)[0].lower() if value else ""
    return bool(value) and command not in _HISTORY_SECRET_COMMANDS and not (
        _HISTORY_SECRET_ASSIGNMENT.search(value)
    )


def _result_tag(ok):
    return _paint("PASS" if ok else "FAIL", "success" if ok else "danger", "strong")


def _emit(text):
    """Print command output with untrusted text made inert (P0-2).

    Output from tools, files, the model and errors can carry ESC, OSC 52,
    BEL or bidi overrides; ``safe_text`` shows them as visible escapes, on a
    terminal and on a pipe alike.  On a terminal, refusals, ``ERROR:`` text
    and unknown commands go through the one notice component.
    """
    value = S.safe_text("" if text is None else text)
    if _stdout_is_interactive():
        rendered = _as_notice(value)
        if rendered is not None:
            print(rendered)
            return
    emit_event = getattr(sys.stdout, "emit_event", None)
    if (callable(emit_event) and not hasattr(sys.stdout, "raw_line")
            and _is_repl_error(value)):
        # ``repl --json``: a command error is an ``error`` event (P2-6), not
        # ordinary output a consumer would have to pattern-match.
        emit_event("error", value)
        return
    print(value)


_NOTICE_PREFIXES = (
    ("refused ", "refused"), ("refused:", "refused"), ("ERROR:", "error"),
    ("ERROR ", "error"), ("unknown command ", "unknown"),
)


_REFUSAL_WORDS = re.compile(
    r"outside (?:the )?allowed roots|outside sonder's file roots|outside the selected"
    r" workspace|\brefused\b|\bnot allowed\b|\bdenied\b|HOST POLICY",
    re.IGNORECASE,
)
# Paths under a system credential store never get a "how to get in" hint
# (spec 2.7): the refusal is the answer.
_CREDENTIAL_PATHS = re.compile(
    r"/etc/(?:shadow|gshadow|sudoers|master\.passwd)|~?/\.ssh\b|/\.gnupg\b|"
    r"/\.aws/credentials|/\.netrc\b|\\config\\sam\b",
    re.IGNORECASE,
)

# The slash line being dispatched, so a refusal or error names what was typed.
_CURRENT_LINE = [""]


def _as_notice(value, title=None):
    """A notice for a refusal/error/unknown line, or None for ordinary output."""
    text = str(value or "")
    for prefix, kind in _NOTICE_PREFIXES:
        if not text.startswith(prefix):
            continue
        first, _, rest = text.partition("\n")
        if kind == "unknown":
            # "? unknown  /hlep · did you mean /help? · ..." -- the kind word
            # already says "unknown", so the title starts at the name.
            title = first[len("unknown "):]
            if title.startswith("command "):
                title = title[len("command "):]
            return S.notice(kind, title, rest.strip() or None, width=_cols())
        body = first[len(prefix):].strip()
        head = title if title is not None else _CURRENT_LINE[0]
        if not head:
            # "refused /cmd: reason" -> title "/cmd", detail "reason".
            name, sep, reason = body.partition(": ")
            head, body = (name, reason) if sep and name.startswith("/") else (body, "")
        elif kind == "refused":
            _name, sep, reason = body.partition(": ")
            body = reason if sep and _name.startswith("/") else body
        if kind == "error" and _REFUSAL_WORDS.search(first):
            kind = "refused"
        hint = _error_hint(text) or None
        # "path is outside allowed roots. Set X or ..." -> detail + hint.
        sentence, dot, advice = body.partition(". ")
        if dot and advice and kind == "refused":
            body, hint = sentence, hint or advice.rstrip(".")
        if _CREDENTIAL_PATHS.search("%s %s" % (head, text)):
            hint = None
        detail = "\n".join(part for part in (body, rest.strip()) if part) or None
        return S.notice(kind, head, detail, hint=hint, width=_cols())
    return None


def _installed_models():
    """Return catalog rows, or ``None`` when Ollama discovery is unavailable.

    An empty list is a valid successful response from a fresh Ollama instance;
    it must not be confused with an unreachable/malformed discovery response.
    ``/model <tag>`` relies on that distinction before creating a session pin.
    """
    try:
        payload = server._get("/api/tags")
    except Exception:
        return None
    return [
        (model.name, model.size)
        for model in ModelSelectionFacade.installed_models(payload) or ()
    ]


def _selectable_tiers():
    """Tiers the REPL may actually route a turn to, with policy applied.

    ``server.TIERS`` is the raw table: it still lists the hosted cloud tiers
    when ``SONDER_ALLOW_CLOUD`` is unset.  ``server.available_tiers()`` is the
    filtered view the serve layer and ``/v1/models`` already publish and that
    ``_serve_target`` enforces.  Reading the raw table here made the console
    the one surface that offered a route it could not take: ``/model
    cloud-code`` reported a successful switch, and then every following chat
    turn came back "hosted/cloud tiers are disabled" -- the rejection the
    selection itself should have carried.
    """
    try:
        tiers = server.available_tiers()
    except Exception:
        tiers = None
    if isinstance(tiers, dict):
        return ModelSelectionFacade.selectable_tiers(tiers, None)
    # Availability filtering is a courtesy, not a gate: the real refusal still
    # happens in `_serve_target`. A policy read that raises must not be able to
    # empty the tier list and leave `/model` with nothing to offer.
    try:
        return ModelSelectionFacade.selectable_tiers(None, server.TIERS)
    except Exception:
        return {}


def _unselectable_tier_reason(name):
    """Why a configured tier is withheld right now, or "" when it is offered.

    Only names that exist in the raw table can be withheld; an unknown word is
    not a tier at all and must keep falling through to model-tag resolution so
    it still gets the catalog's "did you mean" suggestions.
    """
    try:
        available = _selectable_tiers()
        if not name or name not in server.TIERS or name in available:
            return ""
        if name in getattr(server, "CLOUD_TIERS", ()):
            # One copy of the wording: the console explains a refusal exactly
            # the way the chat turn it prevents would have.
            message = server._cloud_disabled_message()
        else:
            message = ""
        return ModelSelectionFacade.withheld_reason(
            name, configured=server.TIERS, available=available,
            cloud_tiers=getattr(server, "CLOUD_TIERS", ()),
            cloud_disabled_message=message,
        )
    except Exception:
        return ""


_MODEL_DISCOVERY_UNSET = object()


class _ModelArgumentCompleter:
    """Cached model/tier vocabulary for the interactive ``/model`` palette.

    The palette redraws after each key, so querying ``/api/tags`` from the
    completer would turn ordinary typing into repeated network traffic. The
    first ``/model`` completion obtains a snapshot, then a successful `/model`
    command refreshes that snapshot from its already-required discovery call.
    """

    def __init__(self):
        self._choices = None

    def refresh(self, installed=_MODEL_DISCOVERY_UNSET):
        if installed is _MODEL_DISCOVERY_UNSET:
            installed = _installed_models()
        # The palette must offer only what `/model` will actually accept, or
        # keyboard selection becomes a shortcut to a refusal.
        try:
            tiers = [str(name) for name in _selectable_tiers() if str(name)]
        except Exception:
            tiers = []
        models = [str(name) for name, _size in (installed or []) if str(name)]
        # Tiers win a same-named collision because /model resolves them before
        # exact tags. Keep display order deterministic for keyboard selection.
        self._choices = ModelSelectionFacade.choices(
            dict((name, True) for name in tiers),
            tuple(InstalledModel(name) for name in models),
        )
        return self._choices

    def __call__(self, command, prefix, *, limit=8):
        if str(command or "").casefold() != "/model":
            return []
        if self._choices is None:
            self.refresh()
        needle = str(prefix or "").casefold()
        return [choice for choice in self._choices if choice.casefold().startswith(needle)][
            :max(1, int(limit))
        ]


def _model_selection_ineligibility(model):
    """Return a known reason a discovered model cannot serve REPL chat.

    ``/model`` used to verify only that a tag was installed.  That made an
    embedding-only model look successfully selected, then deferred the useful
    rejection until the next ordinary chat turn.  The HTTP chat endpoint
    already consults the richer catalog record before prewarming, so reuse the
    same positive capability evidence here.  Discovery remains best-effort:
    a catalog that omits capability metadata must not turn a valid local model
    into a false negative.
    """
    try:
        found = server.resolve_discovered_model_record(model)
        if found is None:
            return ""
        _name, record = found
        return server._fanout_nonchat_reason(record)
    except Exception:
        # `/model` has already established that Ollama was reachable enough to
        # list this tag.  A second metadata lookup is advisory only; preserve
        # the pre-existing selection behavior if it races a catalog reload.
        return ""


def _home_relative(path):
    """Show ~ instead of the home prefix, the way a shell prompt does."""
    try:
        return "~" + os.sep + os.path.relpath(path, os.path.expanduser("~"))
    except (ValueError, OSError):
        return str(path)


def _permission_mode_snapshot():
    """Read current permission/elevation state for presentation only."""
    return PermissionModeFacade(
        lambda: server.permission_mode_data(),
    ).snapshot()


def _mode_fields(snapshot):
    """``(mode, elevated, reason, blurb)`` from a permission snapshot."""
    view = PermissionModeFacade()
    state = view.snapshot(snapshot)
    if state is None:
        return "unknown", False, "", ""
    return (
        str(state.get("mode") or "unknown"),
        view.elevated(state),
        view.elevation_reason(state),
        str(state.get("blurb") or "").strip(),
    )


def _endpoint():
    """``(url, live)`` for the local listener, loopback for wildcard binds."""
    try:
        host, port = listener_probe.DEFAULT_HOST, listener_probe.DEFAULT_PORT
        live = bool(listener_probe.port_open(host, port))
        # 0.0.0.0/:: are bind addresses, not browser destinations.
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return "http://%s:%s" % (display_host, port), live
    except Exception:
        return os.environ.get("SONDER_API", "http://127.0.0.1:11435"), False


def _mode_cycle_key():
    """True only when a Shift+Tab handler is really installed (P1-6)."""
    return bool(getattr(slash_menu, "supports_mode_cycle", False)) and _composer_available()


def _banner_state(strict, persona, project, tier=None, *, session_id="",
                  model_override=None, notices=None):
    """Everything the banner and ``/about`` show, read live from the runtime.

    Every lookup is guarded: a cosmetic header must never be the reason a
    REPL fails to start.  Startup never waits on a remote Git server; the
    cached ref is enough and ``/updatecheck`` is the explicit refresh.
    """
    tier = tier or "code"
    try:
        model = ModelSelectionFacade.resolved_model(server.TIERS, tier, model_override)
    except Exception:
        model = "unknown"
    endpoint, live = _endpoint()
    try:
        source = server.runtime_source_update_status_data(refresh=False)
        source = source if isinstance(source, dict) else {}
    except Exception:
        source = {}
    mode, elevated, reason, blurb = _mode_fields(_permission_mode_snapshot())
    if notices is None:
        try:
            notices = repl_notices.pending_repl_notices()
        except Exception:
            notices = 0
    return S.BannerState.from_source(
        source, persona=str(persona or ""), model=str(model or "unknown"),
        tier=tier, mode=mode, endpoint=endpoint, live=live,
        mode_cycle_key=_mode_cycle_key(), notices=int(notices or 0),
        elevated=elevated, elevated_reason=reason, strict=bool(strict),
        project=str(project or ""), session_id=str(session_id or ""),
        mode_blurb=blurb,
    )


def _startup_banner(strict, persona, project, tier=None, **kwargs):
    """The one startup banner (spec 2.5), the same design on every platform."""
    width = kwargs.pop("width", None) or _cols()
    return S.banner(_banner_state(strict, persona, project, tier, **kwargs), width)


def _composer_available():
    """Whether the raw composer frame will draw the prompt's title itself."""
    if slash_menu is None:
        return False
    try:
        return bool(slash_menu.available())
    except Exception:
        return False


def _prompt_glyph():
    """The gutter glyph the plain prompt is reduced to."""
    return _paint(S.g("prompt"), "accent", "strong") + " "


def _compact_count(value):
    """Render a non-negative count without making the status line noisy."""
    try:
        value = max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return "?"
    return S.compact_count(value)


def _composer_context(session_id, project):
    """Return the current approximate context budget without failing input."""
    return ContextHealthFacade(
        lambda **kwargs: server.context_health_data(**kwargs),
    ).snapshot(session_id, project)


def _turn_metrics(response=None):
    """Extract safe aggregate metrics from the REPL's completed response span."""
    response = response if isinstance(response, dict) else {}
    try:
        return {
            "tokens_in": max(0, int(response.get("tokens_in") or 0)),
            "tokens_out": max(0, int(response.get("tokens_out") or 0)),
            "elapsed_ms": max(0, int(response.get("elapsed_ms") or 0)),
            "model_calls": max(0, int(response.get("model_calls") or 0)),
            "tool_calls": max(0, int(response.get("tool_calls") or 0)),
        }
    except (TypeError, ValueError, OverflowError):
        return None


def _latest_repl_turn_metrics(session_id="", *, surfaces=("terminal/mcp",)):
    """Read metrics for the expected, immediately completed REPL response."""
    try:
        response = activity_tracker.latest()
    except Exception:
        return None
    if not isinstance(response, dict) or response.get("surface") not in set(surfaces):
        return None
    if session_id and response.get("session") != session_id:
        return None
    return _turn_metrics(response)


def _status_state(tier=None, *, context=None, model_override=None,
                  permission=None, status=None, project="default"):
    """The persistent session state for the status line (spec 2.4).

    Per-turn metrics are deliberately absent: the answer footer is the only
    place they appear (P1-2).
    """
    resolved_tier = str(tier or "code")
    try:
        model = ModelSelectionFacade.resolved_model(server.TIERS, resolved_tier, model_override)
    except Exception:
        model = "unknown"
    mode, elevated, reason, _blurb = _mode_fields(permission)
    lanes, agents = ExecutionStatusFacade(
        lambda: server.execution_status_data(),
    ).counts(status)
    context = context if isinstance(context, dict) else {}
    state = S.StatusState(
        mode=mode, tier=resolved_tier, model=str(model or ""),
        ctx_used=context.get("used"), ctx_limit=context.get("limit"),
        agents=agents if isinstance(agents, int) else 0,
        lanes=lanes if isinstance(lanes, int) else 0,
        project=str(project or "default"), elevated=elevated,
        elevated_reason=reason,
    )
    # The status line omits zero counts; /status must not print an unknown
    # execution status as "0 running" (the old "[lanes ? | agents ?]").
    state.execution_known = isinstance(agents, int) and isinstance(lanes, int)
    return state


def _status_text(tier=None, *, width=None, **kwargs):
    """One status line, one vocabulary at every width (P1-1)."""
    return S.status_line(_status_state(tier, **kwargs), width or _cols())


def _status_long(state, width):
    """``/status``: every status field, labelled, plus the pool in words."""
    rows = [
        ("mode", state.mode + (" ELEVATED" if state.elevated else "")
         + (" (%s)" % state.elevated_reason if state.elevated_reason else "")),
        ("tier", state.tier), ("model", state.model or "unknown"),
        ("context", ("%s of %s tokens" % (_compact_count(state.ctx_used),
                                            _compact_count(state.ctx_limit)))
         if state.ctx_limit else "unknown"),
        ("agents", ("%d running, %d lanes" % (state.agents, state.lanes))
         if getattr(state, "execution_known", True) else "unknown (execution status unavailable)"),
        ("project", state.project),
    ]
    endpoint, live = _endpoint()
    rows.append(("endpoint", "%s %s" % (endpoint, "listening" if live else "not listening")))
    rows.append(("pool", _pool_words()))
    label_w = max(len(r[0]) for r in rows)
    out = []
    for label, value in rows:
        prefix = "  %s  " % label.ljust(label_w)
        lines = S.wrap(S.safe_text(value), width - 1, indent=prefix,
                       hanging=" " * len(prefix))
        lines[0] = "  " + _paint(label.ljust(label_w), "muted") + lines[0][2 + label_w:]
        out.extend(lines)
    out.extend(_paint(line, "muted") for line in S.wrap(
        "/status pool for worker detail %s /about for provenance" % S.g("sep"),
        width - 1, indent="  ", hanging="  "))
    return "\n".join(out)


def _pool_words():
    """The inference pool in plain words: ``Ollama: 1 worker, idle``."""
    try:
        summary = server.OLLAMA_POOL.summary()
        workers = int(summary.get("worker_count") or 0)
        eligible = int(summary.get("eligible_worker_count") or 0)
        queue = summary.get("queue") or {}
        waiting = int(queue.get("waiting") or 0)
    except Exception:
        return "unknown (the pool did not answer)"
    words = "%d worker%s" % (workers, "" if workers == 1 else "s")
    if eligible != workers:
        words += " (%d ready)" % eligible
    state = "idle" if waiting == 0 else "%d waiting" % waiting
    return "Ollama: %s, %s" % (words, state)


def _composer_frame_width():
    """Use the raw composer's current width for the frame title when drawn."""
    if slash_menu is None:
        return None
    try:
        return int(slash_menu._terminal_size()[0])
    except Exception:
        return None


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
  /about             show source provenance, endpoint, session and mode details
  /status [pool]     show mode, model, context, endpoint; pool shows worker detail
  /logs [n]          show the last n records of this session's REPL log
  /trace [on|off]    toggle trace mode (bare = on); shows retrieval + prompt
  /strict [on|off]   toggle strict mode (bare = on); pins to the sonder alias
  /persona [name]    show/set active persona (coder/explainer/reviewer/teacher)
  /model [name|tier] list installed models and tiers; switch either one
  /cloud [status|on|off]  change process-local hosted/cloud consent
  /consult <question> ask 2 local tiers (+cloud when enabled) and compare answers
  /route <request>   suggest the tier best suited to a request, and why
  /refactor <file> <fn> [goal]  propose a guarded improvement to one function
  /scaffold <kind> <name> [root]  write a full project skeleton (cpp-msvc, csharp, rust, ...)
  /workspace [path]  show/set the directory used for guarded project work;
                     /files /read /write /append /edit /mkdir /delete then
                     resolve relative paths inside it and refuse escapes
  /workspace-create <path>  create a guarded directory, select it, and resume queued work
  /env [refresh]     show the host OS, shells, and installed toolchains
  /toolstatus <name> run the fixed local version probe for a discovered tool
  /tools [refresh|category|name]  categorized host tool inventory with versions
  /test [runner] [selector]  run the project's tests; /test status|result|cancel <job>
  /digest <job|path> summarize job output or a log: final line, failures, errors
  /crash <dump|core|log> [--exe P] [--sym DIR] [--engine E] [--repro NAME]  digest a crash (minidump, core, sanitizer/valgrind log)
  /crash triage <path|dir>  pure read, no debugger; a folder is bucketed by signature
  /crash symbols on|off  allow symbol-server downloads for this console session
  /crash fix <run_id|last>  fatal diagnostics, local source excerpt and a repro test
  /crash status|result|cancel <run_id>  follow a debugger run
  /profile <capture> [--budget MS] [--top N]  hot paths, frame spikes, allocations
  /profile status|result|cancel <run_id>  follow a profiler run
  /location [on|off] allow approximate IP location for "my area" weather answers
  /stats             show Sonder Runtime's learning stats
  /context           show context, session, and memory health meters
  /contextsize [N]   show/set requested context (8k..1m; native num_ctx is clamped)
  /compact           preview context compaction/rollover recommendations
  /commands [filter] list available commands by category, name, or risk
  /why [text]        explain how the previous plain-language turn (or [text]) routed
  /version           show the runtime version and release stamp
  /activity [watch]  show once, or poll projected new events until Ctrl+C
  /work <task>       execute a guarded tool-using workflow with checklist/report
  /autopilot ...     persistent plan/run/status/resume/pause/cancel autonomy
  /mission ...       one goal + optional task plan + autopilot (status|start|done|abandon)
  /runtime ...       shared local model mappings and execution-lane tiers
  /stash ...         save/restore this install's source edits for a guarded update
  /hardware          detect RAM, GPU runtime, VRAM, and offload support
  /training ...      plan/start/status/deploy/rollback attended weight training
  /selfmod ...       inspect/plan/test/approve/deploy/rollback isolated improvements
  /approvals         list calls refused unattended and the one-shot approvals issued
  /approve <call id> approve exactly one refused call once (/approve revoke <nonce>)
  /mcp ...           audit/refresh atomic MCP source and tool convergence
  /learning          show grounded outcomes, lesson sources, and memory hygiene
  /report            show the latest grounded end report and action transcript
  /checklist [id]    show the current or selected persistent checklist
  /inventory [path]  summarize a guarded workspace with explicit scan budgets
  /tree [path]       list a guarded folder tree
  /search q|root|g   search text under a guarded root (optional glob)
  /vision img|question  ask the local vision tier about a guarded image
  /programs [query]  find installed programs available to the workbench
  /scripts q|root    find runnable scripts under a guarded root
  /image <path>      inspect image metadata and dimensions
  /mkdir <path>      create a guarded directory
  /runprogram p|a|c  run a program with JSON args and optional cwd
  /runscript p|a|c   run a known script type with JSON args and optional cwd
  /dump [label]      dump this chat and debug info to a text file
  /todo ...          list/add/update visible task state
  /todo plan t|s|s   plan a titled set of ordered, auto-sequenced steps
  /todo progress     show a progress bar and per-status task counts
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
  /lanes [help]      inspect and control durable agent conversations
  /recover [cursor] inspect managed work; /recover resume <id> <command-id> resumes verification
  /recovery          show the configured control-state recovery posture (read-only)
  /fanouts [N|active]  list safe recent durable model-fanout summaries
  /capacity [N]      show queued-agent ceiling and safe concurrent worker slots
  /agentcancel <id>  cooperatively cancel an agent/master prefix or all
  /agentretry <id>   explicitly retry persisted interrupted/failed master work
  /weather <place>   get sourced live conditions and a short forecast
  /asset <n> <brief> generate a general icon/audio/model/scene artifact pack
  /artifact-mobility list | status <id>  read local copy receipts
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
  /cot               model reasoning; refused without opt-in (flag + allow rule)
  /permissions [tool] show local permission rules or one matched rule
  /mode [name]       show or set how much runs without asking (plan/manual/acceptEdits/auto)
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
  /clear             clear terminal scrollback; keep chat/session state
  /sessions          list past conversation threads
  /replay [id|title] [N]  re-render the last N stored turns of a thread (default: this one)
  /resume <id|title> continue a past thread by id or title prefix
  terminal editing   Up/Down history; Ctrl+R search; Left/Right/Home/End; Ctrl+W word; Ctrl+K suffix; Ctrl+L clear
  /project [name]    show/set the active project (scopes facts)
  /fact <text>       remember a durable fact for the active project
  /fact forget <id> confirm  remove one listed active-project fact
  /facts             list facts and IDs for the active project
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
    global memory_store, grounding, training_tasks, intents, feedback, personas, debug_dump
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
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


def _is_repl_error(text):
    """Classify narrow host refusals that lack the legacy ``ERROR:`` prefix.

    An exact model pin can disappear or become route-incompatible after the
    REPL's `/model` check. Those host refusals intentionally avoid new server
    error literals, but they must not be rendered as a normal model answer.
    """
    raw = str(text or "")
    # A successful model turn has a durable interaction footer. Never turn a
    # model-authored imitation of a host refusal into an error, because that
    # would discard its answer and break feedback/`/run` continuity.
    if server.parse_interaction_id(raw) is not None:
        return False
    value = server._strip_activity_block(raw).strip()
    if value.startswith("ERROR"):
        return True
    if not value.startswith("model pin '"):
        return False
    return (
        value.endswith(" is unavailable or is not chat-capable.")
        or (
            "' is incompatible with the selected " in value
            and value.endswith(" route.")
        )
    )


def _completion_timing(started_at):
    """A user-facing duration for one REPL model/work turn.

    The server's internal footer is intentionally stripped before printing an
    answer, so keep the terminal's timing signal separate, stable, and free of
    request content.  ``monotonic`` makes wall-clock adjustments irrelevant.
    """
    elapsed_ms = max(0, int((time.monotonic() - float(started_at)) * 1000))
    if elapsed_ms < 1000:
        return "Sonder completed in %dms" % elapsed_ms
    return "Sonder completed in %.2fs" % (elapsed_ms / 1000.0)


class _WorkingIndicator:
    """The live line for one synchronous turn (spec 2.6, P1-3).

    One row, refreshed at 1 Hz: phase, elapsed time, model, tokens so far and
    the cancel hint, read from the activity tracker's live span.  After 20 s
    with no new activity it adds the slow-model hint.  With motion off
    (``SONDER_PLAIN``, ``TERM=dumb``, ``NO_COLOR``) nothing is redrawn: it
    prints ``working...`` once and a ``working (30s)`` line every 15 s, so
    the output holds no carriage returns and no escapes.
    """

    INTERVAL = 1.0
    SLOW_AFTER = 20.0
    PLAIN_EVERY = 15.0

    def __init__(self, label="Sonder", stream=None, *, model="", clock=None):
        self.label = str(label or "Sonder")
        self.stream = stream or sys.stdout
        self.model = str(model or "")
        self._clock = clock or time.monotonic
        self.started = self._clock()
        self._stop = threading.Event()
        self._thread = owned_runtime_thread(target=self._run, daemon=True)
        self._drawn = False
        self._progress_key = None
        self._progress_at = self.started

    def start(self):
        self._thread.start()
        return self

    def _span(self):
        """The newest active response span, or None."""
        try:
            active = activity_tracker.snapshot().get("active") or []
        except Exception:
            return None
        return active[-1] if active else None

    def state(self, now=None):
        now = self._clock() if now is None else now
        span = self._span() or {}
        events = span.get("events") or []
        calls = int(span.get("model_calls") or 0)
        tokens = int(span.get("tokens_in") or 0)
        last = events[-1] if events else {}
        kind = str(last.get("kind") or "")
        if kind in ("tool_call", "tool_result"):
            phase = str(last.get("tool") or last.get("name") or "tool")
        elif calls or kind == "model_call":
            phase = "model call %d" % (calls + 1)
        elif span:
            # The tracker records a model call only when it returns, so from
            # ``response_start`` to the first return the turn is routing and
            # then waiting on the model with no event between them.  Name
            # that honestly rather than showing "routing" for minutes.
            phase = "thinking"
        else:
            phase = "routing"
        key = (len(events), calls, tokens)
        if key != self._progress_key:
            self._progress_key, self._progress_at = key, now
        slow = now - self._progress_at >= self.SLOW_AFTER
        return S.LiveState(phase=phase, elapsed_s=max(0.0, now - self.started),
                           model=self.model, tokens_in=tokens or None, slow=slow)

    def _render(self, now=None):
        return S.live_line(self.state(now), _cols())

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            self._stop.set()

    def _run(self):
        c = S.caps()
        if not c.motion:
            self._write(("working..." if c.plain else self._render()) + "\n")
            next_line = self.started + self.PLAIN_EVERY
            while not self._stop.wait(0.25):
                if self._clock() >= next_line:
                    self._write(self._render() + "\n")
                    next_line += self.PLAIN_EVERY
            return
        while not self._stop.is_set():
            self._write("\r\x1b[2K" + self._render())
            self._drawn = True
            self._stop.wait(self.INTERVAL)

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(1.5)
        if self._drawn:
            self._write("\r\x1b[2K")
            self._drawn = False


_TODO_USAGE = (
    "usage: /todo [list] | /todo add <title> | /todo start <id> | "
    "/todo done <id> | /todo block <id> | /todo show <id>\n"
    "       /todo plan <title> | <step> | <step> ...\n"
    "       /todo progress | /todo delete <id> | "
    "/todo depend <id> <depends-on-id>"
)
_RUN_USAGE = (
    "usage: /run [seconds]  (runs the previous fenced code block, not a"
    " filename or shell command)"
)
# ``/todo <action>`` forms whose branch needs a non-empty remainder, with the
# usage it prints without one.
_TODO_ID_USAGE = {
    "done": "usage: /todo done <task-id>", "complete": "usage: /todo done <task-id>",
    "finish": "usage: /todo done <task-id>",
    "start": "usage: /todo start <task-id>", "doing": "usage: /todo start <task-id>",
    "block": "usage: /todo block <task-id>", "blocked": "usage: /todo block <task-id>",
    "show": "usage: /todo show <task-id>", "view": "usage: /todo show <task-id>",
    "delete": "usage: /todo delete <task-id>", "rm": "usage: /todo delete <task-id>",
    "remove": "usage: /todo delete <task-id>",
}
_TODO_KNOWN_ACTIONS = frozenset(_TODO_ID_USAGE) | {
    "list", "ls", "add", "create", "new", "plan", "progress", "status",
    "depend", "dep", "blockedby",
}
_MCP_ACTIONS = frozenset({"status", "show", "audit", "list", "refresh", "help", "?"})


def _is_int(text):
    try:
        int(text)
    except (TypeError, ValueError):
        return False
    return True


def _branch_usage_error(cmd, arg):
    """The usage text a named branch answers for ``arg`` without any tool call.

    The permission gate grades a command by the tools its branch can reach,
    so it used to run before the branch's own grammar: a bare ``/register``
    asked for approval of a dangerous command and then printed usage, and
    ``/todo bogus`` or ``/fact forget`` were refused as destructive instead of
    being told how to type the command.  This mirrors exactly the argument
    checks those branches make before calling anything, so a malformed line is
    answered here and never reaches the gate.  It is pure: no tool, no state,
    and an empty result means "let the gate and the branch decide".
    """
    command = str(cmd or "").strip().lower()
    raw = str(arg or "")
    text = raw.strip()
    if command == "/register":
        if len(raw.split(None, 1)) != 2:
            return "usage: /register <username> <password>"
    elif command in ("/run", "/runwindow", "/runnew", "/runconsole", "/runproject"):
        if text and not _is_int(text):
            return _RUN_USAGE
    elif command in ("/train", "/learn"):
        if text and not _is_int(text):
            return "usage: /train [N]  (N must be an integer, default %d)" % TRAIN_DEFAULT_N
    elif command == "/fact":
        if not text:
            return "usage: /fact <text> | /fact forget <id> confirm"
        lowered = text.lower()
        if lowered == "forget" or lowered.startswith("forget "):
            bits = text.split()
            if len(bits) != 3 or bits[2].lower() != "confirm":
                return "usage: /fact forget <id> confirm"
    elif command in ("/todo", "/task", "/tasks"):
        if not text:
            return ""
        action, _, rest = text.partition(" ")
        action = action.lower()
        if action not in _TODO_KNOWN_ACTIONS:
            return _TODO_USAGE
        if action in _TODO_ID_USAGE and not rest.strip():
            return _TODO_ID_USAGE[action]
        if action == "plan":
            steps = [part.strip() for part in rest.split("|") if part.strip()]
            if len(steps) < 2:
                return "usage: /todo plan <title> | <step> | <step> ..."
        if action in ("depend", "dep", "blockedby") and len(rest.split()) != 2:
            return "usage: /todo depend <task-id> <depends-on-id>"
    elif command in ("/goal", "/goals"):
        action, _, rest = text.partition(" ")
        if action.lower() in ("adopt", "decline") and not rest.strip():
            return "usage: /goal %s <proposal-id>  (list them with /goal proposals)" % (
                action.lower(),
            )
    elif command in ("/mcp", "/convergence"):
        action = text.lower()
        if action and action not in _MCP_ACTIONS:
            return "usage: /mcp [status|refresh|help]  (unknown MCP action '%s')" % action
    elif command in ("/write", "/append"):
        if len(raw.split(None, 1)) != 2:
            return "usage: %s <path> <text>" % command
    elif command == "/edit":
        pieces = raw.split("|", 2)
        if len(pieces) != 3 or not pieces[0].strip():
            return "usage: /edit <path>|<old>|<new>"
    elif command in ("/read", "/mkdir", "/delete"):
        if not text:
            return "usage: %s <path>" % command
    return ""


def _workspace_scoped_path(workspace, raw):
    """Resolve a file-command path against the selected ``/workspace``.

    Returns ``(path, error)``.  With no workspace selected the argument is
    returned unchanged and the default file roots apply.  With one selected, a
    relative path is joined to the workspace, and any path whose canonical
    form (symlinks followed) leaves the workspace is refused.  The returned
    path is the lexical join, not the canonical one, so the file layer still
    sees -- and refuses -- a symlinked spelling of a mutation target.

    Selecting a workspace never widens file authority: a workspace outside
    Sonder's configured file roots is refused here rather than granted.
    """
    text = str(raw or "").strip()
    if not workspace or not text:
        return text, ""
    base = os.path.realpath(workspace)
    if not file_ops.inside_allowed_roots(base):
        return "", (
            "refused: the selected workspace %s is outside Sonder's file roots,"
            " so file commands cannot use it; add it to SONDER_FILE_ROOTS or"
            " %s, or /workspace clear to use the default roots"
            % (base, file_ops.roots_file_path())
        )
    candidate = os.path.expanduser(text)
    if not os.path.isabs(candidate):
        candidate = os.path.join(base, candidate)
    resolved = os.path.realpath(candidate)
    try:
        inside = os.path.commonpath([
            os.path.normcase(resolved), os.path.normcase(base),
        ]) == os.path.normcase(base)
    except ValueError:
        inside = False
    if not inside:
        return "", (
            "refused: %s is outside the selected workspace %s; use a path"
            " inside it, or /workspace clear to use the default file roots"
            % (text, base)
        )
    return candidate, ""


@contextmanager
def _workspace_file_scope(workspace):
    """Cap file-layer authority at the selected workspace for one command.

    Defence in depth for ``_workspace_scoped_path``: the file layer re-checks
    containment against the workspace at resolution time, so a path that is
    swapped for a link between the two checks still cannot escape.
    """
    if not workspace:
        yield
        return
    root = os.path.realpath(workspace)
    with file_ops.managed_root_scope(lambda: (root,)):
        yield


@contextmanager
def _interruptible_turn():
    """Run one REPL turn in its own cancellable foreground scope.

    While the turn runs, SIGINT first cancels the turn's scope in the shared
    cancellation tree -- so every cooperative checkpoint below it (model
    requests, agent steps) refuses further work -- and then raises
    ``KeyboardInterrupt`` to unwind the blocking call.  The caller turns that
    into "turn cancelled, back to the prompt"; only an interrupt at the idle
    prompt ends the session.  The previous SIGINT disposition is restored when
    the turn ends.
    """
    with foreground_turns.foreground_turn("repl-turn") as node:
        installed = False
        previous = None

        def on_interrupt(signum, frame):
            foreground_turns.cancel(node)
            raise KeyboardInterrupt

        if threading.current_thread() is threading.main_thread():
            try:
                previous = signal.signal(signal.SIGINT, on_interrupt)
                installed = True
            except (ValueError, OSError):
                installed = False
        try:
            yield node
        except KeyboardInterrupt:
            # Also cancel when the interrupt did not come through the handler
            # (a test double, or a platform that raises it directly).
            foreground_turns.cancel(node)
            raise
        finally:
            if installed:
                signal.signal(
                    signal.SIGINT,
                    previous if previous is not None else signal.default_int_handler,
                )


# The model the next turn will use, set by the loop before each turn.
_TURN_MODEL = [""]


def _begin_chat_turn(label="Sonder", *, model=None):
    """Start the live line for an interactive turn (TTY only; P1-3)."""
    if not (_console_has_operator() and _stdout_is_interactive()):
        return None
    return _WorkingIndicator(label, model=_TURN_MODEL[0] if model is None else model).start()


def _parse_activity_events(text):
    """Extract tool call events from the embedded activity block.

    Returns ``(clean_answer, events, stats)`` where *events* is a list of
    dicts with keys ``tool``, ``title``, ``ok``, ``elapsed_ms``, ``summary``
    and *stats* has aggregate counts.  When no activity block is present the
    events list is empty and the answer is returned unchanged.

    The block contains two tool-call sections: ``recent events:`` (with
    timing) and ``actions:`` (with ok/fail status from the transcript).
    The actions section is preferred because it carries status; timing from
    recent events is merged in by matching title order.
    """
    marker = "=== ACTIVITY (observable work) ==="
    end_marker = "=== END ACTIVITY ==="
    idx = text.find(marker)
    if idx < 0:
        return text.rstrip(), [], {}
    clean = text[:idx].rstrip()
    block_end = text.find(end_marker, idx)
    block = text[idx:block_end] if block_end > idx else text[idx:]
    actions = []
    timed_events = []
    stats = {}
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("model calls:"):
            parts = line.split()
            try:
                stats["model_calls"] = int(parts[2])
                stats["tool_calls"] = int(parts[5])
                tokens_part = " ".join(parts[8:])
                if "/" in tokens_part:
                    tin, tout = tokens_part.split("/", 1)
                    stats["tokens_in"] = int(tin)
                    stats["tokens_out"] = int(tout)
            except (IndexError, ValueError):
                pass
        elif line.startswith("files:"):
            parts = line.split()
            try:
                stats["file_creates"] = int(parts[1].lstrip("+"))
                stats["file_edits"] = int(parts[2].lstrip("~"))
                stats["file_deletes"] = int(parts[3].lstrip("-"))
            except (IndexError, ValueError):
                pass
        elif line.startswith(("• ", "× ")):
            ok = line[0] == "•"
            title = line[2:].strip()
            actions.append({"title": title, "ok": ok})
        elif line.startswith("+") and "ms " in line:
            parts = line.split(None, 2)
            if len(parts) >= 3:
                try:
                    elapsed = int(parts[0].lstrip("+").rstrip("ms"))
                except ValueError:
                    elapsed = 0
                kind = parts[1]
                detail = parts[2] if len(parts) > 2 else ""
                if kind == "tool_call":
                    timed_events.append({
                        "title": detail, "ok": True, "elapsed_ms": elapsed,
                    })
    if actions:
        timing_iter = iter(timed_events)
        for action in actions:
            te = next(timing_iter, None)
            if te:
                action["elapsed_ms"] = te.get("elapsed_ms")
        return clean, actions, stats
    return clean, timed_events, stats


def _tool_row_lines(events, width):
    """Tool rows (spec 2.6), aligned with ``style.table``; always shown.

    ``▸ ✓ ok       Read File src/a.py   12ms`` -- the status glyph always
    carries its word, and a failed or refused call is never hidden, even
    when the whole turn errored (P1-4).
    """
    if not events:
        return []
    rows = []
    for ev in events:
        ok = ev.get("ok", True)
        glyph, word, role = ("ok", "ok", "success") if ok else ("fail", "failed", "danger")
        status = _paint("%s %s" % (S.g(glyph), word), role)
        title = S.safe_text(" ".join(str(ev.get("title") or "tool").split()))
        elapsed = ev.get("elapsed_ms")
        timing = _paint(S.duration_label(elapsed), "muted") if elapsed else ""
        rows.append((_paint(S.g("tool"), "muted"), status, title, timing))
    return S.table(rows, [(1, 1, "<"), (8, 9, "<"), (8, None, "<"), (0, 8, ">")],
                   width, indent="  ")


def _render_tool_rows(events, width=None):
    for line in _tool_row_lines(events, width or _cols()):
        print(line)


def _compact_number(n):
    """Format a token count compactly: 1234 -> 1.2k, 12345 -> 12k."""
    if n < 1000:
        return str(n)
    if n < 10000:
        return "%.1fk" % (n / 1000.0)
    return "%dk" % (n // 1000)


# How many answers have offered the long "rate: /pass /fail" footer.
_RATE_OFFERS = [0]


def _print_body(text, width):
    """Answer body: sanitized; prose soft-wrapped on a terminal only."""
    clean = S.safe_text(text)
    for line in S.wrap(clean, width - 1):
        print(line)


def _print_chat_result(text, started_at, *, offer_feedback=False,
                       label="Sonder", error=False, indicator=None,
                       interaction_id=None, metrics=None):
    """Present one completed turn.

    Piped/scripted use keeps the historical plain output (answer, then a
    ``[timing]`` line) so shells and callers do not receive decoration; the
    answer is sanitized either way (P0-2).  SONDER_REPL_NDJSON=1 lets a
    piped caller opt into one structured JSON line per turn instead.  On a
    terminal: turn header, tool rows (always, including on error), the body,
    and one muted footer that is the only place the turn's metrics and its
    single clock appear (P1-2).
    """
    if indicator is not None:
        indicator.stop()
    answer = str(text or "")
    elapsed_ms = max(0, int((time.monotonic() - float(started_at)) * 1000))
    if not (_console_has_operator() and _stdout_is_interactive()):
        if _machine_output.enabled(os.environ) or hasattr(sys.stdout, "raw_line"):
            line = _machine_output.ndjson_line(_machine_output.turn_payload(
                _strip_activity(answer), elapsed_ms=elapsed_ms, error=error,
                interaction_id=interaction_id,
                feedback_offered=offer_feedback, label=label,
                hint=_error_hint(answer) if error else "",
            ))
            raw = getattr(sys.stdout, "raw_line", None)
            if callable(raw):
                raw(line)
            else:
                print(line)
            return
        emit_event = getattr(sys.stdout, "emit_event", None)
        if error and callable(emit_event):
            # ``repl --json``: a turn that failed is an ``error`` event.
            emit_event("error", S.safe_text(_strip_activity(answer)))
            return
        print(S.safe_text(answer))
        print(_paint("[%s]" % _completion_timing(started_at), "muted"))
        if offer_feedback:
            print("(/pass or /fail to teach Sonder Runtime)")
        return

    width = _cols()
    clean_answer, events, stats = _parse_activity_events(answer)
    clean_answer = _strip_activity(clean_answer)
    print(S.turn_header("error" if error else "answer", width))
    rows = _tool_row_lines(events, width)
    for line in rows:
        print(line)
    if rows:
        print()
    _print_body(clean_answer, width)
    metrics = metrics if isinstance(metrics, dict) else {}
    server_ms = int(metrics.get("elapsed_ms") or 0)
    rate = ""
    if offer_feedback:
        _RATE_OFFERS[0] += 1
        rate = "full" if _RATE_OFFERS[0] <= 3 else "short"
    hint = " ".join(S.safe_text(_error_hint(answer) or "").split()) if error else ""
    state = S.FooterState(
        elapsed_ms=server_ms or elapsed_ms, ok=not error,
        model_calls=metrics.get("model_calls") or stats.get("model_calls"),
        tokens_in=metrics.get("tokens_in", stats.get("tokens_in")),
        tokens_out=metrics.get("tokens_out", stats.get("tokens_out")),
        tool_calls=metrics.get("tool_calls") or stats.get("tool_calls"),
        hint=hint, rate=rate,
    )
    line = S.footer(state, width)
    if hint and ("hint: " + hint) not in S.strip_ansi(line):
        # A hint too long for the footer row gets its own wrapped lines
        # rather than being cut off mid-sentence.
        state.hint = ""
        print(S.footer(state, width))
        for text in S.wrap("hint: " + hint, width - 1, indent="  ", hanging="    "):
            print(_paint(text, "muted"))
        return
    print(line)


def _format_fanout_summaries(payload):
    """Render the intentionally content-free durable fanout recovery index."""
    try:
        data = payload or {}
        if data.get("error"):
            return "fanout history refused: %s" % str(data["error"])
        rows = list(data.get("runs") or [])
    except AttributeError:
        return "fanout history unavailable"
    if not rows:
        return "no durable fanout runs"
    lines = ["recent fanouts (%d)" % len(rows)]
    for row in rows:
        elapsed_ms = row.get("total_elapsed_ms")
        try:
            elapsed = "  %s" % _elapsed_label(max(0, int(elapsed_ms)))
        except (TypeError, ValueError, OverflowError):
            elapsed = ""
        lines.append(
            "  %(run_id)s  %(status)s  %(scope)s  "
            "%(models_answered)s/%(models_selected)s answered  "
            "%(models_failed)s failed  %(models_unknown)s unknown  "
            "%(models_pending)s pending  %(models_running)s running  %(models_skipped)s skipped%(elapsed)s" % {
                "run_id": str(row.get("run_id") or "unknown"),
                "status": str(row.get("status") or "unknown"),
                "scope": str(row.get("scope") or "unknown"),
                "models_answered": int(row.get("models_answered") or 0),
                "models_selected": int(row.get("models_selected") or 0),
                "models_failed": int(row.get("models_failed") or 0),
                "models_unknown": int(row.get("models_unknown") or 0),
                "models_pending": int(row.get("models_pending") or 0),
                "models_running": int(row.get("models_running") or 0),
                "models_skipped": int(row.get("models_skipped") or 0),
                "elapsed": elapsed,
            }
        )
    lines.append("  use /model_fanout_status run_id=<id> for an authorized full receipt")
    return "\n".join(lines)


def _approve_lane_command(arguments):
    """Approve the exact console command without consuming piped input."""
    from sonder_runtime.interfaces.repl.facades.agent_lanes import terminal_text
    decision = permission_policy.decide_for_caller(
        "agent_lane", interactive=_console_has_operator(), gate_control_exempt=False,
        surface="repl", arguments=arguments,
    )
    if decision is None or decision.action == permission_policy.allow_action():
        return True, ""
    if decision.action == permission_policy.deny_action():
        reason = decision.reason
        if getattr(decision, "call_id", "") and getattr(decision, "source", "") == "unattended":
            reason += "\nApprove once: /approve " + decision.call_id
        return False, reason
    if not _console_has_operator():
        return False, "interactive approval unavailable; use an operator console or an exact one-shot approval"
    detail = terminal_text(json.dumps(arguments, ensure_ascii=False, indent=2),
                           width=_cols(), limit=20000)
    if _confirm("run this lane action?\n" + detail + "\n" + terminal_text(decision.reason)):
        return True, ""
    return False, "operator declined"


def _artifact_mobility_command(arg):
    """Read-only local receipt view; no publisher or outbound mutation port."""
    parts = arg.split()
    if parts == ["list"] or not parts:
        action = "list"
    elif len(parts) == 2 and parts[0] == "status":
        action = "status"
    else:
        return json.dumps({"outcome_code": "INVALID_REQUEST"})
    try:
        application = server._application()
        payload = (application.artifact_mobility_list() if action == "list"
            else application.artifact_mobility_status(parts[1]))
        return json.dumps(payload, sort_keys=True)
    except Exception:
        return json.dumps({"outcome_code": "UNAVAILABLE"})


def _developer_services():
    """The composed developer tools (inventory, test runs, digest) or None."""
    try:
        return getattr(server._application(), "developer_tools", None)
    except Exception:
        return None


def _developer_context(workspace=""):
    import uuid
    from pathlib import Path

    root = Path(workspace) if workspace else file_ops.workspace_root()
    return _local_owner_context(
        correlation_id=uuid.uuid4().hex, source="repl", workspace_roots=(root,),
    )


def _tools_command(arg):
    """``/tools``: the operator sees full, unredacted host paths."""
    return _render_tools_command(_developer_services(), arg)


def _digest_command(arg, workspace=""):
    return _render_digest_command(
        _developer_services(), arg, _developer_context(workspace),
    )


def _test_command(
    arg, workspace="", *, poll_seconds=1.0, progress_every=10.0,
    clock=time.monotonic, out=print,
):
    """``/test``: start a structured run and wait for its report.

    Ctrl+C while waiting cancels the job through the durable provider (its
    process tree is killed) instead of leaving it running unobserved.
    """
    services = _developer_services()
    context = _developer_context(workspace)
    words = str(arg or "").split()
    if words and words[0].lower() in _TEST_ACTIONS:
        if len(words) != 2:
            out(_TEST_USAGE)
            return
        out(_render_test_followup(services, words[0], words[1], context))
        return
    text, job_id = _start_test_command(services, arg, context, project=workspace or ".")
    out(text)
    if job_id is None:
        return
    _RECENT_TEST_JOBS.insert(0, job_id)
    del _RECENT_TEST_JOBS[8:]
    started = clock()
    last_progress = started
    try:
        while True:
            polled_at = clock()
            text, done = _poll_test_result(
                services, job_id, context, wait_seconds=poll_seconds,
            )
            if done:
                out(text)
                return
            now = clock()
            if now - last_progress >= progress_every:
                out("  ... %s still running (%ds); Ctrl+C cancels" % (
                    job_id, int(now - started),
                ))
                last_progress = now
            if now - polled_at < poll_seconds / 4:
                # A result call that returned early must not become a spin.
                time.sleep(min(0.25, poll_seconds))
    except KeyboardInterrupt:
        out(_render_test_followup(services, "cancel", job_id, context))


def _debug_services():
    """The composed crash/profile digest service (``Application.debug_tools``) or None."""
    try:
        return getattr(server._application(), "debug_tools", None)
    except Exception:
        return None


# Newest-first ids of the ``/test`` runs this console started; ``/crash fix``
# looks for a crashed test among their reports (it never runs one itself).
_RECENT_TEST_JOBS = []


def _recent_test_reports(context):
    services = _developer_services()
    runs = getattr(services, "test_runs", None) if services is not None else None
    if runs is None:
        return ()
    reports = []
    for job_id in list(_RECENT_TEST_JOBS)[:8]:
        try:
            value = runs.result(job_id, context, wait_seconds=0)
        except Exception:
            continue
        if hasattr(value, "failures") and hasattr(value, "runner"):
            reports.append(value)
    return tuple(reports)


def _crash_source_lookup(workspace=""):
    """Map debug-info paths into this checkout; read excerpts through file_ops."""
    from pathlib import Path

    try:
        from sonder_runtime.adapters.debugging.source_map import ProjectSourceMap
        from sonder_runtime.application.debugging.crash_fix import project_source_lookup
    except ImportError:
        return None
    root = Path(workspace) if workspace else file_ops.workspace_root()
    try:
        source_map = ProjectSourceMap((root,))
    except Exception:
        return None

    def resolve(path):
        try:
            return source_map.resolve(path)
        except Exception:
            return None

    def read_lines(local):
        # Only a regular file inside this checkout, reached without a link,
        # and small enough to read: the excerpt is shown to the model, and a
        # source map or report must not be able to point it anywhere else.
        try:
            base = root.resolve()
            candidate = base / local
            if candidate.resolve() != candidate or not candidate.resolve().is_relative_to(base):
                return None
            if not candidate.is_file() or candidate.stat().st_size > 1_000_000:
                return None
            text = file_ops.read_file(str(candidate), max_bytes=1_000_000)["text"]
        except Exception:
            return None
        return text.splitlines()

    return project_source_lookup(resolve, read_lines)


def _crash_command(arg, workspace=""):
    """``/crash``: digest, triage, symbols consent, fix brief, run follow-ups."""
    context = _developer_context(workspace)
    words = str(arg or "").split()
    fix = bool(words) and words[0].lower() == "fix"
    _render_crash_command(
        _debug_services(), arg, context, confirm=_confirm_answer,
        source_lookup=_crash_source_lookup(workspace) if fix else None,
        test_reports=(lambda: _recent_test_reports(context)) if fix else None,
    )


def _confirm_answer(prompt):
    """The operator's typed answer; piped or unattended input never says yes."""
    if not _console_has_operator():
        return ""
    return input(prompt)


def _profile_command(arg, workspace=""):
    _render_profile_command(_debug_services(), arg, _developer_context(workspace))


def _inventory_stale(snapshot):
    try:
        from sonder_runtime.domain.host_tools.model import is_stale

        return bool(is_stale(snapshot, now=time.time(), ttl_seconds=86_400))
    except Exception:
        created = getattr(snapshot, "created_at", 0) or 0
        return time.time() - float(created) >= 86_400


def _start_tool_inventory_warmup():
    """Refresh a missing or stale host tool snapshot off the input thread."""
    services = _developer_services()
    inventory = getattr(services, "inventory", None) if services is not None else None
    if inventory is None:
        return None
    try:
        cached = inventory.cached()
    except Exception:
        cached = None
    if cached is not None and not _inventory_stale(cached):
        return None

    def warm():
        try:
            inventory.snapshot()
        except Exception as exc:  # warm-up is best effort only
            import logging

            logging.getLogger(__name__).debug(
                "tool inventory warm-up failed: %s", type(exc).__name__,
            )

    thread = owned_runtime_thread(
        target=warm, daemon=True, name="sonder-tool-inventory-warm",
    )
    thread.start()
    return thread


def _lanes_command(arg):
    from sonder_runtime.interfaces.repl.facades.agent_lanes import LaneConsoleFacade
    return LaneConsoleFacade(lambda: server._application(), _approve_lane_command).run(
        arg, width=_cols(),
    )


def _fanout_recent_command(arg):
    text = (arg or "").strip().casefold()
    include_finished, limit = True, 20
    if text == "active":
        include_finished = False
    elif text:
        if not text.isdigit() or not 1 <= int(text) <= 100:
            return "usage: /fanouts [N|active]  (N is 1..100)"
        limit = int(text)
    try:
        payload = json.loads(server.model_fanout_recent(
            limit=limit, include_finished=include_finished, token=CURRENT_TOKEN,
        ))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "fanout history unavailable"
    return _format_fanout_summaries(payload)


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
        _emit("- %s" % lesson["text"])


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
        print(_RUN_USAGE)
        return None
    return grounding.clamp_timeout(value)


def _run_train(n):
    tasks = training_tasks.sample(n)
    passed = 0
    lessons = 0
    for t in tasks:
        print("  %s %s" % (_paint("PRACTICE", "teal", "bold"), t["name"]))
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
            print("    %s  %s" % (_result_tag(ok), S.safe_text(msg)))
        else:
            print("    %s  (no interaction id)" % _result_tag(ok))
    print(_paint("practice complete", "cyan", "bold") +
          "  %d tasks · %d passed · %d failed · %d new lessons" % (
        len(tasks), passed, len(tasks) - passed, lessons))


def _print_sessions():
    conn = server._open_db()
    try:
        sessions = memory_store.list_sessions(conn, 20)
    finally:
        conn.close()
    _emit(_format_sessions(sessions))


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
        _emit("  - %s  %s" % (f["id"], f["text"]))


def _recovery_command(session_id, project, argument):
    from sonder_runtime.interfaces.repl.facades.agent_lanes import terminal_text
    text = argument.strip()
    parts = text.split()
    request = None
    if parts and parts[0] == 'resume':
        if len(parts) != 3 or any(not 1 <= len(value) <= 128 or not all(
                char.isascii() and (char.isalnum() or char in '-_') for char in value)
                for value in parts[1:]):
            return 'Usage: /recover resume <continuation-id> <command-id>. Repeat the same command-id for a pending attempt.'
        request = tuple(parts[1:])
        cursor = None
    elif text and (not text.isascii() or not text.isdecimal() or len(text) > 19):
        return 'Usage: /recover [numeric cursor]. Inspection does not resume work.'
    else:
        cursor = int(text or '0')
    if cursor is not None and cursor >= 2**63:
        return 'Recovery cursor is outside its supported range.'
    if not project:
        return 'Select a workspace with /workspace before inspecting managed work.'
    connection = server._open_db()
    try:
        if memory_store.get_session(connection, session_id) is None:
            return 'No managed work for this conversation.'
        databases = connection.execute('PRAGMA database_list').fetchall()
        database = next((row[2] for row in databases if row[1] == 'main'), '')
        if not database:
            raise PermissionError('durable memory database identity unavailable')
    finally:
        connection.close()
    try:
        page = server._run_managed_repl_work(session_id, memory_database=database,
                                             project=project, _recovery_cursor=cursor,
                                             _recovery_request=request)
    except PermissionError:
        return 'Recovery unavailable: inspect the current authority and any unresolved prior outcome.'
    except ValueError:
        return 'Recovery request no longer matches its stored identity; inspect the run and reuse the original command-id.'
    if request is not None:
        lines = ['Recovery: ' + terminal_text(page.code)]
        if page.approval_call_id:
            lines.append('Pending approval: ' + terminal_text(page.approval_call_id))
            if page.code == 'ATTACHMENT_APPROVAL_PENDING':
                lines.append('Reattachment approval applies to this ownership attempt; a released attempt needs fresh approval.')
            lines.append('After approval, repeat the same recovery command and command-id.')
        if page.output:
            lines.extend(('Original terminal output:', terminal_text(page.output, limit=1048576)))
        return '\n'.join(lines)
    lines = ['Managed work — inspection only']
    for item in page.items:
        lines.append('%s | owner: %s | authority: %s | verification: %s' % (
            terminal_text(item.continuation_id), terminal_text(item.owner_state),
            terminal_text(item.authority_state), terminal_text(item.verification_phase or 'none')))
        if item.verification_code:
            lines.append('  ' + terminal_text(item.verification_code))
        if item.pending_approval is not None:
            lines.append('  pending approval: ' + terminal_text(item.pending_approval.call_id))
    if not page.items:
        lines.append('No managed work on this page.')
    if page.has_more:
        lines.append('Next page: /recover %s' % page.next_cursor)
    lines.append('No work was resumed and no approval was consumed.')
    return '\n'.join(lines)


def _recovery_posture_command():
    """Render configured recovery limits without touching ownership state."""
    from sonder_runtime.adapters.web import lifecycle as runtime_lifecycle

    return RecoveryPostureFacade(
        lambda: runtime_lifecycle.get().deployment_payload()
    ).format()


def _run_session_work(session_id, *, host_project, **arguments):
    """Persist the exact REPL-selected conversation before standalone work.

    This private wrapper is not a public session argument or attachment grant.
    The managed host-selection adapter must still authorize this persisted row.
    """
    conn = server._open_db()
    try:
        memory_store.touch_session(conn, session_id, project=host_project)
        row = memory_store.get_session(conn, session_id)
        if row is None or row['session_id'] != session_id:
            raise PermissionError('selected host conversation unavailable')
        databases = conn.execute('PRAGMA database_list').fetchall()
        memory_database = next((entry[2] for entry in databases if entry[1] == 'main'), '')
        if not memory_database:
            raise PermissionError('durable memory database identity unavailable')
    finally:
        conn.close()
    return server._run_managed_repl_work(session_id, memory_database=memory_database, **arguments)


def _model_listing(tier, active_model, tiers, installed, width):
    """``/model`` with no argument (spec 3, /model mockup).

    The active tier is marked ``(active)`` in the accent colour, withheld
    tiers share one footnote per reason instead of repeating it per row, and
    installed models sit in two columns when the terminal is wide enough.
    """
    sep = " %s " % S.g("sep")
    current = str(active_model or server.TIERS.get(tier) or "?")
    active = _paint("(active)", "accent")
    lines = ["%s  %s%stier %s %s" % (
        _paint("model", "muted"), _paint(S.safe_text(current), "info", "strong"),
        sep, S.safe_text(tier), active)]
    lines.append(_paint("tiers", "muted"))
    for name in sorted(tiers):
        row = "  %-14s %s" % (S.safe_text(name), S.safe_text(tiers[name]))
        lines.append(row + ("  " + active if name == tier and not active_model else ""))
    # A withheld tier is named rather than silently omitted: the user
    # configured it, and "where did cloud-code go" is a worse answer than
    # one line saying what would turn it back on.
    reasons = {}
    for name in sorted(set(server.TIERS).difference(tiers)):
        reason = _unselectable_tier_reason(name) or "unavailable"
        if name in getattr(server, "CLOUD_TIERS", ()) and "SONDER_ALLOW_CLOUD" in reason:
            # The listing is a footnote, not the refusal: the full sentence
            # is what ``/model cloud-code`` (and the turn) still answers.
            reason = "cloud off %s SONDER_ALLOW_CLOUD=1 to opt in (prompts then leave this machine)" % S.g("sep")
        reasons.setdefault(reason, []).append(name)
    configured = {str(name).casefold() for name in server.TIERS}
    for name, env_name in OPTIONAL_LOCAL_TIERS:
        if name not in configured:
            reasons.setdefault("no model set %s %s" % (S.g("sep"), env_name), []).append(name)
    if reasons:
        lines.append(_paint("  unavailable", "muted"))
        name_w = max(len(", ".join(v)) for v in reasons.values())
        side_by_side = 4 + name_w + 2 <= (width - 1) // 2
        for reason, names in reasons.items():
            reason = S.safe_text(" ".join(reason.split()))
            if side_by_side:
                head = "    %-*s  " % (name_w, ", ".join(names))
                wrapped = S.wrap(reason, width - 1, indent=head, hanging=" " * len(head))
            else:
                wrapped = S.wrap(", ".join(names), width - 1, indent="    ",
                                 hanging="    ")
                wrapped += S.wrap(reason, width - 1, indent="      ", hanging="      ")
            lines.extend(_paint(line, "muted") for line in wrapped)
    if installed is None:
        lines.append(_paint("installed models: (ollama did not answer)", "warning"))
    elif not installed:
        lines.append(_paint("installed models (ollama): (none installed)", "warning"))
    else:
        lines.append(_paint("installed (ollama)", "muted"))
        cells = []
        for name, size in installed:
            mark = "  " + active if name == current and active_model else ""
            cells.append((S.safe_text(name), S.safe_text(size), mark))
        name_w = max(S.cell_width(c[0]) for c in cells)
        size_w = max(S.cell_width(c[1]) for c in cells)
        rendered = ["%s  %s%s" % (c[0].ljust(name_w), c[1].rjust(size_w), c[2]) for c in cells]
        col_w = max(S.cell_width(r) for r in rendered)
        columns = 2 if 2 + col_w * 2 + 4 <= width - 1 else 1
        rows = (len(rendered) + columns - 1) // columns
        for r in range(rows):
            parts = [rendered[r + k * rows] for k in range(columns) if r + k * rows < len(rendered)]
            line = "  " + "    ".join(
                p + " " * (col_w - S.cell_width(p)) if k < len(parts) - 1 else p
                for k, p in enumerate(parts))
            lines.append(S.truncate(line.rstrip(), width - 1))
    lines.append(_paint("/model <tier>%s/model <name>" % sep, "muted"))
    return lines


def _help_screen(width, mode="manual"):
    """Compact ``/help`` (spec 2.9): legend first, core commands, groups.

    Fits 80x24 without scrolling; the catalog supplies rows and the layout
    is fitted here with the session's glyphs.
    """
    text = command_catalog.format_help(
        width=width, mode=mode, sep=" %s " % S.g("sep"), ellipsis=S.g("ellipsis"),
    )
    lines = text.split("\n")
    if not lines:
        return text
    out = [_paint(lines[0], "accent", "strong")]
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("in ") or stripped.startswith("[") and "] " in stripped:
            out.append(_paint(line, "muted"))
        elif line.startswith("  groups"):
            out.append(_paint(line, "muted"))
        else:
            out.append(re.sub(r"(\[(?:asks|writes|runs|danger)\])$",
                              lambda m: _paint(m.group(1), "danger" if m.group(1) == "[danger]"
                                               else "warning"), line))
    return "\n".join(out)


_HELP_STATUS = (
    ("tier", "the model tier turns go to (/model <tier>)"),
    ("model", "the model that tier is bound to right now"),
    ("mode", "how much runs without asking: plan, manual, acceptEdits, auto"
             " (/mode); ELEVATED means an override is active"),
    ("ctx", "context used / the model's window, in tokens (/context)"),
    ("agents", "agents or lanes running now; shown only when non-zero (/agents)"),
    ("proj", "the active project, when it is not 'default' (/project)"),
)


def _help_status_text(width):
    lines = [_paint("the status line above the prompt", "accent", "strong"),
             _paint("  code %s sonder:latest %s manual %s ctx 64/8.2k" % (
                 (S.g("sep"),) * 3), "muted")]
    for name, text in _HELP_STATUS:
        prefix = "  %-7s " % name
        lines.extend(S.wrap(text, width - 1, indent=prefix, hanging=" " * len(prefix)))
    lines.append(_paint("  fields leave from the right on narrow terminals; the mode"
                        " never does. per-turn numbers are in each answer's footer.",
                        "muted"))
    return "\n".join(lines)


_LOGS_DEFAULT = 20
_LOGS_MAX = 500


def _logs_command(arg):
    """``/logs [n]``: the last n records of SONDER_HOME/logs/repl.log.

    Read-only.  Each record is re-sanitized: the file stores C1 and bidi
    characters raw, and a log line must not be able to drive the terminal.
    """
    text = str(arg or "").strip()
    if text and (not text.isdigit() or not 1 <= int(text) <= _LOGS_MAX):
        return "usage: /logs [n]  (n is 1..%d, default %d)" % (_LOGS_MAX, _LOGS_DEFAULT)
    count = int(text) if text else _LOGS_DEFAULT
    try:
        path = repl_notices.repl_log_path()
    except Exception:
        path = None
    if not path:
        return "logs are not being saved to a file in this session"
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            data = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        return "cannot read the log %s: %s" % (path, exc.strerror or type(exc).__name__)
    rows = [line for line in data.splitlines() if line.strip()][-count:]
    if not rows:
        return "no log records yet (%s)" % path
    out = []
    for raw in rows:
        try:
            record = json.loads(raw)
        except ValueError:
            out.append(S.safe_text(raw))
            continue
        stamp = str(record.get("timestamp") or "")[11:19]
        out.append(S.safe_text("%s %-7s %s: %s" % (
            stamp, record.get("severity") or record.get("level") or "",
            record.get("component") or record.get("logger") or "",
            " ".join(str(record.get("message") or "").split()),
        )))
    out.append(_paint("  %s" % S.safe_text(path), "muted"))
    return "\n".join(out)


def _drain_notices():
    """Show queued log records between turns (spec 2.11).

    Never called while the live line is drawn.  ERROR records get a full
    notice each; everything else is one muted ``! N notices · /logs`` line.
    """
    try:
        notices = repl_notices.drain_repl_notices()
        dropped = repl_notices.last_drain_dropped()
    except Exception:
        return
    if not notices and not dropped:
        return
    width = _cols()
    quiet = 0
    for item in notices:
        if getattr(item, "is_error", False):
            print(S.notice("error", item.message, detail=item.component or None,
                           hint="/logs shows the full record", width=width))
        else:
            quiet += 1
    quiet += int(dropped or 0)
    if quiet:
        print("  " + _paint(S.g("warn"), "warning", "strong") + " " + _paint(
            "%d notice%s%s/logs" % (quiet, "" if quiet == 1 else "s", " %s " % S.g("sep")),
            "muted"))


def _refusal_notice(line, refusal):
    """Render a gate refusal/skip as a notice; plain text off a terminal."""
    text = str(refusal or "")
    if not _stdout_is_interactive():
        return S.safe_text(text)
    command = " ".join(str(line or "").split())
    if text.startswith("skipped "):
        diverted = _DIVERTED_ANSWER
        detail = None
        if diverted and _history_safe(diverted):
            detail = "your %s was not run; press %s to recall it" % (
                diverted, S.g("up"))
        elif diverted:
            # A credential line never enters history, so there is nothing to
            # recall, and the notice names only the command word.
            detail = "your %s was not run" % diverted.split(None, 1)[0]
        return S.notice("skipped", command, detail, width=_cols())
    if text.startswith("refused "):
        _cmd, _sep, reason = text[len("refused "):].partition(": ")
        return S.notice("refused", command, reason or None,
                        hint=_error_hint(text) or None, width=_cols())
    return S.safe_text(text)


def _with_conversation_lifetime(function):
    from functools import wraps

    @wraps(function)
    def invoke(*args, **kwargs):
        with server._managed_repl_conversation_scope():
            return function(*args, **kwargs)

    return invoke


@_with_conversation_lifetime
def main(*, machine_output=False):
    global CURRENT_TOKEN
    trace = False
    strict = None  # None = env default
    location_consent = None  # None = env default (SONDER_LOCATION_CONSENT)
    persona = personas.DEFAULT
    last_iid = None
    last_response = None
    last_run_source = None
    last_turn_metrics = None
    # A fresh conversation thread per REPL launch; /new rerolls it, /resume switches it.
    session_id = memory_store.new_id()
    project = server.DEFAULT_PROJECT
    # ``project`` is a memory/checklist namespace.  Keep the actual filesystem
    # workspace separate so a conversational work request never falls back to
    # Sonder's own checkout merely because the namespace is the default label.
    workspace_root = ""
    pending_workspace_work = ""
    queued_workspace_work = ""
    # None = whatever the runtime resolves by default; /model pins one.
    active_tier = None
    # Exact discovered model selected by /model <tag>.  Keep it separately
    # from TIERS: live config reload intentionally rebuilds that process-wide
    # mapping, but it must not silently undo this REPL session's choice.
    active_model = None
    # Up-arrow recall.  An attended terminal session persists it to
    # SONDER_HOME/repl_history (0600, newest 200 lines; P2-5) -- the same
    # home already holds the conversation turns themselves.  Credential lines
    # never enter it (``_history_safe``), piped scripts never write it, and
    # SONDER_REPL_HISTORY=0 keeps it process-local.
    persist_history = (
        not machine_output and _console_has_operator() and _stdout_is_interactive()
        and str(os.environ.get("SONDER_REPL_HISTORY", "1")).strip() != "0"
    )
    input_history = _load_history() if persist_history else []

    def remember(line):
        if not _history_safe(line) or (input_history and input_history[-1] == line):
            return
        input_history.append(line)
        if len(input_history) > REPL_HISTORY_LIMIT:
            del input_history[:-REPL_HISTORY_LIMIT]
        if _READLINE is not None and "\n" not in line:
            try:
                _READLINE.add_history(line)
            except Exception:
                pass
        if persist_history:
            _save_history(input_history)

    global _HISTORY_SINK
    _HISTORY_SINK = remember
    # The most recent plain-language turn, kept so a bare /why can answer
    # "why did (or didn't) that route" about the thing just typed. Session
    # state only; never persisted.
    last_natural_turn = ""
    model_argument_completer = _ModelArgumentCompleter()

    def _workspace_create_path(raw):
        """Return one canonical path suitable for guarded directory creation."""
        text = str(raw or "").strip().strip('"')
        if not text:
            return "", "workspace path is required"
        try:
            path = os.path.realpath(os.path.abspath(os.path.expanduser(text)))
        except (OSError, ValueError) as exc:
            return "", "invalid workspace path: %s" % exc
        return path, ""

    def _workspace_path(raw):
        """Return one canonical existing directory, never a bare project label."""
        path, error = _workspace_create_path(raw)
        if error:
            return "", error
        if not os.path.isdir(path):
            return "", "workspace directory does not exist: %s" % path
        return path, ""


    
    def _looks_like_slash_command(raw):
        """True for `/workspace`-style commands, false for multi-segment absolute paths.

        Interactive lines that start with `/` used to always enter the command
        router. On POSIX that swallowed work requests that already named an
        absolute folder (`/tmp/project/Games create a game`), so require the first
        token to be a single path segment after the leading slash.
        """
        text = str(raw or "").strip()
        if not text.startswith("/"):
            return False
        first = text.split(None, 1)[0]
        # `/workspace`, `/help` -> one slash; `/tmp/foo` -> two+.
        return first.count("/") == 1

    def _split_existing_workspace_prefix(raw):
        """Peel the longest existing directory prefix off a work request.

        Paths may contain spaces (``D:\\Sonder Games work on the FPS game``),
        so this walks token prefixes instead of splitting on the first space.
        Returns ``(path, remainder)`` or ``("", original)`` when none exists.
        """
        text = str(raw or "").strip().strip('"')
        if not text:
            return "", ""
        if not re.match(r"^(?:[A-Za-z]:[\\/]|~[\\/]|[.]{1,2}[\\/]|[\\/])", text):
            return "", text
        parts = text.split()
        for count in range(len(parts), 0, -1):
            trial = " ".join(parts[:count])
            path, error = _workspace_path(trial)
            if error:
                continue
            remainder = " ".join(parts[count:]).strip()
            return path, remainder
        return "", text

    def _queue_pending_workspace_work():
        nonlocal pending_workspace_work, queued_workspace_work
        if pending_workspace_work and workspace_root:
            queued_workspace_work = pending_workspace_work
            pending_workspace_work = ""
            print("workspace selected; resuming requested work in %s" % workspace_root)

    def do_workspace_select(raw):
        nonlocal workspace_root
        text = str(raw or "").strip()
        if not text:
            print("workspace: %s" % (workspace_root or "(not selected)"))
            return
        if text.lower() in ("none", "clear", "off"):
            server._clear_managed_repl_conversation()
            workspace_root = ""
            print("workspace cleared; the next work request will ask for a directory")
            return
        path, error = _workspace_path(text)
        if error:
            _emit(error + "\nuse /workspace-create <path> to create a new guarded directory")
            return
        server._clear_managed_repl_conversation()
        workspace_root = path
        print("workspace: %s" % workspace_root)
        _queue_pending_workspace_work()

    def do_workspace_create(raw):
        nonlocal workspace_root
        path, error = _workspace_create_path(raw)
        if error:
            print("usage: /workspace-create <path>")
            return
        output = server.directory_create(path=path, parents=True)
        _emit(output)
        if _is_repl_error(output):
            return
        path, error = _workspace_path(path)
        if error:
            _emit(error)
            return
        server._clear_managed_repl_conversation()
        workspace_root = path
        print("workspace: %s" % workspace_root)
        _queue_pending_workspace_work()

    def _workspace_reply_command(raw):
        """Accept only an explicit path-like answer to a pending workspace ask."""
        text = str(raw or "").strip().strip('"')
        create = False
        match = re.match(r"^create(?:\s+(?:it\s+)?(?:at|in))?\s+(.+)$", text, re.I)
        if match:
            create = True
            text = match.group(1).strip().strip('"')
        if not re.match(r"^(?:[A-Za-z]:[\\/]|~[\\/]|[.]{1,2}[\\/]|[\\/]{1,2})", text):
            return ""
        if not create:
            path, remainder = _split_existing_workspace_prefix(text)
            if path:
                if remainder and not pending_workspace_work:
                    # Path+task in one reply: keep the task as the pending work.
                    # Pending is assigned by the caller after interpret; stash on
                    # the reply marker instead by returning workspace only when
                    # the ask already captured the task.
                    pass
                return "/workspace " + path
        return ("/workspace-create " if create else "/workspace ") + text

    def run_workspace_work(task):
        nonlocal last_iid, last_response, last_run_source, last_turn_metrics
        # "use N workers ..." that the worker-count cue refused (a "why", a
        # negation, a quote) used to run one foreground lane with no hint.
        try:
            ignored_cue = server.master_orchestrator.worker_request_ignored_reason(task)
        except Exception:
            ignored_cue = ""
        if ignored_cue:
            print(_paint("(note: %s)" % ignored_cue, "muted"))
        started_at = time.monotonic()
        _TURN_MODEL[0] = current_model()
        indicator = _begin_chat_turn("Sonder work")
        try:
            out = _run_session_work(session_id, host_project=project,
                prompt=task, tier=active_model or active_tier or "auto",
                max_steps=12, project=workspace_root,
            )
        except BaseException:
            if indicator is not None:
                indicator.stop()
            raise
        last_iid = None
        last_response = out
        last_run_source = _answer_only(out)
        last_turn_metrics = _latest_repl_turn_metrics(surfaces=("agent",))
        _print_chat_result(out, started_at, label="Sonder work", indicator=indicator,
                           metrics=last_turn_metrics)

    def current_model():
        try:
            return ModelSelectionFacade.resolved_model(
                server.TIERS, active_tier or "code", active_model,
            )
        except Exception:
            return ""

    def workspace_file_command(raw_path, call):
        """Run one /read /write /append /edit /mkdir /delete in the workspace."""
        path, error = _workspace_scoped_path(workspace_root, raw_path)
        if error:
            _emit(error)
            return
        with _workspace_file_scope(workspace_root):
            _emit(call(path))

    def announce_interrupted_turn():
        nonlocal last_iid, last_response, last_run_source, last_turn_metrics
        # Nothing from the cancelled turn may be rated, re-run, or reported as
        # the latest answer; clear the per-turn handles instead of leaving the
        # previous turn's attached to what the operator abandoned.
        last_iid = None
        last_response = None
        last_run_source = None
        last_turn_metrics = None
        print()
        if _stdout_is_interactive():
            print(S.notice("skipped", "turn cancelled",
                           hint="Ctrl-C again at the prompt, Ctrl-D or /exit quits",
                           width=_cols()))
        else:
            print(
                "interrupted: turn cancelled. Press Ctrl-C again at the prompt"
                " (or Ctrl-D, or /exit) to quit."
            )

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
        wanted = arg.lower()
        available = personas.names()
        if wanted not in available:
            # ``personas.get`` falls back to the default for an unknown name,
            # so accepting it here would report a persona no turn will use.
            print("unknown persona %r; still %s (available: %s)" % (
                arg, persona, ", ".join(available)))
            return
        persona = wanted
        print("persona: %s" % persona)

    def do_model(arg):
        """Show what is installed and switch the model for the rest of the session.

        A named tier selects its live binding. An exact installed tag pins this
        REPL session to that discovered model without mutating the process-wide
        tier table, which live reload is allowed to rebuild.
        """
        nonlocal active_tier, active_model
        arg = (arg or "").strip()
        tier = active_tier or "code"
        tiers = _selectable_tiers()
        installed = _installed_models()
        model_argument_completer.refresh(installed)

        if not arg:
            for line in _model_listing(tier, active_model, tiers, installed, _cols()):
                print(line)
            return

        tier_names = {str(name).casefold(): name for name in tiers}
        selected_tier = tier_names.get(arg.casefold())
        if selected_tier is not None:
            active_tier = selected_tier
            active_model = None
            print("active tier: %s  ->  %s" % (
                selected_tier, tiers.get(selected_tier)))
            return

        # A configured-but-withheld tier is a near miss, not an unknown word.
        # Refuse it here, with the reason, instead of letting it fall through
        # to "no installed model named 'cloud-code'" -- or, before the tier
        # vocabulary was filtered, into a pin whose failure only showed up on
        # the next chat turn.
        withheld = {str(name).casefold(): name for name in server.TIERS}.get(
            arg.casefold())
        withheld_reason = _unselectable_tier_reason(withheld)
        if withheld_reason:
            print(_paint("cannot select tier %r: %s" % (
                withheld, withheld_reason), "red"))
            return

        # An optional local tier with no model bound is absent from the tier
        # table.  Say that, instead of "no installed model named 'vision'".
        configured = {str(name).casefold() for name in server.TIERS}
        unbound = dict(OPTIONAL_LOCAL_TIERS).get(arg.casefold())
        if unbound and arg.casefold() not in configured:
            print(_paint(
                "tier %r has no model configured; set %s to an installed"
                " model and restart Sonder to enable it" % (arg.casefold(), unbound),
                "red",
            ))
            return

        if installed is None:
            print(_paint(
                "cannot verify installed models because Ollama did not answer; model selection was not changed",
                "red",
            ))
            return

        names = [name for name, _size in installed]
        model_names = {str(name).casefold(): name for name in names}
        selected_model = model_names.get(arg.casefold())
        if selected_model is None and ":" not in arg:
            # Ollama treats a bare name as its ":latest" tag.
            selected_model = model_names.get(arg.casefold() + ":latest")
        if selected_model is None:
            # Refuse rather than rebind to something that will fail on the next
            # turn with an opaque ollama error. Suggest, because a near miss is
            # usually a tag typo (":7b" vs ":latest"). Match the command's own
            # case-insensitive resolution, and never suggest from an empty base
            # (an arg like ":latest"), which would match every installed tag.
            # Never suggest a model the selection below would refuse (an
            # embedding model cannot serve chat).
            base = arg.split(":")[0].casefold()
            near = [
                name for name in names
                if base and base in name.casefold()
                and not _model_selection_ineligibility(name)
            ]
            print(_paint("no installed model named %r" % arg, "red"))
            if near:
                print("did you mean: %s" % ", ".join(near[:5]))
            else:
                print("run /model with no argument to list what is installed")
            return

        if selected_model is not None:
            ineligible = _model_selection_ineligibility(selected_model)
            if ineligible:
                print(_paint(
                    "model %r cannot serve chat (%s)" % (
                        selected_model, ineligible,
                    ),
                    "red",
                ))
                print("choose a chat-capable model shown by /model")
                return

        active_tier = tier
        # Preserve the catalog's spelling for the actual request.  Ollama's
        # model tags are conventionally lowercase, but accepting a pasted or
        # manually capitalized selector should not turn a valid discovery into
        # an avoidable next-turn pin refusal.
        active_model = selected_model or arg
        print("%s session model -> %s" % (
            tier, _paint(active_model, "cyan", "bold")))

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
        _emit(code_runner.format_result(result))
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
        _emit(code_runner.format_window_result(result))
        print("[launched]" if result.get("ok") else "[launch failed]")

    def do_runproject(timeout=grounding.MAX_TIMEOUT):
        files = grounding.extract_project_files(last_run_source or last_response)
        if not files:
            print("(no file/path fenced project blocks in the last response)")
            return
        result = code_runner.run_project({"files": files}, timeout=timeout)
        _emit(code_runner.format_project_result(result))
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
        # Two local models plus a cloud model when cloud is enabled. An exact
        # session pin substitutes for its logical tier, so it is genuinely
        # consulted and used as the judge instead of the old tier binding.
        tiers = consult_flow.default_tiers()
        if active_model:
            if active_tier in tiers:
                tiers = [active_model if item == active_tier else item for item in tiers]
            else:
                tiers = [active_model] + tiers
        elif active_tier and active_tier not in tiers:
            tiers = [active_tier] + tiers
        result = consult_flow.consult(question, tiers)
        for answer in result["answers"]:
            _emit("\n=== %s ===\n%s" % (answer["tier"], answer["text"]))
        verdict = consult_flow.verdict_line(result)
        colour = "green" if result["agree"] is True else "amber"
        print("\n" + _paint(verdict, colour, "bold"))

    def do_route(question):
        question = (question or "").strip()
        if not question:
            print("usage: /route <request>")
            return
        # `tier_router.route` falls back precisely so it never names a tier the
        # next call would fail on. Handing it the raw table defeated that: with
        # cloud opt-in off, a recall question was routed to `cloud-general`,
        # which answers only "hosted/cloud tiers are disabled".
        decision = tier_router.route(
            question, available_tiers=set(_selectable_tiers()))
        print("kind:   %s" % _paint(decision["kind"], "cyan"))
        print("tier:   %s" % _paint(decision["tier"], "cyan", "bold"))
        print("reason: %s" % _paint(decision["reason"], "muted"))

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
            _emit("could not read %s: %s" % (fpath, exc))
            return
        chosen = active_tier or tier_router.route(
            objective or "improve %s" % fname,
            available_tiers=set(_selectable_tiers()))["tier"]
        print(_paint("asking %s to improve %s ..." % (chosen, fname), "muted"))
        res = code_improve.improve_function(
            src, fname,
            lambda p, t: server.ensemble_answer(p, tiers=t, mode="code"),
            tier=chosen, objective=objective)
        if not res["ok"]:
            print(_paint("no change: %s" % res["reason"], "amber"))
            return
        _emit(res["diff"] or "(no diff)")
        if _confirm(_paint("apply this change?", "amber")):
            server.file_ops.write_file(fpath, res["edited"], mode="overwrite")
            print(_paint("applied to %s" % fpath, "green"))
        else:
            print(_paint("discarded", "muted"))

    def do_replay(raw):
        """Re-render a stored thread read-only; never switches the session.

        ``/replay`` shows what a thread contains, ``/resume`` continues it --
        keeping those separate means looking at history can never move where
        the next typed turn lands.
        """
        text = (raw or "").strip()
        count = _REPLAY_DEFAULT_TURNS
        bits = text.split()
        if bits and bits[-1].isdigit():
            count = int(bits[-1])
            bits = bits[:-1]
        target = " ".join(bits)
        replay_session = session_id
        if target:
            conn = server._open_db()
            try:
                found = memory_store.find_session(conn, target)
            finally:
                conn.close()
            if not found:
                print("no session matching '%s'  (/sessions lists them)" % target)
                return
            replay_session = found
        conn = server._open_db()
        try:
            turns = memory_store.session_turns(conn, replay_session)
        finally:
            conn.close()
        _emit(_format_session_replay(
            turns, session_id=replay_session, limit=count,
        ))

    if not machine_output:
        _init_terminal()
        banner = _startup_banner(strict, persona, project, active_tier)
        if _stdout_is_interactive():
            if banner:
                print(banner)
            # The banner already counted the startup notices ("! N startup
            # notices · /logs"); they are in the log, not repeated here.
            try:
                repl_notices.drain_repl_notices()
            except Exception:
                pass
            _start_tool_inventory_warmup()
        elif banner:
            # Off a terminal the banner never mixes into stdout (P0-3): one
            # identity line on stderr, and stdout stays script output only.
            try:
                print(str(banner).split("\n", 1)[0], file=sys.stderr)
            except (OSError, ValueError):
                pass
        _setup_readline(input_history)

    # ``SONDER_REPL_NDJSON=1`` on a pipe: every stdout line is JSON, not only
    # the turn lines, so command output cannot break a line-oriented parser.
    ndjson_writer = None
    if (not machine_output and _machine_output.enabled(os.environ)
            and not _stdout_is_interactive()):
        ndjson_writer = _NdjsonCommandWriter(sys.stdout)
        previous_stdout, sys.stdout = sys.stdout, ndjson_writer

    last_status = None
    last_interrupt = [0.0]

    while True:
        # ``/workspace`` may select/create a directory in response to a prior
        # natural work request.  Run that original request on the next loop
        # turn so the selection command itself remains separately permission
        # gated and does not accidentally run the agent in the old cwd.
        if queued_workspace_work:
            task = queued_workspace_work
            queued_workspace_work = ""
            try:
                with _interruptible_turn():
                    run_workspace_work(task)
            except KeyboardInterrupt:
                announce_interrupted_turn()
            continue
        attended = (not machine_output and _stdout_is_interactive()
                    and _console_has_operator())
        try:
            prompt = ""
            if attended:
                _drain_notices()
                status_kwargs = dict(
                    context=_composer_context(session_id, project),
                    model_override=active_model,
                    permission=_permission_mode_snapshot(),
                    project=project,
                )
                if _composer_available():
                    # The raw composer draws the status line as its frame.
                    prompt = _status_text(active_tier, width=_composer_frame_width(),
                                          **status_kwargs)
                else:
                    status = _status_text(active_tier, **status_kwargs)
                    # Plain mode prints the status line only when it changes.
                    if not S.caps().plain or status != last_status:
                        print()
                        print(status)
                    last_status = status
                    prompt = _readline_prompt(_prompt_glyph())
            line = _read_input(
                prompt,
                history=input_history,
                composer=attended,
                argument_completer=model_argument_completer,
                refresh_frame=(lambda: _status_text(
                    active_tier, width=_composer_frame_width(),
                    context=_composer_context(session_id, project),
                    model_override=active_model,
                    permission=_permission_mode_snapshot(), project=project,
                )) if attended else None,
            )
        except EOFError:
            # Only a terminal needs the newline after the prompt; on a pipe
            # (plain or NDJSON) it would be a stray trailing empty line.
            if attended:
                print()
            break
        except KeyboardInterrupt:
            # Ctrl-C at the idle prompt: the first press clears the line, a
            # second one within 2 s quits (P2-4).  Ctrl-D quits at once.
            now = time.monotonic()
            if not attended or now - last_interrupt[0] <= 2.0:
                if not machine_output:
                    print()
                break
            last_interrupt[0] = now
            print()
            print(_paint("(Ctrl-C again or /exit to quit)", "muted"))
            continue

        # PowerShell 5.1 may prefix the first piped UTF-8 line with a BOM. Treat
        # it as transport framing so slash commands remain commands.
        try:
            with _interruptible_turn():
                line = _normalize_input_line(line)
                if not line:
                    continue
                if pending_workspace_work and not line.startswith("/"):
                    workspace_reply = _workspace_reply_command(line)
                    if workspace_reply:
                        print(_paint("(interpreted as: %s)" % workspace_reply, "muted"))
                        line = workspace_reply
                remember(line)
                _maybe_live_reload()

                # Natural-language command resolution: "show me your stats" -> /stats,
                # "which model should handle X" -> /route X, "read file foo.py" ->
                # /read foo.py. The resolved slash line flows into the ordinary
                # dispatch below, so every command has exactly one implementation and
                # the slash form stays the precise way to invoke it. Unmatched turns
                # fall through untouched to feedback/intent/work/chat handling.
                if not line.startswith("/"):
                    last_natural_turn = line
                    resolved = command_router.resolve(line)
                    if resolved:
                        print(_paint("(interpreted as: %s)" % S.safe_text(resolved), "muted"))
                        line = resolved

                _CURRENT_LINE[0] = ""
                if _looks_like_slash_command(line):
                    _CURRENT_LINE[0] = " ".join(line.split())
                    parts = line.split(None, 1)
                    cmd = parts[0].lower()
                    arg = parts[1] if len(parts) > 1 else ""

                    # One choke point for every hand-written branch below, including
                    # the ones forwarded to server.control_command. Commands handled by
                    # _run_catalogued (the `else`) are gated there instead.
                    # A line the branch can only answer with its usage text reaches
                    # no tool, so answer it before the gate: nobody should be asked to
                    # approve (or be refused) a "dangerous" command that would only
                    # have printed how to use it.
                    usage = _branch_usage_error(cmd, arg)
                    if usage:
                        print(usage)
                        continue
                    # Rating commands record an outcome for the answer just
                    # shown; the plain-language form ("that worked") already
                    # does so ungated.  With nothing to rate there is nothing
                    # to approve, so say so before any prompt (P1-9).
                    rating = _rating_precheck(cmd, last_iid)
                    if rating == "nothing":
                        print("(nothing to rate yet)")
                        continue
                    may_run, refusal = (
                        (True, "") if rating == "rate" else _named_command_gate(cmd, arg)
                    )
                    if not may_run:
                        print(_refusal_notice(line, refusal))
                        continue

                    if cmd == "/":
                        # A bare slash is the "what can I type" gesture.
                        _emit(command_catalog.format_matches(""))
                    elif cmd == "/help":
                        topic = arg.strip()
                        if not topic and not machine_output and _stdout_is_interactive():
                            mode = _mode_fields(_permission_mode_snapshot())[0]
                            print(_help_screen(_cols(), mode if mode != "unknown" else "manual"))
                        elif topic.lower() == "status":
                            print(_help_status_text(_cols()))
                        elif topic.lower() == "all":
                            _emit(command_catalog.help_text(""))
                        else:
                            _emit(command_catalog.help_text(topic) + _help_policy_note(arg))
                    elif cmd == "/about":
                        state = _banner_state(
                            strict, persona, project, active_tier,
                            session_id=session_id, model_override=active_model,
                        )
                        print("\n".join(S.about_lines(state, _cols())))
                    elif cmd == "/logs":
                        _emit(_logs_command(arg))
                    elif cmd == "/status":
                        if arg.strip().lower() == "pool":
                            _emit(server.status())
                        elif arg.strip():
                            print("usage: /status [pool]")
                        else:
                            print(_status_long(_status_state(
                                active_tier, context=_composer_context(session_id, project),
                                model_override=active_model,
                                permission=_permission_mode_snapshot(), project=project,
                            ), _cols()))
                    elif cmd == "/why":
                        # A diagnostic read over the resolver's own trace: which stage
                        # claimed (or refused) a plain-language turn, and on what
                        # evidence. Never dispatches anything.
                        target = arg.strip() or last_natural_turn
                        if not target:
                            _emit(
                                "usage: /why [text]  explain how a plain-language turn"
                                " routes; the bare form uses your previous non-slash"
                                " turn"
                            )
                        else:
                            _emit(_format_route_explanation(
                                command_router.explain(target)
                            ))
                    elif cmd == "/version":
                        # Display only: the version literal plus the release stamp
                        # when the install has one. Deliberately no git probe here --
                        # starting a process would break the display-only claim the
                        # permission-gate coverage floor checks this branch against.
                        from sonder_runtime.platform import version as build_identity

                        stamped = build_identity.stamped_build_info()
                        if stamped is not None:
                            _emit("sonder %s (commit %s, stamped release)" % (
                                stamped.version, stamped.commit_sha[:12],
                            ))
                        else:
                            _emit("sonder %s (source checkout)"
                                  % build_identity.VERSION)
                    elif cmd == "/clear":
                        _clear_terminal_scrollback()
                    elif cmd == "/trace":
                        apply_trace(_on_off(arg, trace))
                    elif cmd == "/strict":
                        apply_strict(_on_off(arg, strict))
                    elif cmd == "/persona":
                        do_persona(arg)
                    elif cmd == "/model":
                        do_model(arg)
                    elif cmd == "/cloud":
                        _emit(server.cloud_opt_in(arg.strip() or "status"))
                    elif cmd == "/consult":
                        do_consult(arg)
                    elif cmd == "/route":
                        do_route(arg)
                    elif cmd == "/refactor":
                        do_refactor(arg)
                    elif cmd in ("/env", "/environment"):
                        _emit(server.environment_status(
                            refresh=(arg or "").strip().lower() == "refresh"))
                    elif cmd in ("/toolstatus", "/toolversion"):
                        name = (arg or "").strip()
                        if not name:
                            _emit("usage: /toolstatus <discovered-tool-name>  (try /env first)")
                        else:
                            _emit(server.toolchain_status(name=name))
                    elif cmd == "/scaffold":
                        parts = arg.split()
                        if len(parts) < 2:
                            _emit("usage: /scaffold <kind> <name> [root]   kinds: %s"
                                  % ", ".join(project_scaffold.kinds()))
                        else:
                            kind, name = parts[0], parts[1]
                            root = parts[2] if len(parts) > 2 else name
                            _emit(server.scaffold_project(
                                kind=kind, name=name, root=root, apply=True))
                    elif cmd == "/workspace":
                        do_workspace_select(arg)
                    elif cmd in ("/workspace-create", "/workspacecreate"):
                        do_workspace_create(arg)
                    elif cmd == "/location":
                        a = (arg or "").strip().lower()
                        if a in ("on", "off"):
                            location_consent = a == "on"
                        elif a:
                            _emit("usage: /location [on|off]")
                            continue
                        effective = (
                            server._env_location_consent()
                            if location_consent is None else location_consent
                        )
                        _emit("approximate IP location: %s%s" % (
                            "on" if effective else "off",
                            " (env default)" if location_consent is None else "",
                        ))
                    elif cmd == "/stats":
                        _emit(server.sonder_stats())
                    elif cmd == "/context":
                        _emit(server.context_health(session=session_id, project=project))
                    elif cmd in ("/contextsize", "/ctxsize"):
                        if arg.strip():
                            _emit(server.set_context_size(arg.strip()))
                        else:
                            _emit(server.context_policy_status())
                    elif cmd in ("/compact", "/compaction"):
                        _emit(server.context_compaction_plan(session=session_id, project=project))
                    elif cmd in ("/commands", "/cmds"):
                        _emit(server.command_registry_list(arg.strip()))
                    elif cmd == "/dump":
                        do_dump(arg.strip() or "repl")
                    elif cmd in ("/permissions", "/perms"):
                        _emit(server.permission_policy(arg.strip()))
                    elif cmd == "/mode":
                        _emit(_mode_command(arg.strip()))
                    elif cmd in ("/todo", "/task", "/tasks"):
                        text = arg.strip()
                        if not text or text.lower() in ("list", "ls"):
                            _emit(server.task_list(project=project))
                        else:
                            action, _, rest = text.partition(" ")
                            action = action.lower()
                            if action in ("add", "create", "new"):
                                _emit(server.task_create(title=rest.strip(), project=project))
                            elif action in ("done", "complete", "finish"):
                                if rest.strip():
                                    _emit(server.task_update(task_id=rest.strip(), status="done"))
                                else:
                                    _emit("usage: /todo done <task-id>")
                            elif action in ("start", "doing"):
                                if rest.strip():
                                    _emit(server.task_update(task_id=rest.strip(), status="in_progress"))
                                else:
                                    _emit("usage: /todo start <task-id>")
                            elif action in ("block", "blocked"):
                                if rest.strip():
                                    _emit(server.task_update(task_id=rest.strip(), status="blocked"))
                                else:
                                    _emit("usage: /todo block <task-id>")
                            elif action in ("show", "view"):
                                if rest.strip():
                                    _emit(server.task_show(rest.strip()))
                                else:
                                    _emit("usage: /todo show <task-id>")
                            elif action == "plan":
                                # "/todo plan Build auth | design schema | add API"
                                parts = [p.strip() for p in rest.split("|")]
                                parts = [p for p in parts if p]
                                if len(parts) < 2:
                                    _emit("usage: /todo plan <title> | <step> | <step> ...")
                                else:
                                    _emit(server.task_plan(
                                        title=parts[0],
                                        steps=json.dumps(parts[1:]),
                                        project=project,
                                        owner="sonder",
                                    ))
                            elif action in ("progress", "status"):
                                _emit(server.task_progress(project=project))
                            elif action in ("delete", "rm", "remove"):
                                if rest.strip():
                                    _emit(server.task_delete(task_id=rest.strip()))
                                else:
                                    _emit("usage: /todo delete <task-id>")
                            elif action in ("depend", "dep", "blockedby"):
                                # "/todo depend <task-id> <depends-on-id>"
                                dep_parts = rest.split()
                                if len(dep_parts) == 2:
                                    _emit(server.task_depend(
                                        task_id=dep_parts[0], depends_on=dep_parts[1],
                                    ))
                                else:
                                    _emit("usage: /todo depend <task-id> <depends-on-id>")
                            else:
                                _emit(_TODO_USAGE)
                    elif cmd == "/quality":
                        _emit(server.memory_quality_report())
                    elif cmd == "/qualityfix":
                        _emit(server.memory_quality_repair(apply=(arg.strip().lower() == "apply")))
                    elif cmd in ("/privacy", "/privacyreview", "/privacyfix", "/embeddings", "/embedfix"):
                        _emit(server.control_command(line, session=session_id, project=project))
                    elif cmd in ("/emotion", "/emotions", "/vectors", "/mood"):
                        _emit(server.emotion_command(arg))
                    elif cmd in ("/prefer", "/preference", "/preferences"):
                        _emit(server.preference_command(arg))
                    elif cmd in ("/improve", "/improvements"):
                        _emit(server.system_improvement_report(session=session_id, project=project))
                    elif cmd == "/artifact-mobility":
                        _emit(_artifact_mobility_command(arg))
                    elif cmd == "/lanes":
                        _emit(_lanes_command(arg))
                    elif cmd == "/recover":
                        _emit(_recovery_command(session_id, workspace_root, arg))
                    elif cmd == "/recovery":
                        _emit(_recovery_posture_command())
                    elif cmd in ("/agents", "/masterstatus"):
                        _emit(server.master_status())
                    elif cmd == "/fanouts":
                        _emit(_fanout_recent_command(arg))
                    elif cmd in ("/capacity", "/agentcapacity"):
                        _emit(server.control_command(line, session=session_id, project=project))
                    elif cmd in ("/agentcancel", "/cancelagents"):
                        _emit(server.control_command(line, session=session_id, project=project))
                    elif cmd in ("/agentretry", "/retryagent"):
                        _emit(server.control_command(line, session=session_id, project=project))
                    elif cmd == "/tools":
                        _emit(_tools_command(arg))
                    elif cmd == "/test":
                        _test_command(arg, workspace_root)
                    elif cmd == "/digest":
                        _emit(_digest_command(arg, workspace_root))
                    elif cmd == "/crash":
                        _crash_command(arg, workspace_root)
                    elif cmd == "/profile":
                        _profile_command(arg, workspace_root)
                    elif cmd == "/activity":
                        if arg.strip().lower() in ("watch", "tail"):
                            _watch_activity()
                        else:
                            _emit(server.activity_status())
                    elif cmd in ("/autopilot", "/auto", "/mission"):
                        _emit(server.control_command(
                            line, session=session_id, project=project,
                            autopilot_request_owner=_repl_autopilot_owner(cmd, arg),
                        ))
                    elif cmd in (
                        "/runtime", "/models", "/mcp", "/convergence",
                        "/update", "/updatecheck", "/updatesource",
                        "/stash", "/runtime-stash",
                        "/hardware", "/training", "/weighttraining",
                        "/selfmod", "/selfmodify",
                        "/learning", "/learnhealth", "/metrics",
                        "/goal", "/goals", "/ensemble",
                        "/approve", "/approvals",
                    ):
                        # ``/selfmod deploy`` will not accept "nobody to ask" as a yes,
                        # so this branch reports whether anybody was in fact asked.
                        # Reaching here means ``_named_command_gate`` passed; combined
                        # with an operator actually being attached, that is a person
                        # having answered its prompt, because the source-writing forms
                        # of ``/selfmod`` keep its ``dangerous`` grade and so always
                        # prompt outside ``plan`` (the read forms are narrowed to the
                        # read they are and never prompt, and ``_selfmod_command``
                        # consults this flag only for ``deploy`` and ``rollback``).
                        # With a piped stdin the gate refused rather than asked,
                        # nobody said yes, and this is False -- which is the whole
                        # point.
                        _emit(server.control_command(
                            line, session=session_id, project=project,
                            operator_approved=_console_has_operator(),
                        ))
                    elif cmd in ("/weather", "/forecast"):
                        _emit(server.control_command(
                            line, session=session_id, project=project,
                        ))
                    elif cmd in ("/work", "/agent"):
                        if not arg.strip():
                            _emit("usage: /work <task>")
                        else:
                            out = _run_session_work(session_id, host_project=project,
                                prompt=arg.strip(), tier=active_model or active_tier or "auto",
                                project=workspace_root or project, max_steps=12,
                            )
                            last_response = out
                            last_run_source = _answer_only(out)
                            last_iid = None
                            last_turn_metrics = _latest_repl_turn_metrics(surfaces=("agent",))
                            _emit(out)
                    elif cmd in (
                        "/report", "/endreport", "/checklist", "/plan",
                        "/inventory", "/workspace",
                        "/tree", "/folders", "/search", "/grep",
                        "/programs", "/programfind", "/scripts", "/scriptfind",
                        "/image", "/inspectimage", "/vision", "/analyzeimage",
                        "/runprogram", "/runscript",
                        "/artifactcheck", "/verifyartifact", "/groundartifact",
                    ):
                        _emit(server.control_command(
                            line, session=session_id, project=project,
                        ))
                    elif cmd in ("/asset", "/assets", "/assetgen", "/artifact"):
                        parts = arg.strip().split(None, 1)
                        if len(parts) != 2:
                            _emit("usage: /asset <name> <free-form brief>")
                        else:
                            _emit(server.artifact_generate(name=parts[0], brief=parts[1]))
                    elif cmd in ("/forge", "/gamesuite"):
                        _emit(server.game_reference_suite(name=arg.strip() or "sonder-reference"))
                    elif cmd in ("/game", "/gamegen"):
                        parts = arg.strip().split(None, 2)
                        if len(parts) != 3 or "|" not in parts[2]:
                            _emit("usage: /game <language> <2d|2.5d|3d> <name> | <concept>")
                        else:
                            name, _, concept = parts[2].partition("|")
                            _emit(server.game_generate_and_test(
                                name=name.strip(), concept=concept.strip(),
                                language=parts[0], dimension=parts[1],
                            ))
                    elif cmd in ("/gamefleet", "/gamecampaign"):
                        campaign_args = server._parse_game_campaign_command(arg)
                        if campaign_args is None:
                            _emit("usage: /gamefleet <name> | <concept> [| language | dimension]")
                        else:
                            _emit(server.game_generation_campaign(**campaign_args))
                    elif cmd == "/register":
                        parts = arg.split(None, 1)
                        if len(parts) != 2:
                            _emit("usage: /register <username> <password>")
                        else:
                            _emit(server.admin_register(parts[0], parts[1]))
                    elif cmd == "/login":
                        parts = arg.split(None, 1)
                        if not parts:
                            # Do not put a password in the line editor's process-local
                            # history. The explicit-argument form remains available
                            # for scripts and backwards compatibility, but an
                            # interactive login is masked by default.
                            username = _read_input("username: ").strip()
                            password = getpass.getpass("password: ")
                        elif len(parts) == 2:
                            username, password = parts
                        else:
                            _emit("usage: /login [<username> <password>]")
                            continue
                        out = server.admin_login(username, password)
                        # Keep the bearer token for this session; never print it
                        # (scrollback and `repl --json` stdout outlive the session).
                        from ...domain.login_output import split_login_output
                        token, display = split_login_output(out)
                        if token:
                            CURRENT_TOKEN = token
                        _emit(display)
                    elif cmd == "/whoami":
                        _emit(server.admin_whoami(CURRENT_TOKEN))
                    elif cmd == "/admin":
                        _emit(server.admin_status(CURRENT_TOKEN))
                    elif cmd == "/accounts":
                        _emit(server.admin_accounts(CURRENT_TOKEN))
                    elif cmd == "/setaccount":
                        parts = arg.split()
                        if not parts:
                            _emit("usage: /setaccount <username> role=developer tier=pro dev_flags=x banned=false")
                        else:
                            kv = {}
                            for item in parts[1:]:
                                if "=" in item:
                                    k, v = item.split("=", 1)
                                    kv[k] = v
                            _emit(server.admin_set_account(
                                token=CURRENT_TOKEN,
                                username=parts[0],
                                role=kv.get("role", ""),
                                tier=kv.get("tier", ""),
                                dev_flags=kv.get("dev_flags", ""),
                                banned=kv.get("banned", ""),
                            ))
                    elif cmd in ("/debug", "/inspect"):
                        _emit(server.debug_inspect(CURRENT_TOKEN))
                    elif cmd in ("/cot", "/chainofthought", "/thoughts"):
                        _emit(server.admin_private_chain_of_thought(CURRENT_TOKEN))
                    elif cmd == "/filepolicy":
                        _emit(server.file_policy(token=CURRENT_TOKEN))
                    elif cmd in ("/files", "/find"):
                        # A selected workspace scopes the search root too.
                        root, error = _workspace_scoped_path(
                            workspace_root, workspace_root and ".",
                        )
                        if error:
                            _emit(error)
                        else:
                            with _workspace_file_scope(workspace_root):
                                _emit(server.file_find(
                                    query=arg.strip() or "*", root=root,
                                    token=CURRENT_TOKEN,
                                ))
                    elif cmd == "/read":
                        if not arg.strip():
                            _emit("usage: /read <path>")
                        else:
                            workspace_file_command(
                                arg.strip(),
                                lambda path: server.file_read(path=path, token=CURRENT_TOKEN),
                            )
                    elif cmd in ("/write", "/append"):
                        parts = arg.split(None, 1)
                        if len(parts) != 2:
                            _emit("usage: %s <path> <text>" % cmd)
                        else:
                            workspace_file_command(parts[0], lambda path: server.file_write(
                                path=path,
                                content=parts[1],
                                mode="append" if cmd == "/append" else "create",
                                token=CURRENT_TOKEN,
                            ))
                    elif cmd == "/edit":
                        pieces = arg.split("|", 2)
                        if len(pieces) != 3 or not pieces[0].strip():
                            _emit("usage: /edit <path>|<old>|<new>")
                        else:
                            workspace_file_command(pieces[0].strip(), lambda path: server.file_edit(
                                path=path,
                                old=pieces[1],
                                new=pieces[2],
                                token=CURRENT_TOKEN,
                            ))
                    elif cmd == "/mkdir":
                        if not arg.strip():
                            _emit("usage: /mkdir <path>")
                        else:
                            workspace_file_command(
                                arg.strip(),
                                lambda path: server.directory_create(path=path),
                            )
                    elif cmd == "/delete":
                        if not arg.strip():
                            _emit("usage: /delete <path>")
                        else:
                            workspace_file_command(
                                arg.strip(),
                                lambda path: server.file_delete(
                                    path=path, dry_run=True, token=CURRENT_TOKEN,
                                ),
                            )
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
                        _emit(server.master_orchestrate(task=task, mode=mode))
                    elif cmd == "/lessons":
                        _print_lessons()
                    elif cmd in ("/pass", "/good"):
                        if last_iid:
                            _emit(server.record_outcome(last_iid, "tests_passed"))
                            last_iid = None
                        else:
                            _emit("(nothing to record yet)")
                    elif cmd in ("/accept", "/accepted", "/used", "/copied", "/edited"):
                        if last_iid:
                            signal = {
                                "/accept": "accepted",
                                "/accepted": "accepted",
                                "/used": "used",
                                "/copied": "copied",
                                "/edited": "edited",
                            }[cmd]
                            _emit(server.record_outcome(last_iid, signal))
                            last_iid = None
                        else:
                            _emit("(nothing to record yet)")
                    elif cmd in ("/fail", "/bad"):
                        if last_iid:
                            _emit(server.record_outcome(last_iid, "failed"))
                            last_iid = None
                        else:
                            _emit("(nothing to record yet)")
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
                        server._clear_managed_repl_conversation()
                        session_id = memory_store.new_id()
                        last_iid = None
                        last_response = None
                        last_run_source = None
                        last_turn_metrics = None
                        _emit("started a new thread (%s)" % session_id)
                    elif cmd == "/sessions":
                        _print_sessions()
                    elif cmd == "/replay":
                        do_replay(arg)
                    elif cmd == "/resume":
                        target = (arg or "").strip()
                        if not target:
                            _emit("usage: /resume <session-id|title-prefix>")
                        else:
                            conn = server._open_db()
                            try:
                                found = memory_store.find_session(conn, target)
                            finally:
                                conn.close()
                            if found:
                                server._clear_managed_repl_conversation()
                                session_id = found
                                last_iid = None
                                last_response = None
                                last_run_source = None
                                # Per-turn metrics are tied to the previous session's
                                # activity span.  Leaving them visible after a resume
                                # makes the composer attribute another conversation's
                                # token/call/timing data to the newly selected thread.
                                last_turn_metrics = None
                                _emit("resumed thread %s" % session_id)
                            else:
                                _emit("no session matching '%s'" % target)
                    elif cmd == "/project":
                        a = (arg or "").strip()
                        if not a:
                            _emit("project: %s" % project)
                        else:
                            project = a
                            _emit("project: %s" % project)
                    elif cmd == "/fact":
                        a = (arg or "").strip()
                        if not a:
                            _emit("usage: /fact <text> | /fact forget <id> confirm")
                        elif a.lower() == "forget" or a.lower().startswith("forget "):
                            bits = a.split()
                            if len(bits) != 3 or bits[2].lower() != "confirm":
                                _emit("usage: /fact forget <id> confirm")
                            else:
                                _emit(server.sonder_forget_fact(
                                    bits[1], project=project, confirm=bits[1],
                                ))
                        else:
                            _emit(server.sonder_remember_fact(a, project=project))
                    elif cmd == "/facts":
                        _print_facts(project)
                    elif cmd in ("/exit", "/quit", "/q"):
                        break
                    else:
                        _emit(_run_catalogued(line, cmd))
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
                # An explicit public-web request is not workspace work.  The shared
                # chat boundary already gives it a tightly scoped research agent, but
                # the REPL used to intercept it first and send it to the general
                # workbench loop.  That wasted tool calls and could produce a
                # checklist-backed "complete" response with no relevant sources.
                work_refusal = intents.containment_egress_refusal(line)
                if work_refusal:
                    started_at = time.monotonic()
                    last_response = work_refusal
                    last_run_source = work_refusal
                    last_turn_metrics = None
                    _print_chat_result(work_refusal, started_at, label="Sonder")
                    continue

                if intents.classify_work(line) and not web_intents.explicit_search(line):
                    if not workspace_root:
                        embedded_path, remainder = _split_existing_workspace_prefix(line)
                        if embedded_path:
                            # Path already named the folder - consume it instead of asking.
                            server._clear_managed_repl_conversation()
                            workspace_root = embedded_path
                            print("workspace: %s" % workspace_root)
                            task = remainder or line
                            if remainder:
                                print(_paint("(using folder from your message; working on: %s)" % remainder, "muted"))
                            run_workspace_work(task)
                            continue
                        pending_workspace_work = line
                        last_iid = None
                        last_response = None
                        last_run_source = None
                        last_turn_metrics = None
                        print(
                            "That looks like project work — which folder should I use?\n"
                            "  Existing: /workspace <path>\n"
                            "  Create:   /workspace-create <path>\n"
                            "Or say more about what you meant and I will clarify before touching files.\n"
                            "Guarded project work and runs stay inside the selected directory."
                        )
                        continue
                    run_workspace_work(line)
                    continue

                started_at = time.monotonic()
                _TURN_MODEL[0] = current_model()
                indicator = _begin_chat_turn()
                try:
                    out = server.sonder(line, trace=trace, strict=strict, persona=persona,
                                        session=session_id, project=project,
                                        tier=active_tier or "", model_override=active_model or "",
                                        location_consent=location_consent)
                except BaseException:
                    if indicator is not None:
                        indicator.stop()
                    raise
                last_turn_metrics = _latest_repl_turn_metrics(session_id)
                if _is_repl_error(out):
                    # One label; the error header and footer say it failed
                    # (P1-4: never "Sonder error · error").
                    _print_chat_result(out, started_at, label="Sonder", error=True,
                                       indicator=indicator, metrics=last_turn_metrics)
                    continue

                last_iid = server.parse_interaction_id(out)
                last_response = out
                last_run_source = _answer_only(out)
                cleaned = _strip_footer(out)
                _print_chat_result(cleaned, started_at, offer_feedback=bool(last_iid),
                                   indicator=indicator, interaction_id=last_iid,
                                   metrics=last_turn_metrics)
        except KeyboardInterrupt:
            # Ctrl-C during a turn cancels that turn (its cancellation
            # scope was cancelled by the signal handler) and returns to
            # the prompt. A Ctrl-C at the idle prompt still exits.
            announce_interrupted_turn()
            continue

    if ndjson_writer is not None:
        ndjson_writer.close()
        sys.stdout = previous_stdout


_RATING_COMMANDS = frozenset((
    "/pass", "/good", "/fail", "/bad", "/accept", "/accepted", "/used",
    "/copied", "/edited",
))


def _rating_precheck(cmd, last_iid):
    """``"nothing"``/``"rate"`` for a rating command, ``""`` for any other.

    Rating records an outcome for the answer just shown, the same thing the
    ungated plain-language form ("that worked") records; with no answer to
    rate there is nothing to approve either (P1-9).
    """
    if cmd not in _RATING_COMMANDS:
        return ""
    return "rate" if last_iid else "nothing"


if __name__ == "__main__":
    main()
