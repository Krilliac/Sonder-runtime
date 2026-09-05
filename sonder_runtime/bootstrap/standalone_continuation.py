"""Host-only composition for managed parent admission.

This does not select a host conversation or grant authority. The launcher must
first obtain a BoundContinuation through the authenticated host selection flow.
Inventory callbacks belong to application composition, never model arguments.
"""

from dataclasses import replace
from pathlib import Path


class HostContinuationAdmission:
    """Carry the attachment ceiling into each actual parent operation.

Private stores are checked against the complete live model-root inventory as
well as this parent's narrower context. Callers still need the application's
transactional effect admission and anchored persistence adapters.
"""

    def __init__(self, bound, context, *, private_paths, model_writable_roots):
        if not callable(private_paths) or not callable(model_writable_roots):
            raise ValueError('live private-store and model-root inventories required')
        self._bound = bound
        self._private_paths = private_paths
        self._model_roots = model_writable_roots
        ceiling = bound.authority_ceiling()
        roots = set()
        for fresh in context.workspace_roots:
            fresh = Path(fresh).resolve()
            for allowed in ceiling.workspace_roots:
                allowed = Path(allowed).resolve()
                if fresh.is_relative_to(allowed):
                    roots.add(fresh)
                elif allowed.is_relative_to(fresh):
                    roots.add(allowed)
        if not roots:
            raise PermissionError('fresh host context has no permitted workspace')
        # Preserve the authenticated source, principal and cancellation object.
        # require_current rejects a different host context rather than repairing it.
        self._context = replace(context,
            workspace_roots=tuple(sorted(roots)),
            cloud_allowed=context.cloud_allowed and ceiling.cloud_allowed,
            remote_ollama_allowed=(context.remote_ollama_allowed
                                   and ceiling.remote_ollama_allowed),
            deadline_monotonic=min(context.deadline_monotonic,
                                   ceiling.deadline_monotonic)
                if context.deadline_monotonic is not None else ceiling.deadline_monotonic)
        self.require_current()

    @property
    def context(self):
        self.require_current()
        return self._context

    @staticmethod
    def _paths(values):
        # Preserve lexical locations as well as resolved aliases. Neither a
        # relocated store nor a symlinked model root may hide an overlap.
        if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 4096:
            raise PermissionError('private-store/model-root inventory unavailable')
        paths = set()
        for value in values:
            path = Path(value)
            if not path.is_absolute():
                raise PermissionError('absolute host inventory paths required')
            paths.add(path.absolute())
            paths.add(path.resolve())
        return paths

    def require_current(self, *, context=None):
        actual = self._context if context is None else context
        self._bound.require_current(context=actual)
        try:
            private = self._paths(self._private_paths())
            roots = self._paths(self._model_roots()) | self._paths(actual.workspace_roots)
            if any(p.is_relative_to(root) or root.is_relative_to(p)
                   for p in private for root in roots):
                raise PermissionError('model workspace overlaps private control-plane state')
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise PermissionError('host private-store inventory unavailable') from exc
        # Inventory providers can involve I/O; recheck live revocation afterward.
        self._bound.require_current(context=actual)

    def invoke(self, operation, /, *args, **kwargs):
        """Run one host-selected operation with the checked immutable context."""
        self.require_current()
        return operation(self._context, *args, **kwargs)
