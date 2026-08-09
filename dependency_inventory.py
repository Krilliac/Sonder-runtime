"""Bounded, read-only dependency manifest inventory.

This module only reads and parses files.  It never imports project code, invokes
package managers, resolves registries, or accesses the network.
"""
from __future__ import annotations

import json
import os
import re
import configparser
import xml.etree.ElementTree as ET
from pathlib import Path

import file_ops

try:
    import tomllib
except ImportError:  # pragma: no cover - supported Python includes tomllib
    tomllib = None


DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_TOTAL_BYTES = 2_000_000
DEFAULT_MAX_RESULTS = 2_000
MAX_DEPTH = 8
MAX_FILES = 200
MAX_TOTAL_BYTES = 4_000_000
MAX_FILE_BYTES = 512_000
MAX_RESULTS = 5_000
MAX_SCAN_ENTRIES = 25_000

SKIP_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".kube",
    "node_modules", "target", "build", "dist", ".dart_tool", ".gradle",
    ".idea", ".vs", ".venv", "venv", "__pycache__", "bin", "obj",
})


def _bounded_int(value, default, hard_max, label):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be an integer" % label)
    if value < 1 or value > hard_max:
        raise ValueError("%s must be between 1 and %d" % (label, hard_max))
    return value


def _item(ecosystem, name, version, kind, scope, evidence, **extra):
    item = {
        "ecosystem": ecosystem,
        "name": str(name).strip(),
        "version": str(version or "").strip(),
        "kind": kind,
        "scope": str(scope or "").strip(),
        "evidence": evidence,
    }
    item.update({key: value for key, value in extra.items() if value not in (None, "")})
    return item


def _python_requirement(value):
    raw = str(value).strip()
    if not raw or raw.startswith(("#", "-")):
        return None
    raw = raw.split(" #", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9_.-]+(?:\[[^]]+\])?)\s*(.*)$", raw)
    if not match:
        raise ValueError("unsupported requirement: %s" % raw)
    return match.group(1), match.group(2).strip()


def _parse_requirements(path, text, evidence):
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        parsed = _python_requirement(line)
        if parsed:
            out.append(_item("python", parsed[0], parsed[1], "declared", "requirements", evidence, line=number))
    return out


def _toml(text):
    if tomllib is None:
        raise ValueError("TOML parsing is unavailable")
    return tomllib.loads(text)


def _parse_pyproject(path, text, evidence):
    data = _toml(text)
    out = []
    project = data.get("project", {})
    for value in project.get("dependencies", []) or []:
        parsed = _python_requirement(value)
        if parsed:
            out.append(_item("python", parsed[0], parsed[1], "declared", "dependencies", evidence))
    for scope, values in (project.get("optional-dependencies", {}) or {}).items():
        for value in values or []:
            parsed = _python_requirement(value)
            if parsed:
                out.append(_item("python", parsed[0], parsed[1], "declared", "optional:%s" % scope, evidence))
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for scope, values in (("dependencies", poetry.get("dependencies", {})), ("dev", poetry.get("dev-dependencies", {}))):
        for name, version in (values or {}).items():
            if name.lower() != "python":
                rendered = version if isinstance(version, str) else json.dumps(version, sort_keys=True, separators=(",", ":"))
                out.append(_item("python", name, rendered, "declared", scope, evidence))
    return out


def _parse_python_lock(path, text, evidence):
    if path.name == "Pipfile.lock":
        data = json.loads(text)
        return [_item("python", name, details.get("version", "") if isinstance(details, dict) else details,
                      "resolved", scope.lstrip("_"), evidence)
                for scope in ("default", "develop") for name, details in (data.get(scope, {}) or {}).items()]
    data = _toml(text)
    packages = data.get("package", []) or []
    return [_item("python", row.get("name", ""), row.get("version", ""), "resolved", "lock", evidence)
            for row in packages if isinstance(row, dict) and row.get("name")]


def _parse_setup_cfg(path, text, evidence):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    out = []
    if parser.has_option("options", "install_requires"):
        for value in parser.get("options", "install_requires").splitlines():
            parsed = _python_requirement(value)
            if parsed:
                out.append(_item("python", parsed[0], parsed[1], "declared", "install_requires", evidence))
    for section in parser.sections():
        if section == "options.extras_require":
            for scope, values in parser.items(section):
                for value in values.splitlines():
                    parsed = _python_requirement(value)
                    if parsed:
                        out.append(_item("python", parsed[0], parsed[1], "declared", "optional:%s" % scope, evidence))
    return out


def _parse_pipfile(path, text, evidence):
    data = _toml(text)
    out = []
    for table, scope in (("packages", "dependencies"), ("dev-packages", "dev")):
        values = data.get(table, {}) or {}
        if not isinstance(values, dict):
            raise ValueError("Pipfile [%s] must be a table" % table)
        for name, version in values.items():
            rendered = version if isinstance(version, str) else json.dumps(version, sort_keys=True, separators=(",", ":"))
            out.append(_item("python", name, rendered, "declared", scope, evidence))
    return out


def _parse_package_json(path, text, evidence):
    data = json.loads(text)
    out = []
    for scope in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, version in (data.get(scope, {}) or {}).items():
            out.append(_item("node", name, version, "declared", scope, evidence))
    return out


def _walk_npm_v1(mapping, evidence, out):
    for name, row in (mapping or {}).items():
        if isinstance(row, dict):
            out.append(_item("node", name, row.get("version", ""), "resolved", "lock", evidence))
            _walk_npm_v1(row.get("dependencies"), evidence, out)


def _parse_package_lock(path, text, evidence):
    data = json.loads(text)
    out = []
    if isinstance(data.get("packages"), dict):
        for location, row in data["packages"].items():
            if not location or not isinstance(row, dict):
                continue
            name = row.get("name") or location.rsplit("node_modules/", 1)[-1]
            out.append(_item("node", name, row.get("version", ""), "resolved", "lock", evidence))
    else:
        _walk_npm_v1(data.get("dependencies"), evidence, out)
    return out


def _parse_yarn_lock(path, text, evidence):
    out, names = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            names = []
            for selector in line[:-1].split(","):
                selector = selector.strip().strip('"\'')
                if selector.startswith("@"):
                    pos = selector.find("@", 1)
                else:
                    pos = selector.find("@")
                if pos > 0:
                    names.append(selector[:pos])
        elif names and re.match(r"^\s+version\s+", line):
            version = line.strip().split(None, 1)[1].strip('"\'')
            out.extend(_item("node", name, version, "resolved", "lock", evidence, line=number) for name in names)
            names = []
    if text.strip() and not out and "__metadata:" not in text:
        raise ValueError("no Yarn lock entries recognized")
    return out


def _parse_pnpm_lock(path, text, evidence):
    """Parse package keys used by pnpm lockfile v5 through v9.

    Full YAML interpretation would add a runtime dependency.  Package keys are
    deliberately parsed conservatively and malformed non-empty files fail
    closed instead of pretending to be an empty inventory.
    """
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"^\s{2,}(['\"]?)(.+?)\1:\s*(?:\{\})?\s*$", line)
        if not match:
            continue
        key = match.group(2).lstrip("/")
        name = version = ""
        if "/" in key and not key.startswith("@"):
            name, version = key.rsplit("/", 1)
        elif "@" in key[1:]:
            name, version = key.rsplit("@", 1)
        if name and version and version[0].isdigit():
            version = version.split("(", 1)[0]
            out.append(_item("node", name, version, "resolved", "lock", evidence, line=number))
    if text.strip() and not out and "packages:" not in text:
        raise ValueError("no pnpm lock package entries recognized")
    return out


def _cargo_dep_table(data, prefix=""):
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if key in {"dependencies", "dev-dependencies", "build-dependencies"}:
            yield key, value
        if key in {"target", "workspace"}:
            yield from _cargo_dep_table(value, prefix + key + ".")


def _parse_cargo_toml(path, text, evidence):
    data = _toml(text)
    out = []
    for scope, values in _cargo_dep_table(data):
        for name, value in values.items():
            version = value if isinstance(value, str) else value.get("version", "") if isinstance(value, dict) else ""
            out.append(_item("rust", name, version, "declared", scope, evidence))
    return out


def _parse_cargo_lock(path, text, evidence):
    data = _toml(text)
    return [_item("rust", row.get("name", ""), row.get("version", ""), "resolved", "lock", evidence)
            for row in data.get("package", []) if isinstance(row, dict) and row.get("name")]


def _parse_go_mod(path, text, evidence):
    out, in_block = [], False
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if line.startswith("require "):
            line = line[8:].strip()
        elif not in_block:
            continue
        indirect = "// indirect" in line
        parts = line.split("//", 1)[0].split()
        if len(parts) >= 2:
            out.append(_item("go", parts[0], parts[1], "declared", "indirect" if indirect else "require", evidence, line=number))
    return out


def _xml_tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _parse_dotnet_xml(path, text, evidence):
    root = ET.fromstring(text)
    out = []
    for node in root.iter():
        tag = _xml_tag(node)
        if tag in {"PackageReference", "PackageVersion", "package"}:
            name = node.attrib.get("Include") or node.attrib.get("Update") or node.attrib.get("id")
            version = node.attrib.get("Version") or node.attrib.get("version")
            if not version:
                version_node = next((child for child in node if _xml_tag(child) == "Version"), None)
                version = version_node.text if version_node is not None else ""
            if name:
                out.append(_item("dotnet", name, version, "declared", tag, evidence))
    return out


def _parse_dotnet_lock(path, text, evidence):
    data = json.loads(text)
    out = []
    for framework, packages in (data.get("dependencies", {}) or {}).items():
        for name, row in (packages or {}).items():
            if isinstance(row, dict):
                out.append(_item("dotnet", name, row.get("resolved", ""), "resolved", framework, evidence))
    return out


def _parse_pom(path, text, evidence):
    root = ET.fromstring(text)
    out = []
    for node in root.iter():
        if _xml_tag(node) != "dependency":
            continue
        values = {_xml_tag(child): (child.text or "").strip() for child in node}
        if values.get("artifactId"):
            name = "%s:%s" % (values.get("groupId", ""), values["artifactId"])
            out.append(_item("maven", name, values.get("version", ""), "declared", values.get("scope", "compile"), evidence))
    return out


_GRADLE_COORD = re.compile(r"(?m)^\s*([A-Za-z][\w.-]*)\s*(?:\(\s*)?[\"']([^\"']+:[^\"']+:[^\"']+)[\"']")


def _parse_gradle(path, text, evidence):
    out = []
    for match in _GRADLE_COORD.finditer(text):
        group, name, version = match.group(2).split(":", 2)
        out.append(_item("gradle", "%s:%s" % (group, name), version, "declared", match.group(1), evidence,
                         line=text.count("\n", 0, match.start()) + 1))
    return out


def _parse_gradle_lock(path, text, evidence):
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith(("#", "empty=")):
            continue
        coordinate = value.split("=", 1)[0]
        parts = coordinate.split(":")
        if len(parts) == 3:
            out.append(_item("gradle", "%s:%s" % tuple(parts[:2]), parts[2], "resolved", "lock", evidence, line=number))
        else:
            raise ValueError("malformed Gradle lock entry on line %d" % number)
    return out


def _yaml_sections(text, sections):
    current = None
    for number, original in enumerate(text.splitlines(), 1):
        clean = original.split(" #", 1)[0].rstrip()
        if not clean or clean.lstrip().startswith("#"):
            continue
        indent = len(clean) - len(clean.lstrip())
        stripped = clean.strip()
        if indent == 0 and stripped.endswith(":"):
            current = stripped[:-1] if stripped[:-1] in sections else None
            continue
        # Only direct section children are package names. Nested keys such as
        # ``sdk: flutter``, ``git:``, and ``path:`` describe the preceding
        # dependency and must never be emitted as packages themselves.
        if current and indent == 2 and re.match(r"^[^:#]+:\s*", stripped):
            name, value = stripped.split(":", 1)
            yield current, name.strip(), value.strip().strip('"\''), number


def _parse_pubspec(path, text, evidence):
    return [_item("dart", name, version, "declared", scope, evidence, line=line)
            for scope, name, version, line in _yaml_sections(
                text, {"dependencies", "dev_dependencies", "dependency_overrides"})]


def _parse_pubspec_lock(path, text, evidence):
    lines = text.splitlines()
    out, package = [], None
    in_packages = False
    for number, original in enumerate(lines, 1):
        stripped = original.strip()
        indent = len(original) - len(original.lstrip())
        if indent == 0:
            in_packages = stripped == "packages:"
            package = None
        elif in_packages and indent == 2 and stripped.endswith(":"):
            package = stripped[:-1]
        elif in_packages and package and indent == 4 and stripped.startswith("version:"):
            version = stripped.split(":", 1)[1].strip().strip('"\'')
            out.append(_item("dart", package, version, "resolved", "lock", evidence, line=number))
    if text.strip() and not out and "packages:" not in text:
        raise ValueError("no pubspec.lock package versions recognized")
    return out


def _parser_for(path):
    name = path.name
    lower = name.lower()
    if lower == "pyproject.toml": return _parse_pyproject
    if lower == "setup.cfg": return _parse_setup_cfg
    if name == "Pipfile": return _parse_pipfile
    if re.match(r"^requirements(?:[-_.].*)?\.txt$", lower): return _parse_requirements
    if name == "Pipfile.lock" or lower in {"poetry.lock", "uv.lock"}: return _parse_python_lock
    if lower == "package.json": return _parse_package_json
    if lower in {"package-lock.json", "npm-shrinkwrap.json"}: return _parse_package_lock
    if lower == "yarn.lock": return _parse_yarn_lock
    if lower == "pnpm-lock.yaml": return _parse_pnpm_lock
    if name == "Cargo.toml": return _parse_cargo_toml
    if name == "Cargo.lock": return _parse_cargo_lock
    if lower == "go.mod": return _parse_go_mod
    if lower.endswith(".csproj") or lower in {"directory.packages.props", "packages.config"}: return _parse_dotnet_xml
    if lower == "packages.lock.json": return _parse_dotnet_lock
    if lower == "pom.xml": return _parse_pom
    if lower in {"build.gradle", "build.gradle.kts"}: return _parse_gradle
    if lower == "gradle.lockfile": return _parse_gradle_lock
    if lower == "pubspec.yaml": return _parse_pubspec
    if lower == "pubspec.lock": return _parse_pubspec_lock
    return None


def _safe_relative(path, root):
    return path.relative_to(root).as_posix()


def dependency_inventory(path=".", *, max_depth=DEFAULT_MAX_DEPTH,
                         max_files=DEFAULT_MAX_FILES,
                         max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
                         max_results=DEFAULT_MAX_RESULTS,
                         extra_roots="", bypass=False):
    """Return a deterministic dependency inventory beneath an authorized root."""
    max_depth = _bounded_int(max_depth, DEFAULT_MAX_DEPTH, MAX_DEPTH, "max_depth")
    max_files = _bounded_int(max_files, DEFAULT_MAX_FILES, MAX_FILES, "max_files")
    max_total_bytes = _bounded_int(max_total_bytes, DEFAULT_MAX_TOTAL_BYTES, MAX_TOTAL_BYTES, "max_total_bytes")
    max_results = _bounded_int(max_results, DEFAULT_MAX_RESULTS, MAX_RESULTS, "max_results")
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        requested = file_ops.workspace_root() / requested
    # Check the caller's path before resolve_path follows a directory link.
    file_ops._require_no_reparse_components(requested.absolute())
    root = file_ops.require_read_access(path, extra_roots=extra_roots, bypass=bypass)
    if not root.is_dir():
        raise ValueError("dependency inventory path must be a directory")
    file_ops._require_no_reparse_components(root)

    candidates, errors, entries = [], [], 0
    stack = [(root, 0)]
    truncation = []
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            errors.append({"path": _safe_relative(directory, root) or ".", "error": "could not enumerate: %s" % exc})
            continue
        for entry in children:
            entries += 1
            if entries > MAX_SCAN_ENTRIES:
                truncation.append("scan_entries")
                stack.clear()
                break
            child = Path(entry.path)
            try:
                is_link = file_ops._is_reparse_point(child)
            except PermissionError as exc:
                errors.append({"path": _safe_relative(child, root), "error": str(exc)})
                continue
            if is_link:
                if _parser_for(child):
                    errors.append({"path": _safe_relative(child, root), "error": "refusing symlink or junction"})
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth and entry.name.casefold() not in SKIP_DIRECTORIES:
                        stack.append((child, depth + 1))
                elif entry.is_file(follow_symlinks=False) and _parser_for(child):
                    if len(candidates) >= max_files:
                        if "files" not in truncation: truncation.append("files")
                    else:
                        candidates.append(child)
            except OSError as exc:
                errors.append({"path": _safe_relative(child, root), "error": "could not inspect: %s" % exc})

    candidates.sort(key=lambda value: _safe_relative(value, root).casefold())
    items, bytes_read, files_read = [], 0, 0
    for candidate in candidates:
        evidence = _safe_relative(candidate, root)
        try:
            guarded = file_ops.require_read_access(str(candidate), extra_roots=extra_roots, bypass=bypass)
            file_ops._require_no_reparse_components(guarded)
            size = guarded.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError("file exceeds %d-byte per-file cap" % MAX_FILE_BYTES)
            if bytes_read + size > max_total_bytes:
                if "bytes" not in truncation: truncation.append("bytes")
                continue
            raw = guarded.read_bytes()
            bytes_read += len(raw)
            files_read += 1
            text = raw.decode("utf-8-sig")
            parsed = _parser_for(guarded)(guarded, text, evidence)
            for item in parsed:
                if not item["name"]:
                    continue
                if len(items) >= max_results:
                    if "results" not in truncation: truncation.append("results")
                    break
                items.append(item)
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError, ET.ParseError) as exc:
            errors.append({"path": evidence, "error": str(exc)})

    items.sort(key=lambda row: (row["ecosystem"], row["name"].casefold(), row["kind"], row["scope"], row["evidence"], row["version"]))
    errors.sort(key=lambda row: (row["path"].casefold(), row["error"]))
    return {
        "root": str(root),
        "files_read": files_read,
        "bytes_read": bytes_read,
        "entries_scanned": min(entries, MAX_SCAN_ENTRIES),
        "items": items,
        "errors": errors,
        "truncated": bool(truncation),
        "truncation_reasons": truncation,
        "limits": {"max_depth": max_depth, "max_files": max_files,
                   "max_total_bytes": max_total_bytes, "max_file_bytes": MAX_FILE_BYTES,
                   "max_results": max_results, "max_scan_entries": MAX_SCAN_ENTRIES},
    }
