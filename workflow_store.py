"""File-backed reusable workflows for the loop tool."""
import json
import os
import re


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


def workspace_root():
    return os.path.abspath(os.path.dirname(__file__))


def default_path():
    return os.environ.get(
        "TRILOBITE_WORKFLOWS",
        os.path.join(workspace_root(), "workflows.json"),
    )


def _resolve_path(path=None):
    path = path or default_path()
    if not os.path.isabs(path):
        path = os.path.join(workspace_root(), path)
    path = os.path.abspath(path)
    root = workspace_root()
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("workflow path must stay inside workspace: %r" % path)
    return path


def normalize_name(name):
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise ValueError("invalid workflow name: %r" % name)
    return name


def normalize_actions(actions):
    if not isinstance(actions, list) or not actions:
        raise ValueError("workflow actions must be a non-empty JSON list")
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("each workflow action must be a JSON object")
    return actions


def normalize_workflow(workflow):
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be a JSON object")
    return {
        "description": str(workflow.get("description", "") or ""),
        "actions": normalize_actions(workflow.get("actions")),
    }


def read_workflows(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("workflows file must contain a JSON object")
    workflows = {}
    for name, workflow in raw.items():
        workflows[normalize_name(name)] = normalize_workflow(workflow)
    return workflows


def write_workflows(workflows, path=None):
    path = _resolve_path(path)
    normalized = {}
    for name, workflow in (workflows or {}).items():
        normalized[normalize_name(name)] = normalize_workflow(workflow)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def ensure_workflows(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        write_workflows(DEFAULT_WORKFLOWS, path)
    return read_workflows(path), path


def save_workflow(name, actions, description="", path=None):
    name = normalize_name(name)
    workflows = read_workflows(path)
    workflows[name] = {
        "description": description or "",
        "actions": normalize_actions(actions),
    }
    path = write_workflows(workflows, path)
    return workflows[name], path


def delete_workflow(name, path=None):
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
        wf = workflows[name]
        desc = wf.get("description") or "(no description)"
        lines.append("- %s: %s [%d actions]" % (name, desc, len(wf.get("actions", []))))
    return "\n".join(lines)
