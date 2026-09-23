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

_WORKSPACE_CONFIG_FILES = (
    ("SONDER_EMOTION_VECTORS", "emotion_vectors.json"),
    ("SONDER_SYSTEM_PROFILE", "system_profile.md"),
)


def _bind_workspace_config_paths(root: Path | None = None) -> tuple[str, ...]:
    """Keep nightly mutable workspace files inside its own checkout."""
    workspace = (root or _REPO_ROOT).expanduser().resolve()

    def resolve_inside(candidate: Path) -> Path:
        resolved = candidate.resolve()
        resolved.relative_to(workspace)
        return resolved

    rebound: list[str] = []
    for variable, default_name in _WORKSPACE_CONFIG_FILES:
        raw = os.environ.get(variable, "").strip()
        candidate = Path(raw).expanduser() if raw else workspace / default_name
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            resolved = resolve_inside(candidate)
        except (OSError, RuntimeError, ValueError):
            try:
                resolved = resolve_inside(workspace / default_name)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("workspace default escapes checkout") from exc
            rebound.append(variable)
        os.environ[variable] = str(resolved)
    return tuple(rebound)


def _bind_ollama_pool_from_config(config_path: Path | None = None) -> tuple[str, ...]:
    """Export configured Ollama workers before ``server`` builds its pool."""
    workers_explicit = bool(os.environ.get("SONDER_OLLAMA_WORKERS", "").strip())
    from sonder_runtime.platform import config as sonder_config
    from sonder_runtime.platform import paths as runtime_paths

    if config_path is not None:
        configured = Path(config_path).expanduser()
    else:
        raw = os.environ.get("SONDER_CONFIG", "").strip()
        configured = (
            Path(raw).expanduser() if raw else runtime_paths.default_home() / "sonder.toml"
        )
    if not configured.is_absolute():
        configured = (_REPO_ROOT / configured).resolve()
    if not configured.is_file():
        return ()
    cfg = sonder_config.load_config(configured)
    bound: list[str] = []
    if cfg.ollama.workers and not workers_explicit:
        os.environ["SONDER_OLLAMA_WORKERS"] = ",".join(cfg.ollama.workers)
        bound.append("SONDER_OLLAMA_WORKERS")
    if not workers_explicit and "SONDER_ALLOW_REMOTE_OLLAMA" not in os.environ:
        os.environ["SONDER_ALLOW_REMOTE_OLLAMA"] = (
            "1" if cfg.ollama.allow_remote else "0"
        )
        bound.append("SONDER_ALLOW_REMOTE_OLLAMA")
    if not workers_explicit and cfg.ollama.trusted_origins and not os.environ.get("SONDER_TRUSTED_ORIGINS", "").strip():
        os.environ["SONDER_TRUSTED_ORIGINS"] = ",".join(cfg.ollama.trusted_origins)
        bound.append("SONDER_TRUSTED_ORIGINS")
    if cfg.ollama.ca_bundle and not os.environ.get("SONDER_OLLAMA_CA_BUNDLE", "").strip():
        os.environ["SONDER_OLLAMA_CA_BUNDLE"] = cfg.ollama.ca_bundle
        bound.append("SONDER_OLLAMA_CA_BUNDLE")
    return tuple(bound)


def _preflight(root: Path | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate and bind nightly checkout settings without opening runtime state."""
    rebound = _bind_workspace_config_paths(root or _REPO_ROOT)
    workers_bound = _bind_ollama_pool_from_config()
    return rebound, workers_bound


def _blocking_result(name, result) -> str | None:
    """Return a bounded failure reason for known fail-soft blocking results."""
    text = str(result or "").strip()
    if name == "selfmod" and text.startswith("working tree dirty ("):
        return "working tree dirty"
    return None


class _CodeModelUnavailable(RuntimeError):
    """The configured local code model failed its bounded readiness check."""


def _local_ollama_json(server, path: str, payload: dict | None, timeout: float):
    base = str(getattr(server, "BASE", "")).rstrip("/")
    endpoint = getattr(server, "ollama_endpoint", None)
    if not base or endpoint is None or not endpoint.is_loopback(base):
        raise _CodeModelUnavailable("local Ollama endpoint is not configured")
    from urllib.request import Request
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method="POST" if body else "GET",
    )
    with endpoint.open_url(request, timeout=timeout, allow_remote=False) as response:
        raw = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise _CodeModelUnavailable("local Ollama response exceeded 1 MiB")
    return json.loads(raw.decode("utf-8"))


def _prewarm_code_model(server, timeout_seconds: int = 60) -> str:
    """Wait for the configured local code model to become resident."""
    model = str(getattr(server, "TIERS", {}).get("code") or "").strip()
    if not model:
        raise _CodeModelUnavailable("code tier has no configured model")
    if getattr(server, "_is_cloud_model_name", lambda value: False)(model):
        raise _CodeModelUnavailable("code tier resolves to a cloud model")

    deadline = time.monotonic() + max(60, int(timeout_seconds or 60))
    try:
        def resident():
            state = _local_ollama_json(server, "/api/ps", None, 5)
            rows = state.get("models", []) if isinstance(state, dict) else []
            return any(
                str(row.get("name") or row.get("model") or "").strip() == model
                for row in rows if isinstance(row, dict)
            )

        if not resident():
            _local_ollama_json(
                server, "/api/generate",
                {"model": model, "prompt": "", "stream": False, "keep_alive": "2m"},
                max(60, int(timeout_seconds or 60)),
            )
            if resident():
                return "ready model=%s" % model
        while time.monotonic() < deadline:
            if resident():
                return "ready model=%s" % model
            time.sleep(1.0)
    except _CodeModelUnavailable:
        raise
    except Exception as exc:
        raise _CodeModelUnavailable("readiness probe failed: %s" % str(exc)[:160]) from exc
    raise _CodeModelUnavailable(
        "model did not become resident within %ds" % max(60, int(timeout_seconds or 60))
    )


def _run_campaign(server, args, log=None):
    """Run the campaign with one model request worker during nightly cold load."""
    out = server.campaign_generate_compile_execute_record(
        total=max(1, args.campaign_total), max_workers=1, repair_rounds=1,
        timeout=12, record_failures=True,
    )
    if log is not None:
        for line in str(out or "").splitlines()[1:8]:
            if line.startswith("first pitfall error:"):
                log("[campaign] " + line[:300])
                break
    return _first_line(out)


def _run_code_model_stages(server, args, log, failures) -> bool:
    """Prewarm once, then run model-bound campaign and repair stages."""
    prewarm = _stage(
        log, "code-model-prewarm",
        lambda: _prewarm_code_model(server), failures,
    )
    if prewarm is None:
        log("[campaign] SKIPPED: configured code model is not ready")
        log("[repo-repair] SKIPPED: configured code model is not ready")
        return False
    _stage(log, "campaign", lambda: _run_campaign(server, args, log), failures)

    def repair():
        out = server.campaign_repo_repair(
            total=max(1, args.repair_total), max_workers=2,
            repair_rounds=2, timeout=45,
        )
        return _first_line(out)
    _stage(log, "repo-repair", repair, failures)
    return True


def _stage(log, name, fn, failures=None):
    started = time.time()
    try:
        result = fn()
        blocking = _blocking_result(name, result)
        if blocking:
            if failures is not None:
                failures.append(name)
            log("[%s] FAILED after %.0fs: %s" % (
                name, time.time() - started, blocking))
            return result
        log("[%s] ok in %.0fs%s" % (
            name, time.time() - started,
            (": %s" % result) if isinstance(result, str) and result else "",
        ))
        return result
    except Exception as exc:
        if failures is not None:
            failures.append(name)
        log("[%s] FAILED after %.0fs: %s" % (
            name, time.time() - started, str(exc)[:300]))
        return None


def _first_line(text):
    return str(text or "").strip().splitlines()[0] if text else ""


def _pid_state(pid: int) -> str:
    """Inspect a lock owner without sending a signal on Windows."""
    if os.name == "nt":
        # Python's os.kill(pid, 0) calls TerminateProcess on Windows.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return "gone" if ctypes.get_last_error() == 87 else "unknown"
        try:
            status = kernel32.WaitForSingleObject(handle, 0)
        finally:
            kernel32.CloseHandle(handle)
        return "running" if status == 258 else "gone" if status == 0 else "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except OSError:
        return "unknown"
    return "running"


def _claim_lock(path, log) -> bool | None:
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
            try:
                owner_pid = int(path.read_text(encoding="utf-8").strip())
                if owner_pid > 0:
                    state = _pid_state(owner_pid)
                    if state == "unknown":
                        log("stale lock owner %s is not inspectable; refusing reclaim" % owner_pid)
                        return False
                    if state == "running":
                        log("stale lock owner %s is still alive; refusing reclaim" % owner_pid)
                        return False
            except (OSError, ValueError):
                pass
            log("reclaiming a stale lock (%.1fh old)" % (age / 3600))
            path.unlink(missing_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return True
    except FileExistsError:
        log("another nightly run claimed the lock first; exiting")
        return False
    except OSError as exc:
        log("lock unavailable (%s); refusing nightly run" % str(exc)[:80])
        return None


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


def _run_locked(args, log, sonder_paths):
    """Run the stages after the caller has acquired the nightly lock."""
    log("=== nightly self-improvement start ===")
    rebound, workers_bound = _preflight()
    if rebound:
        log("workspace config paths rehomed: %s" % ", ".join(rebound))
    if workers_bound:
        log("ollama pool env bound: %s" % ", ".join(workers_bound))
    import server
    import lesson_pruner
    import sonder_runtime.adapters.memory_store as memory_store

    critical_failures = []
    code_model_ready = True
    rounds = max(1, min(int(args.rounds or 1), 12))
    for round_index in range(rounds):
        if rounds > 1:
            log("--- round %d/%d ---" % (round_index + 1, rounds))

        if not args.skip_campaign:
            code_model_ready = _run_code_model_stages(
                server, args, log, critical_failures,
            )

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

    def selfmod_cycle():
        import nightly_selfmod
        return nightly_selfmod.run(server, log)
    if code_model_ready:
        _stage(log, "selfmod", selfmod_cycle, critical_failures)
    else:
        log("[selfmod] SKIPPED: configured code model is not ready")
    _stage(log, "winml-vitisai-check", lambda: _winml_vitisai_check(log))

    if critical_failures:
        log("critical stage failures: %s" % ", ".join(critical_failures))
    log("=== nightly self-improvement done ===")
    return 1 if critical_failures else 0


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--campaign-total", type=int, default=24)
    parser.add_argument("--repair-total", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=1,
                        help="repeat the exercise-and-groom cycle N times")
    parser.add_argument("--skip-campaign", action="store_true")
    parser.add_argument("--preflight", action="store_true",
                        help="validate checkout and provider binding without touching state")
    args = parser.parse_args()

    if args.preflight:
        try:
            rebound, workers_bound = _preflight()
        except Exception as exc:
            print("nightly preflight FAILED: %s" % str(exc)[:300])
            return 1
        print("nightly preflight ok%s%s" % (
            ("; workspace paths rehomed: " + ", ".join(rebound)) if rebound else "",
            ("; Ollama env bound: " + ", ".join(workers_bound)) if workers_bound else "",
        ))
        return 0

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
    claimed = _claim_lock(lock, log)
    if not claimed:
        sink.close()
        return 1 if claimed is None else 0

    try:
        result = _run_locked(args, log, sonder_paths)
    except Exception as exc:
        log("nightly run failed: %s" % str(exc)[:300])
        result = 1
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError as exc:
            log("nightly lock cleanup failed: %s" % str(exc)[:120])
        sink.close()
    return result

if __name__ == "__main__":
    raise SystemExit(main())
