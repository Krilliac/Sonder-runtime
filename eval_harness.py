"""eval_harness — extensible, offline-first evaluation harness for Sonder Runtime.

The existing ``eval_*`` scripts (eval_models, eval_solver, eval_duel,
eval_retrieval) each hard-code one question about one live model and print a
score. This module is the shared machinery underneath that family:

  * a **scenario registry** — suites are JSON files in ``eval_scenarios/``
    (plus an adapter over ``training_tasks.TASKS``), each with a canonical
    ``suite_hash`` so a changed suite can never masquerade as the old one;
  * a **provider matrix** — the same suite runs against a deterministic
    replay cassette (offline, the default), or any local Ollama model
    (explicit ``--live`` opt-in; nothing here talks to the network otherwise);
  * **structured per-case outcomes** — pass / fail / error / timeout are
    never merged: an infrastructure failure (cassette miss, dead provider,
    harness timeout) cannot masquerade as a graded zero, following the
    outcome-class discipline of scripts/benchmark_schema_offload.py;
  * **replayable traces** — every case writes a JSONL trace holding the exact
    prompts, responses, and execution results, plus a
    ``sonder.evaluation-trajectory.v1`` record so two runs can be proved
    step-equivalent with the existing trajectory-replay comparator;
  * a **regression baseline** — a checked-in JSON ratchet
    (``eval_scenarios/eval_baseline.json``) in the style of
    scripts/error_signal_baseline.json: pass-rate floors and required-pass
    scenario pins that fail the run loudly when violated.

What this module does NOT do: it never promotes, demotes, or reconfigures
models (promotion stays with promotion_eval.promotion_decision); it does not
own persisted history (that is evaluation_history_store — ``--record-history``
delegates to it); and a green replay run proves the harness, scenarios, and
graders are healthy, not that any model is good. Grading executes model code
in a subprocess via grounding.run_code; that is failure isolation, not a
security sandbox — the same posture as the rest of the runtime.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

import grounding
import solver

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

SUITE_SCHEMA = "sonder.eval-harness.suite/v1"
CASSETTE_SCHEMA = "sonder.eval-harness.cassette/v1"
RUN_SCHEMA = "sonder.eval-harness.run/v1"
TRACE_SCHEMA = "sonder.eval-harness.trace/v1"
BASELINE_SCHEMA = "sonder.eval-harness.baseline/v1"
HARNESS_VERSION = 1

DEFAULT_SCENARIO_DIR = os.path.join(REPO_ROOT, "eval_scenarios")
DEFAULT_CASSETTE_DIR = os.path.join(DEFAULT_SCENARIO_DIR, "cassettes")
DEFAULT_BASELINE_PATH = os.path.join(DEFAULT_SCENARIO_DIR, "eval_baseline.json")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "eval_runs")

# The only scenario kind implemented today. The field exists so future kinds
# (tool-call scenarios, retrieval scenarios) extend the registry instead of
# forking it; validation rejects unknown kinds rather than half-running them.
SUPPORTED_KINDS = ("python_function",)

MAX_ATTEMPTS_LIMIT = 5
OUTPUT_EXCERPT_CHARS = 2000


class HarnessError(ValueError):
    """Invalid suite, cassette, baseline, or run input."""


class CassetteMiss(HarnessError):
    """A replay provider was asked for a response it has no recording for."""


# --- canonical serialization -------------------------------------------------

def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_of(value):
    return _sha256(_canonical(value))


def _atomic_write_json(path, payload):
    """Write JSON via a same-directory temp file + os.replace, never in place."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True,
                      ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- scenario registry -------------------------------------------------------

def _require_text(value, name, scenario_id=None):
    where = " in scenario %r" % scenario_id if scenario_id else ""
    if not isinstance(value, str) or not value.strip():
        raise HarnessError("%s must be a non-empty string%s" % (name, where))
    return value


def normalize_scenario(raw, source):
    """Validate one raw scenario dict into the canonical internal form."""
    if not isinstance(raw, dict):
        raise HarnessError("scenario entries must be objects (got %r)" % (raw,))
    scenario_id = _require_text(raw.get("id"), "id")
    kind = raw.get("kind", "python_function")
    if kind not in SUPPORTED_KINDS:
        raise HarnessError(
            "scenario %r has unsupported kind %r (supported: %s)"
            % (scenario_id, kind, ", ".join(SUPPORTED_KINDS)))
    timeout_s = raw.get("timeout_s", grounding.DEFAULT_TIMEOUT)
    if type(timeout_s) is not int or timeout_s < 1:
        raise HarnessError("scenario %r timeout_s must be a positive int"
                           % scenario_id)
    max_attempts = raw.get("max_attempts", 2)
    if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
        raise HarnessError("scenario %r max_attempts must be an int in 1..%d"
                           % (scenario_id, MAX_ATTEMPTS_LIMIT))
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise HarnessError("scenario %r tags must be a list of strings"
                           % scenario_id)
    return {
        "id": scenario_id,
        "kind": kind,
        "prompt": _require_text(raw.get("prompt"), "prompt", scenario_id),
        "check": _require_text(raw.get("check"), "check", scenario_id),
        "timeout_s": timeout_s,
        "max_attempts": max_attempts,
        "tags": sorted(tags),
        "source": source,
    }


def _builtin_scenarios(names):
    """Adapt entries of training_tasks.TASKS into scenarios (no copying)."""
    import training_tasks
    by_name = {t["name"]: t for t in training_tasks.TASKS}
    scenarios = []
    for name in names:
        task = by_name.get(name)
        if task is None:
            raise HarnessError("builtin task %r not found in training_tasks"
                               % name)
        scenarios.append(normalize_scenario(
            {"id": name, "prompt": task["prompt"], "check": task["check"],
             "tags": ["builtin"]},
            source="builtin:training_tasks"))
    return scenarios


def load_suite(path):
    """Load and validate one suite JSON file into its resolved form."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise HarnessError("cannot read suite file %s: %s" % (path, exc))
    except ValueError as exc:
        raise HarnessError("suite file %s is not valid JSON: %s" % (path, exc))
    if not isinstance(raw, dict) or raw.get("schema") != SUITE_SCHEMA:
        raise HarnessError("suite file %s must declare schema %r"
                           % (path, SUITE_SCHEMA))
    name = _require_text(raw.get("suite"), "suite")
    version = raw.get("version")
    if type(version) is not int or version < 1:
        raise HarnessError("suite %r version must be a positive int" % name)
    scenarios = [normalize_scenario(entry, source=os.path.basename(path))
                 for entry in raw.get("scenarios", [])]
    builtin = raw.get("builtin_tasks", [])
    if not isinstance(builtin, list):
        raise HarnessError("suite %r builtin_tasks must be a list" % name)
    scenarios.extend(_builtin_scenarios(builtin))
    if not scenarios:
        raise HarnessError("suite %r resolves to zero scenarios" % name)
    seen = set()
    for scenario in scenarios:
        if scenario["id"] in seen:
            raise HarnessError("suite %r has duplicate scenario id %r"
                               % (name, scenario["id"]))
        seen.add(scenario["id"])
    scenarios.sort(key=lambda s: s["id"])
    suite = {
        "suite": name,
        "version": version,
        "description": raw.get("description", ""),
        "scenarios": scenarios,
        "path": os.path.abspath(path),
    }
    suite["suite_hash"] = suite_hash(suite)
    return suite


def select_scenarios(suite, only=None, start=0, count=None):
    """Return a (possibly) narrowed copy of `suite` for chunked or focused runs.

    Follows eval_retrieval's [start] [count] chunk-resume convention so live
    runs fit bounded foreground slices. A narrowed suite recomputes its
    suite_hash over the subset and records the selection: a partial run can
    therefore never satisfy a full-suite baseline pin or blend into the full
    suite's history identity — it is honestly a different (smaller) suite.
    """
    scenarios = suite["scenarios"]
    if only:
        wanted = set(only)
        unknown = wanted - {scenario["id"] for scenario in scenarios}
        if unknown:
            raise HarnessError("unknown scenario id(s): %s"
                               % ", ".join(sorted(unknown)))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    if type(start) is not int or start < 0:
        raise HarnessError("start must be a non-negative int")
    if count is not None and (type(count) is not int or count < 1):
        raise HarnessError("count must be a positive int")
    scenarios = scenarios[start:start + count if count is not None else None]
    if not scenarios:
        raise HarnessError("selection matches zero scenarios")
    if len(scenarios) == len(suite["scenarios"]):
        return suite
    narrowed = dict(suite, scenarios=scenarios,
                    selection={"only": sorted(only) if only else None,
                               "start": start, "count": count})
    narrowed["suite_hash"] = suite_hash(narrowed)
    return narrowed


def suite_hash(suite):
    """Canonical digest over everything that defines what the suite measures."""
    return _digest_of({
        "schema": SUITE_SCHEMA,
        "suite": suite["suite"],
        "version": suite["version"],
        "scenarios": [
            {key: scenario[key]
             for key in ("id", "kind", "prompt", "check", "timeout_s",
                         "max_attempts")}
            for scenario in suite["scenarios"]
        ],
    })


def discover_suites(scenario_dir=DEFAULT_SCENARIO_DIR):
    """Return {suite_name: path} for every suite file in the registry dir."""
    suites = {}
    if not os.path.isdir(scenario_dir):
        return suites
    for entry in sorted(os.listdir(scenario_dir)):
        if not entry.endswith(".json") or entry == "eval_baseline.json":
            continue
        path = os.path.join(scenario_dir, entry)
        suite = load_suite(path)
        if suite["suite"] in suites:
            raise HarnessError("duplicate suite name %r (in %s and %s)"
                               % (suite["suite"], suites[suite["suite"]], path))
        suites[suite["suite"]] = path
    return suites


def resolve_suite(name_or_path, scenario_dir=DEFAULT_SCENARIO_DIR):
    if os.path.sep in name_or_path or name_or_path.endswith(".json"):
        return load_suite(name_or_path)
    suites = discover_suites(scenario_dir)
    if name_or_path not in suites:
        raise HarnessError("unknown suite %r (known: %s)"
                           % (name_or_path, ", ".join(sorted(suites)) or "none"))
    return load_suite(suites[name_or_path])


# --- providers ---------------------------------------------------------------

class ReplayProvider:
    """Deterministic offline provider serving recorded responses in order.

    Entries are keyed by (scenario_id, call index), NOT by prompt hash: repair
    prompts embed real tracebacks whose tempfile paths differ run to run, so
    prompt-hash keying would miss on every replay. The recorded prompt digest
    is kept as advisory metadata — a mismatch is counted as drift (the suite
    or solver templates changed since recording) without failing the case.
    """

    kind = "replay"
    deterministic = True

    def __init__(self, cassette, name="replay"):
        if isinstance(cassette, str):
            try:
                with open(cassette, "r", encoding="utf-8") as handle:
                    cassette = json.load(handle)
            except OSError as exc:
                raise HarnessError("cannot read cassette: %s" % exc)
            except ValueError as exc:
                raise HarnessError("cassette is not valid JSON: %s" % exc)
        if not isinstance(cassette, dict) or cassette.get("schema") != CASSETTE_SCHEMA:
            raise HarnessError("cassette must declare schema %r" % CASSETTE_SCHEMA)
        entries = cassette.get("entries")
        if not isinstance(entries, dict):
            raise HarnessError("cassette entries must be an object")
        self.name = name
        self._cassette = cassette
        self._entries = entries
        self._scenario_id = None
        self._cursor = 0
        self.drift = []  # [(scenario_id, call_index)] advisory prompt mismatches

    def digest(self):
        return _digest_of(self._cassette)

    def begin_case(self, scenario_id):
        self._scenario_id = scenario_id
        self._cursor = 0

    def generate(self, prompt, history=None):
        recorded = self._entries.get(self._scenario_id, [])
        index = self._cursor
        if index >= len(recorded):
            raise CassetteMiss(
                "no recording for scenario %r call %d (cassette has %d)"
                % (self._scenario_id, index + 1, len(recorded)))
        entry = recorded[index]
        self._cursor += 1
        expected_sha = entry.get("prompt_sha256")
        if expected_sha and expected_sha != _sha256(prompt):
            self.drift.append((self._scenario_id, index))
        return entry["response"]


class RecordingProvider:
    """Wrap a live provider and capture its responses into a cassette dict."""

    kind = "recording"
    deterministic = False

    def __init__(self, inner, suite_name):
        self.inner = inner
        self.name = inner.name
        self._scenario_id = None
        self.cassette = {
            "schema": CASSETTE_SCHEMA,
            "suite": suite_name,
            "recorded_from": inner.name,
            "entries": {},
        }

    def digest(self):
        return self.inner.digest()

    def begin_case(self, scenario_id):
        self._scenario_id = scenario_id
        if hasattr(self.inner, "begin_case"):
            self.inner.begin_case(scenario_id)

    def generate(self, prompt, history=None):
        response = self.inner.generate(prompt)
        self.cassette["entries"].setdefault(self._scenario_id, []).append(
            {"prompt_sha256": _sha256(prompt), "response": response})
        return response


class CallableProvider:
    """Adapt any prompt->text callable (tests, custom baselines)."""

    kind = "callable"

    def __init__(self, fn, name="callable", deterministic=True):
        self._fn = fn
        self.name = name
        self.deterministic = deterministic

    def digest(self):
        # A callable has no stable content identity; digest its name so the
        # value is well-formed but obviously not a model manifest digest.
        return _sha256("callable:" + self.name)

    def begin_case(self, scenario_id):
        del scenario_id

    def generate(self, prompt, history=None):
        return self._fn(prompt)


class OllamaProvider:
    """Live local-model provider. Constructed only under an explicit --live.

    Generation goes through server._make_generate, which fails closed to the
    configured local endpoint; temperature 0 keeps runs as repeatable as the
    backend allows, but this provider is still declared non-deterministic.
    """

    kind = "ollama"
    deterministic = False

    def __init__(self, model, temperature=0.0, num_predict=1024, num_ctx=4096):
        import server  # heavy; deferred so offline paths never pay for it
        self.name = "ollama:" + model
        self.model = model
        self._generate = server._make_generate(
            model, "", temperature, num_predict, num_ctx)

    def digest(self):
        import promotion_eval
        return promotion_eval.local_model_digest(self.model)

    def begin_case(self, scenario_id):
        del scenario_id

    def generate(self, prompt, history=None):
        return self._generate(prompt)


def parse_provider_spec(spec, suite, live=False, cassette_path=None):
    """Turn a CLI provider spec into a provider instance."""
    if spec == "replay":
        path = cassette_path or default_cassette_path(suite["suite"])
        return ReplayProvider(path)
    if spec.startswith("ollama:"):
        if not live:
            raise HarnessError(
                "provider %r requires --live (live model access is an "
                "explicit opt-in; the default run is offline replay)" % spec)
        return OllamaProvider(spec.split(":", 1)[1])
    raise HarnessError("unknown provider spec %r (use 'replay' or "
                       "'ollama:<model>')" % spec)


def default_cassette_path(suite_name, cassette_dir=DEFAULT_CASSETTE_DIR):
    return os.path.join(cassette_dir, suite_name + ".cassette.json")


# --- case runner -------------------------------------------------------------

def _classify_failure(result, misses, generate_errors):
    """Map a non-passing solver result onto a structured failure.

    Infrastructure problems (cassette miss, provider errors on every attempt,
    harness timeout) are distinct statuses from graded failures so an outage
    can never read as a model scoring zero.
    """
    transcript = result["transcript"]
    if misses:
        return "error", {"kind": "cassette_miss", "message": misses[0]}
    if transcript and len(generate_errors) >= len(transcript) and all(
            entry["code"] is None for entry in transcript):
        return "error", {"kind": "provider_error",
                         "message": generate_errors[0]}
    last = transcript[-1] if transcript else {"code": None, "output": ""}
    output = last.get("output") or ""
    if last.get("code") is None:
        kind = "no_code"
    elif "timed out after" in output:
        kind = "exec_timeout"
    elif "AssertionError" in output:
        kind = "assertion"
    else:
        kind = "execution"
    return "fail", {"kind": kind,
                    "message": output[:OUTPUT_EXCERPT_CHARS] or "(no output)"}


def run_case(scenario, provider, run_code_fn=None, case_timeout=None,
             clock=time.monotonic):
    """Run one scenario against one provider; never raises for case trouble.

    Returns {"scenario", "status", "passed", "attempts", "latency_s",
    "failure", "events", "trajectory"} where status is one of
    pass | fail | error | timeout and events is the full replayable trace.
    """
    events = []
    misses = []
    generate_errors = []
    attempt_counter = {"n": 0}
    run_code_fn = run_code_fn or grounding.run_code

    def generate_fn(prompt, history=None):
        attempt_counter["n"] += 1
        event = {
            "event": "generate",
            "attempt": attempt_counter["n"],
            "prompt": prompt,
            "prompt_sha256": _sha256(prompt),
            "response": None,
            "response_sha256": None,
            "error": None,
        }
        events.append(event)
        try:
            response = provider.generate(prompt)
        except CassetteMiss as exc:
            event["error"] = str(exc)
            misses.append(str(exc))
            raise
        except Exception as exc:  # recorded, then solver treats it as a failed attempt
            event["error"] = repr(exc)
            generate_errors.append(repr(exc))
            raise
        event["response"] = response
        event["response_sha256"] = _sha256(response)
        return response

    def grading_run(code, check):
        ok, output = run_code_fn(code, check, timeout=scenario["timeout_s"])
        events.append({
            "event": "exec",
            "attempt": attempt_counter["n"],
            "code_sha256": _sha256(code),
            "ok": bool(ok),
            "output": (output or "")[:OUTPUT_EXCERPT_CHARS],
        })
        return ok, output

    def execute():
        return solver.solve(scenario["prompt"], scenario["check"], generate_fn,
                            run_code_fn=grading_run,
                            max_attempts=scenario["max_attempts"])

    started = clock()
    if case_timeout is None:
        # Generous wall-clock ceiling: every attempt gets its exec timeout
        # plus headroom for generation. A hung provider is a "timeout" case,
        # never a silently absent one.
        case_timeout = scenario["max_attempts"] * (scenario["timeout_s"] + 30)
    provider.begin_case(scenario["id"])
    timed_out = False
    result = None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(execute)
        try:
            result = future.result(timeout=case_timeout)
        except concurrent.futures.TimeoutError:
            timed_out = True
            future.cancel()
    finally:
        executor.shutdown(wait=not timed_out)
    latency_s = round(clock() - started, 6)

    if timed_out:
        status = "timeout"
        failure = {"kind": "case_timeout",
                   "message": "case exceeded %ss wall clock" % case_timeout}
        attempts = attempt_counter["n"]
        passed = False
    elif result["passed"]:
        status, failure = "pass", None
        attempts = result["attempts"]
        passed = True
    else:
        status, failure = _classify_failure(result, misses, generate_errors)
        attempts = result["attempts"]
        passed = False

    events.append({
        "event": "outcome",
        "status": status,
        "attempts": attempts,
        "latency_s": latency_s,
        "failure": failure,
    })
    return {
        "scenario": scenario["id"],
        "status": status,
        "passed": passed,
        "attempts": attempts,
        "latency_s": latency_s,
        "failure": failure,
        "events": events,
        "trajectory": _trajectory_record(scenario["id"], events),
    }


def _trajectory_record(scenario_id, events):
    """Project trace events onto sonder.evaluation-trajectory.v1.

    Inputs/outputs are digests and booleans, not raw text: raw repair prompts
    embed tempfile paths that differ per run, which would make honest replays
    look divergent. Two runs of the same cassette and suite therefore yield
    identical trajectory digests iff generation and grading behaved the same.
    """
    from sonder_runtime.application.evaluation import trajectory_replay

    steps = []
    pending = None
    for event in events:
        if event["event"] == "generate":
            if pending is not None:
                steps.append(pending)
            pending = {
                "input": {"scenario": scenario_id,
                          "attempt": event["attempt"]},
                "output": {"response_sha256": event["response_sha256"],
                           "provider_error": bool(event["error"]),
                           "exec_ok": None},
            }
        elif event["event"] == "exec" and pending is not None:
            pending["output"]["exec_ok"] = event["ok"]
    if pending is not None:
        steps.append(pending)
    record = trajectory_replay.TrajectoryRecord.from_steps(
        "eval-harness:%s" % scenario_id,
        [trajectory_replay.TrajectoryStep(i, s["input"], s["output"])
         for i, s in enumerate(steps)],
    )
    return record.as_dict()


# --- suite runner ------------------------------------------------------------

def _git_rev():
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=REPO_ROOT, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _provider_digest(provider):
    try:
        return provider.digest()
    except Exception as exc:
        return "unavailable: %r" % (exc,)


def run_suite(suite, provider, out_dir=None, ts=None, case_timeout=None,
              run_code_fn=None):
    """Run every scenario in `suite` against `provider`.

    Returns the run summary. When out_dir is given, also writes
    results.jsonl, traces/<scenario>.jsonl, and summary.json under
    out_dir/<provider-safe-name>/.
    """
    ts = time.time() if ts is None else ts
    cases = []
    provider_dir = None
    if out_dir is not None:
        provider_dir = os.path.join(out_dir, _safe_name(provider.name))
        os.makedirs(os.path.join(provider_dir, "traces"), exist_ok=True)

    for scenario in suite["scenarios"]:
        case = run_case(scenario, provider, run_code_fn=run_code_fn,
                        case_timeout=case_timeout)
        cases.append(case)
        if provider_dir is not None:
            _write_trace(provider_dir, suite, provider, case, ts)

    totals = _totals(cases)
    totals["cassette_drift"] = len(getattr(provider, "drift", []))
    summary = {
        "schema": RUN_SCHEMA,
        "harness_version": HARNESS_VERSION,
        "suite": suite["suite"],
        "suite_version": suite["version"],
        "suite_hash": suite["suite_hash"],
        "selection": suite.get("selection"),
        "provider": {
            "name": provider.name,
            "kind": provider.kind,
            "digest": _provider_digest(provider),
            "deterministic": bool(provider.deterministic),
        },
        "ts": ts,
        "git_rev": _git_rev(),
        "totals": totals,
        "cases": [
            {"scenario": case["scenario"], "status": case["status"],
             "attempts": case["attempts"], "latency_s": case["latency_s"],
             "failure_kind": (case["failure"] or {}).get("kind"),
             "trajectory_digest": case["trajectory"]["trajectory_digest"]}
            for case in cases
        ],
    }
    summary["report_id"] = _digest_of(
        {key: value for key, value in summary.items() if key != "ts"})
    if provider_dir is not None:
        with open(os.path.join(provider_dir, "results.jsonl"), "w",
                  encoding="utf-8") as handle:
            for case in cases:
                row = {key: case[key] for key in
                       ("scenario", "status", "passed", "attempts",
                        "latency_s", "failure")}
                handle.write(_canonical(row) + "\n")
        _atomic_write_json(os.path.join(provider_dir, "summary.json"), summary)
    summary["_cases_full"] = cases  # in-memory only, for reports/replay checks
    return summary


def _safe_name(name):
    return "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in name)


def _write_trace(provider_dir, suite, provider, case, ts):
    path = os.path.join(provider_dir, "traces",
                        _safe_name(case["scenario"]) + ".jsonl")
    header = {
        "schema": TRACE_SCHEMA,
        "suite": suite["suite"],
        "suite_hash": suite["suite_hash"],
        "scenario": case["scenario"],
        "provider": provider.name,
        "ts": ts,
    }
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_canonical(header) + "\n")
        for event in case["events"]:
            handle.write(_canonical(event) + "\n")
        handle.write(_canonical({"event": "trajectory",
                                 "record": case["trajectory"]}) + "\n")


def _totals(cases):
    counts = {"cases": len(cases), "pass": 0, "fail": 0, "error": 0,
              "timeout": 0}
    for case in cases:
        counts[case["status"]] += 1
    graded = counts["pass"] + counts["fail"]
    counts["graded"] = graded
    # pass_rate is over GRADED cases only; infra trouble (error/timeout) is
    # reported separately and gated by forbid_infra, never averaged away.
    counts["pass_rate"] = (counts["pass"] / graded) if graded else None
    return counts


# --- regression baseline -----------------------------------------------------

def load_baseline(path=DEFAULT_BASELINE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            baseline = json.load(handle)
    except OSError as exc:
        raise HarnessError("cannot read baseline %s: %s" % (path, exc))
    except ValueError as exc:
        raise HarnessError("baseline %s is not valid JSON: %s" % (path, exc))
    if not isinstance(baseline, dict) or baseline.get("schema") != BASELINE_SCHEMA:
        raise HarnessError("baseline %s must declare schema %r"
                           % (path, BASELINE_SCHEMA))
    return baseline


def check_baseline(summary, baseline):
    """Compare one run summary against the checked-in expectations.

    Returns a list of violation strings; empty means the ratchet holds. A
    suite/provider pair absent from the baseline is itself a violation —
    an unbaselined suite silently passing is exactly the failure mode this
    ratchet exists to prevent.
    """
    violations = []
    expectations = (baseline.get("suites", {})
                    .get(summary["suite"], {})
                    .get(summary["provider"]["name"]))
    if expectations is None:
        return ["no baseline entry for suite %r provider %r — add one via "
                "'eval_harness.py baseline update'"
                % (summary["suite"], summary["provider"]["name"])]

    pinned_hash = expectations.get("suite_hash")
    if pinned_hash and pinned_hash != summary["suite_hash"]:
        violations.append(
            "suite_changed: suite %r hash %s does not match baselined %s "
            "(scenario content changed; re-baseline deliberately)"
            % (summary["suite"], summary["suite_hash"][:12], pinned_hash[:12]))

    totals = summary["totals"]
    if expectations.get("forbid_infra", True):
        if totals["error"] or totals["timeout"]:
            violations.append(
                "infra_failure: %d error, %d timeout cases (infrastructure "
                "trouble is never a graded result)"
                % (totals["error"], totals["timeout"]))

    min_pass_rate = expectations.get("min_pass_rate")
    if min_pass_rate is not None:
        if totals["pass_rate"] is None:
            violations.append("vacuous_run: zero graded cases, cannot meet "
                              "min_pass_rate %s" % min_pass_rate)
        elif totals["pass_rate"] < min_pass_rate:
            violations.append("pass_rate_regression: %.3f < required %.3f"
                              % (totals["pass_rate"], min_pass_rate))

    statuses = {case["scenario"]: case["status"] for case in summary["cases"]}
    for required in expectations.get("required_pass", []):
        status = statuses.get(required)
        if status != "pass":
            violations.append("required_pass: scenario %r is %s"
                              % (required, status or "missing from run"))
    return violations


def update_baseline(baseline, summary, pin_suite_hash=True):
    """Return a new baseline dict with this run's expectations recorded.

    Records the current pass set as required_pass and the current pass rate
    as the floor — an explicit ratchet update, mirroring eval_models'
    explicit --record-history posture (never automatic).
    """
    totals = summary["totals"]
    entry = {
        "min_pass_rate": totals["pass_rate"] if totals["pass_rate"] is not None else 0.0,
        "required_pass": sorted(case["scenario"] for case in summary["cases"]
                                if case["status"] == "pass"),
        "forbid_infra": True,
    }
    if pin_suite_hash:
        entry["suite_hash"] = summary["suite_hash"]
    updated = {
        "schema": BASELINE_SCHEMA,
        "suites": {name: dict(providers) for name, providers
                   in baseline.get("suites", {}).items()},
    }
    updated["suites"].setdefault(summary["suite"], {})
    updated["suites"][summary["suite"]] = dict(
        updated["suites"][summary["suite"]])
    updated["suites"][summary["suite"]][summary["provider"]["name"]] = entry
    return updated


# --- failure report ----------------------------------------------------------

def render_report(summaries, baseline_violations=None):
    """Render one Markdown failure report over a provider matrix of runs.

    Format follows the per-case sample layout of Inspect AI logs and
    Promptfoo/Braintrust failure tables: provenance header, outcome matrix,
    then one section per non-passing case with its failure class, the final
    grading output, and the trace path for replay.
    """
    lines = ["# Eval harness report", ""]
    first = summaries[0]
    lines += [
        "- suite: `%s` v%d (hash `%s`)" % (first["suite"],
                                           first["suite_version"],
                                           first["suite_hash"][:16]),
        "- git: `%s`" % (first["git_rev"] or "unknown"),
        "- schema: `%s`" % RUN_SCHEMA,
        "",
        "| provider | pass | fail | error | timeout | pass rate | drift |",
        "|---|---|---|---|---|---|---|",
    ]
    for summary in summaries:
        totals = summary["totals"]
        rate = ("%.1f%%" % (100 * totals["pass_rate"])
                if totals["pass_rate"] is not None else "n/a")
        lines.append("| `%s` | %d | %d | %d | %d | %s | %d |" % (
            summary["provider"]["name"], totals["pass"], totals["fail"],
            totals["error"], totals["timeout"], rate,
            totals.get("cassette_drift", 0)))
    lines.append("")

    if baseline_violations:
        lines += ["## Baseline violations", ""]
        lines += ["- %s" % violation for violation in baseline_violations]
        lines.append("")

    for summary in summaries:
        failing = [case for case in summary.get("_cases_full", [])
                   if case["status"] != "pass"]
        if not failing:
            continue
        lines += ["## Failures — `%s`" % summary["provider"]["name"], ""]
        for case in failing:
            failure = case["failure"] or {}
            lines += [
                "### %s — %s (%s)" % (case["scenario"], case["status"],
                                      failure.get("kind", "unknown")),
                "",
                "- attempts: %d, latency: %.2fs" % (case["attempts"],
                                                    case["latency_s"]),
                "- trace: `traces/%s.jsonl`" % _safe_name(case["scenario"]),
                "",
                "```",
                (failure.get("message") or "").strip()[:OUTPUT_EXCERPT_CHARS],
                "```",
                "",
            ]
    if len(lines) and lines[-1] != "":
        lines.append("")
    return "\n".join(lines)


def failures_json(summaries):
    """Machine-readable failure list (for CI annotations and dashboards)."""
    failures = []
    for summary in summaries:
        for case in summary.get("_cases_full", []):
            if case["status"] == "pass":
                continue
            failures.append({
                "suite": summary["suite"],
                "provider": summary["provider"]["name"],
                "scenario": case["scenario"],
                "status": case["status"],
                "failure": case["failure"],
                "attempts": case["attempts"],
                "trajectory_digest": case["trajectory"]["trajectory_digest"],
            })
    return {"schema": RUN_SCHEMA + "+failures", "failures": failures}


# --- replay-equivalence verification -----------------------------------------

def verify_replay(suite, cassette_path, run_code_fn=None):
    """Run the suite twice against the same cassette and compare trajectories.

    Proves the harness end to end: deterministic fixtures + deterministic
    grading must produce step-identical trajectory records. Divergence means
    nondeterminism has crept into scenarios, graders, or the runner itself.
    """
    from sonder_runtime.application.evaluation import trajectory_replay

    def one_run():
        provider = ReplayProvider(cassette_path)
        return run_suite(suite, provider, out_dir=None,
                         run_code_fn=run_code_fn)

    first, second = one_run(), one_run()
    divergences = []
    for case_a, case_b in zip(first["_cases_full"], second["_cases_full"]):
        record_a = _record_from_dict(trajectory_replay, case_a["trajectory"])
        record_b = _record_from_dict(trajectory_replay, case_b["trajectory"])
        report = trajectory_replay.compare_trajectories(record_a, record_b)
        if not report.equivalent:
            divergences.append({
                "scenario": case_a["scenario"],
                "divergences": [
                    {"index": item.index, "field": item.field}
                    for item in report.divergences
                ],
            })
    return {"equivalent": not divergences, "divergences": divergences,
            "cases": len(first["_cases_full"])}


def _record_from_dict(trajectory_replay, payload):
    return trajectory_replay.TrajectoryRecord.from_steps(
        payload["trajectory_id"],
        [trajectory_replay.TrajectoryStep(
            step["index"], step["input"], step["output"], step["state"])
         for step in payload["steps"]],
        metadata=payload.get("metadata") or None,
    )


# --- history integration -----------------------------------------------------

def record_history(summary, history_path=None):
    """Append this run's aggregate to the durable evaluation history.

    Delegates entirely to evaluation_history_store (locked, atomic,
    digest-verified). The identity's suite name is namespaced so harness
    records can never blend into promotion-eval history groups. Only graded
    cases are recorded; a run with zero graded cases is refused.
    """
    import sonder_runtime.adapters.evaluation_history_store as eval_history

    totals = summary["totals"]
    if not totals["graded"]:
        raise HarnessError("refusing to record a run with zero graded cases")
    digest = summary["provider"]["digest"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise HarnessError(
            "provider digest %r is not a 64-hex content digest; cannot "
            "record identity-bound history" % (digest,))
    return eval_history.record_result(
        history_path,
        model=summary["provider"]["name"],
        model_digest=digest,
        suite="eval-harness:" + summary["suite"],
        suite_version=str(summary["suite_version"]),
        suite_digest=summary["suite_hash"],
        passed=totals["pass"],
        total=totals["graded"],
        source="eval_harness",
    )


# --- CLI ---------------------------------------------------------------------

def _cmd_list(args):
    suites = discover_suites(args.scenario_dir)
    if not suites:
        print("no suites found in %s" % args.scenario_dir)
        return 0
    for name in sorted(suites):
        suite = load_suite(suites[name])
        print("%-24s v%-3d %2d scenarios  hash %s  %s"
              % (name, suite["version"], len(suite["scenarios"]),
                 suite["suite_hash"][:12], suite["description"]))
    return 0


def _cmd_run(args):
    suite = resolve_suite(args.suite, args.scenario_dir)
    suite = select_scenarios(suite, only=args.only, start=args.start,
                             count=args.count)
    ts = time.time()
    out_dir = args.out or os.path.join(
        DEFAULT_OUT_DIR, "%s-%d" % (suite["suite"], int(ts)))
    providers = args.provider or ["replay"]

    summaries = []
    for spec in providers:
        provider = parse_provider_spec(spec, suite, live=args.live,
                                       cassette_path=args.cassette)
        summaries.append(run_suite(suite, provider, out_dir=out_dir, ts=ts))

    violations = []
    if args.check_baseline:
        baseline = load_baseline(args.baseline)
        for summary in summaries:
            violations.extend(check_baseline(summary, baseline))

    report = render_report(summaries, violations)
    report_path = os.path.join(out_dir, "report.md")
    os.makedirs(out_dir, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(report)
    _atomic_write_json(os.path.join(out_dir, "failures.json"),
                       failures_json(summaries))

    history_error = None
    if args.record_history:
        for summary in summaries:
            try:
                record_history(summary, args.history_path)
            except Exception as exc:
                history_error = str(exc)

    for summary in summaries:
        totals = summary["totals"]
        rate = ("%.1f%%" % (100 * totals["pass_rate"])
                if totals["pass_rate"] is not None else "n/a")
        print("%s  %s: %d pass / %d fail / %d error / %d timeout  (%s)"
              % (summary["suite"], summary["provider"]["name"],
                 totals["pass"], totals["fail"], totals["error"],
                 totals["timeout"], rate))
    for violation in violations:
        print("BASELINE VIOLATION: %s" % violation)
    print("run dir: %s" % out_dir)
    if history_error:
        print("history error: %s" % history_error)
        return 2
    if violations:
        return 1
    if args.strict and any(
            summary["totals"]["cases"] != summary["totals"]["pass"]
            for summary in summaries):
        return 1
    return 0


def _cmd_record(args):
    suite = resolve_suite(args.suite, args.scenario_dir)
    if not args.live:
        raise HarnessError("record requires --live: recording a cassette "
                           "contacts the local model server")
    inner = OllamaProvider(args.model)
    provider = RecordingProvider(inner, suite["suite"])
    summary = run_suite(suite, provider, out_dir=None)
    cassette_path = args.cassette or default_cassette_path(suite["suite"])
    _atomic_write_json(cassette_path, provider.cassette)
    totals = summary["totals"]
    print("recorded %d scenarios (%d pass / %d fail) -> %s"
          % (totals["cases"], totals["pass"], totals["fail"], cassette_path))
    if totals["fail"] or totals["error"] or totals["timeout"]:
        print("note: cassette contains non-passing runs; replays will "
              "reproduce them exactly (that may be what you want)")
    return 0


def _cmd_verify_replay(args):
    suite = resolve_suite(args.suite, args.scenario_dir)
    cassette_path = args.cassette or default_cassette_path(suite["suite"])
    outcome = verify_replay(suite, cassette_path)
    if outcome["equivalent"]:
        print("replay equivalent: %d cases, trajectories identical"
              % outcome["cases"])
        return 0
    print("REPLAY DIVERGENCE in %d case(s):" % len(outcome["divergences"]))
    for item in outcome["divergences"]:
        print("  %s: %s" % (item["scenario"], item["divergences"]))
    return 1


def _cmd_baseline_update(args):
    summary_path = os.path.join(args.run, _safe_name(args.provider),
                                "summary.json")
    try:
        with open(summary_path, "r", encoding="utf-8") as handle:
            summary = json.load(handle)
    except OSError as exc:
        raise HarnessError("cannot read %s: %s" % (summary_path, exc))
    try:
        baseline = load_baseline(args.baseline)
    except HarnessError:
        baseline = {"schema": BASELINE_SCHEMA, "suites": {}}
    updated = update_baseline(baseline, summary)
    _atomic_write_json(args.baseline, updated)
    print("baseline updated for suite %r provider %r -> %s"
          % (summary["suite"], summary["provider"]["name"], args.baseline))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="eval_harness",
        description="Offline-first scenario evaluation harness (see module "
                    "docstring; live model access always requires --live).")
    parser.add_argument("--scenario-dir", default=DEFAULT_SCENARIO_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list registered suites")

    run_parser = sub.add_parser("run", help="run a suite over providers")
    run_parser.add_argument("--suite", required=True)
    run_parser.add_argument("--provider", action="append",
                            help="repeatable: replay | ollama:<model>")
    run_parser.add_argument("--cassette", default=None)
    run_parser.add_argument("--only", action="append", metavar="SCENARIO_ID",
                            help="repeatable: run only these scenarios")
    run_parser.add_argument("--start", type=int, default=0,
                            help="chunk-resume offset (eval_retrieval style)")
    run_parser.add_argument("--count", type=int, default=None,
                            help="chunk size from --start")
    run_parser.add_argument("--out", default=None)
    run_parser.add_argument("--live", action="store_true",
                            help="allow providers that contact the local "
                                 "model server")
    run_parser.add_argument("--check-baseline", action="store_true")
    run_parser.add_argument("--baseline", default=DEFAULT_BASELINE_PATH)
    run_parser.add_argument("--strict", action="store_true",
                            help="exit 1 unless every case passed")
    run_parser.add_argument("--record-history", action="store_true",
                            help="append aggregates to the durable "
                                 "evaluation history")
    run_parser.add_argument("--history-path", default=None)

    record_parser = sub.add_parser(
        "record", help="record a cassette from a live model")
    record_parser.add_argument("--suite", required=True)
    record_parser.add_argument("--model", required=True)
    record_parser.add_argument("--cassette", default=None)
    record_parser.add_argument("--live", action="store_true")

    verify_parser = sub.add_parser(
        "verify-replay", help="prove two replay runs are step-equivalent")
    verify_parser.add_argument("--suite", required=True)
    verify_parser.add_argument("--cassette", default=None)

    baseline_parser = sub.add_parser("baseline", help="baseline maintenance")
    baseline_sub = baseline_parser.add_subparsers(dest="baseline_command",
                                                  required=True)
    update_parser = baseline_sub.add_parser(
        "update", help="record a run's outcomes as the new expectations")
    update_parser.add_argument("--run", required=True,
                               help="run directory written by 'run'")
    update_parser.add_argument("--provider", default="replay")
    update_parser.add_argument("--baseline", default=DEFAULT_BASELINE_PATH)

    args = parser.parse_args(argv)
    commands = {
        "list": _cmd_list,
        "run": _cmd_run,
        "record": _cmd_record,
        "verify-replay": _cmd_verify_replay,
        "baseline": _cmd_baseline_update,
    }
    try:
        return commands[args.command](args)
    except HarnessError as exc:
        print("eval_harness error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
