"""File-backed emotional steering vectors for trilobite.

These are behavioral tone controls, not claims about internal feelings. The
values are normalized to [-1.0, 1.0] and rendered into the system prompt so the
model can adjust warmth, confidence, curiosity, and similar response qualities.
"""
import json
import os
import re


DEFAULT_VECTORS = {
    "warmth": 0.35,
    "calm": 0.30,
    "curiosity": 0.30,
    "confidence": 0.20,
    "playfulness": 0.10,
    "urgency": 0.00,
    "skepticism": 0.15,
    "brevity": 0.20,
}

DESCRIPTIONS = {
    "warmth": ("more emotionally warm and reassuring", "more neutral and spare"),
    "calm": ("steadier and more grounding", "more intense and energetic"),
    "curiosity": ("more exploratory and question-aware", "more decisive and narrow"),
    "confidence": ("more assertive when evidence is strong", "more tentative and hedged"),
    "playfulness": ("lighter and more playful", "more formal and restrained"),
    "urgency": ("more brisk and action-oriented", "more patient and unhurried"),
    "skepticism": ("more careful about assumptions and risks", "more trusting and fluid"),
    "brevity": ("more concise", "more expansive"),
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def workspace_root():
    return os.path.abspath(os.path.dirname(__file__))


def default_path():
    return os.environ.get(
        "TRILOBITE_EMOTION_VECTORS",
        os.path.join(workspace_root(), "emotion_vectors.json"),
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
        raise ValueError("emotion vector path must stay inside workspace: %r" % path)
    return path


def _clamp(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("emotion vector values must be numbers")
    return max(-1.0, min(1.0, value))


def _normalize_name(name):
    name = (name or "").strip().lower().replace("-", "_")
    if not _NAME_RE.match(name):
        raise ValueError("invalid emotion vector name: %r" % name)
    return name


def normalize_vectors(vectors):
    if not isinstance(vectors, dict):
        raise ValueError("emotion vectors must be a JSON object")
    normalized = {}
    for name, value in vectors.items():
        normalized[_normalize_name(name)] = round(_clamp(value), 3)
    return normalized


def read_vectors(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return normalize_vectors(raw)


def ensure_vectors(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        write_vectors(DEFAULT_VECTORS, path)
    return read_vectors(path), path


def write_vectors(vectors, path=None):
    path = _resolve_path(path)
    normalized = normalize_vectors(vectors)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def update_vectors(updates, mode="merge", path=None):
    mode = (mode or "merge").strip().lower()
    updates = normalize_vectors(updates)
    if mode == "replace":
        merged = updates
    elif mode == "merge":
        merged = read_vectors(path) or {}
        merged.update(updates)
    elif mode == "clear":
        merged = {}
    else:
        raise ValueError("unknown mode %r (use merge, replace, or clear)" % mode)
    path = write_vectors(merged, path)
    return read_vectors(path), path


def describe_vector(name, value):
    pos, neg = DESCRIPTIONS.get(
        name,
        ("lean into %s" % name.replace("_", " "), "de-emphasize %s" % name.replace("_", " ")),
    )
    if value > 0:
        direction = pos
    elif value < 0:
        direction = neg
    else:
        direction = "neutral"
    return "%s=%+.2f: %s" % (name, value, direction)


def system_prompt():
    vectors = read_vectors()
    active = {name: value for name, value in vectors.items() if abs(value) >= 0.001}
    if not active:
        return ""
    lines = [
        "Emotion/tone vectors (behavioral steering, not internal feelings):",
    ]
    for name in sorted(active):
        lines.append("- " + describe_vector(name, active[name]))
    lines.append("Keep these vectors subordinate to correctness, safety, and the user's explicit instructions.")
    return "\n".join(lines)


def format_vectors(vectors):
    if not vectors:
        return "(none)"
    return "\n".join(
        "- " + describe_vector(name, vectors[name])
        for name in sorted(vectors)
    )
