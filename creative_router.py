"""Conservative routing for explicit greenfield game/artifact build requests."""
from __future__ import annotations

import hashlib
import re


_BUILD_ACTION = re.compile(
    r"\b(?:build|create|make|generate|implement|develop|forge|produce|scaffold)\b",
    re.IGNORECASE,
)
_GAME_TARGET = re.compile(
    r"\b(?:games?|rpg|rogueli(?:ke|te)|platformer|shooter|dungeon crawler|"
    r"adventure|puzzle game|simulator game|arcade game)\b",
    re.IGNORECASE,
)
_ARTIFACT_TARGET = re.compile(
    r"\b(?:assets?|logos?|icons?|images?|illustrations?|sounds?|audio|music|"
    r"models?|meshes?|textures?|sprites?|diagrams?|documents?|datasets?|"
    r"mockups?|scenes?|palettes?|characters?|avatars?|humanoids?|brand kit)\b",
    re.IGNORECASE,
)
_CAMPAIGN = re.compile(
    r"\b(?:fleet|swarm|campaign|suite|several|many|multiple|various|"
    r"across\s+(?:several\s+|multiple\s+|various\s+)?languages?)\b",
    re.IGNORECASE,
)
_QUESTION_ONLY = re.compile(
    r"^\s*(?:how|why|what|when|where|explain|describe|compare|should)\b",
    re.IGNORECASE,
)

_STOP_WORDS = {
    "a", "an", "and", "all", "anything", "build", "create", "develop",
    "everything", "for", "forge", "game", "games", "generate", "implement",
    "in", "language", "languages", "make", "of", "produce", "project",
    "scaffold", "the", "to", "using", "with", "without", "2d", "3d",
    "cpp", "csharp", "javascript", "python", "assets", "asset", "character",
    "characters", "avatar", "avatars", "humanoid", "humanoids",
}


def _dimension(text: str) -> str:
    lowered = text.lower()
    if re.search(r"\b2\s*[.]\s*5\s*d\b|\bisometric\b", lowered):
        return "2.5d"
    if re.search(r"\b3\s*d\b|\bthree[- ]dimensional\b", lowered):
        return "3d"
    if re.search(
        r"\b(?:humanoid|biped|rigged|skeletal|armature|glb|gltf|mesh)\b|"
        r"\bbone\s+rig\b|\banimation\s+clips?\b",
        lowered,
    ):
        return "3d"
    return "2d"


def _dimension_explicit(text: str) -> bool:
    return bool(re.search(
        r"\b(?:2\s*[.]\s*5\s*d|isometric|2\s*d|3\s*d|"
        r"two[- ]dimensional|three[- ]dimensional)\b",
        text,
        re.IGNORECASE,
    ))


def _detected_language(text: str) -> str:
    lowered = text.lower()
    if re.search(r"\bc\s*\+\s*\+|\bcpp\b", lowered):
        return "cpp"
    if (
        re.search(r"\bc\s*#|\bcsharp\b", lowered)
        or re.search(r"(?<!\w)[.]net\b", lowered)
    ):
        return "csharp"
    if re.search(r"\bjavascript\b|\bnode(?:[.]js)?\b|\bjs\b", lowered):
        return "javascript"
    if re.search(r"\bpython\b|\bpy\b", lowered):
        return "python"
    return ""


def _language(text: str) -> str:
    return _detected_language(text) or "python"


def _language_explicit(text: str) -> bool:
    return bool(_detected_language(text))


def _theme(text: str) -> str:
    lowered = text.lower()
    for theme, terms in {
        "frost": ("frost", "ice", "snow", "winter"),
        "verdant": ("verdant", "forest", "nature", "jungle"),
        "ember": ("ember", "fire", "lava", "infernal", "diablo"),
        "arcane": ("arcane", "magic", "mystic", "wizard"),
    }.items():
        if any(term in lowered for term in terms):
            return theme
    return "arcane"


def _name(text: str, prefix: str) -> str:
    words = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _STOP_WORDS or len(word) < 3:
            continue
        if word not in words:
            words.append(word)
        if len(words) == 4:
            break
    stem = "-".join(words) or prefix
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:7]
    return "%s-%s" % (stem[:48].strip("-"), digest)


def _campaign_total(text: str, default: int = 4) -> int:
    match = re.search(
        r"(?<![.\w])(\d{1,2})(?![.\w])(?=[^!?\n]{0,64}\bgames?\b)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return default
    return max(1, min(int(match.group(1)), 12))


def classify(task: str, mode: str = "") -> dict | None:
    """Return a grounded forge intent, or ``None`` for ordinary master work."""
    text = str(task or "").strip()
    if not text or not _BUILD_ACTION.search(text):
        return None
    if _QUESTION_ONLY.search(text):
        return None
    dimension = _dimension(text)
    theme = _theme(text)
    if _GAME_TARGET.search(text):
        campaign = bool(
            str(mode or "").lower() in ("fleet", "swarm", "fanout")
            or _CAMPAIGN.search(text)
        )
        return {
            "kind": "game_campaign" if campaign else "game",
            "name": _name(text, "game"),
            "concept": text,
            "language": _language(text),
            "dimension": dimension,
            "language_explicit": _language_explicit(text),
            "dimension_explicit": _dimension_explicit(text),
            "theme": theme,
            "total": _campaign_total(text, default=4),
        }
    if _ARTIFACT_TARGET.search(text):
        return {
            "kind": "artifact",
            "name": _name(text, "artifact"),
            "brief": text,
            "dimension": dimension,
            "theme": theme,
            "kinds": "auto",
        }
    return None
