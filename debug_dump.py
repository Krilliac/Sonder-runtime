"""Write human-readable, secret-redacted Sonder chat/debug dumps.

Debug dumps are deliberately developer-gated, but they can contain request
history and persisted conversation turns.  They therefore remain an export
boundary: a developer may need the structure of a transcript without causing
credentials copied into a prompt to become an indefinitely retained plain-text
file under ``SONDER_HOME/dumps``.
"""

from datetime import datetime
from pathlib import Path

from sonder_logging import Redactor


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


def write_dump(
    state_home, label="chat", messages=None, sections=None, events=None,
    *, redactor=None,
):
    """Write a developer-requested debug dump after mandatory secret redaction.

    ``redactor`` is injectable solely for focused tests.  Production callers
    use the same configured-value and textual-secret rules as structured logs.
    """
    redactor = redactor or Redactor()
    root = Path(state_home)
    dump_dir = root / "dumps"
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # The label is included in the filename as well as the file body. Derive
    # both projections from the same redacted value so a bearer token supplied
    # as ``/dump <label>`` cannot survive in directory metadata.
    safe_label = "".join(
        c if c.isalnum() or c in ("-", "_") else "-"
        for c in _safe(label, redactor)
    )
    path = dump_dir / ("sonder-%s-%s.txt" % (safe_label or "chat", stamp))
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
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return str(path)
