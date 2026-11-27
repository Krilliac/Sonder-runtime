"""Turn a project's pip requirements tree into a flat file Sonder can hand to pip.

``pip install -r requirements.txt`` and ``pip-audit -r requirements.txt`` follow
``-r``/``-c`` includes, install editables, local archives and URLs, honour index
overrides named inside the file, and reopen every file themselves (so a file
checked first can be swapped before pip reads it). Filtering the text and then
giving pip the repository's pathname left both a parser mismatch
(``evil.zip[x]`` maps to the committed archive ``evil.zip``) and that race open.

:func:`sanitized_requirements` therefore reads the file and every include
through ``confined_read`` (no-follow open, handle-path check, protected-read
predicate), parses every requirement with the caller's strict PEP 508 parser
(``packaging.requirements.Requirement``, injected because the adapters layer
may not import a third-party package) and keeps only what it validated,
re-emitted in canonical form. The caller writes the result with
:meth:`SanitizedRequirements.write` to a folder outside the root and gives pip
THAT file. It refuses:

- a line that does not parse, ``${VAR}`` expansion anywhere (pip expands it),
  a direct reference (``name @ url``), and a name that ends in an archive,
  wheel or sdist suffix or holds a path separator (after extras and markers
  are stripped);
- editables (``-e``/``--editable``), index and fetch overrides (``-i``,
  ``--index-url``, ``--extra-index-url``, ``-f``/``--find-links``,
  ``--trusted-host``) and every option outside a small allow-list
  (``--require-hashes``, ``--pre``, ``--prefer-binary``, ``--only-binary`` and a
  per-requirement ``--hash``, each re-emitted only after its value validates);
- an include that is a URL, escapes the root, is a link, is protected, is
  missing or too large, and a tree deeper or larger than the caps below.

Error text names a file and a line number, never the line: an include may be a
credential file read as requirements.

Stateless functions; :class:`SanitizedRequirements` is an immutable value. Any
thread.
"""
from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .confined_read import ConfinedReadRefused, read_confined_bytes


class SpecifierLike(Protocol):
    """One version clause (``packaging.specifiers.Specifier``)."""

    @property
    def operator(self) -> str: ...


class RequirementLike(Protocol):
    """A parsed PEP 508 requirement (``packaging.requirements.Requirement``)."""

    @property
    def name(self) -> str: ...

    @property
    def url(self) -> str | None: ...

    @property
    def specifier(self) -> Iterable[SpecifierLike]: ...


# Parses one requirement string; raises ValueError (``InvalidRequirement``) when it is not PEP 508.
RequirementParser = Callable[[str], RequirementLike]

MAX_DEPTH = 8
MAX_FILES = 32
MAX_FILE_BYTES = 1 << 20
MAX_REQUIREMENTS = 10000
REQUIREMENTS_NAME = "requirements.txt"
CONSTRAINTS_NAME = "constraints.txt"

_INCLUDE_OPTIONS = {"-r": False, "--requirement": False, "-c": True, "--constraint": True}
_FLAG_OPTIONS = frozenset({"--require-hashes", "--pre", "--prefer-binary"})
# pip (req/constructors.py ``is_archive_file``) turns an existing file with one of these
# extensions into a ``file:`` URL even when the name has no separator, then builds it.
_ARCHIVE_SUFFIXES = (
    ".zip", ".whl", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz",
    ".tar.xz", ".txz", ".tlz", ".tar.lz", ".tar.lzma",
)
_HASH = re.compile(r"sha256:[0-9a-fA-F]{64}|sha384:[0-9a-fA-F]{96}|sha512:[0-9a-fA-F]{128}")
_ONLY_BINARY = re.compile(r":all:|:none:|[A-Za-z0-9][A-Za-z0-9._-]*(?:,[A-Za-z0-9][A-Za-z0-9._-]*)*")
# pip treats ``scheme:`` (two or more letters) as a URL; ``file:/x`` has no ``//`` yet is not joined
# to the parent folder. A single letter is a Windows drive and is left to the confined read.
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]+:")
# What pip would read differently in the canonical text: a comment, an option, an escape, a URL.
_UNSAFE_CANONICAL = re.compile(r"#|\\|\$\{|://|(?:^|\s)-")

_REQUIREMENT_REFUSED = (
    "a requirement is a link, path or unsupported form (including local archives); only PEP 508 "
    "names with versions, extras and markers are allowed")
_OPTION_REFUSED = "an option is not allowed (index overrides, links and flags)"


@dataclass(frozen=True)
class SanitizedRequirements:
    """Validated requirement lines, constraint lines and global options, in file order."""

    requirements: tuple[str, ...]
    constraints: tuple[str, ...]
    options: tuple[str, ...]

    def write(self, folder: Path) -> Path:
        """Write ``requirements.txt`` (and ``constraints.txt`` when needed) into ``folder``; return the former.

        Both files are created exclusively: ``folder`` must be a fresh directory
        the caller owns. The constraints file is named relative to the
        requirements file, which pip resolves against the requirements file's folder.
        """
        lines = list(self.options)
        if self.constraints:
            with open(folder / CONSTRAINTS_NAME, "x", encoding="utf-8", newline="\n") as handle:
                handle.write("".join(line + "\n" for line in self.constraints))
            lines.append("-c " + CONSTRAINTS_NAME)
        lines.extend(self.requirements)
        target = folder / REQUIREMENTS_NAME
        with open(target, "x", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(line + "\n" for line in lines))
        return target


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Logical lines as ``(first physical line number, text)`` with comments removed."""
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


def _break_args_options(line: str) -> tuple[str, str]:
    """pip's split (req_file.py ``break_args_options``): the requirement, then options from the first ``-``."""
    tokens = line.split(" ")
    for index, token in enumerate(tokens):
        if token.startswith("-"):
            return " ".join(tokens[:index]).strip(), " ".join(tokens[index:])
    return line.strip(), ""


def _option_tokens(options: str) -> list[str]:
    # Non-POSIX splitting keeps Windows backslashes in include paths; quotes are stripped by hand.
    return [
        token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'" else token
        for token in shlex.split(options, posix=False)
    ]


def _split_option(token: str) -> tuple[str, bool, str]:
    """``(name, has inline value, inline value)`` for ``--name=value``, ``-rvalue`` or ``--name``."""
    name, equals, inline = token.partition("=")
    if name.startswith("-") and not name.startswith("--") and len(name) > 2:
        return name[:2], True, name[2:] + equals + inline
    return name, bool(equals), inline


def _canonical_requirement(spec: str, parse: RequirementParser) -> str | None:
    """The canonical PEP 508 text of ``spec`` when it is a plain named requirement, else None."""
    try:
        requirement = parse(spec)
    except ValueError:
        return None
    name = requirement.name
    if (
        requirement.url
        or name.lower().endswith(_ARCHIVE_SUFFIXES)
        or "/" in name or "\\" in name
        or any(item.operator == "===" for item in requirement.specifier)
    ):
        return None
    canonical = str(requirement)
    if _UNSAFE_CANONICAL.search(canonical):
        return None
    # ``foo==1.0+evil.zip``: pip reads a requirement that ends in an archive suffix as a path.
    if canonical.partition(";")[0].strip().lower().endswith(_ARCHIVE_SUFFIXES):
        return None
    try:
        if str(parse(canonical)) != canonical:
            return None
    except ValueError:
        return None
    return canonical


class _Walk:
    """One traversal of a requirements tree. Owned by one call; not shared."""

    def __init__(self, root: Path, is_protected: Callable[[Path], bool], parse: RequirementParser) -> None:
        self.root = root
        self.is_protected = is_protected
        self.parse = parse
        self.seen: set[tuple[str, bool]] = set()
        self.requirements: list[str] = []
        self.constraints: list[str] = []
        self.options: list[str] = []

    def result(self) -> SanitizedRequirements:
        return SanitizedRequirements(tuple(self.requirements), tuple(self.constraints), tuple(self.options))

    def file(self, path: Path, depth: int, constraint: bool) -> str | None:
        if depth > MAX_DEPTH:
            return "requirements includes nest deeper than %d levels" % MAX_DEPTH
        key = (os.path.normcase(str(path)), constraint)
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
        text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
        for number, line in _logical_lines(text):
            error = self.line(line, path, depth, constraint)
            if error:
                # Never echo the line: an include may be a credential file read as requirements.
                return "%s line %d: %s" % (path.name, number, error)
        return None

    def include(self, value: str, parent: Path, depth: int, constraint: bool) -> str | None:
        if (not value or "://" in value or "\0" in value or "${" in value
                or _URL_SCHEME.match(value) or value.startswith(("\\\\", "//"))):
            return "an include is a URL or network path"
        target = Path(os.path.normpath(str(parent.parent / value)))
        return self.file(target, depth + 1, constraint)

    def line(self, line: str, parent: Path, depth: int, constraint: bool) -> str | None:
        if "${" in line:
            # pip expands ${VAR} from the environment before it parses the line.
            return "uses ${VAR} expansion"
        spec, options = _break_args_options(line)
        try:
            tokens = _option_tokens(options)
        except ValueError:
            return "is not parseable"
        if spec:
            return self.requirement(spec, tokens, constraint)
        return self.option_line(tokens, parent, depth)

    def option_line(self, tokens: list[str], parent: Path, depth: int) -> str | None:
        index = 0
        while index < len(tokens):
            name, has_inline, inline = _split_option(tokens[index])
            if name in ("-e", "--editable"):
                return "editable requirements are not allowed"
            if name in _INCLUDE_OPTIONS or name == "--only-binary":
                if has_inline:
                    value = inline
                else:
                    index += 1
                    value = tokens[index] if index < len(tokens) else ""
                if name == "--only-binary":
                    if not _ONLY_BINARY.fullmatch(value):
                        return _OPTION_REFUSED
                    self._option("--only-binary=" + value)
                else:
                    error = self.include(value, parent, depth, _INCLUDE_OPTIONS[name])
                    if error:
                        return error
            elif name in _FLAG_OPTIONS and not has_inline:
                self._option(name)
            else:
                return _OPTION_REFUSED
            index += 1
        return None

    def requirement(self, spec: str, tokens: list[str], constraint: bool) -> str | None:
        hashes: list[str] = []
        index = 0
        while index < len(tokens):
            name, has_inline, inline = _split_option(tokens[index])
            if name in ("-e", "--editable"):
                return "editable requirements are not allowed"
            if name != "--hash":
                return _OPTION_REFUSED
            if has_inline:
                value = inline
            else:
                index += 1
                value = tokens[index] if index < len(tokens) else ""
            if not _HASH.fullmatch(value):
                return _OPTION_REFUSED
            hashes.append(value.lower())
            index += 1
        canonical = _canonical_requirement(spec, self.parse)
        if canonical is None:
            return _REQUIREMENT_REFUSED
        if len(self.requirements) + len(self.constraints) >= MAX_REQUIREMENTS:
            return "requirements name more than %d entries" % MAX_REQUIREMENTS
        entry = canonical + "".join(" --hash=" + value for value in hashes)
        (self.constraints if constraint else self.requirements).append(entry)
        return None

    def _option(self, option: str) -> None:
        if option not in self.options:
            self.options.append(option)


def sanitized_requirements(
    root: Path | str,
    name: str = REQUIREMENTS_NAME,
    *,
    is_protected: Callable[[Path], bool],
    parse: RequirementParser,
) -> tuple[SanitizedRequirements | None, str | None]:
    """``(sanitized tree, None)`` for ``root/name`` and its includes, or ``(None, why it is refused)``.

    ``is_protected`` is the file tools' protected-read predicate, applied to the
    opened handle path of the file and of every include. ``parse`` is a strict
    PEP 508 parser (``packaging.requirements.Requirement``) that raises
    ``ValueError`` on anything else. Error text names a file and line number,
    never the line. A missing top-level file is an error here.
    """
    walk = _Walk(Path(root), is_protected, parse)
    error = walk.file(Path(root) / name, 0, False)
    if error:
        return None, error
    return walk.result(), None


def requirements_content_error(
    root: Path | str,
    name: str = REQUIREMENTS_NAME,
    *,
    is_protected: Callable[[Path], bool],
    parse: RequirementParser,
) -> str | None:
    """Why ``root/name`` (or an include of it) must not be handed to pip, or None.

    A missing top-level file is not an error here; callers decide what a missing
    file means.
    """
    if not os.path.lexists(Path(root) / name):
        return None
    return sanitized_requirements(root, name, is_protected=is_protected, parse=parse)[1]
