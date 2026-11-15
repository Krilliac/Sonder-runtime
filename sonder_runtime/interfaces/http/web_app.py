"""Serve the Flutter web build of the Sonder app at ``/app/``.

The bundle is a release directory written by ``scripts/build_web_app.py``:
``<root>/current.json`` names a release under ``<root>/releases/`` and each
release carries ``sonder-web-manifest.json`` listing every file with its size
and sha256.  Membership in that manifest is the allowlist: a request path is
served only when it names a listed file, after strict segment validation and a
containment check that refuses symlinks and junctions.

The static shell is unauthenticated on purpose (a browser navigation cannot
carry the credential headers and the shell holds no secrets).  Every ``/v1``
call the app makes stays behind the server's normal authentication.

Thread affinity: ``handle_web_app`` runs on the request thread.  The bundle
cache is shared across request threads and guarded by a lock.  Nothing here
mutates the bundle directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

__all__ = [
    "SECURITY_HEADERS",
    "BundleLoad",
    "FileEntry",
    "WebAppBundle",
    "WebAppResolution",
    "WebAppSettings",
    "content_type_for",
    "handle_web_app",
    "load_bundle",
    "resolve_request",
    "same_origin",
    "same_origin_request",
    "root_from_config",
    "settings_from_config",
]

APP_PREFIX = "/app"
MANIFEST_NAME = "sonder-web-manifest.json"
CURRENT_NAME = "current.json"
RELEASES_DIR = "releases"
MANIFEST_SCHEMA = 1
BUILD_REMEDY = (
    "run scripts/build_web_app.py; see docs/wiki/03-configuration.md "
    "[server].web_app_*"
)
_CHUNK = 64 * 1024
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_FILES = 20000
_RELEASE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SEGMENT = re.compile(r"[A-Za-z0-9 ._@+~-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_ESCAPES = ("%2f", "%5c", "%00", "%25")
_FORBIDDEN_CHARS = ("\\", "\x00", ":")
_ORIGIN = re.compile(
    r"(https?)://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::([0-9]{1,5}))?\Z",
    re.IGNORECASE,
)

# CanvasKit instantiates wasm (wasm-unsafe-eval, never unsafe-eval); the engine
# injects <style> elements (style-src unsafe-inline); canvaskit.wasm is fetched
# (connect-src 'self').  Referrer-Policy stays same-origin: no-referrer would
# make browsers send "Origin: null" on same-origin POSTs.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; "
     "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
     "font-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
     "worker-src 'self' blob:; base-uri 'self'; form-action 'none'; "
     "frame-ancestors 'none'; object-src 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=()"),
)

# Explicit table: the Windows registry can map .js to text/plain.
_CONTENT_TYPES: Mapping[str, str] = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",
    "json": "application/json",
    "map": "application/json",
    "wasm": "application/wasm",
    "css": "text/css; charset=utf-8",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "ico": "image/x-icon",
    "svg": "image/svg+xml",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "txt": "text/plain; charset=utf-8",
    "frag": "application/octet-stream",
    "bin": "application/octet-stream",
}


@dataclass(frozen=True)
class WebAppSettings:
    enabled: bool
    root: Path

    @classmethod
    def disabled(cls) -> "WebAppSettings":
        return cls(False, Path())


def root_from_config(config: Any) -> Path:
    """The bundle root: ``[server].web_app_dir`` or ``<state home>/web_app``."""
    configured = str(config.server.web_app_dir or "").strip()
    if configured:
        return Path(configured)
    from sonder_runtime.platform.paths import default_home

    home = str(getattr(config.state, "home", "") or "").strip()
    return (Path(home) if home else default_home()) / "web_app"


def settings_from_config(config: Any) -> WebAppSettings:
    """Build settings from a typed ``SonderConfig`` (server and state sections)."""
    if not config.server.web_app_enabled:
        return WebAppSettings.disabled()
    return WebAppSettings(True, root_from_config(config))


@dataclass(frozen=True)
class FileEntry:
    sha256: str
    size: int


@dataclass(frozen=True)
class WebAppBundle:
    release_dir: Path
    commit: str
    files: Mapping[str, FileEntry]


@dataclass(frozen=True)
class BundleLoad:
    bundle: WebAppBundle | None = None
    code: str = ""
    remedy: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.bundle is not None

    @classmethod
    def success(cls, bundle: WebAppBundle) -> "BundleLoad":
        return cls(bundle=bundle)

    @classmethod
    def error(cls, code: str, message: str, remedy: str = BUILD_REMEDY) -> "BundleLoad":
        return cls(None, code, remedy, message)


def content_type_for(relpath: str) -> str:
    name = relpath.rsplit("/", 1)[-1]
    if "." not in name:
        return "text/plain; charset=utf-8"
    ext = name.rsplit(".", 1)[-1].lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def valid_segment(segment: str) -> bool:
    return (
        bool(segment)
        and segment not in (".", "..")
        and not segment.startswith(".")
        and _SEGMENT.fullmatch(segment) is not None
    )


def valid_relpath(relpath: object) -> bool:
    return (
        isinstance(relpath, str)
        and bool(relpath)
        and all(valid_segment(part) for part in relpath.split("/"))
    )


def _no_link(path: Path) -> bool:
    return not (path.is_symlink() or path.is_junction())


def _read_json(path: Path) -> object:
    with open(path, "rb") as handle:
        raw = handle.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("file too large")
    return json.loads(raw.decode("utf-8"))


def _sha256_of(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def load_bundle(root: Path) -> BundleLoad:
    """Read ``current.json`` and verify the named release against its manifest."""
    root = Path(root)
    not_built = "WEB_APP_NOT_BUILT"
    current = root / CURRENT_NAME
    if not current.is_file():
        return BundleLoad.error(not_built, f"no web app build found under {root}")
    try:
        pointer = _read_json(current)
    except (OSError, ValueError):
        return BundleLoad.error(not_built, "current.json is unreadable")
    name = pointer.get("release") if isinstance(pointer, dict) else None
    if (not isinstance(name, str) or _RELEASE_NAME.fullmatch(name) is None
            or name.startswith(".")):
        return BundleLoad.error(not_built, "current.json names an invalid release")
    try:
        root_resolved = root.resolve(strict=True)
        release_path = root / RELEASES_DIR / name
        if not _no_link(release_path) or not _no_link(root / RELEASES_DIR):
            return BundleLoad.error(not_built, "release path is a link")
        release_dir = release_path.resolve(strict=True)
    except OSError:
        return BundleLoad.error(not_built, "the release named by current.json is missing")
    if not release_dir.is_dir() or not release_dir.is_relative_to(root_resolved):
        return BundleLoad.error(not_built, "release is outside the web app root")
    try:
        manifest = _read_json(release_dir / MANIFEST_NAME)
    except (OSError, ValueError):
        return BundleLoad.error(not_built, "release manifest is missing or unreadable")
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        return BundleLoad.error(not_built, "release manifest has an unsupported schema")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or not 0 < len(raw_files) <= _MAX_FILES:
        return BundleLoad.error(not_built, "release manifest lists no usable files")
    files: dict[str, FileEntry] = {}
    for relpath, meta in raw_files.items():
        if (not valid_relpath(relpath) or not isinstance(meta, dict)
                or not isinstance(meta.get("sha256"), str)
                or _SHA256.fullmatch(meta["sha256"]) is None
                or type(meta.get("size")) is not int or meta["size"] < 0):
            return BundleLoad.error(not_built, f"release manifest entry invalid: {relpath!r}")
        files[relpath] = FileEntry(meta["sha256"], meta["size"])
    if "index.html" not in files:
        return BundleLoad.error(not_built, "release manifest does not list index.html")
    for relpath, entry in files.items():
        target = _safe_file(release_dir, relpath)
        if target is None:
            return BundleLoad.error(not_built, f"listed file is missing or unsafe: {relpath}")
        try:
            digest, size = _sha256_of(target)
        except OSError:
            return BundleLoad.error(not_built, f"listed file is unreadable: {relpath}")
        if size != entry.size or digest != entry.sha256:
            return BundleLoad.error(not_built, f"listed file does not match its manifest: {relpath}")
    for directory, _dirs, names in os.walk(release_dir):
        for filename in names:
            if filename.lower() != "index.html":
                continue
            rel = (Path(directory) / filename).relative_to(release_dir).as_posix()
            if rel not in files:
                return BundleLoad.error(not_built, f"unlisted index.html in release: {rel}")
    commit = manifest.get("commit")
    return BundleLoad.success(WebAppBundle(
        release_dir, commit if isinstance(commit, str) else "", files))


def _safe_file(release_dir: Path, relpath: str) -> Path | None:
    """Return the real file for ``relpath`` or None when any component is a link."""
    current = release_dir
    for segment in relpath.split("/"):
        current = current / segment
        if not _no_link(current):
            return None
    try:
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(release_dir.resolve()) or not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[tuple[int, int, int] | None, BundleLoad]] = {}


def cached_bundle(root: Path) -> BundleLoad:
    """Load once per ``current.json`` identity, so a swap needs no restart."""
    try:
        info = os.stat(Path(root) / CURRENT_NAME)
        key: tuple[int, int, int] | None = (info.st_mtime_ns, info.st_size, info.st_ino)
    except OSError:
        key = None
    cache_key = str(root)
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        if entry is not None and entry[0] == key:
            return entry[1]
        loaded = load_bundle(Path(root))
        _CACHE[cache_key] = (key, loaded)
        return loaded


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


@dataclass(frozen=True)
class WebAppResolution:
    """What to answer for one request.  ``kind`` is file, redirect,
    not_modified, not_built or not_found."""

    kind: str
    status: int = 200
    path: Path | None = None
    content_type: str = ""
    etag: str = ""
    size: int = 0
    location: str = ""
    code: str = ""
    remedy: str = ""
    message: str = ""


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def is_app_target(raw_target: str) -> bool:
    path = _path_of(raw_target)
    return path == APP_PREFIX or path.startswith(APP_PREFIX + "/")


def _path_of(raw_target: str) -> str:
    return raw_target.split("#", 1)[0].split("?", 1)[0]


def resolve_request(raw_target: str, load: BundleLoad,
                    if_none_match: str | None = None) -> WebAppResolution:
    """Map a raw request target to a response plan.  Pure apart from stat calls."""
    path = _path_of(raw_target)
    if path == APP_PREFIX:
        query = raw_target.split("#", 1)[0].partition("?")[2]
        return WebAppResolution(
            "redirect", 308, location=APP_PREFIX + "/" + (("?" + query) if query else ""))
    if not path.startswith(APP_PREFIX + "/"):
        return WebAppResolution("not_found", 404, code="NOT_FOUND", message="not found")
    if not load.ok:
        return WebAppResolution(
            "not_built", 404, code=load.code or "WEB_APP_NOT_BUILT",
            remedy=load.remedy or BUILD_REMEDY, message=load.message or "web app is not built")
    bundle = load.bundle
    assert bundle is not None
    rest = path[len(APP_PREFIX) + 1:]
    lowered = rest.lower()
    if any(token in lowered for token in _FORBIDDEN_ESCAPES) or any(
            char in rest for char in _FORBIDDEN_CHARS):
        return _not_found()
    try:
        decoded = unquote(rest, errors="strict")
    except UnicodeDecodeError:
        return _not_found()
    if any(char in decoded for char in _FORBIDDEN_CHARS):
        return _not_found()
    if decoded == "":
        relpath = "index.html"
    else:
        if decoded.endswith("/"):
            decoded = decoded[:-1]
        segments = decoded.split("/")
        if not all(valid_segment(segment) for segment in segments):
            return _not_found()
        relpath = "/".join(segments)
    if relpath not in bundle.files:
        if "." in relpath.rsplit("/", 1)[-1]:
            return _not_found()
        relpath = "index.html"
    entry = bundle.files[relpath]
    target = _safe_file(bundle.release_dir, relpath)
    if target is None:
        return _not_found()
    etag = '"' + entry.sha256 + '"'
    if _etag_matches(if_none_match, etag):
        return WebAppResolution("not_modified", 304, etag=etag)
    return WebAppResolution(
        "file", 200, path=target, content_type=content_type_for(relpath),
        etag=etag, size=entry.size)


def _not_found() -> WebAppResolution:
    return WebAppResolution("not_found", 404, code="NOT_FOUND", message="not found")


def same_origin(origin: str | None, host_header: str | None, *, https_declared: bool) -> bool:
    """True when ``Origin`` is exactly the scheme and authority of this request.

    The scheme is http, or https only when a TLS-terminating proxy is declared.
    A missing port means the scheme default on both sides.  ``Origin: null``
    never matches.  The caller must also require a trusted Host.
    """
    if not isinstance(origin, str) or not isinstance(host_header, str):
        return False
    match = _ORIGIN.fullmatch(origin.strip())
    if match is None:
        return False
    scheme = match.group(1).lower()
    if scheme == "https" and not https_declared:
        return False
    default_port = 443 if scheme == "https" else 80
    name = match.group(2).lower()
    if name.startswith("["):
        name = name[1:-1]
    port = int(match.group(3)) if match.group(3) else default_port
    from sonder_runtime.interfaces.http.host_policy import parse_host_header

    parsed = parse_host_header(host_header)
    if parsed is None:
        return False
    host_name, host_port = parsed
    return name == host_name and port == (host_port if host_port is not None else default_port)


def same_origin_request(headers: Any, *, https_declared: bool) -> bool:
    """``same_origin`` over a request's headers; a repeated Origin is refused."""
    getter = getattr(headers, "get_all", None)
    values = getter("Origin") if callable(getter) else None
    if values is not None and len(values) != 1:
        return False
    return same_origin(headers.get("Origin"), headers.get("Host"), https_declared=https_declared)


def _headers_for(resolution: WebAppResolution, *, cache: str) -> dict[str, str]:
    headers = dict(SECURITY_HEADERS)
    headers["Cache-Control"] = cache
    if resolution.etag:
        headers["ETag"] = resolution.etag
    return headers


def handle_web_app(handler: Any, settings: WebAppSettings) -> bool:
    """Answer a GET for ``/app`` or ``/app/...``; False when not ours or disabled."""
    if not settings.enabled:
        return False
    target = getattr(handler, "path", "")
    if not isinstance(target, str) or not is_app_target(target):
        return False
    load = cached_bundle(settings.root)
    resolution = resolve_request(target, load, handler.headers.get("If-None-Match"))
    if resolution.kind == "redirect":
        headers = _headers_for(resolution, cache="no-store")
        headers["Location"] = resolution.location
        _send_empty(handler, resolution.status, headers)
        return True
    if resolution.kind == "not_modified":
        _send_empty(handler, 304, _headers_for(resolution, cache="no-cache"))
        return True
    if resolution.kind == "file":
        _send_file(handler, resolution)
        return True
    error: dict[str, str] = {"code": resolution.code, "message": resolution.message}
    if resolution.remedy:
        error["remedy"] = resolution.remedy
    handler._send_json_payload(
        {"error": error}, status=resolution.status,
        headers=_headers_for(resolution, cache="no-store"))
    return True


def _send_empty(handler: Any, status: int, headers: Mapping[str, str]) -> None:
    try:
        handler.send_response(status)
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        handler.close_connection = True


def _send_file(handler: Any, resolution: WebAppResolution) -> None:
    assert resolution.path is not None
    try:
        with open(resolution.path, "rb") as source:
            if os.fstat(source.fileno()).st_size != resolution.size:
                handler._send_json_payload(
                    {"error": {"code": "WEB_APP_BUNDLE_MODIFIED",
                               "message": "a bundle file changed after it was verified",
                               "remedy": BUILD_REMEDY}},
                    status=500, headers=_headers_for(resolution, cache="no-store"))
                return
            headers = _headers_for(resolution, cache="no-cache")
            handler.send_response(200)
            handler.send_header("Content-Type", resolution.content_type)
            handler.send_header("Content-Length", str(resolution.size))
            for name, value in headers.items():
                handler.send_header(name, value)
            handler.end_headers()
            remaining = resolution.size
            while remaining > 0:
                chunk = source.read(min(_CHUNK, remaining))
                if not chunk:
                    handler.close_connection = True
                    return
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        handler.close_connection = True
    except OSError:
        handler.close_connection = True
