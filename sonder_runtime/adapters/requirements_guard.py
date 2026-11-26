"""Confine a pip requirements file and everything it includes.

``pip install -r requirements.txt`` and ``pip-audit -r requirements.txt`` follow
``-r``/``-c`` includes, install editables, fetch URLs and honour index overrides
named inside the file, so checking only the top-level file leaves a nested include
(``-r ../../elsewhere.txt``) or a VCS/URL requirement free to read or run anything.
:func:`requirements_content_error` parses the file and every include recursively
and refuses:

- ``${VAR}`` environment expansion anywhere in a line (pip expands it before it
  resolves includes) and an include value that starts with a URL scheme
  (``file:/x``, ``https:...``);
- an include that is a URL, escapes the root, is a link, or is missing/too large;
- an include cycle or a tree deeper/larger than the caps below;
- editables (``-e``/``--editable``), direct references (``name @ url``, bare URLs,
  ``git+...``, local paths and archives, including bare names such as ``evil-1.0.tar.gz``);
- index and fetch overrides (``-i``, ``--index-url``, ``--extra-index-url``,
  ``-f``/``--find-links``, ``--trusted-host``) and every option outside a small
  allow-list.

Every file is read through ``confined_read`` (no-follow open, handle-path check, protected-read
predicate) and error text never contains a line from the file.
Stateless; any thread.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Callable

from .confined_read import ConfinedReadRefused, read_confined_bytes

MAX_DEPTH = 8
MAX_FILES = 32
MAX_FILE_BYTES = 1 << 20

_INCLUDE_OPTIONS = frozenset({"-r", "--requirement", "-c", "--constraint"})
_ALLOWED_OPTIONS = frozenset({
    "--hash", "--require-hashes", "--only-binary", "--no-binary", "--prefer-binary", "--pre",
})
# pip (req/constructors.py ``is_archive_file``) turns an existing file with one of these
# extensions into a ``file:`` URL even when the name has no separator, then builds it.
_ARCHIVE_EXTENSIONS = (
    ".zip", ".whl", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz",
    ".tar.xz", ".txz", ".tlz", ".tar.lz", ".tar.lzma",
)
_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,\s-]*\])?"
    r"\s*(?:(?:===|==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9.*+!_-]+\s*,?\s*)*"
)


# pip treats ``scheme:`` (two or more letters) as a URL; ``file:/x`` has no ``//`` yet is not joined
# to the parent folder. A single letter is a Windows drive and is left to the confined read.
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]+:")


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Logical lines as ``(first physical line number, text)``."""
    lines: list[tuple[int, str]] = []
    pending = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), 1):
        piece = raw.rstrip()
        if not pending:
            start = number
        if piece.endswith("\\"):
            pending += piece[:-1] + " "
            continue
        line = (pending + piece).strip()
        pending = ""
        stripped = re.sub(r"(^|\s)#.*$", "", line).strip()
        if stripped:
            lines.append((start, stripped))
    if pending.strip():
        tail = re.sub(r"(^|\s)#.*$", "", pending).strip()
        if tail:
            lines.append((start, tail))
    return lines


class _Walk:
    def __init__(self, root: Path, is_protected: Callable[[Path], bool]) -> None:
        self.root = root
        self.is_protected = is_protected
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
                self.root, path, max_bytes=MAX_FILE_BYTES, is_protected=self.is_protected)
        except FileNotFoundError:
            return "requirements include %s does not exist" % path.name
        except ConfinedReadRefused as exc:
            return "requirements include %s was refused: %s" % (path.name, exc)
        except OSError:
            return "requirements include %s could not be read safely" % path.name
        for number, line in _logical_lines(data.decode("utf-8", errors="replace")):
            error = self.line(line, path, depth)
            if error:
                # Never echo the line: an include may be a credential file read as requirements.
                return "%s line %d: %s" % (path.name, number, error)
        return None

    def include(self, value: str, parent: Path, depth: int) -> str | None:
        if (not value or "://" in value or "\0" in value or "${" in value
                or _URL_SCHEME.match(value) or value.startswith(("\\\\", "//"))):
            return "an include is a URL or network path"
        target = Path(os.path.normpath(str(parent.parent / value)))
        return self.file(target, depth + 1)

    def line(self, line: str, parent: Path, depth: int) -> str | None:
        if "${" in line:
            # pip expands ${VAR} from the environment before it resolves includes.
            return "uses ${VAR} expansion"
        try:
            # Non-POSIX splitting keeps Windows backslashes; quotes are stripped by hand.
            tokens = [
                token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'" else token
                for token in shlex.split(line, posix=False)
            ]
        except ValueError:
            return "is not parseable"
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
                return "editable requirements are not allowed"
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
            return "an option is not allowed (index overrides, links and flags)"
        if spec:
            return self.requirement(" ".join(spec))
        return None

    @staticmethod
    def requirement(text: str) -> str | None:
        main = text.split(";", 1)[0].strip()
        if (
            "@" in main or "://" in main or "/" in main or "\\" in main
            or not _NAME.fullmatch(main)
            or main.lower().endswith(_ARCHIVE_EXTENSIONS)
        ):
            return "a requirement is a link, path or unsupported form (including local archives); only pinned names are allowed"
        return None


def requirements_content_error(
    root: Path | str,
    name: str = "requirements.txt",
    *,
    is_protected: Callable[[Path], bool],
) -> str | None:
    """Why ``root/name`` (or an include of it) must not be handed to pip, or None.

    ``is_protected`` is the file tools' protected-read predicate, applied to the
    opened handle path of the file and of every include. Error text names a file
    and line number, never the line.

    A missing top-level file is not an error here; callers decide what a missing
    file means.
    """
    root_path = Path(root)
    top = root_path / name
    if not os.path.lexists(top):
        return None
    return _Walk(root_path, is_protected).file(top, 0)
