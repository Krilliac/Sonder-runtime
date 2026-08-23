"""curriculum_store — persist sonder's self-generated (and self-validated)
training tasks as JSONL, so the curriculum grows across runs instead of being
regenerated from scratch every time.

Durability contract: appends serialize across processes via an exclusive
sidecar lock (``<path>.lock``), are written as one buffer, and are fsynced
before the lock is released, so concurrent curriculum runs can no longer
interleave partial lines. ``load`` treats an unterminated final line as a
torn append (crash evidence, not data) and skips — with a logged count —
any interior line that fails to parse, so one corrupt record can no longer
make the entire curriculum unreadable.
"""
import json
import logging
import os

import durable_locks
import training_tasks

GEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_tasks.jsonl")

_log = logging.getLogger(__name__)


def load(path=GEN_FILE):
    """Return the list of stored generated task dicts, or [] if the file is absent."""
    path = str(path)
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        data = f.read()
    complete, _sep, tail = data.rpartition(b"\n")
    if tail.strip():
        _log.warning(
            "curriculum store %s: ignoring unterminated final line (torn append)",
            path,
        )
    tasks = []
    corrupt = 0
    lines = complete.split(b"\n") if complete else []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            task = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            corrupt += 1
            continue
        if not isinstance(task, dict):
            corrupt += 1
            continue
        tasks.append(task)
    if corrupt:
        # Loud, counted, and non-fatal: the store stays usable, but the loss
        # is never silent.
        _log.warning(
            "curriculum store %s: skipped %d corrupt line(s); %d task(s) remain loadable",
            path,
            corrupt,
            len(tasks),
        )
    return tasks


def append(tasks, path=GEN_FILE):
    """Append accepted task dicts to the store as JSONL. Crash- and race-safe."""
    if not tasks:
        return
    path = str(path)
    # Serialize the whole batch first: an unserializable task raises here,
    # before the store is opened, and can never leave a partial batch behind.
    payload = b"".join(json.dumps(t).encode("utf-8") + b"\n" for t in tasks)
    with durable_locks.exclusive_file_lock(path + ".lock"):
        if os.path.islink(path):
            raise OSError("refusing to append through a symbolic link: %s" % path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("curriculum store append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def names(path=GEN_FILE):
    """Set of task names across both the stored generated tasks AND training_tasks.TASKS."""
    result = {t["name"] for t in training_tasks.TASKS}
    result.update(t["name"] for t in load(path) if "name" in t)
    return result
