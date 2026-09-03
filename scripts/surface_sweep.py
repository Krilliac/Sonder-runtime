"""Exercise every catalogued command on every surface and record what happened.

Not a test: a sweep. It drives the whole surface of Sonder Runtime -- every
command in the catalog on the console chain, the control chain, the legacy
MCP server, the native MCP server, the served HTTP API and the agent
dispatcher, plus the natural-language router and the CLI entry point -- and
classifies each outcome rather than asserting one. The point is to see what a
real caller sees, in one place, on one build, and to find the things a unit
test did not think to ask.

Hermetic by construction: a fresh Sonder home and a throwaway workspace, the
operator's mode and rules untouched, the model stubbed (``--live-model`` to
use the configured local model instead). Everything a command writes lands in
the sweep's temporary directories: the guarded file root, every module that
anchors a writable file to its own directory (standing instructions, emotion
vectors, workflows, generated assets and games, self-heal, the code runner)
and the working directory are all pointed there before the first call. The
``dangerous`` class is refused for an unattended caller in every mode, so
nothing here can update the source tree, grant an account or change the
running policy of a real install. The checkout is compared before and after
the run; any path the sweep changed is recorded as a harness finding and
fails the run.

Outcome classes, one per invocation:

    ok             the call ran and answered
    gated          the permission gate refused it for an unattended caller, as
                   the mode says it should (``refused`` / ``HOST POLICY``)
    usage          the command answered with its usage or an argument error:
                   reachable, but the sweep could not synthesise its arguments
    argument       the command refused the synthetic argument on its merits
                   (no such id, not JSON, not a git repository, login required)
    containment    the guarded primitives refused a path or root
    model          the call needed a model turn the environment cannot make
    dependency     a host program, extension or service the environment lacks
    unavailable    the feature is off by configuration (web tools, cloud, ...)
    error          an ``ERROR:`` answer that is none of the above -- read it
    crash          an exception escaped the surface -- a defect
    timeout        the watchdog fired -- a defect or a missing bound
    skipped        the surface cannot express the call (a multi-line argument)

Usage::

    python scripts/surface_sweep.py --out eval_runs/sweep [--mode manual|auto|plan]
        [--surfaces control,console,mcp,native,http,agent,router,cli]
        [--only /name ...] [--timeout 20] [--live-model]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SURFACES = ("control", "console", "mcp", "native", "http", "agent", "router", "cli")
CLASSES = ("ok", "gated", "usage", "argument", "containment", "model", "dependency",
           "unavailable", "error", "crash", "timeout", "skipped")

PATCH = "--- a/patched.txt\n+++ b/patched.txt\n@@ -1,3 +1,3 @@\n-needle\n+thread\n second line\n third\n"
EXCERPT_CHARS = 240


class SweepTimeout(Exception):
    """The per-call watchdog fired."""


# --- environment ----------------------------------------------------------------


def _prepare_environment(root: str) -> dict:
    """Point every store at a throwaway home before the runtime imports."""
    home = os.path.join(root, "home")
    workspace = os.path.join(root, "workspace")
    os.makedirs(home, exist_ok=True)
    os.makedirs(workspace, exist_ok=True)
    with open(os.path.join(workspace, "notes.txt"), "w", encoding="utf-8") as handle:
        handle.write("needle\nsecond line\nthird\n")
    with open(os.path.join(workspace, "patched.txt"), "w", encoding="utf-8") as handle:
        handle.write("needle\nsecond line\nthird\n")
    with open(os.path.join(workspace, "data.json"), "w", encoding="utf-8") as handle:
        handle.write('{"a": 1}\n')
    with open(os.path.join(workspace, "tool.py"), "w", encoding="utf-8") as handle:
        handle.write("print('ok')\n")
    with open(os.path.join(workspace, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("# sweep workspace\n")
    os.environ["SONDER_HOME"] = home
    for name in (
        "SONDER_FILE_ROOTS", "SONDER_FILE_BYPASS", "SONDER_FILE_APPROVAL_CODE",
        "SONDER_ALLOW_CLOUD", "SONDER_WEB_TOOLS", "SONDER_ALLOW_REMOTE_OLLAMA",
        "SONDER_API_KEY", "SONDER_ALLOW_PERMISSION_EDITS",
    ):
        os.environ.pop(name, None)
    os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:9")
    return {"home": home, "workspace": workspace}


def _stub_generate(*_args, **_kwargs):
    """A generate() closure that answers instantly and finalises every loop."""

    def gen(prompt, history=None):
        del prompt, history
        gen.last_usage = {}
        gen.last_response_meta = {}
        return json.dumps({"final": "sweep stub answer"})

    gen.last_usage = {}
    gen.last_response_meta = {}
    return gen


# --- argument synthesis -----------------------------------------------------------


_NAME_VALUES = {
    "path": "notes.txt", "file": "notes.txt", "source": "notes.txt", "target": "notes.txt",
    "destination": "copied.txt", "root": ".", "directory": ".", "cwd": ".", "project": "",
    "project_dir": ".", "repo": ".", "query": "needle", "pattern": "needle", "needle": "needle",
    "text": "sweep probe", "content": "hello", "old": "needle", "new": "thread",
    "prompt": "sweep probe", "task": "sweep probe", "question": "sweep probe",
    "message": "sweep probe", "objective": "sweep probe", "title": "sweep probe",
    "name": "sweep", "note": "sweep note", "reason": "sweep", "summary": "sweep",
    "brief": "sweep probe", "instruction": "sweep probe", "description": "sweep probe",
    "program": "python", "args_json": "[]", "code": "print(1)", "language": "python",
    "url": "http://127.0.0.1:9/", "session": "sweep", "session_id": "sweep",
    "tool_name": "file_read", "tool": "file_read", "token": "", "approval": "",
    "extra_roots": "", "glob": "*", "kind": "note", "label": "sweep", "key": "sweep",
    "value": "sweep", "username": "sweep", "password": "sweep-password", "role": "user",
    "action": "status", "command": "status", "selector": "sweep", "run_id": "sweep",
    "id": "sweep", "task_id": "sweep", "agent_id": "sweep", "job_id": "sweep",
    "interaction_id": "sweep", "model": "sonder", "tier": "code", "persona": "default",
    "operations_json": '[{"path":"batch.txt","content":"x","mode":"create"}]',
    "inputs_json": '["notes.txt"]', "files_json": '{"a.py":"print(1)"}',
    "actions_json": '[{"type":"sleep","seconds":0}]', "arguments_json": "{}",
    "call_id": "0123456789abcdef", "nonce": "apv_0000000000000000",
    "confirm": "", "patch": PATCH, "symbol": "needle", "new_name": "thread",
    "format": "json", "encoding": "utf-8", "recipe": "note", "goal": "sweep probe",
    "scope": "", "filter": "", "category": "", "hint": "", "context": "",
    "signal": "accepted", "verdict": "accepted", "feedback": "good",
}
_INT_VALUES = {"max_steps": 1, "timeout": 5, "timeout_seconds": 5, "limit": 5,
               "count": 1, "max_results": 5, "max_entries": 50, "depth": 1,
               "start_line": 1, "end_line": 3, "max_bytes": 4096, "max_output": 4096,
               "num_predict": 8, "sample_limit": 2, "max_cycles": 1, "seconds": 0,
               "port": 0, "workers": 1, "max_agents": 1, "requested_agents": 1}
_COMMAND_VALUES = {
    "/json_patch": {"operations_json": '[{"op":"replace","path":"/a","value":2}]'},
    "/file_edit": {"old": "needle", "new": "thread"},
    "/text_patch": {"root": ".", "patch": PATCH},
    "/file_read_range": {"path": "notes.txt"},
    "/directory_create": {"path": "made"},
    "/file_delete": {"path": "notes.txt"},
    "/task_delete": {"task_id": "sweep"},
    "/help": {},
}


def _value_for(name: str, kind: str, command_name: str):
    override = _COMMAND_VALUES.get(command_name, {})
    if name in override:
        return override[name]
    if kind in ("int", "integer"):
        return _INT_VALUES.get(name, 1)
    if kind in ("num", "number", "float"):
        return 1.0
    if kind in ("bool", "boolean"):
        return False
    if name in _NAME_VALUES:
        return _NAME_VALUES[name]
    for hint, value in (("path", "notes.txt"), ("json", "{}"), ("dir", "."),
                        ("id", "sweep"), ("name", "sweep"), ("text", "sweep probe"),
                        ("prompt", "sweep probe"), ("query", "needle")):
        if hint in name:
            return value
    return "sweep"


def _arguments_for(command) -> dict:
    args = {}
    for param in command.params:
        required = bool(getattr(param, "required", False))
        name = param.name
        if required or name in _INT_VALUES and name in ("max_steps", "timeout", "timeout_seconds", "max_cycles"):
            args[name] = _value_for(name, str(getattr(param, "type", "str")), command.name)
    return args


def _slash_line(command, args: dict) -> str | None:
    parts = [command.name]
    for key, value in args.items():
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        else:
            text = str(value)
            if "\n" in text:
                return None
            if " " in text or not text:
                if '"' in text:
                    return None
                text = '"%s"' % text
        parts.append("%s=%s" % (key, text))
    return " ".join(parts)


# --- classification --------------------------------------------------------------


_GATE = re.compile(r"(^refused /|^refused [a-z_]+:|ERROR: HOST POLICY|permission gate refused|"
                   r"permission_denied|refused by the active permission gate|^skipped /|"
                   r"not allowed by the repository read-only policy|is not allowed for this "
                   r"autonomous run|cannot be called by an agent)", re.I)
_ARGUMENT = re.compile(r"(no (?:such|active|unique|unambiguous|workflow|checklist|task|interaction|"
                       r"weather location|validated loopback)|not found|was not found|"
                       r"must (?:be|describe|match|name)|is not valid|invalid |not valid JSON|"
                       r"login required|not a git repository|git root probe failed|"
                       r"cannot be inspected|supports (?:SQLite|only)|is required|"
                       r"private/local network|already exists|accepts (?:PNG|only)|"
                       r"no sufficiently relevant|not an allowed value|rejected:|"
                       r"already exists|destination exists|file exists|unsupported (?:project|script)|"
                       r"could not auto-detect|not a supported|not supported|"
                       r"\bNotFound\b|\bFileExistsError\b|\bValueError\b|\bInvalidInput\b)", re.I)
_FAULT_CODES = re.compile(r"^(KeyError|TypeError|AttributeError|IndexError|AssertionError|"
                          r"UnboundLocalError|NameError|RecursionError|ZeroDivisionError):")
_USAGE = re.compile(r"(^usage:|\busage:|missing \d+ required|takes \d+ positional|"
                    r"unexpected keyword|required argument|must be a JSON|is not a catalogued|"
                    r"unknown (?:command|tool|argument|key)|does not accept|"
                    r"shorter than minLength|is required$|needs operations)", re.I)
_CONTAINMENT = re.compile(r"(outside allowed roots|outside every authorized root|protected Sonder|"
                          r"refusing to (?:read|mutate|delete)|contains an empty, dot, or parent|"
                          r"path must be relative|no host-selected project root|"
                          r"has no project to work on|outside the (?:project|workspace))", re.I)
_MODEL = re.compile(r"(ollama|model (?:call|turn|request|endpoint|gateway|backend)|no model|"
                    r"generate\(|connection refused|could not connect|127\.0\.0\.1:9\b|"
                    r"model .* is not (?:available|loaded)|cannot reach the model|"
                    r"unreachable|max retries|remote end closed|Errno 111)", re.I)
_DEPENDENCY = re.compile(r"(not installed|not found on PATH|No such file or directory|"
                         r"not available on this host|No module named|executable .* not|"
                         r"is not approved for autonomous runs|program .* not found|"
                         r"command not found|toolchain .* missing)", re.I)
_UNAVAILABLE = re.compile(r"(disabled|are off|is off|not enabled|opt[- ]in|consent|"
                          r"SONDER_WEB_TOOLS|SONDER_ALLOW_CLOUD|not configured|"
                          r"not available in this process|requires? .*SONDER_|"
                          r"opt_in_required|required_environment|urlopen error|"
                          r"read operation timed out|network is unreachable)", re.I)


def classify(text: str, *, exception: BaseException | None = None,
             is_error: bool | None = None) -> str:
    if isinstance(exception, SweepTimeout):
        return "timeout"
    if exception is not None:
        message = str(exception)
        if _GATE.search(message) or "refused" in message.lower() and "permission" in message.lower():
            return "gated"
        return "crash"
    body = str(text or "")
    head = body.lstrip()[:400]
    if _GATE.search(head):
        return "gated"
    if _FAULT_CODES.match(head):
        return "crash"
    errored = head.startswith("ERROR") or bool(is_error)
    if _USAGE.search(head):
        return "usage"
    if not errored:
        return "ok"
    if _CONTAINMENT.search(head):
        return "containment"
    if _MODEL.search(head):
        return "model"
    if _DEPENDENCY.search(head):
        return "dependency"
    if _UNAVAILABLE.search(head):
        return "unavailable"
    if _ARGUMENT.search(head):
        return "argument"
    return "error"


# --- watchdog ---------------------------------------------------------------------


@contextlib.contextmanager
def watchdog(seconds: float):
    """Raise ``SweepTimeout`` in the main thread after ``seconds``."""
    if threading.current_thread() is not threading.main_thread() or seconds <= 0:
        yield
        return

    def _fire(signum, frame):
        del signum, frame
        raise SweepTimeout("no answer within %.0fs" % seconds)

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# --- the checkout guard -----------------------------------------------------------


def checkout_state(repo_root: str = REPO_ROOT) -> dict:
    """Every path git reports as changed, untracked or ignored in the checkout,
    keyed to its status and, for a regular file, a digest of its content.

    The sweep must leave the checkout exactly as it found it: its probes run
    the real runtime, and a runtime that anchors a writable file to its own
    module directory writes into the repository unless the harness redirects
    it. Comparing two of these states after a run names every path the sweep
    touched. An empty mapping means git could not answer, and the guard has
    nothing to say.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain=v1", "--untracked-files=all",
             "--ignored=matching"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    state = {}
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        status, entry = line[:2], line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if "__pycache__" in entry or entry.endswith((".pyc", ".pyo")):
            continue
        digest = ""
        full = os.path.join(repo_root, entry)
        if os.path.isfile(full):
            import hashlib
            hasher = hashlib.sha256()
            with open(full, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 16), b""):
                    hasher.update(block)
            digest = hasher.hexdigest()
        state[entry] = "%s:%s" % (status, digest)
    return state


def checkout_changes(before: dict, after: dict) -> list:
    """The paths whose checkout state differs between ``before`` and ``after``."""
    return sorted(path for path in set(before) | set(after)
                  if before.get(path) != after.get(path))


# --- the sweep --------------------------------------------------------------------


class Sweep:
    def __init__(self, *, out_dir: str, mode: str, surfaces: tuple, only: tuple,
                 timeout: float, live_model: bool) -> None:
        self.out_dir = out_dir
        self.mode = mode
        self.surfaces = surfaces
        self.only = set(only)
        self.timeout = timeout
        self.live_model = live_model
        self.records: list[dict] = []
        self.root = tempfile.mkdtemp(prefix="sonder-sweep-")
        self.paths = _prepare_environment(self.root)
        self.checkout_before = checkout_state()
        self._cwd = os.getcwd()
        self._redact = None
        self._server = None
        self._catalog = None

    # -- setup ---------------------------------------------------------------

    def boot(self) -> None:
        from sonder_runtime.platform import paths as runtime_paths
        from sonder_runtime.platform.logging import Redactor

        runtime_paths.configure_home(self.paths["home"])
        self._redact = Redactor().redact
        import server
        import permission_modes
        import command_catalog
        from sonder_runtime.adapters.filesystem import file_ops

        self._server = server
        from sonder_runtime.bootstrap.legacy_interfaces import configure_legacy_interfaces

        configure_legacy_interfaces(server)
        workspace = self.paths["workspace"]
        file_ops.workspace_root = lambda: __import__("pathlib").Path(workspace)
        server.file_ops = file_ops
        self._redirect_module_roots()
        # Generators that build their output path from the working directory
        # (``artifacts/generated/<name>``, ``games/<name>``) must land in the
        # sweep's home, not wherever the sweep was launched from.
        os.chdir(self.paths["home"])
        if not self.live_model:
            server._make_generate = _stub_generate
        permission_modes.set_mode(self.mode)
        self._catalog = list(command_catalog.catalog())
        if self.only:
            self._catalog = [c for c in self._catalog if c.name in self.only
                             or any(a in self.only for a in c.aliases)]

    # Every module that anchors a writable file to its own directory rather
    # than the runtime home. ``file_ops`` is redirected above; these resolve
    # their own ``workspace_root`` from ``__file__`` and refuse any path
    # outside it, so an environment variable cannot move them. Left alone,
    # a probe that edited the standing instructions appended to the
    # checkout's own ``system_profile.md``, ``tune_emotion_vectors`` rewrote
    # the tracked ``emotion_vectors.json``, and the workflow store, the asset
    # and game generators, self-heal and the code runner all had the
    # repository as their root. ``game_forge`` delegates to ``assetgen``.
    MODULE_ROOTS = (
        "sonder_runtime.platform.system_profile",
        "emotion_vectors",
        "sonder_runtime.adapters.filesystem.workflow_store",
        "assetgen",
        "self_heal",
        "sonder_runtime.adapters.execution_tools.code_runner",
    )

    def _redirect_module_roots(self) -> None:
        import importlib

        for module_name in self.MODULE_ROOTS:
            module = importlib.import_module(module_name)
            root = os.path.join(self.paths["home"], "roots", module_name.rsplit(".", 1)[-1])
            os.makedirs(root, exist_ok=True)
            module.workspace_root = lambda root=root: root

    def audit_checkout(self) -> list:
        """Record every path the sweep changed in the checkout as a harness
        finding (class ``error``) and return the paths."""
        changed = checkout_changes(self.checkout_before, checkout_state())
        for entry in changed:
            self.record("harness", "checkout", entry,
                        "ERROR: the sweep changed the checkout: %s" % entry)
        return changed

    # -- recording -----------------------------------------------------------

    def record(self, surface: str, command: str, invocation: str, text: str = "",
               *, exception: BaseException | None = None, is_error: bool | None = None,
               elapsed: float = 0.0, status: str | None = None, extra: dict | None = None) -> dict:
        klass = status or classify(text, exception=exception, is_error=is_error)
        excerpt = str(text or "")
        if exception is not None and not excerpt:
            excerpt = "%s: %s" % (type(exception).__name__, exception)
        excerpt = excerpt.replace("\n", " ")[:EXCERPT_CHARS]
        if self._redact is not None:
            excerpt = self._redact(excerpt)
        row = {
            "surface": surface, "command": command, "invocation": invocation,
            "class": klass, "elapsed_ms": int(elapsed * 1000), "excerpt": excerpt,
        }
        if extra:
            row.update(extra)
        self.records.append(row)
        return row

    def _run(self, surface: str, command: str, invocation: str, fn) -> dict:
        started = time.monotonic()
        try:
            with watchdog(self.timeout):
                result = fn()
        except SweepTimeout as exc:
            return self.record(surface, command, invocation, exception=exc,
                               elapsed=time.monotonic() - started)
        except BaseException as exc:  # noqa: BLE001 - a crash is a finding, not a stop
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return self.record(surface, command, invocation, "".join(
                traceback.format_exception_only(type(exc), exc)).strip(),
                exception=exc, elapsed=time.monotonic() - started)
        text, is_error = result if isinstance(result, tuple) else (result, None)
        return self.record(surface, command, invocation, "" if text is None else str(text),
                           is_error=is_error, elapsed=time.monotonic() - started)

    # -- surfaces ------------------------------------------------------------

    def sweep_control(self) -> None:
        server = self._server
        for command in self._catalog:
            args = _arguments_for(command)
            line = _slash_line(command, args)
            if line is None:
                self.record("control", command.name, "", status="skipped",
                            extra={"note": "multi-line argument"})
                continue
            self._run("control", command.name, line, lambda line=line: (
                lambda out: ("(not a control command)" if out is None else out)
            )(server.control_command(line)))

    def sweep_console(self) -> None:
        import builtins
        import sonder_runtime.interfaces.repl.repl as sonder_repl

        lines = []
        for command in self._catalog:
            if command.name in ("/exit", "/quit", "/q") or "/exit" in command.aliases:
                self.record("console", command.name, command.name, "ends the session",
                            status="ok", extra={"note": "session-ending command, not fed"})
                continue
            line = _slash_line(command, _arguments_for(command))
            if line is None:
                self.record("console", command.name, "", status="skipped",
                            extra={"note": "multi-line argument"})
                continue
            lines.append((command.name, line))
        buffer = io.StringIO()
        marks: list[tuple[str, str, int, float]] = []
        feed = iter(lines)
        state = {"current": None}

        def read_input(*_args, **_kwargs):
            signal.setitimer(signal.ITIMER_REAL, 0)
            if state["current"] is not None:
                name, line, start, began = state["current"]
                marks.append((name, line, start, buffer.tell(), time.monotonic() - began))
            try:
                name, line = next(feed)
            except StopIteration:
                state["current"] = None
                return "/exit"
            state["current"] = (name, line, buffer.tell(), time.monotonic())
            signal.setitimer(signal.ITIMER_REAL, self.timeout)
            return line

        def _fire(signum, frame):
            del signum, frame
            raise SweepTimeout("console line exceeded %.0fs" % self.timeout)

        saved = {
            "read": sonder_repl._read_input, "banner": sonder_repl._startup_banner,
            "reload": sonder_repl._maybe_live_reload, "input": builtins.input,
        }
        sonder_repl._read_input = read_input
        sonder_repl._startup_banner = lambda *a, **k: ""
        sonder_repl._maybe_live_reload = lambda: None
        builtins.input = lambda *a, **k: "n"
        previous_handler = signal.signal(signal.SIGALRM, _fire)
        try:
            while True:
                try:
                    with contextlib.redirect_stdout(buffer):
                        sonder_repl.main()
                    if state["current"] is None:
                        break
                    # A branch ended the loop early (a session-ending command
                    # or an uncaught return): note the line and carry on with
                    # the rest of the feed in a fresh loop.
                    name, line, start, began = state["current"]
                    marks.append((name, line, start, buffer.tell(), time.monotonic() - began))
                    state["current"] = None
                    continue
                except SweepTimeout as exc:
                    # Note the stuck line and resume the loop from the next one.
                    if state["current"] is not None:
                        name, line, start, began = state["current"]
                        marks.append((name, line, start, buffer.tell(), time.monotonic() - began))
                        self.record("console", name, line, exception=exc,
                                    elapsed=time.monotonic() - began)
                        state["current"] = None
                    continue
                except SystemExit:
                    break
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            sonder_repl._read_input = saved["read"]
            sonder_repl._startup_banner = saved["banner"]
            sonder_repl._maybe_live_reload = saved["reload"]
            builtins.input = saved["input"]
        text = buffer.getvalue()
        for name, line, start, end, elapsed in marks:
            self.record("console", name, line, text[start:end], elapsed=elapsed)

    def sweep_mcp(self) -> None:
        server = self._server
        for command in self._catalog:
            if not command.tool:
                continue
            args = _arguments_for(command)

            def call(command=command, args=args):
                result = asyncio.run(server.mcp.call_tool(command.tool, dict(args)))
                content = getattr(result, "content", None) or []
                text = "\n".join(getattr(block, "text", "") for block in content)
                return text, bool(getattr(result, "isError", False))

            self._run("mcp", command.name, "%s %s" % (command.tool, json.dumps(args, sort_keys=True)), call)

    def sweep_native(self) -> None:
        from sonder_runtime.bootstrap import app as bootstrap_app
        from sonder_runtime.bootstrap.native_mcp import native_tool_registry, run_native_mcp

        application = bootstrap_app.build_application()
        self._server._APP_GRAPH = application
        for descriptor in native_tool_registry().list_all():
            if self.only and ("/" + descriptor.name) not in self.only:
                continue
            schema = descriptor.input_schema or {}
            props = schema.get("properties", {}) or {}
            args = {}
            for name in schema.get("required", []) or []:
                kind = str((props.get(name) or {}).get("type", "string"))
                args[name] = _value_for(name, kind, "/" + descriptor.name)
            for name in ("timeout", "max_seconds", "timeout_seconds"):
                if name in props and name not in args:
                    args[name] = 5

            def call(name=descriptor.name, args=args):
                initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}}}
                request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": name, "arguments": args}}
                output = io.StringIO()
                run_native_mcp(
                    application,
                    input_stream=io.StringIO(json.dumps(initialize) + "\n" + json.dumps(request) + "\n"),
                    output_stream=output,
                )
                rows = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
                reply = rows[1] if len(rows) > 1 else rows[-1]
                if "error" in reply:
                    return "protocol error %s: %s" % (reply["error"].get("code"), reply["error"].get("message")), True
                result = reply.get("result", {})
                text = result.get("output", "")
                if result.get("isError") and result.get("error"):
                    text = "%s: %s" % (result["error"], text)
                return text, bool(result.get("isError"))

            self._run("native", "/" + descriptor.name,
                      "%s %s" % (descriptor.name, json.dumps(args, sort_keys=True)), call)

    def sweep_http(self) -> None:
        import sonder_runtime.interfaces.http.serve as ts

        ts._maybe_live_reload = lambda: None
        ts.API_KEY = ""
        ts.AUTH_MODE = "local-open"
        ts.REQUIRE_ACCOUNT = False
        httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]

        def request(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=max(5, self.timeout))
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
            conn.close()
            return response.status, payload

        try:
            for path in (
                "/v1/commands", "/v1/commands/help?name=/stats", "/v1/commands/complete?q=/st",
                "/v1/permission-mode", "/v1/models", "/v1/sonder/status", "/v1/jobs",
                "/v1/extensions", "/v1/fanout", "/v1/compute/snapshot",
                "/v1/observability/trace", "/v1/admin/updates/status",
                "/v1/local/server-log", "/v1/sessions/", "/v1/sonder/feed",
            ):
                def get(path=path):
                    status, payload = request("GET", path)
                    text = payload.decode("utf-8", errors="replace")[:EXCERPT_CHARS]
                    if status >= 500:
                        return "ERROR: HTTP %d %s" % (status, text), True
                    if status >= 400:
                        return "ERROR: HTTP %d not found or not allowed here: %s" % (status, text), True
                    return "HTTP %d %s" % (status, text), False

                self._run("http", "GET " + path.split("?")[0], "GET " + path, get)
            for command in self._catalog:
                line = _slash_line(command, _arguments_for(command))
                if line is None:
                    self.record("http", command.name, "", status="skipped",
                                extra={"note": "multi-line argument"})
                    continue

                def post(line=line):
                    body = json.dumps({"model": "sonder", "messages": [
                        {"role": "user", "content": line}]}).encode("utf-8")
                    status, payload = request("POST", "/v1/chat/completions", body)
                    text = payload.decode("utf-8", errors="replace")
                    if status >= 500:
                        return "ERROR: HTTP %d %s" % (status, text[:EXCERPT_CHARS]), True
                    if status != 200:
                        return "ERROR: HTTP %d %s" % (status, text[:EXCERPT_CHARS]), True
                    try:
                        parsed = json.loads(text)
                        content = parsed["choices"][0]["message"]["content"]
                    except Exception:
                        content = text
                    return str(content), None

                self._run("http", command.name, "POST /v1/chat/completions " + line, post)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def sweep_agent(self) -> None:
        import tool_capabilities

        server = self._server
        names = sorted(tool_capabilities.dispatch_names(server._agent_dispatch))
        by_tool = {c.tool: c for c in self._catalog if c.tool}
        workspace = self.paths["workspace"]
        for name in names:
            command = by_tool.get(name)
            if self.only and (command is None or command.name not in self.only):
                continue
            args = _arguments_for(command) if command is not None else {}
            for read_only in (True, False):
                def call(name=name, args=args, read_only=read_only):
                    return server._agent_dispatch(
                        name, dict(args), read_only=read_only,
                        repository_extra_roots=workspace,
                    )

                self._run("agent" if not read_only else "agent-ro", "/" + name,
                          "%s %s" % (name, json.dumps(args, sort_keys=True)), call)

    def sweep_router(self) -> None:
        from sonder_runtime.interfaces.repl import command_router

        cases = _documented_phrases()
        for phrase, expected, source_doc in cases:
            def resolve(phrase=phrase):
                return command_router.resolve(phrase)

            started = time.monotonic()
            try:
                with watchdog(self.timeout):
                    resolved = resolve()
                    explanation = command_router.explain(phrase)
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                self.record("router", expected or "(none)", phrase, exception=exc,
                            elapsed=time.monotonic() - started, extra={"doc": source_doc})
                continue
            observed = resolved or None
            if expected is None:
                matched = observed is None
            elif expected.endswith("<name>"):
                matched = bool(observed) and observed.startswith(expected[:-6].rstrip())
            else:
                matched = bool(observed) and (observed == expected or observed.split()[0] == expected.split()[0])
            self.record(
                "router", expected or "(none)", phrase,
                "resolved=%s source=%s" % (observed, (explanation or {}).get("source")),
                status="ok" if matched else "error",
                elapsed=time.monotonic() - started,
                extra={"doc": source_doc, "expected": expected, "resolved": observed,
                       "route_source": (explanation or {}).get("source")},
            )
        # Every catalogued command by its own name, spoken plainly: the router
        # may resolve it or fall through, never crash; a resolution must name
        # the same command.
        for command in self._catalog:
            phrase = command.name.lstrip("/").replace("_", " ")
            started = time.monotonic()
            try:
                with watchdog(self.timeout):
                    resolved = command_router.resolve(phrase)
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                self.record("router-names", command.name, phrase, exception=exc,
                            elapsed=time.monotonic() - started)
                continue
            head = (resolved or "").split()[0] if resolved else None
            status = "ok" if (head is None or _same_command(command, resolved)) else "error"
            self.record("router-names", command.name, phrase, "resolved=%s" % resolved,
                        status=status, elapsed=time.monotonic() - started,
                        extra={"resolved": resolved})

    def sweep_cli(self) -> None:
        env = dict(os.environ)
        env["SONDER_HOME"] = self.paths["home"]
        env["PYTHONPATH"] = REPO_ROOT
        subcommands = ("preflight", "doctor", "status", "diagnostics", "config", "migrate",
                       "backup", "restore", "smoke", "serve", "repl", "mcp", "drain",
                       "update", "rotate-key", "eval-history")
        invocations = [("--version", ["--version"])]
        invocations += [("%s --help" % sub, [sub, "--help"]) for sub in subcommands]
        invocations += [
            ("status", ["status"]), ("config", ["config"]), ("preflight", ["preflight"]),
            ("doctor", ["doctor"]), ("diagnostics", ["diagnostics"]), ("migrate", ["migrate"]),
            ("backup list", ["backup", "list"]), ("eval-history", ["eval-history"]),
        ]
        for label, argv in invocations:
            def run(argv=argv):
                proc = subprocess.run(
                    [sys.executable, "-m", "sonder_runtime", *argv],
                    cwd=self.paths["workspace"], env=env,
                    capture_output=True, text=True, timeout=max(30, self.timeout * 3),
                )
                text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
                if proc.returncode not in (0, 1, 2):
                    return "ERROR: exit %d %s" % (proc.returncode, text[-EXCERPT_CHARS:]), True
                if proc.returncode == 0:
                    return "exit 0 " + text[:EXCERPT_CHARS].replace("usage:", "help:"), False
                if proc.returncode == 2 and "usage:" in text:
                    return "exit 2 %s" % text[:EXCERPT_CHARS], False
                if proc.returncode == 1 and re.search(r"ollama|model", text, re.I):
                    return "ERROR: exit 1 (ollama unreachable) %s" % text[:EXCERPT_CHARS], True
                return "exit %d %s" % (proc.returncode, text[:EXCERPT_CHARS]), False

            self._run("cli", label, "python -m sonder_runtime " + " ".join(argv), run)

    # -- output --------------------------------------------------------------

    def write(self) -> dict:
        os.makedirs(self.out_dir, exist_ok=True)
        summary = {"mode": self.mode, "surfaces": list(self.surfaces),
                   "live_model": self.live_model, "records": len(self.records),
                   "by_surface": {}, "by_class": {}}
        for row in self.records:
            summary["by_class"][row["class"]] = summary["by_class"].get(row["class"], 0) + 1
            per = summary["by_surface"].setdefault(row["surface"], {})
            per[row["class"]] = per.get(row["class"], 0) + 1
        with open(os.path.join(self.out_dir, "sweep-%s.json" % self.mode), "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "records": self.records}, handle, indent=1, sort_keys=True)
        with open(os.path.join(self.out_dir, "sweep-%s.md" % self.mode), "w", encoding="utf-8") as handle:
            handle.write(render(summary, self.records))
        return summary

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.chdir(self._cwd)
        shutil.rmtree(self.root, ignore_errors=True)


def _same_command(command, resolved: str) -> bool:
    """Whether ``resolved`` runs ``command``: its own name, an alias, or a native
    slash whose branch fronts the same tool (``autopilot cancel`` resolves to
    ``/autopilot cancel``, ``master status`` to ``/agents``)."""
    import command_catalog

    head = resolved.split()[0]
    if head in {command.name, *command.aliases}:
        return True
    other = command_catalog.by_name(head)
    if other is not None and command.tool and other.tool == command.tool:
        return True
    try:
        fronted = set(command_catalog.console_tools().get(head, ()))
    except Exception:
        fronted = set()
    if command.tool and command.tool in fronted:
        return True
    stem = set(command.name.lstrip("/").replace("_", " ").split())
    words = set(re.findall(r"[a-z0-9]+", resolved.lower()))
    return stem <= words


def _documented_phrases() -> list[tuple[str, str | None, str]]:
    """(phrase, expected slash or None, document) from the natural-language guides."""
    cases: list[tuple[str, str | None, str]] = []
    docs = (
        ("docs/NATURAL_LANGUAGE_TOOLS.md", True),
        ("docs/NATURAL_LANGUAGE_CAPABILITY_QUERIES.md", True),
    )
    row_re = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|")
    for relative, _ in docs:
        path = os.path.join(REPO_ROOT, relative)
        if not os.path.exists(path):
            continue
        negative = False
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                if line.startswith("|"):
                    if "You type" in line and "falls through" in line:
                        negative = True
                        continue
                    if "You type" in line:
                        negative = False
                        continue
                    if set(line.replace("|", "").strip()) <= {"-", " "}:
                        continue
                    match = row_re.match(line)
                    if not match:
                        continue
                    phrases = re.findall(r"`([^`]+)`", match.group(1))
                    if negative:
                        for phrase in phrases:
                            cases.append((phrase, None, relative))
                        continue
                    runs = re.findall(r"`(/[^`]+)`", match.group(2))
                    if not runs:
                        continue
                    expected = runs[0]
                    for phrase in phrases:
                        cases.append((phrase, expected, relative))
    return cases


def render(summary: dict, records: list[dict]) -> str:
    lines = ["# Surface sweep (mode: %s)" % summary["mode"], ""]
    lines.append("records: %d; model: %s" % (summary["records"], "live" if summary["live_model"] else "stubbed"))
    lines.append("")
    lines.append("| surface | " + " | ".join(CLASSES) + " |")
    lines.append("|---|" + "---|" * len(CLASSES))
    for surface, counts in sorted(summary["by_surface"].items()):
        lines.append("| %s | %s |" % (surface, " | ".join(str(counts.get(c, 0)) for c in CLASSES)))
    lines.append("")
    attention = [r for r in records if r["class"] in ("crash", "timeout", "error")]
    lines.append("## Needs reading (%d)" % len(attention))
    lines.append("")
    for row in attention:
        lines.append("- **%s** `%s` %s — %s: %s" % (
            row["class"], row["surface"], row["command"], row["invocation"][:80], row["excerpt"][:160]))
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", default="manual", choices=("plan", "manual", "acceptEdits", "auto"))
    parser.add_argument("--surfaces", default=",".join(SURFACES))
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--live-model", action="store_true")
    args = parser.parse_args(argv)
    surfaces = tuple(s.strip() for s in args.surfaces.split(",") if s.strip())
    unknown = [s for s in surfaces if s not in SURFACES]
    if unknown:
        parser.error("unknown surfaces: %s" % ", ".join(unknown))
    sweep = Sweep(out_dir=args.out, mode=args.mode, surfaces=surfaces, only=tuple(args.only),
                  timeout=args.timeout, live_model=args.live_model)
    try:
        sweep.boot()
        for surface in surfaces:
            started = time.monotonic()
            getattr(sweep, "sweep_" + surface)()
            print("%s: %d records in %.1fs" % (
                surface, sum(1 for r in sweep.records if r["surface"].startswith(surface)),
                time.monotonic() - started), file=sys.stderr, flush=True)
        changed = sweep.audit_checkout()
        summary = sweep.write()
    finally:
        sweep.close()
    print(json.dumps(summary["by_class"], sort_keys=True))
    if changed:
        print("the sweep changed the checkout: %s" % ", ".join(changed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
