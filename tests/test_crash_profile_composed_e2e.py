"""End to end through the composed application: crash and profile digests.

``build_application()`` composes the real typed gateway, with the build tools'
executor in front of the debug tools' executor and one permission evaluator
carrying both features' resolvers. On a real Linux host this test:

1. compiles a tiny C++ program with ``-g`` that segfaults and has gdb write an
   ELF core (``generate-core-file``);
2. runs ``crash_triage`` (Tier 0, pure) and ``crash_digest`` with the gdb
   engine (Tier 1, a durable job) through ``application.tools.execute`` and
   asserts the faulting frame's file and line;
3. digests an AddressSanitizer report with ``crash_triage``;
4. digests a callgrind profile and a Chrome trace with ``profile_digest``;
5. checks that ``/crash`` and ``/profile`` are registered REPL commands.

Fixtures are generated in a temporary directory; a core holds process memory
and none is committed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import permission_modes as pm

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux host only")]

CRASHER = r"""
#include <cstdio>
#include <cstring>
__attribute__((noinline)) void store(int *p) {
    *p = 42;
}
int main(int argc, char **argv) {
    if (argc > 1 && !std::strcmp(argv[1], "uaf")) {
        int *q = new int[4];
        delete[] q;
        std::printf("%d\n", q[1]);
        return 0;
    }
    int *p = argc > 5 ? new int : nullptr;
    store(p);
    return 0;
}
"""
FAULT_LINE = 5  # ``*p = 42;``

CALLGRIND = """# callgrind format
version: 1
creator: callgrind-3.22.0
cmd: ./game
positions: line
events: Ir
summary: 1000

fl=(1) /src/game/main.cpp
fn=(1) main
10 100
cfn=(2) update
calls=1 20
20 900

fn=(2) update
20 900
"""


def _need(*tools):
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip("not installed: %s" % ", ".join(missing))


@pytest.fixture
def app(tmp_path, monkeypatch):
    from sonder_runtime.adapters.filesystem import file_ops
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.platform import paths as runtime_paths

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(allowed))
    monkeypatch.setattr(file_ops, "workspace_root", lambda: allowed)
    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    composed = []

    class Recording(bootstrap_app.DeveloperToolPermissionEvaluator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            composed.append(self)

    monkeypatch.setattr(bootstrap_app, "DeveloperToolPermissionEvaluator", Recording)
    application = bootstrap_app.build_application()
    try:
        yield application, allowed, composed
    finally:
        application.close_providers(timeout=5)
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)


def _call(application, name, arguments):
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGatewayRequest,
        ToolPermission,
        ToolScope,
    )

    descriptor = application.tools.graph.registry.get(name)
    effects = frozenset(effect.name.lower() for effect in descriptor.effects)
    receipt = application.tools.execute(ToolGatewayRequest(
        "e2e-" + uuid.uuid4().hex, name, arguments,
        ToolScope(principal_id="owner", source="mcp", allowed_effects=effects),
        ToolPermission(effects)))
    return json.loads(receipt.output)


def _report(payload):
    """The typed ``CrashReport`` the wire payload carries (sonder.crash_report/1)."""
    from sonder_runtime.domain.crash.model import CrashReport
    from sonder_runtime.domain.crash.render import report_from_wire

    report = report_from_wire(payload)
    assert isinstance(report, CrashReport)
    return report


def _digest(payload):
    """The typed ``ProfileDigest`` the wire payload carries (sonder.profile_digest/1)."""
    from sonder_runtime.domain.profiling.model import ProfileDigest
    from sonder_runtime.domain.profiling.render import digest_from_wire

    digest = digest_from_wire(payload)
    assert isinstance(digest, ProfileDigest)
    return digest


@pytest.fixture(scope="module")
def crasher(tmp_path_factory):
    _need("g++", "gdb")
    work = tmp_path_factory.mktemp("e2e-crasher")
    source = work / "crasher.cpp"
    source.write_text(CRASHER)
    binary = work / "crasher"
    subprocess.run(["g++", "-g", "-O0", "-o", str(binary), str(source)], check=True,
                   capture_output=True, timeout=180)
    core = work / "core.crasher"
    subprocess.run(["gdb", "-nx", "-batch", "-ex", "run", "-ex", "generate-core-file %s" % core,
                    "--args", str(binary)], capture_output=True, timeout=120, cwd=work)
    if not core.exists():
        pytest.skip("gdb could not produce a core on this host")
    return {"work": work, "source": source, "binary": binary, "core": core}


def test_the_composed_gateway_serves_both_features(app):
    application, _, composed = app
    names = {item.name for item in application.tools.graph.registry.list_all()}
    assert {"crash_triage", "crash_digest", "profile_digest", "profile_capture_digest",
            "debug_run_result"} <= names
    assert {"build_model", "build_job", "build_fix"} <= names
    (evaluator,) = composed  # the main facade; lanes are lazy
    assert {"test_run", "build_job", "build_fix", "crash_digest",
            "profile_capture_digest"} <= set(evaluator.resolvers)


def test_symbol_server_from_the_gateway_is_refused(app, crasher):
    from sonder_runtime.domain.common.errors import Forbidden

    application, allowed, _ = app
    core = allowed / "core"
    shutil.copy(crasher["core"], core)
    with pytest.raises(Forbidden) as caught:
        _call(application, "crash_digest", {"path": str(core), "symbol_server": True})
    assert getattr(caught.value, "code", "") == "SYMBOL_SERVER_NEEDS_CONSOLE"


def test_a_segfault_core_tier0_then_gdb_through_the_composed_gateway(app, crasher):
    application, allowed, _ = app
    binary = allowed / "crasher"
    core = allowed / "core.crasher"
    shutil.copy(crasher["binary"], binary)
    shutil.copy(crasher["core"], core)
    shutil.copy(crasher["source"], allowed / "crasher.cpp")

    tier0 = _call(application, "crash_triage", {"path": str(core)})
    assert tier0.get("object") == "crash_report", tier0
    report0 = _report(tier0)
    assert report0.source_kind == "elf_core"
    assert report0.exception is not None and "SIGSEGV" in report0.exception.name

    body = _call(application, "crash_digest", {
        "path": str(core), "executable": str(binary), "engine": "gdb", "wait_seconds": 60})
    deadline = time.monotonic() + 180
    while body.get("status") == "running" and time.monotonic() < deadline:
        body = _call(application, "debug_run_result", {"run_id": body["run_id"], "wait_seconds": 30})
    assert body.get("object") == "debug_run" and "error_code" not in body, body
    assert "gdb" in body["engines"], body
    report = _report(body["crash"])
    assert "gdb" in report.engines
    frame = report.crashing_thread().frames[0]
    assert "store" in (frame.function or ""), frame
    assert Path(frame.file or "").name == "crasher.cpp", frame
    assert frame.line == FAULT_LINE, frame


def test_an_asan_report_through_crash_triage(app, crasher):
    application, allowed, _ = app
    binary = crasher["work"] / "crasher_asan"
    try:
        subprocess.run(["g++", "-g", "-O0", "-fsanitize=address", "-o", str(binary),
                        str(crasher["source"])], check=True, capture_output=True, timeout=180)
    except subprocess.CalledProcessError:
        pytest.skip("AddressSanitizer is not available to g++ here")
    run = subprocess.run([str(binary), "uaf"], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/bin:/bin", "ASAN_OPTIONS": "detect_leaks=0"})
    log = allowed / "asan_uaf.log"
    log.write_text(run.stderr)
    body = _call(application, "crash_triage", {"path": str(log)})
    assert body.get("object") == "crash_report", body
    report = _report(body)
    assert report.source_kind == "sanitizer_report"
    assert "heap-use-after-free" in report.exception.name
    frame = report.crashing_thread().frames[0]
    assert frame.line is not None
    assert Path(frame.file or "").name == "crasher.cpp", frame


def test_callgrind_and_chrome_trace_profile_digests(app):
    application, allowed, _ = app
    callgrind = allowed / "callgrind.out.123"
    callgrind.write_text(CALLGRIND)
    body = _call(application, "profile_digest", {"path": str(callgrind)})
    assert body.get("object") == "profile_digest", body
    digest = _digest(body)
    assert digest.source_kind == "callgrind"
    assert digest.top_self and digest.top_self[0].name == "update", digest.top_self

    events = [{"name": "Frame", "ph": "X", "ts": i * 16_667, "dur": 16_000 if i != 5 else 40_000,
               "pid": 1, "tid": 1} for i in range(20)]
    trace = allowed / "trace.json"
    trace.write_text(json.dumps({"traceEvents": events}))
    body = _call(application, "profile_digest", {"path": str(trace), "frame_budget_ms": 16.6})
    assert body.get("object") == "profile_digest", body
    digest = _digest(body)
    assert digest.source_kind == "chrome_trace"
    assert digest.frames is not None and digest.frames.count == 20, digest.frames


def test_crash_and_profile_are_registered_repl_commands():
    from sonder_runtime.adapters import command_catalog
    from sonder_runtime.interfaces.repl import repl

    source = Path(repl.__file__).read_text(encoding="utf-8")
    assert 'elif cmd == "/crash":' in source and 'elif cmd == "/profile":' in source
    assert callable(repl._crash_command) and callable(repl._profile_command)
    catalog = Path(command_catalog.__file__).read_text(encoding="utf-8")
    assert '"/crash"' in catalog and '"/profile"' in catalog
