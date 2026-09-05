"""Host-only composition for managed parent admission.

This does not select a host conversation or grant authority. The launcher must
first obtain a BoundContinuation through the authenticated host selection flow.
Inventory callbacks belong to application composition, never model arguments.
"""

from dataclasses import dataclass, replace
import math
from pathlib import Path

from sonder_runtime.adapters.host_terminal_result import TerminalResultCodec
from sonder_runtime.application.ports.lane_continuation import TerminalProjectionReceipt
from sonder_runtime.application.ports.delegated_verification import VerificationVerdict


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


@dataclass(frozen=True)
class PublishedHostTerminal:
    output: str
    valid: bool
    verdict: VerificationVerdict
    receipt: TerminalProjectionReceipt


class HostTerminalPublisher:
    """Reload original evidence and publish only after the backend result CAS.

    One instance belongs to one current private attachment. Reattachment creates
    a new publisher; it never reconstructs authority from a persisted principal.
    Composition installs ``codec`` as that continuation service's result codec.
    This component does not approve work, run checks, or generate replacement text.
    """

    def __init__(self, *, bound, verifier, original_codec, require_current):
        if not callable(require_current):
            raise ValueError('live host admission required')
        self._bound = bound
        self._verifier = verifier
        self._original = original_codec
        self._admit = require_current
        self.codec = TerminalResultCodec(original_codec=original_codec,
            load_original=self._load, prepared_verification=self._prepared,
            current_verdict=self._verdict, certificate=self._certificate)

    def _identity(self, binding=None):
        self._admit()
        self._bound.require_current()
        identity = self._bound.pending_verification()
        if identity is None:
            raise PermissionError('original terminal verification unavailable')
        if binding is not None:
            original = self._bound.terminal_projection(identity)
            if self._original.binding(original) != binding:
                raise PermissionError('terminal publication binding mismatch')
        return identity

    def _load(self, binding):
        return self._bound.terminal_projection(self._identity(binding))

    def _prepared(self, binding):
        return self._bound.prepared_verification(self._identity(binding))

    def _verdict(self, binding):
        identity = self._identity(binding)
        return self._bound.verification_view(self._verifier,
            identity.verification_id, action='validate')

    def _certificate(self, binding):
        identity = self._identity(binding)
        view = self._bound.verification_view(self._verifier,
            identity.verification_id, action='inspect')
        certificate = view.get('certificate')
        expected_keys = {'id', 'bundle', 'approval_id', 'before_manifest_digest',
            'after_manifest_digest', 'manifest_policy', 'cleanup_proofs', 'created_at'}
        if not isinstance(certificate, dict) or set(certificate) != expected_keys:
            raise PermissionError('complete verifier certificate required')
        manifests = (certificate['before_manifest_digest'], certificate['after_manifest_digest'])
        if (any(not isinstance(d, str) or len(d) != 64
                or any(c not in '0123456789abcdef' for c in d) for d in manifests)
                or manifests[0] != manifests[1]
                or not isinstance(certificate['approval_id'], str)
                or not 1 <= len(certificate['approval_id']) <= 256
                or not isinstance(certificate['manifest_policy'], dict)
                or not isinstance(certificate['cleanup_proofs'], list)
                or len(certificate['cleanup_proofs']) != len(self._prepared(binding).checks)
                or type(certificate['created_at']) not in (int, float)
                or not math.isfinite(certificate['created_at'])
                or certificate['created_at'] <= 0):
            raise PermissionError('invalid verifier certificate structure')
        return certificate

    def publish(self):
        identity = self._identity()
        original = self._bound.terminal_projection(identity)
        result = self.codec.capture(original)
        self._admit()
        receipt = self._bound.commit_terminal_projection(
            identity, identity.projection_revision, result)
        # A storage error or lost response propagates. No success-shaped object
        # escapes before the authoritative immutable result receipt is returned.
        return PublishedHostTerminal(result.output, result.valid, result.verdict, receipt)
