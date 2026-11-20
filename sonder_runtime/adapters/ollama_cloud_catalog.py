"""Best-effort read of the public ollama.com cloud catalog (no official API).

Only two fixed URL shapes are ever requested: the cloud search page and one
family's tags page.  Requests go through the root ``web_tools`` bounded
public-address transport (generic ``sonder-local-agent/1.0`` User-Agent,
``Accept-Encoding: identity``, no cookies, no body, response size capped), so
no user data, prompt or configuration leaves the machine.  The fetch is gated
by ``SONDER_WEB_TOOLS`` and ``SONDER_CLOUD_CATALOG``; it does not need cloud
opt-in because nothing it sends is chosen by a user or a model.

Any HTTP or transport failure raises :class:`CatalogUnavailable`; the caller
discards the catalog for that run.  A search page that parses to nothing is
reported as ``unparsed`` (the page layout changed), not as an error.

Stateless; safe to call from any thread.
"""

from __future__ import annotations

import http.client
import importlib
import re
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sonder_runtime.domain.cloud_model_refresh import (
    CATALOG_REPROBE_SECONDS,
    MAX_FAMILIES,
    parse_catalog_families,
    parse_catalog_tags,
)

SEARCH_URL = "https://ollama.com/search?c=cloud"
TAGS_URL = "https://ollama.com/library/{family}/tags"
CATALOG_HOST = "ollama.com"
TAGS_REFETCH_SECONDS = CATALOG_REPROBE_SECONDS
_TAGS_PATH_RE = re.compile(r"/library/[a-z0-9][a-z0-9._-]{0,63}/tags")
_OFF = frozenset({"0", "false", "no", "off"})

Requester = Callable[[str, float], bytes]


class CatalogUnavailable(Exception):
    """The catalog could not be read; ``reason`` is ``transport``, ``http`` or ``too_large``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CatalogSnapshot:
    """One catalog read: family -> cloud stub tags (empty when not re-fetched this run)."""

    fetched_at: float
    families: Mapping[str, tuple[str, ...]]
    status: str
    refreshed: frozenset[str] = field(default_factory=frozenset)


def enabled(env: Mapping[str, str]) -> bool:
    """Web tools on (process and ``env``) and ``SONDER_CLOUD_CATALOG`` not off."""
    web_tools = importlib.import_module("web_tools")
    return (
        bool(web_tools.enabled())
        and str(env.get("SONDER_WEB_TOOLS", "1")).strip().lower() not in _OFF
        and str(env.get("SONDER_CLOUD_CATALOG", "1")).strip().lower() not in _OFF
    )


def _allowed_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme != "https"
        or parts.hostname != CATALOG_HOST
        or parts.netloc != CATALOG_HOST
        or port is not None
        or parts.fragment
    ):
        return False
    if parts.path == "/search":
        return parts.query == "c=cloud"
    return _TAGS_PATH_RE.fullmatch(parts.path) is not None and not parts.query


def _request_bytes(url: str, timeout: float) -> bytes:
    """GET one of the two fixed catalog URL shapes; refuses any other URL before I/O."""
    if not _allowed_url(url):
        raise ValueError("catalog URL refused: only %s and %s" % (SEARCH_URL, TAGS_URL))
    web_tools = importlib.import_module("web_tools")
    raw, _content_type = web_tools._request(url, timeout=timeout)
    if not isinstance(raw, bytes):
        raise CatalogUnavailable("transport")
    return raw


def _fetch_text(request: Requester, url: str, timeout: float) -> str:
    try:
        return request(url, timeout).decode("utf-8", errors="replace")
    except CatalogUnavailable:
        raise
    except urllib.error.HTTPError as exc:
        raise CatalogUnavailable("http") from exc
    except ValueError as exc:
        reason = "too_large" if "exceeds" in str(exc) else "transport"
        raise CatalogUnavailable(reason) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise CatalogUnavailable("transport") from exc


def fetch_catalog(
    *,
    request: Requester | None = None,
    timeout: float = 10.0,
    budget_seconds: float = 90.0,
    max_families: int = MAX_FAMILIES,
    known_families: Mapping[str, float] | None = None,
    now: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> CatalogSnapshot:
    """Read the search page, then each family's tags page that is new or older than 7 days.

    ``known_families`` maps a family to when its tags were last fetched.  When
    ``budget_seconds`` runs out the remaining families keep empty tags and the
    status is ``partial``.  Fetches are sequential.
    """
    send = request if request is not None else _request_bytes
    started = monotonic()
    fetched_at = time.time() if now is None else now
    families = parse_catalog_families(_fetch_text(send, SEARCH_URL, timeout))[: max(0, max_families)]
    if not families:
        return CatalogSnapshot(fetched_at, {}, "unparsed")
    known = known_families or {}
    tags: dict[str, tuple[str, ...]] = {}
    refreshed: set[str] = set()
    status = "ok"
    for family in families:
        last = known.get(family)
        tags[family] = ()
        if last is not None and fetched_at - last < TAGS_REFETCH_SECONDS:
            continue
        if monotonic() - started >= budget_seconds:
            status = "partial"
            continue
        page = _fetch_text(send, TAGS_URL.format(family=family), timeout)
        tags[family] = parse_catalog_tags(family, page)
        refreshed.add(family)
    return CatalogSnapshot(fetched_at, tags, status, frozenset(refreshed))


__all__ = [
    "CatalogSnapshot",
    "CatalogUnavailable",
    "SEARCH_URL",
    "TAGS_URL",
    "enabled",
    "fetch_catalog",
]
