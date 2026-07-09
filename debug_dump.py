"""Write human-readable Trilobite chat/debug dumps."""

from datetime import datetime
from pathlib import Path


def _safe(value):
    return "" if value is None else str(value)


def _format_messages(messages):
    lines = []
    for index, msg in enumerate(messages or [], 1):
        if isinstance(msg, dict):
            role = _safe(msg.get("role"))
            content = _safe(msg.get("content"))
        else:
            role = _safe(getattr(msg, "role", ""))
            content = _safe(getattr(msg, "content", ""))
        lines.append("[%03d] %s" % (index, role or "unknown"))
        lines.append(content)
        lines.append("")
    if not lines:
        lines.append("(no messages supplied)")
        lines.append("")
    return "\n".join(lines).rstrip()


def write_dump(state_home, label="chat", messages=None, sections=None, events=None):
    root = Path(state_home)
    dump_dir = root / "dumps"
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_label = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in label)
    path = dump_dir / ("trilobite-%s-%s.txt" % (safe_label or "chat", stamp))
    parts = [
        "trilobite debug dump",
        "created: %s" % datetime.now().isoformat(timespec="seconds"),
        "label: %s" % label,
        "",
        "== messages ==",
        _format_messages(messages or []),
    ]
    for title, body in sections or []:
        parts.extend(["", "== %s ==" % title, _safe(body).rstrip()])
    if events:
        parts.extend(["", "== recent server events ==", _format_messages(events)])
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return str(path)
