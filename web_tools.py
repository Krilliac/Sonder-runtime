"""Minimal stdlib web search/fetch helpers for the local agent.

Network access is opt-out via TRILOBITE_WEB_TOOLS=0. Search defaults to
DuckDuckGo's HTML endpoint and uses lightweight HTML parsing; callers can point
TRILOBITE_SEARCH_URL at another endpoint containing "{query}".
"""
import html
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import urllib.parse
import urllib.request


DEFAULT_SEARCH_URL = "https://duckduckgo.com/html/?q={query}"
USER_AGENT = "trilobite-local-agent/1.0"


def enabled():
    return os.environ.get("TRILOBITE_WEB_TOOLS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _urlopen(req, timeout=10):
    return urllib.request.urlopen(req, timeout=timeout)


def _validate_public_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL has no hostname")
    if host in ("localhost", "localhost.localdomain"):
        raise ValueError("localhost URLs are not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        raise ValueError("private/local network URLs are not allowed")
    return url


class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        href = attrs.get("href", "")
        css = attrs.get("class", "")
        if "result__a" in css or href.startswith("http") or "uddg=" in href:
            self._href = href
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self._href is None:
            return
        title = " ".join("".join(self._text).split())
        if title:
            self.links.append({"title": html.unescape(title), "url": _clean_result_url(self._href)})
        self._href = None
        self._text = []


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        text = html.unescape(" ".join(self.parts))
        return re.sub(r"\n\s+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _clean_result_url(url):
    url = html.unescape(url or "")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs and qs["uddg"]:
        return qs["uddg"][0]
    if parsed.scheme in ("http", "https"):
        return url
    return urllib.parse.urljoin("https://duckduckgo.com", url)


def _request(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=timeout) as resp:
        raw = resp.read(512000)
        ctype = resp.headers.get("Content-Type", "")
    return raw, ctype


def web_search(query, limit=5, timeout=10):
    if not enabled():
        raise RuntimeError("web tools disabled by TRILOBITE_WEB_TOOLS")
    query = (query or "").strip()
    if not query:
        raise ValueError("empty search query")
    limit = max(1, min(int(limit or 5), 10))
    endpoint = os.environ.get("TRILOBITE_SEARCH_URL", DEFAULT_SEARCH_URL)
    url = endpoint.format(query=urllib.parse.quote_plus(query))
    _validate_public_url(url)
    raw, ctype = _request(url, timeout=timeout)
    text = raw.decode("utf-8", "replace")
    if "json" in ctype:
        data = json.loads(text)
        rows = data.get("results") if isinstance(data, dict) else data
        return [
            {"title": str(r.get("title", "")), "url": str(r.get("url", "")), "snippet": str(r.get("snippet", ""))}
            for r in (rows or [])[:limit]
            if isinstance(r, dict)
        ]
    parser = _SearchParser()
    parser.feed(text)
    results = []
    seen = set()
    for row in parser.links:
        url = row["url"]
        if not url.startswith(("http://", "https://")) or "duckduckgo.com" in urllib.parse.urlparse(url).netloc:
            continue
        if url in seen:
            continue
        seen.add(url)
        results.append({"title": row["title"], "url": url, "snippet": ""})
        if len(results) >= limit:
            break
    return results


def web_fetch(url, max_chars=8000, timeout=10):
    if not enabled():
        raise RuntimeError("web tools disabled by TRILOBITE_WEB_TOOLS")
    _validate_public_url(url)
    max_chars = max(1000, min(int(max_chars or 8000), 30000))
    raw, ctype = _request(url, timeout=timeout)
    text = raw.decode("utf-8", "replace")
    if "html" in ctype or "<html" in text[:1000].lower():
        parser = _TextParser()
        parser.feed(text)
        text = parser.text()
    return text[:max_chars]


def format_search_results(results):
    if not results:
        return "(no results)"
    lines = []
    for i, row in enumerate(results, start=1):
        lines.append("%d. %s" % (i, row.get("title") or "(untitled)"))
        lines.append("   %s" % row.get("url", ""))
        if row.get("snippet"):
            lines.append("   %s" % row["snippet"])
    return "\n".join(lines)
