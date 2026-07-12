"""lesson_tagger — infer domain/language tags for a lesson's text.

sonder's `lessons` table (see memory_store.py) is a flat bag of free-text
strings retrieved by FTS5 + semantic similarity (retriever.py). That works
well when the *topic* of the current task matches the topic of a past lesson,
but retrieval has no notion of *domain* — a task about a SQL injection bug
can pull back a lesson about Python list comprehensions just because the
embeddings landed nearby, wasting a retrieval slot and diluting context.

This module assigns zero or more coarse tags (python, sql, cpp, javascript,
concurrency, security, algorithm, io, testing, git, networking, regex,
performance, windows) to a piece of text using deterministic keyword/regex
signals — no model call, no network, no GPU. Callers (retriever.py,
reflection.py, curriculum_run.py, ...) can use the tags to pre-filter or
boost candidate lessons before/alongside the existing FTS+embedding fusion,
or to bucket lessons for reporting (e.g. "how many concurrency lessons have
we learned?").

Pure and stdlib-only: one function of a string in, one sorted list of tag
names out (plus an optional per-tag hit-count breakdown for debugging /
weighting). Multiple tags can and often should fire on the same text — a
lesson about a Python threading bug is legitimately both "python" and
"concurrency".
"""
import re

# --- tag signal definitions ---------------------------------------------------
# Each tag maps to ONE compiled alternation pattern (mirrors the style used by
# difficulty_estimator's KEYWORD_CATEGORIES). A tag fires if its pattern
# matches at least once anywhere in the text; `tag_scores` additionally
# reports how many distinct signals within a tag's pattern fired, for callers
# that want a confidence-ish weight rather than a bare yes/no.
_TAG_PATTERNS = {
    "python": re.compile(
        r"\bpython(3)?\b|\bdef\s+\w+\s*\(|\bself\.\w+\b|\b__init__\b|"
        r"\belif\b|\blambda\b|\bpip\s+install\b|\bvenv\b|\.py\b|\bPEP\s?8\b|"
        r"\basyncio\b|\blist comprehension\b|\bNone\b|\bimport\s+\w+\s*$",
        re.I | re.M,
    ),
    "sql": re.compile(
        r"\bsql\b|\bselect\b.{0,80}\bfrom\b|\binsert\s+into\b|\bupdate\b.{0,80}\bset\b|"
        r"\bdelete\s+from\b|\binner\s+join\b|\bleft\s+join\b|\bgroup\s+by\b|"
        r"\border\s+by\b|\bprimary\s+key\b|\bforeign\s+key\b|\bsqlite\b|"
        r"\bpostgres(ql)?\b|\bmysql\b|\bwhere\s+clause\b",
        re.I | re.S,
    ),
    "cpp": re.compile(
        r"\bc\+\+\b|\bcpp\b|\bstd::\w+|\bnullptr\b|\btemplate\s*<|\bnamespace\s+\w+|"
        r"#include\s*[<\"]|\bconstexpr\b|\bunique_ptr\b|\bshared_ptr\b|\bRAII\b|"
        r"\boverride\b|\bmake_(unique|shared)\b|\bMSVC\b|\bvcvars\b",
        re.I,
    ),
    "javascript": re.compile(
        r"\bjavascript\b|\btypescript\b|\bnode(\.js)?\b|\bconst\s+\w+\s*=|"
        r"\blet\s+\w+\s*=|=>\s*\{|\bfunction\s*\(|\bnpm\b|\bconsole\.log\b|"
        r"\breact\b|\basync function\b|\bnpx\b|\bpackage\.json\b",
        re.I,
    ),
    "concurrency": re.compile(
        r"\bthread(s|ing)?\b|\basync(io)?\b|\bconcurren(t|cy)\b|\bmutex\b|"
        r"\block(s|ing)?\b|\brace condition\b|\bdeadlock\b|\bsemaphore\b|"
        r"\bmulti\s?process(ing)?\b|\batomic(ally)?\b|\bcondition_variable\b|\bGIL\b",
        re.I,
    ),
    "security": re.compile(
        r"\bsecurity\b|\bvulnerab(le|ility)\b|\bsql injection\b|\bXSS\b|\bCSRF\b|"
        r"\bsaniti[sz](e|ation)\b|\bauthenticat(e|ion)\b|\bauthoriz(e|ation)\b|"
        r"\bencrypt(ion|ed)?\b|\bhash(ing)?\b|\bpassword\b|\bCVE-\d+|\bexploit\b|"
        r"\bbuffer overflow\b|\bsecret(s)?\b|\bcredential(s)?\b",
        re.I,
    ),
    "algorithm": re.compile(
        r"\balgorithm\b|\bsort(ing)?\b|\bbig[- ]o\b|\bO\([^)]{1,20}\)|"
        r"\brecursi(on|ve(ly)?)\b|\bdynamic programming\b|\bgreedy\b|"
        r"\bdivide and conquer\b|\bgraph\b|\bbinary search\b|\bmemoi[sz](e|ation)\b",
        re.I,
    ),
    "io": re.compile(
        r"\bfile(s)?\b|\bopen\(|\.read\(\)|\.write\(\)|\bwith open\b|"
        r"\bstdin\b|\bstdout\b|\bstderr\b|\bfilesystem\b|\bfile path\b|"
        r"\bstream(ing)?\b|\bencoding\b|\butf-8\b|\bread/write\b",
        re.I,
    ),
    "testing": re.compile(
        r"\btest(s|ing)?\b|\bpytest\b|\bunittest\b|\bassert\w*\b|\bmock(ed|ing)?\b|"
        r"\bfixture(s)?\b|\bTDD\b|\bcoverage\b|\bregression\b|\bunit test\b",
        re.I,
    ),
    "git": re.compile(
        r"\bgit\b|\bcommit(s|ted)?\b|\bmerge\b|\brebase\b|\bbranch(es)?\b|"
        r"\bpull request\b|\bgithub\b|\bworktree\b|\bgit add\b",
        re.I,
    ),
    "networking": re.compile(
        r"\bhttps?\b|\bsocket(s)?\b|\bTCP\b|\bUDP\b|\bDNS\b|\bREST(ful)?\b|"
        r"\bendpoint(s)?\b|\bAPI\s+(call|request|endpoint)\b|\bport\s+\d+\b",
        re.I,
    ),
    "regex": re.compile(
        r"\bregex\b|\bregular expression\b|\bre\.(match|search|sub|findall|compile)\b",
        re.I,
    ),
    "performance": re.compile(
        r"\bperformance\b|\boptimi[sz](e|ation)\b|\blatency\b|\bthroughput\b|"
        r"\bbenchmark(ed|ing)?\b|\bprofil(e|ing)\b|\bcache(d|ing)?\b|\bsccache\b",
        re.I,
    ),
    "windows": re.compile(
        r"\bwindows\b|\bpowershell\b|\bcmd\.exe\b|\bMSVC\b|\.bat\b|\.ps1\b|"
        r"\bregistry\b|\bHKLM\b|\bHKCU\b",
        re.I,
    ),
}

# Public, stable, sorted tuple of every tag name this module can produce.
TAGS = tuple(sorted(_TAG_PATTERNS))


def tag(text):
    """Return the sorted list of tag names whose signal fires in `text`.

    Multiple tags may (and often should) fire on the same text. Returns an
    empty list for empty/whitespace-only/None input.
    """
    text = text or ""
    if not text.strip():
        return []
    return sorted(name for name, pattern in _TAG_PATTERNS.items() if pattern.search(text))


def tag_scores(text):
    """Return {tag_name: hit_count} for every tag with at least one hit.

    `hit_count` is the number of non-overlapping matches of that tag's
    pattern in `text` — a rough confidence signal for callers that want to
    weight tags rather than treat them as a flat yes/no set (e.g. boosting
    retrieval for the tag with the strongest signal when several fire).
    """
    text = text or ""
    if not text.strip():
        return {}
    scores = {}
    for name, pattern in _TAG_PATTERNS.items():
        n = len(pattern.findall(text))
        if n:
            scores[name] = n
    return scores


def filter_by_tag(lessons, wanted_tag, text_key="text"):
    """Filter an iterable of lesson-like items to those tagged `wanted_tag`.

    `lessons` items may be sqlite3.Row, dict, or any mapping supporting
    `item[text_key]`. Convenience helper for retriever-style pre-filtering;
    pure and does not touch any database itself.
    """
    wanted_tag = wanted_tag.lower()
    return [item for item in lessons if wanted_tag in tag(item[text_key])]
