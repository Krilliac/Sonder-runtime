"""Public compatibility surface for the packaged Ollama lifecycle adapter.

The implementation lives in ``sonder_runtime``.  Keep this root module only
for callers that still import the historical path, and expose public helpers
explicitly so private implementation and platform hooks cannot become a
second supported API by accident.
"""

from sonder_runtime.adapters.ollama_lifecycle import (
    cleanup_orphaned_discovery_probes,
    resident_models,
)

__all__ = (
    "cleanup_orphaned_discovery_probes",
    "resident_models",
)
