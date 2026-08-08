"""project_scaffold: the harness owns the recall-heavy project file formats.

The tests parse every structured file a scaffold emits (XML, JSON) and pin the
solution-file plumbing (GUID consistency, fixed project-type GUIDs) that the
local model was measured unable to recall.
"""
import json
import re
import xml.etree.ElementTree as ET

import pytest

import project_scaffold as ps

_GUID = "12345678-ABCD-4EF0-9876-543210FEDCBA"


def test_every_kind_renders_and_substitutes_the_name():
    for kind in ps.kinds():
        files = ps.render(kind, "Demo", guid=_GUID)
        assert files, kind
        joined = "\n".join(files.values()) + "\n".join(files)
        assert "@NAME@" not in joined and "@LOWER@" not in joined, kind
        assert "@GUID@" not in joined, kind
        assert "Demo" in joined or "demo" in joined, kind


def test_msvc_scaffold_has_consistent_solution_plumbing():
    files = ps.render("cpp-msvc", "Fib", guid=_GUID)
    sln = files["Fib.sln"]
    vcx = files["Fib.vcxproj"]

    # The .sln names the project file and carries the fixed C++ project-type
    # GUID; the project GUID appears in both files identically.
    assert '"Fib.vcxproj"' in sln
    assert "8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942" in sln
    assert sln.count("{%s}" % _GUID) >= 5  # project line + 4 config rows
    assert "<ProjectGuid>{%s}</ProjectGuid>" % _GUID in vcx

    # The .vcxproj is well-formed XML and references the emitted sources.
    root = ET.fromstring(vcx)
    assert root.tag.endswith("Project")
    assert "src/main.cpp" in files and "include/Fib.h" in files
    assert 'Include="src\\main.cpp"' in vcx
    assert 'Include="include\\Fib.h"' in vcx


def test_csharp_scaffold_is_sdk_style_with_cs_type_guid():
    files = ps.render("csharp", "App", guid=_GUID)
    assert "9A19103F-16F7-4668-BE54-9A1E7A4F7556" in files["App.sln"]
    root = ET.fromstring(files["App.csproj"])
    assert root.attrib.get("Sdk") == "Microsoft.NET.Sdk"
    assert "namespace App;" in files["Program.cs"]


def test_node_package_json_is_valid_json():
    files = ps.render("node", "MyTool")
    data = json.loads(files["package.json"])
    assert data["name"] == "mytool"
    assert data["main"] == "index.js"


def test_java_pom_is_well_formed_xml():
    files = ps.render("java-maven", "Thing")
    root = ET.fromstring(files["pom.xml"])
    assert root.tag.endswith("project")
    assert "src/main/java/Main.java" in files


def test_rust_python_go_use_identifier_names():
    rust = ps.render("rust", "My-Crate")
    assert 'name = "my_crate"' in rust["Cargo.toml"]
    py = ps.render("python", "My-Pkg")
    assert "src/my_pkg/__main__.py" in py
    go = ps.render("go", "My-Mod")
    assert go["go.mod"].startswith("module my_mod")


def test_aliases_normalize_and_bad_input_is_rejected():
    assert ps.normalize_kind("C++") == "cpp-msvc"
    assert ps.normalize_kind("c#") == "csharp"
    assert ps.normalize_kind("js") == "node"
    assert ps.normalize_kind("brainfudge") is None
    with pytest.raises(ValueError):
        ps.render("brainfudge", "x")
    with pytest.raises(ValueError):
        ps.render("rust", "///")


def test_fresh_guid_is_generated_when_not_injected():
    files = ps.render("cpp-msvc", "G")
    match = re.search(r"<ProjectGuid>\{([0-9A-F-]{36})\}</ProjectGuid>",
                      files["G.vcxproj"])
    assert match, "vcxproj must carry an uppercase GUID"
    assert match.group(1) in files["G.sln"]


def test_typescript_scaffold_json_files_parse_and_pin_nodenext():
    files = ps.render("typescript", "WebTool")
    pkg = json.loads(files["package.json"])
    assert pkg["name"] == "webtool"
    assert pkg["scripts"]["build"] == "tsc"
    tsconfig = json.loads(files["tsconfig.json"])
    opts = tsconfig["compilerOptions"]
    assert opts["module"] == "NodeNext" and opts["moduleResolution"] == "NodeNext"
    assert opts["strict"] is True
    assert "src/index.ts" in files
    assert ps.normalize_kind("ts") == "typescript"


def test_python_without_tests_is_unchanged():
    expected = {
        "pyproject.toml",
        "src/demo/__init__.py",
        "src/demo/__main__.py",
    }
    default = ps.render("python", "Demo")
    without_tests = ps.render("python", "Demo", with_tests=False)
    assert without_tests == default
    assert set(without_tests) == expected


def test_opt_in_test_files_for_python_go_node_and_typescript():
    python = ps.render("python", "My-Pkg", with_tests=True)
    assert "tests/test_my_pkg.py" in python
    assert "from my_pkg.__main__ import main" in python["tests/test_my_pkg.py"]

    go = ps.render("go", "Demo", with_tests=True)
    assert "main_test.go" in go
    assert "func TestSmoke(t *testing.T)" in go["main_test.go"]

    node = ps.render("node", "Demo", with_tests=True)
    assert "test/index.test.js" in node
    assert "import { test } from 'node:test';" in node["test/index.test.js"]

    typescript = ps.render("typescript", "Demo", with_tests=True)
    assert "src/index.test.ts" in typescript
    assert "import assert from 'node:assert';" in typescript["src/index.test.ts"]


def test_rust_tests_are_appended_to_the_existing_main_file():
    base = ps.render("rust", "Demo")
    files = ps.render("rust", "Demo", with_tests=True)
    assert set(files) == set(base)
    assert "#[cfg(test)]" in files["src/main.rs"]


@pytest.mark.parametrize(
    "kind", ["csharp", "cpp-msvc", "cpp-cmake", "java-maven"]
)
def test_kinds_without_test_templates_are_unchanged(kind):
    assert ps.render(kind, "Demo", guid=_GUID, with_tests=True) == ps.render(
        kind, "Demo", guid=_GUID
    )
