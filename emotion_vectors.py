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
    "empathy": 0.25,
    "encouragement": 0.20,
    "calm": 0.30,
    "patience": 0.20,
    "curiosity": 0.30,
    "creativity": 0.15,
    "initiative": 0.15,
    "adaptability": 0.15,
    "confidence": 0.20,
    "directness": 0.10,
    "precision": 0.25,
    "rigor": 0.20,
    "transparency": 0.25,
    "humility": 0.15,
    "playfulness": 0.10,
    "urgency": 0.00,
    "skepticism": 0.15,
    "brevity": 0.20,
}

DESCRIPTIONS = {
    "warmth": ("more emotionally warm and reassuring", "more neutral and spare"),
    "empathy": ("more attentive to the user's emotional state", "more task-only and detached"),
    "encouragement": ("more confidence-building and supportive", "more understated about praise"),
    "calm": ("steadier and more grounding", "more intense and energetic"),
    "patience": ("more patient and step-by-step", "more brisk and compressed"),
    "curiosity": ("more exploratory and question-aware", "more decisive and narrow"),
    "creativity": ("more imaginative and willing to explore alternatives", "more conventional and literal"),
    "initiative": ("more proactive about useful next steps", "more wait-for-instructions"),
    "adaptability": ("more responsive to the user's changing style", "more consistent and fixed-style"),
    "confidence": ("more assertive when evidence is strong", "more tentative and hedged"),
    "directness": ("more direct and plain-spoken", "more gentle and indirect"),
    "precision": ("more exact about details and constraints", "more broad-strokes"),
    "rigor": ("more careful, test-minded, and systematic", "more lightweight and informal"),
    "transparency": ("more explicit about actions, limits, and uncertainty", "more terse about process"),
    "humility": ("more willing to name uncertainty and defer to evidence", "more self-assured in tone"),
    "playfulness": ("lighter and more playful", "more formal and restrained"),
    "urgency": ("more brisk and action-oriented", "more patient and unhurried"),
    "skepticism": ("more careful about assumptions and risks", "more trusting and fluid"),
    "brevity": ("more concise", "more expansive"),
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_ASSIGN_RE = re.compile(r"([a-z][a-z0-9_-]{1,31})\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))", re.I)

TUNE_RULES = [
    ("warmth", 1, ("warmer", "more warm", "less cold", "less robotic", "friendlier", "more friendly")),
    ("warmth", -1, ("colder", "less warm", "more neutral", "more spare")),
    ("empathy", 1, ("more empathetic", "more empathy", "emotionally aware", "more caring")),
    ("empathy", -1, ("less empathetic", "less emotional", "more detached")),
    ("encouragement", 1, ("more encouraging", "more supportive", "build confidence", "more praise")),
    ("encouragement", -1, ("less praise", "less encouraging", "more understated")),
    ("calm", 1, ("calmer", "more calm", "steadier", "more grounding")),
    ("calm", -1, ("more energetic", "more intense", "less calm")),
    ("patience", 1, ("more patient", "slower", "step by step", "more explanatory")),
    ("patience", -1, ("less patient", "faster", "more brisk")),
    ("curiosity", 1, ("more curious", "ask more", "more exploratory")),
    ("curiosity", -1, ("less curious", "ask less", "more decisive")),
    ("creativity", 1, ("more creative", "more imaginative", "more ideas", "brainstorm")),
    ("creativity", -1, ("less creative", "more conventional", "more literal")),
    ("initiative", 1, ("more proactive", "take initiative", "keep going", "do more")),
    ("initiative", -1, ("less proactive", "wait for instructions", "ask first")),
    ("adaptability", 1, ("adapt more", "match my style", "more flexible")),
    ("adaptability", -1, ("less adaptive", "more consistent", "fixed style")),
    ("confidence", 1, ("more confident", "more assertive", "less hedging")),
    ("confidence", -1, ("less confident", "more tentative", "hedge more")),
    ("directness", 1, ("more direct", "be blunt", "plain spoken", "get to the point")),
    ("directness", -1, ("less direct", "more gentle", "softer")),
    ("precision", 1, ("more precise", "more exact", "more specific")),
    ("precision", -1, ("less precise", "broader", "broad strokes")),
    ("rigor", 1, ("more rigorous", "more careful", "test more", "verify more")),
    ("rigor", -1, ("less rigorous", "lighter", "less formal")),
    ("transparency", 1, ("more transparent", "show status", "explain what changed", "name uncertainty")),
    ("transparency", -1, ("less transparent", "less process", "less status")),
    ("humility", 1, ("more humble", "admit uncertainty", "defer to evidence")),
    ("humility", -1, ("less humble", "more self assured")),
    ("playfulness", 1, ("more playful", "more humor", "lighter")),
    ("playfulness", -1, ("less playful", "more formal", "more restrained")),
    ("urgency", 1, ("more urgent", "faster", "more action oriented")),
    ("urgency", -1, ("less urgent", "more unhurried", "slow down")),
    ("skepticism", 1, ("more skeptical", "more cautious", "challenge assumptions")),
    ("skepticism", -1, ("less skeptical", "more trusting")),
    ("brevity", 1, ("more concise", "shorter", "less verbose", "brief")),
    ("brevity", -1, ("less concise", "more detailed", "longer", "more expansive")),
]


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
    vectors = read_vectors(path)
    missing = {name: value for name, value in DEFAULT_VECTORS.items() if name not in vectors}
    if missing:
        vectors.update(missing)
        write_vectors(vectors, path)
        vectors = read_vectors(path)
    return vectors, path


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
    elif mode in ("defaults", "reset"):
        merged = dict(DEFAULT_VECTORS)
    elif mode == "merge":
        merged = read_vectors(path) or {}
        merged.update(updates)
    elif mode == "clear":
        merged = {}
    else:
        raise ValueError("unknown mode %r (use merge, replace, or clear)" % mode)
    path = write_vectors(merged, path)
    return read_vectors(path), path


def parse_assignments(text):
    """Parse `warmth=0.5 brevity=-0.2` style live vector updates."""
    updates = {}
    for name, raw in _ASSIGN_RE.findall(text or ""):
        updates[_normalize_name(name)] = _clamp(raw)
    return normalize_vectors(updates)


def tune_suggestions(text, step=0.1):
    """Return small vector deltas inferred from plain-language feedback."""
    text_l = " ".join((text or "").lower().split())
    try:
        step = abs(float(step))
    except (TypeError, ValueError):
        step = 0.1
    step = max(0.01, min(step, 0.25))
    deltas = {}
    matched = []
    for name, direction, phrases in TUNE_RULES:
        for phrase in phrases:
            if phrase in text_l:
                deltas[name] = deltas.get(name, 0.0) + (step * direction)
                matched.append("%s -> %s%0.2f" % (phrase, "+" if direction > 0 else "-", step))
                break
    return {name: round(value, 3) for name, value in deltas.items()}, matched


def tune_from_text(text, step=0.1, path=None):
    vectors, path = ensure_vectors(path)
    deltas, matched = tune_suggestions(text, step=step)
    explicit = parse_assignments(text)
    updated = dict(vectors)
    for name, delta in deltas.items():
        updated[name] = round(_clamp(updated.get(name, 0.0) + delta), 3)
    for name, value in explicit.items():
        updated[name] = value
    write_vectors(updated, path)
    return read_vectors(path), path, deltas, explicit, matched


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
