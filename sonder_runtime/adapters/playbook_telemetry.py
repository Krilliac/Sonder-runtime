"""Content-free playbook usage joined to the existing grounded outcome store.

The lazy table lives on the caller's memory connection, not in the Markdown.
No table is created on the empty-playbook path. Usage is observation, never
proof that an instruction was obeyed or caused the eventual outcome.
"""
from __future__ import annotations

import re

from sonder_runtime.domain.memory import rules

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playbook_usage (
    interaction_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    entry_id TEXT NOT NULL DEFAULT '',
    loaded_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (interaction_id, topic, entry_id)
)
"""
MAX_USAGE_ROWS = 10000


def _has_usage(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='playbook_usage'"
    ).fetchone() is not None


def log_usage(conn, topics, interaction_id):
    """Link bounded loaded topic/entry identities to a captured interaction.

    Topics may be slug strings or ``{topic, entry_ids}`` records. The caller
    owns the transaction. An absent interaction cannot acquire invented use.
    """
    if not topics or not isinstance(interaction_id, str) or len(interaction_id) > 128:
        return 0
    if not conn.execute("SELECT 1 FROM interactions WHERE id=?", (interaction_id,)).fetchone():
        return 0
    rows = []
    for item in list(topics)[:8]:
        topic = item if isinstance(item, str) else item.get("topic", "")
        if not isinstance(topic, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", topic):
            continue
        entry_ids = [""] if isinstance(item, str) else item.get("entry_ids", [""])
        for entry_id in list(entry_ids)[:64]:
            if isinstance(entry_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{0,128}", entry_id):
                rows.append((interaction_id, topic, entry_id))
    if not rows:
        return 0
    conn.execute(_SCHEMA)
    count = 0
    for row in rows:
        count += conn.execute(
            "INSERT OR IGNORE INTO playbook_usage(interaction_id, topic, entry_id) VALUES(?,?,?)", row,
        ).rowcount
    conn.execute(
        "DELETE FROM playbook_usage WHERE rowid IN "
        "(SELECT rowid FROM playbook_usage ORDER BY loaded_ts DESC, rowid DESC LIMIT -1 OFFSET ?)",
        (MAX_USAGE_ROWS,),
    )
    return count


def usage_report(conn):
    """Report bounded correlations, separating every outcome provenance.

    Joining existing outcomes means caller, machine and blame attribution
    all use their canonical evidence without adding another outcome writer.
    """
    if not _has_usage(conn):
        return {"loaded_turns": 0, "topic_loads": 0, "topics": [], "retained_row_cap": MAX_USAGE_ROWS}
    rows = conn.execute(
        "SELECT u.topic, u.entry_id, o.source, o.signal, COUNT(DISTINCT u.interaction_id) AS n "
        "FROM playbook_usage u LEFT JOIN outcomes o ON o.interaction_id=u.interaction_id "
        "GROUP BY u.topic,u.entry_id,o.source,o.signal ORDER BY u.topic,u.entry_id,o.source,o.signal"
    ).fetchall()
    topics = {}
    for topic, entry_id, source, signal, count in rows:
        item = topics.setdefault((topic, entry_id), {"topic": topic, "entry_id": entry_id, "outcomes": []})
        if signal is not None:
            item["outcomes"].append({
                "source": source or "unknown", "signal": signal, "count": count,
                "good": rules.reward_is_good(signal), "reward": rules.reward_score(signal),
            })
    loaded_turns = conn.execute("SELECT COUNT(DISTINCT interaction_id) FROM playbook_usage").fetchone()[0]
    topic_loads = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT interaction_id, topic FROM playbook_usage)"
    ).fetchone()[0]
    return {
        "loaded_turns": loaded_turns, "topic_loads": topic_loads,
        "topics": list(topics.values()), "retained_row_cap": MAX_USAGE_ROWS,
    }
