"""Run a sealed Python script from an inherited descriptor as ``__main__``.

``workbench.run_script`` launches this file as::

    <runtime python> -P sealed_python_main.py <fd> <script path> [args...]

when an enforcing execution-risk policy requires that the bytes it inspected
are the bytes that run.  ``<fd>`` is a sealed memfd holding those bytes; this
bootstrap reads the code only from that descriptor, never from ``<script
path>``.  The path supplies the ordinary script semantics: ``__file__``,
``sys.argv[0]``, ``sys.path[0]`` (``-P`` keeps this bootstrap's own directory
off ``sys.path``) and traceback file names.  Differences from ``python
<script>``: ``sys.flags.safe_path`` is set, ``__loader__`` is ``None``, and an
uncaught exception's traceback begins with this bootstrap's frame.

Only the standard library is imported, and nothing is imported between
reading the descriptor and executing the code.
"""

import builtins
import os
import sys
import types


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        raise SystemExit("usage: sealed_python_main.py FD SCRIPT [ARGS...]")
    descriptor = int(argv[1])
    origin = argv[2]
    with os.fdopen(descriptor, "rb") as handle:
        source = handle.read()
    code = compile(source, origin, "exec", dont_inherit=True)
    module = types.ModuleType("__main__")
    module.__dict__.update({
        "__file__": origin,
        "__builtins__": builtins,
        "__cached__": None,
        "__loader__": None,
        "__package__": None,
        "__spec__": None,
    })
    sys.argv = [origin, *argv[3:]]
    sys.path.insert(0, os.path.dirname(origin))
    sys.modules["__main__"] = module
    exec(code, module.__dict__)


if __name__ == "__main__":
    main(sys.argv)
