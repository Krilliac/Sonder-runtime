"""Export good-outcome interactions as fine-tuning JSONL (chat format).

Usage: ./venv/Scripts/python.exe export_training_data.py [out_path]
Default out_path: training_data.jsonl (gitignored).
"""
import json
import sys

import memory_store
import reward
import trilobite_paths


def build_examples(conn):
    good = {s for s in reward.VALID_SIGNALS if reward.is_good(s)}
    pairs = memory_store.interactions_with_good_outcome(conn, good)
    seen = set()
    examples = []
    for p in pairs:
        task = (p["task"] or "").strip()
        resp = (p["response"] or "").strip()
        if not task or not resp or task in seen:
            continue
        seen.add(task)
        examples.append({"messages": [
            {"role": "user", "content": task},
            {"role": "assistant", "content": resp},
        ]})
    return examples


def main(out_path="training_data.jsonl", db_path=None):
    db_path = db_path or trilobite_paths.memory_db_path()
    conn = memory_store.connect(db_path)
    try:
        examples = build_examples(conn)
    finally:
        conn.close()
    with open(out_path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    total_chars = sum(len(m["content"]) for ex in examples for m in ex["messages"])
    print("exported %d examples to %s (%d total chars, ~%d tokens rough)"
          % (len(examples), out_path, total_chars, total_chars // 4))
    return len(examples)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "training_data.jsonl")
