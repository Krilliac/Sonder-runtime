"""domain/build/output.py + attribution.py over captured and labelled synthetic logs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sonder_runtime.domain.build import file_api
from sonder_runtime.domain.build.compile_db import SanitizedArgv, parse_compile_commands, sanitize_for_trace
from sonder_runtime.domain.build.repair import (
    BuildProgress,
    EditScope,
    RepairEvidence,
    parse_candidate_patch,
    progress_key,
    repair_prompt,
    validate_patch,
)
from sonder_runtime.domain.build.report import relabel_attribution, relabel_trace
from sonder_runtime.domain.build.templates import (
    TemplateRejected,
    build_argv,
    network_hardening_args,
    validate_request_against_model,
)
from sonder_runtime.domain.build.tool_targets import classify_targets
from sonder_runtime.domain.build.attribution import (
    attribute_steps,
    detect_non_english_msvc,
    first_errors,
    normalized_error_lines,
)
from sonder_runtime.domain.build.model import BuildModel, BuildSystem
from sonder_runtime.domain.build.output import (
    parse_build_diagnostics,
    parse_cmake_configure_errors,
    parse_dash_H,
    parse_make_failures,
    parse_msbuild_projects,
    parse_msvc_codeless,
    parse_ninja_segments,
    parse_show_includes,
)

LOGS = Path(__file__).parent / "fixtures" / "cpp_build" / "logs"


def _log(name: str) -> str:
    return (LOGS / name).read_text(encoding="utf-8")


def _msbuild_model() -> BuildModel:
    return BuildModel(project_label="SparkLite", source_root="C:/src/SparkLite", build_dir="",
                      system=BuildSystem.MSBUILD)


def test_ninja_gcc_first_error_per_tu():
    text = _log("ninja-gxx.log")
    segments = parse_ninja_segments(text)
    failed = [segment for segment in segments if segment.failed]
    assert [segment.tu_label for segment in failed] == [
        "/work/sparklite/src/game/main.cpp", "/work/sparklite/src/core/math.cpp"]
    assert [segment.project for segment in failed] == ["game", "core"]
    dset = parse_build_diagnostics(text)
    atts = attribute_steps(text, dset, segments)
    assert [(item.kind, item.label) for item in atts] == [
        ("tu", "/work/sparklite/src/game/main.cpp"), ("tu", "/work/sparklite/src/core/math.cpp")]
    firsts = first_errors(atts)
    assert [(item.line, item.message.split(";")[0]) for item in firsts] == [
        (10, "'class Entity' has no member named 'pos'"), (19, "'lenght' was not declared in this scope")]
    lines = normalized_error_lines(firsts)
    assert all(len(line) <= 240 for line in lines) and "lenght" in lines[1]


def test_ninja_clang_attribution_includes_notes():
    text = _log("ninja-clangxx.log")
    atts = attribute_steps(text, parse_build_diagnostics(text), parse_ninja_segments(text))
    assert len(atts) == 2
    math = next(item for item in atts if item.label.endswith("math.cpp"))
    assert math.error_count == 1 and any(diag.severity == "note" for diag in math.diagnostics)
    assert math.first_error.line == 19


def test_make_failures_are_attributed_to_their_tu():
    text = _log("make-gxx.log")
    segments = parse_make_failures(text)
    assert len(segments) == 1 and segments[0].tu_label == "src/core/math.cpp"
    assert segments[0].project == "core"
    atts = attribute_steps(text, parse_build_diagnostics(text), segments)
    assert atts[0].kind == "tu" and atts[0].first_error.line == 19


def test_a_header_error_is_attributed_to_the_including_tu():
    body = _log("parity-gxx.log")
    text = ("[1/2] Building CXX object CMakeFiles/parity.dir/cc.cpp.o\n"
            "FAILED: CMakeFiles/parity.dir/cc.cpp.o\n"
            "/usr/bin/c++ -o CMakeFiles/parity.dir/cc.cpp.o -c /work/parity/cc.cpp\n"
            + body + "ninja: build stopped: subcommand failed.\n")
    atts = attribute_steps(text, parse_build_diagnostics(text), parse_ninja_segments(text))
    assert len(atts) == 1 and atts[0].label == "/work/parity/cc.cpp"
    assert atts[0].first_error.file == "local.h" and atts[0].error_count == 2


def test_gcc_clang_and_clang_cl_triples_are_identical():
    triples = {}
    for name in ("parity-gxx.log", "parity-clangxx.log", "parity-clang-cl.log"):
        dset = parse_build_diagnostics(_log(name))
        triples[name] = sorted((item.file, item.line, item.severity) for item in dset.diagnostics
                               if item.severity in ("error", "fatal"))
    assert triples["parity-gxx.log"] == [("cc.cpp", 5, "error"), ("local.h", 3, "error")]
    assert triples["parity-gxx.log"] == triples["parity-clangxx.log"] == triples["parity-clang-cl.log"]


def test_clang_cl_codeless_fallback_and_show_includes():
    text = _log("parity-clang-cl.log")
    codeless = parse_msvc_codeless(text)
    assert [(item.file, item.line, item.col, item.code) for item in codeless] == [
        ("local.h", 3, 37, ""), ("cc.cpp", 5, 22, "")]
    trace = parse_show_includes(text, root_file="cc.cpp")
    assert trace.edges == (("cc.cpp", "local.h", 1),) and not trace.pch_consumed


def test_msbuild_serial_codes_and_provable_tu():
    text = _log("msbuild-vm-serial.synthetic.log")
    dset = parse_build_diagnostics(text)
    codes = {item.code for item in dset.diagnostics}
    assert {"C2065", "C2039", "C1083", "C1853", "LNK2019", "LNK1120", "MSB8020", "MSB3073"} <= codes
    rows = {row.raw_line_no: row for row in parse_msbuild_projects(text, dset)}
    by_code = {item.code: rows[item.raw_line_no] for item in dset.diagnostics}
    assert by_code["C2065"].project.endswith("src/core/core.vcxproj")
    assert by_code["C2065"].tu_label == "C:/src/SparkLite/src/core/math.cpp"
    assert by_code["LNK2019"].tu_label == "" and by_code["LNK2019"].project.endswith("game.vcxproj")
    assert by_code["MSB3073"].project.endswith("tools/tools.vcxproj")
    atts = attribute_steps(text, dset, (), model=_msbuild_model())
    kinds = {(item.kind, item.label.rsplit("/", 1)[-1]) for item in atts}
    assert ("tu", "math.cpp") in kinds and ("project", "tools.vcxproj") in kinds


def test_msbuild_mp_interleaving_recovers_projects_and_never_guesses_header_tus():
    text = _log("msbuild-vm-mp.synthetic.log")
    dset = parse_build_diagnostics(text)
    rows = {row.raw_line_no: row for row in parse_msbuild_projects(text, dset)}
    header = next(item for item in dset.diagnostics if item.code == "C4244")
    # math.h's warning follows the math.cpp error, but /MP echoed four files
    # first: no TU is provable for it, only the project.
    assert rows[header.raw_line_no].tu_label == ""
    assert rows[header.raw_line_no].project.endswith("core.vcxproj")
    atts = attribute_steps(text, dset, (), model=_msbuild_model())
    by_label = {(item.kind, item.label.rsplit("/", 1)[-1]): item for item in atts}
    assert by_label[("tu", "entity.cpp")].project.endswith("core.vcxproj")
    assert by_label[("tu", "main.cpp")].project.endswith("game.vcxproj")
    assert by_label[("project", "core.vcxproj")].warning_count == 1
    assert by_label[("project", "game.vcxproj")].error_count == 2  # LNK2019 + LNK1120
    assert first_errors(atts)[0].message.startswith("'positon'")


def test_cmake_configure_errors():
    text = _log("configure-error.log")
    diags = parse_cmake_configure_errors(text)
    assert len(diags) == 1
    assert (diags[0].file, diags[0].line, diags[0].severity, diags[0].code) == (
        "CMakeLists.txt", 5, "error", "CMAKE")
    assert "SparkLite requires the Vulkan SDK" in diags[0].message
    assert any(item.code == "CMAKE" for item in parse_build_diagnostics(text, configure=True).diagnostics)


def test_dash_h_traces():
    trace = parse_dash_H(_log("trace-gxx-H.log"), root_file="/work/sparklite/src/core/entity.cpp")
    assert trace.edges[0] == ("/work/sparklite/src/core/entity.cpp", "/work/sparklite/src/core/entity.h", 1)
    assert trace.edges[1] == ("/work/sparklite/src/core/entity.h", "/work/sparklite/src/core/math.h", 2)
    assert trace.max_depth >= 7 and trace.unique_headers > 5 and not trace.truncated
    assert not trace.pch_consumed
    used = parse_dash_H("! /b/cmake_pch.hxx.gch\n. /s/a.h\n", root_file="/s/a.cpp")
    assert used.pch_consumed
    clang = parse_dash_H(_log("trace-clangxx-H.log"), root_file="/work/sparklite/src/core/entity.cpp")
    assert clang.edges[0][1] == "/work/sparklite/src/core/entity.h"


def test_show_includes_synthetic_nesting_and_bounds():
    trace = parse_show_includes(_log("cl-showincludes.synthetic.log"), root_file="math.cpp")
    assert trace.edges[0] == ("math.cpp", "C:/src/SparkLite/src/core/pch.h", 1)
    assert trace.edges[2][0].endswith("/include/string") and trace.edges[2][2] == 3
    assert trace.max_depth == 3
    many = "".join("Note: including file: /h/%d.h\n" % i for i in range(6000))
    assert parse_show_includes(many, root_file="x.cpp").truncated


def test_non_english_detection():
    assert detect_non_english_msvc(_log("cl-german.synthetic.log"))
    assert not detect_non_english_msvc(_log("msbuild-vm-serial.synthetic.log"))
    assert not detect_non_english_msvc(_log("ninja-gxx.log"))


# --- real processes: the pure domain driving a real CMake + Ninja build -----------

SPARKLITE = Path(__file__).parent / "fixtures" / "cpp_build" / "sparklite"
FIXES = (
    ("src/core/math.cpp", "float l = lenght(v);", "float l = length(v);"),
    ("src/game/main.cpp", "length(player.pos)", "length(player.position)"),
    ("src/core/entity.cpp", "    position.z += delta.z;\n}\n",
     "    position.z += delta.z;\n}\n\nvoid Entity::tick(float dt) {\n    (void)dt;\n}\n"),
)


def _run(argv, cwd, compiler, timeout=240):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "LANG": "C",
           "LC_ALL": "C", "CXX": compiler, "TERM": "dumb", "NINJA_STATUS": "[%f/%t] "}
    completed = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    return completed.returncode, completed.stdout + completed.stderr


def _snapshot(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and "build" not in path.relative_to(root).parts}


def _load_model(source: Path, build: Path):
    reply = build / ".cmake" / "api" / "v1" / "reply"
    files = {path.name: path.read_bytes() for path in reply.iterdir()}
    index = file_api.parse_reply_index(files[file_api.newest_index_name(files)])
    return file_api.model_from_file_api(
        index=index, objects=files, source_root=str(source), build_dir=str(build),
        project_label="sparklite", compile_db_available=(build / "compile_commands.json").exists())


class _ScriptedModel:
    """Deterministic stand-in for the repair model: fixed JSON answers in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def propose(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


@pytest.mark.integration
@pytest.mark.parametrize("compiler", ["g++", "clang++"])
def test_real_ninja_build_model_diagnostics_trace_and_bounded_fix(tmp_path, compiler):
    for tool in ("cmake", "ninja", compiler):
        if shutil.which(tool) is None:
            pytest.skip("%s not installed" % tool)
    source = tmp_path / "sparklite"
    shutil.copytree(SPARKLITE, source)
    build = source / "build" / "ninja-debug"
    query = build / ".cmake" / "api" / "v1" / "query" / "client-sonder"
    query.mkdir(parents=True)
    (query / "query.json").write_bytes(file_api.query_document())
    cmake, ninja = shutil.which("cmake"), shutil.which("ninja")
    configure = build_argv("cmake.configure", executable=cmake, values={
        "source_dir": str(source), "build_dir": str(build), "generator": "Ninja", "config": "Debug"},
        lists={"net": network_hardening_args(BuildSystem.CMAKE)})
    code, output = _run(configure, source, compiler)
    assert code == 0, output

    model = _load_model(source, build)
    safety = classify_targets(model)
    assert model.target("shadergen").build_time_tool and "deploy" in safety.utility
    assert model.units_for("src/core/math.cpp")[0].pch_header == "src/core/pch.h"
    with pytest.raises(TemplateRejected) as excinfo:
        validate_request_against_model({"action": "build", "target": "deploy"}, model, safety)
    assert excinfo.value.code == "UTILITY_TARGET_REFUSED"

    request = validate_request_against_model({"action": "build", "target": "game", "config": "Debug"},
                                             model, safety)
    build_cmd = build_argv("cmake.build", executable=cmake, values={
        "build_dir": str(build), "target": request.target, "jobs": "2"})
    code, output = _run(build_cmd, source, compiler)
    assert code != 0
    dset = parse_build_diagnostics(output)
    atts = [relabel_attribution(item, source_root=str(source), build_dir=str(build))
            for item in attribute_steps(output, dset, parse_ninja_segments(output), model=model)]
    seeded = {("src/core/math.cpp", 19), ("src/game/main.cpp", 10)}
    firsts = {(item.first_error.file, item.first_error.line) for item in atts if item.first_error}
    assert firsts and firsts <= seeded, output
    assert all(item.kind == "tu" and item.label in {"src/core/math.cpp", "src/game/main.cpp"}
               for item in atts if item.first_error)

    unit = validate_request_against_model({"action": "compile_one", "file": "src/core/math.cpp"},
                                          model, safety).unit
    one = build_argv("ninja.compile_one", executable=ninja, values={
        "build_dir": str(build), "file_target": str(source / unit.file_rel) + "^"})
    code, output = _run(one, source, compiler)
    assert code != 0
    one_atts = attribute_steps(output, parse_build_diagnostics(output), parse_ninja_segments(output))
    assert [item.first_error.line for item in one_atts] == [19]

    db = parse_compile_commands((build / "compile_commands.json").read_bytes(),
                                source_root=str(source), build_dir=str(build))
    entry = db.entry_for("src/core/entity.cpp")
    sanitized = sanitize_for_trace(entry.argv, unit.family, source_file=entry.file,
                                   roots=(str(source),), directory=entry.directory,
                                   pch_header=str(source / "src/core/pch.h"))
    assert isinstance(sanitized, SanitizedArgv), sanitized
    code, output = _run(sanitized.argv, entry.directory, compiler)
    assert code == 0, output
    trace = relabel_trace(parse_dash_H(output, root_file=entry.file,
                                       forced_includes=sanitized.forced_includes),
                          source_root=str(source),
                          build_dir=str(build))
    headers = trace.headers()
    assert "src/core/pch.h" in headers and "src/core/math.h" in headers

    # A bounded repair loop driven only by the domain policy and a scripted model.
    scope = EditScope(roots=(str(source),), excluded_rel=safety.tool_sources,
                      generated_rel=frozenset(model.generated_rel), excluded_dirs=("build",))
    fixer = _ScriptedModel([
        json.dumps({"hunks": [{"file": "src/core/math.cpp", "anchor": '#include "math.h"',
                               "replace": '#include "/etc/shadow"\n#include "math.h"'}]}),
        json.dumps({"hunks": [{"file": "tools/shadergen.cpp", "anchor": "return out ? 0 : 1;",
                               "replace": "return 0;"}]}),
        json.dumps({"hunks": [{"file": rel, "anchor": anchor, "replace": replace}
                              for rel, anchor, replace in FIXES],
                    "rationale": "typo, renamed member, missing definition"}),
    ])
    before_tree = _snapshot(source)
    initial = BuildProgress(True, True, errors_total=dset.count("error"),
                            failed_units=sum(1 for item in atts if item.first_error))
    best = initial
    changed_lines = 0
    touched: set[str] = set()
    outcomes = []
    for attempt in range(1, 5):
        focus = next(iter(sorted(firsts)))[0]
        evidence = RepairEvidence(
            target="game", config="Debug", focus_file=focus,
            source_window=(source / focus).read_text()[:12_000],
            diagnostics=tuple(item.first_error for item in atts if item.first_error)[:24],
            progress=best, compiler=unit.family)
        patch = parse_candidate_patch(fixer.propose(repair_prompt(evidence)), model_id="scripted")
        current = {rel: (source / rel).read_text() for rel in patch.files()
                   if (source / rel).is_file()}
        texts, reasons = validate_patch(patch, scope=scope, current=current,
                                        loop_changed_lines_used=changed_lines,
                                        loop_files_used=frozenset(touched))
        if reasons:
            outcomes.append(reasons[0].split(":", 1)[0])
            continue
        for rel, text in texts.items():
            (source / rel).write_text(text)
            touched.add(rel)
        code, output = _run(build_cmd, source, compiler)
        dset = parse_build_diagnostics(output)
        progress = BuildProgress(True, True, errors_total=dset.count("error"),
                                 failed_units=int(code != 0))
        assert progress_key(progress) < progress_key(initial)
        best = progress
        outcomes.append("built")
        if progress.fixed and code == 0:
            break
    assert outcomes == ["HOSTILE_DIRECTIVE", "BUILD_TIME_TOOL_SOURCE", "built"]
    assert best.fixed and len(fixer.prompts) == 3
    after_tree = _snapshot(source)
    changed = {rel for rel in after_tree if after_tree[rel] != before_tree.get(rel)}
    assert changed == {"src/core/math.cpp", "src/game/main.cpp", "src/core/entity.cpp"}
    assert "/etc/shadow" not in (source / "src/core/math.cpp").read_text().split("//")[0]
