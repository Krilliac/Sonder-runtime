"""Conservative chat routing for public web and weather requests."""
from __future__ import annotations

import re


_WEATHER = re.compile(
    r"\b(?:weather|forecast|temperature|rain(?:ing)?|snow(?:ing)?|"
    r"humidity|wind chill)\b",
    re.IGNORECASE,
)
_CAPABILITY = re.compile(
    r"\b(?:can|could|do|don't|have|has|use|using|access|call)\b[^\n]{0,80}"
    r"\b(?:internet|web|online|browser|tools?)\b|"
    r"\b(?:internet|web|online|browser|tools?)\b[^\n]{0,80}"
    r"\b(?:access|available|call|enabled|use)\b",
    re.IGNORECASE,
)
_EXPLICIT_RESEARCH = re.compile(
    r"\b(?:search|browse|check|look\s+up|find)\b[^\n]{0,60}"
    r"\b(?:web|internet|online)\b|"
    r"\b(?:web|internet|online)\b[^\n]{0,60}"
    r"\b(?:search|browse|check|look\s+up|find)\b|"
    r"\bopen\s+https?://",
    re.IGNORECASE,
)
_CURRENT_INFO = re.compile(
    r"(?:\b(?:latest|breaking|today(?:'s)?|current|recent)\b[^\n]{0,60}"
    r"\b(?:news|price|score|standings|schedule|release|version|president|ceo)\b)|"
    r"(?:\b(?:news|price|score|standings|schedule|release|version|president|ceo)\b"
    r"[^\n]{0,60}\b(?:latest|today|current|recent|now)\b)",
    re.IGNORECASE,
)
_WEATHER_FOLLOWUP = re.compile(
    r"^\s*(?:what about\s+)?(?:today|tomorrow|tonight|this weekend|next week|"
    r"later|and tomorrow|how about tomorrow)[?!.\s]*$",
    re.IGNORECASE,
)
_LOCATION_QUERY = re.compile(
    r"\b(?:where\s+am\s+i|what(?:'s|\s+is)\s+my\s+location|"
    r"find\s+my\s+location|locate\s+me|what\s+city\s+am\s+i\s+in)\b",
    re.IGNORECASE,
)
_LOCAL_QUERY = re.compile(
    r"\b(?:near\s+me|nearby|in\s+my\s+area|around\s+me|local\s+to\s+me)\b",
    re.IGNORECASE,
)
_LOCATION_PLACEHOLDERS = {
    "here", "home", "local", "locally", "my area", "my city", "my location",
    "near me", "around me", "current location", "the area", "this area",
    "where i am", "where i'm at", "outside",
}
_TRAILING_TIME = re.compile(
    r"\s+\b(?:today|tomorrow|tonight|right now|currently|this week|"
    r"this weekend|next week)\b.*$",
    re.IGNORECASE,
)


def _content(message) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")


def _clean_location(value: str) -> str:
    location = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n?!.,;:")
    location = _TRAILING_TIME.sub("", location).strip(" \t\r\n?!.,;:")
    lowered = location.lower()
    if lowered.startswith("the ") and lowered not in _LOCATION_PLACEHOLDERS:
        location = location[4:].strip()
        lowered = location.lower()
    if (
        not 2 <= len(location) <= 120
        or lowered in _LOCATION_PLACEHOLDERS
        or any(ord(char) < 32 for char in location)
    ):
        return ""
    return location


def extract_weather_location(text: str) -> str:
    text = str(text or "").strip()
    postal = re.search(r"(?<!\d)\d{5}(?:-\d{4})?(?!\d)", text)
    if postal:
        return postal.group(0)
    match = re.search(
        r"\b(?:in|for|near|around)\s+(.+)$",
        text,
        re.IGNORECASE,
    )
    if match:
        return _clean_location(match.group(1))
    match = re.search(
        r"^\s*(.{2,80}?)\s+(?:weather|forecast|temperature)\b",
        text,
        re.IGNORECASE,
    )
    if match and not re.search(r"\b(?:what|how|tell|show|check|current)\b", match.group(1), re.I):
        return _clean_location(match.group(1))
    return ""


def _recent_weather_context(history) -> bool:
    for message in list(history or [])[-8:]:
        if _WEATHER.search(_content(message)):
            return True
    return False


def _awaiting_location(history) -> bool:
    for message in reversed(list(history or [])[-4:]):
        if str(message.get("role") or "").lower() != "assistant":
            continue
        text = _content(message).lower()
        return "city/state or zip" in text or "city or zip" in text
    return False


def _previous_weather_location(history) -> str:
    for message in reversed(list(history or [])[-8:]):
        text = _content(message)
        if str(message.get("role") or "").lower() == "assistant":
            match = re.search(r"^Weather for (.+)$", text, re.MULTILINE)
            if match:
                return _clean_location(match.group(1))
        elif _WEATHER.search(text):
            location = extract_weather_location(text)
            if location:
                return location
    return ""


def _plausible_location_reply(text: str) -> str:
    text = str(text or "").strip()
    if not 2 <= len(text) <= 120 or "\n" in text:
        return ""
    if _CAPABILITY.search(text) or _EXPLICIT_RESEARCH.search(text) or _WEATHER.search(text):
        return ""
    if re.fullmatch(r"[\w .,'-]+", text, re.UNICODE) is None:
        return ""
    return _clean_location(text)


def classify(prompt: str, history=None) -> dict | None:
    """Return weather/research/capability intent, or ``None`` for local chat."""
    text = str(prompt or "").strip()
    if not text:
        return None
    previous_weather = _recent_weather_context(history)
    if _EXPLICIT_RESEARCH.search(text):
        return {"kind": "research", "query": text}
    if _WEATHER.search(text):
        return {"kind": "weather", "location": extract_weather_location(text)}
    if _LOCATION_QUERY.search(text):
        return {"kind": "location"}
    if previous_weather and _CAPABILITY.search(text):
        return {"kind": "weather", "location": _previous_weather_location(history)}
    if _awaiting_location(history):
        location = _plausible_location_reply(text)
        if location:
            return {"kind": "weather", "location": location}
    if previous_weather and _WEATHER_FOLLOWUP.match(text):
        return {"kind": "weather", "location": _previous_weather_location(history)}
    if _CAPABILITY.search(text):
        return {"kind": "capability"}
    if _LOCAL_QUERY.search(text):
        return {"kind": "research", "query": text, "needs_location": True}
    if _CURRENT_INFO.search(text):
        return {"kind": "research", "query": text}
    return None
