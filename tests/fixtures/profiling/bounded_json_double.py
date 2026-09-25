"""Stand-in for ``sonder_runtime.domain.common.bounded_json`` (owned by lane A).

Lane B (profiling) codes against lane A's ``iter_array_objects``. Until lane
A's module is in the same tree, ``install()`` registers a verbatim copy of it
(``lane_a_bounded_json.py``, from lane A commit 254015da) under the real module
name, so these tests run against the real scanner: ``JsonBoundsExceeded`` (an
``InvalidInput``, not a ``ValueError``) and the ``skipped_oversize`` /
``skipped_invalid`` / ``truncated`` counters. Once the real module exists it is
used unchanged and this stand-in is inert.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_NAME = "sonder_runtime.domain.common.bounded_json"
_COPY = Path(__file__).with_name("lane_a_bounded_json.py")


def install() -> types.ModuleType:
    """Return the real bounded_json when present, else register lane A's copy."""
    try:
        from sonder_runtime.domain.common import bounded_json  # type: ignore[attr-defined]
        return bounded_json
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location(_NAME, _COPY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.SONDER_TEST_DOUBLE = True  # type: ignore[attr-defined]
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
    import sonder_runtime.domain.common as common

    common.bounded_json = module  # type: ignore[attr-defined]
    return module
