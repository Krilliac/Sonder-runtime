"""Editable system and agent prompts: shipped Markdown defaults plus overrides.

Every prompt the runtime sends as instructions to a model is a Markdown file
under ``prompts/`` in the repository (the source of truth shipped in git).
An operator changes one on the fly -- no code change, no restart -- by putting
a file with the same relative name in an override directory:

1. ``$SONDER_PROMPTS_DIR`` when set, else
2. ``<Sonder state home>/prompts/``.

Both are consulted in that order; the first file that exists is the override.

Reads are fresh on every call (a ``stat`` decides whether the cached bytes are
still current), exactly like ``system_profile.md``. Within one turn the text
is pinned (:func:`turn_scope`), so a turn that builds the same prompt twice
cannot straddle an edit.

A bad edit must never break a chat. An override that is empty, larger than
:data:`MAX_BYTES`, not UTF-8, unreadable, resolves outside its directory (a
symlink escape), or whose ``$placeholders`` do not match the prompt's declared
fields is ignored: one warning is logged per distinct file state and the
shipped default is used.

The shipped default is different. Its absence is a packaging defect, not an
operator mistake, so :class:`PromptUnavailable` is raised instead of falling
back to a copy embedded in code: an embedded copy would drift silently from
the file operators are told to edit. ``tests/test_prompt_store*.py`` and the
packaging allowlist keep every default present.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from ..domain import prompt_templates
from ..domain.runtime_identity import runtime_identity_fields
from ..platform import paths as runtime_paths

_logger = logging.getLogger("sonder.prompts")

MAX_BYTES = 64 * 1024
ENV_DIR = "SONDER_PROMPTS_DIR"


@dataclass(frozen=True)
class PromptSpec:
    fields: tuple[str, ...]
    summary: str


# name -> declared template fields. The field set is the contract between the
# code that renders a prompt and every file that may replace it: an override
# must use exactly these placeholders (see prompt_templates.render).
CATALOG: dict[str, PromptSpec] = {
    "agent_hosted": PromptSpec((), "System prompt for hosted (cloud) tool-using agent runs."),
    "agent_local": PromptSpec(("host_brief",), "System prompt for local tool-using agent runs; $host_brief is the one-line host summary."),
    "autopilot_planner": PromptSpec(
        ("objective", "project", "policy", "web", "adaptive", "initial_limit",
         "max_tasks", "max_replans", "tools"),
        "Autopilot planner request (initial plan JSON)."),
    "autopilot_reviewer": PromptSpec(
        ("objective", "issue", "failures", "max_failures", "task_count", "max_tasks",
         "checkpoints", "replans", "max_replans", "ledger"),
        "Autopilot checkpoint reviewer request (next decision JSON)."),
    "autopilot_system": PromptSpec(("role",), "System prompt for Autopilot planner/reviewer model calls."),
    "autopilot_worker": PromptSpec(
        ("objective", "task_id", "kind", "title", "instruction", "criteria", "prior"),
        "Autopilot per-task work request."),
    "child_lane": PromptSpec(("workspace_root", "tools"), "System prompt for a scoped child agent lane."),
    "claim_reviewer": PromptSpec(("tools",), "System prompt for the negative-claim evidence reviewer."),
    "curriculum_task_generator": PromptSpec((), "Self-curriculum practice-task generator prompt."),
    "execution_router": PromptSpec(("project", "request"), "Execution-mode router request (workbench vs autopilot)."),
    "execution_router_system": PromptSpec((), "System prompt for the execution-mode router."),
    "grounded_extraction_system": PromptSpec((), "System prompt for quote-grounded fact extraction."),
    "ollama_alias_system": PromptSpec((), "SYSTEM text baked into the sonder Ollama alias Modelfile (applies when the alias is rebuilt)."),
    "personas/coder": PromptSpec((), "Persona: coder (default)."),
    "personas/explainer": PromptSpec((), "Persona: plain-language explainer."),
    "personas/mathematician": PromptSpec((), "Persona: formal mathematics."),
    "personas/reviewer": PromptSpec((), "Persona: code reviewer."),
    "personas/teacher": PromptSpec((), "Persona: teacher."),
    "reflection_distill": PromptSpec(("signal", "task", "response"), "Lesson-distillation request for a good outcome."),
    "reflection_distill_system": PromptSpec((), "System prompt for lesson distillation."),
    "reflection_pitfall": PromptSpec(("task", "response", "error"), "Pitfall-distillation request for a failed attempt."),
    "reflection_pitfall_system": PromptSpec((), "System prompt for pitfall distillation."),
    "runtime_identity": PromptSpec(("model", "where", "diagnostics"), "Facts block naming the model serving a request."),
    "selfmod_editor": PromptSpec(("objective", "evidence", "criteria", "files", "workspace"), "Task given to the self-improvement editing agent."),
    "trace_instructions": PromptSpec((), "Appended to the system prompt when /trace reasoning is on."),
    "web_research_agent": PromptSpec((), "System prompt for web-routed research runs."),
}


class PromptUnavailable(RuntimeError):
    """A shipped default prompt file is missing or unreadable (packaging defect)."""


@dataclass(frozen=True)
class LoadedPrompt:
    name: str
    text: str
    source: str  # "default" | "override"
    sha256: str
    path: str
    note: str = ""  # why an override was rejected, when it was

    @property
    def label(self) -> str:
        """Compact provenance: ``default`` or ``override@<sha256[:8]>``."""
        return "default" if self.source == "default" else "override@" + self.sha256[:8]


def repo_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts"


def override_dirs() -> list[Path]:
    dirs = []
    configured = os.environ.get(ENV_DIR, "").strip()
    if configured:
        dirs.append(Path(configured).expanduser())
    dirs.append(runtime_paths.default_home() / "prompts")
    return dirs


def _spec(name: str) -> PromptSpec:
    try:
        return CATALOG[name]
    except (KeyError, TypeError):
        raise KeyError("unknown prompt %r" % (name,)) from None


def _relative(name: str) -> str:
    return name + ".md"


def _inside(path: Path, root: Path) -> bool:
    """Case/spelling-tolerant containment, as emotion_vectors._inside_workspace."""
    path_text = os.path.normcase(os.path.normpath(str(path)))
    root_text = os.path.normcase(os.path.normpath(str(root)))
    try:
        return os.path.commonpath([path_text, root_text]) == root_text
    except ValueError:
        return False


def _canonical(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.normpath(os.path.abspath(str(path))))


_LOCK = threading.RLock()
# path -> (mtime_ns, size, text-or-None, error)
_CACHE: dict[str, tuple[int, int, str | None, str]] = {}
_WARNED: set[tuple] = set()


def _read(path: Path) -> tuple[str | None, str, tuple]:
    """Read one prompt file through the stat cache: (text, error, stat stamp)."""
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        cached = _CACHE.get(str(path))
        if cached is not None and cached[:2] == stamp:
            return cached[2], cached[3], stamp
    text, error = None, ""
    if stat.st_size > MAX_BYTES:
        error = "larger than %d bytes" % MAX_BYTES
    else:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                error = "larger than %d bytes" % MAX_BYTES
            else:
                text = prompt_templates.normalize(raw.decode("utf-8"))
        except UnicodeDecodeError:
            error = "not valid UTF-8"
        except OSError as exc:
            error = "unreadable (%s)" % type(exc).__name__
    with _LOCK:
        _CACHE[str(path)] = (stamp[0], stamp[1], text, error)
    return text, error, stamp


def _validate(name: str, text: str) -> str:
    """Why ``text`` cannot serve as prompt ``name``, or ``""`` when it can."""
    if not text.strip():
        return "empty"
    fields = CATALOG[name].fields
    if fields:
        try:
            prompt_templates.render(text, {field: "" for field in fields})
        except prompt_templates.PromptTemplateError as exc:
            return str(exc)
    if name == "ollama_alias_system" and '"""' in text:
        # The text is embedded in a Modelfile as SYSTEM """...""".
        return 'contains """ which would terminate the Modelfile SYSTEM block'
    return ""


def _warn_once(name: str, path: Path, stamp: tuple, reason: str) -> None:
    marker = (str(path), stamp, reason)
    with _LOCK:
        if marker in _WARNED:
            return
        _WARNED.add(marker)
    _logger.warning(
        "prompt override %s ignored (%s); using the shipped default for %r",
        path, reason, name,
    )


def _default(name: str) -> LoadedPrompt:
    path = repo_dir() / _relative(name)
    try:
        text, error, _stamp = _read(path)
    except OSError as exc:
        raise PromptUnavailable(
            "shipped default prompt %r is missing at %s (%s)" % (name, path, type(exc).__name__)
        ) from None
    if text is None or error:
        raise PromptUnavailable("shipped default prompt %r at %s is %s" % (name, path, error))
    problem = _validate(name, text)
    if problem:
        raise PromptUnavailable("shipped default prompt %r at %s is invalid: %s" % (name, path, problem))
    return LoadedPrompt(name, text, "default", prompt_templates.digest(text), str(path))


def _override_candidate(name: str) -> tuple[Path, Path] | None:
    """(override directory, candidate file) for the first existing override."""
    for directory in override_dirs():
        candidate = directory / _relative(name)
        if os.path.lexists(candidate):
            return directory, candidate
    return None


def _select(name: str) -> LoadedPrompt:
    """The effective prompt for ``name``: a valid override, else the default."""
    _spec(name)
    found = _override_candidate(name)
    if found is None:
        return _default(name)
    directory, candidate = found
    resolved = _canonical(candidate)
    note = ""
    if not (_inside(resolved, _canonical(directory)) or _inside(resolved, _canonical(repo_dir()))):
        note = "resolves outside its override directory"
        stamp = ("escape",)
    else:
        try:
            text, error, stamp = _read(resolved)
        except OSError as exc:
            text, error, stamp = None, "unreadable (%s)" % type(exc).__name__, ("oserror",)
        note = error or _validate(name, text or "")
        if not note:
            return LoadedPrompt(name, text, "override", prompt_templates.digest(text), str(resolved))
    _warn_once(name, candidate, stamp, note)
    default = _default(name)
    return LoadedPrompt(default.name, default.text, default.source, default.sha256,
                        default.path, note="override %s ignored: %s" % (candidate, note))


# --- per-turn pinning and provenance ---------------------------------------

_TURN = threading.local()


@contextlib.contextmanager
def turn_scope():
    """Pin every prompt read on this thread for one turn and record provenance.

    Re-entrant: a nested scope (a lane inside an already-scoped turn) keeps
    the outer turn's pins instead of taking a fresh reading.
    """
    if getattr(_TURN, "pins", None) is not None:
        yield
        return
    _TURN.pins = {}
    try:
        yield
    finally:
        _TURN.pins = None


def turn_scoped(function):
    """Decorator form of :func:`turn_scope`."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with turn_scope():
            return function(*args, **kwargs)
    wrapper.__prompt_turn_scoped__ = True
    return wrapper


def current_provenance() -> dict[str, str]:
    """``{name: "default" | "override@<hash8>"}`` for prompts used in this turn."""
    pins = getattr(_TURN, "pins", None) or {}
    return {name: loaded.label for name, loaded in sorted(pins.items())}


def load(name: str) -> LoadedPrompt:
    """The effective (unrendered) prompt, pinned for the current turn if any."""
    pins = getattr(_TURN, "pins", None)
    if pins is not None and name in pins:
        return pins[name]
    loaded = _select(name)
    if pins is not None:
        pins[name] = loaded
    return loaded


def render(name: str, **fields) -> str:
    """Render prompt ``name`` with exactly its declared fields."""
    declared = set(_spec(name).fields)
    if set(fields) != declared:
        raise prompt_templates.PromptTemplateError(
            "prompt %r takes fields %s, got %s" % (name, sorted(declared), sorted(fields))
        )
    return prompt_templates.render(load(name).text, fields)


def reload() -> int:
    """Drop cached file contents and warning memory; returns entries dropped."""
    with _LOCK:
        dropped = len(_CACHE)
        _CACHE.clear()
        _WARNED.clear()
    return dropped


def runtime_identity_block(model, cloud: bool = False, provider: str | None = None) -> str:
    """The rendered identity block for one request, or ``""`` without a model."""
    fields = runtime_identity_fields(model, cloud, provider)
    if fields is None:
        return ""
    return render("runtime_identity", **fields)


# --- /prompts ---------------------------------------------------------------

PROMPTS_USAGE = (
    "usage: /prompts [list] | show <name> | path <name> | reload\n"
    "  Prompts are Markdown files; edit a copy in the override directory"
    " (see /prompts path <name>). Changes apply on the next turn."
)


def _describe(loaded: LoadedPrompt) -> str:
    text = "%-28s %-18s sha256:%s" % (loaded.name, loaded.label, loaded.sha256[:12])
    if loaded.note:
        text += "  (%s)" % loaded.note
    return text


def command(arg: str = "") -> str:
    """Read-only ``/prompts`` console command (``reload`` only clears a cache)."""
    parts = str(arg or "").split()
    action = parts[0].lower() if parts else "list"
    target = parts[1] if len(parts) > 1 else ""
    if action in ("list", "ls"):
        lines = ["prompts (default = shipped in %s):" % repo_dir()]
        for name in sorted(CATALOG):
            try:
                lines.append("  " + _describe(_select(name)))
            except PromptUnavailable as exc:
                lines.append("  %-28s MISSING  %s" % (name, exc))
        lines.append("override dirs, first match wins: %s" % ", ".join(str(d) for d in override_dirs()))
        return "\n".join(lines)
    if action in ("show", "path") and not target:
        return PROMPTS_USAGE
    if action in ("show", "path") and target not in CATALOG:
        return "unknown prompt %r; /prompts list shows the names." % target
    if action == "show":
        loaded = _select(target)
        spec = CATALOG[target]
        header = [_describe(loaded), "file: %s" % loaded.path]
        if spec.fields:
            header.append("placeholders: %s" % " ".join("$" + f for f in spec.fields))
        return "\n".join(header) + "\n\n" + loaded.text
    if action == "path":
        spec = CATALOG[target]
        destination = override_dirs()[0] / _relative(target)
        lines = [
            "override: %s" % destination,
            "default:  %s" % (repo_dir() / _relative(target)),
            "Copy the default there and edit it; it is re-read on the next turn.",
        ]
        if spec.fields:
            lines.append(
                "Keep exactly these placeholders: %s ($$ for a literal $)."
                % " ".join("$" + f for f in spec.fields)
            )
        return "\n".join(lines)
    if action == "reload":
        return "prompt cache cleared (%d cached file(s)); every prompt is re-read from disk." % reload()
    return PROMPTS_USAGE
