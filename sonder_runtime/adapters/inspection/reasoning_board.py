"""Session/owner-scoped structured reasoning scratchpad (facts, hypotheses, decisions, audit).

Gives Sonder an explicit, bounded working-memory workspace during complex
debugging, design, and multi-step investigation so local models avoid context
drift, circular reasoning, and ungrounded conclusions:
- ``init``: start or reset a goal/question board.
- ``add_fact``: record a grounded observation with a concrete source ref.
- ``add_hypothesis`` / ``update_hypothesis``: track candidate explanations with
  status (``open | supported | refuted | confirmed``), confidence (0..1), and
  supporting/refuting evidence linked to fact IDs.
- ``add_decision``: record an architectural/implementation choice with rationale
  and rejected alternatives.
- ``audit``: flag unverified high-confidence hypotheses, hypotheses lacking
  grounded facts, simultaneous conflicting ``confirmed`` hypotheses, or open
  questions remaining before concluding.
- ``summary``: crisp structured cognitive snapshot.

Security & hygiene invariants:
- In-memory bounded LRU keyed by scope (caller owner/session + board_id); never
  writes secrets to disk and applies ``redaction.redact_text`` to stored strings.
"""
from __future__ import annotations

from collections import OrderedDict
import threading
import time
from typing import Any

from sonder_runtime.domain.security import redaction

MAX_BOARDS = 128
MAX_FACTS = 64
MAX_HYPOTHESES = 32
MAX_DECISIONS = 32
MAX_TEXT_CHARS = 800

VALID_HYPOTHESIS_STATUSES = frozenset({"open", "supported", "refuted", "confirmed"})

_LOCK = threading.Lock()
_BOARDS: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _clean_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return redaction.redact_text(raw[:limit])


def _scope_key(board_id: str, owner: str) -> str:
    clean_owner = re_safe(owner or "local")
    clean_board = re_safe(board_id or "default")
    return f"{clean_owner}::{clean_board}"


def re_safe(text: str) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum() or ch in ("-", "_", ".", ":"))[:80] or "default"


def _get_or_create_board(key: str) -> dict[str, Any]:
    board = _BOARDS.get(key)
    if board is None:
        while len(_BOARDS) >= MAX_BOARDS:
            _BOARDS.popitem(last=False)
        board = {
            "goal": "",
            "facts": [],
            "hypotheses": [],
            "decisions": [],
            "updated_at": time.time(),
        }
        _BOARDS[key] = board
    else:
        _BOARDS.move_to_end(key)
    return board


def _audit_board(board: dict[str, Any]) -> dict[str, Any]:
    fact_ids = {f["id"] for f in board["facts"]}
    issues: list[dict[str, str]] = []
    confirmed = [h for h in board["hypotheses"] if h["status"] == "confirmed"]
    open_hyps = [h for h in board["hypotheses"] if h["status"] == "open"]

    if len(confirmed) > 1:
        issues.append({
            "kind": "multiple_confirmed_hypotheses",
            "severity": "warning",
            "detail": f"{len(confirmed)} hypotheses marked confirmed ({', '.join(h['id'] for h in confirmed)}); verify they are compatible, not contradictory.",
        })

    for h in board["hypotheses"]:
        has_for = bool(h["evidence_for"])
        has_against = bool(h["evidence_against"])
        if h["status"] in {"confirmed", "supported"} and not has_for:
            issues.append({
                "kind": "unsupported_hypothesis",
                "severity": "high",
                "detail": f"{h['id']} is '{h['status']}' with zero grounded supporting evidence.",
            })
        if h["status"] == "confirmed" and has_against:
            issues.append({
                "kind": "unaddressed_counterevidence",
                "severity": "high",
                "detail": f"{h['id']} is 'confirmed' despite recorded counter-evidence ({len(h['evidence_against'])} item(s)).",
            })
        if h["confidence"] >= 0.8 and not has_for and h["status"] != "refuted":
            issues.append({
                "kind": "overconfident_without_evidence",
                "severity": "medium",
                "detail": f"{h['id']} has confidence {h['confidence']:.2f} with no supporting facts.",
            })
        for ref in h.get("fact_refs", []):
            if ref not in fact_ids:
                issues.append({
                    "kind": "dangling_fact_ref",
                    "severity": "low",
                    "detail": f"{h['id']} references unknown fact_id {ref!r}.",
                })

    if board["hypotheses"] and not board["facts"]:
        issues.append({
            "kind": "no_grounded_facts",
            "severity": "high",
            "detail": "Hypotheses exist without any recorded grounded facts.",
        })

    return {
        "clean": len(issues) == 0,
        "issue_count": len(issues),
        "issues": issues,
        "counts": {
            "facts": len(board["facts"]),
            "hypotheses_total": len(board["hypotheses"]),
            "hypotheses_open": len(open_hyps),
            "hypotheses_confirmed": len(confirmed),
            "decisions": len(board["decisions"]),
        },
    }


def reasoning_board(
    action: str = "summary",
    *,
    board_id: str = "default",
    owner: str = "local",
    goal: str = "",
    text: str = "",
    source: str = "",
    hypothesis_id: str = "",
    status: str = "",
    confidence: float | None = None,
    evidence_for: str = "",
    evidence_against: str = "",
    fact_refs: list[str] | None = None,
    alternatives: list[str] | None = None,
) -> dict[str, Any]:
    """Maintain a structured, session-scoped hypothesis/evidence/decision workspace."""
    act = str(action or "summary").strip().lower() or "summary"
    key = _scope_key(board_id, owner)

    with _LOCK:
        if act == "clear":
            _BOARDS.pop(key, None)
            return {"ok": True, "action": "clear", "board_id": board_id}

        if act == "init":
            board = _get_or_create_board(key)
            board["goal"] = _clean_text(goal or text)
            board["facts"] = []
            board["hypotheses"] = []
            board["decisions"] = []
            board["updated_at"] = time.time()
            return {"ok": True, "action": "init", "board_id": board_id, "goal": board["goal"]}

        board = _get_or_create_board(key)

        if act == "add_fact":
            claim = _clean_text(text)
            if not claim:
                return {"ok": False, "error": "add_fact requires text"}
            if len(board["facts"]) >= MAX_FACTS:
                return {"ok": False, "error": f"fact limit ({MAX_FACTS}) reached on board {board_id!r}"}
            fid = f"F{len(board['facts']) + 1}"
            entry = {"id": fid, "text": claim, "source": _clean_text(source, 240) or "observed"}
            board["facts"].append(entry)
            board["updated_at"] = time.time()
            return {"ok": True, "action": "add_fact", "fact": entry, "fact_count": len(board["facts"])}

        if act == "add_hypothesis":
            claim = _clean_text(text)
            if not claim:
                return {"ok": False, "error": "add_hypothesis requires text"}
            if len(board["hypotheses"]) >= MAX_HYPOTHESES:
                return {"ok": False, "error": f"hypothesis limit ({MAX_HYPOTHESES}) reached"}
            st = str(status or "open").strip().lower() or "open"
            if st not in VALID_HYPOTHESIS_STATUSES:
                return {"ok": False, "error": f"invalid status {st!r} (use open, supported, refuted, confirmed)"}
            conf = 0.5 if confidence is None else max(0.0, min(1.0, float(confidence)))
            hid = f"H{len(board['hypotheses']) + 1}"
            entry = {
                "id": hid,
                "claim": claim,
                "status": st,
                "confidence": round(conf, 3),
                "evidence_for": [_clean_text(evidence_for)] if _clean_text(evidence_for) else [],
                "evidence_against": [_clean_text(evidence_against)] if _clean_text(evidence_against) else [],
                "fact_refs": [str(r).strip() for r in (fact_refs or []) if str(r).strip()][:16],
            }
            board["hypotheses"].append(entry)
            board["updated_at"] = time.time()
            return {"ok": True, "action": "add_hypothesis", "hypothesis": entry}

        if act == "update_hypothesis":
            hid = str(hypothesis_id or "").strip().upper()
            target = next((h for h in board["hypotheses"] if h["id"] == hid), None)
            if target is None:
                return {"ok": False, "error": f"unknown hypothesis_id {hypothesis_id!r}"}
            if status:
                st = str(status).strip().lower()
                if st not in VALID_HYPOTHESIS_STATUSES:
                    return {"ok": False, "error": f"invalid status {st!r}"}
                target["status"] = st
            if confidence is not None:
                target["confidence"] = round(max(0.0, min(1.0, float(confidence))), 3)
            ev_for = _clean_text(evidence_for)
            if ev_for and len(target["evidence_for"]) < 16:
                target["evidence_for"].append(ev_for)
            ev_against = _clean_text(evidence_against)
            if ev_against and len(target["evidence_against"]) < 16:
                target["evidence_against"].append(ev_against)
            if fact_refs:
                existing = set(target["fact_refs"])
                for ref in fact_refs:
                    r_clean = str(ref).strip()
                    if r_clean and r_clean not in existing and len(target["fact_refs"]) < 16:
                        target["fact_refs"].append(r_clean)
                        existing.add(r_clean)
            board["updated_at"] = time.time()
            return {"ok": True, "action": "update_hypothesis", "hypothesis": dict(target)}

        if act == "add_decision":
            choice = _clean_text(text)
            if not choice:
                return {"ok": False, "error": "add_decision requires text"}
            if len(board["decisions"]) >= MAX_DECISIONS:
                return {"ok": False, "error": f"decision limit ({MAX_DECISIONS}) reached"}
            did = f"D{len(board['decisions']) + 1}"
            entry = {
                "id": did,
                "decision": choice,
                "rationale": _clean_text(source),
                "alternatives": [_clean_text(a, 240) for a in (alternatives or []) if _clean_text(a, 240)][:8],
            }
            board["decisions"].append(entry)
            board["updated_at"] = time.time()
            return {"ok": True, "action": "add_decision", "decision": entry}

        if act == "audit":
            return {"ok": True, "action": "audit", "board_id": board_id, **_audit_board(board)}

        if act == "summary":
            return {
                "ok": True,
                "action": "summary",
                "board_id": board_id,
                "goal": board["goal"],
                "facts": list(board["facts"]),
                "hypotheses": [dict(h) for h in board["hypotheses"]],
                "decisions": list(board["decisions"]),
                "audit": _audit_board(board),
            }

        return {
            "ok": False,
            "error": (
                f"unknown action {act!r}; expected init, add_fact, add_hypothesis, "
                "update_hypothesis, add_decision, audit, summary, or clear"
            ),
        }


def _reset_for_tests() -> None:
    with _LOCK:
        _BOARDS.clear()


__all__ = ["reasoning_board"]
