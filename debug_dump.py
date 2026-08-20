"""Write human-readable, secret-redacted Sonder chat/debug dumps.

Debug dumps are deliberately developer-gated, but they can contain request
history and persisted conversation turns.  They therefore remain an export
boundary: a developer may need the structure of a transcript without causing
credentials copied into a prompt to become an indefinitely retained plain-text
file under ``SONDER_HOME/dumps``.
"""

from datetime import datetime
import os
from pathlib import Path
import secrets

from sonder_runtime.platform.logging import Redactor


def _safe(value, redactor):
    """Convert and redact one value before it reaches the durable dump."""
    if value is None:
        return ""
    return redactor.redact(str(value))


def _format_messages(messages, redactor):
    lines = []
    for index, msg in enumerate(messages or [], 1):
        if isinstance(msg, dict):
            role = _safe(msg.get("role"), redactor)
            content = _safe(msg.get("content"), redactor)
        else:
            role = _safe(getattr(msg, "role", ""), redactor)
            content = _safe(getattr(msg, "content", ""), redactor)
        lines.append("[%03d] %s" % (index, role or "unknown"))
        lines.append(content)
        lines.append("")
    if not lines:
        lines.append("(no messages supplied)")
        lines.append("")
    return "\n".join(lines).rstrip()


def _new_dump_path(dump_dir):
    """Reserve a server-generated dump name without replacing an old dump.

    Labels are useful inside a dump, but they are user-controlled data and must
    not become filesystem metadata.  A timestamp and random suffix are enough
    for operators to identify a dump while avoiding label disclosure and the
    former same-second overwrite behavior.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for _ in range(10):
        path = dump_dir / ("sonder-dump-%s-%s.txt" % (stamp, secrets.token_hex(8)))
        try:
            # The mode requests owner-only creation on POSIX. Windows ACLs are
            # governed by the runtime-home directory and are not inferred here.
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        return path, os.fdopen(descriptor, "w", encoding="utf-8")
    raise FileExistsError("could not reserve a unique debug dump filename")


def write_dump(
    state_home, label="chat", messages=None, sections=None, events=None,
    *, redactor=None,
):
    """Write a developer-requested debug dump after mandatory secret redaction.

    ``redactor`` is injectable solely for focused tests. Production callers use
    the same configured-value and textual-secret rules as structured logs.
    """
    redactor = redactor or Redactor()
    root = Path(state_home)
    dump_dir = root / "dumps"
    dump_dir.mkdir(parents=True, exist_ok=True)
    parts = [
        "sonder debug dump",
        "created: %s" % datetime.now().isoformat(timespec="seconds"),
        "label: %s" % _safe(label, redactor),
        "",
        "== messages ==",
        _format_messages(messages or [], redactor),
    ]
    for title, body in sections or []:
        parts.extend([
            "", "== %s ==" % _safe(title, redactor),
            _safe(body, redactor).rstrip(),
        ])
    if events:
        parts.extend([
            "", "== recent server events ==",
            _format_messages(events, redactor),
        ])
    path, handle = _new_dump_path(dump_dir)
    with handle:
        handle.write("\n".join(parts).rstrip() + "\n")
    return str(path)
