"""Real compilers on planted defects: the parsed file and line match the plant.

Each test is skipped when its tool is absent. Processes are launched by the
test with a bounded timeout, never by the tool under test.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from sonder_runtime.domain.diagnostics.parsers import parse_diagnostics


def _run(argv, cwd, timeout=60, extra_env=None):
    env = dict(os.environ)
    env.update(extra_env or {})
    env.update({"NO_COLOR": "1", "TERM": "dumb", "CARGO_TERM_COLOR": "never",
                "GOFLAGS": "-mod=mod", "GOTOOLCHAIN": "local"})
    completed = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL, env=env,
    )
    return completed.stdout + "\n" + completed.stderr


C_SOURCE = """\
void helper(void) {
    int unused = 1;
}
int main(void) {
    return missing_identifier;
}
"""


@pytest.mark.parametrize("compiler", ["gcc", "clang"])
def test_real_c_compilers(tmp_path, compiler):
    if shutil.which(compiler) is None:
        pytest.skip("%s not installed" % compiler)
    (tmp_path / "t.c").write_text(C_SOURCE, encoding="utf-8")
    output = _run([compiler, "-Wall", "-fno-color-diagnostics" if compiler == "clang"
                   else "-fdiagnostics-color=never", "-c", "t.c", "-o", "t.o"], tmp_path)
    found = parse_diagnostics(output).diagnostics
    errors = [d for d in found if d.severity == "error"]
    warnings = [d for d in found if d.severity == "warning"]
    assert errors and errors[0].file == "t.c" and errors[0].line == 5, output
    assert "missing_identifier" in errors[0].message
    assert any(w.line == 2 and w.code == "-Wunused-variable" for w in warnings), output


def test_real_rustc_type_error(tmp_path):
    if shutil.which("rustc") is None:
        pytest.skip("rustc not installed")
    (tmp_path / "main.rs").write_text(
        'fn main() {\n    let x: i32 = "text";\n    println!("{}", x);\n}\n', encoding="utf-8",
    )
    output = _run(["rustc", "--color", "never", "--edition", "2021", "main.rs",
                   "-o", str(tmp_path / "out")], tmp_path, timeout=120)
    found = [d for d in parse_diagnostics(output).diagnostics if d.tool == "rustc"]
    first = found[0]
    assert (first.severity, first.code, first.file, first.line) == (
        "error", "E0308", "main.rs", 2,
    ), output


def test_real_go_vet_or_build(tmp_path):
    if shutil.which("go") is None:
        pytest.skip("go not installed")
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.20\n", encoding="utf-8")
    (tmp_path / "main.go").write_text(
        'package main\n\nfunc main() {\n\tundefinedName()\n}\n', encoding="utf-8",
    )
    cache = {} if os.environ.get("GOCACHE") else {"GOCACHE": str(tmp_path / "gocache")}
    output = _run(["go", "build", "./..."], tmp_path, timeout=120, extra_env=cache)
    found = [d for d in parse_diagnostics(output).diagnostics if d.tool == "go"]
    assert found and found[0].file == "main.go" and found[0].line == 4, output
    assert "undefinedName" in found[0].message


def test_real_tsc_no_emit(tmp_path):
    if shutil.which("tsc") is None:
        pytest.skip("tsc not installed")
    (tmp_path / "a.ts").write_text(
        "const n: number = 1;\nconst s: string = n;\nexport { s };\n", encoding="utf-8",
    )
    output = _run(["tsc", "--noEmit", "--pretty", "false", "a.ts"], tmp_path, timeout=120)
    found = [d for d in parse_diagnostics(output).diagnostics if d.tool == "tsc"]
    assert found and (found[0].file, found[0].line, found[0].code) == ("a.ts", 2, "TS2322"), output
