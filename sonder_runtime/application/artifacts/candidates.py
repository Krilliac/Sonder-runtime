"""Producer seals and fan-in for the parallel generate/verify tools."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading
import uuid

from .fanin import readiness_error
from .readiness import ArtifactReadiness


def _content(result):
    return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CandidateOutput:
    result: dict
    readiness: ArtifactReadiness | None
    verifier_receipt: str


class CandidateFanIn:
    """Seal after the existing verifier returns, then check before aggregation."""

    def __init__(self, prompt: str, check: str):
        self.run_id = uuid.uuid4().hex
        self.source_revision = hashlib.sha256(_content([prompt, check]).encode("utf-8")).hexdigest()

    @staticmethod
    def bind_generator(generator):
        # parallel_generate_run shares one closure. Its public last-response
        # field is not per-call evidence when sibling threads are returning.
        # The tier wrapper (_TierGenerator) forwards reads to ``raw`` but not
        # writes, and the raw closure is what records each reply, so bind the
        # slot there; the check below reads it back through the wrapper.
        getattr(generator, "raw", generator)._response_metadata_local = threading.local()

    @staticmethod
    def require_complete_generation(generator):
        local = getattr(generator, "_response_metadata_local", None)
        metadata = getattr(local, "value", getattr(generator, "last_response_meta", {})) or {}
        if metadata.get("done_reason") in ("length", "max_tokens", "max_output_tokens"):
            raise ValueError("provider output is truncated")

    def produce(self, worker, *args):
        result = worker(*args)
        receipt = ""
        evidence = None
        if result.get("ok") and result.get("code"):
            receipt = hashlib.sha256(_content({
                "ok": True, "output": result.get("output"),
                "source_revision": self.source_revision,
            }).encode("utf-8")).hexdigest()
            evidence = ArtifactReadiness.from_content(
                str(result["index"]), self.run_id, _content(result),
                source_revision=self.source_revision, deterministic_verifier="grounding.run_code",
                verifier_receipt=receipt,
            )
        return CandidateOutput(result, evidence, receipt)

    def consume(self, output: CandidateOutput, index: int):
        result = output.result
        if not result.get("ok") and output.readiness is None and not output.verifier_receipt:
            # A failed candidate is not an artifact: the producer never sealed
            # it and winner selection already requires ``ok``. Keep its public
            # diagnostics byte-identical to the pre-fan-in tool output.
            return result
        error = readiness_error(
            output.readiness, run_id=self.run_id, producer_id=str(index), content=_content(result),
            source_revision=self.source_revision, verifier_receipt=output.verifier_receipt,
            require_verifier_receipt=True,
        )
        if not error:
            return result
        # Do not carry rejected code/response bytes into winner selection or
        # aggregation. Failed verifier diagnostics remain visible as failures.
        diagnostic = str(result.get("output") or "") if not result.get("ok") else ""
        return {
            "index": index, "name": result.get("name", "candidate-%d" % (index + 1)),
            "language": result.get("language"), "ok": False, "seconds": 0,
            "output": (diagnostic + "\n" if diagnostic else "") + "[NOT READY: %s]" % error,
        }
