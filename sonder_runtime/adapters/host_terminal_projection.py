"""Trusted host codec for immutable terminal state linked to a continuation.

Decode is a private persistence seam, never a model or HTTP input endpoint.
The continuation store authenticates scope and payload digest before decode.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json

from sonder_runtime.application.ports.lane_continuation import ProjectionBinding
from sonder_runtime.application.ports.terminal_output import MAX_OUTPUT_BYTES, TerminalOutputReference
from .agent_terminal_evidence import HostObservationLedger

_FAILURES = ("ERROR", "EVIDENCE_REQUIRED", "VALIDATION_FAILED", "CANCELLED")
# Match the host's actual markers, including markers without a required colon.
_FAILURE_MARKERS = (("ERROR:", "ERROR"),
                    ("VALIDATION_FAILED:", "VALIDATION_FAILED"),
                    ("EVIDENCE_REQUIRED", "EVIDENCE_REQUIRED"),
                    ("CANCELLED", "CANCELLED"))


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class _HostTerminalProjection:
    binding_value: ProjectionBinding
    ledger_bytes: bytes
    output: str
    terminal_class: str
    blockers: tuple[str, ...]
    terminal_receipt_id: str
    _issuer: object = field(repr=False, compare=False)
    output_reference: TerminalOutputReference | None = None


class TerminalProjectionCodec:
    def __init__(self, *, output_store=None, output_context=None):
        if (output_store is None) != (output_context is None) or (
                output_context is not None and not callable(output_context)):
            raise ValueError('output store requires a trusted live context provider')
        self._issuer = object()
        self._output_store = output_store
        self._output_context = output_context

    def capture(self, *, binding, ledger, output, terminal_class, blockers,
                terminal_receipt_id):
        """Called only by the host after observing the original terminal turn."""
        return self._capture(binding=binding, ledger=ledger, output=output,
            terminal_class=terminal_class, blockers=blockers,
            terminal_receipt_id=terminal_receipt_id, persist=True)

    def _capture(self, *, binding, ledger, output, terminal_class, blockers,
                 terminal_receipt_id, persist, reference=None):
        if not isinstance(binding, ProjectionBinding) or type(ledger) is not HostObservationLedger:
            raise ValueError("typed host binding and ledger required")
        binding.__post_init__()
        if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("output requires bounded inline or immutable blob storage")
        if terminal_class not in ("NORMAL",) + _FAILURES:
            raise ValueError("unknown terminal class")
        for marker, failure_class in _FAILURE_MARKERS:
            if output.lstrip().startswith(marker):
                terminal_class = failure_class
                break
        if (not isinstance(blockers, tuple) or len(blockers) > 256
                or any(not isinstance(b, str) or not 1 <= len(b.encode()) <= 256
                       for b in blockers)):
            raise ValueError("invalid completion blockers")
        if (not isinstance(terminal_receipt_id, str)
                or not 1 <= len(terminal_receipt_id.encode()) <= 256):
            raise ValueError("invalid terminal receipt")
        sealed_ledger = ledger.seal()
        # Binding and ledger must describe the same host-selected project.
        scope = json.loads(sealed_ledger)["project_scope"]
        if scope not in binding.project_roots:
            raise ValueError("parent evidence project is outside the bound roots")
        if len(output.encode()) > 16384:
            if self._output_store is None:
                raise ValueError('immutable output store unavailable')
            if persist:
                reference = self._output_store.put(binding, output,
                    context=self._output_context(binding))
            self._check_reference(binding, output, reference)
        elif reference is not None:
            raise ValueError('small output must use canonical inline encoding')
        projection = _HostTerminalProjection(binding, sealed_ledger, output,
                                             terminal_class, blockers,
                                             terminal_receipt_id, self._issuer, reference)
        self.encode(projection)  # Enforce the aggregate envelope bound now.
        return projection

    @staticmethod
    def _check_reference(binding, output, reference):
        if type(reference) is not TerminalOutputReference:
            raise ValueError('typed immutable output reference required')
        reference.__post_init__()
        if (reference.binding_sha256 != hashlib.sha256(_canonical(asdict(binding))).hexdigest()
                or reference.sha256 != hashlib.sha256(output.encode()).hexdigest()
                or reference.size_bytes != len(output.encode())):
            raise ValueError('immutable output content or binding mismatch')

    def _require(self, projection):
        if type(projection) is not _HostTerminalProjection or projection._issuer is not self._issuer:
            raise PermissionError("private host-issued projection required")

    def binding(self, projection):
        self._require(projection)
        return projection.binding_value

    def encode(self, projection):
        self._require(projection)
        value = dict(schema=1, binding=asdict(projection.binding_value),
                     ledger=json.loads(projection.ledger_bytes), output=projection.output,
                     output_sha256=hashlib.sha256(projection.output.encode()).hexdigest(),
                     terminal_class=projection.terminal_class,
                     blockers=list(projection.blockers),
                     terminal_receipt_id=projection.terminal_receipt_id)
        if projection.output_reference is not None:
            self._check_reference(projection.binding_value, projection.output,
                                  projection.output_reference)
            value['schema'] = 2
            del value['output']
            value['output_reference'] = asdict(projection.output_reference)
        payload = _canonical(value)
        if len(payload) > 65536:
            raise ValueError("terminal projection exceeds envelope bound")
        return payload

    def decode(self, payload):
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= 65536:
            raise ValueError("invalid terminal projection payload")
        try:
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError('invalid terminal projection')
            schema = value.get('schema')
            output_key = 'output_reference' if schema == 2 else 'output'
            if (set(value) != {
                    "schema", "binding", "ledger", output_key, "output_sha256",
                    "terminal_class", "blockers", "terminal_receipt_id"}
                    or type(schema) is not int or schema not in (1, 2)
                    or _canonical(value) != payload
                    or not isinstance(value["blockers"], list)
                    or not isinstance(value["binding"], dict)
                    or not isinstance(value["binding"].get("project_roots"), list)):
                raise ValueError("invalid terminal projection")
            binding_fields = dict(value["binding"])
            binding_fields["project_roots"] = tuple(binding_fields["project_roots"])
            binding = ProjectionBinding(**binding_fields)
            reference = None
            if schema == 2:
                if self._output_store is None:
                    raise ValueError('immutable output store unavailable')
                reference = TerminalOutputReference(**value['output_reference'])
                if reference.binding_sha256 != hashlib.sha256(_canonical(asdict(binding))).hexdigest():
                    raise ValueError('immutable output binding mismatch')
                output = self._output_store.get(binding, reference,
                    context=self._output_context(binding))
            else:
                output = value['output']
            if (not isinstance(output, str)
                    or hashlib.sha256(output.encode()).hexdigest() != value['output_sha256']):
                raise ValueError('terminal output digest mismatch')
            result = self._capture(
                binding=binding,
                ledger=HostObservationLedger.restore(_canonical(value["ledger"])),
                output=output, terminal_class=value["terminal_class"],
                blockers=tuple(value["blockers"]),
                terminal_receipt_id=value["terminal_receipt_id"],
                persist=False, reference=reference,
            )
            if self.encode(result) != payload:
                raise ValueError("terminal projection roundtrip mismatch")
            return result
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ValueError("invalid terminal projection payload") from None

    def parent_effects_valid(self, projection):
        self._require(projection)
        return (projection.terminal_class == "NORMAL" and not projection.blockers
                and HostObservationLedger.restore(projection.ledger_bytes)
                .resolve().parent_effects_valid)
