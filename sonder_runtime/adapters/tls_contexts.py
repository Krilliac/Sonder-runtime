"""Verifying TLS client contexts for outbound HTTPS adapters.

Building an ``ssl.SSLContext`` loads trust anchors: the platform store costs
~150-200 ms on Windows, and ``urllib.request.build_opener()`` builds one for
its default HTTPS handler eagerly, even when the URL is plain HTTP.  Openers
built per request therefore paid that load on every request.

Guarantees:

* Pinned CA bundles (:func:`bundle_https_context`) are read on every request.
  Nothing is cached, so a rotated or removed bundle takes effect on the next
  request and a missing bundle raises instead of falling back to system trust.
* The shared system context (:func:`default_https_context`) is built once and
  shared across threads; a context is never mutated after creation.  It is
  rebuilt on the next request when ``SSL_CERT_FILE`` or ``SSL_CERT_DIR``
  changes, or when the ``SSL_CERT_FILE`` file's inode, size or mtime changes.
  Changes to the OS trust store are picked up within ``_MAX_AGE_SECONDS`` or
  immediately after :func:`reset_https_contexts`.

Every context is ``CERT_REQUIRED`` with ``check_hostname`` on.
"""
from __future__ import annotations

import os
import ssl
import threading
import time
import urllib.request

__all__ = [
    "bundle_https_context",
    "default_https_context",
    "https_handler",
    "reset_https_contexts",
]

# Upper bound on how long the shared system context may outlive an OS
# trust-store change that leaves the environment and SSL_CERT_FILE untouched.
_MAX_AGE_SECONDS = 300.0

_Key = tuple["str | None", "str | None", "tuple[int, int, int] | None"]

_lock = threading.Lock()
_slot: tuple[_Key, float, ssl.SSLContext] | None = None


def _finish(context: ssl.SSLContext) -> ssl.SSLContext:
    # Match urllib/http.client's own default context.
    context.set_alpn_protocols(["http/1.1"])
    if context.post_handshake_auth is not None:
        context.post_handshake_auth = True
    return context


def _current_key() -> _Key:
    cert_file = os.environ.get("SSL_CERT_FILE")
    cert_dir = os.environ.get("SSL_CERT_DIR")
    stamp: tuple[int, int, int] | None = None
    if cert_file:
        try:
            stat = os.stat(cert_file)
        except OSError:
            stamp = None
        else:
            stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return (cert_file, cert_dir, stamp)


def default_https_context() -> ssl.SSLContext:
    """The shared platform-trust context.

    Rebuilt when ``SSL_CERT_FILE``/``SSL_CERT_DIR`` or the ``SSL_CERT_FILE``
    file stamp changes, or after ``_MAX_AGE_SECONDS``.
    """
    global _slot
    key = _current_key()
    with _lock:
        slot = _slot
    if slot is not None and slot[0] == key and time.monotonic() - slot[1] <= _MAX_AGE_SECONDS:
        return slot[2]
    # Built outside the lock; a duplicate concurrent build is harmless.
    built_at = time.monotonic()
    context = _finish(ssl.create_default_context())
    with _lock:
        _slot = (key, built_at, context)
    return context


def reset_https_contexts() -> None:
    """Drop the shared system context so the next request rebuilds it."""
    global _slot
    with _lock:
        _slot = None


def bundle_https_context(cafile: str) -> ssl.SSLContext:
    """A context that trusts exactly ``cafile`` (not the platform store).

    Built per call (about 1 ms): rotation and removal take effect on the next
    request.  ``FileNotFoundError`` / ``ssl.SSLError`` propagate; there is
    never a fallback to the system context.
    """
    return ssl.create_default_context(cafile=cafile)


def https_handler():
    """An ``HTTPSHandler`` over :func:`default_https_context`.

    Pass it to ``build_opener`` so the opener does not build its own default
    handler (and with it a fresh context) on every call.
    """
    return urllib.request.HTTPSHandler(context=default_https_context())
