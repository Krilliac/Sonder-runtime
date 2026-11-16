"""Synthetic fixtures for the security_eval tests: a tiny repo and a small labelled set.

Nothing here is text from a real review; every name and finding is invented.
"""
from __future__ import annotations

import json
from pathlib import Path

CS_SOURCE = """namespace Demo
{
    public class PacketReader
    {
        public int Limit = 64;

        public byte[] ReadBlock(byte[] data, int length)
        {
            if (length > Limit)
            {
                throw new System.ArgumentException("too long");
            }
            var buffer = new byte[length];
            System.Array.Copy(data, buffer, length);
            return buffer;
        }

        public byte[] ReadUnchecked(byte[] data, int count)
        {
            var scratch = new byte[count];
            System.Array.Copy(data, scratch, count);
            return scratch;
        }
    }
}
"""

PY_CALLER = """from reader import ReadBlock


def handle(packet):
    size = packet[0]
    if size > 100:
        return None
    return ReadBlock(packet, size)
"""

NOISE = "\n".join("int common_value = 1;" for _ in range(3)) + "\n"


def build_repo(root: Path) -> Path:
    """Create the synthetic repo under ``root`` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir()
    (root / "src" / "PacketReader.cs").write_text(CS_SOURCE, encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools" / "caller.py").write_text(PY_CALLER, encoding="utf-8")
    (root / "src" / "blob.bin").write_bytes(b"\x00\x01\x02ReadBlock\x00")
    (root / ".env").write_text("TOKEN=ReadBlock\n", encoding="utf-8")
    (root / "src" / "server.pem").write_text("ReadBlock key\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("ReadBlock\n", encoding="utf-8")
    (root / "nested" / "deep").mkdir(parents=True)
    (root / "nested" / "deep" / "note.txt").write_text(NOISE, encoding="utf-8")
    return root


def record(hid, repo_root, *, file="src/PacketReader.cs", lines="13-14", category="bounds",
           label="CONFIRMED", stage="r1", hypothesis="the copy trusts the caller supplied length",
           severity="medium", reason="synthetic"):
    return {"hid": hid, "repo_root": str(repo_root), "file": file, "lines": lines,
            "category": category, "hypothesis": hypothesis, "label": label,
            "severity": severity, "stage": stage, "reason": reason}


def fixture_records(repo_root):
    """Ten synthetic records: mixed labels, both stages, three categories."""
    return [
        record("h01", repo_root, label="CONFIRMED", category="bounds", stage="r1"),
        record("h02", repo_root, label="CONFIRMED", category="bounds", stage="new", lines="21-22"),
        record("h03", repo_root, label="REFUTED", category="bounds", stage="r1", lines="9-12"),
        record("h04", repo_root, label="REFUTED", category="auth", stage="new", lines="9-12"),
        record("h05", repo_root, label="CONFIRMED", category="auth", stage="r1", lines="21-22"),
        record("h06", repo_root, label="UNCERTAIN", category="auth", stage="r1", lines="13"),
        record("h07", repo_root, label="REFUTED", category="race", stage="new", lines="13-14"),
        record("h08", repo_root, label="CONFIRMED", category="race", stage="new", lines="21-22"),
        record("h09", repo_root, label="REFUTED", category="race", stage="r1", lines="9-12"),
        record("h10", repo_root, label="REFUTED", category="bounds", stage="new", lines="13-14"),
    ]


def write_dataset(path: Path, records, *, extra_lines=()) -> Path:
    lines = [json.dumps(item) for item in records]
    lines.extend(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
