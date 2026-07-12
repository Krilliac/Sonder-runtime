"""curriculum_store — persist sonder's self-generated (and self-validated)
training tasks as JSONL, so the curriculum grows across runs instead of being
regenerated from scratch every time.
"""
import json
import os

import training_tasks

GEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_tasks.jsonl")


def load(path=GEN_FILE):
    """Return the list of stored generated task dicts, or [] if the file is absent."""
    if not os.path.exists(path):
        return []
    tasks = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            tasks.append(json.loads(line))
    return tasks


def append(tasks, path=GEN_FILE):
    """Append accepted task dicts to the store as JSONL."""
    if not tasks:
        return
    with open(path, "a", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")


def names(path=GEN_FILE):
    """Set of task names across both the stored generated tasks AND training_tasks.TASKS."""
    result = {t["name"] for t in training_tasks.TASKS}
    result.update(t["name"] for t in load(path))
    return result
