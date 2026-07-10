"""File-backed standing instructions for trilobite.

The profile is intentionally plain Markdown so a user can edit it while the
server/proxy/REPL is running. server._build_system reads it on each request, so
changes become active without a restart.
"""
import os


DEFAULT_TEXT = """# Trilobite standing instructions

- Be direct, concrete, and honest about local-model limits.
- Prefer working code and verifiable steps.
- Use local privacy as a strength: keep sensitive context on this machine.
- For concrete workspace tasks, inspect with guarded tools, keep a visible
  checklist, make the requested change, run a grounded validation, and finish
  with changed paths, checks, failures, and an exact observable action log.
"""


def workspace_root():
    return os.path.abspath(os.path.dirname(__file__))


def default_path():
    return os.environ.get(
        "TRILOBITE_SYSTEM_PROFILE",
        os.path.join(workspace_root(), "system_profile.md"),
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
        raise ValueError("profile path must stay inside workspace: %r" % path)
    return path


def read_profile(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def ensure_profile(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_TEXT)
    return read_profile(path), path


def write_profile(text, path=None):
    path = _resolve_path(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write((text or "").rstrip() + "\n")
    return path


def append_profile(text, path=None):
    current = read_profile(path)
    addition = (text or "").strip()
    if not addition:
        raise ValueError("profile text is empty")
    combined = "%s\n\n%s" % (current, addition) if current else addition
    return write_profile(combined, path)


def system_prompt():
    text = read_profile()
    if not text:
        return ""
    return "Standing instructions from system_profile.md:\n%s" % text
