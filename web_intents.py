"""Conservative chat routing for public web and weather requests."""
from __future__ import annotations

import re
import time


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
# Implicit-recency event/result questions ("who won the super bowl?"). These
# carry no explicit "latest news" wording, so the base _CURRENT_INFO regex
# misses them and the offline model confidently hallucinates a stale answer.
# Precision-first: the left side must be a result/incumbent question shape and
# the right side must be a recurring named event or a recency superlative;
# _mentions_past_year() suppresses explicitly historical questions.
_EVENT_RESULT = re.compile(
    r"\b(?:who\s+won|who\s+wins|winner\s+of|"
    r"who\s+is\s+the\s+(?:current|new|reigning))\b"
    r"[^\n]{0,60}?"
    r"\b(?:super\s*bowl|world\s+cup|world\s+series|olympics?|election|"
    r"finals|championship|stanley\s+cup|grand\s+prix|"
    r"most\s+recent|latest|this\s+(?:year|season))\b",
    re.IGNORECASE,
)
# Generic interrogative + recency superlative ("what is the most recent /
# latest X?", "who ... as of today?").
_RECENCY_QUESTION = re.compile(
    r"\b(?:who|what|when|which|where)(?:['’]s)?\b[^\n]{0,80}?"
    r"\b(?:most\s+recent(?:ly)?|latest|as\s+of\s+(?:now|today)|right\s+now)\b",
    re.IGNORECASE,
)
_USE_WEB_ACTION = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?"
    r"use\s+(?:the\s+)?(?:web|internet|browser)(?:\s+tools?)?\s+to\s+"
    r"(?:tell|answer|get|find|check|verify|look\s+up|learn|research|show|"
    r"give|fetch|open|summarize|compare|explain|identify|locate)\b",
    re.IGNORECASE,
)
_LIVE_ROLE = (
    r"(?:president|prime\s+minister|premier|chancellor|governor|mayor|"
    r"ceo|chief\s+executive(?:\s+officer)?|"
    r"chair(?:person|man|woman)?|commissioner|head\s+coach)"
)
_INCUMBENT_OF = re.compile(
    r"^\s*(?:who(?:['’]s|\s+is)\s+)?(?:the\s+)?"
    r"(?:(?:current|new|acting|interim)\s+)?"
    r"(?P<role>%s)\s+(?:of|for|at)\s+"
    r"(?P<subject>[^?\n]{2,100}?)\s*[?!.]*$" % _LIVE_ROLE,
    re.IGNORECASE,
)
_INCUMBENT_POSSESSIVE = re.compile(
    r"^\s*who(?:['’]s|\s+is)\s+(?:the\s+)?"
    r"(?P<subject>[^?\n]{2,100}?)['’]s\s+"
    r"(?:(?:current|new|acting|interim)\s+)?"
    r"(?P<role>%s)\s*[?!.]*$" % _LIVE_ROLE,
    re.IGNORECASE,
)
_PRICE_OF = re.compile(
    r"^\s*(?:(?:what(?:['’]s|\s+is)|tell\s+me)\s+)?(?:the\s+)?"
    r"(?:(?:current|latest|spot|market)\s+)?"
    r"(?P<metric>stock\s+price|share\s+price|price|quote|value|market\s+cap)"
    r"\s+(?:of|for)\s+(?P<subject>[^?\n]{2,100}?)\s*[?!.]*$",
    re.IGNORECASE,
)
_PRICE_POSSESSIVE = re.compile(
    r"^\s*(?:(?:what(?:['’]s|\s+is))\s+)?"
    r"(?P<subject>[A-Za-z0-9][^?\n]{1,80}?)['’]s\s+"
    r"(?P<metric>stock\s+price|share\s+price|price|quote|market\s+cap)"
    r"\s*[?!.]*$",
    re.IGNORECASE,
)
_PRICE_TAIL = re.compile(
    r"^\s*(?:(?:what(?:['’]s|\s+is))\s+(?:the\s+)?)?"
    r"(?P<subject>[A-Za-z0-9][^?\n]{1,80}?)\s+"
    r"(?P<metric>stock\s+price|share\s+price|price|quote)\s*[?!.]*$",
    re.IGNORECASE,
)
_WORTH = re.compile(
    r"^\s*how\s+much\s+(?:is|are)\s+"
    r"(?P<subject>[^?\n]{2,100}?)\s+worth\s*[?!.]*$",
    re.IGNORECASE,
)
_EXCHANGE_RATE = re.compile(
    r"^\s*(?:(?:what(?:['’]s|\s+is)|tell\s+me)\s+)?(?:the\s+)?"
    r"(?P<subject>(?:[A-Z]{3}|[A-Za-z]{3,20})\s*(?:/|to)\s*"
    r"(?:[A-Z]{3}|[A-Za-z]{3,20}))\s+exchange\s+rate\s*[?!.]*$",
    re.IGNORECASE,
)
_UPCOMING_EVENT = re.compile(
    r"^\s*(?:(?:when|where|what(?:\s+date|\s+time)?)(?:['’]s|\s+is)\s+)?"
    r"(?:the\s+)?(?:next|upcoming)\s+"
    r"(?P<subject>[^?\n]{1,70}?)\s+"
    r"(?P<event>race|grand\s+prix|game|match|election|"
    r"release|launch|episode|concert)\s*[?!.]*$",
    re.IGNORECASE,
)
_STANDINGS = re.compile(
    r"^\s*(?:what(?:['’]s|\s+are)|show\s+me)\s+(?:the\s+)?"
    r"(?:(?:current|latest)\s+)?(?P<subject>[^?\n]{2,80}?)\s+"
    r"standings\s*[?!.]*$",
    re.IGNORECASE,
)
_VERSION_SUBJECT_FIRST = re.compile(
    r"\b(?:latest|current|newest|most\s+recent)\s+"
    r"(?P<subject>(?:\.[A-Za-z]|[A-Za-z0-9])[^?\n]{0,70}?)\s+version\b",
    re.IGNORECASE,
)
_VERSION_VERSION_FIRST = re.compile(
    r"\b(?:latest|current|newest|most\s+recent)\s+version\s+"
    r"(?:of|for)\s+(?P<subject>[^?\n]{2,80}?)\s*[?!.]*$",
    re.IGNORECASE,
)
_FOLLOWUP_SUBJECT = re.compile(
    r"^\s*(?:(?:and|then)\s+(?:what\s+about\s+)?|"
    r"(?:what|how)\s+about\s+)"
    r"(?P<subject>(?:\.[A-Za-z]|[A-Za-z0-9])"
    r"[A-Za-z0-9 .+#&/'’()-]{0,79}?)"
    r"\s*[?!.]*$",
    re.IGNORECASE,
)
_STATIC_OR_META = re.compile(
    r"^\s*(?:please\s+)?(?:translate|rewrite|paraphrase|quote|parse|"
    r"classify|tokenize)\b|"
    r"^\s*(?:please\s+)?(?:explain|analyze|discuss)\b[^\n]*"
    r"(?:[\"“][^\n]*[\"”]|[\s(]'[^'\n]{3,}'(?=$|[\s).,?!]))|"
    r"^\s*(?:please\s+)?(?:explain|analyze|discuss)\b[^\n]{0,50}"
    r"\b(?:phrase|term|wording|question)\b[^\n]{0,80}"
    r"\b(?:ambiguous|means?|worded|matches?)\b|"
    r"^\s*how\s+(?:do|does)\b[^\n]{0,100}\b"
    r"(?:decide|determine|choose|select|work|calculate|compute)\b|"
    r"^\s*(?:do|does|can|could|would)\s+(?:this|that|the)\s+"
    r"(?:regex|pattern|sentence|phrase|query|prompt)\b|"
    r"^\s*(?:is|are)\s+(?:the\s+)?(?:phrase|term|wording|question|"
    r"sentence)\b",
    re.IGNORECASE,
)
_WORK_LEAD = re.compile(
    r"^\s*(?:(?:please|can|could|would|will)\s+(?:you\s+)?)?"
    r"(?:write|create|build|implement|fix|update|edit|refactor|debug|"
    r"test|design|document|code|add|install|download|upgrade|configure|"
    r"pin|migrate|use|make)\b",
    re.IGNORECASE,
)
_DECLARATIVE_WORK = re.compile(
    r"^\s*(?:a|an|this|that|the|my|our)\s+"
    r"(?:app|website|page|dashboard|ui|component|readme|file|report|docs|"
    r"documentation|tool|feature|script|service|program|code|widget)\b"
    r"[^\n]{0,100}\b(?:should|must|needs?\s+to|will)\b",
    re.IGNORECASE,
)
_NEGATED_LIVE = re.compile(
    r"^\s*(?:please\s+)?(?:do\s+not|don't|never)\b|"
    r"^\s*i\s+(?:am|['’]m)\s+not\s+(?:asking|looking)\b|"
    r"\bwithout\s+(?:checking|searching|browsing|looking\s+up)\b",
    re.IGNORECASE,
)
_HYPOTHETICAL = re.compile(
    r"^\s*(?:assume|suppose|imagine|hypothetically|given\s+that|"
    r"in\s+(?:this|a)\s+(?:story|scenario|hypothetical))\b",
    re.IGNORECASE,
)
_HISTORICAL_WORDING = re.compile(
    r"^\s*(?:who|what|when|which|where|how\s+much)\s+"
    r"(?:was|were|did)\b|"
    r"\b(?:former|previous|historical|history\s+of|back\s+in|"
    r"at\s+the\s+time)\b",
    re.IGNORECASE,
)
_PRIVATE_CONTEXT = re.compile(
    r"(?:^|\s)[A-Za-z]:[\\/]|(?:^|\s)[~/][^\s]+|"
    r"\b(?:repo|repository|workspace|codebase|src\s+directory|folder|"
    r"private\s+project|local\s+project|proprietary|confidential|"
    r"pyproject\.toml|package\.json|requirements(?:-[\w.-]+)?\.txt|"
    r"app\.config|cargo\.toml|cmakelists\.txt|[\w.-]+\.(?:csproj|vcxproj))\b|"
    r"\b(?:my|our|your|private|local)\s+(?:[A-Za-z-]+\s+){0,2}"
    r"(?:calendar|meeting|flight|club|team|group|project|version|app|"
    r"startup|company|organization|association|tool|car|device)\b",
    re.IGNORECASE,
)
_INTERNAL_LOCAL_CONTEXT = re.compile(
    r"\binternal\s+(?:app|tool|service|project|package|repo|repository|system)\b",
    re.IGNORECASE,
)
_NONPUBLIC_SUBJECT = re.compile(
    r"\b(?:my|our|your|private|local)\b|"
    r"\b(?:repo|repository|workspace|codebase|src|directory|folder|file|"
    r"calendar)\b|\b(?:chess\s+)?(?:club|committee|group)\b",
    re.IGNORECASE,
)
_TECHNICAL_UPCOMING_SUBJECT = re.compile(
    r"\b(?:regex|iterator|state\s+machine|algorithm|button|branch|selector|"
    r"sender|pytest)\b",
    re.IGNORECASE,
)
_WEATHER_CORE = re.compile(
    r"\b(?:weather|forecast|temperature|humidity|wind\s+chill)\b",
    re.IGNORECASE,
)
_PRECIPITATION_META = re.compile(
    r"^\s*who\s+(?:sings|wrote|created|directed)\b|"
    r"^\s*what\s+is\b[^\n]{0,100}\babout\b|"
    r"\b(?:song|movie|film|book|album|poem|band)\b|"
    r"[\"']\s*[^\n]{2,80}\b(?:rain|raining|snow|snowing)\b[^\n]{0,80}"
    r"[\"']?[^\n]{0,30}\bmean(?:s|ing)?\b",
    re.IGNORECASE,
)
_PRECIPITATION = re.compile(
    r"^\s*(?:raining|snowing)\b(?:[?!.]|\s+(?:in|near|around|today|"
    r"tomorrow|tonight))|"
    r"\b(?:it\s+is|it's|currently)\s+(?:raining|snowing)\b|"
    r"\b(?:will|did|does)\s+(?:it\s+)?(?:rain|snow)\b|"
    r"\b(?:is|was)\s+it\s+(?:raining|snowing)\b|"
    r"\b(?:is|was|will)\s+there\s+(?:be\s+)?(?:any\s+)?(?:rain|snow)\b|"
    r"\bchance\s+of\s+(?:rain|snow)\b|"
    r"^\s*(?:what|how)\s+about\s+(?:rain|snow)(?:fall|ing)?\b"
    r"(?:\s+accumulation\b)?(?:\s*[?!.]\s*$|\s+(?:in|near|around|for|"
    r"today|tomorrow|tonight|this\s+weekend)\b)|"
    r"^\s*how\s+much\s+(?:rain|snow)(?:fall)?\b"
    r"(?:\s*[?!.]\s*$|\s+(?:will|did|does|is|was|fell|falls?|has|in|near|"
    r"for|today|tomorrow|tonight)\b)|"
    r"^\s*(?:any\s+)?(?:rain|snow)\s+(?:in|near|around|for)\b|"
    r"\b(?:rain|snow)\s+(?:today|tomorrow|tonight|this\s+weekend)\b|"
    r"\b(?:rain|snow)(?:fall)?\s+(?:forecast|expected|chance)\b",
    re.IGNORECASE,
)
_MARKET_ASSET = re.compile(
    r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|crypto(?:currency)?|"
    r"stock|share|gold|silver|platinum|oil|brent|wti|natural\s+gas|"
    r"nasdaq|s&p|dow\s+jones)\b",
    re.IGNORECASE,
)
_PRICE_FRESHNESS = re.compile(
    r"\b(?:current|latest|spot|today|now|right\s+now)\b",
    re.IGNORECASE,
)
_EXPLICIT_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
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
# Post-hoc guard: a generated reply that wrongly claims the assistant has no
# internet/web/real-time access. Deliberately narrow (see denies_web_access);
# phrases like "web tools are disabled" intentionally do NOT match.
_WEB_DENIAL = re.compile(
    r"\bas an ai(?: language)? model\b"
    r"|\b(?:do(?:es)?(?:n['’]t| not)|can(?:['’]t|not)|unable to)"
    r"(?:\s+\w+){0,2}?\s+(?:have|access|browse|reach|use|connect to|"
    r"perform|conduct|do|run|execute|carry out)"
    r"(?:\s+\w+){0,3}?\s+(?:internet|web|real[-\s]?time)\b"
    r"|\bno\s+(?:direct\s+)?(?:internet|web)\s+access\b",
    re.IGNORECASE,
)
# Explicit imperative search request ("search the web for X", "look it up
# online", "google it"). Used to override the classify_work gate in the
# pre-model chat routing: an explicit web-search order is never a workspace
# task even when it also parses as work ("search ... for ..." looks like a
# repo search to intents.classify_work).
_EXPLICIT_SEARCH = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:"
    r"(?:search|scour|query)\s+(?:the\s+)?(?:web|internet|net|online)\b|"
    r"(?:browse|check|find|look(?:ing)?(?:\s+\w+){0,2}\s+up)\b[^\n]{0,60}"
    r"\b(?:online|on\s+the\s+(?:web|internet))\b|"
    r"google\s+(?:it|that|this|for|the\s+web)\b|"
    r"open\s+https?://|"
    r"(?:(?:do|run|perform|use)\s+(?:a\s+)?)?"
    r"(?:web|internet|online)\s+search\s+"
    r"(?:for|about|to\s+(?:find|check|verify|answer|learn))\b)",
    re.IGNORECASE,
)
_SEARCH_THEN_WORK = re.compile(
    r"(?:\b(?:and|then|before|after|afterward|afterwards)\s+|"
    r"[.!?;,&]\s*|\r?\n+\s*|"
    r"\bso\s+(?:that\s+)?(?:you|we|i)\s+(?:can|could|will)\s+|"
    r"\b(?:find|check|research|look\s+up)\b[^\n.!?]{1,80}"
    r"\b(?:in\s+order\s+)?to\s+|\b(?:results?|api)\s+to\s+)"
    r"(?:please\s+)?(?:use\s+(?:what\s+you\s+find|(?:the\s+)?results?)"
    r"\s+to\s+)?"
    r"(?:build(?:ing)?|creat(?:e|ing)|implement(?:ing)?|updat(?:e|ing)|"
    r"edit(?:ing)?|writ(?:e|ing)|modif(?:y|ying)|fix(?:ing)?|add(?:ing)?|"
    r"install(?:ing)?|configur(?:e|ing)|refactor(?:ing)?|chang(?:e|ing)|"
    r"commit(?:ting)?|sav(?:e|ing)|delet(?:e|ing)|remov(?:e|ing)|"
    r"renam(?:e|ing)|mov(?:e|ing)|run(?:ning)?|execut(?:e|ing)|"
    r"test(?:ing)?|verif(?:y|ying)|deploy(?:ing)?|publish(?:ing)?|"
    r"upload(?:ing)?|push(?:ing)?|patch(?:ing)?|repair(?:ing)?|"
    r"apply(?:ing)?|mak(?:e|ing)\s+changes?)\b",
    re.IGNORECASE,
)
_EXPLICIT_SEARCH_LEAD = re.compile(
    r"^\s*(?:please\s+)?(?:search|browse|check|look\s+up|google)\b|"
    r"^\s*(?:please\s+)?(?:(?:do|run|perform|use)\s+(?:a\s+)?)?"
    r"(?:web|internet|online)\s+search\b|"
    r"^\s*(?:please\s+)?use\s+(?:the\s+)?(?:web|internet|browser)\b",
    re.IGNORECASE,
)
_PATH_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"[A-Za-z]:[\\/]|"
    r"\\\\[^\\\s]+[\\/]|"
    r"\.{1,2}[\\/]|"
    r"~[\\/]|"
    r"\$[A-Za-z_][A-Za-z0-9_]*[\\/]|"
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}[\\/]|"
    r"%[A-Za-z_][A-Za-z0-9_]*%[\\/]|"
    r"/(?:home|Users|etc|var|opt|srv|tmp)/|"
    r"(?<![A-Za-z0-9:/\\])(?:[A-Za-z0-9_.-]+[\\/])+"
    r"[A-Za-z0-9_.-]+"
    r")",
)
_LOCAL_MANIFEST_REFERENCE = re.compile(
    r"\b(?:my|our|your|private|local|internal)\s+"
    r"(?:\.[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\."
    r"(?:toml|xml|json|ya?ml|ini|cfg|config|env|txt|lock))\b",
    re.IGNORECASE,
)
_MANIFEST_REFERENCE = re.compile(
    r"\b(?:in|from|inside|within|according\s+to|"
    r"(?:defined|specified|listed|recorded)\s+(?:in|by))\s+"
    r"(?:(?:my|our|your|this|the)\s+)?(?:"
    r"\.[A-Za-z0-9_.-]+|go\.(?:mod|sum)|dockerfile|makefile|gemfile|"
    r"jenkinsfile|build(?:\.bazel)?|workspace(?:\.bazel)?|"
    r"version(?:\.[A-Za-z0-9_.-]+)?|"
    r"(?:build|settings)\.gradle(?:\.kts)?|gradle\.properties|"
    r"[A-Za-z0-9_.-]+\.(?:toml|xml|json|ya?ml|ini|cfg|config|env|txt|"
    r"lock|mod|sum|gradle|properties|bazel))\b",
    re.IGNORECASE,
)
_BARE_MANIFEST_REFERENCE = re.compile(
    r"^\s*(?:\.(?:env|tool-versions|python-version|nvmrc)|"
    r"go\.(?:mod|sum)|dockerfile|makefile|"
    r"gemfile|jenkinsfile|build(?:\.bazel)?|workspace(?:\.bazel)?|"
    r"version(?:\.[A-Za-z0-9_.-]+)?|"
    r"(?:build|settings)\.gradle(?:\.kts)?|gradle\.properties|"
    r"[A-Za-z0-9_.-]+\.(?:toml|xml|json|ya?ml|ini|cfg|config|env|txt|"
    r"lock|mod|sum|gradle|properties|bazel))\s*$",
    re.IGNORECASE,
)
_DEMONSTRATIVE_LOCAL_CONTEXT = re.compile(
    r"\b(?:in|from|inside|within)\s+(?:(?:this|the)\s+)?"
    r"(?:project|app|application|code|json|configuration|config|manifest|"
    r"workspace|repo|repository)\b|\bconfigured\s+here\b",
    re.IGNORECASE,
)
_POSSESSIVE_PRIVATE = re.compile(r"\b(?:my|our|your)\b", re.IGNORECASE)


def _mentions_past_year(text: str) -> bool:
    """True when the prompt names an explicit year before the current one.

    Keeps the implicit-recency rules precision-first: "who won the world cup
    in 1998" is a historical question the offline model can answer."""
    try:
        current_year = time.localtime().tm_year
    except Exception:
        current_year = 2026
    for match in _EXPLICIT_YEAR.finditer(str(text or "")):
        if int(match.group(1)) < current_year:
            return True
    return False


def _non_live_frame(text: str) -> bool:
    """Reject quoted/meta, hypothetical, negated, and implementation requests."""
    static_or_meta = bool(_STATIC_OR_META.search(text))
    # "How can I use the web?" is a genuine capability question, not a static
    # explainer frame. Preserve that existing route while still rejecting
    # explanatory current-fact/meta prompts.
    if static_or_meta and _CAPABILITY.search(text):
        static_or_meta = False
    work_frame = bool(_WORK_LEAD.search(text))
    if work_frame and (
        explicit_search(text) or _USE_WEB_ACTION.search(text)
    ):
        work_frame = False
    return bool(
        static_or_meta
        or work_frame
        or _DECLARATIVE_WORK.search(text)
        or _SEARCH_THEN_WORK.search(text)
        or _NEGATED_LIVE.search(text)
        or _HYPOTHETICAL.search(text)
    )


def _public_slash_subject(subject: str, family: str) -> bool:
    """Allow one strongly typed public slash-name without weakening paths."""
    if subject.count("/") != 1 or "\\" in subject:
        return False
    if family == "upcoming":
        return bool(re.fullmatch(
            r"[A-Z0-9][A-Z0-9&.'+-]{1,24}/[A-Z0-9][A-Z0-9&.'+-]{1,24}",
            subject,
        ))
    if family == "standings":
        return bool(re.fullmatch(
            r"(?:19|20)\d{2}/\d{2}\s+[A-Za-z0-9 .&'-]{2,60}", subject,
        ))
    if family == "version":
        return bool(re.fullmatch(
            r"(?:ASP\.NET|\.NET|Node\.js|React|Angular|Vue|Kotlin|Java|"
            r"Python|C\+\+|C#)/[A-Za-z0-9.+#-]{1,40}",
            subject,
        ))
    return False


def _clean_live_subject(value: str, family: str = "") -> str:
    subject = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n?!,;:")
    subject = re.sub(
        r"\s+(?:as\s+of\s+(?:now|today)|right\s+now|today|currently|now)$",
        "",
        subject,
        flags=re.IGNORECASE,
    ).strip(" \t\r\n?!,;:")
    if not 2 <= len(subject) <= 100 or "\n" in subject:
        return ""
    lowered = subject.casefold()
    if lowered in {"a", "an", "the"}:
        return ""
    if lowered.startswith(("what ", "what's ", "whats ", "who ", "when ", "where ", "how ")):
        return ""
    if lowered.startswith((
        "my ", "our ", "your ", "this ", "that ", "local ",
        "the repo", "the repository", "calendar ",
    )):
        return ""
    if _NONPUBLIC_SUBJECT.search(subject):
        return ""
    if (
        _PRIVATE_CONTEXT.search(subject)
        or _INTERNAL_LOCAL_CONTEXT.search(subject)
        or _DEMONSTRATIVE_LOCAL_CONTEXT.search(subject)
        or _BARE_MANIFEST_REFERENCE.match(subject)
    ):
        return ""
    if (
        (_PATH_REFERENCE.search(subject) and not _public_slash_subject(subject, family))
        or _LOCAL_MANIFEST_REFERENCE.search(subject)
        or _MANIFEST_REFERENCE.search(subject)
    ):
        return ""
    return subject


def _market_price_request(text: str, subject: str, metric: str) -> bool:
    """Allow implicit prices only when market semantics are unambiguous."""
    metric = str(metric or "").casefold()
    if metric in {"stock price", "share price", "quote", "market cap"}:
        return True
    if _PRICE_FRESHNESS.search(text) or _MARKET_ASSET.search(subject):
        return True
    return bool(re.fullmatch(r"\$?[A-Z]{2,6}", subject.strip()))


def _topic(family, query, subject="", **extra):
    result = {"family": family, "query": query}
    if subject:
        result["subject"] = subject
    result.update(extra)
    return result


def _direct_current_topic(text: str) -> dict | None:
    """Parse a whole-turn live fact request into typed, standalone metadata."""
    text = str(text or "").strip()
    if not text or _non_live_frame(text):
        return None
    exchange_match = _EXCHANGE_RATE.match(text)
    if exchange_match:
        subject = re.sub(
            r"\s+", " ", exchange_match.group("subject")
        ).strip(" \t\r\n?!,;:")
        return _topic(
            "exchange-rate", "What is the current %s exchange rate?" % subject,
            subject, fresh=True,
        )
    if (
        _PRIVATE_CONTEXT.search(text)
        or _INTERNAL_LOCAL_CONTEXT.search(text)
        or _DEMONSTRATIVE_LOCAL_CONTEXT.search(text)
        or _LOCAL_MANIFEST_REFERENCE.search(text)
        or _MANIFEST_REFERENCE.search(text)
        or _POSSESSIVE_PRIVATE.search(text)
    ):
        return None
    explicitly_fresh = bool(
        _CURRENT_INFO.search(text) or _RECENCY_QUESTION.search(text)
    )
    if (
        (_mentions_past_year(text) or _HISTORICAL_WORDING.search(text))
        and not explicitly_fresh
    ):
        return None

    for pattern in (_INCUMBENT_POSSESSIVE, _INCUMBENT_OF):
        match = pattern.match(text)
        if match:
            subject = _clean_live_subject(match.group("subject"))
            role = re.sub(r"\s+", " ", match.group("role")).strip()
            if not subject:
                return None
            query = "Who is the current %s of %s?" % (role, subject)
            if _CURRENT_INFO.search(text):
                query = text
            return _topic(
                "incumbent", query, subject, role=role, fresh=True,
            )

    for pattern in (_PRICE_OF, _PRICE_POSSESSIVE, _PRICE_TAIL):
        match = pattern.match(text)
        if match:
            subject = _clean_live_subject(match.group("subject"))
            metric = re.sub(r"\s+", " ", match.group("metric")).strip()
            if not subject or not _market_price_request(text, subject, metric):
                return None
            query = "What is the current %s of %s?" % (metric, subject)
            if _CURRENT_INFO.search(text):
                query = text
            return _topic(
                "price", query, subject, metric=metric, fresh=True,
            )

    match = _WORTH.match(text)
    if match:
        subject = _clean_live_subject(match.group("subject"))
        if not subject or not _market_price_request(text, subject, "value"):
            return None
        return _topic(
            "price", "What is the current value of %s?" % subject,
            subject, metric="value", fresh=True,
        )

    match = _UPCOMING_EVENT.match(text)
    if match:
        subject = _clean_live_subject(match.group("subject"), family="upcoming")
        event = re.sub(r"\s+", " ", match.group("event")).strip()
        if not subject or _TECHNICAL_UPCOMING_SUBJECT.search(subject):
            return None
        return _topic(
            "upcoming", "When is the next %s %s?" % (subject, event),
            subject, event=event, fresh=True,
        )

    match = _STANDINGS.match(text)
    if match:
        subject = _clean_live_subject(match.group("subject"), family="standings")
        if not subject:
            return None
        return _topic(
            "standings", "What are the current %s standings?" % subject,
            subject, fresh=True,
        )

    for pattern in (_VERSION_SUBJECT_FIRST, _VERSION_VERSION_FIRST):
        match = pattern.search(text)
        if match:
            subject = _clean_live_subject(match.group("subject"), family="version")
            if not subject:
                return None
            # Keep existing explicit-recency prompts byte-for-byte for
            # compatibility; the family metadata is what follow-ups need.
            return _topic("version", text, subject, fresh=True)

    if _PATH_REFERENCE.search(text) or _BARE_MANIFEST_REFERENCE.match(text):
        return None
    if _CURRENT_INFO.search(text):
        return _topic("generic", text, fresh=True)
    if _EVENT_RESULT.search(text) or _RECENCY_QUESTION.search(text):
        return _topic("generic", text, fresh=True)
    return None


def _current_info(text: str) -> bool:
    """Current-info signal shared by classify() and post-hoc guards."""
    return _direct_current_topic(text) is not None


def _content(message) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _role(message) -> str:
    if not isinstance(message, dict):
        return ""
    role = message.get("role")
    return str(role or "").strip().lower() if isinstance(role, str) else ""


def _history_messages(history, limit=8):
    if not isinstance(history, (list, tuple)):
        return []
    return [
        message for message in list(history)[-limit:]
        if isinstance(message, dict)
    ]


def _weather_signal(text: str) -> bool:
    """Recognize weather syntax, not titles/names containing rain or snow."""
    text = str(text or "")
    if _PRECIPITATION_META.search(text):
        return False
    return bool(_WEATHER_CORE.search(text) or _PRECIPITATION.search(text))


def _previous_completed_user_prompt(history) -> str:
    """Return only the user turn immediately completed by the last assistant turn."""
    messages = [
        message for message in _history_messages(history, limit=8)
        if _role(message) in ("user", "assistant") and _content(message).strip()
    ]
    if len(messages) < 2:
        return ""
    previous, completed = messages[-2:]
    if _role(previous) != "user" or _role(completed) != "assistant":
        return ""
    return _content(previous).strip()


def _followup_subject(text: str) -> str:
    match = _FOLLOWUP_SUBJECT.match(str(text or ""))
    if not match:
        return ""
    subject = _clean_live_subject(match.group("subject"))
    if not subject or len(subject.split()) > 6:
        return ""
    lowered = subject.casefold()
    if lowered in {
        "it", "that", "this", "same", "there", "then", "today",
        "tomorrow", "yesterday", "him", "her", "them", "he", "she",
    }:
        return ""
    if re.search(
        r"\b(?:history|geography|culture|syntax|architecture|documentation|"
        r"docs|api|source|code|implementation|meaning|definition)\b",
        lowered,
    ):
        return ""
    if _mentions_past_year(subject):
        return ""
    return subject


def _current_followup_topic(text: str, history) -> dict | None:
    subject = _followup_subject(text)
    if not subject:
        return None
    previous = _previous_completed_user_prompt(history)
    if not previous:
        return None
    context = _direct_current_topic(previous)
    if not context:
        return None
    family = context.get("family")
    if family == "version":
        query = "What is the current %s version?" % subject
    elif family == "incumbent":
        query = "Who is the current %s of %s?" % (context["role"], subject)
    elif family == "price":
        query = "What is the current %s of %s?" % (context["metric"], subject)
    elif family == "exchange-rate":
        query = "What is the current %s exchange rate?" % subject
    elif family == "upcoming":
        query = "When is the next %s %s?" % (subject, context["event"])
    elif family == "standings":
        query = "What are the current %s standings?" % subject
    else:
        return None
    return _topic(family, query, subject, fresh=True, inherited=True)


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
        r"^\s*how\s+much\s+(?:rain|snow)\s+will\s+(.{2,80}?)\s+"
        r"(?:get|receive|have)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return _clean_location(match.group(1))
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
    for message in _history_messages(history):
        if _weather_signal(_content(message)):
            return True
    return False


def _weather_unresolved(history) -> bool:
    """True while the thread has asked for weather but no forecast landed yet.

    A delivered forecast is an assistant turn starting a line with
    ``Weather for <place>``. Once one lands, the pending weather context is
    resolved and capability-style follow-ups must NOT re-run the lookup
    (a news-flavored capability prompt on a weather-primed thread was being
    misrouted into a stale weather re-fetch)."""
    asked = False
    for message in _history_messages(history):
        text = _content(message)
        role = _role(message)
        if role == "assistant":
            if re.search(r"^Weather for ", text, re.MULTILINE):
                asked = False
        elif _weather_signal(text):
            asked = True
    return asked


def _awaiting_location(history) -> bool:
    for message in reversed(_history_messages(history, limit=4)):
        if _role(message) != "assistant":
            continue
        text = _content(message).lower()
        return "city/state or zip" in text or "city or zip" in text
    return False


def _previous_weather_location(history) -> str:
    for message in reversed(_history_messages(history)):
        text = _content(message)
        if _role(message) == "assistant":
            match = re.search(r"^Weather for (.+)$", text, re.MULTILINE)
            if match:
                return _clean_location(match.group(1))
        elif _weather_signal(text):
            location = extract_weather_location(text)
            if location:
                return location
    return ""


def _plausible_location_reply(text: str) -> str:
    text = str(text or "").strip()
    if not 2 <= len(text) <= 120 or "\n" in text:
        return ""
    if _CAPABILITY.search(text) or _EXPLICIT_RESEARCH.search(text) or _weather_signal(text):
        return ""
    if re.fullmatch(r"[\w .,'-]+", text, re.UNICODE) is None:
        return ""
    return _clean_location(text)


def classify(prompt: str, history=None) -> dict | None:
    """Return weather/research/capability intent, or ``None`` for local chat."""
    text = str(prompt or "").strip()
    if not text:
        return None
    # Whole-turn framing outranks keywords found inside quotes, examples, code
    # requests, hypotheticals, or explicit instructions not to look anything up.
    if _non_live_frame(text):
        return None
    previous_weather = _recent_weather_context(history)
    if (
        explicit_search(text)
        or _EXPLICIT_RESEARCH.search(text)
        or _USE_WEB_ACTION.search(text)
    ):
        return {"kind": "research", "query": text}
    if _weather_signal(text):
        return {"kind": "weather", "location": extract_weather_location(text)}
    if _LOCATION_QUERY.search(text):
        return {"kind": "location"}
    # Capability phrasing only continues a weather thread while that weather
    # request is still unresolved AND the prompt carries no competing
    # current-info signal; a resolved forecast or a news-flavored prompt must
    # not re-trigger a stale weather lookup.
    if (
        previous_weather
        and _CAPABILITY.search(text)
        and _weather_unresolved(history)
        and not _current_info(text)
    ):
        return {"kind": "weather", "location": _previous_weather_location(history)}
    if _awaiting_location(history):
        location = _plausible_location_reply(text)
        if location:
            return {"kind": "weather", "location": location}
    if previous_weather and _WEATHER_FOLLOWUP.match(text):
        return {"kind": "weather", "location": _previous_weather_location(history)}
    if _LOCAL_QUERY.search(text):
        return {"kind": "research", "query": text, "needs_location": True}
    # Current-info outranks capability: "you have a web tool, use it to tell
    # me one current news headline" must attempt a live search, not return the
    # canned capability answer.
    current_topic = _direct_current_topic(text)
    if current_topic:
        return {"kind": "research", "query": current_topic["query"]}
    followup_topic = _current_followup_topic(text, history)
    if followup_topic:
        return {"kind": "research", "query": followup_topic["query"]}
    if _CAPABILITY.search(text):
        return {"kind": "capability"}
    return None


def explicit_search(prompt) -> bool:
    """True for an explicit imperative web search ("search the web for X",
    "look it up online", "google it"). Callers use this to bypass the
    classify_work gate: an explicit search order must reach the web routing
    even when the phrasing also classifies as a workspace task."""
    text = str(prompt or "")
    if _SEARCH_THEN_WORK.search(text):
        return False
    if _USE_WEB_ACTION.search(text):
        return True
    if _WORK_LEAD.search(text) and not _EXPLICIT_SEARCH_LEAD.search(text):
        return False
    return bool(_EXPLICIT_SEARCH.search(text) or _USE_WEB_ACTION.search(text))


def denies_web_access(reply) -> bool:
    """Return True when a model reply claims it lacks internet/web access.

    Used as a post-hoc safety net for denial phrasings the pre-model routing
    regexes miss. Kept narrow on purpose: callers must additionally require
    web_tools.enabled() and a positive classify() signal before re-dispatching,
    so honest "browsing is disabled" replies are never rewritten.
    """
    return bool(_WEB_DENIAL.search(str(reply or "")))


# A generated reply that FAKES tool usage instead of denying access: a fenced
# block invoking web_search/web_fetch as a pseudo-command or pseudo-call. The
# denial guard cannot catch these (nothing is denied), yet the reply is pure
# hallucination on a web-intent prompt.
_FENCED_BLOCK = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_PSEUDO_WEB_CALL = re.compile(
    r"^\s*(?:[$>]\s*)?(?:await\s+)?(?:web_search|web_fetch)\s*(?:\(|['\"]|\s+\S)",
    re.IGNORECASE,
)


def fabricated_tool_call(reply) -> bool:
    """True when a reply contains a fenced block that pretends to run the
    web_search/web_fetch tools as shell commands or function calls.

    Deliberately narrow: only fenced blocks count (prose that merely mentions
    the tool names never matches), and callers must additionally require
    web_tools.enabled() plus a positive classify() before re-dispatching, so a
    legitimate code answer that happens to call a user-defined web_search()
    function on a non-web prompt is never rewritten."""
    for _info, body in _FENCED_BLOCK.findall(str(reply or "")):
        for line in body.splitlines():
            if _PSEUDO_WEB_CALL.match(line):
                return True
    return False
