"""Independent two-tier consultation and agreement reporting.

The answers are never merged or ranked. Agreement is normally judged by a
third model call. If that judge is unavailable or does not begin with YES/NO,
the verdict falls back to a conservative normalized-token Jaccard overlap
(at least 0.5), and confidence remains ``unknown`` to expose that downgrade.
"""

import re
import sys


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_JUDGE_RE = re.compile(r"^\s*(YES|NO)\b", re.IGNORECASE)
_HEURISTIC_AGREEMENT_THRESHOLD = 0.5


def _default_ask(prompt, tier):
    # server.py is intentionally huge and imports this module to expose the MCP
    # tool. Delaying this import avoids both startup cost and a circular import.
    import server

    return server.ensemble_answer(prompt, tiers=tier, mode="general")


def _failure(text):
    value = str(text or "").strip()
    lowered = value.lower()
    return value.startswith("ERROR:") or "no model produced an answer" in lowered


def _ask(ask_fn, prompt, tier):
    try:
        text = str(ask_fn(prompt, tier) or "").strip()
    except Exception as exc:
        return "ERROR: %s" % exc, True
    return text, _failure(text)


def _judge_prompt(prompt, answers):
    """Judge substantive agreement across N answers. `answers` is [(tier, text)]."""
    blocks = "\n\n".join("ANSWER FROM %s:\n%s" % (tier, text) for tier, text in answers)
    return (
        "Do these answers agree on the substance? Reply YES or NO and one "
        "sentence. Judge only whether their conclusions and recommendations "
        "are compatible; different wording is not disagreement.\n\n"
        "QUESTION:\n%s\n\n%s" % (prompt, blocks)
    )


def _token_overlap(answer_a, answer_b):
    tokens_a = set(_TOKEN_RE.findall(answer_a.lower()))
    tokens_b = set(_TOKEN_RE.findall(answer_b.lower()))
    if not tokens_a or not tokens_b:
        return False
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b) >= (
        _HEURISTIC_AGREEMENT_THRESHOLD
    )


def _all_overlap(texts):
    """True only when every pair of answers clears the overlap threshold.

    A single divergent answer among several is a disagreement -- the heuristic
    must not report agreement just because most pairs happen to overlap.
    """
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if not _token_overlap(texts[i], texts[j]):
                return False
    return True


def default_tiers(cloud_ok=None):
    """The tiers a consult contrasts by default.

    Always at least two independent LOCAL models (``code`` + ``reasoning``); a
    cloud model JOINS them when cloud is enabled, so an enabled cloud tier is
    actually used but a disabled one never blocks the consult. `cloud_ok` is an
    injection seam for tests; when None it is read from ``server.cloud_allowed``.
    """
    tiers = ["code", "reasoning"]
    if cloud_ok is None:
        try:
            import server

            cloud_ok = bool(server.cloud_allowed())
        except Exception:
            cloud_ok = False
    if cloud_ok:
        tiers.append("cloud-general")
    return tiers


def consult(prompt, tiers=None, *, ask_fn=None, cloud_ok=None):
    """Ask ``prompt`` to several independent tiers and report their agreement.

    ``tiers`` is a list of tier names; when None it is :func:`default_tiers`
    (two local models plus a cloud model when cloud is enabled). The answers are
    never merged or ranked -- each is shown verbatim and a judge model decides
    whether they agree on substance. Two good answers still yield a verdict even
    if a third tier fails; only fewer than two successes leaves it unknown.

    ``ask_fn(prompt, tier) -> str`` is an injection seam for offline tests, and
    ``cloud_ok`` forces the cloud decision for :func:`default_tiers` in tests.
    Returned model errors are failures, not answers; a failed or malformed judge
    falls back to normalized-token overlap and leaves confidence unknown.
    """
    question = str(prompt or "")
    ask = ask_fn or _default_ask
    if tiers is None:
        tiers = default_tiers(cloud_ok)
    # De-duplicate while preserving order: asking the same tier twice buys no
    # independence and would let one model "agree with itself".
    ordered = []
    for tier in tiers:
        if tier not in ordered:
            ordered.append(tier)

    graded = [(tier,) + _ask(ask, question, tier) for tier in ordered]
    answers = [
        {"tier": tier, "text": "[failed] %s" % text if failed else text}
        for tier, text, failed in graded
    ]
    ok = [(tier, text) for tier, text, failed in graded if not failed]
    failed_tiers = [tier for tier, _text, failed in graded if failed]
    tail = "" if not failed_tiers else " (no answer from %s)" % ", ".join(failed_tiers)

    if len(ok) < 2:
        return {
            "prompt": question,
            "answers": answers,
            "agree": None,
            "confidence": "unknown",
            "note": "Need two answers to compare; failed for tier(s): %s."
            % (", ".join(failed_tiers) or "unknown"),
        }

    judge, judge_failed = _ask(ask, _judge_prompt(question, ok), ok[0][0])
    match = None if judge_failed else _JUDGE_RE.match(judge)
    if match is None:
        agree = _all_overlap([text for _tier, text in ok])
        note = "Judge failed; token-overlap heuristic says the tiers %s.%s" % (
            "agree" if agree else "disagree",
            tail,
        )
        confidence = "unknown"
    else:
        agree = match.group(1).upper() == "YES"
        confidence = "high" if agree else "low"
        note = ("All tiers agree on the substance." if agree else "Tiers disagree.") + tail

    return {
        "prompt": question,
        "answers": answers,
        "agree": agree,
        "confidence": confidence,
        "note": note,
    }


def verdict_line(result, encoding=None):
    """Return a clear verdict, falling back to ASCII for legacy consoles."""
    agree = result.get("agree")
    confidence = result.get("confidence")
    if agree is True and confidence == "high":
        rich, plain = "✓ tiers agree", "[OK] tiers agree"
    elif agree is False and confidence == "low":
        rich = "⚠ tiers DISAGREE — treat as low confidence"
        plain = "[WARNING] tiers DISAGREE - treat as low confidence"
    elif agree is True:
        rich = "? tiers tentatively agree — confidence unknown"
        plain = "[?] tiers tentatively agree - confidence unknown"
    elif agree is False:
        rich = "⚠ tiers DISAGREE (heuristic only) — confidence unknown"
        plain = "[WARNING] tiers DISAGREE (heuristic only) - confidence unknown"
    else:
        rich = "? agreement unknown — %s" % result.get("note", "answer failed")
        plain = "[?] agreement unknown - %s" % result.get("note", "answer failed")

    target_encoding = encoding or getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        rich.encode(target_encoding)
    except (UnicodeEncodeError, LookupError, TypeError):
        return plain
    return rich


def format_result(result, encoding=None):
    """Format both independent answers followed by the agreement verdict."""
    sections = [
        "=== %s ===\n%s" % (answer["tier"], answer["text"])
        for answer in result["answers"]
    ]
    sections.append(verdict_line(result, encoding=encoding))
    return "\n\n".join(sections)
