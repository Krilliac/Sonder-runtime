"""Minimal stdlib web search/fetch helpers for the local agent.

Network access is opt-out via TRILOBITE_WEB_TOOLS=0. Search defaults to
DuckDuckGo's HTML endpoint and uses lightweight HTML parsing; callers can point
TRILOBITE_SEARCH_URL at another endpoint containing "{query}".
"""
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_SEARCH_URL = "https://duckduckgo.com/html/?q={query}"
USER_AGENT = "trilobite-local-agent/1.0"
MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_NUMERIC_HOST_PART = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")


def enabled():
    return os.environ.get("TRILOBITE_WEB_TOOLS", "1").strip().lower() not in (
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
    addresses = tuple(getattr(req, "_trilobite_addresses", ()))
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
    current_url = url
    redirects = 0
    while True:
        _, addresses = _validated_public_target(current_url)
        req = urllib.request.Request(current_url, headers={"User-Agent": USER_AGENT})
        req._trilobite_addresses = addresses
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
            return resp.read(512000), resp.headers.get("Content-Type", "")


def web_search(query, limit=5, timeout=10):
    if not enabled():
        raise RuntimeError("web tools disabled by TRILOBITE_WEB_TOOLS")
    query = (query or "").strip()
    if not query:
        raise ValueError("empty search query")
    limit = max(1, min(int(limit or 5), 10))
    endpoint = os.environ.get("TRILOBITE_SEARCH_URL", DEFAULT_SEARCH_URL)
    url = endpoint.format(query=urllib.parse.quote_plus(query))
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
