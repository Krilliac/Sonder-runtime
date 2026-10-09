"""Shared, verifying TLS client contexts for outbound HTTPS adapters.

Building an ``ssl.SSLContext`` loads trust anchors: the platform store costs
~150-200 ms on Windows, and ``urllib.request.build_opener()`` builds one for
its default HTTPS handler eagerly, even when the URL is plain HTTP.  Openers
built per request therefore paid that load on every request.  These helpers
build each context once and hand out the same one; a context is never
mutated after creation, so threads share it safely.

Neither helper weakens verification: both are ``CERT_REQUIRED`` with
``check_hostname`` on.  Cache keys follow what the uncached code read at
call time, so configuration changes still take effect on the next request:
the OpenSSL trust-path variables for the default context, and the bundle
file's path, size and mtime for an explicit CA bundle.
"""
from __future__ import annotations

import functools
import os
import ssl
import urllib.request

__all__ = ["bundle_https_context", "default_https_context", "https_handler"]


def _finish(context: ssl.SSLContext) -> ssl.SSLContext:
    # Match urllib/http.client's own default context.
    context.set_alpn_protocols(["http/1.1"])
    if context.post_handshake_auth is not None:
        context.post_handshake_auth = True
    return context


def default_https_context() -> ssl.SSLContext:
    """The platform-trust context, keyed by ``SSL_CERT_FILE``/``SSL_CERT_DIR``."""
    return _default(os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR"))


@functools.lru_cache(maxsize=4)
def _default(_cert_file: str | None, _cert_dir: str | None) -> ssl.SSLContext:
    return _finish(ssl.create_default_context())


def bundle_https_context(cafile: str) -> ssl.SSLContext:
    """A context that trusts exactly ``cafile`` (not the platform store).

    Keyed by the file's identity and content stamp, so a rotated bundle is
    picked up on the next request.
    """
    stat = os.stat(cafile)
    return _bundle(os.path.abspath(cafile), stat.st_size, stat.st_mtime_ns)


@functools.lru_cache(maxsize=8)
def _bundle(cafile: str, _size: int, _mtime_ns: int) -> ssl.SSLContext:
    # Exactly what the uncached Ollama path built (no ALPN change).
    return ssl.create_default_context(cafile=cafile)


def https_handler():
    """An ``HTTPSHandler`` over :func:`default_https_context`.

    Pass it to ``build_opener`` so the opener does not build its own default
    handler (and with it a fresh context) on every call.
    """
    return urllib.request.HTTPSHandler(context=default_https_context())
