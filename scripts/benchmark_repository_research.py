"""Deterministically score bounded repository-research evidence.

This harness makes no model or network calls.  Its built-in fixture suite names
only public, repository-relative paths and symbols.  Candidate prose and tool
arguments are consumed for scoring but are never copied into reports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


SUBMISSION_SCHEMA = "sonder.repository-research-submission.v1"
REPORT_SCHEMA = "sonder.repository-research-report.v1"
SUITE_NAME = "sonder-core-repository-research"
SUITE_VERSION = "1"
MAX_INPUT_BYTES = 512 * 1024
MAX_CASES = 32
MAX_CLAIMS = 64
MAX_CITATIONS = 16
MAX_TOOL_CALLS = 128
MAX_TEXT_CHARS = 8_000
REPEATED_TOOL_THRESHOLD = 3

DEFAULT_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "benchmark-moat",
        "question": "Locate the deterministic grading and scorecard entry points.",
        "implementation_exists": True,
        "required_evidence": (
            ("scripts/benchmark_moat.py", "benchmark"),
            ("scripts/benchmark_moat.py", "render_scorecard"),
        ),
    },
    {
        "id": "evaluation-history",
        "question": "Locate record construction and aggregate history status.",
        "implementation_exists": True,
        "required_evidence": (
            ("eval_history.py", "make_record"),
            ("eval_history.py", "history_status"),
        ),
    },
    {
        "id": "promotion-evaluation",
        "question": "Locate report validation and the promotion decision.",
        "implementation_exists": True,
        "required_evidence": (
            ("promotion_eval.py", "validate_model_report"),
            ("promotion_eval.py", "promotion_decision"),
        ),
    },
    {
        "id": "model-gateway-contract",
        "question": "Locate the provider capability and contract probe types.",
        "implementation_exists": True,
        "required_evidence": (
            ("tests/model_gateway_contract.py", "ProviderCapabilities"),
            ("tests/model_gateway_contract.py", "GatewayContractProbe"),
        ),
    },
)

_NEGATIVE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bno\s+(?:such\s+)?implementation\s+exists\b",
        r"\b(?:is|are|was|were)\s+not\s+implemented\b",
        r"\bcould\s+not\s+find\s+(?:an|any|the)?\s*implementation\b",
        r"\bno\s+implementation\s+(?:was\s+)?found\b",
    )
)


class ResearchBenchmarkError(ValueError):
    """Raised for invalid or unsafe benchmark inputs."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_id(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object(value: Any, label: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ResearchBenchmarkError(f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ResearchBenchmarkError(f"{label} has unknown fields: {sorted(unknown)}")
    return value


def _array(value: Any, label: str, limit: int) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ResearchBenchmarkError(f"{label} must be an array")
    if len(value) > limit:
        raise ResearchBenchmarkError(f"{label} exceeds limit {limit}")
    return value


def _text(value: Any, label: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise ResearchBenchmarkError(f"{label} must be a string")
    if len(value) > limit:
        raise ResearchBenchmarkError(f"{label} exceeds {limit} characters")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ResearchBenchmarkError(f"{label} contains control characters")
    return value


def _safe_repo_path(value: Any, label: str) -> str:
    text = _text(value, label, limit=512)
    path = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or path.is_absolute()
        or ":" in text
        or "~" in path.parts
        or ".." in path.parts
        or "." in path.parts
    ):
        return "<unsafe-path>"
    return path.as_posix()


def _safe_name(value: Any, label: str, *, limit: int = 128) -> str:
    text = _text(value, label, limit=limit)
    if not text or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", text):
        return "<unsafe-name>"
    return text


def _negative_claim(text: str) -> bool:
    return any(pattern.search(text) for pattern in _NEGATIVE_PATTERNS)


def _opaque_evidence_id(value: str) -> str:
    """Identify untrusted evidence without copying its possibly-private text."""
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def suite_descriptor(cases: Sequence[Mapping[str, Any]] = DEFAULT_CASES) -> dict[str, Any]:
    public_cases = [
        {
            "id": case["id"],
            "question": case["question"],
            "implementation_exists": case["implementation_exists"],
            "required_evidence": [
                {"path": path, "symbol": symbol}
                for path, symbol in case["required_evidence"]
            ],
        }
        for case in cases
    ]
    descriptor = {
        "name": SUITE_NAME,
        "version": SUITE_VERSION,
        "cases": public_cases,
    }
    descriptor["digest"] = _content_id(descriptor)
    return descriptor


def _load_json(path: Path) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ResearchBenchmarkError("cannot inspect submission") from exc
    if size > MAX_INPUT_BYTES:
        raise ResearchBenchmarkError(f"submission exceeds {MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite number: {value}")
            ),
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ResearchBenchmarkError("submission is not valid bounded UTF-8 JSON") from exc


def _canonical_arguments(value: Any, label: str) -> bytes:
    if not isinstance(value, (dict, list, str, int, float, bool)) and value is not None:
        raise ResearchBenchmarkError(f"{label} must be a JSON value")
    try:
        encoded = _canonical(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResearchBenchmarkError(f"{label} is not canonical JSON") from exc
    if len(encoded) > MAX_TEXT_CHARS:
        raise ResearchBenchmarkError(f"{label} exceeds {MAX_TEXT_CHARS} encoded bytes")
    return encoded


def _tool_loop(tool_calls: Sequence[Any], label: str) -> tuple[bool, int]:
    longest = 0
    current = 0
    previous: tuple[str, bytes] | None = None
    for index, raw in enumerate(tool_calls):
        call = _object(raw, f"{label}[{index}]", {"tool", "arguments"})
        tool = _safe_name(call.get("tool"), f"{label}[{index}].tool", limit=64)
        signature = (
            tool,
            _canonical_arguments(call.get("arguments"), f"{label}[{index}].arguments"),
        )
        current = current + 1 if signature == previous else 1
        previous = signature
        if current > longest:
            longest = current
    repeated = longest >= REPEATED_TOOL_THRESHOLD
    return repeated, longest


def evaluate(
    submission: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]] = DEFAULT_CASES,
) -> dict[str, Any]:
    """Validate and score one submission without touching models or the network."""
    root = _object(submission, "submission", {"schema", "cases"})
    if root.get("schema") != SUBMISSION_SCHEMA:
        raise ResearchBenchmarkError(f"submission.schema must be {SUBMISSION_SCHEMA}")
    raw_cases = _array(root.get("cases"), "submission.cases", MAX_CASES)
    submitted: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(raw_cases):
        item = _object(raw, f"submission.cases[{index}]", {"id", "answer", "claims", "tool_calls"})
        case_id = _safe_name(item.get("id"), f"submission.cases[{index}].id", limit=96)
        if case_id in submitted:
            raise ResearchBenchmarkError(f"duplicate case id: {case_id}")
        submitted[case_id] = item

    expected_ids = {str(case["id"]) for case in cases}
    unknown_ids = set(submitted) - expected_ids
    if unknown_ids:
        raise ResearchBenchmarkError(f"unknown case ids: {sorted(unknown_ids)}")

    case_reports: list[dict[str, Any]] = []
    for fixture in cases:
        case_id = str(fixture["id"])
        required = {(str(path), str(symbol)) for path, symbol in fixture["required_evidence"]}
        item = submitted.get(case_id)
        if item is None:
            case_reports.append(
                {
                    "id": case_id,
                    "present": False,
                    "passed": False,
                    "false_negative_count": 0,
                    "unsupported_claim_count": 0,
                    "unsupported_claim_rate": 0.0,
                    "invented_paths": [],
                    "invented_symbols": [],
                    "missing_coverage": [
                        {"path": path, "symbol": symbol} for path, symbol in sorted(required)
                    ],
                    "repeated_identical_tool_loop": False,
                    "max_identical_tool_run": 0,
                }
            )
            continue

        answer = _text(item.get("answer", ""), f"case {case_id}.answer")
        raw_claims = _array(item.get("claims", []), f"case {case_id}.claims", MAX_CLAIMS)
        tool_calls = _array(
            item.get("tool_calls", []), f"case {case_id}.tool_calls", MAX_TOOL_CALLS
        )
        cited: set[tuple[str, str]] = set()
        invented_paths: set[str] = set()
        invented_symbols: set[str] = set()
        known_paths = {known_path for known_path, _ in required}
        known_symbols = {known_symbol for _, known_symbol in required}
        unsupported = 0
        explicit_missing = False

        for claim_index, raw_claim in enumerate(raw_claims):
            claim = _object(
                raw_claim,
                f"case {case_id}.claims[{claim_index}]",
                {"text", "status", "citations"},
            )
            claim_text = _text(claim.get("text"), f"case {case_id}.claims[{claim_index}].text")
            status = claim.get("status")
            if status not in {"exists", "missing"}:
                raise ResearchBenchmarkError(
                    f"case {case_id}.claims[{claim_index}].status must be exists or missing"
                )
            explicit_missing = (
                explicit_missing or status == "missing" or _negative_claim(claim_text)
            )
            raw_citations = _array(
                claim.get("citations", []),
                f"case {case_id}.claims[{claim_index}].citations",
                MAX_CITATIONS,
            )
            valid_for_claim = 0
            invalid_for_claim = False
            for citation_index, raw_citation in enumerate(raw_citations):
                citation = _object(
                    raw_citation,
                    f"case {case_id}.claims[{claim_index}].citations[{citation_index}]",
                    {"path", "symbol"},
                )
                path = _safe_repo_path(citation.get("path"), "citation.path")
                symbol = _safe_name(citation.get("symbol"), "citation.symbol")
                pair = (path, symbol)
                if pair in required:
                    cited.add(pair)
                    valid_for_claim += 1
                else:
                    invalid_for_claim = True
                    if path not in known_paths:
                        invented_paths.add(
                            path if path == "<unsafe-path>" else _opaque_evidence_id(path)
                        )
                    if symbol not in known_symbols:
                        invented_symbols.add(
                            symbol if symbol == "<unsafe-name>" else _opaque_evidence_id(symbol)
                        )
            if invalid_for_claim or (status == "exists" and valid_for_claim == 0):
                unsupported += 1

        false_negative = bool(fixture["implementation_exists"]) and (
            explicit_missing or _negative_claim(answer)
        )
        repeated, longest = _tool_loop(tool_calls, f"case {case_id}.tool_calls")
        missing = required - cited
        claim_count = len(raw_claims)
        report = {
            "id": case_id,
            "present": True,
            "passed": not (false_negative or unsupported or missing or repeated),
            "false_negative_count": int(false_negative),
            "unsupported_claim_count": unsupported,
            "unsupported_claim_rate": round(unsupported / max(1, claim_count), 6),
            "invented_paths": sorted(invented_paths),
            "invented_symbols": sorted(invented_symbols),
            "missing_coverage": [
                {"path": path, "symbol": symbol} for path, symbol in sorted(missing)
            ],
            "repeated_identical_tool_loop": repeated,
            "max_identical_tool_run": longest,
        }
        case_reports.append(report)

    false_negatives = sum(case["false_negative_count"] for case in case_reports)
    unsupported_claims = sum(case["unsupported_claim_count"] for case in case_reports)
    passed = sum(bool(case["passed"]) for case in case_reports)
    report = {
        "schema": REPORT_SCHEMA,
        "suite": suite_descriptor(cases),
        "model_free": True,
        "causal_claim": False,
        "limits": {
            "input_bytes": MAX_INPUT_BYTES,
            "cases": MAX_CASES,
            "claims_per_case": MAX_CLAIMS,
            "citations_per_claim": MAX_CITATIONS,
            "tool_calls_per_case": MAX_TOOL_CALLS,
            "repeated_tool_threshold": REPEATED_TOOL_THRESHOLD,
        },
        "summary": {
            "cases_total": len(case_reports),
            "cases_passed": passed,
            "case_pass_rate": round(passed / max(1, len(case_reports)), 6),
            "false_negative_count": false_negatives,
            "false_negative_rate": round(false_negatives / max(1, len(case_reports)), 6),
            "unsupported_claim_count": unsupported_claims,
            "cases_with_missing_coverage": sum(
                bool(case["missing_coverage"]) for case in case_reports
            ),
            "repeated_tool_loop_count": sum(
                bool(case["repeated_identical_tool_loop"]) for case in case_reports
            ),
        },
        "cases": case_reports,
    }
    report["report_id"] = _content_id(report)
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Repository research benchmark",
        "",
        f"Suite: `{report['suite']['name']}` v{report['suite']['version']}",
        f"Report: `{report['report_id']}`",
        "",
        "This is a deterministic, model-free evidence check; it makes no causal claim.",
        "",
        "| Case | Pass | False negatives | Unsupported claims | Missing evidence | Tool loop |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for case in report["cases"]:
        lines.append(
            f"| {case['id']} | {'yes' if case['passed'] else 'no'} | "
            f"{case['false_negative_count']} | {case['unsupported_claim_count']} | "
            f"{len(case['missing_coverage'])} | "
            f"{'yes' if case['repeated_identical_tool_loop'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            f"Passed: {summary['cases_passed']}/{summary['cases_total']}; "
            f"false negatives: {summary['false_negative_count']}; "
            f"unsupported claims: {summary['unsupported_claim_count']}; "
            f"repeated tool loops: {summary['repeated_tool_loop_count']}.",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write(path: Path, data: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _distinct_paths(paths: Sequence[Path | None]) -> None:
    resolved = [path.resolve() for path in paths if path is not None]
    if len(resolved) != len(set(resolved)):
        raise ResearchBenchmarkError("input and output paths must be distinct")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission", type=Path)
    parser.add_argument("--json", type=Path, dest="json_output")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--print-suite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.print_suite:
            if args.json_output or args.markdown:
                raise ResearchBenchmarkError("--print-suite cannot be combined with report outputs")
            print(json.dumps(suite_descriptor(), indent=2, sort_keys=True))
            return 0
        if args.submission is None:
            raise ResearchBenchmarkError("--submission is required unless --print-suite is used")
        _distinct_paths((args.submission, args.json_output, args.markdown))
        submission = _load_json(args.submission)
        report = evaluate(submission)
        if args.json_output:
            _atomic_write(args.json_output, json.dumps(report, indent=2, sort_keys=True) + "\n")
        markdown = render_markdown(report)
        if args.markdown:
            _atomic_write(args.markdown, markdown)
        if not args.markdown:
            print(markdown, end="")
        return 0
    except ResearchBenchmarkError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
