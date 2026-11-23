"""project_scaffold -- deterministic multi-language project skeletons.

Why this exists: asked in plain language for "a full C++ MSVC project", the
local model produced a correct .cpp and .h and NO .sln/.vcxproj at all.
Solution-file plumbing (format headers, project-type GUIDs, configuration
blocks) is pure recall -- the measured worst case for a local model. So the
harness owns those formats as templates and the model only supplies the one
fact it actually has: the project's name.

`render(kind, name)` returns {relative_path: content} and never touches disk;
callers write through the guarded file_ops layer so the filesystem policy
stays in force. No model calls happen here -- this module is deliberately
deterministic apart from freshly generated GUIDs (injectable for tests).

Stdlib only.
"""
import re
import uuid

# One name token per substitution job, replaced literally (no str.format, so
# template braces in JSON/XML/C never need escaping):
#   @NAME@   project name as given (file stems, display names)
#   @LOWER@  lowercased identifier form (crate/package/module names)
#   @GUID@   project GUID, uppercase, braceless
_TOKEN = re.compile(r"@(NAME|LOWER|GUID)@")

ALIASES = {
    "c++": "cpp-msvc",
    "cpp": "cpp-msvc",
    "msvc": "cpp-msvc",
    "visualstudio": "cpp-msvc",
    "cmake": "cpp-cmake",
    "c#": "csharp",
    "cs": "csharp",
    "dotnet": "csharp",
    "js": "node",
    "javascript": "node",
    "ts": "typescript",
    "ts-node": "typescript",
    "py": "python",
    "golang": "go",
    "java": "java-maven",
    "maven": "java-maven",
    "web": "web-app",
    "webapp": "web-app",
    "html": "web-app",
    "flutter-dart": "dart",
    "c": "c-cmake",
    "c11": "c-cmake",
    "kt": "kotlin-gradle",
    "kotlin": "kotlin-gradle",
    "rb": "ruby",
    "ex": "elixir",
    "exs": "elixir",
    "ps1": "powershell",
    "pwsh": "powershell",
    "sh": "bash-cli",
    "bash": "bash-cli",
    "shell": "bash-cli",
}


def identifier(name):
    """A safe lowercase identifier derived from a project name."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(name or "")).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = "p_" + cleaned
    return cleaned.lower()


def kinds():
    """Supported scaffold kinds, canonical names only."""
    return tuple(sorted(_TEMPLATES))


def normalize_kind(kind):
    """Canonical kind for `kind`, or None when it is not supported."""
    key = str(kind or "").strip().lower()
    key = ALIASES.get(key, key)
    return key if key in _TEMPLATES else None


# --- templates ---------------------------------------------------------------
# Visual Studio's fixed project-type GUIDs (documented, never change):
#   C++ (.vcxproj)        8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942
#   C# SDK-style (.csproj) 9A19103F-16F7-4668-BE54-9A1E7A4F7556
_VS_CPP_TYPE = "8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942"
_VS_CS_TYPE = "9A19103F-16F7-4668-BE54-9A1E7A4F7556"


def _sln(project_type_guid, project_file):
    return (
        "Microsoft Visual Studio Solution File, Format Version 12.00\n"
        "# Visual Studio Version 17\n"
        "VisualStudioVersion = 17.0.31903.59\n"
        "MinimumVisualStudioVersion = 10.0.40219.1\n"
        'Project("{' + project_type_guid + '}") = "@NAME@", "' + project_file +
        '", "{@GUID@}"\n'
        "EndProject\n"
        "Global\n"
        "\tGlobalSection(SolutionConfigurationPlatforms) = preSolution\n"
        "\t\tDebug|x64 = Debug|x64\n"
        "\t\tRelease|x64 = Release|x64\n"
        "\tEndGlobalSection\n"
        "\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n"
        "\t\t{@GUID@}.Debug|x64.ActiveCfg = Debug|x64\n"
        "\t\t{@GUID@}.Debug|x64.Build.0 = Debug|x64\n"
        "\t\t{@GUID@}.Release|x64.ActiveCfg = Release|x64\n"
        "\t\t{@GUID@}.Release|x64.Build.0 = Release|x64\n"
        "\tEndGlobalSection\n"
        "\tGlobalSection(SolutionProperties) = preSolution\n"
        "\t\tHideSolutionNode = FALSE\n"
        "\tEndGlobalSection\n"
        "EndGlobal\n"
    )


_VCXPROJ = """<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="Build" ToolsVersion="17.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Label="ProjectConfigurations">
    <ProjectConfiguration Include="Debug|x64">
      <Configuration>Debug</Configuration>
      <Platform>x64</Platform>
    </ProjectConfiguration>
    <ProjectConfiguration Include="Release|x64">
      <Configuration>Release</Configuration>
      <Platform>x64</Platform>
    </ProjectConfiguration>
  </ItemGroup>
  <PropertyGroup Label="Globals">
    <ProjectGuid>{@GUID@}</ProjectGuid>
    <RootNamespace>@NAME@</RootNamespace>
    <WindowsTargetPlatformVersion>10.0</WindowsTargetPlatformVersion>
  </PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.Default.props" />
  <PropertyGroup Label="Configuration">
    <ConfigurationType>Application</ConfigurationType>
    <PlatformToolset>v143</PlatformToolset>
    <CharacterSet>Unicode</CharacterSet>
  </PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.props" />
  <PropertyGroup>
    <IncludePath>$(ProjectDir)include;$(IncludePath)</IncludePath>
  </PropertyGroup>
  <ItemDefinitionGroup>
    <ClCompile>
      <WarningLevel>Level4</WarningLevel>
      <LanguageStandard>stdcpp20</LanguageStandard>
      <SDLCheck>true</SDLCheck>
    </ClCompile>
    <Link>
      <SubSystem>Console</SubSystem>
    </Link>
  </ItemDefinitionGroup>
  <ItemGroup>
    <ClCompile Include="src\\main.cpp" />
  </ItemGroup>
  <ItemGroup>
    <ClInclude Include="include\\@NAME@.h" />
  </ItemGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets" />
</Project>
"""

_MAIN_CPP = """#include "@NAME@.h"

#include <iostream>

int main()
{
    std::cout << "@NAME@ up and running\\n";
    return 0;
}
"""

_HEADER_H = """#pragma once

// Public declarations for @NAME@.
"""

_CMAKELISTS = """cmake_minimum_required(VERSION 3.25)
project(@NAME@ LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(@NAME@ src/main.cpp)
target_include_directories(@NAME@ PRIVATE include)
"""

_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">

  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <RootNamespace>@NAME@</RootNamespace>
  </PropertyGroup>

</Project>
"""

_PROGRAM_CS = """namespace @NAME@;

internal static class Program
{
    private static void Main()
    {
        Console.WriteLine("@NAME@ up and running");
    }
}
"""

_CARGO_TOML = """[package]
name = "@LOWER@"
version = "0.1.0"
edition = "2021"

[dependencies]
"""

_MAIN_RS = """fn main() {
    println!("@LOWER@ up and running");
}
"""

_PYPROJECT = """[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "@LOWER@"
version = "0.1.0"
requires-python = ">=3.10"

[tool.setuptools.packages.find]
where = ["src"]
"""

_PY_MAIN = """def main() -> None:
    print("@LOWER@ up and running")


if __name__ == "__main__":
    main()
"""

_PACKAGE_JSON = """{
  "name": "@LOWER@",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "index.js",
  "scripts": {
    "start": "node index.js"
  }
}
"""

_INDEX_JS = """console.log("@LOWER@ up and running");
"""

_TS_PACKAGE_JSON = """{
  "name": "@LOWER@",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "tsc",
    "start": "node dist/index.js"
  },
  "devDependencies": {
    "typescript": "^5"
  }
}
"""

_TSCONFIG = """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "outDir": "dist",
    "rootDir": "src",
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true
  }
}
"""

_INDEX_TS = """console.log("@LOWER@ up and running");
"""

_GO_MOD = """module @LOWER@

go 1.22
"""

_MAIN_GO = """package main

import "fmt"

func main() {
\tfmt.Println("@LOWER@ up and running")
}
"""

_POM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>local.@LOWER@</groupId>
  <artifactId>@LOWER@</artifactId>
  <version>0.1.0</version>
  <packaging>jar</packaging>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
</project>
"""

_MAIN_JAVA = """public final class Main {
    public static void main(String[] args) {
        System.out.println("@NAME@ up and running");
    }
}
"""

_GITIGNORE_RUST = "/target\n"
_GITIGNORE_NODE = "node_modules/\n"

# kind -> {relative path template: content template}
_TEMPLATES = {
    "cpp-msvc": {
        "@NAME@.sln": _sln(_VS_CPP_TYPE, "@NAME@.vcxproj"),
        "@NAME@.vcxproj": _VCXPROJ,
        "src/main.cpp": _MAIN_CPP,
        "include/@NAME@.h": _HEADER_H,
    },
    "cpp-cmake": {
        "CMakeLists.txt": _CMAKELISTS,
        "src/main.cpp": _MAIN_CPP,
        "include/@NAME@.h": _HEADER_H,
    },
    "csharp": {
        "@NAME@.sln": _sln(_VS_CS_TYPE, "@NAME@.csproj"),
        "@NAME@.csproj": _CSPROJ,
        "Program.cs": _PROGRAM_CS,
    },
    "rust": {
        "Cargo.toml": _CARGO_TOML,
        "src/main.rs": _MAIN_RS,
        ".gitignore": _GITIGNORE_RUST,
    },
    "python": {
        "pyproject.toml": _PYPROJECT,
        "src/@LOWER@/__init__.py": "",
        "src/@LOWER@/__main__.py": _PY_MAIN,
    },
    "node": {
        "package.json": _PACKAGE_JSON,
        "index.js": _INDEX_JS,
        ".gitignore": _GITIGNORE_NODE,
    },
    "typescript": {
        "package.json": _TS_PACKAGE_JSON,
        "tsconfig.json": _TSCONFIG,
        "src/index.ts": _INDEX_TS,
        ".gitignore": _GITIGNORE_NODE + "dist/\n",
    },
    "go": {
        "go.mod": _GO_MOD,
        "main.go": _MAIN_GO,
    },
    "java-maven": {
        "pom.xml": _POM_XML,
        "src/main/java/Main.java": _MAIN_JAVA,
    },
    "web-app": {
        "index.html": (
            "<!doctype html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\" />\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
            "  <title>@NAME@</title>\n"
            "  <link rel=\"stylesheet\" href=\"styles.css\" />\n"
            "</head>\n"
            "<body>\n"
            "  <main class=\"app\">\n"
            "    <h1>@NAME@</h1>\n"
            "    <p id=\"status\">Ready.</p>\n"
            "    <button id=\"action-btn\" type=\"button\">Run Action</button>\n"
            "  </main>\n"
            "  <script type=\"module\" src=\"app.js\"></script>\n"
            "</body>\n"
            "</html>\n"
        ),
        "styles.css": (
            ":root { color-scheme: dark; font-family: system-ui, sans-serif; }\n"
            "body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0f172a; color: #e2e8f0; }\n"
            ".app { padding: 2rem; border-radius: 12px; background: #1e293b; border: 1px solid #334155; text-align: center; }\n"
            "button { margin-top: 1rem; padding: 0.6rem 1.2rem; border-radius: 8px; border: none; background: #38bdf8; color: #0f172a; font-weight: 600; cursor: pointer; }\n"
        ),
        "app.js": (
            "export function nextMessage(count) {\n"
            "  return `@NAME@ action count: ${count}`;\n"
            "}\n\n"
            "if (typeof document !== 'undefined') {\n"
            "  let clicks = 0;\n"
            "  const status = document.getElementById('status');\n"
            "  const btn = document.getElementById('action-btn');\n"
            "  btn?.addEventListener('click', () => {\n"
            "    clicks += 1;\n"
            "    if (status) status.textContent = nextMessage(clicks);\n"
            "  });\n"
            "}\n"
        ),
    },
    "dart": {
        "pubspec.yaml": (
            "name: @LOWER@\n"
            "description: @NAME@ Dart application.\n"
            "version: 0.1.0\n"
            "environment:\n"
            "  sdk: ^3.0.0\n"
            "dev_dependencies:\n"
            "  test: ^1.25.0\n"
        ),
        "lib/@LOWER@.dart": (
            "String greeting(String target) => 'Hello from @NAME@, $target!';\n"
        ),
        "bin/@LOWER@.dart": (
            "import 'package:@LOWER@/@LOWER@.dart';\n\n"
            "void main(List<String> args) {\n"
            "  final target = args.isNotEmpty ? args.first : 'world';\n"
            "  print(greeting(target));\n"
            "}\n"
        ),
        "test/@LOWER@_test.dart": (
            "import 'package:test/test.dart';\n"
            "import 'package:@LOWER@/@LOWER@.dart';\n\n"
            "void main() {\n"
            "  test('greeting formats target', () {\n"
            "    expect(greeting('Sonder'), 'Hello from @NAME@, Sonder!');\n"
            "  });\n"
            "}\n"
        ),
    },
    "c-cmake": {
        "CMakeLists.txt": (
            "cmake_minimum_required(VERSION 3.16)\n"
            "project(@NAME@ LANGUAGES C)\n"
            "set(CMAKE_C_STANDARD 11)\n"
            "set(CMAKE_C_STANDARD_REQUIRED ON)\n"
            "add_executable(@NAME@ src/main.c)\n"
            "target_include_directories(@NAME@ PRIVATE include)\n"
        ),
        "include/@LOWER@.h": (
            "#ifndef @LOWER@_H\n"
            "#define @LOWER@_H\n\n"
            "const char *@LOWER@_greeting(void);\n\n"
            "#endif\n"
        ),
        "src/main.c": (
            "#include <stdio.h>\n"
            "#include \"@LOWER@.h\"\n\n"
            "const char *@LOWER@_greeting(void) { return \"Hello from @NAME@!\"; }\n\n"
            "int main(void) {\n"
            "    puts(@LOWER@_greeting());\n"
            "    return 0;\n"
            "}\n"
        ),
    },
    "zig": {
        "build.zig": (
            "const std = @import(\"std\");\n\n"
            "pub fn build(b: *std.Build) void {\n"
            "    const target = b.standardTargetOptions(.{});\n"
            "    const optimize = b.standardOptimizeOption(.{});\n"
            "    const exe = b.addExecutable(.{\n"
            "        .name = \"@LOWER@\",\n"
            "        .root_source_file = b.path(\"src/main.zig\"),\n"
            "        .target = target,\n"
            "        .optimize = optimize,\n"
            "    });\n"
            "    b.installArtifact(exe);\n"
            "    const unit_tests = b.addTest(.{\n"
            "        .root_source_file = b.path(\"src/main.zig\"),\n"
            "        .target = target,\n"
            "        .optimize = optimize,\n"
            "    });\n"
            "    const test_step = b.step(\"test\", \"Run unit tests\");\n"
            "    test_step.dependOn(&b.addRunArtifact(unit_tests).step);\n"
            "}\n"
        ),
        "src/main.zig": (
            "const std = @import(\"std\");\n\n"
            "pub fn add(a: i32, b: i32) i32 {\n"
            "    return a + b;\n"
            "}\n\n"
            "pub fn main() !void {\n"
            "    std.debug.print(\"Hello from @NAME@!\\n\", .{});\n"
            "}\n\n"
            "test \"add works\" {\n"
            "    try std.testing.expectEqual(@as(i32, 5), add(2, 3));\n"
            "}\n"
        ),
    },
    "kotlin-gradle": {
        "settings.gradle.kts": "rootProject.name = \"@LOWER@\"\n",
        "build.gradle.kts": (
            "plugins {\n"
            "    kotlin(\"jvm\") version \"2.0.21\"\n"
            "    application\n"
            "}\n\n"
            "repositories { mavenCentral() }\n"
            "application { mainClass.set(\"MainKt\") }\n"
        ),
        "src/main/kotlin/Main.kt": (
            "fun greeting(target: String): String = \"Hello from @NAME@, $target!\"\n\n"
            "fun main() {\n"
            "    println(greeting(\"world\"))\n"
            "}\n"
        ),
    },
    "swift": {
        "Package.swift": (
            "// swift-tools-version: 5.9\n"
            "import PackageDescription\n\n"
            "let package = Package(\n"
            "    name: \"@NAME@\",\n"
            "    targets: [\n"
            "        .executableTarget(name: \"@NAME@\"),\n"
            "    ]\n"
            ")\n"
        ),
        "Sources/@NAME@/main.swift": (
            "func greeting(_ target: String) -> String {\n"
            "    \"Hello from @NAME@, \\(target)!\"\n"
            "}\n"
            "print(greeting(\"world\"))\n"
        ),
    },
    "elixir": {
        "mix.exs": (
            "defmodule @NAME@.MixProject do\n"
            "  use Mix.Project\n\n"
            "  def project do\n"
            "    [app: :@LOWER@, version: \"0.1.0\", elixir: \"~> 1.15\", deps: []]\n"
            "  end\n"
            "end\n"
        ),
        "lib/@LOWER@.ex": (
            "defmodule @NAME@ do\n"
            "  def greeting(target \\\\ \"world\"), do: \"Hello from @NAME@, #{target}!\"\n"
            "end\n"
        ),
        "test/@LOWER@_test.exs": (
            "ExUnit.start()\n"
            "defmodule @NAME@Test do\n"
            "  use ExUnit.Case\n"
            "  test \"greeting\" do\n"
            "    assert @NAME@.greeting(\"Sonder\") == \"Hello from @NAME@, Sonder!\"\n"
            "  end\n"
            "end\n"
        ),
    },
    "ruby": {
        "Gemfile": "source 'https://rubygems.org'\n\ngem 'rspec'\n",
        "lib/@LOWER@.rb": (
            "module @NAME@\n"
            "  def self.greeting(target = 'world')\n"
            "    \"Hello from @NAME@, #{target}!\"\n"
            "  end\n"
            "end\n"
        ),
        "spec/@LOWER@_spec.rb": (
            "require_relative '../lib/@LOWER@'\n\n"
            "RSpec.describe @NAME@ do\n"
            "  it 'greets cleanly' do\n"
            "    expect(described_class.greeting('Sonder')).to eq('Hello from @NAME@, Sonder!')\n"
            "  end\n"
            "end\n"
        ),
    },
    "php": {
        "composer.json": (
            "{\n"
            "  \"name\": \"local/@LOWER@\",\n"
            "  \"description\": \"@NAME@ PHP project\",\n"
            "  \"autoload\": {\"psr-4\": {\"App\\\\\": \"src/\"}}\n"
            "}\n"
        ),
        "src/App.php": (
            "<?php\n"
            "declare(strict_types=1);\n\n"
            "namespace App;\n\n"
            "final class App {\n"
            "    public static function greeting(string $target = 'world'): string {\n"
            "        return \"Hello from @NAME@, {$target}!\";\n"
            "    }\n"
            "}\n"
        ),
        "index.php": (
            "<?php\n"
            "require __DIR__ . '/src/App.php';\n"
            "echo App\\App::greeting('world') . PHP_EOL;\n"
        ),
    },
    "lua": {
        "src/@LOWER@.lua": (
            "local M = {}\n"
            "function M.greeting(target)\n"
            "  return 'Hello from @NAME@, ' .. (target or 'world') .. '!'\n"
            "end\n"
            "return M\n"
        ),
        "src/main.lua": (
            "local mod = require('src.@LOWER@')\n"
            "print(mod.greeting('world'))\n"
        ),
    },
    "flutter": {
        "pubspec.yaml": (
            "name: @LOWER@\n"
            "description: @NAME@ Flutter application.\n"
            "version: 0.1.0+1\n"
            "environment:\n"
            "  sdk: ^3.0.0\n"
            "dependencies:\n"
            "  flutter:\n"
            "    sdk: flutter\n"
            "dev_dependencies:\n"
            "  flutter_test:\n"
            "    sdk: flutter\n"
        ),
        "lib/main.dart": (
            "import 'package:flutter/material.dart';\n\n"
            "void main() => runApp(const @NAME@App());\n\n"
            "class @NAME@App extends StatelessWidget {\n"
            "  const @NAME@App({super.key});\n"
            "  @override\n"
            "  Widget build(BuildContext context) {\n"
            "    return const MaterialApp(\n"
            "      home: Scaffold(body: Center(child: Text('Hello from @NAME@!'))),\n"
            "    );\n"
            "  }\n"
            "}\n"
        ),
        "test/widget_test.dart": (
            "import 'package:flutter_test/flutter_test.dart';\n"
            "import 'package:@LOWER@/main.dart';\n\n"
            "void main() {\n"
            "  testWidgets('renders greeting', (tester) async {\n"
            "    await tester.pumpWidget(const @NAME@App());\n"
            "    expect(find.text('Hello from @NAME@!'), findsOneWidget);\n"
            "  });\n"
            "}\n"
        ),
    },
    "powershell": {
        "@NAME@.psm1": (
            "function Get-@NAME@Greeting {\n"
            "    param([string]$Target = 'world')\n"
            "    return \"Hello from @NAME@, $Target!\"\n"
            "}\n"
            "Export-ModuleMember -Function Get-@NAME@Greeting\n"
        ),
        "tests/@NAME@.Tests.ps1": (
            "Import-Module \"$PSScriptRoot\\..\\@NAME@.psm1\" -Force\n"
            "$msg = Get-@NAME@Greeting -Target 'Sonder'\n"
            "if ($msg -ne 'Hello from @NAME@, Sonder!') { throw \"Unexpected: $msg\" }\n"
            "Write-Host 'PASS'\n"
        ),
    },
    "bash-cli": {
        "bin/@LOWER@.sh": (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "target=\"${1:-world}\"\n"
            "printf 'Hello from @NAME@, %s!\\n' \"$target\"\n"
        ),
        "tests/test_@LOWER@.sh": (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "out=\"$(bash \"$(dirname \"$0\")/../bin/@LOWER@.sh\" Sonder)\"\n"
            "test \"$out\" = 'Hello from @NAME@, Sonder!'\n"
            "echo PASS\n"
        ),
    },
}


def render(kind, name, guid=None):
    """Return {relative_path: content} for one project skeleton.

    Raises ValueError on an unsupported kind or an empty name. `guid` is an
    injection seam for tests; production callers leave it None and get a fresh
    uppercase GUID, which is the only non-deterministic value in a scaffold.
    """
    canonical = normalize_kind(kind)
    if canonical is None:
        raise ValueError(
            "unsupported project kind %r; supported: %s"
            % (kind, ", ".join(kinds()))
        )
    base = re.sub(r"[^A-Za-z0-9_.-]+", "", str(name or "").strip())
    if not base:
        raise ValueError("project name must contain a usable file-name character")

    values = {
        "NAME": base,
        "LOWER": identifier(base),
        "GUID": (guid or str(uuid.uuid4())).upper().strip("{}"),
    }

    def fill(text):
        return _TOKEN.sub(lambda m: values[m.group(1)], text)

    return {fill(path): fill(content) for path, content in _TEMPLATES[canonical].items()}
