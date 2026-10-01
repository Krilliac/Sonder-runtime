"""Bounded, non-executing evidence checks for files changed by an agent.

This adapter deliberately observes tool receipts instead of invoking tools.  A
mutation is only considered statically evidenced after a successful read-back
of the same path, and a read-back is invalidated by every later mutation.
"""
from __future__ import annotations

import ast
import hashlib
import html.parser
import json
import os
import re
from pathlib import Path
from xml.etree import ElementTree

import tomllib

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MUTATION_TOOLS = frozenset({"file_write", "file_edit", "text_patch"})
_READ_TOOLS = frozenset({"file_read", "file_read_range"})
_VOID_HTML_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_HTTP_URL = re.compile(r"(?:https?://|^\s*//)", re.IGNORECASE)
_PATH_KEYS = frozenset({"path", "file_path", "filepath", "filename", "target", "target_path"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                raise OSError("artifact exceeds static-check byte limit")
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path):
    """Return a no-follow identity so replacement/symlink changes fail closed."""
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _paths_from(value):
    """Yield path-like values from the small, JSON-shaped tool arguments."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _PATH_KEYS:
                if isinstance(child, str):
                    yield child
                elif isinstance(child, (list, tuple)):
                    yield from (item for item in child if isinstance(item, str))
            elif key.casefold() in {"files", "changes", "targets"}:
                yield from _paths_from(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _paths_from(child)


class _HTMLStructure(html.parser.HTMLParser):
    def __init__(self, *, require_self_contained: bool):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.require_self_contained = require_self_contained

    def _check_attrs(self, attrs):
        if not self.require_self_contained:
            return
        for name, value in attrs:
            if value and name.casefold() in {"src", "href", "action", "poster"} and _HTTP_URL.search(value):
                self.errors.append(f"external URL in {name}")
            if value and name.casefold() == "style" and _HTTP_URL.search(value):
                self.errors.append("external URL in style attribute")

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        self._check_attrs(attrs)
        if tag not in _VOID_HTML_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self._check_attrs(attrs)

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in _VOID_HTML_TAGS:
            self.errors.append(f"closing void tag {tag}")
        elif not self.stack or self.stack[-1] != tag:
            self.errors.append(f"mismatched closing tag {tag}")
        else:
            self.stack.pop()

    def handle_data(self, data):
        # Restrict this to CSS-like references.  Ordinary prose containing a
        # URL is not a dependency and should remain valid.
        if (self.require_self_contained and _HTTP_URL.search(data)
                and re.search(r"(?:@import|url\s*\()\s*['\"]?https?://", data, re.IGNORECASE)):
            self.errors.append("external URL in CSS")

    def handle_comment(self, data):
        if self.require_self_contained and re.search(r"(?:@import|url\s*\()\s*['\"]?https?://", data, re.IGNORECASE):
            self.errors.append("external URL in CSS comment")


def _validate_html(text: str, *, self_contained: bool):
    parser = _HTMLStructure(require_self_contained=self_contained)
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError) as exc:
        return f"HTML parse failed: {exc}"
    if parser.stack:
        return "unclosed HTML tags: " + ", ".join(parser.stack)
    if parser.errors:
        return parser.errors[0]
    if self_contained and re.search(r"(?:@import|url\s*\()\s*['\"]?https?://", text, re.IGNORECASE):
        return "external URL in CSS"
    return None


def _validate(path: Path, objective: str):
    suffix = path.suffix.casefold()
    if suffix not in {".py", ".json", ".toml", ".html", ".htm", ".svg", ".xml"}:
        return None, True, f"no non-executing static check for {suffix or 'extensionless artifact'}", None
    try:
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            return None, True, f"artifact exceeds {MAX_ARTIFACT_BYTES} byte static-check limit", None
        with path.open("rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            return False, False, "artifact grew past static-check limit", None
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, False, f"cannot read artifact for static check: {exc}", None
    try:
        if suffix == ".py":
            tree = ast.parse(text, filename=str(path))
            compile(tree, str(path), "exec")
            checker = "ast_parse_compile"
        elif suffix == ".json":
            json.loads(text)
            checker = "json_loads"
        elif suffix == ".toml":
            tomllib.loads(text)
            checker = "tomllib_loads"
        elif suffix in {".html", ".htm"}:
            error = _validate_html(text, self_contained="self-contained" in objective.casefold())
            if error:
                return False, False, error, "html_parser"
            checker = "html_parser"
        elif suffix in {".xml", ".svg"}:
            ElementTree.fromstring(text)
            if "self-contained" in objective.casefold():
                root = ElementTree.fromstring(text)
                for element in root.iter():
                    for name, value in element.attrib.items():
                        if name.rsplit("}", 1)[-1].casefold() in {"src", "href"} and _HTTP_URL.search(value):
                            return False, False, "external URL in XML/SVG attribute", "xml_elementtree"
            checker = "xml_elementtree"
        else:
            return None, True, f"no non-executing static check for {suffix or 'extensionless artifact'}", None
    except (SyntaxError, ValueError, TypeError, json.JSONDecodeError, tomllib.TOMLDecodeError, ElementTree.ParseError) as exc:
        return False, False, f"static check failed: {exc}", {
            ".py": "ast_parse_compile", ".json": "json_loads", ".toml": "tomllib_loads",
            ".html": "html_parser", ".htm": "html_parser", ".xml": "xml_elementtree",
            ".svg": "xml_elementtree",
        }.get(suffix)
    return True, False, None, checker


class StaticArtifactEvidence:
    """Track mutation/read-back receipts and statically validate their files."""

    def __init__(self, project_root, objective: str = ""):
        self.project_root = Path(project_root).expanduser().resolve()
        self.objective = objective or ""
        self._generation = 0
        self._mutations: dict[Path, dict] = {}
        self._readbacks: dict[Path, dict] = {}
        self._unknown_mutation = False
        self._failed_mutation = False

    def _safe_path(self, raw):
        if not isinstance(raw, str) or not raw.strip():
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        try:
            candidate = candidate.absolute()
            candidate.relative_to(self.project_root)
            # Do not accept a final or intermediate symlink.  Resolving first
            # would make an otherwise-contained link indistinguishable from
            # the target and could let it change between receipts.
            relative_parts = candidate.relative_to(self.project_root).parts
            current = self.project_root
            for part in relative_parts:
                current /= part
                if current.is_symlink() or getattr(os.path, "isjunction", lambda _path: False)(current):
                    return None
            resolved = candidate.resolve()
            resolved.relative_to(self.project_root)
        except (OSError, ValueError):
            return None
        return resolved

    def observe(self, tool, args=None, output=None, success=False, mutation=False):
        """Record one tool receipt; returns ``None`` for easy event piping."""
        candidates = [self._safe_path(raw) for raw in _paths_from(args or {})]
        paths = [path for path in candidates if path is not None]
        is_mutation = mutation or tool in _MUTATION_TOOLS
        if is_mutation:
            self._generation += 1
            if not success:
                self._failed_mutation = True
            if not paths or any(path is None for path in candidates):
                self._unknown_mutation = True
            for path in paths:
                self._mutations[path] = {
                    "generation": self._generation,
                    "tool": tool,
                    "success": bool(success),
                }
                self._readbacks.pop(path, None)
        elif success and tool in _READ_TOOLS:
            for path in paths:
                mutation = self._mutations.get(path)
                if mutation is None or not path.is_file():
                    continue
                try:
                    if path.stat().st_size > MAX_ARTIFACT_BYTES:
                        continue
                    self._readbacks[path] = {
                        "generation": mutation["generation"],
                        "sha256": _sha256(path),
                        "identity": _identity(path),
                        "tool": tool,
                    }
                except OSError:
                    continue

    def assess(self):
        evidence = []
        if not self._mutations and not self._unknown_mutation:
            return {"attempted": False, "passed": False, "unsupported": False, "deferable": False, "evidence": evidence, "error": "no mutation observed"}
        overall = True
        unsupported = False
        invalid_or_missing = self._unknown_mutation or self._failed_mutation
        if self._failed_mutation:
            evidence.append({"path": "<failed mutation>", "status": "failed", "error": "mutation receipt reported failure"})
        if self._unknown_mutation:
            evidence.append({"path": "<unknown mutation path>", "status": "missing_readback", "error": "mutation path was not present in the receipt"})
        for path, mutation in self._mutations.items():
            row = {"path": str(path), "mutation_tool": mutation["tool"]}
            if not mutation["success"]:
                row.update({"status": "failed", "error": "mutation receipt reported failure"})
                overall = False
                evidence.append(row)
                continue
            readback = self._readbacks.get(path)
            if not readback or readback["generation"] != mutation["generation"]:
                row["status"] = "missing_readback"
                overall = False
                invalid_or_missing = True
                evidence.append(row)
                continue
            row.update({"readback_tool": readback["tool"], "sha256": readback["sha256"]})
            try:
                if self._safe_path(str(path)) != path:
                    raise OSError("artifact path changed or became a link after read-back")
                if _identity(path) != readback["identity"]:
                    row["status"] = "changed_after_readback"
                    row["error"] = "artifact identity changed after read-back"
                    overall = False
                    invalid_or_missing = True
                    evidence.append(row)
                    continue
                if _sha256(path) != readback["sha256"]:
                    row["status"] = "changed_after_readback"
                    row["error"] = "artifact changed after read-back"
                    overall = False
                    invalid_or_missing = True
                    evidence.append(row)
                    continue
            except OSError as exc:
                row["status"] = "failed"
                row["error"] = f"artifact disappeared after read-back: {exc}"
                overall = False
                invalid_or_missing = True
                evidence.append(row)
                continue
            passed, is_unsupported, error, checker = _validate(path, self.objective)
            try:
                if self._safe_path(str(path)) != path or _identity(path) != readback["identity"] or _sha256(path) != readback["sha256"]:
                    passed, is_unsupported, error = False, False, "artifact changed during static validation"
            except OSError as exc:
                passed, is_unsupported, error = False, False, str(exc)
            row["status"] = "passed" if passed else "unsupported" if is_unsupported else "failed"
            if checker:
                row["checker"] = checker
            if error:
                row["error"] = error
            evidence.append(row)
            unsupported |= is_unsupported
            if passed is not True:
                overall = False
                invalid_or_missing |= not is_unsupported
        return {"attempted": True, "passed": overall and not unsupported and not invalid_or_missing, "unsupported": unsupported, "deferable": unsupported and not invalid_or_missing, "evidence": evidence, "error": next((item.get("error") for item in evidence if item.get("error")), None)}


__all__ = ["MAX_ARTIFACT_BYTES", "StaticArtifactEvidence"]
