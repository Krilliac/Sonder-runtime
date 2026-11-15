"""Enforcement for restricted models in the ``agent`` and explicit-tier ``workbench_agent`` lanes.

A model a restricted profile matches (``domain.model_tool_grants``) stays on
the tool-lane deny list, so every lane refuses it by default.  Only an agent
turn that an entry point marked as one of the two restricted lanes asks this
module, and then only for a model the caller named directly (``_serve_target``
refused it as ``tool-lane-denied``).  Everything else (tier-bound models, the
default route, bridged tiers, prepared work, unsafe lab, and every other lane)
keeps the refusal it has today.

Entry marking.  ``server.agent`` asks :func:`entry` with lane ``agent``;
``server.workbench_agent`` with an explicit tier wraps its call in
:func:`entering` (``workbench_agent``).  A caller that runs those entry points
on behalf of another lane (the model-driven loop, the escalating workbench,
the chat file router) wraps them in :func:`suppressed`, which no inner
:func:`entering` can lift.  ``_agent_impl`` called directly carries no entry
and so never grants.

A restricted turn (:class:`RestrictedRun`) is then fenced:

* its tool allowlist is the intersection of the caller's allowlist (if any)
  and the granted tools, and the agent loop refuses any other tool at dispatch
  (``server._agent_turn`` checks ``allowed_tools`` before every dispatch);
* web, location and writes are off (``read_only``), speculation is off, and it
  gets no private host brief, profile or playbooks;
* it needs a project directory the caller named, and every path argument is
  confined to it and checked against the secret globs after the host's own
  project scoping (``tool_policy``);
* ``include_hidden``/``include_ignored`` are refused, and the lines of a
  content search (``text_search``, ``repository_symbol_index``) and the
  entries of a ``directory_tree`` that name a path matching a secret glob
  (spelled or real; a tree entry under a matching directory too) are withheld
  from the observation (``filter_observation``); a line that does not parse is
  withheld too.

Its own model calls.  The restricted model stays on the deny list, so the
generation guard (``tool_lane_guard.require_generation``) would refuse its
first call.  ``_agent_turn`` wraps only this turn's generator in
:func:`permitted` (``generation_permit``), which holds a permit for the length
of each call on the calling thread; the guard then lets exactly this run's
model through, after :meth:`RestrictedRun.still_granted` decides its grant
again.  Tool dispatch, nested agents and every other model call run outside
that window, and the permit names one model, so it opens nothing else.

A :class:`RestrictedRun` belongs to one agent turn on one thread; it is not
shared and holds no lock.
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sonder_runtime.adapters.filesystem.path_aliases as _path_aliases
import sonder_runtime.adapters.generation_permit as _generation_permit
import sonder_runtime.adapters.model_tool_grants as _grants
import sonder_runtime.adapters.model_tool_policy as _mtp
import sonder_runtime.adapters.runtime_policy as _policy
import sonder_runtime.adapters.tool_lane_guard as _guard
from sonder_runtime.domain import model_tool_grants as _rules
from sonder_runtime.domain.tool_lane_models import ModelLaneStatus, refusal_message

# None: no entry point has spoken (lane defaults apply); "": suppressed.
_ENTRY: ContextVar[str | None] = ContextVar("sonder_restricted_tool_lane_entry", default=None)
_SUPPRESSED = ""
_POLICY = "ERROR: HOST POLICY: "
_SEARCH_TOOLS = frozenset({"text_search", "repository_symbol_index"})
_TREE_TOOL = "directory_tree"
_PATH_KEYS = frozenset({
    "path", "root", "file", "filename", "file_path", "left", "right", "source",
    "destination", "input_path", "output_path", "project_dir", "cwd", "directory", "dir",
})
_PATH_SUFFIXES = ("_path", "_dir", "_root", "_file")
_HOST_KEYS = frozenset({"extra_roots", "token", "approval"})
_LIST_PATH_KEYS = frozenset({"paths_json", "paths"})
_HIDDEN_KEYS = ("include_hidden", "include_ignored")
# A content-search row: ``<path>:<line>:<column>`` then ``: `` (text_search) or
# `` `` (repository_symbol_index).
_ROW = re.compile(r":\d+:\d+(?=: | )")
# A ``directory_tree`` entry as ``server.directory_tree`` formats it:
# ``<indent>[D] <relative>`` or ``<indent>[F] <relative> (<n> bytes)``.
_TREE_ENTRY = re.compile(r"^ {2,}\[(D|F)\] (.+)$")
_TREE_FILE_SIZE = re.compile(r"^(.+) \(\d+ bytes\)$")
_TREE_TRUNCATED = re.compile(r"^  \.\.\. truncated at \d+ entries$")


def _refusal(text: str) -> str:
    message = "%s%s" % (_POLICY, text)
    return message


# ---- entry marking ------------------------------------------------------------

@dataclass(frozen=True)
class LaneEntry:
    """The restricted lane an entry point opened, and the project its caller named."""

    lane: str
    caller_project: str


@contextlib.contextmanager
def entering(lane: str) -> Iterator[None]:
    """Mark calls inside as entering ``lane``; a surrounding :func:`suppressed` still wins."""
    if lane not in _rules.RESTRICTED_LANES:
        raise ValueError("not a restricted lane: %r" % (lane,))
    current = _ENTRY.get()
    token = _ENTRY.set(_SUPPRESSED if current == _SUPPRESSED else lane)
    try:
        yield
    finally:
        _ENTRY.reset(token)


@contextlib.contextmanager
def suppressed() -> Iterator[None]:
    """No call inside may open a restricted lane (another lane runs the entry point)."""
    token = _ENTRY.set(_SUPPRESSED)
    try:
        yield
    finally:
        _ENTRY.reset(token)


def suppressing(function: Callable) -> Callable:
    """Decorator form of :func:`suppressed`."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with suppressed():
            return function(*args, **kwargs)

    return wrapped


def entry(default_lane: str, caller_project: Any) -> LaneEntry | None:
    """The lane this entry point opens, or None when a restricted lane may not open here."""
    current = _ENTRY.get()
    if current == _SUPPRESSED:
        return None
    lane = current or default_lane
    if lane not in _rules.RESTRICTED_LANES:
        return None
    return LaneEntry(lane, "" if caller_project is None else str(caller_project))


# ---- the decision -------------------------------------------------------------

def _policy_section(policy: Mapping[str, Any] | None) -> Any:
    return policy.get("tool_lane_denied_models") if isinstance(policy, Mapping) else None


def decide(model: Any, policy: Mapping[str, Any] | None = None) -> _rules.ModelToolDecision:
    """The tool decision for a caller-named ``model`` (deny list, grants, deny-only entries)."""
    config = _mtp.load()
    grants = _grants.grant_set(config)
    deny = _guard.deny_list(policy)
    deny_only = _grants.deny_only_list(grants, deny.floor, _policy_section(policy))
    return _rules.decide_tools(model, deny, grants.grants, deny_only, free_form=True)


def admit(
    lane_entry: LaneEntry | None,
    selector: Any,
    tier_label: Any,
    policy: Mapping[str, Any] | None,
    *,
    unsafe: bool,
    prepared: bool,
    resolve: Callable[[], tuple],
    provider_for: Callable[[Any], Any],
) -> tuple["RestrictedRun | None", str]:
    """Open a restricted run for this agent turn, if one applies.

    ``(run, "")`` when it applies; ``(None, refusal)`` when the selector has a
    grant for this lane but the resolved target cannot honour it; ``(None,
    "")`` when it does not apply (the caller keeps its ordinary refusal).
    ``resolve`` returns the chat-only target ``(model, cloud, augment,
    tier_label)``; ``provider_for`` names a non-Ollama provider for a label.
    """
    if lane_entry is None or unsafe or prepared or tier_label != _guard.DENIED_LABEL:
        return None, ""
    decision = decide(selector, policy)
    if not decision.permits(lane_entry.lane):
        return None, ""
    target = tuple(resolve())
    model, cloud, _augment, label = target
    if model is None or not str(label or "").startswith("model:"):
        return None, _refusal(
            "model '%s' has a restricted tool grant but is not an installed local model." % (selector,)
        )
    if cloud or provider_for(label):
        return None, _refusal(
            "model '%s' has a restricted tool grant, which applies only to a local Ollama model." % (selector,)
        )
    confirmed = decide(model, policy)
    tools = decision.tools & confirmed.tools
    if not confirmed.permits(lane_entry.lane) or not tools:
        return None, _rules_refusal(model, confirmed)
    config = _mtp.load()
    globs = _grants.grant_set(config).secret_globs
    if not globs:
        return None, _rules_refusal(model, confirmed)
    return RestrictedRun(
        lane=lane_entry.lane, model=str(model), tools=tools,
        profiles=tuple(dict.fromkeys(decision.profiles + confirmed.profiles)),
        secret_globs=globs, caller_project=lane_entry.caller_project, target=target,
    ), ""


def permitted(run: "RestrictedRun | None", generate: Callable[..., Any]) -> Callable[..., Any]:
    """``generate`` unchanged without a restricted run, else :meth:`RestrictedRun.generator`."""
    return generate if run is None else run.generator(generate)


def _rules_refusal(model: Any, decision: _rules.ModelToolDecision) -> str:
    status = {
        _rules.ModelToolStatus.POLICY_UNREADABLE: ModelLaneStatus.POLICY_UNREADABLE,
        _rules.ModelToolStatus.CONFIG_UNAVAILABLE: ModelLaneStatus.CONFIG_UNAVAILABLE,
        _rules.ModelToolStatus.OVERLAY_UNUSABLE: ModelLaneStatus.OVERLAY_UNUSABLE,
    }.get(decision.status, ModelLaneStatus.DENIED)
    return refusal_message(model, status, decision.matched)


# ---- one restricted turn ------------------------------------------------------

def _is_absolute(text: str) -> bool:
    return os.path.isabs(text) or (len(text) > 2 and text[1] == ":" and text[2] in "\\/")


class RestrictedRun:
    """The fences of one restricted agent turn.

    Owned by that turn on one thread; ``bind_project`` is called once, before
    the first tool call.  ``tools`` never grows.
    """

    def __init__(
        self, *, lane: str, model: str, tools: frozenset[str], profiles: tuple[str, ...],
        secret_globs: tuple[str, ...], caller_project: str, target: tuple,
    ) -> None:
        self.lane = lane
        self.model = model
        self.tools = frozenset(tools)
        self.profiles = profiles
        self.secret_globs = tuple(secret_globs)
        self.caller_project = caller_project
        self.target = target
        self.project_root = ""

    def generator(self, generate: Callable[..., Any]) -> Callable[..., Any]:
        """``generate``, with this run's own model let through the generation guard.

        The permit holds for the length of each call, on the thread making it
        (``generation_permit.wrap``); a call made on another thread, or after
        the call returns, does not carry it and is refused as before.
        """
        return _generation_permit.wrap(generate, model=self.model, still_granted=self.still_granted)

    def current_tools(self, policy: Mapping[str, Any] | None = None) -> frozenset[str]:
        """This run's tools that the files still grant in its lane, decided again now.

        Never more than ``tools``: a tool granted after the turn opened does
        not join it.  Any fault answers the empty set.
        """
        try:
            decision = decide(self.model, policy if policy else _policy.load(create=False))
        except Exception:  # noqa: BLE001 - an unreadable policy grants nothing
            return frozenset()
        return self.tools & decision.tools if decision.permits(self.lane) else frozenset()

    def still_granted(self, policy: Mapping[str, Any] | None = None) -> bool:
        """Whether the files still grant every tool this run was opened with, in its lane.

        Decided again on each model call, so revoking any one of the run's
        tools mid-turn stops its next model call.  Any fault answers False.
        """
        return self.current_tools(policy) == self.tools

    def current_secret_globs(self) -> tuple[str, ...] | None:
        """The globs captured at admission plus every glob the files hold now; None when they cannot be read.

        Read again at every dispatch and every observation filter, so a glob
        the operator adds mid-turn applies at once; one removed mid-turn keeps
        applying until the turn ends.  The files are loaded once, and an unusable
        shipped file (missing, invalid, or changed since the startup pin) or
        overlay (present but invalid) gives None, so a turn that started under a
        grant withholds everything once the files stop being trustworthy.
        """
        try:
            config = _mtp.load()
            if not (config.readable_shipped and config.readable_overlay) or config.overlay_problem:
                return None
            current = tuple(_grants.grant_set(config).secret_globs)
        except Exception:  # noqa: BLE001 - unreadable globs: the caller refuses or withholds
            return None
        return tuple(dict.fromkeys(self.secret_globs + current))

    def allowlist(self, requested: Any) -> frozenset[str]:
        """The caller's allowlist (when it gave one) narrowed to the granted tools."""
        if requested is None:
            return self.tools
        return self.tools & frozenset(str(name) for name in requested if name)

    def bind_project(self, project_scope: str, *, codegen_requested: bool = False) -> str:
        """Bind the project root; a refusal when there is none the caller named."""
        if codegen_requested:
            return _refusal(
                "model '%s' is restricted to read-only tools and cannot run compiler-feedback retries."
                % self.model
            )
        scope = str(project_scope or "").strip()
        caller = self.caller_project.strip()
        named = ""
        if caller:
            try:
                named = os.path.realpath(os.path.abspath(os.path.expanduser(caller)))
            except (OSError, ValueError):
                named = ""
        if (
            not scope or not named or not os.path.isdir(named)
            or os.path.normcase(named) != os.path.normcase(os.path.realpath(scope))
        ):
            return _refusal(
                "model '%s' is restricted (profile %s): a restricted model needs a project root; "
                "pass project=<an existing project directory>." % (self.model, ", ".join(self.profiles))
            )
        self.project_root = os.path.realpath(scope)
        return ""

    # -- path checks --------------------------------------------------------

    def _relative(self, value: str) -> str | None:
        """``value`` relative to the project root (posix), or None when it is outside."""
        root = Path(self.project_root)
        candidate = Path(os.path.expanduser(value))
        if not _is_absolute(str(candidate)):
            candidate = root / candidate
        try:
            resolved = Path(os.path.realpath(candidate))
            return resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            return None

    def _path_values(self, args: Mapping[str, Any]) -> Iterator[tuple[str, str]] | None:
        values: list[tuple[str, str]] = []
        for key, value in args.items():
            if key in _HOST_KEYS:
                continue
            if key in _LIST_PATH_KEYS:
                items = value
                if isinstance(value, str):
                    try:
                        items = json.loads(value)
                    except ValueError:
                        return None
                if not isinstance(items, (list, tuple)):
                    return None
                for item in items:
                    if not isinstance(item, str):
                        return None
                    values.append((key, item))
            elif (key in _PATH_KEYS or key.endswith(_PATH_SUFFIXES)) and isinstance(value, str) and value.strip():
                values.append((key, value.strip()))
        return iter(values)

    def path_refusal(
        self, tool_name: str, scoped_args: Mapping[str, Any], globs: tuple[str, ...] | None = None,
    ) -> str:
        """A refusal when a path argument leaves the project or names a secret, else ``""``.

        ``globs`` defaults to :meth:`current_secret_globs`.
        """
        globs = self.current_secret_globs() if globs is None else globs
        if globs is None:
            return _refusal("tool '%s': the restricted secret path globs could not be read." % tool_name)
        if not self.project_root:
            return _refusal("restricted model '%s' has no bound project root." % self.model)
        values = self._path_values(scoped_args)
        if values is None:
            return _refusal("tool '%s': the path list could not be read." % tool_name)
        for key, value in values:
            alias = _path_aliases.win32_alias_component(value)
            if alias:
                return _refusal(
                    "tool '%s': %s %r has a Windows alias component (%r); restricted model '%s' may not use it."
                    % (tool_name, key, value, alias, self.model)
                )
            relative = self._relative(value)
            if relative is None:
                return _refusal(
                    "tool '%s': %s %r is outside the project root of restricted model '%s'."
                    % (tool_name, key, value, self.model)
                )
            matched = _rules.secret_glob_match(relative, globs)
            if matched:
                return _refusal(
                    "tool '%s': %s %r matches the restricted secret path glob %r; restricted model "
                    "'%s' may not read it." % (tool_name, key, relative, matched, self.model)
                )
        return ""

    def tool_policy(
        self, inner: Callable[[str, Any], Any] | None,
        scope_args: Callable[[str, Any, str], Any],
    ) -> Callable[[str, Any], str]:
        """An argument-aware policy for this turn, composed after ``inner`` would allow.

        A tool is dispatched only while the files still grant it
        (:meth:`current_tools`, decided again at every dispatch), so revoking
        it mid-turn refuses its next call; the secret globs are read again at
        every dispatch too (:meth:`current_secret_globs`).  ``scope_args`` is the host's own project scoping
        (``server._project_scope_args``), so the paths checked are the paths
        dispatched.
        """
        def check(tool_name: str, args: Any) -> str:
            if tool_name not in self.tools:
                return _refusal(
                    "restricted model '%s' may use only: %s; '%s' is not granted."
                    % (self.model, ", ".join(sorted(self.tools)), tool_name)
                )
            if tool_name not in self.current_tools():
                return _refusal(
                    "tool '%s' is no longer granted to restricted model '%s'." % (tool_name, self.model)
                )
            if not isinstance(args, dict):
                return _refusal("tool '%s': arguments must be a JSON object." % tool_name)
            for key in _HIDDEN_KEYS:
                if key in args and args[key] not in (False, None):
                    return _refusal(
                        "tool '%s': restricted model '%s' may not set %s." % (tool_name, self.model, key)
                    )
            globs = self.current_secret_globs()
            if globs is None:
                return _refusal("tool '%s': the restricted secret path globs could not be read." % tool_name)
            pattern = args.get("glob")
            if isinstance(pattern, str) and pattern.strip():
                matched = _rules.secret_glob_match(pattern.strip(), globs)
                if matched:
                    return _refusal(
                        "tool '%s': glob %r selects files matching the restricted secret path glob %r."
                        % (tool_name, pattern, matched)
                    )
            refused = self.path_refusal(tool_name, scope_args(tool_name, args, self.project_root), globs)
            if refused:
                return refused
            return str(inner(tool_name, args) or "") if inner is not None else ""

        return check

    # -- observation filter -------------------------------------------------

    def _secret_or_outside(self, base: str, path_text: str, globs: tuple[str, ...]) -> bool:
        """Whether a result row's path is outside the project or names a secret.

        Both the spelled path and its ``realpath`` are checked: a junction or
        symlink inside the project can spell an inside path for a file that is
        outside it, and the glob applies to either name.
        """
        if not path_text.strip() or not self.project_root:
            return True
        lexical = os.path.normpath(os.path.join(base, path_text.strip()))
        root = os.path.realpath(self.project_root)
        for candidate in (lexical, os.path.realpath(lexical)):
            try:
                relative = os.path.relpath(candidate, root).replace("\\", "/")
            except ValueError:  # another drive on Windows
                return True
            if relative == ".." or relative.startswith("../"):
                return True
            if _rules.secret_glob_match(relative, globs):
                return True
        return False

    def _row_names_secret(self, base: str, rest: str, globs: tuple[str, ...]) -> bool:
        """Whether a ``path:line:col`` result row names a secret path (or does not parse).

        Every prefix followed by ``:<line>:<col>`` is a candidate path, so a
        path that itself contains such a sequence is still checked.
        """
        candidates = [rest[:found.start()] for found in _ROW.finditer(rest) if found.start() > 0]
        if not candidates:
            return True
        return any(self._secret_or_outside(base, candidate, globs) for candidate in candidates)

    def observation_filter(self, tool_name: str, scoped_args: Mapping[str, Any]) -> Callable[[Any], Any]:
        """The dispatcher applies this before anything (the activity record included) sees the result.

        It never raises: a filter fault withholds the whole result.
        """
        def apply(observation: Any) -> Any:
            try:
                return self.filter_observation(tool_name, scoped_args, observation)
            except Exception as exc:  # noqa: BLE001 - fail closed, never the raw text
                message = _refusal("tool '%s': its result could not be filtered (%s), so it is withheld."
                                   % (tool_name, type(exc).__name__))
                return message

        return apply

    def _tree_entry_withheld(self, base: str, line: str, globs: tuple[str, ...]) -> bool:
        """Whether a ``directory_tree`` line is withheld: it does not parse, or it or a parent names a secret."""
        entry = _TREE_ENTRY.match(line)
        if entry is None:
            return True
        relative = entry.group(2)
        if entry.group(1) == "F":
            sized = _TREE_FILE_SIZE.match(relative)
            if sized is None:
                return True
            relative = sized.group(1)
        parts = [part for part in re.split(r"[\\/]", relative) if part]
        if not parts:
            return True
        # The entry itself and every directory above it: nothing is listed
        # from inside a directory that matches a glob.
        return any(
            self._secret_or_outside(base, os.path.join(*parts[:end]), globs) for end in range(1, len(parts) + 1)
        )

    def _filter_tree(self, scoped_args: Mapping[str, Any], text: str, globs: tuple[str, ...]) -> str:
        raw = (scoped_args.get("path") or scoped_args.get("root")) if isinstance(scoped_args, Mapping) else ""
        if not self.project_root:
            return _refusal("tool '%s': the tree root is unknown, so its result is withheld." % _TREE_TOOL)
        base = os.path.realpath(os.path.join(self.project_root, str(raw or ".")))
        lines = text.split("\n")
        kept = lines[:1] if lines and lines[0].startswith("directory tree: ") else []
        withheld = 0
        for line in lines[len(kept):]:
            if _TREE_TRUNCATED.match(line) or not self._tree_entry_withheld(base, line, globs):
                kept.append(line)
            else:
                withheld += 1
        if withheld:
            kept.append(
                "  [withheld %d entr(y/ies) that name a path matching the restricted secret path globs]" % withheld
            )
        return "\n".join(kept)

    def filter_observation(self, tool_name: str, scoped_args: Mapping[str, Any], observation: Any) -> Any:
        """Withhold content-search lines and tree entries that name a secret path (or do not parse).

        The globs are read again now (:meth:`current_secret_globs`); when they
        cannot be read the whole result is withheld.
        """
        if tool_name not in _SEARCH_TOOLS and tool_name != _TREE_TOOL:
            return observation
        text = str(observation)
        if text.lstrip().startswith("ERROR"):
            return observation
        globs = self.current_secret_globs()
        if globs is None:
            return _refusal(
                "tool '%s': the restricted secret path globs could not be read, so its result is withheld."
                % tool_name
            )
        if tool_name == _TREE_TOOL:
            return self._filter_tree(scoped_args, text, globs)
        key = "root" if tool_name == "text_search" else "path"
        base = str(scoped_args.get(key) or self.project_root) if isinstance(scoped_args, Mapping) else ""
        if not base:
            return _refusal("tool '%s': the search root is unknown, so its result is withheld." % tool_name)
        base = os.path.realpath(base)
        if os.path.isfile(base):
            base = os.path.dirname(base)
        # ``split("\n")``, not ``splitlines``: the formatter joins rows with
        # ``\n`` only, and a row's own text may hold other line breaks that
        # must stay inside that row.
        lines = text.split("\n")
        kept: list[str] = []
        withheld = 0
        section = "rows" if tool_name == "text_search" else "header"
        for index, line in enumerate(lines):
            if tool_name == "text_search" and index == 0:
                kept.append(line)
                continue
            if section == "header":
                kept.append(line)
                if line == "symbols:":
                    section = "rows"
                continue
            if line == "errors:" and tool_name == "repository_symbol_index":
                kept.append(line)
                section = "errors"
                continue
            if line.startswith(("  (no matches)", "  (none)", "  ... truncated")):
                kept.append(line)
                continue
            if not line.startswith("  "):
                withheld += 1
                continue
            rest = line[2:]
            if section == "errors":
                path_text, separator, _detail = rest.partition(": ")
                secret = not separator or self._secret_or_outside(base, path_text, globs)
            else:
                secret = self._row_names_secret(base, rest, globs)
            if secret:
                withheld += 1
                continue
            kept.append(line)
        if withheld:
            kept.append(
                "  [withheld %d line(s) that name a path matching the restricted secret path globs]" % withheld
            )
        return "\n".join(kept)


__all__ = [
    "LaneEntry", "RestrictedRun", "admit", "decide", "entering", "entry", "permitted",
    "suppressed", "suppressing",
]
