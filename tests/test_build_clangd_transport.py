"""clangd navigation (lane D): the bounded JSON-RPC client and both adapters.

Framing bounds and the client's failure handling are tested without clangd
(a fake stdio server written in Python); everything else runs the real
clangd on a tiny CMake/Ninja project and skips when clangd, cmake or ninja is
missing (``apt-get install -y clangd``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.build.clangd import (
    LSP_JOB_KIND,
    ClangdError,
    ClangdNavigator,
    ClangdRoot,
    ClangdSession,
    ClangdSymbolTransport,
    FrameError,
    FrameReader,
    JsonRpcStdioClient,
    clangd_argv,
    encode_frame,
    find_clangd,
    label_for,
    path_to_uri,
    uri_to_path,
)

pytestmark = pytest.mark.unit

CLANGD = shutil.which("clangd") or shutil.which("clangd-18") or ""
needs_clangd = pytest.mark.skipif(
    not (CLANGD and shutil.which("cmake") and shutil.which("ninja") and shutil.which("g++")),
    reason="needs clangd, cmake, ninja and g++ (apt-get install -y clangd)")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")

MAIN = textwrap.dedent("""\
    #include "math.h"
    int main() {
      int lenght = 3;
      return square(length);
    }
""")


# --- framing (no clangd) ------------------------------------------------------------------


def test_frames_round_trip_and_split_anywhere():
    reader = FrameReader()
    data = encode_frame({"jsonrpc": "2.0", "id": 1, "result": {"x": "é"}}) + encode_frame(
        {"jsonrpc": "2.0", "method": "note"})
    messages = []
    for index in range(len(data)):
        messages.extend(reader.feed(data[index:index + 1]))
    assert messages == [{"jsonrpc": "2.0", "id": 1, "result": {"x": "é"}},
                        {"jsonrpc": "2.0", "method": "note"}]


@pytest.mark.parametrize("data, why", [
    (b"Content-Length: 99999999\r\n\r\n", "oversized frame"),
    (b"X-Pad: " + b"a" * 9000 + b"\r\n\r\n", "header block cap (complete)"),
    (b"X-Pad: " + b"a" * 9000, "header block cap (incomplete)"),
    (b"Content-Length: 2\r\n\r\n[]", "not an object"),
    (b"Content-Length: 1\r\n\r\n{", "not JSON"),
    (b"Content-Length: x\r\n\r\n", "bad length"),
    (b"Content-Type: a\r\n\r\n", "no length"),
    (b"nonsense\r\n\r\n", "malformed header"),
    (b"".join(b"A: b\r\n" for _ in range(9)) + b"Content-Length: 2\r\n\r\n{}", "too many lines"),
])
def test_frame_bounds_are_enforced(data, why):
    with pytest.raises(FrameError):
        FrameReader(max_frame_bytes=1024).feed(data)


def test_uris_and_labels_never_leak_host_paths(tmp_path):
    inner = tmp_path / "src" / "a b.cpp"
    inner.parent.mkdir()
    inner.write_text("int a;\n")
    assert uri_to_path(path_to_uri(str(inner))) == str(inner)
    assert label_for(str(inner), str(tmp_path)) == "src/a b.cpp"
    assert label_for("/usr/include/stdio.h", str(tmp_path)) == "[external]/stdio.h"
    assert uri_to_path("http://x/y") == ""


def test_argv_is_hardened():
    argv = clangd_argv("/usr/bin/clangd", compile_commands_dir="/b", enable_config=False)
    assert "--background-index=false" in argv and "--enable-config=false" in argv
    assert "--log=error" in argv and "--clang-tidy=false" in argv
    assert not any(item.startswith("--query-driver") for item in argv)
    assert "--enable-config=true" in clangd_argv("/c", compile_commands_dir="", enable_config=True)


FAKE_SERVER = textwrap.dedent("""\
    import json, sys
    out = sys.stdout.buffer
    def frame(obj):
        body = json.dumps(obj).encode()
        out.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body); out.flush()
    mode = sys.argv[1]
    raw = sys.stdin.buffer
    while True:
        header = b""
        while not header.endswith(b"\\r\\n\\r\\n"):
            ch = raw.read(1)
            if not ch:
                sys.exit(0)
            header += ch
        length = int(header.split(b":")[1].strip().split(b"\\r")[0])
        message = json.loads(raw.read(length))
        if "id" not in message or "method" not in message:
            continue
        if mode == "huge":
            out.write(b"Content-Length: 99999999\\r\\n\\r\\n"); out.flush()
        elif mode == "silent":
            pass
        elif mode == "request-first":
            frame({"jsonrpc": "2.0", "id": 77, "method": "workspace/configuration",
                   "params": {"items": [{}, {}]}})
            frame({"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message["method"]}})
        else:
            frame({"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message["method"]}})
""")


def _fake_client(tmp_path, mode, **kwargs):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER)
    client = JsonRpcStdioClient([sys.executable, str(script), mode], cwd=str(tmp_path),
                                env={"PATH": os.environ.get("PATH", "")}, request_timeout=5,
                                max_frame_bytes=64 * 1024, **kwargs)
    client.start()
    return client


def _gone(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if os.waitpid(pid, os.WNOHANG) != (0, 0):
                return True
        except ChildProcessError:
            return True
        time.sleep(0.05)
    return False


@posix_only
@pytest.mark.parametrize("threaded", [False, True])
def test_the_client_answers_server_requests_and_returns_results(tmp_path, threaded):
    client = _fake_client(tmp_path, "request-first", use_thread_reader=threaded)
    try:
        assert client.request("ping", {}) == {"echo": "ping"}
        assert client.request("again", {}) == {"echo": "again"}
    finally:
        client.close()
    assert not client.alive


@posix_only
def test_an_oversized_frame_ends_the_session_and_kills_the_group(tmp_path):
    client = _fake_client(tmp_path, "huge")
    pid = client.pid
    with pytest.raises(ClangdError, match="exceeds"):
        client.request("ping", {})
    assert not client.alive and _gone(pid)
    with pytest.raises(ClangdError):
        client.request("ping", {})


@posix_only
def test_a_silent_server_times_out_and_is_killed(tmp_path):
    client = _fake_client(tmp_path, "silent")
    pid = client.pid
    started = time.monotonic()
    with pytest.raises(ClangdError, match="in time"):
        client.request("ping", {}, timeout=0.5)
    assert time.monotonic() - started < 5
    assert _gone(pid)


def test_find_clangd_uses_the_inventory_only():
    record = SimpleNamespace(path="/opt/llvm/bin/clangd-18")
    lookup = SimpleNamespace(lookup=lambda name: record if name == "clangd-18" else None)
    assert find_clangd(lookup) == "/opt/llvm/bin/clangd-18"
    assert find_clangd(SimpleNamespace(lookup=lambda name: None)) == ""
    assert find_clangd(None) == ""


# --- real clangd --------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "sparklite"
    (root / "src").mkdir(parents=True)
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(sparklite CXX)\n"
        "add_executable(game src/main.cpp src/math.cpp src/cfg.cpp)\n")
    (root / "src/math.h").write_text("#pragma once\nint square(int v);\n")
    (root / "src/math.cpp").write_text('#include "math.h"\nint square(int v) { return v * v; }\n')
    (root / "src/main.cpp").write_text(MAIN)
    (root / "src/cfg.cpp").write_text("#ifdef FROM_CLANGD_CONFIG\n#error configured-by-dot-clangd\n#endif\n"
                                      "int cfg;\n")
    (root / ".clangd").write_text("CompileFlags:\n  Add: [-DFROM_CLANGD_CONFIG]\n")
    subprocess.run(["cmake", "-S", str(root), "-B", str(root / "build"), "-G", "Ninja",
                    "-DCMAKE_CXX_COMPILER=g++", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"],
                   check=True, capture_output=True, timeout=120)
    return root


def _navigator(project, **kwargs):
    return ClangdNavigator(CLANGD, project_root=str(project),
                           compile_commands_dir=str(project / "build"),
                           request_timeout=60, **kwargs)


def _gcc_errors(project, rel):
    result = subprocess.run(["g++", "-fsyntax-only", "-I", str(project / "src"), str(project / rel)],
                            capture_output=True, text=True, timeout=60)
    found = set()
    for line in result.stderr.splitlines():
        match = re.match(r"^(.*?):(\d+):(\d+): (error|warning): ", line)
        if match:
            found.add((Path(match.group(1)).name, int(match.group(2)), match.group(4)))
    return found


@needs_clangd
@posix_only
def test_definition_hover_and_prefetched_diagnostics(project):
    navigator = _navigator(project)
    try:
        diagnostics = navigator.prefetch_diagnostics("src/main.cpp")
        clangd_errors = {(Path(item["file"]).name, item["line"], item["severity"])
                         for item in diagnostics if item["severity"] == "error"}
        assert clangd_errors == {item for item in _gcc_errors(project, "src/main.cpp")
                                 if item[2] == "error"}
        assert all(not item["file"].startswith("/") for item in diagnostics)
        context = navigator.context_for("src/main.cpp", [{"line": 4, "column": 10}] * 3 +
                                        [{"line": 0}], max_items=20)
        assert len(context) == 1
        item = context[0]
        assert item["file"] == "src/main.cpp" and "square" in item["hover"]
        assert item["definition"] == {"file": "src/math.h", "line": 2}
        with pytest.raises(ClangdError):
            navigator.prefetch_diagnostics("../outside.cpp")
    finally:
        navigator.close()
    assert not (project / ".cache").exists(), "no index or cache is written into the project"


@needs_clangd
@posix_only
def test_a_project_dot_clangd_is_ignored_unless_the_operator_enables_it(project):
    default = _navigator(project)
    try:
        messages = [item["message"] for item in default.prefetch_diagnostics("src/cfg.cpp")]
        assert not any("configured-by-dot-clangd" in message.lower() for message in messages)
    finally:
        default.close()
    enabled = _navigator(project, enable_config=True)
    try:
        messages = [item["message"] for item in enabled.prefetch_diagnostics("src/cfg.cpp")]
        assert any("configured-by-dot-clangd" in message.lower() for message in messages)
    finally:
        enabled.close()


@needs_clangd
@posix_only
def test_idle_shutdown_kills_the_process_group_and_tracks_the_job(project):
    class Registry:
        def __init__(self):
            self.created, self.finished = [], []

        def create(self, identity, *, metadata=None):
            self.created.append((identity, metadata))

        def claim(self, job_id, worker_id, *, lease_seconds=300):
            return SimpleNamespace(claim_token="t")

        def finish(self, job_id, worker_id, status, *, claim_token=None, **kwargs):
            self.finished.append((job_id, status))

    registry = Registry()
    now = [1000.0]
    session = ClangdSession(CLANGD, project_root=str(project),
                            compile_commands_dir=str(project / "build"), idle_seconds=30,
                            request_timeout=60, job_registry=lambda: registry,
                            use_thread_reader=True, clock=lambda: now[0])
    session.diagnostics("src/math.cpp")
    pid = session.pid
    private = session.private_dir
    assert pid and os.getpgid(pid) == pid, "clangd leads its own process group"
    assert registry.created[0][0].kind == LSP_JOB_KIND
    assert not session.shutdown_if_idle()
    now[0] += 31
    assert session.shutdown_if_idle()
    assert _gone(pid)
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
    assert registry.finished and registry.finished[0][0] == registry.created[0][0].job_id
    assert not os.path.exists(private), "the private HOME/cache directory is removed"
    # the next call starts a fresh session
    assert session.diagnostics("src/math.cpp") == ()
    session.close()


@needs_clangd
@posix_only
def test_lsp_transport_conformance_with_the_multiroot_contracts(project):
    from sonder_runtime.application.repository_intelligence import (
        MultiRepositoryNavigator,
        MultiRootReadContext,
    )
    from sonder_runtime.application.repository_intelligence.lsp_multiroot import (
        RepositoryRoot,
        open_live_lsp,
    )
    from sonder_runtime.application.repository_intelligence.navigation import NavigationEvidence

    transport = ClangdSymbolTransport(CLANGD, {"engine": ClangdRoot(
        str(project), str(project / "build"), git_revision="r1",
        seed_files=("src/math.cpp", "src/main.cpp"))}, request_timeout=60)
    root = RepositoryRoot("engine", "SparkLite", "r1")
    context = MultiRootReadContext((root,))
    assert transport.open(root_id="engine", language="python", operations=frozenset({"definition"}),
                          max_results=5) is None
    assert transport.open(root_id="other", language="cpp", operations=frozenset({"definition"}),
                          max_results=5) is None
    provider = open_live_lsp(context, transport, root_id="engine", language="cpp",
                             operations=("definition", "symbol"), max_results=10)
    try:
        rows = provider.query("engine", "square", "definition")
        assert rows and all(isinstance(row, NavigationEvidence) for row in rows)
        assert all(row.root_id == "engine" and row.revision == "r1" for row in rows)
        assert {row.file_path for row in rows} <= {"src/math.cpp", "src/math.h"}
        assert provider.query("engine", "square", "references") == ()

        class Port:
            root = RepositoryRoot("engine", "SparkLite", "r1")

            def language_for(self, symbol):
                return "cpp"

            def indexed_provider(self):
                return None

            def lexical_provider(self):
                return None

        results = MultiRepositoryNavigator([Port()], max_results=5).query(
            symbol="square", operation="definition", lsp_by_root={"engine": provider})
        assert results[0].backend.mode == "lsp" and results[0].evidence
        assert len(results[0].evidence) <= 5
    finally:
        provider.close()
    assert not (project / ".cache").exists()
