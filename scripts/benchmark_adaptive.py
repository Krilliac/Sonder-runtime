"""Deterministic, model-free adaptive-improvement evidence comparator.

This tool does not run a model.  It records already-observed task outcomes for
two checkpoints and compares a fresh history with an accumulated grounded
history only when model, suite, and hardware identities match exactly.

Task input is a bounded JSON array::

    [{"name":"task-a","completed":true,"retries":0,
      "tokens_in":120,"tokens_out":40}]

Create and compare records::

    python scripts/benchmark_adaptive.py record ... --tasks fresh-tasks.json \
      --checkpoint fresh --grounded-records 0 --output fresh.json
    python scripts/benchmark_adaptive.py compare --fresh fresh.json \
      --accumulated accumulated.json --markdown report.md --json report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eval_history


RECORD_SCHEMA = "sonder.adaptive-checkpoint.v1"
REPORT_SCHEMA = "sonder.adaptive-comparison.v1"
MAX_JSON_BYTES = 512 * 1024
MAX_TASKS = 256
MAX_COUNTER = (1 << 63) - 1
_EMPTY_HISTORY_DIGEST = hashlib.sha256(b"").hexdigest()


class AdaptiveBenchmarkError(ValueError):
    """Invalid, unbounded, incomparable, or tampered benchmark evidence."""


def _text(value, label, limit):
    if not isinstance(value, str):
        raise AdaptiveBenchmarkError("%s must be text" % label)
    text = value.strip()
    if not text or len(text) > limit or any(c in text for c in "\r\n\x00"):
        raise AdaptiveBenchmarkError(
            "%s is required and must not exceed %d characters" % (label, limit)
        )
    return text


def _digest(value, label):
    if not isinstance(value, str):
        raise AdaptiveBenchmarkError("%s must be a 64-character SHA-256 digest" % label)
    try:
        return eval_history.make_identity(
            "digest-check", value, "digest-check", "1", value,
        )[0]["model_digest"]
    except eval_history.HistoryError as exc:
        raise AdaptiveBenchmarkError("%s must be a 64-character SHA-256 digest" % label) from exc


def _counter(value, label):
    if type(value) is not int or value < 0 or value > MAX_COUNTER:
        raise AdaptiveBenchmarkError(
            "%s must be a non-negative integer no greater than %d"
            % (label, MAX_COUNTER)
        )
    return value


def _canonical_digest(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_unknown_keys(value, allowed, label):
    unknown = sorted(repr(key) for key in value if key not in allowed)
    if unknown:
        raise AdaptiveBenchmarkError(
            "%s contains unsupported fields: %s" % (label, ", ".join(unknown))
        )


def _normalize_tasks(tasks):
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
        raise AdaptiveBenchmarkError(
            "tasks must be a non-empty array with at most %d entries" % MAX_TASKS
        )
    normalized = []
    seen = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise AdaptiveBenchmarkError("task %d must be an object" % index)
        _reject_unknown_keys(
            task,
            {"name", "completed", "retries", "tokens_in", "tokens_out"},
            "task %d" % index,
        )
        name = _text(task.get("name"), "task name", 128)
        if name in seen:
            raise AdaptiveBenchmarkError("duplicate task name: %s" % name)
        seen.add(name)
        completed = task.get("completed")
        if type(completed) is not bool:
            raise AdaptiveBenchmarkError("task completed must be a boolean")
        normalized.append({
            "name": name,
            "completed": completed,
            "retries": _counter(task.get("retries"), "task retries"),
            "tokens_in": _counter(task.get("tokens_in"), "task tokens_in"),
            "tokens_out": _counter(task.get("tokens_out"), "task tokens_out"),
        })
    return sorted(normalized, key=lambda item: item["name"])


def _summary(tasks):
    completed = sum(1 for task in tasks if task["completed"])
    tokens_in = sum(task["tokens_in"] for task in tasks)
    tokens_out = sum(task["tokens_out"] for task in tasks)
    retries = sum(task["retries"] for task in tasks)
    tokens_total = tokens_in + tokens_out
    if any(
        value > MAX_COUNTER
        for value in (tokens_in, tokens_out, tokens_total, retries)
    ):
        raise AdaptiveBenchmarkError("aggregate counters exceed the supported ceiling")
    return {
        "completed": completed,
        "total": len(tasks),
        "completion_rate": completed / len(tasks),
        "retries": retries,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_total,
    }


def make_record(
    *, model, model_digest, suite, suite_version, suite_digest,
    hardware, hardware_digest, checkpoint, checkpoint_label,
    grounded_records, grounded_history_digest, tasks, source="manual",
):
    model = _text(model, "model", 256)
    model_digest = _digest(model_digest, "model_digest")
    suite = _text(suite, "suite", 128)
    suite_version = _text(suite_version, "suite_version", 128)
    suite_digest = _digest(suite_digest, "suite_digest")
    try:
        base_identity, _ = eval_history.make_identity(
            model, model_digest, suite, suite_version, suite_digest,
        )
    except eval_history.HistoryError as exc:
        raise AdaptiveBenchmarkError(str(exc)) from exc
    identity = {
        **base_identity,
        "hardware": _text(hardware, "hardware", 256),
        "hardware_digest": _digest(hardware_digest, "hardware_digest"),
    }
    identity_key = _canonical_digest(identity)
    checkpoint = _text(checkpoint, "checkpoint", 32)
    if checkpoint not in {"fresh", "accumulated"}:
        raise AdaptiveBenchmarkError("checkpoint must be fresh or accumulated")
    grounded_records = _counter(grounded_records, "grounded_records")
    if checkpoint == "fresh" and grounded_records != 0:
        raise AdaptiveBenchmarkError("fresh checkpoint must have zero grounded records")
    if checkpoint == "accumulated" and grounded_records == 0:
        raise AdaptiveBenchmarkError("accumulated checkpoint must have grounded records")
    history_digest = _digest(grounded_history_digest, "grounded_history_digest")
    if checkpoint == "fresh" and history_digest != _EMPTY_HISTORY_DIGEST:
        raise AdaptiveBenchmarkError(
            "fresh checkpoint history digest must be SHA-256 of empty history"
        )
    normalized_tasks = _normalize_tasks(tasks)
    record = {
        "schema": RECORD_SCHEMA,
        "identity": identity,
        "identity_key": identity_key,
        "checkpoint": {
            "kind": checkpoint,
            "label": _text(checkpoint_label, "checkpoint_label", 128),
            "grounded_records": grounded_records,
            "grounded_history_digest": history_digest,
        },
        "tasks": normalized_tasks,
        "summary": _summary(normalized_tasks),
        "source": _text(source, "source", 128),
    }
    record["record_id"] = _canonical_digest(record)
    encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise AdaptiveBenchmarkError("checkpoint record exceeds byte ceiling")
    return record


def validate_record(value):
    if not isinstance(value, dict) or value.get("schema") != RECORD_SCHEMA:
        raise AdaptiveBenchmarkError("unsupported or missing checkpoint schema")
    _reject_unknown_keys(
        value,
        {
            "schema", "identity", "identity_key", "checkpoint", "tasks",
            "summary", "source", "record_id",
        },
        "checkpoint record",
    )
    identity = value.get("identity")
    checkpoint = value.get("checkpoint")
    if not isinstance(identity, dict) or not isinstance(checkpoint, dict):
        raise AdaptiveBenchmarkError("record identity and checkpoint must be objects")
    _reject_unknown_keys(
        identity,
        {
            "model", "model_digest", "suite", "suite_version", "suite_digest",
            "hardware", "hardware_digest",
        },
        "record identity",
    )
    _reject_unknown_keys(
        checkpoint,
        {"kind", "label", "grounded_records", "grounded_history_digest"},
        "record checkpoint",
    )
    normalized = make_record(
        model=identity.get("model"),
        model_digest=identity.get("model_digest"),
        suite=identity.get("suite"),
        suite_version=identity.get("suite_version"),
        suite_digest=identity.get("suite_digest"),
        hardware=identity.get("hardware"),
        hardware_digest=identity.get("hardware_digest"),
        checkpoint=checkpoint.get("kind"),
        checkpoint_label=checkpoint.get("label"),
        grounded_records=checkpoint.get("grounded_records"),
        grounded_history_digest=checkpoint.get("grounded_history_digest"),
        tasks=value.get("tasks"),
        source=value.get("source"),
    )
    if value.get("identity_key") != normalized["identity_key"]:
        raise AdaptiveBenchmarkError("identity key does not match identity fields")
    if value.get("summary") != normalized["summary"]:
        raise AdaptiveBenchmarkError("summary does not match task evidence")
    if value.get("record_id") != normalized["record_id"]:
        raise AdaptiveBenchmarkError("record id does not match record contents")
    return normalized


def load_record(path):
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AdaptiveBenchmarkError("cannot inspect checkpoint JSON: %s" % exc) from exc
    if size <= 0 or size > MAX_JSON_BYTES:
        raise AdaptiveBenchmarkError("checkpoint JSON is empty or exceeds byte ceiling")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise AdaptiveBenchmarkError("cannot read checkpoint JSON: %s" % exc) from exc
    return validate_record(value)


def _direction(delta, higher_is_better):
    if delta == 0:
        return "unchanged"
    improved = delta > 0 if higher_is_better else delta < 0
    return "improved" if improved else "regressed"


def compare_records(fresh, accumulated):
    fresh = validate_record(fresh)
    accumulated = validate_record(accumulated)
    if fresh["checkpoint"]["kind"] != "fresh":
        raise AdaptiveBenchmarkError("fresh input is not a fresh checkpoint")
    if accumulated["checkpoint"]["kind"] != "accumulated":
        raise AdaptiveBenchmarkError("accumulated input is not an accumulated checkpoint")
    if fresh["identity_key"] != accumulated["identity_key"]:
        raise AdaptiveBenchmarkError(
            "model, suite, and hardware identities must match exactly"
        )
    fresh_tasks = {task["name"]: task for task in fresh["tasks"]}
    accumulated_tasks = {task["name"]: task for task in accumulated["tasks"]}
    if set(fresh_tasks) != set(accumulated_tasks):
        raise AdaptiveBenchmarkError("checkpoint task sets must match exactly")
    per_task = []
    improved_tasks = []
    regressed_tasks = []
    retry_regressed_tasks = []
    token_regressed_tasks = []
    for name in sorted(fresh_tasks):
        before = fresh_tasks[name]
        after = accumulated_tasks[name]
        if not before["completed"] and after["completed"]:
            completion_change = "improved"
            improved_tasks.append(name)
        elif before["completed"] and not after["completed"]:
            completion_change = "regressed"
            regressed_tasks.append(name)
        else:
            completion_change = "unchanged"
        retries_delta = after["retries"] - before["retries"]
        tokens_total_delta = (
            after["tokens_in"] + after["tokens_out"]
            - before["tokens_in"] - before["tokens_out"]
        )
        if retries_delta > 0:
            retry_regressed_tasks.append(name)
        if tokens_total_delta > 0:
            token_regressed_tasks.append(name)
        per_task.append({
            "name": name,
            "fresh_completed": before["completed"],
            "accumulated_completed": after["completed"],
            "completion_change": completion_change,
            "retries_delta": retries_delta,
            "tokens_in_delta": after["tokens_in"] - before["tokens_in"],
            "tokens_out_delta": after["tokens_out"] - before["tokens_out"],
            "tokens_total_delta": tokens_total_delta,
        })
    before = fresh["summary"]
    after = accumulated["summary"]
    deltas = {
        "completed": after["completed"] - before["completed"],
        "completion_rate": after["completion_rate"] - before["completion_rate"],
        "retries": after["retries"] - before["retries"],
        "tokens_in": after["tokens_in"] - before["tokens_in"],
        "tokens_out": after["tokens_out"] - before["tokens_out"],
        "tokens_total": after["tokens_total"] - before["tokens_total"],
    }
    if improved_tasks and regressed_tasks:
        completion_assessment = "mixed"
    elif regressed_tasks:
        completion_assessment = "regressed"
    elif improved_tasks:
        completion_assessment = "improved"
    else:
        completion_assessment = "unchanged"
    report = {
        "schema": REPORT_SCHEMA,
        "model_free": True,
        "causal_claim": False,
        "identity": fresh["identity"],
        "identity_key": fresh["identity_key"],
        "fresh": {
            "record_id": fresh["record_id"], "source": fresh["source"],
            "checkpoint": fresh["checkpoint"], "summary": before,
        },
        "accumulated": {
            "record_id": accumulated["record_id"],
            "source": accumulated["source"],
            "checkpoint": accumulated["checkpoint"], "summary": after,
        },
        "deltas": deltas,
        "assessment": {
            "completion": completion_assessment,
            "retries": _direction(deltas["retries"], False),
            "tokens": _direction(deltas["tokens_total"], False),
            "any_completion_regression": bool(regressed_tasks),
            "improved_tasks": improved_tasks,
            "regressed_tasks": regressed_tasks,
            "retry_regressed_tasks": retry_regressed_tasks,
            "token_regressed_tasks": token_regressed_tasks,
        },
        "tasks": per_task,
    }
    report["report_id"] = _canonical_digest(report)
    return report


def render_report(report):
    report = dict(report)
    identity = report["identity"]
    fresh = report["fresh"]["summary"]
    accumulated = report["accumulated"]["summary"]
    delta = report["deltas"]
    assessment = report["assessment"]
    lines = [
        "# Adaptive improvement checkpoint comparison",
        "",
        "Exact identity: model `%s` (`%s...`), suite `%s` v%s (`%s...`), hardware `%s` (`%s...`)."
        % (
            identity["model"], identity["model_digest"][:12], identity["suite"],
            identity["suite_version"], identity["suite_digest"][:12],
            identity["hardware"], identity["hardware_digest"][:12],
        ),
        "",
        "This is a deterministic, model-free comparison of supplied evidence. "
        "It does not run a model or by itself prove causation.",
        "",
        "| Checkpoint | Completed | Retries | Tokens in | Tokens out | Tokens total |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        "| Fresh | %d/%d | %d | %d | %d | %d |" % (
            fresh["completed"], fresh["total"], fresh["retries"],
            fresh["tokens_in"], fresh["tokens_out"], fresh["tokens_total"],
        ),
        "| Accumulated | %d/%d | %d | %d | %d | %d |" % (
            accumulated["completed"], accumulated["total"], accumulated["retries"],
            accumulated["tokens_in"], accumulated["tokens_out"], accumulated["tokens_total"],
        ),
        "| Delta | %+d | %+d | %+d | %+d | %+d |" % (
            delta["completed"], delta["retries"], delta["tokens_in"],
            delta["tokens_out"], delta["tokens_total"],
        ),
        "",
        "Completion: **%s**. Retries: **%s**. Tokens: **%s**."
        % (assessment["completion"], assessment["retries"], assessment["tokens"]),
        "",
        "Improved tasks: %s" % (", ".join(assessment["improved_tasks"]) or "none"),
        "",
        "Regressed tasks: %s" % (", ".join(assessment["regressed_tasks"]) or "none"),
        "",
        "Tasks with more retries: %s"
        % (", ".join(assessment["retry_regressed_tasks"]) or "none"),
        "",
        "Tasks with more tokens: %s"
        % (", ".join(assessment["token_regressed_tasks"]) or "none"),
        "",
    ]
    return "\n".join(lines)


def _read_tasks(path):
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AdaptiveBenchmarkError("cannot inspect task JSON: %s" % exc) from exc
    if size <= 0 or size > MAX_JSON_BYTES:
        raise AdaptiveBenchmarkError("task JSON is empty or exceeds byte ceiling")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise AdaptiveBenchmarkError("cannot read task JSON: %s" % exc) from exc


def _atomic_write(path, encoded):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(encoded) > MAX_JSON_BYTES:
        raise AdaptiveBenchmarkError("output exceeds byte ceiling")
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_json(path, value):
    encoded = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    _atomic_write(path, encoded)


def _write_text(path, value):
    _atomic_write(path, (value + "\n").encode("utf-8"))


def _path_key(path):
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _reject_path_collisions(named_paths):
    seen = {}
    for label, path in named_paths:
        if not path:
            continue
        key = _path_key(path)
        if key in seen:
            raise AdaptiveBenchmarkError(
                "%s path must differ from %s path" % (label, seen[key])
            )
        seen[key] = label


def _add_identity_arguments(parser):
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-digest", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--suite-version", required=True)
    parser.add_argument("--suite-digest", required=True)
    parser.add_argument("--hardware", required=True)
    parser.add_argument("--hardware-digest", required=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="create one bounded checkpoint record")
    _add_identity_arguments(record)
    record.add_argument("--checkpoint", choices=("fresh", "accumulated"), required=True)
    record.add_argument("--checkpoint-label", required=True)
    record.add_argument("--grounded-records", type=int, required=True)
    record.add_argument("--grounded-history-digest", required=True)
    record.add_argument("--tasks", required=True, help="bounded task-result JSON array")
    record.add_argument("--source", default="manual")
    record.add_argument("--output", required=True)

    compare = commands.add_parser("compare", help="compare exact-identity checkpoints")
    compare.add_argument("--fresh", required=True)
    compare.add_argument("--accumulated", required=True)
    compare.add_argument("--json", default="")
    compare.add_argument("--markdown", default="")
    compare.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            _reject_path_collisions([
                ("tasks", args.tasks), ("output", args.output),
            ])
            result = make_record(
                model=args.model, model_digest=args.model_digest,
                suite=args.suite, suite_version=args.suite_version,
                suite_digest=args.suite_digest, hardware=args.hardware,
                hardware_digest=args.hardware_digest, checkpoint=args.checkpoint,
                checkpoint_label=args.checkpoint_label,
                grounded_records=args.grounded_records,
                grounded_history_digest=args.grounded_history_digest,
                tasks=_read_tasks(args.tasks), source=args.source,
            )
            _write_json(args.output, result)
            print(json.dumps(result, sort_keys=True))
            return 0
        _reject_path_collisions([
            ("fresh", args.fresh), ("accumulated", args.accumulated),
            ("json", args.json), ("markdown", args.markdown),
        ])
        result = compare_records(load_record(args.fresh), load_record(args.accumulated))
        report = render_report(result)
        if args.json:
            _write_json(args.json, result)
        if args.markdown:
            _write_text(args.markdown, report)
        print(json.dumps(result, sort_keys=True) if args.json_stdout else report)
        return 0
    except (AdaptiveBenchmarkError, OSError) as exc:
        print("adaptive benchmark error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
