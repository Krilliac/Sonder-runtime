"""domain/build/templates.py: closed argv templates and model validation."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.domain.build import file_api
from sonder_runtime.domain.build.model import (
    BuildAction,
    BuildSystem,
    BuildWorld,
    NetworkPolicy,
    finalize_model,
)
from sonder_runtime.domain.build.msbuild import model_from_msbuild
from sonder_runtime.domain.build.presets import parse_cmake_presets
from sonder_runtime.domain.build.templates import (
    PLACEHOLDERS,
    TEMPLATES,
    TemplateRejected,
    build_argv,
    clamp_jobs,
    clamp_timeout,
    command_digest,
    network_hardening_args,
    parse_build_profiles,
    validate_placeholder,
    validate_request_against_model,
)
from sonder_runtime.domain.build.tool_targets import classify_targets

FIX = Path(__file__).parent / "fixtures" / "cpp_build"
SOURCE = "/work/sparklite"
BUILD = SOURCE + "/build/ninja-debug"
EXE = "/usr/bin/tool"


def _cmake_model(compile_db: bool = True):
    folder = FIX / "file_api" / "sonder"
    files = {path.name: path.read_bytes() for path in folder.iterdir()}
    presets = parse_cmake_presets(
        (FIX / "sparklite" / "CMakePresets.json").read_bytes(),
        includes={"presets/common.json": (FIX / "sparklite" / "presets" / "common.json").read_bytes()},
        source_root=SOURCE)
    index = file_api.parse_reply_index(files[file_api.newest_index_name(files)])
    return file_api.model_from_file_api(index=index, objects=files, source_root=SOURCE,
                                        build_dir=BUILD, project_label="sparklite",
                                        presets=presets.all(), compile_db_available=compile_db)


def _msbuild_model():
    root = FIX / "msbuild"
    projects = {rel: (root / rel).read_bytes() for rel in (
        "src/core/core.vcxproj", "src/game/game.vcxproj", "tools/tools.vcxproj")}
    props = {rel: (root / rel).read_bytes() for rel in ("props/engine.props", "props/common.props")}
    return model_from_msbuild(solution=(root / "SparkLite.sln").read_bytes(),
                              solution_label="SparkLite.sln", projects=projects, props=props,
                              source_root="C:/src/SparkLite", project_label="SparkLite")


VALUES = {
    "source_dir": SOURCE, "build_dir": BUILD, "generator": "Ninja", "config": "Debug",
    "target": "game", "jobs": "4", "preset": "ninja-debug", "build_preset": "ninja-debug",
    "file_target": "/work/sparklite/src/core/math.cpp^", "log_file": "/state/build-runs/j/msbuild.log",
    "binlog": "/state/build-runs/j/build.binlog", "project_file": "C:/src/SparkLite/src/core/core.vcxproj",
    "platform": "x64",
}


def test_every_template_renders():
    rendered = {}
    for template_id, template in TEMPLATES.items():
        values = {name: VALUES[name] for name in template.placeholders() if name in VALUES}
        if template_id == "msbuild.build":
            values["target"] = "Engine\\SparkLite_Core:Build"
        if template_id == "msbuild.compile_one":
            values["file_target"] = "C:/src/SparkLite/src/core/math.cpp"
        lists = {"net": network_hardening_args(template.system)} if "{net}" in str(template.segments) else {}
        argv = build_argv(template, executable=EXE, values=values, lists=lists)
        assert argv[0] == EXE
        rendered[template_id] = argv
    assert rendered["cmake.build"] == (EXE, "--build", BUILD, "--config", "Debug", "--target", "game",
                                       "--parallel", "4")
    assert rendered["ninja.compile_one"][-1] == "/work/sparklite/src/core/math.cpp^"
    assert "-f" in rendered["ninja.compile_one"]  # build-{config}.ninja for multi-config
    assert rendered["cmake.configure.preset"] == (EXE, "--preset", "ninja-debug",
                                                  *network_hardening_args(BuildSystem.CMAKE))
    msbuild = rendered["msbuild.build"]
    assert "-t:Engine\\SparkLite_Core:Build" in msbuild and "-p:Platform=x64" in msbuild
    assert "-p:VcpkgManifestInstall=false" in msbuild
    assert "-flp:LogFile=/state/build-runs/j/msbuild.log;Verbosity=normal;Encoding=UTF-8" in msbuild
    single = build_argv("cmake.build", executable=EXE, values={"build_dir": BUILD, "jobs": "2"})
    assert single == (EXE, "--build", BUILD, "--parallel", "2")


def test_unknown_placeholders_and_missing_values_raise():
    with pytest.raises(TemplateRejected):
        build_argv("cmake.build", executable=EXE, values={"build_dir": BUILD, "jobs": "1", "evil": "x"})
    with pytest.raises(TemplateRejected):
        build_argv("cmake.build", executable=EXE, values={"jobs": "1"})
    with pytest.raises(TemplateRejected):
        build_argv("no.such.template", executable=EXE, values={})
    with pytest.raises(TemplateRejected):
        build_argv("cmake.build", executable="relative/cmake", values={"build_dir": BUILD, "jobs": "1"})
    with pytest.raises(TemplateRejected):
        build_argv("cmake.configure", executable=EXE,
                   values={"source_dir": SOURCE, "build_dir": BUILD, "generator": "Ninja"},
                   lists={"net": ("-DEVIL=1",)})
    with pytest.raises(TemplateRejected):
        build_argv("cmake.configure", executable=EXE,
                   values={"source_dir": SOURCE, "build_dir": BUILD, "generator": "Ninja"},
                   lists={"defines": ("-C/tmp/evil.cmake",)})
    ok = build_argv("cmake.configure", executable=EXE,
                    values={"source_dir": SOURCE, "build_dir": BUILD, "generator": "Ninja"},
                    lists={"defines": ("-DSPARK_OPT:BOOL=ON",)})
    assert "-DSPARK_OPT:BOOL=ON" in ok and "-DCMAKE_BUILD_TYPE=" not in " ".join(ok)
    assert PLACEHOLDERS >= {"project_file", "build_preset", "platform"}


INJECTION = ["-DFOO", "/p:X=1", "a;b", "all && calc", "$(Foo)", "%PATH%", "%3B", "a\x00b",
             "-f", "a:b", "x y", "a|b", "a`b", "a'b", 'a"b', "a,b", "a>b", "a^"]


@pytest.mark.parametrize("value", INJECTION)
def test_injection_corpus_is_refused_for_every_model_value(value):
    for name in ("target", "config", "preset", "build_preset"):
        template = {"target": "make.build", "config": "cmake.build", "preset": "cmake.configure.preset",
                    "build_preset": "cmake.build.preset"}[name]
        values = {"build_dir": BUILD, "jobs": "1", name: value}
        with pytest.raises(TemplateRejected):
            build_argv(template, executable=EXE, values=values)


def test_make_variables_and_platform_rules():
    for bad in ("VAR=1", "CC=evil", "-j100", "-fevil.mk"):
        with pytest.raises(TemplateRejected):
            build_argv("make.build", executable=EXE, values={"build_dir": BUILD, "jobs": "2", "target": bad})
    values = dict(VALUES, target="Build", platform="Any CPU", project_file="C:/p/x.vcxproj")
    argv = build_argv("msbuild.build", executable=EXE,
                      values={k: values[k] for k in ("project_file", "target", "config", "platform",
                                                     "jobs", "log_file", "binlog")})
    assert "-p:Platform=Any CPU" in argv
    with pytest.raises(TemplateRejected):
        build_argv("msbuild.build", executable=EXE, values=dict(
            {k: values[k] for k in ("project_file", "target", "config", "jobs", "log_file", "binlog")},
            platform="x64 ;evil"))


@pytest.mark.parametrize("target", ["Rebuild", "Publish", "Clean", "core:Rebuild", "core:Publish",
                                    "core:Clean", "core:Build:Rebuild", "rebuild", "CLEAN",
                                    "publish", "core:rebuild"])
def test_msbuild_destructive_targets_are_refused(target):
    with pytest.raises(TemplateRejected):
        build_argv("msbuild.build", executable=EXE, values={
            "solution": "C:/p/SparkLite.sln", "target": target, "config": "Debug",
            "platform": "x64", "jobs": "2", "log_file": "C:/s/m.log", "binlog": "C:/s/b.binlog"})


@pytest.mark.parametrize("name, value", [
    ("jobs", "\u00b2"), ("jobs", "\u0664"), ("file_target", "@rsp^"),
    ("build_dir", "//server/share/build"), ("build_dir", "\\\\server\\share\\build"),
    ("source_dir", "\\\\?\\C:\\src"), ("project_file", "//server/share/core.vcxproj"),
])
def test_placeholder_values_refused_without_crashing(name, value):
    template = TEMPLATES["ninja.compile_one"] if name == "file_target" else TEMPLATES["msbuild.build"]
    with pytest.raises(TemplateRejected):
        validate_placeholder(name, value, template=template)


def test_request_validation_against_the_cmake_model():
    model = _cmake_model()
    safety = classify_targets(model)
    ok = validate_request_against_model(SimpleNamespace(action="build", target="game", config="Debug",
                                                        platform="", preset="", build_preset="",
                                                        file="", generator=""), model, safety)
    assert ok.target_info.name == "game"
    for field, value, code in (("target", "nope", "UNKNOWN_TARGET"), ("target", "a:b", "UNKNOWN_TARGET"),
                               ("config", "Release", "UNKNOWN_CONFIG"),
                               ("platform", "x64", "UNKNOWN_PLATFORM"),
                               ("preset", "base", "UNKNOWN_PRESET"),
                               ("build_preset", "nope", "UNKNOWN_PRESET")):
        with pytest.raises(TemplateRejected) as excinfo:
            validate_request_against_model({"action": "build", field: value}, model, safety)
        assert excinfo.value.code == code, (field, value)
    unit = validate_request_against_model({"action": "compile_one", "file": "src/core/math.cpp"},
                                          model, safety)
    assert unit.unit.target == "core" and not unit.needs_target_build
    with pytest.raises(TemplateRejected) as excinfo:
        validate_request_against_model({"action": "compile_one", "file": "../etc/passwd"}, model, safety)
    assert excinfo.value.code == "UNKNOWN_FILE"
    preset = validate_request_against_model({"action": "configure", "preset": "ninja-debug"}, model, safety)
    assert preset.preset.binary_dir == BUILD
    no_db = _cmake_model(compile_db=False)
    with pytest.raises(TemplateRejected) as excinfo:
        validate_request_against_model({"action": "include_trace", "file": "src/core/math.cpp"},
                                       no_db, classify_targets(no_db))
    assert excinfo.value.code == "RUNNER_UNAVAILABLE"


def test_unresolvable_or_source_dir_presets_are_refused():
    model = _cmake_model()
    safety = classify_targets(model)
    bad = parse_cmake_presets(json.dumps({"version": 6, "configurePresets": [
        {"name": "env", "generator": "Ninja", "binaryDir": "$env{OUT}"},
        {"name": "insource", "generator": "Ninja", "binaryDir": "${sourceDir}"},
        {"name": "parent", "generator": "Ninja", "binaryDir": "${sourceParentDir}"}]}).encode(),
        includes={}, source_root=SOURCE)
    model = finalize_model(replace(model, presets=bad.all()))
    for name in ("env", "insource", "parent"):
        with pytest.raises(TemplateRejected) as excinfo:
            validate_request_against_model({"action": "configure", "preset": name}, model, safety)
        assert excinfo.value.code == "UNKNOWN_PRESET"


def test_utility_targets_are_refused_unless_allowlisted():
    model = _cmake_model()
    safety = classify_targets(model)
    for name in ("deploy", "slow", "shaders"):
        with pytest.raises(TemplateRejected) as excinfo:
            validate_request_against_model({"action": "build", "target": name}, model, safety)
        assert excinfo.value.code == "UTILITY_TARGET_REFUSED"
    allowed = validate_request_against_model({"action": "build", "target": "deploy"}, model, safety,
                                             operator_utility_allow=frozenset({"deploy"}))
    assert allowed.notes and "allowlist" in allowed.notes[0]
    assert validate_request_against_model({"action": "build", "target": "all"}, model, safety).target == "all"


def test_msbuild_request_platform_and_makefile_project():
    model = _msbuild_model()
    safety = classify_targets(model)
    ok = validate_request_against_model({"action": "build", "target": "Engine\\SparkLite_Core:Build",
                                         "config": "Release", "platform": "Gaming.Xbox.Scarlett.x64"},
                                        model, safety)
    assert ok.target_info.project == "src/core/core.vcxproj"
    with pytest.raises(TemplateRejected) as excinfo:
        validate_request_against_model({"action": "build", "target": "Tools"}, model, safety)
    assert excinfo.value.code == "UTILITY_TARGET_REFUSED"
    with pytest.raises(TemplateRejected) as excinfo:
        validate_request_against_model({"action": "build", "platform": "PS5"}, model, safety)
    assert excinfo.value.code == "UNKNOWN_PLATFORM"
    with pytest.raises(TemplateRejected) as excinfo:
        validate_request_against_model({"action": "configure"}, model, safety)
    assert excinfo.value.code == "ACTION_UNSUPPORTED"
    pch = validate_request_against_model({"action": "compile_one", "file": "src/core/pch.cpp"},
                                         model, safety)
    assert pch.needs_target_build


def test_command_digest_is_stable_and_binding():
    argv = ("cmake", "--build", BUILD)
    base = command_digest(argv, cwd_label="build", env_keys=["PATH", "HOME"],
                          world=BuildWorld.HOST, network=NetworkPolicy.ENFORCED_OFF)
    assert base == command_digest(argv, cwd_label="build", env_keys=("HOME", "PATH", "PATH"),
                                  world="host", network="enforced_off")
    assert base != command_digest(argv, cwd_label="build", env_keys=["PATH", "HOME"],
                                  world=BuildWorld.CONTAINER, network=NetworkPolicy.ENFORCED_OFF)
    assert base != command_digest(argv, cwd_label="build", env_keys=["PATH", "HOME"],
                                  world=BuildWorld.HOST, network=NetworkPolicy.ALLOWED)
    assert base != command_digest(argv, cwd_label="build", env_keys=["PATH"],
                                  world=BuildWorld.HOST, network=NetworkPolicy.ENFORCED_OFF)
    assert base != command_digest(argv + ("--target", "x"), cwd_label="build",
                                  env_keys=["PATH", "HOME"], world=BuildWorld.HOST,
                                  network=NetworkPolicy.ENFORCED_OFF)


def test_network_hardening_only_when_network_is_not_allowed():
    assert network_hardening_args(BuildSystem.CMAKE) == (
        "-DFETCHCONTENT_FULLY_DISCONNECTED=ON", "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON",
        "-DVCPKG_MANIFEST_INSTALL=OFF")
    assert network_hardening_args("msbuild") == ("-p:VcpkgManifestInstall=false",
                                                 "-p:RestorePackages=false")
    assert network_hardening_args(BuildSystem.CMAKE, allow_network=True) == ()
    assert network_hardening_args(BuildSystem.MSBUILD, True) == ()
    assert network_hardening_args(BuildSystem.MAKE) == ()


def test_timeouts_and_jobs_are_clamped():
    assert clamp_timeout(None, TEMPLATES["cmake.build"]) == 1800
    assert clamp_timeout(99_999) == 7200
    assert clamp_timeout(99_999, operator_max=20_000) == 20_000
    assert clamp_timeout(99_999, operator_max=10**9) == 86400
    assert clamp_timeout(1) == 30
    assert clamp_timeout("junk", TEMPLATES["ninja.compile_one"]) == 600
    assert clamp_jobs(999, 8) == 8 and clamp_jobs(None, 4) == 4 and clamp_jobs(0, 4) == 1


def test_operator_profiles():
    good = parse_build_profiles(json.dumps({"profiles": [{
        "name": "fastbuild", "executable": "fbuild", "network_daemon": True,
        "actions": {"build": ["-config", "{source_dir}/fbuild.bff", "{target}", "-j{jobs}"]},
    }]}).encode())
    template = good[0].template(BuildAction.BUILD)
    argv = build_argv(template, executable="/usr/bin/fbuild",
                      values={"source_dir": SOURCE, "target": "game", "jobs": "8"})
    assert argv == ("/usr/bin/fbuild", "-config", SOURCE + "/fbuild.bff", "game", "-j8")
    assert good[0].template(BuildAction.CONFIGURE) is None
    bad_documents = [
        {"profiles": [{"name": "x", "executable": "cmake", "actions": {"build": ["-P", "evil.cmake"]}}]},
        {"profiles": [{"name": "x", "executable": "cmake", "actions": {"build": ["--trace-expand"]}}]},
        {"profiles": [{"name": "x", "executable": "fbuild", "actions": {"deploy": ["x"]}}]},
        {"profiles": [{"name": "x", "executable": "fbuild", "actions": {"build": ["install"]}}]},
        {"profiles": [{"name": "x", "executable": "fbuild", "actions": {"build": ["{evil}"]}}]},
        {"profiles": [{"name": "x", "executable": "fbuild", "actions": {"build": ["a"]},
                       "defines": ["-DX=1\n"]}]},
        {"profiles": [{"name": "x", "executable": "../fbuild", "actions": {"build": ["a"]}}]},
        {"nope": []},
    ]
    for document in bad_documents:
        with pytest.raises(Exception):
            parse_build_profiles(json.dumps(document).encode())
