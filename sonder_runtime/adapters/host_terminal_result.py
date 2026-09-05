"""Private once-only terminal result codec; original evidence stays immutable.

Capture obtains fresh workspace evidence. Decode restores a historical result
from scoped private storage; it never grants authority for a later effect.
"""

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json

from sonder_runtime.application.ports.delegated_verification import PreparedVerification, VerificationVerdict, digest
from sonder_runtime.application.ports.lane_continuation import ProjectionBinding
from .host_terminal_projection import _canonical


@dataclass(frozen=True)
class _TerminalResult:
    binding_value: ProjectionBinding
    original_digest: str
    verdict: VerificationVerdict
    certificate_sha256: str
    output: str
    valid: bool
    _issuer: object = field(repr=False, compare=False)


class TerminalResultCodec:
    def __init__(self, *, original_codec, load_original, prepared_verification, current_verdict, certificate):
        if not all(callable(f) for f in (load_original, prepared_verification, current_verdict, certificate)):
            raise ValueError("trusted original, prepared bundle and current verifier required")
        self._original_codec = original_codec
        self._load = load_original
        self._prepared = prepared_verification
        self._verify = current_verdict
        self._certificate = certificate
        self._issuer = object()

    def _validated(self, binding, verdict):
        prepared = self._prepared(binding)
        if (type(prepared) is not PreparedVerification
                or prepared.verification_id != binding.verification_id
                or prepared.bundle_digest != binding.bundle_digest
                or prepared.principal_id != binding.principal_id
                or prepared.parent_session_id != binding.parent_session_id
                or prepared.parent_grant_revision != binding.parent_grant_revision
                or prepared.roots != binding.project_roots
                or type(verdict) is not VerificationVerdict
                or verdict.valid is not True or verdict.code != "CERTIFIED"
                or verdict.certificate_id != prepared.verification_id
                or type(verdict.generation) is not int or verdict.generation != prepared.generation
                or verdict.parent_session_id != prepared.parent_session_id
                or type(verdict.parent_grant_revision) is not int
                or verdict.parent_grant_revision != prepared.parent_grant_revision
                or verdict.roots != prepared.roots or verdict.children != prepared.children):
            raise PermissionError("fresh exact delegated certificate required")

    def _build(self, binding, original_digest, verdict, certificate_sha256):
        if (not isinstance(certificate_sha256, str) or len(certificate_sha256) != 64
                or any(c not in "0123456789abcdef" for c in certificate_sha256)):
            raise ValueError("invalid complete certificate digest")
        original = self._load(binding)
        if self._original_codec.binding(original) != binding:
            raise PermissionError("original terminal binding mismatch")
        actual = hashlib.sha256(self._original_codec.encode(original)).hexdigest()
        if actual != original_digest:
            raise PermissionError("original terminal evidence changed")
        self._validated(binding, verdict)
        return _TerminalResult(replace(binding, revision=binding.revision + 1),
            original_digest, verdict, certificate_sha256, original.output,
            self._original_codec.parent_effects_valid(original), self._issuer)

    def capture(self, original):
        binding = self._original_codec.binding(original)
        original_digest = hashlib.sha256(self._original_codec.encode(original)).hexdigest()
        verdict = self._verify(binding)
        certificate = self._certificate(binding)
        if (not isinstance(certificate, dict) or certificate.get("id") != binding.verification_id
                or certificate.get("bundle") != self._prepared(binding).approval_payload()
                or len(_canonical(certificate)) > 1048576):
            raise PermissionError("complete exact verifier certificate unavailable")
        result = self._build(binding, original_digest, verdict, digest(certificate))
        self.encode(result)
        return result

    def _require(self, result):
        if type(result) is not _TerminalResult or result._issuer is not self._issuer:
            raise PermissionError("private host-issued terminal result required")

    def binding(self, result):
        self._require(result)
        return result.binding_value

    def certificate_digest(self, result):
        self._require(result)
        return result.certificate_sha256

    def encode(self, result):
        self._require(result)
        payload = _canonical(dict(schema=1, binding=asdict(result.binding_value),
            original_digest=result.original_digest, verdict=asdict(result.verdict),
            certificate_digest=result.certificate_sha256))
        if len(payload) > 65536:
            raise ValueError("terminal result exceeds envelope bound")
        return payload

    def decode(self, payload):
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= 65536:
            raise ValueError("invalid terminal result payload")
        try:
            value = json.loads(payload)
            if (not isinstance(value, dict)
                    or set(value) != {"schema", "binding", "original_digest", "verdict", "certificate_digest"}
                    or type(value["schema"]) is not int or value["schema"] != 1
                    or _canonical(value) != payload):
                raise ValueError("invalid terminal result schema")
            fields = dict(value["binding"])
            if not isinstance(fields["project_roots"], list):
                raise ValueError("invalid result roots")
            fields["project_roots"] = tuple(fields["project_roots"])
            binding = ProjectionBinding(**fields)
            if binding.revision < 2:
                raise ValueError("invalid terminal result revision")
            evidence = dict(value["verdict"])
            if not isinstance(evidence["roots"], list) or not isinstance(evidence["children"], list):
                raise ValueError("invalid certificate scope")
            evidence["roots"] = tuple(evidence["roots"])
            evidence["children"] = tuple(tuple(child) for child in evidence["children"])
            result = self._build(replace(binding, revision=binding.revision - 1),
                                 value["original_digest"], VerificationVerdict(**evidence),
                                 value["certificate_digest"])
            if self.encode(result) != payload:
                raise ValueError("terminal result roundtrip mismatch")
            return result
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ValueError("invalid terminal result payload") from None
