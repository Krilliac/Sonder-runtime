"""Real processes on a Linux host: crasher cores, gdb/lldb/eu-stack with the
lane's own templates, sanitizer logs, valgrind XML and llvm-symbolizer.

Every test skips when its tool is missing. Debuggers run exactly the argv
produced by ``materialize(template)`` (no literal argv copy), with the
scratch POSIX environment, an empty cwd and a fresh nonce.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.domain.binaries.elf_ids import read_elf_identity
from sonder_runtime.domain.binaries.pdb_info import pdb_matches, read_pdb_identity
from sonder_runtime.domain.binaries.pe_debug import read_pe_identity
from sonder_runtime.domain.binaries.reader import BytesReader
from sonder_runtime.domain.crash.debugger_text import (
    parse_eu_stack, parse_gdb, parse_lldb, parse_symbolizer_json,
)
from sonder_runtime.domain.crash.elf_core import core_to_report, read_elf_core
from sonder_runtime.domain.crash.render import merge_findings
from sonder_runtime.domain.crash.sanitizer import parse_sanitizer_report
from sonder_runtime.domain.crash.valgrind_xml import parse_valgrind_xml
from sonder_runtime.domain.debugging.templates import (
    eu_stack_template, gdb_template, lldb_template, materialize, posix_environment, symbolizer_template,
    with_netns,
)


pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux host tools")
FIXTURES = Path(__file__).parent / "fixtures" / "crash"
SOURCE = FIXTURES / "crasher.cpp"


def _line(marker: str) -> int:
    for number, text in enumerate(SOURCE.read_text().splitlines(), 1):
        if marker in text:
            return number
    raise AssertionError(marker)


def _tool(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _compile(tmp: Path, name: str, *flags: str) -> Path:
    compiler = _tool("g++")
    if compiler is None:
        pytest.skip("g++ is not installed")
    output = tmp / name
    subprocess.run([compiler, "-g", "-O0", "-fno-omit-frame-pointer", *flags, "-o", str(output), str(SOURCE)],
                   check=True, timeout=180, capture_output=True)
    return output


@pytest.fixture(scope="module")
def crash_core(tmp_path_factory):
    gdb = _tool("gdb")
    if gdb is None:
        pytest.skip("gdb is not installed")
    tmp = tmp_path_factory.mktemp("crash")
    binary = _compile(tmp, "crasher")
    core = tmp / "core.null"
    subprocess.run([gdb, "-nx", "-batch", "-ex", "run", "-ex", "generate-core-file %s" % core,
                    "--args", str(binary), "null"], cwd=tmp, capture_output=True, timeout=180,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C.UTF-8"})
    if not core.exists():
        pytest.skip("gdb could not generate a core here")
    return binary, core


def _rundir(tmp: Path) -> Path:
    run = tmp / ("run-" + secrets.token_hex(4))
    for sub in ("home", "tmp", "cwd"):
        (run / sub).mkdir(parents=True)
    return run


def _netns_ok() -> str | None:
    unshare = _tool("unshare")
    if unshare is None:
        return None
    probe = subprocess.run([unshare, "--user", "--map-current-user", "--net", "--", "/bin/true"],
                           capture_output=True, timeout=30)
    return unshare if probe.returncode == 0 else None


def _run(template, bindings: dict, rundir: Path, *, isolate: bool = True) -> str:
    unshare = _netns_ok() if isolate else None
    if unshare:
        template = with_netns(template, unshare)
    wanted = set(template.placeholders())
    argv, _ = materialize(template, {k: v for k, v in bindings.items() if k in wanted})
    env_template = posix_environment(network=False)
    env = {key: value.replace("{rundir}", str(rundir)) for key, value in env_template}
    result = subprocess.run(list(argv), env=env, cwd=rundir / "cwd", capture_output=True, text=True,
                            timeout=180)
    return result.stdout + result.stderr


def test_pure_core_reader_on_real_core(crash_core):
    binary, core = crash_core
    report = core_to_report(read_elf_core(BytesReader(core.read_bytes())))
    exc = report.exception
    assert exc.name == "SIGSEGV" and exc.access_address == 0 and exc.detail == "SEGV_MAPERR"
    top = report.crashing_thread().frames[0]
    assert top.module == "crasher" and top.module_offset and top.trust == "context"
    assert report.modules[0].name == "crasher"
    assert report.modules[0].debug_id == read_elf_identity(BytesReader(binary.read_bytes())).build_id
    assert "null_deref" in {hint.kind for hint in report.hints}


@pytest.mark.parametrize("engine", ["gdb", "lldb", "eu_stack"])
def test_real_debugger_with_lane_templates(crash_core, tmp_path, engine, monkeypatch):
    tools = {"gdb": ("gdb",), "lldb": ("lldb", "lldb-18"), "eu_stack": ("eu-stack",)}
    tool = _tool(*tools[engine])
    if tool is None:
        pytest.skip("%s is not installed" % engine)
    # A hostile parent environment must not reach the debugger (env is built from scratch).
    monkeypatch.setenv("DEBUGINFOD_URLS", "http://127.0.0.1:9")
    binary, core = crash_core
    builders = {"gdb": gdb_template, "lldb": lldb_template, "eu_stack": eu_stack_template}
    template = builders[engine](tool)
    nonce = secrets.token_hex(8)
    rundir = _rundir(tmp_path)
    output = _run(template, {"nonce": nonce, "rundir": str(rundir), "input": str(core), "exe": str(binary),
                             "solibpath": ""}, rundir)
    parser = {"gdb": lambda text: parse_gdb(text, nonce), "lldb": lambda text: parse_lldb(text, nonce),
              "eu_stack": parse_eu_stack}[engine]
    findings = parser(output)
    base = core_to_report(read_elf_core(BytesReader(core.read_bytes())))
    merged = merge_findings(base, findings, engine)
    top = merged.crashing_thread().frames[0]
    assert "run_frame" in top.function, output[-2000:]
    assert top.file.endswith("crasher.cpp") and top.line == _line("CRASH_LINE_CALL")
    assert merged.engines == ("pure", engine) and merged.symbolication == "full"
    assert merged.signature_basis == "functions"
    assert "null_deref" in {hint.kind for hint in merged.hints}


def test_signatures_agree_across_engines(crash_core, tmp_path):
    binary, core = crash_core
    base = core_to_report(read_elf_core(BytesReader(core.read_bytes())))
    signatures = {}
    for engine, names, builder, parse in (
            ("gdb", ("gdb",), gdb_template, parse_gdb), ("lldb", ("lldb", "lldb-18"), lldb_template, parse_lldb)):
        tool = _tool(*names)
        if tool is None:
            continue
        nonce = secrets.token_hex(8)
        rundir = _rundir(tmp_path)
        text = _run(builder(tool), {"nonce": nonce, "rundir": str(rundir), "input": str(core),
                                    "exe": str(binary), "solibpath": ""}, rundir)
        signatures[engine] = merge_findings(base, parse(text, nonce), engine).signature
    if len(signatures) < 2:
        pytest.skip("needs gdb and lldb")
    assert len(set(signatures.values())) == 1


@pytest.mark.parametrize("mode,kind,marker", [
    ("uaf", "heap-use-after-free", "CRASH_LINE_UAF"),
    ("overflow", "heap-buffer-overflow", "CRASH_LINE_OVERFLOW"),
])
def test_real_asan_reports(tmp_path, mode, kind, marker):
    binary = _compile(tmp_path, "crasher_asan", "-fsanitize=address")
    result = subprocess.run([str(binary), mode], capture_output=True, text=True, timeout=120,
                            env={"PATH": "/usr/bin:/bin", "ASAN_OPTIONS": "detect_leaks=0"})
    report = parse_sanitizer_report(result.stderr)
    assert report.exception.name == kind
    top = report.crashing_thread().frames[0]
    assert top.file.endswith("crasher.cpp") and top.line == _line(marker)


def test_real_ubsan_report(tmp_path):
    binary = _compile(tmp_path, "crasher_ubsan", "-fsanitize=undefined")
    result = subprocess.run([str(binary), "ubsan"], capture_output=True, text=True, timeout=120,
                            env={"PATH": "/usr/bin:/bin", "UBSAN_OPTIONS": "print_stacktrace=1"})
    report = parse_sanitizer_report(result.stderr)
    assert report.exception.name == "undefined-behavior"
    assert report.crashing_thread().frames[0].line == _line("CRASH_LINE_UBSAN")


def test_real_valgrind_memcheck_xml(tmp_path):
    valgrind = _tool("valgrind")
    if valgrind is None:
        pytest.skip("valgrind is not installed")
    binary = _compile(tmp_path, "crasher_vg")
    xml = tmp_path / "memcheck.xml"
    subprocess.run([valgrind, "--tool=memcheck", "--xml=yes", "--xml-file=%s" % xml, str(binary), "uaf"],
                   capture_output=True, timeout=300, env={"PATH": "/usr/bin:/bin"})
    report = parse_valgrind_xml(xml.read_bytes())
    assert report.exception.name == "InvalidRead"
    assert report.crashing_thread().frames[0].line == _line("CRASH_LINE_UAF")


def test_real_llvm_symbolizer_on_verified_pe_pdb(tmp_path):
    tool = _tool("llvm-symbolizer", "llvm-symbolizer-18")
    if tool is None:
        pytest.skip("llvm-symbolizer is not installed")
    exe = (FIXTURES / "pe" / "a" / "spark_tiny.exe").read_bytes()
    pdb = (FIXTURES / "pe" / "a" / "spark_tiny.pdb").read_bytes()
    assert pdb_matches(read_pe_identity(BytesReader(exe)), read_pdb_identity(BytesReader(pdb)))
    rundir = _rundir(tmp_path)
    staged = rundir / "sym" / "spark_tiny"
    staged.mkdir(parents=True)
    (staged / "spark_tiny.exe").write_bytes(exe)
    (staged / "spark_tiny.pdb").write_bytes(pdb)
    template = symbolizer_template(tool, "spark_tiny.exe", [0x1006, 0x1003])
    output = _run(template, {"rundir": str(rundir)}, rundir, isolate=False)
    symbolized = {item.offset: item for item in parse_symbolizer_json(output).symbolized}
    assert symbolized[0x1006].frames[0].function == "main" and symbolized[0x1006].frames[0].line == 9
    assert symbolized[0x1003].frames[0].file.endswith("spark_tiny.c")


def test_symbolizer_never_uses_mismatched_pdb_plan_gate():
    exe_a = read_pe_identity(BytesReader((FIXTURES / "pe" / "a" / "spark_tiny.exe").read_bytes()))
    pdb_b = read_pdb_identity(BytesReader((FIXTURES / "pe" / "b" / "spark_tiny.pdb").read_bytes()))
    # The planner (lane C) stages a module for llvm-symbolizer only when this is true.
    assert not pdb_matches(exe_a, pdb_b)
    assert os.path.basename(exe_a.pdb_path.replace("\\", "/")) == "spark_tiny.pdb"
