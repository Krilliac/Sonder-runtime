"""Export proven, privacy-safe interactions as fine-tuning JSONL.

Usage: ./venv/Scripts/python.exe export_training_data.py [out_path]
Default out_path: training_data.jsonl (gitignored).
"""
import collections
import hashlib
import heapq
import json
import os
from pathlib import Path
import sys
import tempfile
import unicodedata

import contribute
import memory_store
import reward
import sonder_paths
import training_data

MAX_TRAINING_FILE_BYTES = training_data.MAX_TRAINING_FILE_BYTES
MAX_TRAINING_RECORD_BYTES = training_data.MAX_TRAINING_RECORD_BYTES
MAX_TRAINING_FIELD_CHARS = training_data.MAX_TRAINING_FIELD_CHARS
MAX_TRAINING_TOTAL_CHARS = training_data.MAX_TRAINING_TOTAL_CHARS
MAX_TRAINING_EXAMPLES = training_data.MAX_TRAINING_EXAMPLES
MAX_EXPORT_SOURCE_INTERACTIONS = 50_000
MAX_EXPORT_EVIDENCE_ROWS = 200_000
MAX_EXPORT_RETAINED_CHARS = MAX_TRAINING_TOTAL_CHARS * 2

EXPORT_SCHEMA = 1


def _canonical_prompt(text):
    value = unicodedata.normalize("NFKC", text or "")
    return " ".join(value.split()).casefold()


def _privacy_reasons(task, response):
    return sorted(set(
        contribute.private_reasons(task or "")
        + contribute.private_reasons(response or "")
    ))


def _select_examples(conn):
    """Return deterministic examples and non-sensitive selection statistics.

    Eligibility is fail closed: an interaction needs at least one grounded good
    signal, any negative/unknown signal vetoes it, and task/response text that
    trips the shared privacy classifier is omitted.  Duplicate prompts choose
    the strongest-proven response, then the newest positive evidence.
    """
    rejected = collections.Counter()
    winners = {}
    ranked = []
    retained_chars = 0
    interaction_count = 0
    evidence_count = 0
    eligible_count = 0

    def finalize_group(group):
        nonlocal eligible_count, retained_chars
        first = group["first"]
        task = (first.get("task") or "").strip()
        response = (first.get("response") or "").strip()
        if not task or not response:
            rejected["empty_content"] += 1
            return
        if (
            len(task) > MAX_TRAINING_FIELD_CHARS
            or len(response) > MAX_TRAINING_FIELD_CHARS
        ):
            rejected["field_too_large"] += 1
            return
        if group["best"] is None:
            rejected["no_good_outcome"] += 1
            return
        if group["contradictory"]:
            rejected["contradictory_outcome"] += 1
            return

        privacy = _privacy_reasons(task, response)
        if privacy:
            rejected["privacy"] += 1
            for reason in privacy:
                rejected["privacy.%s" % reason] += 1
            return

        best = group["best"]
        eligible_count += 1
        candidate = {
            "id": first["id"],
            "task": task,
            "response": response,
            "prompt_key": _canonical_prompt(task),
            "best_reward": reward.score(best.get("signal")),
            "evidence_rowid": int(best.get("outcome_rowid") or 0),
            "interaction_rowid": int(first.get("interaction_rowid") or 0),
            "chars": len(task) + len(response),
        }
        candidate["rank"] = (
            candidate["best_reward"],
            candidate["evidence_rowid"],
            candidate["interaction_rowid"],
            candidate["id"],
        )
        key = candidate["prompt_key"]
        current = winners.get(key)
        if current is not None:
            rejected["duplicate_prompt"] += 1
            if candidate["rank"] <= current["rank"]:
                return
            retained_chars -= current["chars"]
        winners[key] = candidate
        retained_chars += candidate["chars"]
        heapq.heappush(ranked, (candidate["rank"], key))

        while (
            len(winners) > MAX_TRAINING_EXAMPLES
            or retained_chars > MAX_EXPORT_RETAINED_CHARS
        ):
            while ranked:
                worst_rank, worst_key = heapq.heappop(ranked)
                worst = winners.get(worst_key)
                if worst is not None and worst["rank"] == worst_rank:
                    break
            else:
                raise training_data.TrainingDataError(
                    "training export selection heap became inconsistent"
                )
            retained_chars -= worst["chars"]
            del winners[worst_key]
            rejected["selection_capacity"] += 1

    current = None
    for row in memory_store.interaction_outcome_evidence(conn):
        evidence_count += 1
        if evidence_count > MAX_EXPORT_EVIDENCE_ROWS:
            raise training_data.TrainingDataError(
                "training export exceeds the supported outcome evidence limit"
            )
        if current is None or row["id"] != current["first"]["id"]:
            if current is not None:
                interaction_count += 1
                if interaction_count > MAX_EXPORT_SOURCE_INTERACTIONS:
                    raise training_data.TrainingDataError(
                        "training export exceeds the supported interaction limit"
                    )
                finalize_group(current)
            current = {"first": row, "best": None, "contradictory": False}
        signal = row.get("signal")
        if signal in reward.VALID_SIGNALS and reward.is_good(signal):
            if current["best"] is None or (
                reward.score(signal), int(row.get("outcome_rowid") or 0)
            ) > (
                reward.score(current["best"].get("signal")),
                int(current["best"].get("outcome_rowid") or 0),
            ):
                current["best"] = row
        else:
            current["contradictory"] = True
    if current is not None:
        interaction_count += 1
        if interaction_count > MAX_EXPORT_SOURCE_INTERACTIONS:
            raise training_data.TrainingDataError(
                "training export exceeds the supported interaction limit"
            )
        finalize_group(current)

    examples = []
    payload_bytes = 0
    payload_chars = 0
    accepted = []
    for candidate in sorted(
        winners.values(), key=lambda row: row["rank"], reverse=True,
    ):
        example = {"messages": [
            {"role": "user", "content": candidate["task"]},
            {"role": "assistant", "content": candidate["response"]},
        ]}
        try:
            example = training_data.canonical_record(example)
            encoded = (
                json.dumps(example, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        except (training_data.TrainingDataError, UnicodeError):
            rejected["invalid_record"] += 1
            continue
        if len(encoded) > MAX_TRAINING_RECORD_BYTES:
            rejected["record_too_large"] += 1
            continue
        if len(accepted) >= MAX_TRAINING_EXAMPLES:
            rejected["example_limit"] += 1
            continue
        if payload_bytes + len(encoded) > MAX_TRAINING_FILE_BYTES:
            rejected["file_size_limit"] += 1
            continue
        record_chars = sum(len(message["content"]) for message in example["messages"])
        if payload_chars + record_chars > MAX_TRAINING_TOTAL_CHARS:
            rejected["content_size_limit"] += 1
            continue
        accepted.append((candidate["prompt_key"], candidate["id"], example))
        payload_bytes += len(encoded)
        payload_chars += record_chars
    examples = [
        example for _prompt, _identifier, example in sorted(accepted)
    ]
    stats = {
        "interactions_with_outcomes": interaction_count,
        "outcome_evidence_rows": evidence_count,
        "eligible_before_deduplication": eligible_count,
        "accepted": len(examples),
        "rejected": interaction_count - len(examples),
        "rejected_by_reason": dict(sorted(rejected.items())),
    }
    return examples, stats


def build_examples(conn):
    examples, _stats = _select_examples(conn)
    return examples


def _atomic_write(path, payload):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", delete=False, dir=str(destination.parent),
            prefix=destination.name + ".tmp-",
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def main(out_path="training_data.jsonl", db_path=None, manifest_path=None):
    db_path = db_path or sonder_paths.memory_db_path()
    conn = memory_store.connect(db_path)
    try:
        examples, stats = _select_examples(conn)
    finally:
        conn.close()
    payload = training_data.encode_jsonl(examples)
    total_chars = sum(len(m["content"]) for ex in examples for m in ex["messages"])
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema": EXPORT_SCHEMA,
        "format": "sonder-chat-jsonl",
        **stats,
        "characters": total_chars,
        "sha256": digest,
        "privacy_policy": "exclude-shared-private-markers",
    }
    manifest_path = manifest_path or (str(out_path) + ".manifest.json")
    if Path(out_path).resolve() == Path(manifest_path).resolve():
        raise ValueError("training data and selection manifest paths must differ")
    # A stale manifest must never claim a newly replaced dataset. Invalidate it
    # before committing data; failures can leave no manifest, never a false one.
    try:
        Path(manifest_path).unlink()
    except FileNotFoundError:
        pass
    _atomic_write(out_path, payload)
    _atomic_write(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print("exported %d examples to %s (%d total chars, ~%d tokens rough)"
          % (len(examples), out_path, total_chars, total_chars // 4))
    print("selection manifest: %s (sha256 %s)" % (manifest_path, digest))
    return len(examples)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "training_data.jsonl")
