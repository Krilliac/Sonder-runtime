"""Pure job-planning and report-formatting helpers for parallel and campaign runs."""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

SUPPORTED_CAMPAIGN_LANGUAGES = frozenset(
    {"python", "javascript", "powershell", "cpp", "csharp"}
)


def plan_language_jobs(
    languages: str,
    variants_per_language: int,
    *,
    normalize_language: Callable[[str], str],
) -> tuple[list[tuple[str, int]], str]:
    """Validate requested languages and build (language, variant) job tuples."""
    language_list = [
        normalize_language(x)
        for x in (languages or "").split(",")
        if x.strip()
    ]
    if not language_list:
        return [], "at least one language is required"
    bad = [x for x in language_list if x not in SUPPORTED_CAMPAIGN_LANGUAGES]
    if bad:
        return [], "unsupported language(s): %s" % ", ".join(bad)
    variants = max(1, min(int(variants_per_language or 1), 6))
    jobs: list[tuple[str, int]] = []
    for lang in language_list:
        for i in range(variants):
            jobs.append((lang, i + 1))
    return jobs, ""


def plan_campaign_jobs(
    languages: str,
    total: int,
    tasks: Sequence[tuple[str, str]],
    *,
    normalize_language: Callable[[str], str],
) -> tuple[list[tuple[int, str, str, str]], str]:
    """Select supported campaign languages and distribute tasks without residue skew."""
    language_list = [
        normalize_language(x)
        for x in (languages or "").split(",")
        if x.strip()
    ]
    language_list = [x for x in language_list if x in SUPPORTED_CAMPAIGN_LANGUAGES]
    if not language_list:
        return [], "no supported languages selected"
    jobs: list[tuple[int, str, str, str]] = []
    for i in range(total):
        lang = language_list[i % len(language_list)]
        task_name, task_text = tasks[(i // len(language_list)) % len(tasks)]
        jobs.append((i, lang, task_name, task_text))
    return jobs, ""


def format_parallel_generate_report(
    generation_results: Sequence[Mapping[str, Any] | None],
    *,
    variants: int,
    elapsed: float,
    tier: str,
    workers_label: str,
) -> str:
    """Format the summary and winning candidate block for parallel_generate_run."""
    passed = sum(1 for r in generation_results if r and r.get("ok"))
    lines = [
        "parallel generate/run: %d/%d passed in %.3fs (tier=%s, %s)"
        % (passed, variants, elapsed, tier, workers_label)
    ]
    for r in generation_results:
        if r is None:
            continue
        status = "PASS" if r.get("ok") else "FAIL"
        lines.append("[%s] %s" % (status, r.get("name")))
        out = str(r.get("output") or "").strip()
        if out:
            lines.append(out[:1200])
    winner = next(
        (r for r in generation_results if r and r.get("ok") and r.get("code")),
        None,
    )
    if winner:
        lines.append("winner code:")
        lines.append("```python\n%s\n```" % winner["code"])
    return "\n".join(lines)


def format_parallel_languages_report(
    results: Sequence[Mapping[str, Any] | None],
    *,
    elapsed: float,
    tier: str,
    workers_label: str,
    approval_required_type: type[Any],
    lang_fences: Mapping[str, str],
) -> str:
    """Format the summary and winner blocks for parallel_generate_run_languages."""
    passed = sum(1 for r in results if r and r.get("ok"))
    lines = [
        "parallel multi-language generate/run: %d/%d passed in %.3fs (tier=%s, %s)"
        % (passed, len(results), elapsed, tier, workers_label)
    ]
    for r in results:
        if r is None:
            continue
        status = (
            "PASS"
            if r.get("ok")
            else "SKIP"
            if isinstance(r.get("output"), approval_required_type)
            else "FAIL"
        )
        lines.append("[%s] %s [%s]" % (status, r.get("name"), r.get("language")))
        out = str(r.get("output") or "").strip()
        if out:
            lines.append(out[:1200])
    winners = [r for r in results if r and r.get("ok") and r.get("code")]
    if winners:
        lines.append("winner code blocks:")
        for r in winners[:3]:
            lang = str(r["language"])
            fence = lang_fences.get(lang, lang)
            lines.append("```%s\n%s\n```" % (fence, r["code"]))
    return "\n".join(lines)


def format_campaign_report(
    results: Sequence[Mapping[str, Any] | None],
    *,
    elapsed: float,
    headline_fn: Callable[[int, int, int, int, int, float], str],
    drain_summary: str = "",
) -> str:
    """Summarize multi-language campaign results, per-language pass rates, and attempts."""
    valid = [r for r in results if r is not None]
    passed = sum(1 for r in valid if r.get("ok"))
    recorded = sum(
        1
        for r in valid
        for a in r.get("attempts", ())
        if a.get("ok") and a.get("record")
    )
    failed_recorded = sum(
        1
        for r in valid
        for a in r.get("attempts", ())
        if not a.get("ok") and a.get("record")
    )
    pitfall_errors = [
        str(a.get("pitfall_error"))
        for r in valid
        for a in r.get("attempts", ())
        if a.get("pitfall_error")
    ]
    env_skipped = sum(
        1
        for r in valid
        for a in r.get("attempts", ())
        if a.get("env_skipped")
    )
    by_lang: dict[str, tuple[int, int]] = {}
    for r in valid:
        lang = str(r["language"])
        ok, total_lang = by_lang.get(lang, (0, 0))
        by_lang[lang] = (ok + (1 if r.get("ok") else 0), total_lang + 1)
    lines = [
        headline_fn(
            passed,
            len(results),
            recorded,
            failed_recorded,
            len(pitfall_errors),
            elapsed,
        ),
        "by language: %s"
        % ", ".join(
            "%s=%d/%d" % (lang, ok, total_lang)
            for lang, (ok, total_lang) in sorted(by_lang.items())
        ),
    ]
    if pitfall_errors:
        lines.append("first pitfall error: %s" % pitfall_errors[0])
    if env_skipped:
        lines.append(
            "environment failures skipped, not recorded against the model: %d"
            % env_skipped,
        )
    if drain_summary:
        lines.append(drain_summary)
    for r in valid:
        attempts = r.get("attempts") or []
        last_attempt = attempts[-1]
        status = (
            "PASS"
            if r["ok"]
            else "SKIP"
            if last_attempt.get("approval_required")
            else "FAIL"
        )
        lines.append(
            "[%s] %s attempts=%d iid=%s"
            % (status, r["name"], len(attempts), r.get("iid") or "-")
        )
        final_out = str(last_attempt.get("output") or "").strip()
        if final_out:
            lines.append(final_out[:800])
        record_msg = str(last_attempt.get("record") or "").strip()
        if record_msg:
            lines.append(record_msg[:800])
    return "\n".join(lines)


def format_repo_repair_report(
    results: Sequence[Mapping[str, Any] | None],
    *,
    elapsed: float,
    drain_summary: str = "",
) -> str:
    """Format the summary report for campaign_repo_repair."""
    valid = [r for r in results if r is not None]
    passed = sum(1 for r in valid if r.get("ok"))
    infra_skipped = sum(1 for r in valid if r.get("infra"))
    pitfall_errors = sum(
        1
        for r in valid
        for a in r.get("attempts", ())
        if a.get("pitfall_error")
    )
    lines = [
        "campaign repo-repair: %d/%d suites fixed in %.3fs%s%s"
        % (
            passed,
            len(results),
            elapsed,
            (" (%d job(s) unattributable — no outcome recorded)" % infra_skipped)
            if infra_skipped
            else "",
            (" (%d pitfall-error(s))" % pitfall_errors) if pitfall_errors else "",
        ),
    ]
    if infra_skipped and infra_skipped >= max(1, len(results) // 2):
        lines.append(
            "WARNING: most jobs failed for infrastructure reasons (commonly "
            "memory pressure with too many workers) — lower max_workers or "
            "free memory; these jobs taught the model nothing.",
        )
    if drain_summary:
        lines.append(drain_summary)
    for r in valid:
        status = "PASS" if r["ok"] else "FAIL"
        attempts = r.get("attempts") or []
        lines.append(
            "[%s] %s-%d attempts=%d iid=%s%s"
            % (
                status,
                r["task"],
                int(r["index"]) + 1,
                len(attempts),
                r.get("iid") or "-",
                (" (%s)" % r["error"]) if r.get("error") else "",
            )
        )
        if attempts and not r["ok"]:
            lines.append(str(attempts[-1].get("output") or "")[:400])
        if attempts and attempts[-1].get("record"):
            lines.append(str(attempts[-1]["record"])[:200])
    return "\n".join(lines)


def resolve_game_campaign_matrix(
    target_language: str,
    target_dimension: str,
    default_matrix: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Build the (language, dimension) job matrix for game_generation_campaign."""
    language_order = tuple(dict.fromkeys(lang for lang, _ in default_matrix))
    if target_language and target_dimension:
        return ((target_language, target_dimension),)
    if target_language:
        return tuple((target_language, dim) for dim in ("2d", "2.5d", "3d"))
    if target_dimension:
        return tuple((lang, target_dimension) for lang in language_order)
    return tuple(default_matrix)


def format_game_generate_report(result: Mapping[str, Any]) -> str:
    """Format the operator report for game_generate_and_test."""
    lines = [
        "generated game: %s" % ("PASS" if result["ok"] else "FAIL"),
        "  target: %s / %s" % (result["language"], result["dimension"]),
        "  attempts: %d" % len(result["attempts"]),
        "  model result: %s | reference fallback: %s"
        % (
            "PASS" if result.get("model_ok") else "FAIL",
            "used" if result.get("fallback_used") else "not used",
        ),
        "  root: %s" % result["root"],
    ]
    for attempt in result["attempts"]:
        lines.append(
            "  [%s] attempt %d (%s) iid=%s"
            % (
                "PASS" if attempt["ok"] else "FAIL",
                attempt["attempt"],
                attempt.get("kind", "model"),
                attempt.get("iid") or "-",
            )
        )
        if attempt.get("output"):
            lines.append(str(attempt["output"])[:1200])
        if attempt.get("record"):
            lines.append(str(attempt["record"])[:500])
    return "\n".join(lines)


def format_game_campaign_report(
    results: Sequence[Mapping[str, Any] | None],
    *,
    total: int,
    elapsed: float,
    workers: int,
    target_language: str,
    target_dimension: str,
) -> tuple[str, int]:
    """Format the summary report for game_generation_campaign and return (text, passed)."""
    valid = [r for r in results if r is not None]
    passed = sum(1 for r in valid if r.get("ok"))
    model_passed = sum(1 for r in valid if r.get("model_ok"))
    fallback_passed = sum(
        1 for r in valid if r.get("ok") and r.get("fallback_used")
    )
    lines = [
        "greenfield game campaign: %d/%d runnable in %.3fs "
        "(model=%d, reference-fallback=%d, workers=%d, target=%s/%s)"
        % (
            passed,
            total,
            elapsed,
            model_passed,
            fallback_passed,
            workers,
            target_language or "mixed",
            target_dimension or "mixed",
        ),
    ]
    for result in valid:
        attempts = result.get("attempts") or []
        final = attempts[-1]
        lines.append(
            "[%s] %s/%s model=%s fallback=%s attempts=%d root=%s"
            % (
                "PASS" if result.get("ok") else "FAIL",
                result.get("language"),
                result.get("dimension"),
                "PASS" if result.get("model_ok") else "FAIL",
                "yes" if result.get("fallback_used") else "no",
                len(attempts),
                result.get("root") or "-",
            )
        )
        if final.get("output"):
            lines.append(str(final["output"])[:900])
        if final.get("record"):
            lines.append(str(final["record"])[:400])
    return "\n".join(lines), passed
