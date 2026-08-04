"""Explain why recorded failures did or did not become pitfall lessons.

A campaign failure is supposed to leave a pitfall lesson behind, but "no
lesson" has several very different causes and the stored rows do not
distinguish them: the gates may have deliberately refused a weak or wrong
lesson, the reply may have carried no code block at all, or the distiller may
have crashed. Answering that question by hand means re-extracting the code,
re-running it to recover the error text, and calling the distiller again --
which is slow to reconstruct and easy to get subtly wrong.

This replays that pipeline for failures that produced no lesson and reports
the exact stage each one stopped at:

    no-code        the reply had no code block for its language
    infra-toolchain  the compiler/runtime was missing - not the model's fault
    infra-timeout  execution or compilation timed out - usually load, but a
                   generated infinite loop lands here too, so it is reported
                   separately rather than assumed environmental
    ran-clean      the code now passes AND matches expected output, so the
                   recorded failure is not reproducible
    refused        the distiller ran and the gates rejected the candidate
    candidate      a lesson WOULD be stored; absence means the storage step
                   never ran (e.g. the feature post-dated the run)
    crashed        the distiller raised

The campaign scores a run by exact expected output, not just exit status, so
this applies the same ``server._campaign_expected`` /
``_campaign_output_matches`` check. Skipping it would report every
wrong-output failure -- the most common kind -- as "ran-clean".

Found with this on 2026-08-03: replaying 10 failures while the nightly held
the GPU produced crashed=2, and the same interactions replayed cleanly when
idle. The distiller's budget is 20s (SONDER_DISTILLATION_TIMEOUT), so a
contended GPU pushes the learning call past it and _generate_text raises
ModelCallError. That used to vanish into a bare except and read as "nothing
worth learning"; the note returned by _record_failure_pitfall now names the
exception in the nightly log. Run this against a quiet GPU when you want the
gate verdict, and against a busy one when you want the crash rate.

Usage (repo root, runtime venv, Ollama serving):

    python scripts/replay_failure_pitfall.py --hours 12
    python scripts/replay_failure_pitfall.py --iid 118d7ec964143767
    python scripts/replay_failure_pitfall.py --hours 24 --limit 5 --no-rerun

Timestamps in memory.db are UTC text, so --hours is counted in UTC. Replaying
re-executes generated code, exactly as the campaign did; pass --no-rerun to
skip execution and use a placeholder error when only the gate verdict matters.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grounding  # noqa: E402
import reflection  # noqa: E402
import server  # noqa: E402

_LANGUAGES = ("powershell", "python", "javascript", "csharp", "cpp")


def _language_of(task):
    lowered = str(task or "").lower()
    for language in _LANGUAGES:
        if language in lowered:
            return language
    return "python"


def _rows(conn, iid, hours, limit):
    if iid:
        return list(
            conn.execute(
                "SELECT i.id, i.task, i.response FROM interactions i "
                "WHERE i.id=?",
                (iid,),
            )
        )
    since = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=hours)
    ).strftime("%Y-%m-%d %H:%M:%S")
    # Only failures that left no lesson are interesting; one that distilled
    # cleanly needs no explanation.
    return list(
        conn.execute(
            "SELECT i.id, i.task, i.response FROM interactions i "
            "JOIN outcomes o ON o.interaction_id = i.id "
            "WHERE o.signal='failed' AND o.ts >= ? "
            "AND NOT EXISTS(SELECT 1 FROM lessons l "
            "               WHERE l.source_interaction = i.id) "
            "ORDER BY o.ts DESC LIMIT ?",
            (since, limit),
        )
    )


def _campaign_task_name(task):
    """Map a campaign prompt back to the task name that owns its expected output."""
    lowered = str(task or "").lower()
    for name, text in server._CAMPAIGN_TASKS:
        if str(text or "").lower() in lowered:
            return name
    return ""


def _recover_error(task, response, rerun):
    language = _language_of(task)
    code = grounding.extract_code_block(response, language)
    if not code:
        return code, "no %s code block returned" % language, "no-code"
    if not rerun:
        return code, "replay placeholder: original error not re-executed", ""
    ok, out = grounding.run_language_code(
        code, language=language, timeout=60, execute=True
    )
    if not ok:
        if "missing runtime/compiler" in out:
            return code, out, "infra-toolchain"
        if "timed out after" in out:
            return code, out, "infra-timeout"
        return code, out, ""
    # Exit status alone is not the campaign's bar: it also requires the
    # output to match exactly, and a wrong answer is a model failure.
    name = _campaign_task_name(task)
    if not name:
        # Repo-repair interactions land here: they are scored by running the
        # template's pytest suite, not by expected stdout, and the planted-bug
        # project cannot be reconstructed from the interaction alone. Saying
        # "ran-clean" because the extracted module imported cleanly would be a
        # verdict this script has not earned.
        return code, out, "unscorable"
    expected = server._campaign_expected(name)
    if expected and not server._campaign_output_matches(out, expected):
        return code, "wrong output; expected exactly %r, got %r" % (
            expected, out), ""
    return code, out, "ran-clean"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iid", default="", help="replay one interaction id")
    parser.add_argument("--hours", type=int, default=12)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--no-rerun", action="store_true",
        help="skip re-executing the code (gate verdict only)",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(server._DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = _rows(conn, args.iid.strip(), args.hours, args.limit)
    finally:
        conn.close()

    if not rows:
        print("no unexplained failures in range")
        return 0

    print("replaying %d failure(s)\n" % len(rows))
    tally = {}
    for row in rows:
        task = str(row["task"] or "")
        print("=" * 70)
        print("interaction : %s" % row["id"])
        print("task        : %s" % task[:90].replace("\n", " "))
        code, error, stage = _recover_error(
            task, str(row["response"] or ""), not args.no_rerun
        )
        if not stage:
            try:
                candidate = reflection.prepare_pitfall_candidate(
                    task[:4000], str(row["response"] or "")[:4000], error,
                    offload_fn=lambda prompt, **options: server._generate_text(
                        prompt,
                        timeout=server._distillation_timeout_seconds(),
                        **options
                    ),
                )
                stage = (
                    "candidate"
                    if candidate.get("status") == "candidate"
                    else "refused"
                )
                print("error       : %s" % repr(error[:160]))
                print("verdict     : %s" % stage)
                if stage == "candidate":
                    print("would store : %s" % candidate.get("text"))
                else:
                    print("reason      : %s" % candidate.get("reason"))
            except Exception:
                stage = "crashed"
                print("error       : %s" % repr(error[:160]))
                traceback.print_exc()
        else:
            print("error       : %s" % repr(error[:160]))
            print("verdict     : %s" % stage)
        tally[stage] = tally.get(stage, 0) + 1

    print("\n" + "=" * 70)
    print("summary: " + ", ".join(
        "%s=%d" % (k, v) for k, v in sorted(tally.items())
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
