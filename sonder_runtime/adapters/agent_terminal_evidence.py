"""Bounded internal host observations for restart-safe terminal projection.

These records are host inputs, not an authentication mechanism. Restore must
only receive bytes from the continuation's scoped, digest-checked private
projection. Neither this ledger nor its resolver is a model tool.
"""

from dataclasses import dataclass
import json

from .agent_work_coverage import validation_covers, verification_covers

_MAX_BYTES = 48 * 1024
_MAX_RECORDS = 256


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class ParentEvidenceVerdict:
    dirty: bool
    validation_attempted: bool
    validation_ok: bool
    verification_ok: bool

    @property
    def parent_effects_valid(self):
        # Terminal failure dominance and completion blockers belong to the
        # enclosing host projection. This is only the parent effects predicate.
        return not self.dirty or self.validation_ok or self.verification_ok


class HostObservationLedger:
    def __init__(self, *, project_scope):
        if not isinstance(project_scope, str) or len(project_scope.encode()) > 4096:
            raise ValueError("invalid project scope")
        self._scope = project_scope
        self._records = []
        self._poisoned = False

    def observe(self, *, tool, arguments, observation, dispatched, success,
                dirty=False, mutation_records=(), verifier=False, validator=False):
        """Capture host-classified facts; snapshot mutable arguments immediately.

        Dirty means a dispatched effect may have happened, including failure.
        It deliberately does not assert that a persistent delta occurred.
        """
        try:
            if self._poisoned:
                raise ValueError("host evidence is incomplete")
            if (not isinstance(tool, str) or not 1 <= len(tool) <= 128
                    or not isinstance(arguments, dict)
                    or not isinstance(observation, str)
                    or any(type(v) is not bool for v in
                           (dispatched, success, dirty, verifier, validator))
                    or (dirty and not dispatched)):
                raise ValueError("invalid host observation")
            mutations = list(mutation_records)
            if any(not isinstance(m, dict) or set(m) - {"tool", "path", "source"}
                   or not {"tool", "path"} <= set(m)
                   or any(not isinstance(v, str) for v in m.values())
                   for m in mutations):
                raise ValueError("invalid mutation record")
            record = dict(tool=tool, arguments=arguments, observation=observation,
                          dispatched=dispatched, success=success, dirty=dirty,
                          mutations=mutations, verifier=verifier, validator=validator)
            frozen = json.loads(_canonical(record))
            candidate = self._records + [frozen]
            if len(candidate) > _MAX_RECORDS or len(self._encode(candidate)) > _MAX_BYTES:
                raise ValueError("host evidence bound exceeded")
            self._records = candidate
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._poisoned = True
            raise ValueError("host evidence unavailable") from None

    def _encode(self, records):
        return _canonical(dict(policy=1, project_scope=self._scope, records=records))

    def seal(self):
        if self._poisoned:
            raise ValueError("host evidence is incomplete")
        return self._encode(self._records)

    @classmethod
    def restore(cls, payload):
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= _MAX_BYTES:
            raise ValueError("invalid host evidence payload")
        try:
            value = json.loads(payload)
            if (not isinstance(value, dict)
                    or set(value) != {"policy", "project_scope", "records"}
                    or type(value["policy"]) is not int or value["policy"] != 1
                    or not isinstance(value["records"], list)
                    or _canonical(value) != payload):
                raise ValueError("unsupported host evidence")
            ledger = cls(project_scope=value["project_scope"])
            for record in value["records"]:
                if not isinstance(record, dict) or set(record) != {
                    "tool", "arguments", "observation", "dispatched", "success",
                    "dirty", "mutations", "verifier", "validator",
                }:
                    raise ValueError("invalid host evidence record")
                ledger.observe(**{k: v for k, v in record.items() if k != "mutations"},
                               mutation_records=record["mutations"])
            if ledger.seal() != payload:
                raise ValueError("host evidence roundtrip mismatch")
            return ledger
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ValueError("invalid host evidence payload") from None

    def resolve(self):
        self.seal()  # Refuse a truncated or otherwise incomplete record set.
        dirty = attempted = validation_ok = verification_ok = False
        mutations = []
        for record in self._records:
            if record["dirty"]:
                dirty = True
                attempted = validation_ok = verification_ok = False
            for mutation in record["mutations"]:
                if mutation not in mutations:
                    mutations.append(mutation)
            if record["verifier"]:
                attempted = True
                verification_ok = bool(record["dispatched"] and record["success"]
                    and verification_covers(record["tool"], record["arguments"],
                                            mutations, self._scope))
            if record["validator"]:
                attempted = True
                validation_ok = bool(record["dispatched"] and record["success"]
                    and validation_covers(record["tool"], record["arguments"],
                                          mutations, record["observation"]))
        return ParentEvidenceVerdict(dirty, attempted, validation_ok, verification_ok)
