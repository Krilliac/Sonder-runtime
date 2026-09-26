"""Evaluator-held independent oracle for unattended selfmod candidates (#517).

The public held-out suites and the literal assertions projected from them
(``scripts/selfmod_host_grader.py``) are readable by the candidate, and the
candidate process writes the stdout the parent grades.  A candidate can
therefore print a forged ``SELFMOD HOST CHALLENGE RESULT`` frame or a forged
pytest summary carrying the public expected values.  This module is the
pure half of the oracle that closes that gap:

* :class:`HeldCase` is one held-out functional case.  Its expected outcome
  lives only in an evaluator-owned case set that the candidate uid cannot
  read (``scripts/selfmod_oracle.py`` owns storage and proves the denial).
* :func:`new_challenge` picks a fresh per-run nonce, one fresh token per
  case, and a random presentation order.  The candidate process receives
  only ``(token, args, kwargs)`` triples and the nonce, never an expected
  value.
* :func:`grade_frame` runs in the evaluator (parent) process.  It accepts
  exactly one bounded frame that echoes this run's nonce and exactly this
  run's token set, and compares every raw per-case output with the held
  expected outcome.  Exit codes and pytest summaries are never inputs.
* :func:`ledger_command` and :func:`ledger_output` are the only forms of
  the probe's argv and output the selfmod ledger stores: nonce and digests,
  never the held inputs or the raw outputs, so no ledger read (``/selfmod
  tests``, the HTTP read surface) yields an input -> expected-value table a
  later candidate could replay by input.
* :class:`OracleReceipt` is the durable decision receipt.  It binds the
  verdict to the exact tested candidate digests, the baseline (starting
  commit and sealed backup manifest), the case-set digest, the challenge
  nonce and the probe's isolation attestation.  ``independent`` is true only
  when the probe ran under the ``linux-uid`` supervisor and the parent
  proved, at the OS boundary, that the candidate uid cannot read the case
  set.

The module is pure: no filesystem, subprocess, environment or clock access.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from sonder_runtime.application.selfmod.candidate_isolation import ISOLATION_KINDS, LINUX_UID

ORACLE_FRAME_PREFIX = "SELFMOD ORACLE OUTPUTS "
WITHHELD_OUTPUT_PREFIX = "SELFMOD ORACLE WITHHELD "
# The ledger kind of the candidate-side probe; its row is always redacted
# (``ledger_command`` / ``ledger_output``).
ORACLE_PROBE_KIND = "oracle_probe"
CASE_SET_VERSION = 1
RECEIPT_VERSION = 1
MAX_ORACLE_CASES = 64
# Both the challenge payload and the result frame stay well inside the
# supervisors' retained output tail (120 000 bytes) and the ledger's
# 100 000-character output column, so truncation can never manufacture or
# hide a frame.
MAX_ORACLE_PAYLOAD_BYTES = 65_536
# A case set must fit the channel for any candidate root up to PATH_MAX, so a
# provisioned set can never make the challenge or a correct frame overflow.
MAX_CHALLENGE_ROOT_BYTES = 4096
_NONCE_HEX = 32
_TOKEN_HEX = 24
_OUTCOME_KEYS = frozenset({"value", "raised"})


class OracleError(ValueError):
    """A case set, challenge or receipt violates the oracle contract."""


def canonical_json(value: Any) -> str:
    """One stable JSON spelling, used for every comparison and digest."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _encodable(value: Any, what: str) -> Any:
    try:
        canonical_json(value)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise OracleError(f"{what} is not strict JSON") from exc
    return value


def _dotted_identifier(name: object) -> bool:
    return (isinstance(name, str) and bool(name)
            and all(part.isidentifier() for part in name.split(".")))


@dataclass(frozen=True, slots=True)
class HeldCase:
    """One held-out case: inputs plus the outcome only the evaluator knows."""

    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]
    outcome: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.kwargs, Mapping) or any(
                not isinstance(key, str) for key in self.kwargs):
            raise OracleError("held case kwargs must map names to values")
        if not isinstance(self.outcome, Mapping) or len(self.outcome) != 1 \
                or not set(self.outcome) <= _OUTCOME_KEYS:
            raise OracleError("held case outcome must be exactly one of value/raised")
        raised = self.outcome.get("raised")
        if "raised" in self.outcome and not (isinstance(raised, str) and raised.isidentifier()):
            raise OracleError("held case raised outcome must name an exception class")
        _encodable(list(self.args), "held case args")
        _encodable(dict(self.kwargs), "held case kwargs")
        _encodable(dict(self.outcome), "held case outcome")

    @classmethod
    def from_mapping(cls, raw: object) -> "HeldCase":
        if not isinstance(raw, Mapping):
            raise OracleError("held case must be an object")
        keys = set(raw)
        if not keys <= {"args", "kwargs", "expected", "raises"} or not {"args"} <= keys:
            raise OracleError("held case has unknown or missing fields")
        if ("expected" in raw) == ("raises" in raw):
            raise OracleError("held case needs exactly one of expected/raises")
        args = raw["args"]
        kwargs = raw.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, Mapping):
            raise OracleError("held case args must be a list and kwargs an object")
        outcome = ({"value": raw["expected"]} if "expected" in raw
                   else {"raised": raw["raises"]})
        return cls(tuple(args), dict(kwargs), outcome)

    def inputs_json(self) -> str:
        return canonical_json({"args": list(self.args), "kwargs": dict(self.kwargs)})

    def to_mapping(self) -> dict[str, Any]:
        mapped: dict[str, Any] = {"args": list(self.args), "kwargs": dict(self.kwargs)}
        if "value" in self.outcome:
            mapped["expected"] = self.outcome["value"]
        else:
            mapped["raises"] = self.outcome["raised"]
        return mapped


@dataclass(frozen=True, slots=True)
class CaseSet:
    """The held-out cases for one ``module.function`` target."""

    module: str
    function: str
    cases: tuple[HeldCase, ...]

    def __post_init__(self) -> None:
        if not _dotted_identifier(self.module) or not str(self.function).isidentifier():
            raise OracleError("case set must name an importable module and function")
        if not 1 <= len(self.cases) <= MAX_ORACLE_CASES:
            raise OracleError(f"case set must hold 1..{MAX_ORACLE_CASES} cases")
        seen = set()
        for case in self.cases:
            if not isinstance(case, HeldCase):
                raise OracleError("case set entries must be held cases")
            key = case.inputs_json()
            if key in seen:
                raise OracleError("case set repeats an input")
            seen.add(key)
        self._require_channel_fits()

    def _require_channel_fits(self) -> None:
        """Refuse a set whose challenge or all-correct frame exceeds the bound.

        Measured with worst-case overhead: a PATH_MAX candidate root, a
        full-length nonce and tokens, and the probe's own JSON spelling of
        the frame (``json.dumps`` with ASCII escapes and default
        separators), which is what the grader measures.
        """
        tokens = [format(index, "0%dx" % _TOKEN_HEX) for index in range(len(self.cases))]
        challenge = canonical_json({
            "root": "r" * MAX_CHALLENGE_ROOT_BYTES, "module": self.module,
            "function": self.function, "nonce": "n" * _NONCE_HEX,
            "cases": [{"token": token, "args": list(case.args), "kwargs": dict(case.kwargs)}
                      for token, case in zip(tokens, self.cases)],
        })
        if len(challenge.encode("utf-8")) > MAX_ORACLE_PAYLOAD_BYTES:
            raise OracleError("case set inputs exceed the oracle challenge bound")
        frame = json.dumps({
            "nonce": "n" * _NONCE_HEX,
            "outputs": {token: dict(case.outcome) for token, case in zip(tokens, self.cases)},
        }, sort_keys=True, allow_nan=False)
        if len(frame.encode("utf-8")) > MAX_ORACLE_PAYLOAD_BYTES:
            raise OracleError("case set expected outputs exceed the oracle result frame bound")

    @classmethod
    def parse(cls, raw: object) -> "CaseSet":
        if not isinstance(raw, Mapping) or set(raw) != {"version", "module", "function", "cases"}:
            raise OracleError("case set must carry exactly version/module/function/cases")
        if raw["version"] != CASE_SET_VERSION:
            raise OracleError("unsupported case set version")
        cases = raw["cases"]
        if not isinstance(cases, list):
            raise OracleError("case set cases must be a list")
        return cls(str(raw["module"]), str(raw["function"]),
                   tuple(HeldCase.from_mapping(item) for item in cases))

    def to_json(self) -> str:
        return canonical_json({
            "version": CASE_SET_VERSION, "module": self.module, "function": self.function,
            "cases": [case.to_mapping() for case in self.cases],
        })


@dataclass(frozen=True, slots=True)
class OracleChallenge:
    """Per-run challenge: nonce, one token per presented case, and the order.

    ``order[i]`` is the index of the held case presented at position ``i``
    under ``tokens[i]``.  Tokens and the nonce are fresh random values, so a
    frame recorded for any earlier challenge names neither.
    """

    nonce: str
    tokens: tuple[str, ...]
    order: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.nonce) < 32 or len(self.tokens) != len(self.order) or not self.tokens:
            raise OracleError("challenge needs a nonce and one token per case")
        if len(set(self.tokens)) != len(self.tokens) or sorted(self.order) != list(range(len(self.order))):
            raise OracleError("challenge tokens must be unique and order a permutation")

    def inputs(self, case_set: CaseSet) -> list[dict[str, Any]]:
        """The only per-case data the candidate process receives."""
        if len(case_set.cases) != len(self.order):
            raise OracleError("challenge does not match the case set")
        return [
            {"token": token, "args": list(case_set.cases[index].args),
             "kwargs": dict(case_set.cases[index].kwargs)}
            for token, index in zip(self.tokens, self.order)
        ]


def new_challenge(case_count: int, *, token_hex: Callable[[int], str] = secrets.token_hex,
                  shuffle: Callable[[list[int]], None] | None = None) -> OracleChallenge:
    """Choose a fresh nonce, fresh tokens and a random presentation order."""
    if not 1 <= int(case_count) <= MAX_ORACLE_CASES:
        raise OracleError(f"challenge needs 1..{MAX_ORACLE_CASES} cases")
    order = list(range(int(case_count)))
    (shuffle or secrets.SystemRandom().shuffle)(order)
    tokens: list[str] = []
    while len(tokens) < case_count:
        token = token_hex(_TOKEN_HEX // 2)
        if token not in tokens:
            tokens.append(token)
    return OracleChallenge(token_hex(_NONCE_HEX // 2), tuple(tokens), tuple(order))


@dataclass(frozen=True, slots=True)
class OracleVerdict:
    """The parent's comparison of one frame with the held outcomes."""

    passed: bool
    detail: str
    case_count: int
    matched: int
    outputs_sha256: str | None = None


def _same_outcome(observed: object, expected: Mapping[str, Any]) -> bool:
    if not (isinstance(observed, dict) and len(observed) == 1 and set(observed) <= _OUTCOME_KEYS):
        return False
    try:
        return canonical_json(observed) == canonical_json(dict(expected))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False


def grade_frame(output: str, challenge: OracleChallenge, case_set: CaseSet) -> OracleVerdict:
    """Compare raw per-case outputs with held outcomes; nothing else counts.

    The frame must be unique, bounded, echo this challenge's nonce and carry
    exactly this challenge's tokens.  The verdict detail never contains an
    input or an expected value.  That alone does not keep the set out of
    the ledger: a passing frame *is* the expected values, and the challenge
    payload names the inputs, so the probe row itself is stored redacted
    (:func:`ledger_command`, :func:`ledger_output`) and this function is
    only ever given the parent's in-memory copy of the output.
    """
    count = len(case_set.cases)

    def refuse(reason: str) -> OracleVerdict:
        return OracleVerdict(False, reason, count, 0)

    frames = [line[len(ORACLE_FRAME_PREFIX):] for line in str(output).splitlines()
              if line.startswith(ORACLE_FRAME_PREFIX)]
    if len(frames) != 1:
        return refuse(f"oracle expected exactly one result frame, found {len(frames)}")
    if len(frames[0].encode("utf-8")) > MAX_ORACLE_PAYLOAD_BYTES:
        return refuse("oracle result frame exceeds its bound")
    try:
        report = json.loads(frames[0])
    except (ValueError, RecursionError):
        return refuse("oracle result frame is not JSON")
    if not isinstance(report, dict) or set(report) != {"nonce", "outputs"}:
        return refuse("oracle result frame shape differs from the challenge")
    if report["nonce"] != challenge.nonce:
        return refuse("oracle result frame names another challenge nonce (replayed or forged)")
    outputs = report["outputs"]
    if not isinstance(outputs, dict) or set(outputs) != set(challenge.tokens):
        return refuse("oracle result frame does not answer exactly this challenge's cases")
    observed_by_case: list[Any] = [None] * count
    matched = 0
    for token, index in zip(challenge.tokens, challenge.order):
        observed = outputs[token]
        observed_by_case[index] = observed
        if _same_outcome(observed, case_set.cases[index].outcome):
            matched += 1
    try:
        digest = sha256_text(canonical_json(observed_by_case))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return refuse("oracle outputs are not strict JSON")
    if matched != count:
        return OracleVerdict(False, f"candidate outputs differ from evaluator-held outcomes "
                                    f"({matched}/{count} matched)", count, matched, digest)
    return OracleVerdict(True, f"candidate outputs matched {count} evaluator-held case(s)",
                         count, matched, digest)


def _digest_field(value: object, name: str) -> str:
    if not (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)):
        raise OracleError(f"receipt {name} must be a SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class OracleReceipt:
    """Durable decision receipt for one oracle verdict.

    ``candidate`` is the tested-bytes binding (``files`` and
    ``diff_sha256``); ``baseline`` is the starting commit plus the sealed
    backup manifest digest.  Construction re-validates every invariant so a
    stored receipt that was edited cannot claim more than it proves.
    """

    run_id: str
    probe_id: int
    attestation: str
    candidate_uid: int | None
    supervisor_uid: int | None
    case_set_sha256: str
    case_count: int
    matched: int
    nonce: str
    outputs_sha256: str | None
    confidential: bool
    read_denied: bool
    passed: bool
    candidate: Mapping[str, Any] = field(default_factory=dict)
    baseline: Mapping[str, Any] = field(default_factory=dict)
    version: int = RECEIPT_VERSION

    def __post_init__(self) -> None:
        if self.version != RECEIPT_VERSION or not self.run_id:
            raise OracleError("unsupported or unbound oracle receipt")
        if type(self.probe_id) is not int or self.probe_id <= 0:
            raise OracleError("receipt must name the ledger row of its probe")
        if self.attestation not in ISOLATION_KINDS:
            raise OracleError("receipt probe was not supervisor-attested")
        _digest_field(self.case_set_sha256, "case_set_sha256")
        if self.outputs_sha256 is not None:
            _digest_field(self.outputs_sha256, "outputs_sha256")
        if not 1 <= self.case_count <= MAX_ORACLE_CASES or not 0 <= self.matched <= self.case_count:
            raise OracleError("receipt case counts are inconsistent")
        if self.passed is not (self.matched == self.case_count and self.outputs_sha256 is not None):
            raise OracleError("receipt pass flag does not match its comparison")
        files = self.candidate.get("files") if isinstance(self.candidate, Mapping) else None
        if (not isinstance(files, Mapping) or not files
                or set(self.candidate) != {"files", "diff_sha256"}):
            raise OracleError("receipt is not bound to tested candidate bytes")
        _digest_field(self.candidate["diff_sha256"], "candidate diff_sha256")
        if (not isinstance(self.baseline, Mapping)
                or set(self.baseline) != {"starting_commit", "manifest_sha256"}):
            raise OracleError("receipt is not bound to a baseline")
        _digest_field(self.baseline["manifest_sha256"], "baseline manifest_sha256")
        if self.read_denied and not self.confidential:
            raise OracleError("a read-denial proof requires a confidential case set")

    @property
    def independent(self) -> bool:
        """The verdict came from an oracle the candidate could not read or forge."""
        return (self.passed and self.attestation == LINUX_UID
                and self.confidential and self.read_denied)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version, "run_id": self.run_id, "probe_id": self.probe_id,
            "attestation": self.attestation, "candidate_uid": self.candidate_uid,
            "supervisor_uid": self.supervisor_uid, "case_set_sha256": self.case_set_sha256,
            "case_count": self.case_count, "matched": self.matched, "nonce": self.nonce,
            "outputs_sha256": self.outputs_sha256, "confidential": self.confidential,
            "read_denied": self.read_denied, "passed": self.passed,
            "candidate": {"files": dict(self.candidate["files"]),
                          "diff_sha256": self.candidate["diff_sha256"]},
            "baseline": dict(self.baseline),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_mapping())

    def digest(self) -> str:
        return sha256_text(self.to_json())

    @classmethod
    def from_json(cls, text: str) -> "OracleReceipt":
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise OracleError("receipt is not JSON") from exc
        if not isinstance(raw, dict):
            raise OracleError("receipt must be an object")
        try:
            receipt = cls(**raw)
        except TypeError as exc:
            raise OracleError("receipt fields differ from the contract") from exc
        if receipt.to_json() != canonical_json(raw):
            raise OracleError("receipt is not in canonical form")
        return receipt

    def admission_refusal(self, *, candidate: Mapping[str, Any],
                          baseline: Mapping[str, Any]) -> str | None:
        """Why this receipt cannot authorize unattended promotion, else ``None``."""
        if not self.passed:
            return "independent oracle failed"
        if self.attestation != LINUX_UID:
            return f"oracle probe ran under the {self.attestation} supervisor, which does not bound reads"
        if not self.confidential or not self.read_denied:
            return "candidate uid was not proven unable to read the held expected values"
        if canonical_json(dict(candidate)) != canonical_json(self.to_mapping()["candidate"]):
            return "oracle receipt is bound to different candidate bytes"
        if canonical_json(dict(baseline)) != canonical_json(dict(self.baseline)):
            return "oracle receipt is bound to a different baseline"
        return None


def challenge_payload(challenge: OracleChallenge, case_set: CaseSet, *, root: str,
                      module: str | None = None, function: str | None = None) -> str:
    """The bounded JSON argument handed to the candidate-side probe."""
    payload = canonical_json({
        "root": root, "module": module or case_set.module,
        "function": function or case_set.function,
        "nonce": challenge.nonce, "cases": challenge.inputs(case_set),
    })
    if len(payload.encode("utf-8")) > MAX_ORACLE_PAYLOAD_BYTES:
        raise OracleError("oracle challenge exceeds its bound")
    return payload


def payload_nonce(command: Sequence[object]) -> str | None:
    """The nonce carried by a probe command or its ledger form, if any."""
    if not command:
        return None
    try:
        payload = json.loads(str(command[-1]))
    except (ValueError, RecursionError):
        return None
    nonce = payload.get("nonce") if isinstance(payload, dict) else None
    return nonce if isinstance(nonce, str) else None


def _text_digest(text: str) -> tuple[int, str]:
    data = str(text).encode("utf-8", "surrogatepass")
    return len(data), hashlib.sha256(data).hexdigest()


def ledger_command(command: Sequence[object]) -> list[str]:
    """The oracle probe argv as the selfmod ledger stores it.

    The trailing challenge payload (held inputs keyed by this run's tokens)
    is replaced by its nonce and SHA-256, so no ledger row, ``/selfmod
    tests`` listing or HTTP read of the ledger can pair a held input with
    the output a correct candidate produced for it.
    """
    items = [str(item) for item in command]
    if not items:
        return items
    size, digest = _text_digest(items[-1])
    items[-1] = canonical_json({
        "withheld": "evaluator challenge inputs", "nonce": payload_nonce(items),
        "payload_bytes": size, "payload_sha256": digest,
    })
    return items


def ledger_output(output: str) -> str:
    """The oracle probe output as the selfmod ledger stores it.

    A passing candidate's raw outputs are the held expected values, so the
    ledger keeps only their size and SHA-256.  The parent grades the
    in-memory output and proves it is the recorded one by this digest.
    """
    size, digest = _text_digest(output)
    return WITHHELD_OUTPUT_PREFIX + canonical_json({"bytes": size, "sha256": digest})
