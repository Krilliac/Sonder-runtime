"""repair.normalize_for_scan + scan_hostile_directives (F7)."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.build.repair import (
    EditScope,
    include_checker,
    normalize_for_scan,
    scan_hostile_directives,
)

SCOPE = EditScope(roots=("/work/sparklite",))
OK = include_checker(SCOPE, "src/core/math.cpp")


def _scan(text: str) -> tuple[str, ...]:
    return scan_hostile_directives(normalize_for_scan(text), include_ok=OK)


HOSTILE = [
    '%:include "/etc/passwd"\n',
    '%:  include </etc/shadow>\n',
    '??=include "/etc/passwd"\n',
    '#inc\\\nlude "/etc/passwd"\n',
    '# /* c */ include "C:\\\\Windows\\\\win.ini"\n',
    '#include "\\\\\\\\server\\\\share\\\\x.h"\n',
    '_Pragma("comment(lib, \\"evil.lib\\")")\n',
    '__pragma(comment(linker, "/include:evil"))\n',
    '#pragma comment(lib, "ws2_32.lib")\n',
    '#pragma section(".CRT$XCU", read)\n',
    '#pragma init_seg(lib)\n',
    '#pragma code_seg(".evil")\n',
    '#pragma data_seg(".evil")\n',
    '#pragma include_alias("a.h", "/etc/passwd")\n',
    '#pragma GCC poison printf\n',
    '#line 1 "other.cpp"\n',
    '#using <mscorlib.dll>\n',
    '#import "progid:Evil.Thing"\n',
    '#embed "/etc/passwd"\n',
    '#include "../../../.env"\n',
    '#include "../.env"\n',
    '#include "keys.pem"\n',
    '#include "config.json"\n',
    '#include "noext"\n',
    '#include MACRO_PATH\n',
    '#include <../../etc/passwd>\n',
    'asm(".incbin \\"/etc/passwd\\"");\n',
    '__asm__ volatile(".section .init_array\\n .quad evil\\n .previous");\n',
    'static const char blob[] = ".incbin \\"/etc/shadow\\"";\n',
    '__attribute__((constructor)) static void run() {}\n',
    '__attribute__ ((section(".init_array"))) void* p = 0;\n',
    '[[gnu::constructor]] static void run2() {}\n',
    '__declspec(allocate(".CRT$XCU")) void* q = 0;\n',
    'bool h = __has_include("/etc/passwd");\n',
    'bool e = __has_embed("../../../../etc/shadow.h");\n',
    # Bypass shapes: literal concatenation, case, macros and stringizing.
    'asm(".inc" "bin \\"/etc/passwd\\"");\n',
    '__asm__(".INCBIN \\"/etc/passwd\\"");\n',
    '#define BLOB .incbin "/etc/passwd"\n',
    '#define S(x) #x\nasm(S(.incbin "f"));\n',
    '__attribute__( (constructor)) static void run3() {}\n',
    '__attribute((constructor)) static void run4() {}\n',
    '[[__gnu__::constructor]] static void run5() {}\n',
    '[[using gnu: constructor]] static void run6() {}\n',
    '#include <.env>\n',
    '#include <id_rsa>\n',
    '#include <.ssh/id_ed25519>\n',
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_corpus_is_rejected(text):
    assert _scan(text), text


BENIGN = [
    '#include "math.h"\n',
    '#include "detail/impl.inl"\n',
    '#include <vector>\n',
    '#include <sys/types.h>\n',
    '#pragma once\n',
    '#pragma warning(disable: 4996)\n',
    '#pragma GCC diagnostic ignored "-Wunused"\n',
    'const char* s = "#include \\"/etc/passwd\\"";  // not a directive\n',
    '// #include "/etc/passwd" in a comment\n',
    '/* #line 5 */ int x = 0;\n',
    'auto raw = R"(\n#include "/etc/passwd"\n)";\n',
    'float length(const Vec3& v) { return std::sqrt(dot(v, v)); }\n',
    'bool h = __has_include("math.h");\n',
    '#define SPARK_VERSION 3\n',
    'int big = 1\'000\'000; auto f = cfg.file; auto s = obj.section();\n',
    '[[nodiscard]] int g();\n',
    'const char* both = "a" "b";\n',
]


@pytest.mark.parametrize("text", BENIGN)
def test_benign_code_passes(text):
    assert _scan(text) == (), text


def test_string_growth_cap():
    big = 'const char* s = "' + "a" * (70 * 1024) + '";\n'
    assert any("64 KiB" in reason for reason in _scan(big))


def test_normalization_steps():
    assert normalize_for_scan("#inc\\\nlude <x>") == "#include <x>"
    assert normalize_for_scan("%:define A %:%: B") == "#define A ## B"
    assert normalize_for_scan("a /* x */ b // y\nc") == "a   b  \nc"
    masked = normalize_for_scan('R"x(#include "/etc/passwd")x"')
    assert "#include" not in masked and masked.startswith('R"x(')


def test_include_checker_containment():
    top = include_checker(SCOPE, "main.cpp")
    assert not top("../outside.h")
    nested = include_checker(SCOPE, "src/core/math.cpp")
    assert nested("../game/x.h") and nested("../../root.h") and not nested("../../../up.h")
