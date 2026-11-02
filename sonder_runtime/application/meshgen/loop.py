"""Closed generate -> gate -> build -> render -> critique -> revise loop.

All I/O is injected, so the loop is deterministic under test:

* ``generate(tier, prompt) -> str``     one model completion
* ``build(candidate_index, round_index, code) -> dict``  builds the script
  (``ok``, ``tris``, ``within_budget``, ``error``, ``files`` with an optional
  ``sheet``); it may raise, which is treated as a failed build
* ``critique(sheet_path) -> str | None``  vision reply; ``None`` = unavailable
* ``map_parallel(fn, items) -> list``    ordered parallel map

Each candidate runs on its own thread of control and keeps every attempt plus
its best version; the overall winner is :func:`pick_best` over those.  The
report contains no code bodies.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ...domain.meshgen.critique import parse_critique
from ...domain.meshgen.prompts import extract_code, generation_prompt, repair_prompt, revise_prompt
from ...domain.meshgen.script_safety import check_script
from ...domain.meshgen.selection import pick_best

_ERROR_CLIP = 1500


@dataclass(frozen=True)
class MeshRequest:
    brief: str
    max_tris: int = 3000
    height_m: float = 0.0
    kit_module: str | None = None
    api_doc: str = ""
    candidates: int = 3
    rounds: int = 3
    tiers: tuple = ("general", "code")
    target_score: int = 8
    vision: bool = True


def _clip(text, limit=_ERROR_CLIP):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + " ..."


def _failure_text(result: dict) -> str:
    text = str(result.get("error") or "build failed")
    tail = result.get("tail") or result.get("traceback")
    if tail:
        text += "\n" + "\n".join(str(line) for line in tail)
    return _clip(text)


def _over_budget_text(result: dict, max_tris: int) -> str:
    return ("The model built but has %s triangles; the budget is %d. Cut segments on round parts, "
            "merge or drop small details, and simplify until it is clearly under %d triangles "
            "while keeping it recognisable." % (result.get("tris"), max_tris, max_tris))


def run(request: MeshRequest, *, generate, build, critique, map_parallel) -> dict:
    started = time.monotonic()
    tiers = tuple(request.tiers) or ("general",)
    base = generation_prompt(
        request.brief, max_tris=request.max_tris, height_m=request.height_m,
        kit_module=request.kit_module, api_doc=request.api_doc,
    )
    vision_off = {"reason": "" if request.vision else "disabled by the caller"}

    def attempt_round(index, tier, round_index, prompt, attempt, versions):
        """One round; returns ``(stop, next_prompt)`` (``next_prompt`` None = keep the prompt)."""
        try:
            reply = generate(tier, prompt)
        except Exception as exc:  # noqa: BLE001 - a model failure ends the round, not the run
            attempt["status"] = "generate_error"
            attempt["error"] = _clip("%s: %s" % (type(exc).__name__, exc), 400)
            return False, None
        code = extract_code(reply)
        reasons = check_script(code, request.kit_module)
        if reasons:
            attempt["status"] = "rejected"
            attempt["issues"] = reasons[:8]
            return False, repair_prompt(
                base, code, "The safety check rejected the script:\n- " + "\n- ".join(reasons[:8]))
        try:
            result = build(index, round_index, code)
        except Exception as exc:  # noqa: BLE001 - a builder crash is a failed build
            result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        if not result.get("ok"):
            attempt["status"] = "build_error"
            attempt["error"] = _clip(result.get("error"), 400)
            return False, repair_prompt(base, code, _failure_text(result))
        within = bool(result.get("within_budget"))
        attempt["tris"] = int(result.get("tris") or 0)
        files = dict(result.get("files") or {})
        version = {"round": round_index, "built": True, "within_budget": within,
                   "tris": attempt["tris"], "score": None, "issues": [], "files": files,
                   "size": result.get("size")}
        versions.append(version)
        if not within:
            attempt["status"] = "over_budget"
            return False, repair_prompt(base, code, _over_budget_text(result, request.max_tris))
        sheet = files.get("sheet")
        if request.vision and not vision_off["reason"] and sheet:
            try:
                verdict = critique(sheet)
            except Exception as exc:  # noqa: BLE001 - lost vision degrades to build-only
                verdict = None
                vision_off["reason"] = _clip("%s: %s" % (type(exc).__name__, exc), 200)
            if verdict is None:
                vision_off["reason"] = vision_off["reason"] or "no vision model answered"
            else:
                parsed = parse_critique(verdict)
                version["score"] = attempt["score"] = parsed["score"]
                version["issues"] = attempt["issues"] = list(parsed["issues"])
                if parsed["score"] is not None:
                    if parsed["score"] >= request.target_score:
                        attempt["status"] = "accepted"
                        return True, None
                    attempt["status"] = "revise"
                    return False, revise_prompt(base, code, parsed)
        attempt["status"] = "built"
        return True, None

    def run_candidate(index: int) -> dict:
        tier = tiers[index % len(tiers)]
        attempts: list[dict] = []
        versions: list[dict] = []
        prompt = base
        for round_index in range(max(1, int(request.rounds))):
            began = time.monotonic()
            attempt = {"tier": tier, "round": round_index, "status": "", "tris": None,
                       "score": None, "issues": [], "seconds": 0.0}
            attempts.append(attempt)
            try:
                stop, next_prompt = attempt_round(index, tier, round_index, prompt, attempt, versions)
            finally:
                attempt["seconds"] = round(time.monotonic() - began, 2)
            if next_prompt is not None:
                prompt = next_prompt
            if stop:
                break
        best = pick_best(versions)
        return {"index": index, "tier": tier, "attempts": attempts, "best": best,
                "built": best is not None}

    outcomes = map_parallel(run_candidate, list(range(max(1, int(request.candidates)))))
    candidates = []
    for position, outcome in enumerate(outcomes):
        if isinstance(outcome, dict):
            candidates.append(outcome)
        else:
            candidates.append({"index": position, "tier": tiers[position % len(tiers)], "attempts": [],
                               "best": None, "built": False, "error": _clip(outcome, 400)})
    ranked = [dict(c["best"], candidate=c["index"], tier=c["tier"]) if c.get("best") else
              {"built": False, "candidate": c["index"], "tier": c["tier"]} for c in candidates]
    winner = pick_best(ranked)
    if winner is not None and not winner.get("built"):
        winner = None
    if request.vision and not vision_off["reason"]:
        vision = "on"
    else:
        vision = "unavailable: %s" % (vision_off["reason"] or "disabled by the caller")
    return {"ok": winner is not None, "winner": winner, "candidates": candidates, "vision": vision,
            "elapsed_s": round(time.monotonic() - started, 2)}


__all__ = ["MeshRequest", "run"]
