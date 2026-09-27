"""clangd navigation for the build-fix loop (lane D, optional).

Two adapters over one bounded JSON-RPC stdio client:

* ``ClangdNavigator`` implements the fix loop's ``BuildNavigator`` port:
  position-based ``textDocument/definition`` and ``textDocument/hover`` at
  diagnostic locations, and ``publishDiagnostics`` prefetch for a file;
* ``ClangdSymbolTransport`` implements the REPO-004 ``LspTransport`` /
  ``LspSession`` symbol-query contract (``workspace/symbol`` then
  ``textDocument/definition``) so ``MultiRepositoryNavigator`` can use clangd.

Containment and bounds (docs/security/BUILD-TOOLS.md):

* clangd runs as its own process group (POSIX session / Windows process
  group) with a scrubbed environment whose HOME and cache directories point
  at a private per-session directory, and is killed as a tree on close, on
  idle timeout and on any protocol violation;
* flags: ``--background-index=false`` (no index written into the project),
  ``--enable-config=false`` unless the operator set
  ``SONDER_BUILD_CLANGD_CONFIG=1`` (a project ``.clangd`` is ignored by
  default), ``--log=error``, ``--clang-tidy=false``, no ``--query-driver``
  (clangd never executes the project's compilers);
* frames are bounded (header block, header count, content length); an
  oversized or malformed frame ends the session;
* only files inside the project root, reached without links, are opened;
  locations outside the root come back as ``[external]/<name>`` labels.

The session is tracked as a durable job of kind ``tool.lsp`` when a job
registry is supplied. Nothing here starts until a navigator is first used.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import queue
import selectors
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, unquote, urlsplit

from ..process_termination import terminate_process_tree

logger = logging.getLogger(__name__)

LSP_JOB_KIND = "tool.lsp"
MAX_HEADER_BYTES = 8 * 1024
MAX_HEADER_LINES = 8
DEFAULT_MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_OPEN_FILE_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTICS_PER_FILE = 200
MAX_TRACKED_FILES = 64
MAX_CONTEXT_ITEMS = 8
MAX_HOVER_CHARS = 600
MAX_MESSAGE_CHARS = 400
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_IDLE_SECONDS = 300.0
MAX_SYMBOL_RESULTS = 100
_SEVERITY = {1: "error", 2: "warning", 3: "note", 4: "note"}
_LANGUAGE_IDS = {".c": "c", ".m": "objective-c", ".mm": "objective-cpp"}


class ClangdError(RuntimeError):
    """The clangd session failed, timed out or broke the protocol bounds."""


class FrameError(ClangdError):
    """A frame violated the header or size bounds."""


# --- framing ---------------------------------------------------------------------------


def encode_frame(message: Mapping[str, Any]) -> bytes:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class FrameReader:
    """Incremental LSP frame parser with hard bounds (pure; no I/O)."""

    def __init__(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
                 max_header_bytes: int = MAX_HEADER_BYTES) -> None:
        self._max_frame = int(max_frame_bytes)
        self._max_header = int(max_header_bytes)
        self._buffer = bytearray()
        self._expected: int | None = None

    def feed(self, data: bytes) -> list[dict]:
        self._buffer.extend(data)
        messages: list[dict] = []
        while True:
            if self._expected is None:
                end = self._buffer.find(b"\r\n\r\n")
                if end < 0:
                    if len(self._buffer) > self._max_header:
                        raise FrameError("LSP header block exceeds %d bytes" % self._max_header)
                    return messages
                if end > self._max_header:
                    raise FrameError("LSP header block exceeds %d bytes" % self._max_header)
                header = bytes(self._buffer[:end]).decode("ascii", errors="strict")
                del self._buffer[:end + 4]
                self._expected = self._content_length(header)
            if len(self._buffer) < self._expected:
                return messages
            body = bytes(self._buffer[:self._expected])
            del self._buffer[:self._expected]
            self._expected = None
            try:
                message = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise FrameError("LSP frame is not UTF-8 JSON") from None
            if not isinstance(message, dict):
                raise FrameError("LSP frame is not a JSON object")
            messages.append(message)

    def _content_length(self, header: str) -> int:
        lines = header.split("\r\n")
        if len(lines) > MAX_HEADER_LINES:
            raise FrameError("too many LSP header lines")
        length = None
        for line in lines:
            name, sep, value = line.partition(":")
            if not sep:
                raise FrameError("malformed LSP header line")
            if name.strip().lower() == "content-length":
                value = value.strip()
                if not value.isdigit() or len(value) > 12:
                    raise FrameError("malformed Content-Length")
                length = int(value)
        if length is None:
            raise FrameError("LSP frame without Content-Length")
        if length > self._max_frame:
            raise FrameError("LSP frame of %d bytes exceeds %d" % (length, self._max_frame))
        return length


# --- paths -----------------------------------------------------------------------------


def path_to_uri(path: str) -> str:
    absolute = os.path.abspath(path)
    if os.name == "nt":
        return "file:///" + quote(absolute.replace("\\", "/"), safe="/:")
    return "file://" + quote(absolute, safe="/")


def uri_to_path(uri: str) -> str:
    parts = urlsplit(str(uri or ""))
    if parts.scheme != "file":
        return ""
    path = unquote(parts.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:].replace("/", "\\")
    return path


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(child: str, parent: str) -> bool:
    child_n, parent_n = _norm(child), _norm(parent)
    return child_n == parent_n or child_n.startswith(parent_n.rstrip(os.sep) + os.sep)


def label_for(path: str, project_root: str) -> str:
    """Project-relative label, or ``[external]/<name>``; never a host path."""
    if path and _inside(os.path.realpath(path), project_root):
        return os.path.relpath(os.path.realpath(path), project_root).replace(os.sep, "/")
    return "[external]/" + (os.path.basename(path) or "unknown")


# --- the JSON-RPC client -----------------------------------------------------------------


def _scrubbed_env(private_dir: str, executable: str) -> dict[str, str]:
    folder = os.path.dirname(executable)
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        return {
            "SystemRoot": system_root, "PATH": os.pathsep.join([folder, system_root + r"\System32"]),
            "TEMP": private_dir, "TMP": private_dir, "LOCALAPPDATA": private_dir,
            "APPDATA": private_dir, "USERPROFILE": private_dir,
        }
    return {
        "PATH": os.pathsep.join(dict.fromkeys([folder, "/usr/bin", "/bin"])),
        "HOME": private_dir, "XDG_CACHE_HOME": os.path.join(private_dir, "cache"),
        "XDG_CONFIG_HOME": os.path.join(private_dir, "config"), "TMPDIR": private_dir,
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
    }


def clangd_argv(executable: str, *, compile_commands_dir: str, enable_config: bool,
                max_results: int = MAX_SYMBOL_RESULTS) -> list[str]:
    argv = [
        executable,
        "--background-index=false",
        "--enable-config=%s" % ("true" if enable_config else "false"),
        "--log=error",
        "--clang-tidy=false",
        "--header-insertion=never",
        "--pch-storage=memory",
        "--limit-results=%d" % max(1, min(int(max_results), 1000)),
        "-j=1",
    ]
    if compile_commands_dir:
        argv.append("--compile-commands-dir=%s" % compile_commands_dir)
    return argv


class JsonRpcStdioClient:
    """A bounded LSP client over one process-group-contained server process."""

    def __init__(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str],
                 max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
                 use_thread_reader: bool | None = None,
                 thread_factory: Callable[..., Any] | None = None,
                 on_notification: Callable[[dict], None] | None = None,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env)
        self._reader = FrameReader(max_frame_bytes=max_frame_bytes)
        self._timeout = float(request_timeout)
        self._threaded = (os.name == "nt") if use_thread_reader is None else bool(use_thread_reader)
        self._thread_factory = thread_factory
        self._on_notification = on_notification
        self._popen = popen
        self._proc = None
        self._lock = threading.RLock()
        self._next_id = 0
        self._pending: collections.deque[dict] = collections.deque()
        self._inbox: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)
        self._selector = None
        self._closed = False
        self._failure = ""

    @property
    def pid(self) -> int | None:
        return getattr(self._proc, "pid", None)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._closed

    def start(self) -> None:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
            "cwd": self._cwd, "env": self._env, "close_fds": True, "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            kwargs["start_new_session"] = True
        self._proc = self._popen(self._argv, **kwargs)
        if self._threaded:
            factory = self._thread_factory
            if factory is None:
                from ...platform.runtime_threads import Thread as factory
            thread = factory(target=self._pump, name="sonder-clangd-reader", daemon=True)
            thread.start()
        else:
            os.set_blocking(self._proc.stdout.fileno(), False)
            self._selector = selectors.DefaultSelector()
            self._selector.register(self._proc.stdout, selectors.EVENT_READ)

    def _pump(self) -> None:
        stream = self._proc.stdout
        while True:
            try:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            except (OSError, ValueError):
                chunk = b""
            try:
                self._inbox.put(chunk or None, timeout=30)
            except queue.Full:
                return
            if not chunk:
                return

    def _read_some(self, deadline: float) -> bytes | None:
        remaining = max(0.0, deadline - time.monotonic())
        if self._threaded:
            try:
                return self._inbox.get(timeout=remaining)
            except queue.Empty:
                return b""
        events = self._selector.select(timeout=remaining)
        if not events:
            return b""
        try:
            chunk = os.read(self._proc.stdout.fileno(), 65536)
        except BlockingIOError:
            return b""
        except OSError:
            return None
        return chunk or None

    def _fail(self, reason: str) -> ClangdError:
        self._failure = reason
        self.kill()
        return ClangdError(reason)

    def send(self, message: Mapping[str, Any]) -> None:
        if not self.alive:
            raise ClangdError(self._failure or "clangd is not running")
        try:
            self._proc.stdin.write(encode_frame(message))
            self._proc.stdin.flush()
        except (OSError, ValueError):
            raise self._fail("clangd closed its input") from None

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            self.send({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    def request(self, method: str, params: Mapping[str, Any] | None = None, *,
                timeout: float | None = None) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self.send({"jsonrpc": "2.0", "id": request_id, "method": method,
                       "params": dict(params or {})})
            deadline = time.monotonic() + (self._timeout if timeout is None else float(timeout))
            while True:
                message = self._next_message(deadline)
                if message is None:
                    raise self._fail("clangd did not answer %s in time" % method)
                if message.get("id") == request_id and "method" not in message:
                    if "error" in message:
                        error = message.get("error") or {}
                        raise ClangdError("clangd refused %s: %s" % (
                            method, str(error.get("message", ""))[:MAX_MESSAGE_CHARS]))
                    return message.get("result")

    def pump(self, deadline: float, until: Callable[[], bool]) -> bool:
        """Process incoming traffic until ``until()`` holds or the deadline passes."""
        with self._lock:
            while not until():
                if time.monotonic() >= deadline:
                    return False
                self._next_message(deadline, stop=until)
            return True

    def _next_message(self, deadline: float, stop: Callable[[], bool] | None = None) -> dict | None:
        while True:
            if self._pending:
                message = self._pending.popleft()
                if self._dispatch(message):
                    continue
                return message
            if stop is not None and stop():
                return {}
            if time.monotonic() >= deadline:
                return None
            chunk = self._read_some(deadline)
            if chunk is None:
                raise self._fail("clangd exited")
            if not chunk:
                continue
            try:
                messages = self._reader.feed(chunk)
            except FrameError as exc:
                raise self._fail(str(exc)) from None
            if len(self._pending) + len(messages) > 1024:
                raise self._fail("clangd flooded the client with messages")
            self._pending.extend(messages)

    def _dispatch(self, message: dict) -> bool:
        """Handle server requests and notifications; True when consumed."""
        method = message.get("method")
        if not isinstance(method, str):
            return False
        if "id" in message:
            # A server->client request (progress tokens, configuration,
            # capability registration): answer it so clangd never blocks.
            result: Any = None
            if method == "workspace/configuration":
                items = (message.get("params") or {}).get("items") or []
                result = [None for _ in items[:32]]
            self.send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
            return True
        if self._on_notification is not None:
            try:
                self._on_notification(message)
            except Exception:
                logger.debug("clangd notification handler failed", exc_info=True)
        return True

    def kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            terminate_process_tree(proc)
        finally:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if self._selector is not None:
                try:
                    self._selector.close()
                except (OSError, ValueError, KeyError):
                    pass
            self._closed = True

    def close(self) -> None:
        if self._closed or self._proc is None:
            return
        try:
            if self.alive:
                self.request("shutdown", None, timeout=3)
                self.notify("exit")
        except ClangdError:
            pass
        finally:
            self.kill()


# --- sessions ----------------------------------------------------------------------------


@dataclass(frozen=True)
class NavigationItem:
    file: str
    line: int
    column: int
    hover: str = ""
    definition_file: str = ""
    definition_line: int = 0

    def to_wire(self) -> dict:
        wire = {"file": self.file, "line": self.line, "column": self.column}
        if self.hover:
            wire["hover"] = self.hover
        if self.definition_file:
            wire["definition"] = {"file": self.definition_file, "line": self.definition_line}
        return wire


def _field(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        if value not in (None, ""):
            return value
    return default


class ClangdSession:
    """One live clangd over one project root; lazily started, idle-reaped."""

    def __init__(self, executable: str, *, project_root: str, compile_commands_dir: str = "",
                 enable_config: bool = False, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
                 idle_seconds: float = DEFAULT_IDLE_SECONDS,
                 executable_guard: Callable[[str], str] | None = None,
                 job_registry: Callable[[], Any] | None = None,
                 use_thread_reader: bool | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        root = os.path.realpath(project_root)
        if not os.path.isdir(root):
            raise ClangdError("project root is not a directory")
        self._executable = executable
        self._root = root
        self._ccdir = os.path.realpath(compile_commands_dir) if compile_commands_dir else ""
        self._enable_config = bool(enable_config)
        self._max_frame = max_frame_bytes
        self._timeout = request_timeout
        self._idle = float(idle_seconds)
        self._guard = executable_guard
        self._job_registry = job_registry
        self._threaded = use_thread_reader
        self._clock = clock
        self._client: JsonRpcStdioClient | None = None
        self._private_dir = ""
        self._opened: dict[str, int] = {}
        self._diagnostics: dict[str, tuple[dict, ...]] = {}
        self._diag_versions: dict[str, int] = {}
        self._last_used = clock()
        self._job_id = ""
        self._lock = threading.RLock()

    @property
    def project_root(self) -> str:
        return self._root

    @property
    def pid(self) -> int | None:
        return self._client.pid if self._client is not None else None

    @property
    def private_dir(self) -> str:
        return self._private_dir

    def _start(self) -> JsonRpcStdioClient:
        if self._client is not None and self._client.alive:
            return self._client
        executable = self._executable
        if self._guard is not None:
            executable = self._guard(executable)
        if not os.path.isabs(executable):
            raise ClangdError("clangd must be an absolute inventory path")
        self._private_dir = tempfile.mkdtemp(prefix="sonder-clangd-")
        os.chmod(self._private_dir, 0o700)
        client = JsonRpcStdioClient(
            clangd_argv(executable, compile_commands_dir=self._ccdir,
                        enable_config=self._enable_config),
            cwd=self._private_dir, env=_scrubbed_env(self._private_dir, executable),
            max_frame_bytes=self._max_frame, request_timeout=self._timeout,
            use_thread_reader=self._threaded, on_notification=self._notification,
        )
        self._client = client
        try:
            client.start()
        except OSError as exc:
            self._stop()
            raise ClangdError("clangd could not be started: %s" % type(exc).__name__) from None
        try:
            client.request("initialize", {
                "processId": os.getpid(),
                "rootUri": path_to_uri(self._root),
                "capabilities": {
                    "textDocument": {
                        "hover": {"contentFormat": ["plaintext"]},
                        "definition": {"linkSupport": False},
                        "publishDiagnostics": {"relatedInformation": False},
                    },
                    "workspace": {"symbol": {}},
                },
                "workspaceFolders": None,
            })
            client.notify("initialized", {})
        except ClangdError:
            self._stop()
            raise
        self._track_start()
        return client

    def _track_start(self) -> None:
        if self._job_registry is None:
            return
        try:
            from ...application.ports.jobs import JobIdentity

            registry = self._job_registry()
            self._job_id = "lsp-clangd-" + uuid.uuid4().hex[:24]
            registry.create(JobIdentity(job_id=self._job_id, kind=LSP_JOB_KIND,
                                        operation_id=self._job_id, idempotency_key=self._job_id),
                            metadata={"kind": LSP_JOB_KIND, "server": "clangd",
                                      "pid": str(self.pid or "")})
        except Exception:
            logger.warning("clangd session could not be recorded as a job", exc_info=True)
            self._job_id = ""

    def _track_stop(self) -> None:
        if not self._job_id or self._job_registry is None:
            return
        try:
            from ...application.ports.jobs import JobStatus

            registry = self._job_registry()
            claim = registry.claim(self._job_id, "clangd-session", lease_seconds=60)
            if claim is not None:
                registry.finish(self._job_id, "clangd-session", JobStatus.SUCCEEDED,
                                claim_token=claim.claim_token or None)
        except Exception:
            logger.warning("clangd job record could not be finished", exc_info=True)
        self._job_id = ""

    def _stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()
        self._opened.clear()
        self._track_stop()
        if self._private_dir:
            shutil.rmtree(self._private_dir, ignore_errors=True)
            self._private_dir = ""

    def close(self) -> None:
        with self._lock:
            self._stop()

    def shutdown_if_idle(self) -> bool:
        """Kill the server when it has been idle longer than ``idle_seconds``."""
        with self._lock:
            if self._client is not None and self._clock() - self._last_used >= self._idle:
                self._stop()
                return True
            return False

    def _touch(self) -> JsonRpcStdioClient:
        self.shutdown_if_idle()
        client = self._start()
        self._last_used = self._clock()
        return client

    # -- files ---------------------------------------------------------------------------

    def resolve(self, file_rel: str) -> str:
        if not isinstance(file_rel, str) or not file_rel or "\x00" in file_rel:
            raise ClangdError("file must be a project-relative path")
        candidate = file_rel if os.path.isabs(file_rel) else os.path.join(self._root, file_rel)
        lexical = os.path.normpath(os.path.abspath(candidate))
        real = os.path.realpath(lexical)
        if _norm(real) != _norm(lexical) or not _inside(real, self._root):
            raise ClangdError("file is outside the project root or reached through a link")
        return real

    def _read(self, path: str) -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_OPEN_FILE_BYTES:
                raise ClangdError("file is not a bounded regular file")
            data = os.read(fd, MAX_OPEN_FILE_BYTES + 1)
        finally:
            os.close(fd)
        return data.decode("utf-8", errors="replace")

    def open_file(self, file_rel: str) -> str:
        """didOpen (or re-sync) a file; returns its URI."""
        with self._lock:
            client = self._touch()
            path = self.resolve(file_rel)
            uri = path_to_uri(path)
            text = self._read(path)
            version = self._opened.get(uri, 0) + 1
            if uri in self._opened:
                client.notify("textDocument/didChange", {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}]})
            else:
                if len(self._opened) >= MAX_TRACKED_FILES:
                    oldest = next(iter(self._opened))
                    client.notify("textDocument/didClose", {"textDocument": {"uri": oldest}})
                    self._opened.pop(oldest, None)
                    self._diagnostics.pop(oldest, None)
                language = _LANGUAGE_IDS.get(os.path.splitext(path)[1].lower(), "cpp")
                client.notify("textDocument/didOpen", {"textDocument": {
                    "uri": uri, "languageId": language, "version": version, "text": text}})
            self._opened[uri] = version
            return uri

    def _notification(self, message: dict) -> None:
        if message.get("method") != "textDocument/publishDiagnostics":
            return
        params = message.get("params") or {}
        uri = params.get("uri")
        if not isinstance(uri, str) or uri not in self._opened:
            return
        rows = []
        for item in (params.get("diagnostics") or [])[:MAX_DIAGNOSTICS_PER_FILE]:
            if not isinstance(item, dict):
                continue
            start = ((item.get("range") or {}).get("start") or {})
            rows.append({
                "file": label_for(uri_to_path(uri), self._root),
                "line": int(start.get("line", 0)) + 1,
                "column": int(start.get("character", 0)) + 1,
                "severity": _SEVERITY.get(item.get("severity"), "note"),
                "message": str(item.get("message", ""))[:MAX_MESSAGE_CHARS],
                "code": str(item.get("code", ""))[:64],
            })
        self._diagnostics[uri] = tuple(rows)
        version = params.get("version")
        self._diag_versions[uri] = int(version) if isinstance(version, int) else self._opened.get(uri, 0)

    def diagnostics(self, file_rel: str, *, timeout: float | None = None) -> tuple[dict, ...]:
        with self._lock:
            uri = self.open_file(file_rel)
            wanted = self._opened[uri]
            self._diagnostics.pop(uri, None)
            deadline = time.monotonic() + (self._timeout if timeout is None else float(timeout))
            client = self._client
            ready = client.pump(deadline, lambda: uri in self._diagnostics
                                and self._diag_versions.get(uri, 0) >= wanted)
            if not ready:
                raise ClangdError("clangd published no diagnostics in time")
            self._last_used = self._clock()
            return self._diagnostics[uri]

    def _location(self, result: Any) -> tuple[str, int]:
        items = result if isinstance(result, list) else ([result] if result else [])
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or item.get("targetUri")
            span = item.get("range") or item.get("targetSelectionRange") or {}
            line = int(((span.get("start") or {}).get("line", 0))) + 1
            path = uri_to_path(uri or "")
            if path:
                return label_for(path, self._root), line
        return "", 0

    def definition(self, file_rel: str, line: int, column: int) -> tuple[str, int]:
        with self._lock:
            uri = self.open_file(file_rel)
            result = self._client.request("textDocument/definition", {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, int(line) - 1), "character": max(0, int(column) - 1)}})
            return self._location(result)

    def hover(self, file_rel: str, line: int, column: int) -> str:
        with self._lock:
            uri = self.open_file(file_rel)
            result = self._client.request("textDocument/hover", {
                "textDocument": {"uri": uri},
                "position": {"line": max(0, int(line) - 1), "character": max(0, int(column) - 1)}})
        contents = (result or {}).get("contents") if isinstance(result, dict) else None
        if isinstance(contents, dict):
            text = contents.get("value", "")
        elif isinstance(contents, list):
            text = " ".join(str(item.get("value", item)) if isinstance(item, dict) else str(item)
                            for item in contents[:4])
        else:
            text = str(contents or "")
        return " ".join(str(text).split())[:MAX_HOVER_CHARS]

    def workspace_symbols(self, query_text: str, *, max_results: int) -> list[dict]:
        with self._lock:
            client = self._touch()
            result = client.request("workspace/symbol", {"query": str(query_text)[:256]})
        rows = []
        for item in (result or [])[:max(1, int(max_results)) * 4]:
            if isinstance(item, dict):
                rows.append(item)
        return rows


class ClangdNavigator:
    """``BuildNavigator`` over clangd: context at diagnostic positions."""

    def __init__(self, executable: str, *, project_root: str, compile_commands_dir: str = "",
                 enable_config: bool = False, **session_options: Any) -> None:
        self._session = ClangdSession(executable, project_root=project_root,
                                      compile_commands_dir=compile_commands_dir,
                                      enable_config=enable_config, **session_options)

    @property
    def session(self) -> ClangdSession:
        return self._session

    def prefetch_diagnostics(self, file_rel: str, ctx: Any = None, *,
                             timeout: float | None = None) -> tuple[dict, ...]:
        del ctx
        return self._session.diagnostics(file_rel, timeout=timeout)

    def context_for(self, file_rel: str, diags: Iterable[Any], ctx: Any = None, *,
                    max_items: int = MAX_CONTEXT_ITEMS) -> tuple[dict, ...]:
        del ctx
        limit = max(1, min(int(max_items), MAX_CONTEXT_ITEMS))
        items: list[dict] = []
        seen: set[tuple[int, int]] = set()
        for diag in diags:
            if len(items) >= limit:
                break
            line = int(_field(diag, "line", default=0) or 0)
            column = int(_field(diag, "column", "col", default=1) or 1)
            if line < 1 or (line, column) in seen:
                continue
            seen.add((line, column))
            try:
                hover = self._session.hover(file_rel, line, column)
                target, target_line = self._session.definition(file_rel, line, column)
            except ClangdError as exc:
                logger.info("clangd context unavailable: %s", exc)
                break
            items.append(NavigationItem(file=label_for(self._session.resolve(file_rel),
                                                       self._session.project_root),
                                        line=line, column=column, hover=hover,
                                        definition_file=target,
                                        definition_line=target_line).to_wire())
        return tuple(items)

    def close(self) -> None:
        self._session.close()


# --- REPO-004 symbol transport -----------------------------------------------------------


@dataclass(frozen=True)
class ClangdRoot:
    project_root: str
    compile_commands_dir: str = ""
    git_revision: str = "unknown"
    seed_files: tuple[str, ...] = ()


class ClangdSymbolSession:
    """``LspSession``: symbol queries through ``workspace/symbol`` then definition."""

    OPERATIONS = frozenset({"definition", "symbol"})

    def __init__(self, session: ClangdSession, root: ClangdRoot, *, root_id: str,
                 operations: frozenset[str], max_results: int) -> None:
        self._session = session
        self._root = root
        self._root_id = root_id
        self._operations = frozenset(operations) & self.OPERATIONS
        self._max = max(1, min(int(max_results), MAX_SYMBOL_RESULTS))
        self._primed = False

    def _prime(self) -> None:
        if self._primed:
            return
        self._primed = True
        for rel in self._root.seed_files[:16]:
            try:
                self._session.diagnostics(rel)
            except ClangdError as exc:
                logger.info("clangd seed file not indexed: %s", exc)

    def query(self, *, root_id: str, symbol: str, operation: str, max_results: int):
        from ...application.repository_intelligence.navigation import NavigationEvidence

        if root_id != self._root_id or operation not in self._operations:
            return ()
        limit = max(1, min(int(max_results), self._max))
        self._prime()
        name = str(symbol).split("::")[-1]
        rows = []
        for item in self._session.workspace_symbols(name, max_results=limit):
            if len(rows) >= limit:
                break
            qualified = (str(item.get("containerName") or "") + "::" + str(item.get("name") or "")
                         ).lstrip(":")
            if item.get("name") != name and qualified != symbol:
                continue
            location = item.get("location") or {}
            path = uri_to_path(location.get("uri") or "")
            if not path or not _inside(os.path.realpath(path), self._session.project_root):
                continue
            label = label_for(path, self._session.project_root)
            if operation == "definition":
                start = ((location.get("range") or {}).get("start") or {})
                try:
                    target, _ = self._session.definition(
                        label, int(start.get("line", 0)) + 1, int(start.get("character", 0)) + 1)
                except ClangdError:
                    target = ""
                if target and not target.startswith("[external]/"):
                    label = target
            rows.append(NavigationEvidence(self._root_id, label, symbol, operation,
                                           "lsp:clangd", self._root.git_revision))
        return tuple(rows)

    def close(self) -> None:
        self._session.close()


class ClangdSymbolTransport:
    """``LspTransport`` over clangd for the roots it was configured with."""

    LANGUAGES = frozenset({"c", "cpp", "c++", "objective-c", "objective-cpp"})

    def __init__(self, executable: str, roots: Mapping[str, ClangdRoot], *,
                 enable_config: bool = False, **session_options: Any) -> None:
        self._executable = executable
        self._roots = dict(roots)
        self._enable_config = bool(enable_config)
        self._options = dict(session_options)

    def open(self, *, root_id: str, language: str, operations: frozenset[str],
             max_results: int) -> ClangdSymbolSession | None:
        root = self._roots.get(root_id)
        if root is None or str(language).casefold() not in self.LANGUAGES:
            return None
        session = ClangdSession(self._executable, project_root=root.project_root,
                                compile_commands_dir=root.compile_commands_dir,
                                enable_config=self._enable_config, **self._options)
        return ClangdSymbolSession(session, root, root_id=root_id, operations=operations,
                                   max_results=max_results)


def find_clangd(lookup: Any) -> str:
    """The inventory's clangd path, or ``""`` (lane C composes lane D only then)."""
    for name in ("clangd", "clangd-18", "clangd-17"):
        record = lookup.lookup(name) if lookup is not None else None
        if record is not None and getattr(record, "path", ""):
            return str(Path(str(record.path)))
    return ""


__all__ = [
    "ClangdError", "ClangdNavigator", "ClangdRoot", "ClangdSession", "ClangdSymbolSession",
    "ClangdSymbolTransport", "FrameError", "FrameReader", "JsonRpcStdioClient", "LSP_JOB_KIND",
    "clangd_argv", "encode_frame", "find_clangd", "label_for", "path_to_uri", "uri_to_path",
]
