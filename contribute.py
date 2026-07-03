"""Export scrubbed, shareable lessons from memory.db to an outbox JSONL.

Contribution is strictly OPT-IN: nothing here uploads or opens a PR
automatically. It only writes a local file under contrib/ that YOU review,
then send home yourself (PR or file-server copy). Only distilled lesson
TEXT is considered for export — never raw interactions, code, or the model
itself. Lessons that look like they might leak private information (a
filesystem path, a secret-looking token, an email address) or that are not
a short generic sentence are excluded.

Run: ./venv/Scripts/python.exe contribute.py
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import memory_store  # noqa

MAX_LEN = 300

# Conservative patterns for text that may leak private info.
PRIVATE_MARKERS = [
    re.compile(r"[A-Za-z]:\\"),  # Windows drive path, e.g. C:\
    re.compile(r"(?<![\w/])/home/"),  # unix home dir
    re.compile(r"(?<![\w/])/Users/"),  # macOS home dir
    re.compile(r"\\\\[A-Za-z0-9._-]+\\"),  # UNC path \\server\share
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email address
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|access[_-]?key)\b\s*[:=]"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),  # long hex blob (hash/key)
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),  # long base64-ish blob
]


def is_shareable(text):
    """True only if `text` has no private markers and is a short generic sentence."""
    if not text:
        return False
    if len(text) > MAX_LEN:
        return False
    for pat in PRIVATE_MARKERS:
        if pat.search(text):
            return False
    return True


def scrubbed_lessons(conn):
    lessons = memory_store.all_lessons(conn)
    return [{"id": l["id"], "text": l["text"]} for l in lessons if is_shareable(l["text"])]


def main(out="contrib/lessons_contrib.jsonl", db=None):
    db = db or os.path.join(os.path.dirname(__file__), "memory.db")
    conn = memory_store.connect(db)
    try:
        lessons = scrubbed_lessons(conn)
    finally:
        conn.close()

    out_dir = os.path.dirname(out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        for l in sorted(lessons, key=lambda x: x["id"]):
            f.write(json.dumps({"id": l["id"], "text": l["text"]}, ensure_ascii=False) + "\n")

    print("wrote %d shareable lessons to %s" % (len(lessons), out))
    print("This is OPT-IN: nothing was sent anywhere. Review %s before sharing it." % out)
    print("To send it home base, either:")
    print("  1) open a PR adding this file under contrib/ on GitHub, or")
    print("  2) copy it to your file server / shared store.")


if __name__ == "__main__":
    main()
