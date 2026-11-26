"""Confine a pip requirements file and everything it includes.

``pip install -r requirements.txt`` and ``pip-audit -r requirements.txt`` follow
``-r``/``-c`` includes, install editables, fetch URLs and honour index overrides
named inside the file, so checking only the top-level file leaves a nested include
(``-r ../../elsewhere.txt``) or a VCS/URL requirement free to read or run anything.
:func:`requirements_content_error` parses the file and every include recursively
and refuses:

- an include that is a URL, escapes the root, is a link, or is missing/too large;
- an include cycle or a tree deeper/larger than the caps below;
- editables (``-e``/``--editable``), direct references (``name @ url``, bare URLs,
  ``git+...``, local paths and archives);
- index and fetch overrides (``-i``, ``--index-url``, ``--extra-index-url``,
  ``-f``/``--find-links``, ``--trusted-host``) and every option outside a small
  allow-list.

Every file is read through ``confined_read`` (no-follow open, handle-path check).
Stateless; any thread.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from .confined_read import ConfinedReadRefused, read_confined_bytes

MAX_DEPTH = 8
MAX_FILES = 32
MAX_FILE_BYTES = 1 << 20

_INCLUDE_OPTIONS = frozenset({"-r", "--requirement", "-c", "--constraint"})
_ALLOWED_OPTIONS = frozenset({
    "--hash", "--require-hashes", "--only-binary", "--no-binary", "--prefer-binary", "--pre",
})
_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,\s-]*\])?"
    r"\s*(?:(?:===|==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9.*+!_-]+\s*,?\s*)*"
)


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        piece = raw.rstrip()
        if piece.endswith("\\"):
            pending += piece[:-1] + " "
            continue
        line = (pending + piece).strip()
        pending = ""
        stripped = re.sub(r"(^|\s)#.*$", "", line).strip()
        if stripped:
            lines.append(stripped)
    if pending.strip():
        lines.append(re.sub(r"(^|\s)#.*$", "", pending).strip())
    return [line for line in lines if line]


def _no_protection(_path: Path) -> bool:
    return False


class _Walk:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.seen: set[str] = set()

    def file(self, path: Path, depth: int) -> str | None:
        if depth > MAX_DEPTH:
            return "requirements includes nest deeper than %d levels" % MAX_DEPTH
        key = os.path.normcase(str(path))
        if key in self.seen:
            return None
        if len(self.seen) >= MAX_FILES:
            return "requirements include more than %d files" % MAX_FILES
        self.seen.add(key)
        try:
            data = read_confined_bytes(
                self.root, path, max_bytes=MAX_FILE_BYTES, is_protected=_no_protection)
        except FileNotFoundError:
            return "requirements include %s does not exist" % path.name
        except ConfinedReadRefused as exc:
            return "requirements include %s was refused: %s" % (path.name, exc)
        except OSError:
            return "requirements include %s could not be read safely" % path.name
        for line in _logical_lines(data.decode("utf-8", errors="replace")):
            error = self.line(line, path, depth)
            if error:
                return error
        return None

    def include(self, value: str, parent: Path, depth: int) -> str | None:
        if not value or "://" in value or "\0" in value or value.startswith(("\\\\", "//")):
            return "requirements include %r is a URL or network path" % value[:80]
        target = Path(os.path.normpath(str(parent.parent / value)))
        return self.file(target, depth + 1)

    def line(self, line: str, parent: Path, depth: int) -> str | None:
        try:
            # Non-POSIX splitting keeps Windows backslashes; quotes are stripped by hand.
            tokens = [
                token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'" else token
                for token in shlex.split(line, posix=False)
            ]
        except ValueError:
            return "requirements line is not parseable: %r" % line[:80]
        index = 0
        spec: list[str] = []
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("-"):
                spec.append(token)
                index += 1
                continue
            name, equals, inline = token.partition("=")
            if name.startswith("-") and not name.startswith("--") and len(name) > 2:
                name, inline, equals = name[:2], name[2:], "="
            if name in ("-e", "--editable"):
                return "editable requirements are not allowed: %r" % line[:80]
            if name in _INCLUDE_OPTIONS:
                if equals:
                    value = inline
                else:
                    index += 1
                    value = tokens[index] if index < len(tokens) else ""
                error = self.include(value, parent, depth)
                if error:
                    return error
                index += 1
                continue
            if name in _ALLOWED_OPTIONS:
                if not equals and name in ("--only-binary", "--no-binary"):
                    index += 1
                index += 1
                continue
            return "requirements option %s is not allowed (index overrides, links and flags)" % name
        if spec:
            return self.requirement(" ".join(spec))
        return None

    @staticmethod
    def requirement(text: str) -> str | None:
        main = text.split(";", 1)[0].strip()
        if "@" in main or "://" in main or "/" in main or "\\" in main or not _NAME.fullmatch(main):
            return "requirement %r is a link, path or unsupported form; only pinned names are allowed" % main[:80]
        return None


def requirements_content_error(root: Path | str, name: str = "requirements.txt") -> str | None:
    """Why ``root/name`` (or an include of it) must not be handed to pip, or None.

    A missing top-level file is not an error here; callers decide what a missing
    file means.
    """
    root_path = Path(root)
    top = root_path / name
    if not os.path.lexists(top):
        return None
    return _Walk(root_path).file(top, 0)
