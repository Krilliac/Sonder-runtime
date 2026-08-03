"""Nightly self-improvement cycle: exercise, learn, and groom the stores.

One bounded pass over everything Sonder's learning loop can do unattended:

1. campaign  — a modest generate/compile/execute/record wave (real outcomes,
               inline lesson distillation, campaign-end deferred-job drain).
2. drain     — retry any deferred lesson distillations left over.
3. backfill  — refresh stale/missing lesson and interaction embeddings.
4. prune     — delete near-duplicate lessons (keeps one representative).
5. health    — learning health + accelerator one-liners for the log.
6. proposals — turn host-observed findings into *proposed* goals. Nothing
               is started or adopted automatically; the queue waits for
               an explicit ``/goal adopt``.
7. winml     — recheck whether the Windows ML catalog now offers the AMD
               VitisAI execution provider for this NPU driver; a flip from
               absent to present is logged loudly (it means real NPU
               execution is one reconnect away).

``--rounds N`` repeats the exercise-and-groom cycle N times, which is how
this is meant to run overnight: several waves with grooming between them
rather than one wave and eight idle hours. A lock file makes overlapping
invocations exit immediately, so extra scheduled triggers are a redundancy
rather than a collision.

Deliberately absent, because this runs with nobody watching: it never edits
a source file, never commits or pushes, and never starts an autopilot run
with workspace policy. It exercises the model, records outcomes, grooms the
stores, and queues proposals for a human to read in the morning.

Every stage is fail-soft and bounded; a down Ollama skips the model-bound
stages and still grooms what it can. Output goes to stdout and to
``<state home>/nightly-logs/YYYY-MM-DD.log``.

Usage (repo root, runtime venv):

    python scripts/nightly_self_improve.py
    python scripts/nightly_self_improve.py --campaign-total 12 --skip-campaign

Register as a daily task (run from an elevated or normal prompt):

    schtasks /create /tn SonderNightly /sc daily /st 03:30 /tr
      "<venv>\\python.exe <repo>\\scripts\\nightly_self_improve.py"
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _stage(log, name, fn):
    started = time.time()
    try:
        result = fn()
        log("[%s] ok in %.0fs%s" % (
            name, time.time() - started,
            (": %s" % result) if isinstance(result, str) and result else "",
        ))
        return result
    except Exception as exc:
        log("[%s] FAILED after %.0fs: %s" % (
            name, time.time() - started, str(exc)[:300]))
        return None


def _first_line(text):
    return str(text or "").strip().splitlines()[0] if text else ""


def _claim_lock(path, log) -> bool:
    """Exclusive-create a lock so overlapping triggers are a no-op.

    A stale lock older than six hours is reclaimed: a killed run must not
    silently disable every later night.
    """
    try:
        if path.exists():
            age = time.time() - path.stat().st_mtime
            if age < 6 * 3600:
                log("another nightly run holds the lock (%.0fm old); exiting"
                    % (age / 60))
                return False
            log("reclaiming a stale lock (%.1fh old)" % (age / 3600))
            path.unlink(missing_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return True
    except FileExistsError:
        log("another nightly run claimed the lock first; exiting")
        return False
    except OSError as exc:
        log("lock unavailable (%s); continuing without one" % str(exc)[:80])
        return True


def _winml_vitisai_check(log):
    """Report whether the Windows ML EP catalog offers VitisAI right now."""
    try:
        from winui3.microsoft.windows.applicationmodel.dynamicdependency \
            import bootstrap
        import winui3.microsoft.windows.ai.machinelearning as winml
    except Exception as exc:
        return "winml projection unavailable (%s)" % str(exc)[:80]
    with bootstrap.initialize(
        options=bootstrap.InitializeOptions.ON_NO_MATCH_SHOW_UI,
    ):
        catalog = winml.ExecutionProviderCatalog.get_default()
        names = [p.name for p in catalog.find_all_providers()]
    if any("VitisAI" in name for name in names):
        log("*** WinML catalog NOW OFFERS VitisAI for this driver — real "
            "NPU execution is available; reprovision with providers "
            "vitisai-first and reconnect Sonder. ***")
        return "VitisAI AVAILABLE"
    return "VitisAI absent (catalog: %s)" % ", ".join(names or ["none"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--campaign-total", type=int, default=24)
    parser.add_argument("--repair-total", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=1,
                        help="repeat the exercise-and-groom cycle N times")
    parser.add_argument("--skip-campaign", action="store_true")
    args = parser.parse_args()

    import sonder_paths

    log_dir = Path(sonder_paths.state_path("nightly-logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / (time.strftime("%Y-%m-%d") + ".log")
    sink = log_path.open("a", encoding="utf-8")

    def log(message):
        line = "%s %s" % (time.strftime("%H:%M:%S"), message)
        print(line)
        sink.write(line + "\n")
        sink.flush()

    lock = Path(sonder_paths.state_path("nightly.lock"))
    if not _claim_lock(lock, log):
        sink.close()
        return 0

    log("=== nightly self-improvement start ===")
    import server
    import lesson_pruner
    import memory_store

    rounds = max(1, min(int(args.rounds or 1), 12))
    for round_index in range(rounds):
        if rounds > 1:
            log("--- round %d/%d ---" % (round_index + 1, rounds))

        if not args.skip_campaign:
            def campaign():
                out = server.campaign_generate_compile_execute_record(
                    total=max(1, args.campaign_total), repair_rounds=1,
                    timeout=12, record_failures=True,
                )
                return _first_line(out)
            _stage(log, "campaign", campaign)

            def repair():
                out = server.campaign_repo_repair(
                    total=max(1, args.repair_total), max_workers=2,
                    repair_rounds=2, timeout=45,
                )
                return _first_line(out)
            _stage(log, "repo-repair", repair)

    def drain():
        result = server._drain_deferred_distillations(limit=32)
        return json.dumps(result)
    _stage(log, "distillation-drain", drain)

    def backfill():
        lessons = _first_line(
            server.memory_embedding_backfill(limit=100, apply=True))
        interactions = _first_line(
            server.memory_interaction_embedding_backfill(
                limit=100, apply=True))
        return "%s | %s" % (lessons, interactions)
    _stage(log, "embedding-backfill", backfill)

    def prune():
        conn = memory_store.connect(sonder_paths.memory_db_path())
        try:
            plan, deleted = lesson_pruner.prune(conn, dry_run=False)
        finally:
            conn.close()
        return "pruned %d duplicate lesson(s) in %d cluster(s)" % (
            deleted, len(plan))
    _stage(log, "lesson-prune", prune)

    def health():
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            summary = server.learning_health_status()
        return _first_line(summary) or _first_line(buffer.getvalue())
    _stage(log, "learning-health", health)

    def goal_proposals():
        result = server.refresh_goal_proposals()
        if result.get("error"):
            return result["error"]
        if result["proposed"]:
            log("%d new goal proposal(s) queued - review with "
                "/goal proposals (nothing runs until you adopt one)"
                % result["proposed"])
        return "proposed=%d skipped=%d" % (
            result["proposed"], result["skipped"])
    _stage(log, "goal-proposals", goal_proposals)

    _stage(log, "winml-vitisai-check", lambda: _winml_vitisai_check(log))

    log("=== nightly self-improvement done ===")
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        pass
    sink.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
