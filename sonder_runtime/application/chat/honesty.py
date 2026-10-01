"""Small, side-effect-free guards for plain chat responses.

These helpers deliberately do not inspect the filesystem.  The caller owns the
actual tool receipt and passes the paths written during the turn, which keeps a
plain response from gaining an implicit filesystem authority.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from .tool_intent import classify_file_intent


_UNSAVED_NOTE = "(Not saved — ask me to create it and I will write it to {folder}.)"
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_FILE_CLAIM_RE = re.compile(
    r"\b(?:saved|created|wrote|written|generated)\b"
    r"(?:\s+(?:the|a|an|this|that))?\s+(?:file|script|document)\b",
    re.IGNORECASE,
)
_FILE_CLAIM_PATH_RE = re.compile(
    r"\b(?:saved|created|wrote|written|generated)\b[^\n.!?]{0,120}"
    r"(?:[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,12}|file|script|document)\b",
    re.IGNORECASE,
)
_PASSIVE_FILE_CLAIM_RE = re.compile(
    r"\b(?:[A-Za-z0-9_.`'\"/\\-]+\s+)?(?:file|script|document)\b\s+"
    r"(?:was|has been|is)\s+(?:saved|created|written|generated)\b"
    r"|\b[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,12}\b\s+"
    r"(?:was|has been|is)\s+(?:saved|created|written|generated)\b",
    re.IGNORECASE,
)
_RUN_PATH_RE = re.compile(
    r"\b(?:python(?:\.exe)?|py(?:\.exe)?|node(?:\.exe)?|ruby(?:\.exe)?|bash|sh)"
    r"\s+(?:[\"']([^\"']+)[\"']|(`[^`]+`)|([^\s,;:.!?]+\.[A-Za-z0-9]{1,12}))",
    re.IGNORECASE,
)
_RUN_IT_PATH_RE = re.compile(
    r"\brun\s+(?:it|that|the\s+(?:file|script))\s+(?:with\s+)?"
    r"(?:[\"']([^\"']+)[\"']|(`[^`]+`)|([^\s,;:.!?]+\.[A-Za-z0-9]{1,12}))",
    re.IGNORECASE,
)
_RUN_DIRECT_PATH_RE = re.compile(
    r"\brun\s+(?:(?:python(?:\.exe)?|py(?:\.exe)?)\s+)?"
    r"(?:[\"'`]([^\"'`]+)[\"'`]|"
    r"([A-Za-z]:[\\/][^\s,;!?]+\.[A-Za-z0-9]{1,12})|"
    r"([.\\/A-Za-z0-9_-]+\.[A-Za-z0-9]{1,12}))",
    re.IGNORECASE,
)
_CLAIM_PATH_RE = re.compile(
    r"\b(?:saved|created|wrote|written|generated)\b[^\n.!?]{0,100}?"
    r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,12})\b",
    re.IGNORECASE,
)


def _without_code_fences(text: str) -> str:
    """Ignore fenced examples, where imperative text is usually illustrative."""

    return _CODE_FENCE_RE.sub(" ", text)


def _normalise_path(value: str) -> str:
    value = value.strip().strip("`\"'")
    value = value.rstrip(".,;:!?)]}")
    return value.replace("\\", "/").casefold()


def _written(path: str, written_paths: tuple[str, ...]) -> bool:
    candidate = _normalise_path(path)
    if not candidate:
        return False
    for written in written_paths:
        actual = _normalise_path(written)
        if candidate == actual or actual.endswith("/" + candidate):
            return True
    return False


def _run_paths(text: str) -> tuple[str, ...]:
    paths: list[str] = []
    for match in (
        *_RUN_PATH_RE.finditer(text),
        *_RUN_IT_PATH_RE.finditer(text),
        *_RUN_DIRECT_PATH_RE.finditer(text),
    ):
        paths.extend(part for part in match.groups() if part)
    return tuple(paths)


def _has_file_claim(text: str) -> bool:
    for pattern in (_FILE_CLAIM_RE, _FILE_CLAIM_PATH_RE, _PASSIVE_FILE_CLAIM_RE):
        for match in pattern.finditer(text):
            if re.match(r"no\s+", match.group(0), re.IGNORECASE):
                continue
            prefix = text[max(0, match.start() - 32) : match.start()].casefold()
            if re.search(
                r"(?:did\s+not|didn't|haven't|hasn't|never|not|no|once|when|if|after)\s+(?:\w+\s+){0,2}$",
                prefix,
            ):
                continue
            return True
    return False


def _claim_paths(text: str) -> tuple[str, ...]:
    paths = [match.group(1) for match in _CLAIM_PATH_RE.finditer(text)]
    for match in _PASSIVE_FILE_CLAIM_RE.finditer(text):
        candidate = match.group(0).split()[0].strip("`\"'")
        if "." in candidate and candidate not in paths:
            paths.append(candidate)
    return tuple(paths)


def _is_future_run(text: str, path: str) -> bool:
    marker = re.search(re.escape(path), text, re.IGNORECASE)
    if marker is None:
        return False
    prefix = text[max(0, marker.start() - 40) : marker.start()].casefold()
    return bool(re.search(r"\b(?:once|when|if|after)\b[^.?!]{0,40}\brun\b", prefix))


def guard_unsaved_file_claim(
    text: str, *, folder: str, written_paths: Iterable[str] = ()
) -> str:
    """Append a truthful save note when plain chat claims an unperformed write.

    The check is intentionally narrow: past-tense file claims and executable
    file paths are considered, while fenced code and negated/future examples
    are ignored where practical.  It never reads or writes a path.
    """

    if not isinstance(text, str) or not text:
        return text
    if _UNSAVED_NOTE.format(folder=folder) in text:
        return text
    visible = _without_code_fences(text).replace("`", "")
    written = tuple(str(path) for path in written_paths)

    claim = _has_file_claim(visible)
    claim_paths = _claim_paths(visible)
    run_path = next(
        (
            path
            for path in _run_paths(visible)
            if not _written(path, written) and not _is_future_run(visible, path)
        ),
        None,
    )
    if not claim and run_path is None:
        return text
    unverified_claim = any(not _written(path, written) for path in claim_paths)
    if run_path is not None or unverified_claim or (claim and not written):
        return text + "\n\n" + _UNSAVED_NOTE.format(folder=folder)
    return text


_NO_TOOLS_GUIDANCE = (
    "If this request needs local workspace facts or file contents, Sonder can "
    "use its scoped read-only tools: workspace_inventory, directory_tree, "
    "file_find, text_search, file_read/file_read_range, and "
    "repository_symbol_index. If asked to create or edit files, Sonder can use "
    "the gated workbench/agent lane. Without a tool result, do not claim that "
    "a file was written, run, or verified. Ask directly in chat so the runtime "
    "can choose the appropriate scoped lane."
)


def no_tools_system(system: str, prompt: str) -> str:
    """Add guidance to an unrouted default-local file turn.

    The caller must exclude explicit-model and cloud bypasses. The shared
    classifier remains the only authority for recognizing file turns here.
    """

    if not isinstance(system, str) or not isinstance(prompt, str):
        return system
    if not classify_file_intent(prompt):
        return system
    if _NO_TOOLS_GUIDANCE in system:
        return system
    return f"{system}\n\n{_NO_TOOLS_GUIDANCE}" if system else _NO_TOOLS_GUIDANCE


__all__ = ["guard_unsaved_file_claim", "no_tools_system"]
