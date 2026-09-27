"""compile_db.sanitize_for_trace: an allowlist rebuild of the compile argv (F6)."""
from __future__ import annotations

from pathlib import Path

from sonder_runtime.domain.build.compile_db import (
    SanitizedArgv,
    TraceRefused,
    parse_compile_commands,
    sanitize_for_trace,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cpp_build" / "compile_db"
ROOTS = ("/work/sparklite", "/work/sparklite/build")
SRC = "/work/sparklite/src/core/math.cpp"


def _gnu(*extra: str, launcher: tuple[str, ...] = (), known=frozenset()):
    argv = (*launcher, "/usr/bin/g++", "-I/work/sparklite/src/core", "-DSPARK=1", "-std=gnu++17",
            *extra, "-o", "math.o", "-c", SRC)
    return sanitize_for_trace(argv, "gnu", source_file=SRC, roots=ROOTS, directory="/work/sparklite/build",
                              known_launchers=known)


def test_gnu_allowlist_keeps_only_preprocessing_flags():
    result = _gnu("-fno-exceptions", "-march=native", "-MD", "-MF", "x.d")
    assert isinstance(result, SanitizedArgv)
    assert result.argv == ("/usr/bin/g++", "-I/work/sparklite/src/core", "-DSPARK=1",
                           "-std=gnu++17", "-fno-exceptions", "-march=native", "-fsyntax-only",
                           "-H", SRC)
    assert result.dropped >= 5  # -MD -MF x.d -o math.o -c


def test_code_executing_gnu_flags_are_dropped():
    result = _gnu("-wrapper", "gdb,--args", "-fplugin=/tmp/evil.so", "-Xclang", "-load",
                  "-Xclang", "/tmp/evil.so", "-B/tmp/evil", "-specs=/tmp/x.specs",
                  "--gcc-toolchain=/tmp/tc", "-fuse-ld=/tmp/ld", "-mllvm", "-x86-foo")
    assert isinstance(result, SanitizedArgv)
    joined = " ".join(result.argv)
    for needle in ("evil", "wrapper", "plugin", "Xclang", "specs", "toolchain", "fuse-ld",
                   "-B", "mllvm", "gdb"):
        assert needle not in joined
    assert result.dangerous >= 6
    assert any("code-executing" in note for note in result.notes)


def test_cmake_pch_forced_include_becomes_the_real_header():
    db = parse_compile_commands((FIXTURES / "linux_compile_commands.json").read_bytes(),
                                source_root="/work/sparklite",
                                build_dir="/work/sparklite/build/ninja-debug")
    entry = db.entry_for("src/core/math.cpp")
    result = sanitize_for_trace(entry.argv, "gnu", source_file=entry.file,
                                roots=("/work/sparklite",), directory=entry.directory,
                                pch_header="/work/sparklite/src/core/pch.h")
    assert isinstance(result, SanitizedArgv)
    assert "cmake_pch" not in " ".join(result.argv)
    index = result.argv.index("-include")
    assert result.argv[index + 1] == "/work/sparklite/src/core/pch.h"
    assert result.forced_includes == ("/work/sparklite/src/core/pch.h",)
    assert "-o" not in result.argv and "-MF" not in result.argv


def test_launchers_are_stripped_only_when_inventoried():
    refused = _gnu(launcher=("/usr/bin/sccache",))
    assert isinstance(refused, TraceRefused) and refused.code == "RUNNER_UNAVAILABLE"
    stripped = _gnu(launcher=("/usr/bin/sccache",), known=frozenset({"sccache"}))
    assert isinstance(stripped, SanitizedArgv) and stripped.argv[0] == "/usr/bin/g++"
    assert any("launcher sccache stripped" in note for note in stripped.notes)


def test_unknown_compiler_or_family_mismatch_is_refused():
    assert isinstance(sanitize_for_trace(("/opt/x/wrapper", "-c", SRC), "gnu", source_file=SRC,
                                         roots=ROOTS), TraceRefused)
    assert isinstance(sanitize_for_trace(("cl.exe", "/c", SRC), "gnu", source_file=SRC,
                                         roots=ROOTS), TraceRefused)
    missing = sanitize_for_trace(("g++", "-c", "/work/sparklite/other.cpp"), "gnu", source_file=SRC,
                                 roots=ROOTS)
    assert isinstance(missing, TraceRefused) and missing.code == "UNKNOWN_FILE"


def test_msvc_flags_pch_and_dangerous_switches():
    src = "C:/src/SparkLite/src/core/math.cpp"
    argv = ("C:\\PROGRA~1\\cl.exe", "/nologo", "/TP", "-IC:\\src\\SparkLite\\src\\core", "/DWIN32",
            "/EHsc", "-std:c++17", "/Zc:__cplusplus", "/permissive-", "/YuC:/src/SparkLite/b/cmake_pch.hxx",
            "/FpC:\\src\\x.pch", "/FIC:/src/SparkLite/b/CMakeFiles/core.dir/cmake_pch.hxx",
            "/FIC:/src/SparkLite/src/core/forced.h", "/FIC:/Windows/evil.h", "/B1C:\\evil\\c1.dll",
            "/B2C:\\evil\\c2.dll", "/BxC:\\evil\\x.exe", "/d1reportAllClassLayout", "/d2cgsummary",
            "/analyze:pluginC:\\evil\\p.dll", "/Fomath.obj", "/c", "C:\\src\\SparkLite\\src\\core\\math.cpp")
    result = sanitize_for_trace(argv, "msvc", source_file=src, roots=("C:/src/SparkLite",),
                                directory="C:/src/SparkLite/b",
                                pch_header="C:/src/SparkLite/src/core/pch.h")
    assert isinstance(result, SanitizedArgv)
    joined = " ".join(result.argv)
    for needle in ("/Yu", "/Yc", "/Fp", "cmake_pch", "/B1", "/B2", "/Bx", "/d1", "/d2",
                   "/analyze", "evil", "/Fo"):
        assert needle not in joined
    assert "/FIC:/src/SparkLite/src/core/forced.h" in result.argv
    assert "/FIC:/src/SparkLite/src/core/pch.h" in result.argv
    assert result.argv[-4:] == ("/Zs", "/showIncludes", "/nologo",
                                "C:\\src\\SparkLite\\src\\core\\math.cpp")
    assert "/IC:\\src\\SparkLite\\src\\core" in result.argv
    assert "/Zc:__cplusplus" in result.argv and "/permissive-" in result.argv
    assert result.dangerous >= 6


def test_response_files_expand_once_and_nesting_is_refused():
    src = "/work/sparklite/src/core/math.cpp"
    argv = ("g++", "@flags.rsp", "-c", src)
    missing = sanitize_for_trace(argv, "gnu", source_file=src, roots=ROOTS)
    assert isinstance(missing, TraceRefused)
    ok = sanitize_for_trace(argv, "gnu", source_file=src, roots=ROOTS,
                            rsp_contents={"flags.rsp": "-I/work/sparklite/src/core\n-DA=1\n-fplugin=x.so"})
    assert isinstance(ok, SanitizedArgv)
    assert "-I/work/sparklite/src/core" in ok.argv and "-DA=1" in ok.argv
    assert "-fplugin=x.so" not in ok.argv
    nested = sanitize_for_trace(argv, "gnu", source_file=src, roots=ROOTS,
                                rsp_contents={"flags.rsp": "-DA=1 @inner.rsp"})
    assert isinstance(nested, TraceRefused) and "nested" in nested.reason
    huge = sanitize_for_trace(argv, "gnu", source_file=src, roots=ROOTS,
                              rsp_contents={"flags.rsp": "-DA " * 70_000})
    assert isinstance(huge, TraceRefused)


def test_forced_includes_outside_the_roots_or_non_headers_are_dropped():
    result = _gnu("-include", "/etc/passwd", "-include", "/work/sparklite/src/core/keys.pem",
                  "-include", "/work/sparklite/src/core/config.h")
    assert isinstance(result, SanitizedArgv)
    assert "/etc/passwd" not in result.argv and "/work/sparklite/src/core/keys.pem" not in result.argv
    assert "/work/sparklite/src/core/config.h" in result.argv
