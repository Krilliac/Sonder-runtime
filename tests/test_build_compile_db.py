"""domain/build/compile_db.py: parsing, Windows quoting, caps and flag facts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sonder_runtime.domain.build.compile_db import (
    MAX_ENTRIES,
    classify_flags,
    compiler_name,
    family_for_compiler,
    model_from_compile_db,
    parse_compile_commands,
    split_command,
    strip_launcher,
)
from sonder_runtime.domain.build.model import BuildDomainError, ModelSource, PchMode

FIXTURES = Path(__file__).parent / "fixtures" / "cpp_build" / "compile_db"


@pytest.mark.parametrize("command, expected", [
    ('prog "abc" d e', ["prog", "abc", "d", "e"]),
    ('prog a\\\\b d"e f"g h', ["prog", "a\\\\b", "de fg", "h"]),
    ('prog a\\\\\\"b c d', ["prog", 'a\\"b', "c", "d"]),
    ('prog a\\\\\\\\"b c" d e', ["prog", "a\\\\b c", "d", "e"]),
    ('prog a"b"" c d', ["prog", 'ab" c d']),
    ('"C:\\Program Files\\cl.exe" /c "x y.cpp"', ["C:\\Program Files\\cl.exe", "/c", "x y.cpp"]),
    ('prog "/DNAME=\\"x y\\""', ["prog", '/DNAME="x y"']),
    ('prog   \t spaced', ["prog", "spaced"]),
])
def test_windows_quoting_table(command, expected):
    assert list(split_command(command, windows=True)) == expected


def test_posix_splitting_and_bounds():
    assert split_command("g++ -DX='a b' -c 'x y.cpp'", windows=False) == (
        "g++", "-DX=a b", "-c", "x y.cpp")
    with pytest.raises(BuildDomainError):
        split_command("g++ 'unbalanced", windows=False)
    with pytest.raises(BuildDomainError):
        split_command("x " * 40_000, windows=False)
    with pytest.raises(BuildDomainError):
        split_command("a\x00b", windows=False)


def test_linux_database_parses():
    db = parse_compile_commands((FIXTURES / "linux_compile_commands.json").read_bytes(),
                                source_root="/work/sparklite",
                                build_dir="/work/sparklite/build/ninja-debug")
    rels = {entry.file_rel for entry in db.entries}
    assert {"src/core/math.cpp", "src/game/main.cpp", "tools/shadergen.cpp"} <= rels
    assert not db.truncated
    math = db.entry_for("src/core/math.cpp")
    assert math.family == "gnu" and not math.windows
    facts = classify_flags(math.argv)
    assert facts.pch is PchMode.USE and facts.std == "gnu++17" and facts.include_dir_count == 1


def test_windows_database_short_names_pch_launcher_and_rsp():
    db = parse_compile_commands((FIXTURES / "windows_compile_commands.json").read_bytes(),
                                source_root="C:/src/SparkLite", build_dir="C:/src/SparkLite/build")
    assert len(db.entries) == 4
    math = db.entry_for("src/core/math.cpp")
    assert math.windows and math.family == "msvc"
    assert compiler_name(math.argv[0]) == "cl"
    facts = classify_flags(math.argv)
    assert facts.pch is PchMode.USE and facts.std == "c++17"
    main = db.entry_for("src/game/main.cpp")
    assert main.argv[0].lower().endswith("sccache.exe")
    stripped, launcher = strip_launcher(main.argv, frozenset({"sccache"}))
    assert launcher == "sccache" and compiler_name(stripped[0]) == "cl"
    assert strip_launcher(main.argv, frozenset())[1] == ""
    assert '/DSPARK_TITLE="Spark Lite"' in main.argv
    facts = classify_flags(main.argv)
    assert facts.launcher == "sccache" and facts.pch is PchMode.FORCED_INCLUDE
    gen = db.entry_for("tools/gen tool/gen.cpp")
    assert gen.rsp_files == ("CMakeFiles\\gen.dir\\gen.cpp.obj.rsp",)
    assert "@CMakeFiles\\gen.dir\\gen.cpp.obj.rsp" in gen.argv  # recorded, not expanded
    entity = db.entry_for("src/core/entity.cpp")
    assert entity.family == "clang_cl"


def test_the_entry_cap_is_enforced():
    entries = [{"directory": "/p", "file": "f%d.cpp" % i, "arguments": ["cc", "-c", "f%d.cpp" % i]}
               for i in range(MAX_ENTRIES + 1)]
    db = parse_compile_commands(json.dumps(entries).encode(), source_root="/p", build_dir="/p/b")
    assert len(db.entries) == MAX_ENTRIES and db.truncated
    assert any("truncated" in note for note in db.notes)


def test_malformed_and_oversized_entries_are_skipped():
    entries = [
        {"directory": "/p", "file": "ok.cpp", "arguments": ["g++", "-c", "ok.cpp"]},
        {"directory": "/p", "file": "big.cpp", "command": "g++ " + "-DX " * 20_000},
        {"directory": "/p", "file": 7, "command": "g++"},
        "not an object",
    ]
    db = parse_compile_commands(json.dumps(entries).encode(), source_root="/p", build_dir="/p/b")
    assert [entry.file_rel for entry in db.entries] == ["ok.cpp"]
    assert db.truncated
    with pytest.raises(BuildDomainError):
        parse_compile_commands(b'{"not": "a list"}', source_root="/p", build_dir="/p/b")


def test_fallback_model_from_the_compile_database():
    db = parse_compile_commands((FIXTURES / "linux_compile_commands.json").read_bytes(),
                                source_root="/work/sparklite",
                                build_dir="/work/sparklite/build/ninja-debug")
    model = model_from_compile_db(db, source_root="/work/sparklite",
                                  build_dir="/work/sparklite/build/ninja-debug",
                                  project_label="sparklite", configs=("Debug",))
    assert model.source is ModelSource.COMPILE_DB and model.compile_db_available
    assert {target.name for target in model.targets} == {"core", "game", "crash_test", "shadergen"}
    math = model.units_for("src/core/math.cpp")[0]
    assert math.target == "core" and math.pch is PchMode.USE and math.family == "gnu"
    assert "/work/" not in repr([unit.file_label for unit in model.units])
    assert any("compile_commands.json only" in note for note in model.notes) and model.digest


def test_family_detection():
    assert family_for_compiler("/usr/bin/g++-13") == "gnu"
    assert family_for_compiler("/usr/bin/clang++-18") == "clang"
    assert family_for_compiler("C:\\LLVM\\bin\\clang-cl.exe") == "clang_cl"
    assert family_for_compiler("CL.EXE") == "msvc"
    assert family_for_compiler("/opt/evil/wrapper") == "other"
