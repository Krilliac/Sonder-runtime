"""Minimal stdlib web search/fetch helpers for the local agent.

Network access is opt-out via SONDER_WEB_TOOLS=0. Search defaults to
DuckDuckGo's HTML endpoint and uses lightweight HTML parsing; callers can point
SONDER_SEARCH_URL at another endpoint containing "{query}".
"""
import html
from html.parser import HTMLParser
import base64
from email.message import Message
import http.client
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib


DEFAULT_SEARCH_URL = "https://duckduckgo.com/html/?q={query}"
MOJEEK_SEARCH_URL = "https://www.mojeek.com/search?q={query}"
BING_SEARCH_URL = "https://www.bing.com/search?q={query}"
BING_SEARCH_RSS_URL = "https://www.bing.com/search?q={query}&format=rss"
USER_AGENT = "sonder-local-agent/1.0"
MAX_REDIRECTS = 5
MAX_DECOMPRESSED_BYTES = 2_000_000
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_NUMERIC_HOST_PART = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+$"
)
_JSON_MEDIA_TYPES = {
    "application/json", "application/json-seq", "application/ndjson",
    "application/x-ndjson",
}
_XML_MEDIA_TYPES = {"application/xml", "text/xml"}
_BINARY_SIGNATURES = (
    b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a",
    b"GIF89a", b"PK\x03\x04", b"\x7fELF",
)
_XML_DECLARED_ENCODING = re.compile(
    br"<\?xml\b[^>]{0,200}\bencoding\s*=\s*['\"]\s*([^'\"\s]+)",
    re.IGNORECASE,
)
_HTML_DECLARED_CHARSET = re.compile(
    br"<meta\b[^>]{0,500}\bcharset\s*=\s*['\"]?\s*"
    br"([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_DOCS_URL = "https://open-meteo.com/en/docs"
IP_LOCATION_URL = (
    "https://ipwho.is/?fields=success,message,country,country_code,region,"
    "region_code,city,timezone"
)
IP_LOCATION_DOCS_URL = "https://ipwhois.io/documentation"
_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Light freezing drizzle",
    57: "Heavy freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Rain showers", 82: "Heavy rain showers",
    85: "Light snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with light hail", 99: "Thunderstorm with heavy hail",
}
_SEARCH_STOPWORDS = {
    "about", "and", "api", "browse", "check", "current", "docs",
    "documentation", "find", "for", "from", "give", "internet", "latest",
    "link", "look", "news", "official", "online", "recent", "report",
    "search", "show", "tell", "the", "this", "today", "url", "weather",
    "web", "with",
}
# Conversational filler stripped from provider queries by build_search_query.
# Distinct from _SEARCH_STOPWORDS (post-hoc relevance scoring): this set is
# about how users *phrase* requests to a chat assistant, and must NOT contain
# topical nouns like "news" that make a search query purposeful.
_QUERY_FILLER = {
    "a", "access", "an", "and", "about", "any", "are", "at", "be", "being", "can",
    "could", "did", "do", "does", "fetch", "find", "for", "get", "give",
    "grab", "had", "has", "have", "hey", "hi", "how", "i", "in", "internet",
    "is", "it", "just", "kindly", "know", "like", "me", "my", "need", "of",
    "on", "one", "or", "please", "pull", "should", "show", "some", "tell",
    "that", "the", "this", "those", "to", "tool", "tools", "us", "use",
    "using", "want", "was", "we", "web", "were", "what", "what's", "whats",
    "when", "where", "which", "who", "who's", "whos", "will", "with",
    "would", "you", "your",
}
# Recency adverbs dropped from the query text and replaced by an explicit
# month+year anchor, so "current news headline" becomes a dated news query
# instead of ranking literal-match domains (current.com) first.
_RECENCY_FILLER = {
    "breaking", "current", "currently", "latest", "most", "now", "recent",
    "recently", "today", "today's", "todays",
}
_RECENCY_SIGNAL = re.compile(
    r"\b(?:latest|breaking|current(?:ly)?|today(?:'s)?|right\s+now|"
    r"most\s+recent(?:ly)?|as\s+of\s+(?:now|today)|news)\b",
    re.IGNORECASE,
)
_UPCOMING_RECENCY_SIGNAL = re.compile(
    r"^\s*(?:(?:when|where)(?:['’]s|\s+is)|"
    r"what\s+(?:date|time)(?:['’]s|\s+is))\s+(?:the\s+)?"
    r"(?:next|upcoming)\b(?![^\n]*(?:regex|iterator|state\s+machine|"
    r"algorithm|button|branch|selector|sender))[^\n]{0,60}"
    r"\b(?:race|grand\s+prix|game|match|"
    r"election|release|launch|episode|concert)\b\s*[?!.]*$",
    re.IGNORECASE,
)


def build_search_query(prompt, intent_kind="research", now=None):
    """Construct a purposeful provider query from conversational phrasing.

    Only rewrites research/current-info prompts that carry a recency signal
    (news / latest / current / today ...): conversational filler and recency
    adverbs are stripped, and an explicit "<Month> <Year>" anchor is appended
    so generic phrasing stops ranking irrelevant literal-match domains first.
    Everything else is returned verbatim, so already-specific queries are
    never degraded. Callers should keep the original prompt available as
    context/fallback.
    """
    text = str(prompt or "").strip()
    if not text or intent_kind not in ("research", "current-info", "news"):
        return text
    upcoming = bool(_UPCOMING_RECENCY_SIGNAL.search(text))
    if not (_RECENCY_SIGNAL.search(text) or upcoming):
        return text
    content = []
    for word in re.findall(
        r"(?:\.[A-Za-z][\w+#./-]*|[A-Za-z0-9][\w'./+#-]*)",
        text,
    ):
        lowered = word.lower()
        if (
            lowered in _QUERY_FILLER
            or lowered in _RECENCY_FILLER
            or (upcoming and lowered in {"next", "upcoming"})
        ):
            continue
        content.append(word)
    if not content:
        content = ["news"]
    stamp = time.strftime(
        "%B %Y", time.localtime(time.time() if now is None else now)
    )
    query = " ".join(content)
    if stamp.lower() not in query.lower():
        query = "%s %s" % (query, stamp)
    return query[:200]


_US_STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut",
    "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii",
    "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine",
    "MD": "maryland", "MA": "massachusetts", "MI": "michigan",
    "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
    "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
    "NY": "new york", "NC": "north carolina", "ND": "north dakota",
    "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia",
    "WA": "washington", "WV": "west virginia", "WI": "wisconsin",
    "WY": "wyoming", "DC": "district of columbia",
}


def enabled():
    return os.environ.get("SONDER_WEB_TOOLS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _connect_pinned_socket(address, port, timeout, source_address=None):
    """Connect directly to a validated numeric address without another DNS lookup."""
    ip = ipaddress.ip_address(address)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        target = (ip.compressed, port, 0, 0) if ip.version == 6 else (ip.compressed, port)
        sock.connect(target)
        return sock
    except Exception:
        sock.close()
        raise


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, *, connect_host, timeout=10):
        self._connect_host = connect_host
        super().__init__(host, port=port, timeout=timeout)

    def connect(self):
        self.sock = _connect_pinned_socket(
            self._connect_host,
            self.port,
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, connect_host, timeout=10):
        self._connect_host = connect_host
        super().__init__(host, port=port, timeout=timeout)

    def connect(self):
        self.sock = _connect_pinned_socket(
            self._connect_host,
            self.port,
            self.timeout,
            self.source_address,
        )
        # TLS still verifies and sends SNI for the original hostname while the
        # TCP socket remains pinned to the address that passed policy checks.
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedResponse:
    def __init__(self, response, connection):
        self._response = response
        self._connection = connection
        self.status = response.status
        self.code = response.status
        self.headers = response.headers

    def read(self, size=-1):
        return self._response.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        try:
            close = getattr(self._response, "close", None)
            if close is not None:
                close()
        finally:
            self._connection.close()
        return False


def _host_header(host, port, scheme):
    display = "[%s]" % host if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    return display if port == default_port else "%s:%d" % (display, port)


def _urlopen(req, timeout=10):
    """Open a validated request using only its pre-resolved public addresses."""
    addresses = tuple(getattr(req, "_sonder_addresses", ()))
    if not addresses:
        raise ValueError("request has no validated pinned address")
    parsed = urllib.parse.urlparse(req.full_url)
    host = (parsed.hostname or "").rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("URL has an invalid hostname or port") from exc
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    headers = dict(req.header_items())
    headers["Host"] = _host_header(host, port, parsed.scheme)
    headers["Connection"] = "close"
    connection_type = (
        _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
    )
    last_error = None
    for address in addresses:
        connection = connection_type(
            host,
            port,
            connect_host=address,
            timeout=timeout,
        )
        try:
            connection.request(
                req.get_method(),
                target,
                body=req.data,
                headers=headers,
            )
            return _PinnedResponse(connection.getresponse(), connection)
        except Exception as exc:
            last_error = exc
            connection.close()
    raise urllib.error.URLError(last_error or "no validated address was reachable")


def _looks_like_noncanonical_numeric_host(host):
    candidate = (host or "").rstrip(".")
    if not candidate or ":" in candidate:
        return False
    parts = candidate.split(".")
    if any(not part or not _NUMERIC_HOST_PART.fullmatch(part) for part in parts):
        return False
    try:
        ipaddress.IPv4Address(candidate)
    except ValueError:
        return True
    return False


def _is_globally_routable(address):
    ip = ipaddress.ip_address(address)
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_global and not ip.is_private and not ip.is_loopback
        and not ip.is_link_local and not ip.is_reserved
        and not ip.is_unspecified and not ip.is_multicast
    )


def _resolve_public_addresses(host, port):
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise ValueError("URL hostname could not be resolved") from exc
    addresses = set()
    for row in rows:
        sockaddr = row[4]
        if not sockaddr:
            continue
        raw = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError("URL hostname resolved to an invalid address") from exc
        if not _is_globally_routable(ip):
            raise ValueError("URL hostname must resolve only to globally routable addresses")
        addresses.add(ip.compressed)
    if not addresses:
        raise ValueError("URL hostname did not resolve to an address")
    return tuple(sorted(addresses))


def _validated_public_target(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL userinfo is not allowed")
    if "%" in host:
        raise ValueError("scoped or percent-encoded hostnames are not allowed")
    if host in ("localhost", "localhost.localdomain"):
        raise ValueError("localhost URLs are not allowed")
    if _looks_like_noncanonical_numeric_host(host):
        raise ValueError("non-canonical numeric IP hostnames are not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("URL has an invalid port") from exc
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            dns_host = host.rstrip(".").encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("URL has an invalid hostname") from exc
        return parsed, _resolve_public_addresses(dns_host, port)
    if not _is_globally_routable(ip):
        raise ValueError("private/local network URLs are not allowed")
    return parsed, (ip.compressed,)


def _validate_public_url(url):
    _validated_public_target(url)
    return url


def _clean_search_title(value):
    title = " ".join(str(value or "").split())
    return re.sub(r"^[\u200b-\u200f\ufe0e\ufe0f]+", "", title).strip()


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
        title = _clean_search_title("".join(self._text))
        if title:
            self.links.append({
                "title": html.unescape(title),
                "url": _clean_result_url(self._href),
            })
        self._href = None
        self._text = []


class _BingSearchParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._in_result = False
        self._in_heading = False
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "li" and "b_algo" in attrs.get("class", "").split():
            self._in_result = True
        elif tag == "h2" and self._in_result:
            self._in_heading = True
        elif tag == "a" and self._in_heading:
            self._href = attrs.get("href", "")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            title = _clean_search_title("".join(self._text))
            if title:
                self.links.append({
                    "title": html.unescape(title),
                    "url": _clean_bing_result_url(self._href),
                })
            self._href = None
            self._text = []
        elif tag == "h2":
            self._in_heading = False
        elif tag == "li" and self._in_result:
            self._in_result = False


class _MojeekSearchParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        if "title" not in attrs.get("class", "").split():
            return
        self._href = attrs.get("href", "")
        self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self._href is None:
            return
        title = _clean_search_title("".join(self._text))
        if title:
            self.links.append({
                "title": html.unescape(title),
                "url": html.unescape(self._href),
            })
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


def _clean_bing_result_url(url):
    url = html.unescape(url or "")
    parsed = urllib.parse.urlparse(url)
    values = urllib.parse.parse_qs(parsed.query).get("u") or []
    if values and values[0].startswith("a1"):
        encoded = values[0][2:]
        try:
            padding = "=" * (-len(encoded) % 4)
            target = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            if target.startswith(("http://", "https://")):
                return target
        except (ValueError, UnicodeDecodeError):
            pass
    return url


def _search_rows(text, source_url, limit):
    host = (urllib.parse.urlparse(source_url).hostname or "").lower()
    if host.endswith("bing.com"):
        parser = _BingSearchParser()
    elif host.endswith("mojeek.com"):
        parser = _MojeekSearchParser()
    else:
        parser = _SearchParser()
    parser.feed(text)
    results = []
    seen = set()
    blocked_hosts = {
        "duckduckgo.com", "www.duckduckgo.com", "www.mojeek.com",
        "mojeek.com", "www.bing.com", "bing.com",
    }
    for row in parser.links:
        result_url = row["url"]
        result_host = (urllib.parse.urlparse(result_url).hostname or "").lower()
        if not result_url.startswith(("http://", "https://")):
            continue
        if result_host in blocked_hosts or result_url in seen:
            continue
        seen.add(result_url)
        results.append({"title": row["title"], "url": result_url, "snippet": ""})
        if len(results) >= limit:
            break
    return results


def _search_rss_rows(raw, limit):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("search provider returned invalid RSS") from exc
    results = []
    seen = set()
    for item in root.findall(".//item"):
        title = _clean_search_title(item.findtext("title") or "")
        url = (item.findtext("link") or "").strip()
        description = item.findtext("description") or ""
        parser = _TextParser()
        parser.feed(description)
        if not title or not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": parser.text()[:500]})
        if len(results) >= limit:
            break
    return results


def _search_query_variants(query):
    variants = [query]
    for pattern in (
        r"\b[A-Z][\w]*(?:-[A-Z][\w]*)+\b",
        r'["\']([^"\']{3,80})["\']',
        r"\b(?:https?://)?([A-Za-z0-9-]+\.[A-Za-z]{2,})(?:/\S*)?",
    ):
        for match in re.finditer(pattern, query):
            value = (match.group(1) if match.lastindex else match.group(0)).strip()
            if value and value.lower() not in {item.lower() for item in variants}:
                variants.append(value)
    return variants[:4]


def _search_relevance(query, results):
    terms = {
        term for term in re.findall(r"[a-z0-9]+", query.lower())
        if len(term) >= 3 and term not in _SEARCH_STOPWORDS
    }
    if not terms:
        return 0, 0
    best = 0
    for row in results:
        text = "%s %s" % (row.get("title", ""), row.get("url", ""))
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        best = max(best, len(terms & words))
    return best, min(2, len(terms))


def _decode_content_encoding(raw, encoding):
    """Decode bounded HTTP gzip/deflate bodies; fail closed on expansion bombs."""
    encoding = str(encoding or "").split(",", 1)[0].strip().lower()
    if not encoding or encoding == "identity":
        return raw
    if encoding not in {"gzip", "x-gzip", "deflate"}:
        raise ValueError("unsupported HTTP content encoding %r" % encoding)

    def expand(wbits):
        decoder = zlib.decompressobj(wbits)
        decoded = decoder.decompress(raw, MAX_DECOMPRESSED_BYTES + 1)
        if decoder.unconsumed_tail or len(decoded) > MAX_DECOMPRESSED_BYTES:
            raise ValueError("HTTP response expands beyond the safety limit")
        tail = decoder.flush()
        if len(decoded) + len(tail) > MAX_DECOMPRESSED_BYTES:
            raise ValueError("HTTP response expands beyond the safety limit")
        if not decoder.eof:
            raise ValueError("HTTP response compression stream is incomplete")
        return decoded + tail

    if encoding in {"gzip", "x-gzip"}:
        return expand(zlib.MAX_WBITS | 16)
    try:
        return expand(zlib.MAX_WBITS)
    except zlib.error:
        return expand(-zlib.MAX_WBITS)


def _request(url, timeout=10):
    current_url = url
    redirects = 0
    while True:
        _, addresses = _validated_public_target(current_url)
        req = urllib.request.Request(current_url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
        })
        req._sonder_addresses = addresses
        try:
            response = _urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in _REDIRECT_CODES:
                raise
            response = exc
        with response as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = getattr(resp, "code", 200)
            if status in _REDIRECT_CODES:
                if redirects >= MAX_REDIRECTS:
                    raise ValueError("too many redirects (max %d)" % MAX_REDIRECTS)
                location = resp.headers.get("Location", "")
                if not location:
                    raise ValueError("redirect response has no Location header")
                current_url = urllib.parse.urljoin(current_url, location)
                redirects += 1
                continue
            raw = resp.read(512000)
            raw = _decode_content_encoding(
                raw, resp.headers.get("Content-Encoding", "")
            )
            return raw, resp.headers.get("Content-Type", "")


def web_search(query, limit=5, timeout=10):
    if not enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    query = (query or "").strip()
    if not query:
        raise ValueError("empty search query")
    limit = max(1, min(int(limit or 5), 10))
    configured_endpoint = os.environ.get("SONDER_SEARCH_URL", "").strip()
    endpoints = (
        [configured_endpoint] if configured_endpoint
        else [DEFAULT_SEARCH_URL, MOJEEK_SEARCH_URL, BING_SEARCH_RSS_URL]
    )
    failures = []
    best_results = []
    best_relevance = -1
    for endpoint in endpoints:
        provider_queries = (
            [query] if configured_endpoint else _search_query_variants(query)
        )
        for provider_query in provider_queries:
            url = endpoint.format(query=urllib.parse.quote_plus(provider_query))
            try:
                raw, ctype = _request(url, timeout=timeout)
                text = raw.decode("utf-8", "replace")
                lowered = text.lower()
                if "automated queries" in lowered or "403 - forbidden" in lowered:
                    failures.append(
                        "%s blocked" % urllib.parse.urlparse(url).hostname
                    )
                    break
                if "json" in ctype:
                    data = json.loads(text)
                    rows = data.get("results") if isinstance(data, dict) else data
                    results = [
                        {
                            "title": str(row.get("title", "")),
                            "url": str(row.get("url", "")),
                            "snippet": str(row.get("snippet", "")),
                        }
                        for row in (rows or [])[:limit]
                        if isinstance(row, dict)
                    ]
                elif "xml" in ctype or text.lstrip().startswith("<?xml"):
                    results = _search_rss_rows(raw, limit)
                else:
                    results = _search_rows(text, url, limit)
                if results:
                    relevance, required = _search_relevance(query, results)
                    if relevance > best_relevance:
                        best_results = results
                        best_relevance = relevance
                    if relevance >= required:
                        return results
                if "challenge-form" in text or "anomaly-modal" in text:
                    failures.append(
                        "%s challenge" % urllib.parse.urlparse(url).hostname
                    )
                    break
            except Exception as exc:
                failures.append(
                    "%s %s" % (
                        urllib.parse.urlparse(url).hostname, type(exc).__name__,
                    )
                )
                break
    if best_results:
        return best_results
    if failures and len(failures) == len(endpoints):
        raise RuntimeError("search providers unavailable: %s" % ", ".join(failures))
    return []


def _canonical_charset(value):
    """Return an allowlisted codec label, or reject an unsafe/unknown one."""
    token = str(value or "").strip().strip("\"'").lower().replace("_", "-")
    aliases = {
        "utf-8": "utf-8", "utf8": "utf-8",
        "unicode-1-1-utf-8": "utf-8",
        "utf-16": "utf-16", "utf16": "utf-16",
        "utf-16le": "utf-16le", "utf16le": "utf-16le",
        "utf-16-le": "utf-16le",
        "utf-16be": "utf-16be", "utf16be": "utf-16be",
        "utf-16-be": "utf-16be",
        "iso-8859-1": "latin-1", "iso8859-1": "latin-1",
        "latin-1": "latin-1", "latin1": "latin-1", "cp819": "latin-1",
        "windows-1252": "windows-1252", "windows1252": "windows-1252",
        "cp1252": "windows-1252", "x-cp1252": "windows-1252",
        "us-ascii": "ascii", "ascii": "ascii",
    }
    canonical = aliases.get(token)
    if canonical is None:
        raise ValueError("unsupported HTTP text charset %r" % value)
    return canonical


def _parse_text_content_type(value):
    """Parse and validate an HTTP Content-Type for a readable web document."""
    header = str(value or "").strip()
    if not header:
        raise ValueError("web page response is missing Content-Type")
    media_type = header.split(";", 1)[0].strip().lower()
    if not _MEDIA_TYPE.fullmatch(media_type):
        raise ValueError("invalid HTTP Content-Type %r" % header)

    if media_type in _JSON_MEDIA_TYPES or media_type.endswith("+json"):
        document_kind = "json"
    elif media_type in {"text/html", "application/xhtml+xml"}:
        document_kind = "html"
    elif media_type in _XML_MEDIA_TYPES or media_type.endswith("+xml"):
        document_kind = "xml"
    elif media_type.startswith("text/"):
        document_kind = "text"
    else:
        raise ValueError("unsupported non-text HTTP media type %r" % media_type)

    message = Message()
    message["content-type"] = header
    declared = []
    for key, parameter in (message.get_params(header="content-type") or [])[1:]:
        if str(key or "").lower() == "charset":
            if parameter in (None, ""):
                raise ValueError("HTTP Content-Type has an empty charset")
            declared.append(_canonical_charset(parameter))
    if len(set(declared)) > 1:
        raise ValueError("HTTP Content-Type has conflicting charset parameters")
    return media_type, document_kind, (declared[0] if declared else "")


def _document_declared_charset(raw, document_kind):
    """Read only allowlisted in-document HTML/XML charset declarations."""
    sample = raw[:4096]
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:]
    match = None
    if document_kind == "xml":
        match = _XML_DECLARED_ENCODING.search(sample)
    elif document_kind == "html":
        match = _HTML_DECLARED_CHARSET.search(sample)
    if not match:
        return ""
    try:
        value = match.group(1).decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("document charset declaration is not ASCII") from exc
    return _canonical_charset(value)


def _decode_web_document(raw, content_type):
    """Decode one bounded web document using MIME, BOM, and charset evidence."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("web page response body is not bytes")
    raw = bytes(raw)
    media_type, document_kind, header_charset = _parse_text_content_type(
        content_type
    )
    signature = raw.lstrip(b" \t\r\n\x00")
    if any(signature.startswith(prefix) for prefix in _BINARY_SIGNATURES):
        raise ValueError(
            "binary web content is not readable text (%s)" % media_type
        )

    body_charset = _document_declared_charset(raw, document_kind)
    if header_charset and body_charset and header_charset != body_charset:
        raise ValueError("conflicting HTTP and document charset declarations")
    declared_charset = header_charset or body_charset

    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        raise ValueError("unsupported UTF-32 byte order mark")
    if raw.startswith(b"\xef\xbb\xbf"):
        if declared_charset and declared_charset != "utf-8":
            raise ValueError("UTF-8 BOM conflicts with declared charset")
        codec = "utf-8-sig"
    elif raw.startswith(b"\xff\xfe"):
        if declared_charset not in ("", "utf-16", "utf-16le"):
            raise ValueError("UTF-16LE BOM conflicts with declared charset")
        codec = "utf-16"
    elif raw.startswith(b"\xfe\xff"):
        if declared_charset not in ("", "utf-16", "utf-16be"):
            raise ValueError("UTF-16BE BOM conflicts with declared charset")
        codec = "utf-16"
    else:
        codec = declared_charset or "utf-8"
        if codec == "utf-16":
            raise ValueError("declared UTF-16 text requires a byte order mark")
        codec = {
            "utf-16le": "utf-16-le",
            "utf-16be": "utf-16-be",
        }.get(codec, codec)
    try:
        text = raw.decode(codec, "strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise ValueError("web page body is not valid %s text" % codec) from exc
    if "\x00" in text:
        raise ValueError("web page body contains binary NUL bytes")
    if any(
        (ord(char) < 32 and char not in "\t\r\n\f")
        or 0x7F <= ord(char) <= 0x9F
        for char in text
    ):
        raise ValueError("web page body contains binary control bytes")

    if document_kind in {"html", "xml"} or (
        document_kind == "text" and "<html" in text[:1000].lower()
    ):
        parser = _TextParser()
        parser.feed(text)
        text = parser.text()
    if not text.strip():
        raise ValueError("web page contained no readable text")
    return text


def web_fetch(url, max_chars=8000, timeout=10):
    if not enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    max_chars = max(1000, min(int(max_chars or 8000), 30000))
    raw, ctype = _request(url, timeout=timeout)
    text = _decode_web_document(raw, ctype)
    return text[:max_chars]


def _json_request(url, timeout=10):
    raw, _content_type = _request(url, timeout=timeout)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ValueError("weather service returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("weather service returned an invalid response")
    if data.get("error"):
        raise ValueError(str(data.get("reason") or "weather service error"))
    return data


def _weather_condition(code):
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "Unknown conditions"
    return _WEATHER_CODES.get(value, "Weather code %d" % value)


def _wind_direction(degrees):
    try:
        value = float(degrees) % 360
    except (TypeError, ValueError):
        return ""
    points = (
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
    )
    return points[int((value + 11.25) // 22.5) % len(points)]


def _weather_place(location, timeout):
    queries = [location]
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) > 1 and parts[0].lower() != location.lower():
        queries.append(parts[0])
    qualifiers = [
        _US_STATE_NAMES.get(part.upper(), part.lower()) for part in parts[1:]
    ]
    for query in queries:
        geocode_query = urllib.parse.urlencode({
            "name": query, "count": 10, "language": "en", "format": "json",
        })
        geocode_url = "%s?%s" % (OPEN_METEO_GEOCODING_URL, geocode_query)
        geocode = _json_request(geocode_url, timeout=timeout)
        matches = [
            match for match in (geocode.get("results") or [])
            if isinstance(match, dict)
        ]
        if not matches:
            continue

        def score(place):
            searchable = " ".join(str(place.get(key) or "").lower() for key in (
                "name", "admin1", "admin2", "admin3", "country", "country_code",
            ))
            return sum(1 for qualifier in qualifiers if qualifier in searchable)

        return max(enumerate(matches), key=lambda row: (score(row[1]), -row[0]))[1]
    raise ValueError("no weather location matched %r" % location)


def weather_lookup(location, forecast_days=3, units="auto", timeout=10):
    """Resolve a user-supplied place and fetch current plus daily weather."""
    if not enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    location = re.sub(r"\s+", " ", str(location or "")).strip()
    if len(location) < 2:
        raise ValueError("location must be a city/region or postal code")
    if len(location) > 120 or any(ord(char) < 32 for char in location):
        raise ValueError("location is too long or contains control characters")
    units = str(units or "auto").strip().lower()
    if units not in {"auto", "metric", "imperial"}:
        raise ValueError("units must be auto, metric, or imperial")
    forecast_days = max(1, min(int(forecast_days or 3), 7))

    place = _weather_place(location, timeout)
    try:
        latitude = float(place["latitude"])
        longitude = float(place["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("weather geocoder omitted valid coordinates") from exc
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("weather geocoder returned invalid coordinates")

    resolved_units = units
    if resolved_units == "auto":
        resolved_units = "imperial" if place.get("country_code") == "US" else "metric"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "precipitation,weather_code,wind_speed_10m,wind_direction_10m"
        ),
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum,wind_speed_10m_max"
        ),
        "timezone": "auto",
        "forecast_days": forecast_days,
    }
    if resolved_units == "imperial":
        params.update({
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        })
    forecast_url = "%s?%s" % (
        OPEN_METEO_FORECAST_URL,
        urllib.parse.urlencode(params),
    )
    forecast = _json_request(forecast_url, timeout=timeout)
    return {
        "query": location,
        "place": place,
        "units": resolved_units,
        "forecast": forecast,
        "forecast_url": forecast_url,
        "source_url": OPEN_METEO_DOCS_URL,
    }


def normalize_location_hint(data):
    """Validate and minimize a client/server IP-location result without its IP."""
    if not isinstance(data, dict):
        raise ValueError("location hint must be an object")
    if data.get("success") is False:
        raise ValueError(str(data.get("message") or "IP location lookup failed"))
    result = {}
    for key in ("city", "region", "region_code", "country", "country_code", "timezone"):
        raw_value = data.get(key)
        if key == "timezone" and isinstance(raw_value, dict):
            raw_value = raw_value.get("id") or raw_value.get("name")
        if isinstance(raw_value, (dict, list, tuple, set)):
            raise ValueError("location hint contains an invalid %s" % key)
        value = re.sub(r"\s+", " ", str(raw_value or "")).strip()
        if value:
            if len(value) > 120 or any(ord(char) < 32 for char in value):
                raise ValueError("location hint contains an invalid %s" % key)
            result[key] = value
    if not (result.get("city") or result.get("region") or result.get("country")):
        raise ValueError("location lookup did not return a place")
    result.update({
        "approximate": True,
        "source": "ipwho.is",
        "source_url": IP_LOCATION_DOCS_URL,
    })
    return result


def approximate_location_lookup(timeout=10):
    """Resolve this process's public egress IP to an approximate place."""
    if not enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    return normalize_location_hint(_json_request(IP_LOCATION_URL, timeout=timeout))


def location_label(location):
    location = normalize_location_hint(location)
    parts = [location.get("city"), location.get("region"), location.get("country")]
    return ", ".join(
        str(part) for index, part in enumerate(parts)
        if part and part not in parts[:index]
    )


def format_approximate_location(location):
    location = normalize_location_hint(location)
    lines = [
        "Approximate location: %s" % location_label(location),
    ]
    if location.get("timezone"):
        lines.append("Timezone: %s" % location["timezone"])
    lines.extend([
        "Accuracy: city/region estimate from the public IP; VPNs and ISP routing can make it wrong.",
        "Raw IP: not retained or displayed.",
        "Source: ipwho.is (%s)" % location.get("source_url", IP_LOCATION_DOCS_URL),
    ])
    return "\n".join(lines)


def format_weather(result):
    place = result.get("place") or {}
    forecast = result.get("forecast") or {}
    current = forecast.get("current") or {}
    current_units = forecast.get("current_units") or {}
    daily = forecast.get("daily") or {}
    daily_units = forecast.get("daily_units") or {}
    place_parts = [place.get("name"), place.get("admin1"), place.get("country")]
    display_place = ", ".join(
        str(part) for index, part in enumerate(place_parts)
        if part and part not in place_parts[:index]
    ) or result.get("query") or "requested location"

    temp_unit = current_units.get("temperature_2m", "")
    wind_unit = current_units.get("wind_speed_10m", "")
    precip_unit = current_units.get("precipitation", "")
    direction = _wind_direction(current.get("wind_direction_10m"))
    wind = "%s %s" % (current.get("wind_speed_10m", "?"), wind_unit)
    if direction:
        wind += " %s" % direction
    lines = [
        "Weather for %s" % display_place,
        "Updated: %s (%s)" % (
            current.get("time", "unknown"), forecast.get("timezone", "local time"),
        ),
        "Now: %s, %s%s (feels like %s%s); humidity %s%%; wind %s; precipitation %s %s."
        % (
            _weather_condition(current.get("weather_code")),
            current.get("temperature_2m", "?"), temp_unit,
            current.get("apparent_temperature", "?"), temp_unit,
            current.get("relative_humidity_2m", "?"), wind,
            current.get("precipitation", "?"), precip_unit,
        ),
        "",
        "Forecast:",
    ]
    dates = daily.get("time") or []
    for index, date in enumerate(dates):
        def value(key, fallback="?"):
            values = daily.get(key) or []
            return values[index] if index < len(values) else fallback

        lines.append(
            "- %s: %s; high %s%s, low %s%s; precipitation %s%% (%s %s); wind up to %s %s."
            % (
                date, _weather_condition(value("weather_code", None)),
                value("temperature_2m_max"), daily_units.get("temperature_2m_max", ""),
                value("temperature_2m_min"), daily_units.get("temperature_2m_min", ""),
                value("precipitation_probability_max"), value("precipitation_sum"),
                daily_units.get("precipitation_sum", ""), value("wind_speed_10m_max"),
                daily_units.get("wind_speed_10m_max", ""),
            )
        )
    lines.extend([
        "",
        "Source: Open-Meteo (%s)" % result.get("source_url", OPEN_METEO_DOCS_URL),
        "Live data: %s" % result.get("forecast_url", ""),
    ])
    return "\n".join(lines)


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
