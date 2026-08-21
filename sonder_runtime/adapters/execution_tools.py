"""Compatibility shim for the packaged execution-tools adapter package.

Python resolves the sibling ``execution_tools`` package before this legacy
module. The file remains present during the migration so tracked-source
architecture checks and older tooling see a stable path; new production code
must import ``sonder_runtime.adapters.execution_tools`` as a package.
"""

from sonder_runtime.adapters.execution_tools import *  # noqa: F401,F403
