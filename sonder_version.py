"""Release-tooling compatibility surface for the runtime build identity.

Release validation parses this file without importing it, so ``VERSION`` must
remain a literal assignment.  Runtime implementation ownership is in
``sonder_runtime.platform.version``; the module alias below also preserves
legacy module identity for callers that still import this root name.
"""
from __future__ import annotations

# Bumped by the release process.  The ".dev0" suffix marks a build taken
# from a mutable source checkout rather than a stamped release artifact.
VERSION = "0.9.0.dev0"

import sys

from sonder_runtime.platform import version as _canonical

BuildInfo = _canonical.BuildInfo
build_info = _canonical.build_info
running_source_commit_at_import = _canonical.running_source_commit_at_import
_commit_from_git = _canonical._commit_from_git

# Imports of ``sonder_version`` receive the canonical module object, matching
# the identity-preserving compatibility pattern used by other migrated root
# platform modules.  The literal above remains available to AST-based release
# tooling even though normal runtime imports resolve to the packaged module.
sys.modules[__name__] = _canonical
