"""File-backed reusable workflows behind the package adapter boundary."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path


DEFAULT_WORKFLOWS = {
    "status_sweep": {
        "description": "Check live reload, system profile, emotion vectors, and Ollama status.",
        "actions": [
            {"type": "diagnostics"},
            {"type": "self_heal_check"},
            {"type": "profile_status"},
            {"type": "emotion_status"},
            {"type": "status"},
        ],
    },
    "retry_python_check": {
        "description": "Template workflow: replace the code string, then run until success.",
        "actions": [
            {"type": "code", "language": "python", "code": "print('replace me')"},
        ],
    },
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
MAX_WORKFLOWS = 256
MAX_ACTIONS_PER_WORKFLOW = 100
MAX_WORKFLOW_BYTES = 1024 * 1024
MAX_DESCRIPTION_CHARS = 4000
_LOCK = threading.RLock()


def workspace_root():
    # Preserve the historical root-module storage location, not this package dir.
    return str(Path(__file__).resolve().parents[3])


def default_path():
    return os.environ.get(
        "SONDER_WORKFLOWS", os.path.join(workspace_root(), "workflows.json")
    )


def _resolve_path(path=None):
    path = path or default_path()
    if not os.path.isabs(path):
        path = os.path.join(workspace_root(), path)
    path = os.path.realpath(os.path.abspath(path))
    root = os.path.realpath(workspace_root())
    try:
        inside = os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("workflow path must stay inside workspace: %r" % path)
    return path


def normalize_name(name):
    if not isinstance(name, str):
        raise ValueError("invalid workflow name: %r" % name)
    name = name.strip().lower()
    if not _NAME_RE.match(name):
        raise ValueError("invalid workflow name: %r" % name)
    return name


def normalize_actions(actions):
    if not isinstance(actions, list) or not actions:
        raise ValueError("workflow actions must be a non-empty JSON list")
    if len(actions) > MAX_ACTIONS_PER_WORKFLOW:
        raise ValueError(
            "workflow actions exceed the %d-action limit"
            % MAX_ACTIONS_PER_WORKFLOW
        )
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("each workflow action must be a JSON object")
    try:
        encoded = json.dumps(actions, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("workflow actions must be JSON serializable") from exc
    if len(encoded) > MAX_WORKFLOW_BYTES:
        raise ValueError("workflow actions exceed the byte limit")
    return actions


def normalize_workflow(workflow):
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be a JSON object")
    description = str(workflow.get("description", "") or "")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError("workflow description exceeds the character limit")
    return {
        "description": description,
        "actions": normalize_actions(workflow.get("actions")),
    }


def read_workflows(path=None):
    path = _resolve_path(path)
    with _LOCK:
        if not os.path.exists(path):
            return {}
        if os.path.getsize(path) > MAX_WORKFLOW_BYTES:
            raise ValueError("workflows file exceeds the byte limit")
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("workflows file must contain a JSON object")
    if len(raw) > MAX_WORKFLOWS:
        raise ValueError("workflows file exceeds the workflow-count limit")
    workflows = {}
    for name, workflow in raw.items():
        workflows[normalize_name(name)] = normalize_workflow(workflow)
    return workflows


def write_workflows(workflows, path=None):
    path = _resolve_path(path)
    normalized = {}
    for name, workflow in (workflows or {}).items():
        normalized[normalize_name(name)] = normalize_workflow(workflow)
    if len(normalized) > MAX_WORKFLOWS:
        raise ValueError("workflows exceed the workflow-count limit")
    encoded = (
        json.dumps(normalized, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_WORKFLOW_BYTES:
        raise ValueError("workflows exceed the byte limit")
    parent = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent,
    )
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        with _LOCK:
            os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def ensure_workflows(path=None):
    path = _resolve_path(path)
    with _LOCK:
        if not os.path.exists(path):
            write_workflows(DEFAULT_WORKFLOWS, path)
        return read_workflows(path), path


def save_workflow(name, actions, description="", path=None):
    with _LOCK:
        name = normalize_name(name)
        workflows = read_workflows(path)
        workflows[name] = normalize_workflow({
            "description": description or "",
            "actions": actions,
        })
        path = write_workflows(workflows, path)
        return workflows[name], path


def delete_workflow(name, path=None):
    with _LOCK:
        name = normalize_name(name)
        workflows = read_workflows(path)
        existed = name in workflows
        workflows.pop(name, None)
        path = write_workflows(workflows, path)
        return existed, path


def get_workflow(name, path=None):
    workflows = read_workflows(path)
    return workflows.get(normalize_name(name))


def format_workflows(workflows):
    if not workflows:
        return "(none)"
    lines = []
    for name in sorted(workflows):
        workflow = workflows[name]
        description = workflow.get("description") or "(no description)"
        lines.append(
            "- %s: %s [%d actions]"
            % (name, description, len(workflow.get("actions", [])))
        )
    return "\n".join(lines)
