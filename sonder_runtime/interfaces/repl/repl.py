"""sonder — interactive terminal REPL for Sonder Runtime's local learning loop.

Boots straight into the injected legacy runtime's learning loop, the way
`claude` drops you into an interactive session. Slash-commands control
trace/strict mode, teach outcomes back, and surface stats/lessons. The legacy
runtime is an explicit composition dependency; this interface never discovers
it at import time.
"""
import json
import getpass
import inspect
import os
import re
import shutil
import sys
import threading
import time
from contextlib import redirect_stdout

from sonder_runtime.domain.common.errors import DependencyUnavailable
import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
from sonder_runtime.adapters.observability.repl_formatting import (
    elapsed_label as _elapsed_label,
    response_footer as _response_footer,
    response_status_label as _response_status_label,
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

    def _emit(self, text):
        self._seq += 1
        payload = {
            "schema": self.schema,
            "seq": self._seq,
            "event": "output",
            "text": str(text),
        }
        self._stream.write(json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ) + "\n")

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
    writer = _JsonLinesWriter(stream or sys.stdout)
    ansi_was_enabled = _Ansi.enabled
    legacy_ndjson = os.environ.pop("SONDER_REPL_NDJSON", None)
    _Ansi.enabled = False
    try:
        with redirect_stdout(writer):
            main(machine_output=True)
    finally:
        if legacy_ndjson is not None:
            os.environ["SONDER_REPL_NDJSON"] = legacy_ndjson
        writer.close()
        _Ansi.enabled = ansi_was_enabled


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


class _Ansi:
    """Small dependency-free palette; automatically disappears when piped.

    Truecolor is opt-in from the terminal's ``COLORTERM`` declaration. The
    fallback uses a 256-color surface so a terminal that only advertises ANSI
    colors does not receive unsupported 24-bit escape sequences.
    """

    enabled = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
    truecolor = enabled and os.environ.get("COLORTERM", "").lower() in {
        "truecolor", "24bit",
    }
    reset = "\x1b[0m"
    teal = "\x1b[38;5;80m"
    cyan = "\x1b[38;5;117m"
    # Keep the existing bright-cyan text, but give the composer itself a
    # quieter, darker-blue surface that separates input from the transcript.
    composer_surface = (
        "\x1b[48;2;17;38;54m\x1b[38;5;117m"
        if truecolor
        else "\x1b[48;5;23m\x1b[38;5;117m"
    )
    muted = "\x1b[38;5;245m"
    green = "\x1b[38;5;114m"
    amber = "\x1b[38;5;221m"
    red = "\x1b[38;5;210m"
    bold = "\x1b[1m"


def _paint(text, *styles):
    if not _Ansi.enabled:
        return str(text)
    return "".join(styles) + str(text) + _Ansi.reset


def _read_input(prompt, *, history=None, composer=False, argument_completer=None):
    """Prompt for a line, optionally in the raw terminal composer frame."""
    if slash_menu is not None:
        try:
            if slash_menu.available():
                # Keep ordinary input() as the universal fallback.  The
                # framed composer is raw-terminal presentation only, never a
                # second input protocol or a source of changed prompt text.
                return slash_menu.read_line(
                    "" if composer else prompt,
                    history=history, frame=prompt if composer else "",
                    frame_style=_Ansi.composer_surface if composer and _Ansi.enabled else "",
                    argument_completer=argument_completer,
                    fallback_prompt=prompt,
                )
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            pass  # a menu problem must never cost the user their prompt
    return input(prompt)


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
    """
    if not _console_has_operator():
        return False
    try:
        answer = input("%s [y/N] " % question)
    except (EOFError, OSError, KeyboardInterrupt):
        return False
    return str(answer or "").strip().lower() in ("y", "yes")


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


def _gate_tools(tools, label):
    """Strictest decision across ``tools``; returns ``(may_run, refusal_text)``.

    The console is the one surface that *can* have a human attached, so ``ask``
    means actually asking rather than degrading to allow the way a direct MCP
    call does -- but only when one actually is. ``deny`` prints why and runs
    nothing, whoever is or is not watching.

    ``interactive`` is therefore ``_console_has_operator()`` and not a
    hardcoded ``True``. A piped session (`sonder < script.txt`) has nobody to
    ask, so it degrades to allow exactly like every other non-interactive
    caller: `manual` refuses nothing there that it did not refuse before this
    gate existed, while a ``deny`` rule and ``plan`` still refuse. Asking
    anyway was worse than useless -- ``input()`` read the next line of the
    script as the answer, so one unseen prompt both denied the command and
    swallowed the one after it.

    A named command can front several tools (``/todo`` reaches everything from
    ``task_list`` to ``task_delete``), and which one runs depends on an
    argument this gate has not parsed. It therefore decides on the *strictest*
    member: a deny anywhere refuses, otherwise the highest-risk ``ask`` is the
    one the operator is asked about, once. Rounding the other way -- gating a
    command that can delete at the risk of its most harmless sibling -- would
    be under-enforcement, which is the failure this whole change exists to fix.
    """
    interactive = _console_has_operator()
    worst = None
    for tool in tools:
        # The exemption is not applied here any more: this asks for a decision
        # *for a person at a console*, and `permission_modes` owns which
        # exemptions that kind of caller carries. Four surfaces kept their own
        # copy of the check and the fifth was written without it.
        decision = permission_policy.decide_for_caller(
            tool, interactive=interactive, gate_control_exempt=True,
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
    if _confirm("run %s? %s." % (label, worst.reason)):
        return True, ""
    return False, "skipped %s" % label


def _permission_gate(tool):
    """Gate one tool dispatched as ``/<tool_name>`` through _run_catalogued."""
    return _gate_tools((tool,), "/" + tool)


def _named_command_gate(cmd):
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
    # Workspace creation is implemented by a nested REPL helper, so it is not
    # visible to the catalog's top-level branch scanner.  Keep its filesystem
    # mutation in the same single choke-point gate as every other command.
    if cmd in ("/workspace-create", "/workspacecreate"):
        tools = tuple(tools) + ("directory_create",)
    return _gate_tools(tools, cmd)


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
    return "unknown command %s\n\n%s" % (
        cmd, command_catalog.format_matches(cmd),
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


def _rule(char="─", width=56):
    return _paint(char * width, _Ansi.muted)


def _result_tag(ok):
    return _paint("PASS" if ok else "FAIL", _Ansi.green if ok else _Ansi.red, _Ansi.bold)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*(?:\x1b\\|\x07)")


def _terminal_link(url, label=None):
    """Return an OSC 8 link for an interactive terminal, or plain text.

    Windows Terminal recognizes OSC 8 as a real clickable link, so the REPL's
    loopback endpoint can open the local live-log dashboard directly.  The
    escape sequence is omitted when colour is disabled: copied/piped output
    must remain the literal URL and layout width must stay stable.
    """
    target = str(url or "")
    text = str(label if label is not None else target)
    if not _Ansi.enabled or not target.startswith(("http://", "https://")):
        return text
    return "\x1b]8;;%s\x1b\\%s\x1b]8;;\x1b\\" % (target, text)


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


def _permission_mode_colour(snapshot):
    mode = PermissionModeFacade().snapshot(snapshot)
    if mode is None:
        return _Ansi.muted
    return {
        "plan": _Ansi.green,
        "manual": _Ansi.cyan,
        "acceptEdits": _Ansi.amber,
        "auto": _Ansi.red,
    }.get(str(mode.get("mode") or ""), _Ansi.muted)


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
        model = ModelSelectionFacade.resolved_model(server.TIERS, tier)
    except Exception:
        model = "unknown"
    try:
        host, port = listener_probe.DEFAULT_HOST, listener_probe.DEFAULT_PORT
        live = listener_probe.port_open(host, port)
        # 0.0.0.0/:: are bind addresses, not browser destinations.  The
        # dashboard remains loopback-only in this presentation, matching the
        # readiness probe and avoiding a dead OSC-8 link on wildcard binds.
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        endpoint = "http://%s:%s" % (display_host, port)
    except Exception:
        endpoint, live = os.environ.get("SONDER_API", "http://127.0.0.1:11435"), False

    source = {}
    try:
        # Startup must never wait on a remote Git server.  The cached
        # origin/main ref still shows the installed/newest known revisions;
        # `/updatecheck` is the explicit network refresh command.
        source = server.runtime_source_update_status_data(refresh=False)
        revision = "%s @ %s" % (
            str(source.get("installed_commit") or "unknown")[:12],
            source.get("installed_commit_time") or "unknown time",
        )
        newest = "%s @ %s" % (
            str(source.get("newest_commit") or "unknown")[:12],
            source.get("newest_commit_time") or "unknown time",
        )
        newest_label = (
            "newest source" if source.get("remote_ref_refreshed")
            else "newest known source"
        )
        update = "%s (behind %s)" % (
            source.get("state") or "unknown", source.get("behind", "?"),
        )
        running_commit = str(source.get("running_commit") or "").strip()
        running = running_commit[:12] if running_commit else "unavailable"
        if source.get("restart_required"):
            update += "; restart required"
    except Exception as exc:
        revision = newest = "unavailable (%s)" % type(exc).__name__
        newest_label = "newest source"
        running = "unavailable"
        update = "check unavailable"

    permission = _permission_mode_snapshot()
    rows = [
        ("model", "%s  %s" % (model, _paint("(%s tier)" % tier, _Ansi.muted)),
         (_Ansi.cyan,)),
        ("endpoint", _terminal_link(endpoint) if live else
         "%s  %s" % (_terminal_link(endpoint), _paint("(not listening)", _Ansi.amber)),
         (_Ansi.green,) if live else (_Ansi.muted,)),
        ("directory", _home_relative(os.getcwd()), ()),
        ("persona", str(persona), (_Ansi.cyan,)),
        ("project", str(project or "(none)"), ()),
        ("installed source", revision, ()),
        ("running source", running, (_Ansi.amber,) if source.get("restart_required") else ()),
        (newest_label, newest, ()),
        ("update", "%s  /updatecheck | /update" % update, (_Ansi.amber,)),
    ]
    if permission is not None:
        mode_view = PermissionModeFacade()
        mode_text = mode_view.label(permission)
        blurb = str(permission.get("blurb") or "").strip()
        rows.append((
            "mode", "%s%s" % (mode_text, "  -- " + blurb if blurb else ""),
            (_permission_mode_colour(permission),),
        ))
        if mode_view.elevated(permission):
            reason = mode_view.elevation_reason(permission)
            rows.append((
                "elevation", "on%s" % ("  -- " + reason if reason else ""),
                (_Ansi.red,),
            ))
    if strict:
        rows.append(("strict", "on  pinned to the sonder alias", (_Ansi.amber,)))
    hint = "%s  %s" % (
        _paint("type /help for commands", _Ansi.muted),
        _paint("or just start typing.", _Ansi.muted),
    )
    return "%s\n\n  %s\n" % (_banner(rows), hint)


def _execution_prompt(status=None):
    """Prompt suffix refreshed once per user turn, with no polling thread."""
    facade = ExecutionStatusFacade(
        lambda: server.execution_status_data(),
    )
    value = facade.snapshot(status)
    colour = _Ansi.amber
    if value is not None:
        try:
            colour = _Ansi.green if any(
                int(value.get(key) or 0) for key in
                ("running_lanes", "running_agents", "queued_agents")
            ) else _Ansi.muted
        except (TypeError, ValueError, OverflowError):
            pass
    return _paint(facade.prompt_text(status), colour)


def _compact_count(value):
    """Render a non-negative count without making the composer noisy."""
    try:
        value = max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return "?"
    if value >= 1_000_000:
        return "%.1fm" % (value / 1_000_000.0)
    if value >= 1_000:
        return "%.1fk" % (value / 1_000.0)
    return str(value)


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


def _composer_title(tier=None, status=None, *, context=None, last_turn=None,
                    width=None, model_override=None, permission=None):
    """Live model, runtime, context and last-turn status for the composer."""
    # Resolve the live execution state once. A title that is subsequently
    # compacted must keep this lanes/agents snapshot rather than falling back
    # to compact-mode zero values.
    status_facade = ExecutionStatusFacade(
        lambda: server.execution_status_data(),
    )
    status = status_facade.snapshot(status)
    resolved_tier = str(tier or "code")
    try:
        model = ModelSelectionFacade.resolved_model(
            server.TIERS, resolved_tier, model_override,
        )
    except Exception:
        model = "unknown"
    # This is intentionally the live table rather than a cached launch value:
    # /model changes should be visible before the very next submitted turn.
    compact = width is not None and int(width) <= 88
    if compact:
        # The wide composer delegates to _execution_prompt(), which refreshes
        # a missing status snapshot and explicitly renders an unavailable
        # lookup as unknown.  Do the same here: treating ``None`` as an empty
        # mapping made narrow terminals claim L0/A0 when the status service
        # was unavailable -- an especially misleading place to hide a live
        # fanout or failed status read.
        lanes, agents = status_facade.counts(status)
        parts = ["S %s" % resolved_tier]
        if permission is not None:
            mode_view = PermissionModeFacade()
            parts.append("M:%s" % mode_view.short_label(permission))
            if mode_view.elevated(permission):
                parts.append("E!")
        parts.append("L%s A%s" % (lanes, agents))
    else:
        parts = ["Sonder %s (%s)  %s" % (
            resolved_tier, model, _execution_prompt(status),
        )]
        if permission is not None:
            mode_view = PermissionModeFacade()
            mode_text = "mode %s" % mode_view.label(permission)
            if mode_view.elevated(permission):
                reason = mode_view.elevation_reason(permission)
                mode_text += "  ELEVATED%s" % (
                    " (%s)" % reason if reason else "",
                )
            parts.append(_paint(mode_text, _permission_mode_colour(permission)))
    if isinstance(context, dict):
        parts.append(("C%s/%s L%s" if compact else "ctx~%s/%s (%s left)") % (
            _compact_count(context.get("used")),
            _compact_count(context.get("limit")),
            _compact_count(context.get("left")),
        ))
    if isinstance(last_turn, dict):
        parts.append(("T%s/%s" if compact else "tok %s/%s") % (
            _compact_count(last_turn.get("tokens_in")),
            _compact_count(last_turn.get("tokens_out")),
        ))
        elapsed = max(0, int(last_turn.get("elapsed_ms") or 0))
        if elapsed:
            parts.append(_elapsed_label(elapsed))
        calls = int(last_turn.get("model_calls") or 0)
        tools = int(last_turn.get("tool_calls") or 0)
        if calls or tools:
            parts.append(("M%s T%s" if compact else "calls %sM/%sT") % (
                _compact_count(calls), _compact_count(tools),
            ))
    title = (" | " if compact else "  |  ").join(parts)
    # A wide terminal can still have a long active model tag plus a complete
    # metrics suffix.  The frame truncates titles rather than wrapping them,
    # which previously left an unhelpful clipped tail such as ``ca-``.  Switch
    # to the compact vocabulary whenever the actual composed title will not
    # fit, not merely below one fixed terminal-width threshold.
    if not compact and width is not None:
        try:
            title_budget = max(0, int(width) - 4)
        except (TypeError, ValueError, OverflowError):
            title_budget = 0
        if title_budget and _visible_len(title) > title_budget:
            return _composer_title(
                tier, status, context=context, last_turn=last_turn,
                width=88, model_override=model_override, permission=permission,
            )
    return title


def _composer_frame_width():
    """Use the raw composer's current width for a compact title when needed."""
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
  /trace [on|off]    toggle trace mode (bare = on); shows retrieval + prompt
  /strict [on|off]   toggle strict mode (bare = on); pins to the sonder alias
  /persona [name]    show/set active persona (coder/explainer/reviewer/teacher)
  /model [name|tier] list installed models and tiers; switch either one
  /cloud [status|on|off]  change process-local hosted/cloud consent
  /consult <question> ask 2 local tiers (+cloud when enabled) and compare answers
  /route <request>   suggest the tier best suited to a request, and why
  /refactor <file> <fn> [goal]  propose a guarded improvement to one function
  /scaffold <kind> <name> [root]  write a full project skeleton (cpp-msvc, csharp, rust, ...)
  /workspace [path]  show/set the directory used for guarded project work
  /workspace-create <path>  create a guarded directory, select it, and resume queued work
  /env [refresh]     show the host OS, shells, and installed toolchains
  /toolstatus <name> run the fixed local version probe for a discovered tool
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
  /runtime ...       shared local model mappings and execution-lane tiers
  /stash ...         save/restore this install's source edits for a guarded update
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
  /fanouts [N|active]  list safe recent durable model-fanout summaries
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
    """A terminal-only animated acknowledgement for a synchronous REPL turn."""

    def __init__(self, label, stream=None):
        self.label = str(label or "Sonder")
        self.stream = stream or sys.stdout
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _render(self, tick):
        dots = "." * (1 + tick % 3)
        base = "%s %s is working%s" % (_box_chars()["dot"], self.label, dots)
        if not _Ansi.enabled:
            return base
        # A single cyan highlight crosses the label from left to right.  The
        # text itself stays stable, so it reads as activity rather than a
        # progress or completion claim.
        index = tick % max(1, len(base))
        return (
            _Ansi.muted + base[:index] + _Ansi.cyan + _Ansi.bold
            + base[index:index + 1] + _Ansi.muted + base[index + 1:]
            + _Ansi.reset
        )

    def _run(self):
        tick = 0
        while not self._stop.is_set():
            try:
                self.stream.write("\r" + self._render(tick))
                self.stream.flush()
            except Exception:
                return
            tick += 1
            self._stop.wait(0.14)

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(0.5)
        try:
            width = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
            self.stream.write("\r" + (" " * width) + "\r")
            self.stream.flush()
        except Exception:
            pass


def _begin_chat_turn(label="Sonder"):
    """Acknowledge an interactive submission before synchronous work begins."""
    if not (_console_has_operator() and _stdout_is_interactive()):
        return None
    if not _Ansi.enabled:
        box = _box_chars()
        print(_paint("%s %s is working..." % (box["dot"], label), _Ansi.muted))
        return None
    return _WorkingIndicator(label).start()


def _print_chat_result(text, started_at, *, offer_feedback=False,
                       label="Sonder", error=False, indicator=None,
                       interaction_id=None):
    """Present one completed turn with lightweight terminal chrome.

    The result itself is printed verbatim: the bars are separate lines, never
    a width cap, pager, or content transform.  Piped/scripted uses retain the
    historical plain output so shells and callers do not receive decoration.
    SONDER_REPL_NDJSON=1 lets a piped caller opt into one structured JSON
    line per turn instead of scraping that plain text; interactive terminals
    keep their chrome regardless, and the flagless default never changes.
    """
    if indicator is not None:
        indicator.stop()
    answer = str(text or "")
    timing = _completion_timing(started_at)
    if not (_console_has_operator() and _stdout_is_interactive()):
        if _machine_output.enabled(os.environ):
            elapsed_ms = max(
                0, int((time.monotonic() - float(started_at)) * 1000),
            )
            print(_machine_output.ndjson_line(_machine_output.turn_payload(
                answer, elapsed_ms=elapsed_ms, error=error,
                interaction_id=interaction_id,
                feedback_offered=offer_feedback, label=label,
                hint=_error_hint(answer) if error else "",
            )))
            return
        print(answer)
        print(_paint("[%s]" % timing, _Ansi.muted))
        if offer_feedback:
            print("(/pass or /fail to teach Sonder Runtime)")
        return

    box = _box_chars()
    title = "%s %s " % (
        box["tl"], _response_status_label(label, error=error),
    )
    tone = _Ansi.red if error else _Ansi.teal
    print(_paint(title + box["h"] * 8, tone, _Ansi.bold))
    print(answer)
    footer = _response_footer(box, timing, offer_feedback=offer_feedback)
    print(_paint(footer, _Ansi.muted))
    if error:
        # Garnish, not classification: a known failure shape earns one muted
        # next-step line under the panel; anything unrecognized adds nothing.
        hint = _error_hint(answer)
        if hint:
            print(_paint("  hint: %s" % hint, _Ansi.muted))


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
    print(_format_sessions(sessions))


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
        print("  - %s  %s" % (f["id"], f["text"]))


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
    # Keep raw-palette recall local to this REPL process. Prompts can include
    # private repository context, so terminal convenience must not create a
    # durable prompt log.
    input_history = []
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
            workspace_root = ""
            print("workspace cleared; the next work request will ask for a directory")
            return
        path, error = _workspace_path(text)
        if error:
            print(error + "\nuse /workspace-create <path> to create a new guarded directory")
            return
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
        print(output)
        if _is_repl_error(output):
            return
        path, error = _workspace_path(path)
        if error:
            print(error)
            return
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
        return ("/workspace-create " if create else "/workspace ") + text

    def run_workspace_work(task):
        nonlocal last_iid, last_response, last_run_source, last_turn_metrics
        started_at = time.monotonic()
        indicator = _begin_chat_turn("Sonder work")
        try:
            out = server.workbench_agent(
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
        _print_chat_result(out, started_at, label="Sonder work", indicator=indicator)

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
            current = str(active_model or server.TIERS.get(tier) or "?")
            print("active tier: %s  ->  %s" % (
                _paint(tier, _Ansi.cyan), _paint(current, _Ansi.cyan, _Ansi.bold)))
            print()
            print(_paint("tiers", _Ansi.muted))
            for name in sorted(tiers):
                mark = "*" if name == tier else " "
                print("  %s %-14s %s" % (mark, name, tiers[name]))
            # A withheld tier is named rather than silently omitted: the user
            # configured it, and "where did cloud-code go" is a worse answer
            # than one line saying what would turn it back on.
            for name in sorted(set(server.TIERS).difference(tiers)):
                print(_paint("  - %-14s %s" % (
                    name, _unselectable_tier_reason(name) or "unavailable",
                ), _Ansi.muted))
            print()
            if installed is None:
                print(_paint("installed models: (ollama did not answer)", _Ansi.amber))
            elif installed:
                print(_paint("installed models (ollama)", _Ansi.muted))
                for name, size in installed:
                    mark = "*" if name == current else " "
                    print("  %s %-40s %s" % (mark, name, size))
            else:
                print(_paint("installed models (ollama): (none installed)", _Ansi.amber))
            print()
            print(_paint("usage: /model <model-name>  |  /model <tier>", _Ansi.muted))
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
                withheld, withheld_reason), _Ansi.red))
            return

        if installed is None:
            print(_paint(
                "cannot verify installed models because Ollama did not answer; model selection was not changed",
                _Ansi.red,
            ))
            return

        names = [name for name, _size in installed]
        model_names = {str(name).casefold(): name for name in names}
        selected_model = model_names.get(arg.casefold())
        if selected_model is None:
            # Refuse rather than rebind to something that will fail on the next
            # turn with an opaque ollama error. Suggest, because a near miss is
            # usually a tag typo (":7b" vs ":latest"). Match the command's own
            # case-insensitive resolution, and never suggest from an empty base
            # (an arg like ":latest"), which would match every installed tag.
            base = arg.split(":")[0].casefold()
            near = [name for name in names if base and base in name.casefold()]
            print(_paint("no installed model named %r" % arg, _Ansi.red))
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
                    _Ansi.red,
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
            tier, _paint(active_model, _Ansi.cyan, _Ansi.bold)))

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
            print("\n=== %s ===\n%s" % (answer["tier"], answer["text"]))
        verdict = consult_flow.verdict_line(result)
        colour = _Ansi.green if result["agree"] is True else _Ansi.amber
        print("\n" + _paint(verdict, colour, _Ansi.bold))

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
            available_tiers=set(_selectable_tiers()))["tier"]
        print(_paint("asking %s to improve %s ..." % (chosen, fname), _Ansi.muted))
        res = code_improve.improve_function(
            src, fname,
            lambda p, t: server.ensemble_answer(p, tiers=t, mode="code"),
            tier=chosen, objective=objective)
        if not res["ok"]:
            print(_paint("no change: %s" % res["reason"], _Ansi.amber))
            return
        print(res["diff"] or "(no diff)")
        if _confirm(_paint("apply this change?", _Ansi.amber)):
            server.file_ops.write_file(fpath, res["edited"], mode="overwrite")
            print(_paint("applied to %s" % fpath, _Ansi.green))
        else:
            print(_paint("discarded", _Ansi.muted))

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
        print(_format_session_replay(
            turns, session_id=replay_session, limit=count,
        ))

    if not machine_output:
        print(_startup_banner(strict, persona, project, active_tier))

    while True:
        # ``/workspace`` may select/create a directory in response to a prior
        # natural work request.  Run that original request on the next loop
        # turn so the selection command itself remains separately permission
        # gated and does not accidentally run the agent in the old cwd.
        if queued_workspace_work:
            task = queued_workspace_work
            queued_workspace_work = ""
            run_workspace_work(task)
            continue
        try:
            prompt = "" if machine_output else _composer_title(
                active_tier,
                context=_composer_context(session_id, project),
                last_turn=last_turn_metrics,
                width=_composer_frame_width(),
                model_override=active_model,
                permission=_permission_mode_snapshot(),
            )
            line = _read_input(
                prompt,
                history=input_history,
                composer=not machine_output,
                argument_completer=model_argument_completer,
            )
        except (EOFError, KeyboardInterrupt):
            print()
            break

        # PowerShell 5.1 may prefix the first piped UTF-8 line with a BOM. Treat
        # it as transport framing so slash commands remain commands.
        line = _normalize_input_line(line)
        if not line:
            continue
        if pending_workspace_work and not line.startswith("/"):
            workspace_reply = _workspace_reply_command(line)
            if workspace_reply:
                print(_paint("(interpreted as: %s)" % workspace_reply, _Ansi.muted))
                line = workspace_reply
        if _history_safe(line) and (not input_history or input_history[-1] != line):
            input_history.append(line)
            if len(input_history) > REPL_HISTORY_LIMIT:
                del input_history[:-REPL_HISTORY_LIMIT]
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
                print(_paint("(interpreted as: %s)" % resolved, _Ansi.muted))
                line = resolved

        if line.startswith("/"):
            parts = line.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            # One choke point for every hand-written branch below, including
            # the ones forwarded to server.control_command. Commands handled by
            # _run_catalogued (the `else`) are gated there instead.
            may_run, refusal = _named_command_gate(cmd)
            if not may_run:
                print(refusal)
                # Terminal-only garnish; piped refusal text stays byte-stable.
                if _stdout_is_interactive():
                    hint = _error_hint(refusal)
                    if hint:
                        print(_paint("  hint: %s" % hint, _Ansi.muted))
                continue

            if cmd == "/":
                # A bare slash is the "what can I type" gesture.
                print(command_catalog.format_matches(""))
            elif cmd == "/help":
                print(command_catalog.help_text(arg.strip()))
            elif cmd == "/why":
                # A diagnostic read over the resolver's own trace: which stage
                # claimed (or refused) a plain-language turn, and on what
                # evidence. Never dispatches anything.
                target = arg.strip() or last_natural_turn
                if not target:
                    print(
                        "usage: /why [text]  explain how a plain-language turn"
                        " routes; the bare form uses your previous non-slash"
                        " turn"
                    )
                else:
                    print(_format_route_explanation(
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
                    print("sonder %s (commit %s, stamped release)" % (
                        stamped.version, stamped.commit_sha[:12],
                    ))
                else:
                    print("sonder %s (source checkout)"
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
                print(server.cloud_opt_in(arg.strip() or "status"))
            elif cmd == "/consult":
                do_consult(arg)
            elif cmd == "/route":
                do_route(arg)
            elif cmd == "/refactor":
                do_refactor(arg)
            elif cmd in ("/env", "/environment"):
                print(server.environment_status(
                    refresh=(arg or "").strip().lower() == "refresh"))
            elif cmd in ("/toolstatus", "/toolversion"):
                name = (arg or "").strip()
                if not name:
                    print("usage: /toolstatus <discovered-tool-name>  (try /env first)")
                else:
                    print(server.toolchain_status(name=name))
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
            elif cmd == "/workspace":
                do_workspace_select(arg)
            elif cmd in ("/workspace-create", "/workspacecreate"):
                do_workspace_create(arg)
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
            elif cmd == "/mode":
                print(_mode_command(arg.strip()))
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
                    elif action == "plan":
                        # "/todo plan Build auth | design schema | add API"
                        parts = [p.strip() for p in rest.split("|")]
                        parts = [p for p in parts if p]
                        if len(parts) < 2:
                            print("usage: /todo plan <title> | <step> | <step> ...")
                        else:
                            print(server.task_plan(
                                title=parts[0],
                                steps=json.dumps(parts[1:]),
                                project=project,
                                owner="sonder",
                            ))
                    elif action in ("progress", "status"):
                        print(server.task_progress(project=project))
                    elif action in ("delete", "rm", "remove"):
                        if rest.strip():
                            print(server.task_delete(task_id=rest.strip()))
                        else:
                            print("usage: /todo delete <task-id>")
                    elif action in ("depend", "dep", "blockedby"):
                        # "/todo depend <task-id> <depends-on-id>"
                        dep_parts = rest.split()
                        if len(dep_parts) == 2:
                            print(server.task_depend(
                                task_id=dep_parts[0], depends_on=dep_parts[1],
                            ))
                        else:
                            print("usage: /todo depend <task-id> <depends-on-id>")
                    else:
                        print(
                            "usage: /todo [list] | /todo add <title> | /todo start <id> | "
                            "/todo done <id> | /todo block <id> | /todo show <id>\n"
                            "       /todo plan <title> | <step> | <step> ...\n"
                            "       /todo progress | /todo delete <id> | "
                            "/todo depend <id> <depends-on-id>"
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
            elif cmd == "/fanouts":
                print(_fanout_recent_command(arg))
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
                "/update", "/updatecheck", "/updatesource",
                "/stash", "/runtime-stash",
                "/hardware", "/training", "/weighttraining",
                "/selfmod", "/selfmodify",
                "/learning", "/learnhealth", "/metrics",
                "/goal", "/goals", "/ensemble",
            ):
                # ``/selfmod deploy`` will not accept "nobody to ask" as a yes,
                # so this branch reports whether anybody was in fact asked.
                # Reaching here means ``_named_command_gate`` passed; combined
                # with an operator actually being attached, that is a person
                # having answered its prompt, because ``/selfmod`` is graded
                # ``dangerous`` and so always prompts outside ``plan``. With a
                # piped stdin the gate degraded rather than asked, nobody said
                # yes, and this is False -- which is the whole point.
                print(server.control_command(
                    line, session=session_id, project=project,
                    operator_approved=_console_has_operator(),
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
                        prompt=arg.strip(), tier=active_model or active_tier or "auto",
                        project=workspace_root or project, max_steps=12,
                    )
                    last_response = out
                    last_run_source = _answer_only(out)
                    last_iid = None
                    last_turn_metrics = _latest_repl_turn_metrics(surfaces=("agent",))
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
                    print("usage: /login [<username> <password>]")
                    continue
                out = server.admin_login(username, password)
                marker = "token: "
                from ...domain.cloud_access import has_legacy_error_prefix
                if marker in out and not has_legacy_error_prefix(out):
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
                last_turn_metrics = None
                print("started a new thread (%s)" % session_id)
            elif cmd == "/sessions":
                _print_sessions()
            elif cmd == "/replay":
                do_replay(arg)
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
                        # Per-turn metrics are tied to the previous session's
                        # activity span.  Leaving them visible after a resume
                        # makes the composer attribute another conversation's
                        # token/call/timing data to the newly selected thread.
                        last_turn_metrics = None
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
                    print("usage: /fact <text> | /fact forget <id> confirm")
                elif a.lower().startswith("forget "):
                    bits = a.split()
                    if len(bits) != 3 or bits[2].lower() != "confirm":
                        print("usage: /fact forget <id> confirm")
                    else:
                        print(server.sonder_forget_fact(
                            bits[1], project=project, confirm=bits[1],
                        ))
                else:
                    print(server.sonder_remember_fact(a, project=project))
            elif cmd == "/facts":
                _print_facts(project)
            elif cmd in ("/exit", "/quit", "/q"):
                break
            else:
                print(_run_catalogued(line, cmd))
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
                pending_workspace_work = line
                last_iid = None
                last_response = None
                last_run_source = None
                last_turn_metrics = None
                print(
                    "Where should I create or work on this project?\n"
                    "  Existing folder: /workspace <project-folder>\n"
                    "  New folder:      /workspace-create <project-folder>\n"
                    "I will keep all guarded project work and requested runs inside that directory."
                )
                continue
            run_workspace_work(line)
            continue

        started_at = time.monotonic()
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
            _print_chat_result(out, started_at, label="Sonder error", error=True,
                               indicator=indicator)
            continue

        last_iid = server.parse_interaction_id(out)
        last_response = out
        last_run_source = _answer_only(out)
        cleaned = _strip_footer(out)
        _print_chat_result(cleaned, started_at, offer_feedback=bool(last_iid),
                           indicator=indicator, interaction_id=last_iid)


if __name__ == "__main__":
    main()
