"""Serve owner-compiled, digest-bound bytecode to the managed runtime child.

The managed child runs with ``-B`` and a private ``pycache_prefix``: it never
writes bytecode, and it never reads a ``__pycache__`` file, because those are
outside the hashed manifest. Without help it therefore compiles every module
it imports on every launch.

The owner compiles the closure once, when it creates the payload, into one
file inside the owned root. That file is part of the hashed manifest, so the
payload digest covers it. The parent passes its path and SHA-256 from the
manifest; :func:`install` reads the bytes once, checks them against that
digest, and serves the entries from memory. Each entry is a CHECKED_HASH pyc,
so the standard import machinery uses it only while the source file's bytes
still match and compiles from source otherwise. Every other bytecode read
below the private prefix fails, so nothing written there is ever executed.
"""

from hashlib import sha256
import importlib.machinery as machinery
from importlib.util import cache_from_source
import marshal
import os
import sys

MAX_BYTECODE = 512 * 1024**2

# Counters for tests and diagnostics only.
stats = {"served": 0, "refused": 0}


class VerifiedBytecodeError(RuntimeError):
    """The owner-compiled bytecode does not match the launched payload."""


def _load(path, expected):
    with open(path, "rb") as stream:
        data = stream.read(MAX_BYTECODE + 1)
    if len(data) > MAX_BYTECODE or sha256(data).hexdigest() != expected:
        raise VerifiedBytecodeError("owner-compiled bytecode differs from the payload manifest")
    # Safe to unmarshal: these exact bytes were just matched against the
    # SHA-256 in the owner's payload manifest, and the result must be a plain
    # dict of str to bytes (checked below). Executing it is the point anyway.
    table = marshal.loads(data)
    if type(table) is not dict or any(
            type(key) is not str or type(value) is not bytes for key, value in table.items()):
        raise VerifiedBytecodeError("owner-compiled bytecode has an unexpected shape")
    return table


def install(binding):
    """Serve the verified bytecode named by ``[path, sha256]``; None disables it.

    Returns the number of cached modules. Refuses unless the interpreter
    already forbids bytecode writes, uses a private prefix and checks
    hash-based pycs, because those settings are what make the cache safe.
    """
    if binding is None:
        return 0
    import _imp

    prefix = sys.pycache_prefix
    if not sys.dont_write_bytecode or not prefix or _imp.check_hash_based_pycs == "never":
        raise VerifiedBytecodeError("verified bytecode requires -B, a private prefix and checked pycs")
    path, expected = binding
    table = _load(path, expected)
    cached = {cache_from_source(source): pyc for source, pyc in table.items()}
    del table
    private = os.path.normcase(os.path.abspath(prefix)).rstrip("\\/") + os.sep

    class VerifiedBytecodeLoader(machinery.SourceFileLoader):
        def get_data(self, data_path):
            pyc = cached.get(data_path)
            if pyc is not None:
                stats["served"] += 1
                return pyc
            if os.path.normcase(os.path.abspath(data_path)).startswith(private):
                stats["refused"] += 1
                raise OSError("bytecode outside the verified payload is not read")
            return super().get_data(data_path)

    details = (
        (machinery.ExtensionFileLoader, machinery.EXTENSION_SUFFIXES),
        (VerifiedBytecodeLoader, machinery.SOURCE_SUFFIXES),
        (machinery.SourcelessFileLoader, machinery.BYTECODE_SUFFIXES),
    )
    hooks = [hook for hook in sys.path_hooks
             if getattr(hook, "__name__", "") == "path_hook_for_FileFinder"]
    if len(hooks) != 1:
        raise VerifiedBytecodeError("exactly one standard file finder hook is required")
    sys.path_hooks[sys.path_hooks.index(hooks[0])] = machinery.FileFinder.path_hook(*details)
    sys.path_importer_cache.clear()
    return len(cached)
