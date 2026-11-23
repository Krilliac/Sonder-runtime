"""Pure, bounded, stdlib-only multi-candidate self-consistency & tool-receipt reconciler.

Two high-leverage anti-hallucination operations:
1. ``compare_candidates``: compares 2-8 candidate answers (e.g. from multi-pass
   sampling, ensembles, or fanout), extracts numbers, file paths, code blocks,
   and yes/no verdicts, and reports an agreement score, majority consensus facts,
   and every divergent/contested claim (where single-model hallucinations hide).
2. ``audit_against_receipts``: compares a draft reply against grounded tool
   execution receipts (``tool``, ``status``, ``exit_code``, ``output``,
   ``files_written``) to catch fabricated pass claims, contradicted failures,
   and phantom file reads/writes.
"""
from __future__ import annotations

from collections import Counter
import re
from typing import Any

MAX_CANDIDATES = 8
MAX_TEXT_CHARS = 32_000

_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])(-?\d+(?:\.\d+)?)(?![A-Za-z0-9_])")
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_/@])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:py|js|ts|dart|rs|go|c|cpp|h|cs|java|json|toml|md|ps1|sh))\b"
)
_PASS_CLAIM_RE = re.compile(
    r"(?:\b(?:all\s+\d*\s*tests?\s+pass(?:ed)?|tests?\s+passed|build\s+succeeded|verified\s+(?:cleanly|successfully)|exit\s+code\s*(?:is|=|:)?\s*0)\b|[\u2705\u2713])",
    re.IGNORECASE,
)
_WRITE_CLAIM_RE = re.compile(
    r"\b(?:created|wrote|written|saved|modified|updated|edited)\s+(?:file\s+)?`?([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,10})`?",
    re.IGNORECASE,
)
_EXEC_TOOLS = frozenset({"run_code", "run_project", "test_run", "build_run", "lint_run", "typecheck_run", "workspace_run", "script_run"})
_WRITE_TOOLS = frozenset({"file_write", "file_edit", "file_batch_write", "text_patch", "json_patch", "apply_patch", "write_file", "edit_file"})


def _extract_signals(text: str) -> dict[str, set[str]]:
    clean = str(text or "")[:MAX_TEXT_CHARS]
    prose = re.sub(r"```.*?```", " ", clean, flags=re.DOTALL)
    nums = set(_NUMBER_RE.findall(prose))
    paths = set(_PATH_RE.findall(clean))
    first_sentence = prose.strip().split("\n", 1)[0][:200].lower()
    verdict = "unknown"
    if re.search(r"\b(?:yes|true|valid|correct|safe|vulnerable|confirmed)\b", first_sentence):
        verdict = "affirmative"
    elif re.search(r"\b(?:no|false|invalid|incorrect|not\s+vulnerable|refuted|impossible)\b", first_sentence):
        verdict = "negative"
    words = {w for w in re.findall(r"[a-z][a-z0-9_]{3,}", prose.lower()) if w not in {"this", "that", "with", "from", "have", "will", "into", "when", "then", "than", "also", "more", "some", "only"}}
    return {"numbers": nums, "paths": paths, "verdict": {verdict} if verdict != "unknown" else set(), "terms": words}


def compare_candidates(candidates: list[str]) -> dict[str, Any]:
    """Compare 2-8 candidate answers and surface consensus vs contested/hallucinated claims."""
    if not isinstance(candidates, list) or len(candidates) < 2:
        return {"ok": False, "error": "compare_candidates requires a list of at least 2 candidate answers"}
    bounded = [str(c or "")[:MAX_TEXT_CHARS] for c in candidates[:MAX_CANDIDATES]]
    n = len(bounded)
    signals = [_extract_signals(c) for c in bounded]

    # Pairwise Jaccard similarity over significant terms + numbers
    pair_scores: list[float] = []
    for i in range(n):
        si = signals[i]["terms"] | signals[i]["numbers"] | signals[i]["paths"]
        for j in range(i + 1, n):
            sj = signals[j]["terms"] | signals[j]["numbers"] | signals[j]["paths"]
            union = si | sj
            pair_scores.append((len(si & sj) / len(union)) if union else 1.0)
    agreement = sum(pair_scores) / len(pair_scores) if pair_scores else 1.0

    num_counts = Counter(num for s in signals for num in s["numbers"])
    path_counts = Counter(p for s in signals for p in s["paths"])
    verdict_counts = Counter(v for s in signals for v in s["verdict"])

    majority = (n // 2) + 1
    consensus_numbers = sorted(k for k, c in num_counts.items() if c >= majority)
    contested_numbers = sorted(k for k, c in num_counts.items() if c < majority)
    consensus_paths = sorted(k for k, c in path_counts.items() if c >= majority)
    contested_paths = sorted(k for k, c in path_counts.items() if c < majority)
    verdict_conflict = len(verdict_counts) > 1

    risk_flags: list[str] = []
    if verdict_conflict:
        risk_flags.append("verdict_conflict")
    if contested_numbers:
        risk_flags.append("numeric_divergence")
    if contested_paths:
        risk_flags.append("file_path_divergence")
    if agreement < 0.25:
        risk_flags.append("low_overall_agreement")

    return {
        "ok": True,
        "action": "compare_candidates",
        "candidate_count": n,
        "agreement_score": round(agreement, 3),
        "verdict_conflict": verdict_conflict,
        "verdicts": dict(verdict_counts),
        "consensus_numbers": consensus_numbers[:30],
        "contested_numbers": contested_numbers[:30],
        "consensus_paths": consensus_paths[:30],
        "contested_paths": contested_paths[:30],
        "hallucination_risk": "high" if (verdict_conflict or agreement < 0.25) else ("medium" if risk_flags else "low"),
        "risk_flags": risk_flags,
    }


def audit_against_receipts(reply: str, receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit a draft answer against grounded tool receipts to catch fabricated pass/write claims."""
    text = str(reply or "")[:MAX_TEXT_CHARS]
    prose = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    recs = [r for r in (receipts or []) if isinstance(r, dict)]

    exec_ok = False
    exec_failed: list[dict[str, Any]] = []
    written_files: set[str] = set()

    for rec in recs:
        tool = str(rec.get("tool") or "").strip()
        status = str(rec.get("status") or "").strip().lower()
        exit_code = rec.get("exit_code")
        if tool in _EXEC_TOOLS:
            if status in {"ok", "completed", "ran"} and (exit_code is None or exit_code == 0):
                exec_ok = True
            elif status in {"error", "failed", "timeout", "timed_out"} or (isinstance(exit_code, int) and exit_code != 0):
                exec_failed.append({"tool": tool, "status": status, "exit_code": exit_code})
        if tool in _WRITE_TOOLS and status in {"ok", "completed"}:
            for f in (rec.get("files_written") or []):
                written_files.add(str(f).replace("\\", "/").lower())
            if rec.get("path"):
                written_files.add(str(rec["path"]).replace("\\", "/").lower())

    issues: list[dict[str, Any]] = []
    claims_pass = bool(_PASS_CLAIM_RE.search(prose))
    if claims_pass and exec_failed and not exec_ok:
        issues.append({
            "kind": "contradicted_execution_pass",
            "severity": "high",
            "detail": f"Reply claims tests/execution succeeded, but execution tool(s) failed: {exec_failed}",
        })
    elif claims_pass and not exec_ok and not exec_failed:
        issues.append({
            "kind": "unverified_execution_pass",
            "severity": "high",
            "detail": "Reply claims tests/execution passed, but no execution tool ran in receipts.",
        })

    for wm in _WRITE_CLAIM_RE.finditer(prose):
        prefix = prose[max(0, wm.start() - 25) : wm.start()].lower()
        if any(neg in prefix for neg in ("not ", "didn't ", "did not ", "never ", "will ", "would ", "if ")):
            continue
        claimed_path = wm.group(1).replace("\\", "/").lower()
        base = claimed_path.rsplit("/", 1)[-1]
        matched = any(wf == claimed_path or wf.endswith("/" + base) or wf == base for wf in written_files)
        if not matched:
            issues.append({
                "kind": "unverified_file_write",
                "severity": "high",
                "path": wm.group(1),
                "detail": f"Reply claims {wm.group(1)!r} was written/modified, but no successful write receipt covers it.",
            })

    return {
        "ok": True,
        "action": "audit_against_receipts",
        "clean": len(issues) == 0,
        "receipt_count": len(recs),
        "issue_count": len(issues),
        "issues": issues,
    }


def consistency_check(
    *,
    action: str = "compare_candidates",
    candidates: list[str] | None = None,
    reply: str = "",
    receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare candidate answers for consensus/divergence or audit a draft reply against tool receipts."""
    act = str(action or "compare_candidates").strip().lower() or "compare_candidates"
    if act == "compare_candidates":
        return compare_candidates(candidates or [])
    if act == "audit_against_receipts":
        return audit_against_receipts(reply, receipts or [])
    return {"ok": False, "error": f"unknown action {act!r} (use compare_candidates or audit_against_receipts)"}


__all__ = ["audit_against_receipts", "compare_candidates", "consistency_check"]
