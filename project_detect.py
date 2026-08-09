"""Bounded evidence-only project and command discovery.

Repository content is parsed as data and never executed. Command candidates
are emitted only when a matching manifest, declared script, target, plugin, or
source entry point supplies the evidence needed for that command shape.
"""
from __future__ import annotations

import configparser
import json
import os
import re
import stat
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import file_ops


HARD_MAX_FILES = 1_000
HARD_MAX_TOTAL_BYTES = 8_000_000
HARD_MAX_FILE_BYTES = 512_000
HARD_MAX_RESULTS = 2_000
HARD_MAX_DEPTH = 16
HARD_MAX_DISCOVERY_ENTRIES = 50_000
HARD_MAX_ERRORS = 200

DEFAULT_MAX_FILES = 200
DEFAULT_MAX_TOTAL_BYTES = 2_000_000
DEFAULT_MAX_FILE_BYTES = 256_000
DEFAULT_MAX_RESULTS = 500
DEFAULT_MAX_DEPTH = 8

SKIP_DIRECTORIES = frozenset({
    *file_ops.SENSITIVE_READ_DIRECTORIES,
    ".cache", ".gradle", ".idea", ".next", ".tox", ".venv", ".vscode",
    "__pycache__", "build", "coverage", "dist", "node_modules", "target",
    "vendor", "venv",
})
EXACT_MANIFESTS = {
    "pyproject.toml": "python-project",
    "setup.cfg": "python-config",
    "setup.py": "python-setup",
    "pytest.ini": "pytest-config",
    "tox.ini": "tox-config",
    "package.json": "node-package",
    "cargo.toml": "cargo",
    "go.mod": "go-module",
    "cmakelists.txt": "cmake",
    "meson.build": "meson",
    "makefile": "make",
    "gnumakefile": "make",
    "pom.xml": "maven",
    "settings.gradle": "gradle",
    "settings.gradle.kts": "gradle",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "pubspec.yaml": "dart-package",
    "composer.json": "composer",
    "gemfile": "ruby-bundle",
    "rakefile": "rake",
    "package.swift": "swift-package",
    "dockerfile": "dockerfile",
    "compose.yaml": "docker-compose",
    "compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
    "docker-compose.yml": "docker-compose",
}
SUFFIX_MANIFESTS = {
    ".sln": "dotnet-solution",
    ".csproj": "dotnet-csharp",
    ".fsproj": "dotnet-fsharp",
    ".vbproj": "dotnet-vb",
    ".vcxproj": "msbuild-cpp",
}
MANIFEST_ROLES = {
    "python-project": ("build", "test", "runtime"),
    "python-config": ("build", "test"),
    "python-setup": ("build",),
    "pytest-config": ("test",), "tox-config": ("test",),
    "node-package": ("build", "test", "runtime"),
    "cargo": ("build", "test", "runtime"),
    "go-module": ("build", "test", "runtime"),
    "cmake": ("build", "test"), "meson": ("build", "test"),
    "make": ("build", "test"),
    "maven": ("build", "test", "runtime"), "gradle": ("build", "test", "runtime"),
    "dotnet-solution": ("build", "test"),
    "dotnet-csharp": ("build", "test", "runtime"),
    "dotnet-fsharp": ("build", "test", "runtime"),
    "dotnet-vb": ("build", "test", "runtime"),
    "msbuild-cpp": ("build",),
    "dart-package": ("test", "runtime"), "composer": ("test", "runtime"),
    "ruby-bundle": ("runtime",), "rake": ("build", "test"),
    "swift-package": ("build", "test", "runtime"),
    "dockerfile": ("build", "runtime"), "docker-compose": ("runtime",),
}

NODE_FRAMEWORKS = {
    "@angular/core": "Angular", "@nestjs/core": "NestJS", "express": "Express",
    "next": "Next.js", "react": "React", "svelte": "Svelte", "vite": "Vite",
    "vue": "Vue",
}
PYTHON_FRAMEWORKS = {
    "django": "Django", "fastapi": "FastAPI", "flask": "Flask",
    "pytest": "pytest", "starlette": "Starlette",
}
DOTNET_FRAMEWORKS = {
    "avalonia": "Avalonia", "microsoft.aspnetcore.app": "ASP.NET Core",
    "microsoft.net.test.sdk": ".NET Test SDK", "nunit": "NUnit",
    "xunit": "xUnit",
}
PHP_FRAMEWORKS = {"laravel/framework": "Laravel", "symfony/framework-bundle": "Symfony"}
RUBY_FRAMEWORKS = {"rails": "Rails", "rspec": "RSpec"}


def _bounded(value, default: int, ceiling: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(ceiling, parsed))


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _requested_path(path: str) -> Path:
    candidate = Path(str(path or ".")).expanduser()
    if not candidate.is_absolute():
        candidate = file_ops.workspace_root() / candidate
    return candidate.absolute()


def _reject_symlinked_root(path: str) -> None:
    requested = _requested_path(path)
    if _is_reparse(requested):
        raise PermissionError("project detection root may not be a symlink or junction")
    lexical = os.path.normcase(os.path.normpath(os.path.abspath(str(requested))))
    physical = os.path.normcase(os.path.normpath(os.path.realpath(str(requested))))
    if lexical != physical:
        raise PermissionError("project detection root traverses a symlink or junction")


def _manifest_kind(name: str) -> str:
    lowered = name.casefold()
    return EXACT_MANIFESTS.get(lowered, SUFFIX_MANIFESTS.get(Path(lowered).suffix, ""))


def _iter_manifests(root: Path, max_depth: int):
    if root.is_file():
        kind = _manifest_kind(root.name)
        if kind:
            yield root, root.name, kind, 1
        return
    stack = [(root, "", 0)]
    discovered = 0
    while stack:
        directory, prefix, depth = stack.pop()
        try:
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    discovered += 1
                    if discovered > HARD_MAX_DISCOVERY_ENTRIES:
                        yield None, "", "__DISCOVERY_LIMIT__", discovered
                        return
                    entries.append(entry)
            entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError as exc:
            yield None, prefix or ".", "__ERROR__:%s" % exc, discovered
            continue
        children = []
        for entry in entries:
            relative = "%s/%s" % (prefix, entry.name) if prefix else entry.name
            child = Path(entry.path)
            try:
                if entry.is_symlink() or _is_reparse(child):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth and entry.name.casefold() not in SKIP_DIRECTORIES:
                        children.append((child, relative, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    kind = _manifest_kind(entry.name)
                    if kind:
                        yield child, relative.replace("\\", "/"), kind, discovered
            except OSError:
                yield None, relative.replace("\\", "/"), "__ERROR__:could not inspect entry", discovered
        for child in reversed(children):
            stack.append(child)


def _cwd(relative: str) -> str:
    parent = Path(relative).parent.as_posix()
    return "." if parent in {"", "."} else parent


def _normalize_python_dependency(value: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", str(value or ""))
    return match.group(1).replace("_", "-").casefold() if match else ""


class _Collector:
    def __init__(self, result: dict):
        self.result = result
        self.seen = set()

    def add(self, category: str, row: dict) -> None:
        identity = row
        if category == "commands":
            identity = {
                key: row[key] for key in ("kind", "cwd", "argv", "platform")
            }
        key = (category, json.dumps(identity, sort_keys=True, ensure_ascii=False))
        if key in self.seen:
            return
        current = sum(
            len(self.result[name]) for name in ("languages", "frameworks", "commands")
        )
        if current >= self.result["limits"]["max_results"]:
            self.result["truncated"] = True
            if "max_results" not in self.result["truncation_reasons"]:
                self.result["truncation_reasons"].append("max_results")
            return
        self.seen.add(key)
        self.result[category].append(row)

    def language(self, name: str, source: str) -> None:
        self.add("languages", {"name": name, "source": source})

    def framework(self, name: str, language: str, source: str, evidence: str) -> None:
        self.add("frameworks", {
            "name": name, "language": language, "source": source,
            "evidence": evidence,
        })

    def command(
        self, kind: str, cwd: str, argv: list[str], source: str,
        evidence: str, platform: str = "any",
    ) -> None:
        self.add("commands", {
            "kind": kind, "cwd": cwd, "argv": argv, "platform": platform,
            "source": source, "evidence": evidence,
        })


def _declared_frameworks(collector, dependencies, mapping, language, source):
    normalized = {str(item).casefold(): str(item) for item in dependencies}
    for dependency, framework in mapping.items():
        if dependency in normalized:
            collector.framework(framework, language, source, normalized[dependency])


def _declared_java_frameworks(collector, artifacts, language, source):
    for artifact in artifacts:
        lowered = str(artifact).casefold()
        if "spring-boot" in lowered or lowered == "org.springframework.boot":
            collector.framework("Spring Boot", language, source, str(artifact))
        elif "junit-jupiter" in lowered:
            collector.framework("JUnit Jupiter", language, source, str(artifact))
        elif "quarkus" in lowered:
            collector.framework("Quarkus", language, source, str(artifact))


def _package_runner(directory: Path) -> str:
    candidates = (
        ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
        ("bun.lock", "bun"), ("bun.lockb", "bun"),
        ("package-lock.json", "npm"),
    )
    for filename, runner in candidates:
        path = directory / filename
        if path.is_file() and not _is_reparse(path):
            return runner
    return "npm"


def _parse_python_toml(data, path, cwd, collector):
    collector.language("Python", path)
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    dependencies = list(project.get("dependencies") or [])
    optional = project.get("optional-dependencies") or {}
    if isinstance(optional, dict):
        for values in optional.values():
            if isinstance(values, list):
                dependencies.extend(values)
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    poetry_deps = poetry.get("dependencies") or {}
    if isinstance(poetry_deps, dict):
        dependencies.extend(poetry_deps)
    names = {_normalize_python_dependency(item) for item in dependencies}
    _declared_frameworks(collector, names, PYTHON_FRAMEWORKS, "Python", path)
    if "pytest" in names or "pytest" in tool:
        collector.command("test", cwd, ["python", "-m", "pytest"], path, "declared pytest configuration/dependency")
    if "build" in names:
        collector.command("build", cwd, ["python", "-m", "build"], path, "declared build dependency")
    scripts = project.get("scripts") or {}
    if isinstance(scripts, dict):
        for name in sorted(scripts, key=str.casefold):
            collector.command("runtime", cwd, [str(name)], path, "declared project.scripts entry point")
    poetry_scripts = poetry.get("scripts") or {}
    if isinstance(poetry_scripts, dict):
        for name in sorted(poetry_scripts, key=str.casefold):
            collector.command("runtime", cwd, ["poetry", "run", str(name)], path, "declared tool.poetry.scripts entry point")


def _parse_node(data, path, directory, cwd, collector):
    collector.language("JavaScript/TypeScript", path)
    dependencies = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    _declared_frameworks(collector, dependencies, NODE_FRAMEWORKS, "JavaScript/TypeScript", path)
    scripts = data.get("scripts") or {}
    if not isinstance(scripts, dict):
        return
    runner = _package_runner(directory)
    for name in sorted(scripts, key=str.casefold):
        lowered = str(name).casefold()
        if lowered == "build" or lowered.startswith("build:") or lowered in {"compile", "bundle"}:
            kind = "build"
        elif lowered == "test" or lowered.startswith("test:"):
            kind = "test"
        elif lowered in {"start", "dev", "serve"} or lowered.startswith(("start:", "dev:", "serve:")):
            kind = "runtime"
        else:
            continue
        argv = [runner, str(name)] if runner == "yarn" else [runner, "run", str(name)]
        collector.command(kind, cwd, argv, path, "declared package.json script %s" % name)


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _parse_dotnet(root, kind, path, cwd, collector):
    language = {
        "dotnet-csharp": "C#", "dotnet-fsharp": "F#", "dotnet-vb": "Visual Basic",
        "msbuild-cpp": "C++",
    }.get(kind, ".NET")
    collector.language(language, path)
    command = "msbuild" if kind == "msbuild-cpp" else "dotnet"
    build_argv = [command, "build", Path(path).name] if command == "dotnet" else [command, Path(path).name]
    collector.command("build", cwd, build_argv, path, "project manifest")
    package_refs = []
    values = {}
    for node in root.iter():
        local = _xml_local(node.tag)
        if local in {"packagereference", "frameworkreference"}:
            value = node.attrib.get("Include") or node.attrib.get("Update")
            if value:
                package_refs.append(value)
        elif local in {"istestproject", "outputtype", "targetframework", "targetframeworks"}:
            values.setdefault(local, (node.text or "").strip())
    _declared_frameworks(collector, package_refs, DOTNET_FRAMEWORKS, language, path)
    is_test = values.get("istestproject", "").casefold() == "true" or any(
        value.casefold() in {"microsoft.net.test.sdk", "nunit", "xunit", "mstest.testframework"}
        for value in package_refs
    )
    if is_test and command == "dotnet":
        collector.command("test", cwd, ["dotnet", "test", Path(path).name], path, "declared test project/package")
    if values.get("outputtype", "").casefold() in {"exe", "winexe"} and command == "dotnet":
        collector.command("runtime", cwd, ["dotnet", "run", "--project", Path(path).name], path, "declared executable OutputType")


def _parse_maven(root, path, cwd, collector):
    collector.language("Java/Kotlin", path)
    artifacts = []
    plugins = []
    for node in root.iter():
        declaration = _xml_local(node.tag)
        if declaration not in {"dependency", "plugin"}:
            continue
        value = next(
            (
                (child.text or "").strip()
                for child in node
                if _xml_local(child.tag) == "artifactid" and (child.text or "").strip()
            ),
            "",
        )
        if value:
            artifacts.append(value)
            if declaration == "plugin":
                plugins.append(value)
    _declared_java_frameworks(collector, artifacts, "Java/Kotlin", path)
    collector.command("build", cwd, ["mvn", "package"], path, "pom.xml")
    collector.command("test", cwd, ["mvn", "test"], path, "pom.xml")
    if any(value.casefold() == "spring-boot-maven-plugin" for value in plugins):
        collector.command("runtime", cwd, ["mvn", "spring-boot:run"], path, "declared spring-boot-maven-plugin")


def _parse_manifest(kind, text, path, absolute, collector):
    cwd = _cwd(path)
    directory = absolute.parent
    if kind == "python-project":
        _parse_python_toml(tomllib.loads(text), path, cwd, collector)
    elif kind == "python-config":
        parser = configparser.ConfigParser()
        parser.read_string(text)
        collector.language("Python", path)
        if parser.has_section("tool:pytest"):
            collector.command("test", cwd, ["python", "-m", "pytest"], path, "declared tool:pytest section")
    elif kind == "python-setup":
        collector.language("Python", path)
    elif kind == "pytest-config":
        parser = configparser.ConfigParser()
        parser.read_string(text)
        collector.language("Python", path)
        collector.command("test", cwd, ["python", "-m", "pytest"], path, "pytest.ini")
    elif kind == "tox-config":
        parser = configparser.ConfigParser()
        parser.read_string(text)
        collector.language("Python", path)
        collector.command("test", cwd, ["python", "-m", "tox"], path, "tox.ini")
    elif kind == "node-package":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("package.json root must be an object")
        _parse_node(data, path, directory, cwd, collector)
    elif kind == "cargo":
        data = tomllib.loads(text)
        collector.language("Rust", path)
        collector.command("build", cwd, ["cargo", "build"], path, "Cargo.toml")
        collector.command("test", cwd, ["cargo", "test"], path, "Cargo.toml")
        rust_src = directory / "src"
        rust_main = rust_src / "main.rs"
        if (
            isinstance(data.get("package"), dict)
            and rust_main.is_file()
            and not _is_reparse(rust_src)
            and not _is_reparse(rust_main)
        ):
            collector.command("runtime", cwd, ["cargo", "run"], path, "package plus src/main.rs")
    elif kind == "go-module":
        if not re.search(r"(?m)^\s*module\s+\S+", text):
            raise ValueError("go.mod has no module directive")
        collector.language("Go", path)
        collector.command("build", cwd, ["go", "build", "./..."], path, "go.mod")
        collector.command("test", cwd, ["go", "test", "./..."], path, "go.mod")
        go_main = directory / "main.go"
        if go_main.is_file() and not _is_reparse(go_main):
            collector.command("runtime", cwd, ["go", "run", "."], path, "go.mod plus main.go")
    elif kind == "cmake":
        collector.language("C/C++", path)
        collector.command("build", cwd, ["cmake", "-S", ".", "-B", "build"], path, "CMakeLists.txt configure")
        collector.command("build", cwd, ["cmake", "--build", "build"], path, "CMakeLists.txt build")
        if re.search(r"(?im)^\s*(?:enable_testing\s*\(|include\s*\(\s*CTest\b)", text):
            collector.command("test", cwd, ["ctest", "--test-dir", "build"], path, "declared CTest enablement")
    elif kind == "meson":
        collector.language("C/C++", path)
        collector.command("build", cwd, ["meson", "setup", "build"], path, "meson.build")
        collector.command("build", cwd, ["meson", "compile", "-C", "build"], path, "meson.build")
        if re.search(r"(?m)^\s*test\s*\(", text):
            collector.command("test", cwd, ["meson", "test", "-C", "build"], path, "declared Meson test()")
    elif kind == "make":
        collector.language("C/C++ or project-defined", path)
        collector.command("build", cwd, ["make"], path, "Makefile")
        if re.search(r"(?m)^test\s*:(?![=])", text):
            collector.command("test", cwd, ["make", "test"], path, "declared test target")
    elif kind in {"dotnet-csharp", "dotnet-fsharp", "dotnet-vb", "msbuild-cpp"}:
        _parse_dotnet(ET.fromstring(text), kind, path, cwd, collector)
    elif kind == "dotnet-solution":
        collector.language(".NET", path)
        collector.command("build", cwd, ["dotnet", "build", Path(path).name], path, "solution manifest")
    elif kind == "maven":
        _parse_maven(ET.fromstring(text), path, cwd, collector)
    elif kind == "gradle":
        collector.language("Java/Kotlin/Groovy", path)
        posix_wrapper = (directory / "gradlew").is_file() and not _is_reparse(directory / "gradlew")
        windows_wrapper = (directory / "gradlew.bat").is_file() and not _is_reparse(directory / "gradlew.bat")
        if posix_wrapper:
            collector.command("build", cwd, ["./gradlew", "build"], path, "Gradle wrapper", "posix")
            collector.command("test", cwd, ["./gradlew", "test"], path, "Gradle wrapper", "posix")
        if windows_wrapper:
            collector.command("build", cwd, ["gradlew.bat", "build"], path, "Gradle wrapper", "windows")
            collector.command("test", cwd, ["gradlew.bat", "test"], path, "Gradle wrapper", "windows")
        if not (posix_wrapper or windows_wrapper):
            collector.command("build", cwd, ["gradle", "build"], path, "Gradle manifest")
            collector.command("test", cwd, ["gradle", "test"], path, "Gradle manifest")
        declared = []
        declared_boot_plugin = False
        uncommented = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        for raw_line in uncommented.splitlines():
            line = raw_line.split("//", 1)[0]
            plugin = re.search(r"\bid\s*(?:\(\s*)?[\"']([^\"']+)[\"']", line)
            if plugin:
                declared.append(plugin.group(1))
                declared_boot_plugin = (
                    declared_boot_plugin
                    or plugin.group(1).casefold() == "org.springframework.boot"
                )
            dependency = re.search(
                r"\b(?:api|implementation|compileOnly|runtimeOnly|testImplementation)\s*"
                r"(?:\(\s*)?[\"']([^\"']+)[\"']",
                line,
            )
            if dependency:
                declared.append(dependency.group(1))
        _declared_java_frameworks(collector, declared, "Java/Kotlin", path)
        if declared_boot_plugin:
            if posix_wrapper:
                collector.command("runtime", cwd, ["./gradlew", "bootRun"], path, "declared Spring Boot plugin", "posix")
            if windows_wrapper:
                collector.command("runtime", cwd, ["gradlew.bat", "bootRun"], path, "declared Spring Boot plugin", "windows")
            if not (posix_wrapper or windows_wrapper):
                collector.command("runtime", cwd, ["gradle", "bootRun"], path, "declared Spring Boot plugin", "any")
    elif kind == "dart-package":
        is_flutter = bool(re.search(r"(?m)^\s{0,4}flutter\s*:", text))
        collector.language("Dart", path)
        if is_flutter:
            collector.framework("Flutter", "Dart", path, "declared flutter key")
            if re.search(r"(?m)^\s+flutter_test\s*:", text):
                collector.command("test", cwd, ["flutter", "test"], path, "declared flutter_test dependency")
            lib_dir = directory / "lib"
            main = lib_dir / "main.dart"
            if main.is_file() and not _is_reparse(lib_dir) and not _is_reparse(main):
                collector.command("runtime", cwd, ["flutter", "run"], path, "pubspec plus lib/main.dart")
        else:
            if re.search(r"(?m)^\s+test\s*:", text):
                collector.command("test", cwd, ["dart", "test"], path, "declared test dependency")
            bin_dir = directory / "bin"
            if bin_dir.is_dir() and not _is_reparse(bin_dir) and any(
                child.is_file() and child.suffix.casefold() == ".dart" and not _is_reparse(child)
                for child in bin_dir.iterdir()
            ):
                collector.command("runtime", cwd, ["dart", "run"], path, "pubspec plus bin Dart entry point")
    elif kind == "composer":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("composer.json root must be an object")
        collector.language("PHP", path)
        dependencies = {}
        for key in ("require", "require-dev"):
            if isinstance(data.get(key), dict):
                dependencies.update(data[key])
        _declared_frameworks(collector, dependencies, PHP_FRAMEWORKS, "PHP", path)
        scripts = data.get("scripts") or {}
        if isinstance(scripts, dict):
            for name in sorted(scripts, key=str.casefold):
                lowered = str(name).casefold()
                kind_name = "test" if lowered == "test" or lowered.startswith("test:") else "runtime" if lowered in {"start", "serve", "dev"} else "build" if lowered == "build" else ""
                if kind_name:
                    collector.command(kind_name, cwd, ["composer", "run-script", str(name)], path, "declared Composer script")
    elif kind in {"ruby-bundle", "rake"}:
        collector.language("Ruby", path)
        gems = re.findall(r"(?m)^\s*gem\s+[\"']([^\"']+)[\"']", text)
        _declared_frameworks(collector, gems, RUBY_FRAMEWORKS, "Ruby", path)
        if kind == "rake":
            collector.command("build", cwd, ["bundle", "exec", "rake"], path, "Rakefile")
            if re.search(r"(?m)^\s*(?:task\s+)?[:\"']?test\b", text):
                collector.command("test", cwd, ["bundle", "exec", "rake", "test"], path, "declared test task")
    elif kind == "swift-package":
        collector.language("Swift", path)
        collector.command("build", cwd, ["swift", "build"], path, "Package.swift")
        collector.command("test", cwd, ["swift", "test"], path, "Package.swift")
        if re.search(r"\.executableTarget\s*\(", text):
            collector.command("runtime", cwd, ["swift", "run"], path, "declared executableTarget")
    elif kind == "dockerfile":
        collector.framework("Docker", "Container", path, "Dockerfile")
        collector.command("build", cwd, ["docker", "build", "."], path, "Dockerfile")
    elif kind == "docker-compose":
        collector.framework("Docker Compose", "Container", path, Path(path).name)
        collector.command("runtime", cwd, ["docker", "compose", "up"], path, "Compose manifest")


def detect_project(
    path: str = ".", *, max_depth: int = DEFAULT_MAX_DEPTH,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_results: int = DEFAULT_MAX_RESULTS,
    extra_roots: str = "",
) -> dict:
    limits = {
        "max_depth": _bounded(max_depth, DEFAULT_MAX_DEPTH, HARD_MAX_DEPTH),
        "max_files": _bounded(max_files, DEFAULT_MAX_FILES, HARD_MAX_FILES),
        "max_total_bytes": _bounded(max_total_bytes, DEFAULT_MAX_TOTAL_BYTES, HARD_MAX_TOTAL_BYTES),
        "max_file_bytes": _bounded(max_file_bytes, DEFAULT_MAX_FILE_BYTES, HARD_MAX_FILE_BYTES),
        "max_results": _bounded(max_results, DEFAULT_MAX_RESULTS, HARD_MAX_RESULTS),
    }
    _reject_symlinked_root(path)
    root = file_ops.resolve_repository_read_path(
        path, allow_workspace_root=True, reject_sensitive=True, extra_roots=extra_roots,
    )
    if not root.exists():
        raise FileNotFoundError("project detection path not found: %s" % root)
    if not (root.is_dir() or root.is_file()):
        raise ValueError("project detection path is not a file or directory: %s" % root)
    result = {
        "root": str(root), "limits": limits, "files": 0, "bytes": 0,
        "manifests": [], "languages": [], "frameworks": [], "commands": [],
        "errors": [], "truncated": False, "truncation_reasons": [],
    }
    collector = _Collector(result)

    def truncate(reason):
        result["truncated"] = True
        if reason not in result["truncation_reasons"]:
            result["truncation_reasons"].append(reason)

    def add_error(relative, error):
        if len(result["errors"]) >= HARD_MAX_ERRORS:
            truncate("max_errors")
            return False
        result["errors"].append({"path": relative, "error": error})
        return True

    for candidate, relative, kind, _ in _iter_manifests(root, limits["max_depth"]):
        if kind == "__DISCOVERY_LIMIT__":
            truncate("max_discovery_entries")
            break
        if kind.startswith("__ERROR__:"):
            if not add_error(relative, kind.split(":", 1)[1]):
                break
            continue
        if result["files"] >= limits["max_files"]:
            truncate("max_files")
            break
        result["files"] += 1
        manifest = {"path": relative, "type": kind, "roles": list(MANIFEST_ROLES.get(kind, ())), "bytes": 0}
        result["manifests"].append(manifest)
        try:
            guarded = file_ops.resolve_repository_read_path(
                str(candidate), allow_workspace_root=False, reject_sensitive=True,
                extra_roots=extra_roots,
            )
            if _is_reparse(candidate):
                raise PermissionError("symlink or junction is not inspected")
            size = guarded.stat().st_size
            if size > limits["max_file_bytes"]:
                if not add_error(relative, "file exceeds max_file_bytes (%d > %d)" % (size, limits["max_file_bytes"])):
                    break
                continue
            remaining = limits["max_total_bytes"] - result["bytes"]
            if size > remaining:
                truncate("max_total_bytes")
                break
            with guarded.open("rb") as handle:
                payload = handle.read(min(remaining, limits["max_file_bytes"]))
                observed_size = os.fstat(handle.fileno()).st_size
            if observed_size > limits["max_file_bytes"]:
                if not add_error(relative, "file grew beyond max_file_bytes while reading"):
                    break
                continue
            if observed_size > remaining:
                truncate("max_total_bytes")
                break
            result["bytes"] += len(payload)
            manifest["bytes"] = len(payload)
            text = payload.decode("utf-8-sig")
            _parse_manifest(kind, text, relative, guarded, collector)
        except UnicodeDecodeError as exc:
            if not add_error(relative, "invalid UTF-8 at byte %d" % exc.start):
                break
        except (configparser.Error, ET.ParseError, json.JSONDecodeError, tomllib.TOMLDecodeError, OSError, PermissionError, ValueError) as exc:
            if not add_error(relative, "malformed %s: %s" % (kind, exc)):
                break
    result["languages"].sort(key=lambda row: (row["name"].casefold(), row["source"]))
    result["frameworks"].sort(key=lambda row: (row["name"].casefold(), row["source"], row["evidence"]))
    result["commands"].sort(key=lambda row: (row["cwd"], row["kind"], row["platform"], row["argv"], row["source"]))
    return result


def format_detection(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
