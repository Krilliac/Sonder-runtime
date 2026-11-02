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

import re
import time
from dataclasses import dataclass

from ...domain.meshgen.critique import parse_critique
from ...domain.meshgen.prompts import (
    extract_code, generation_prompt, repair_hint, repair_prompt, revise_prompt,)
from ...domain.meshgen.script_safety import check_script
from ...domain.meshgen.selection import pick_best

_ERROR_CLIP = 1500


@dataclass(frozen=True)
class MeshRequest:
    brief: str
    max_tris: int = 3000
    height_m: float = 0.0
    kit_module: str | None = None
    kit_names: frozenset = frozenset()
    kit_excluded: frozenset = frozenset()
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
    text = ("The model built but has %s triangles; the budget is %d. Cut segments on round parts, "
            "merge or drop small details, and simplify until it is clearly under %d triangles "
            "while keeping it recognisable." % (result.get("tris"), max_tris, max_tris))
    parts = [part for part in (result.get("parts") or []) if isinstance(part, dict)][:5]
    if parts:
        text += " Biggest contributors: " + ", ".join(
            "%s (%s tris)" % (part.get("name"), part.get("tris")) for part in parts) + "."
    return text


def _first_line(text) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def _words(text) -> set:
    return set(re.findall(r"[a-z]{3,}", str(text or "").lower()))


def _same_problem(previous, current) -> bool:
    """True when ``current`` repeats ``previous`` (same error, same top issue, same score)."""
    if not previous or previous["status"] != current["status"]:
        return False
    status = current["status"]
    if status == "build_error":
        return bool(_first_line(current.get("error"))) and (
            _first_line(previous.get("error")) == _first_line(current.get("error")))
    if status == "rejected":
        def strip(issue):
            return re.sub(r"^line \d+:", "", str(issue)).strip()
        return bool(current["issues"]) and bool(previous["issues"]) and (
            strip(previous["issues"][0]) == strip(current["issues"][0]))
    if status == "over_budget":
        return bool(previous["tris"]) and bool(current["tris"]) and current["tris"] >= previous["tris"] * 0.95
    if status == "revise":
        if previous["score"] != current["score"] or not previous["issues"] or not current["issues"]:
            return False
        if not previous["tris"] or not current["tris"] or abs(current["tris"] - previous["tris"]) > 0.15 * previous["tris"]:
            return False
        first, second = _words(previous["issues"][0]), _words(current["issues"][0])
        return bool(first and second) and len(first & second) / len(first | second) >= 0.6
    return False


def _stuck_note(attempt) -> str:
    status = attempt["status"]
    if status == "build_error":
        return ("Your last revision did not fix: %s. The identical failure happened again, so changing "
                "other parts of the script achieves nothing. Change exactly that line this time."
                % _first_line(attempt.get("error")))
    if status == "revise":
        return ("Your last revision did not fix: %s. The score stayed at %s and the geometry barely changed. "
                "Make a real, visible change to exactly that part, and verify the rotation or position "
                "numerically before returning the script." % (attempt["issues"][0], attempt["score"]))
    if status == "rejected":
        return "Your last revision hit the same safety rejection again: %s." % attempt["issues"][0]
    return ("Your last revision did not reduce the triangle count (%s). Remove or simplify the biggest "
            "parts instead of adjusting small ones." % attempt["tris"])


def run(request: MeshRequest, *, generate, build, critique, map_parallel) -> dict:
    started = time.monotonic()
    tiers = tuple(request.tiers) or ("general",)
    base = generation_prompt(
        request.brief, max_tris=request.max_tris, height_m=request.height_m,
        kit_module=request.kit_module, api_doc=request.api_doc,
    )
    vision_off = {"reason": "" if request.vision else "disabled by the caller", "used": 0}

    def attempt_round(index, tier, round_index, prompt, attempt, versions):
        """One round; returns ``(stop, build_prompt)``.

        ``build_prompt(stuck)`` renders the next prompt (``None`` = keep the current one), so a
        no-progress note can be placed inside it, before the closing instruction.
        """
        try:
            reply = generate(tier, prompt)
        except Exception as exc:  # noqa: BLE001 - a model failure ends the round, not the run
            attempt["status"] = "generate_error"
            attempt["error"] = _clip("%s: %s" % (type(exc).__name__, exc), 400)
            return False, None
        code = extract_code(reply)
        reasons = check_script(code, request.kit_module, request.kit_names)
        if reasons:
            attempt["status"] = "rejected"
            attempt["issues"] = reasons[:8]
            failure = "The safety check rejected the script:\n- " + "\n- ".join(reasons[:8])
            return False, lambda stuck: repair_prompt(base, code, failure, stuck=stuck)
        try:
            result = build(index, round_index, code)
        except Exception as exc:  # noqa: BLE001 - a builder crash is a failed build
            result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        if not result.get("ok"):
            attempt["status"] = "build_error"
            attempt["error"] = _clip(result.get("error"), 400)
            failure = _failure_text(result)
            hint = repair_hint(failure, request.kit_module, request.kit_names, request.kit_excluded)
            return False, lambda stuck: repair_prompt(base, code, failure, hint=hint, stuck=stuck)
        within = bool(result.get("within_budget"))
        attempt["tris"] = int(result.get("tris") or 0)
        files = dict(result.get("files") or {})
        version = {"round": round_index, "built": True, "within_budget": within, "tier": tier,
                   "tris": attempt["tris"], "score": None, "issues": [], "files": files,
                   "size": result.get("size")}
        versions.append(version)
        if not within:
            attempt["status"] = "over_budget"
            failure = _over_budget_text(result, request.max_tris)
            return False, lambda stuck: repair_prompt(base, code, failure, stuck=stuck)
        sheet = files.get("sheet")
        if request.vision and not vision_off["reason"] and sheet:
            vision_off["used"] += 1
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
                    return False, lambda stuck: revise_prompt(base, code, parsed, stuck=stuck)
        attempt["status"] = "built"
        return True, None

    def next_tier(current, used):
        """A tier different from ``current``, preferring one this candidate has not used yet."""
        others = [name for name in tiers if name != current]
        if not others:
            return current
        fresh = [name for name in others if name not in used]
        return (fresh or others)[0]

    def run_candidate(index: int) -> dict:
        tier = tiers[index % len(tiers)]
        used = [tier]
        attempts: list[dict] = []
        versions: list[dict] = []
        prompt = base
        for round_index in range(max(1, int(request.rounds))):
            began = time.monotonic()
            attempt = {"tier": tier, "round": round_index, "status": "", "tris": None,
                       "score": None, "issues": [], "seconds": 0.0}
            attempts.append(attempt)
            try:
                stop, build_prompt = attempt_round(index, tier, round_index, prompt, attempt, versions)
            finally:
                attempt["seconds"] = round(time.monotonic() - began, 2)
            previous = attempts[-2] if len(attempts) > 1 else None
            if build_prompt is not None:
                stuck = ""
                if _same_problem(previous, attempt):
                    stuck = _stuck_note(attempt)
                    attempt["stuck"] = True
                    switched = next_tier(tier, used)
                    if switched != tier:
                        tier = switched
                        attempt["escalated_to"] = tier
                        if tier not in used:
                            used.append(tier)
                prompt = build_prompt(stuck)
            if stop:
                break
        best = pick_best(versions)
        return {"index": index, "tier": attempts[0]["tier"], "tiers_used": used, "attempts": attempts,
                "best": best, "built": best is not None}

    outcomes = map_parallel(run_candidate, list(range(max(1, int(request.candidates)))))
    candidates = []
    for position, outcome in enumerate(outcomes):
        if isinstance(outcome, dict):
            candidates.append(outcome)
        else:
            candidates.append({"index": position, "tier": tiers[position % len(tiers)], "attempts": [],
                               "best": None, "built": False, "error": _clip(outcome, 400)})
    ranked = [dict(c["best"], candidate=c["index"], tier=c["best"].get("tier", c["tier"])) if c.get("best") else
              {"built": False, "candidate": c["index"], "tier": c["tier"]} for c in candidates]
    winner = pick_best(ranked)
    if winner is not None and not winner.get("built"):
        winner = None
    if request.vision and not vision_off["reason"]:
        vision = "on" if vision_off["used"] else "unavailable: no candidate reached the critique step"
    else:
        vision = "unavailable: %s" % (vision_off["reason"] or "disabled by the caller")
    return {"ok": winner is not None, "winner": winner, "candidates": candidates, "vision": vision,
            "elapsed_s": round(time.monotonic() - started, 2)}


__all__ = ["MeshRequest", "run"]
