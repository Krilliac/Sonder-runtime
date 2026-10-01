"""Bounded, report-only playbook findings alongside the lesson quality audit."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from sonder_runtime.adapters.playbook_telemetry import usage_report
from sonder_runtime.domain.memory.playbooks import similarity

MAX_AUDIT_ENTRIES = 256
MAX_FINDINGS = 20


def _canonical(text):
    return " ".join(text.lower().split())


def report(store, *, conn=None, config=None, today=None, detect_conflicts=None):
    """Inspect notes without changing their Markdown or approval status.

    Conflict pairs use the existing lesson detector. Without scored use they
    are explicitly lexical candidates for owner review, never proven errors.
    Exact duplicate merging is a plan to supersede duplicates, not deletion.
    """
    current = today or date.today()
    thresholds = {
        "environment": getattr(config, "environment_stale_days", 30),
        "measurement": getattr(config, "measurement_stale_days", 90),
    }
    entries = []
    topics = store.list_topics()
    truncated = False
    for topic in topics:
        for entry in store.read(topic["topic"], approved_only=False):
            if len(entries) == MAX_AUDIT_ENTRIES:
                truncated = True
                break
            entries.append(entry)
        if truncated:
            break
    statuses = Counter(entry.get("status", "proposed") for entry in entries)
    duplicate_groups = defaultdict(list)
    stale = []
    for entry in entries:
        if entry.get("status") in ("rejected", "superseded"):
            continue
        duplicate_groups[(entry["category"], _canonical(entry["body"]))].append(entry)
        if entry["category"] in thresholds:
            try:
                age = (current - date.fromisoformat(entry["date"])).days
            except (ValueError, TypeError):
                age = None
            if age is None or age >= thresholds[entry["category"]]:
                if len(stale) < MAX_FINDINGS:
                    stale.append({
                        "topic": entry["topic"], "id": entry["id"],
                        "category": entry["category"], "age_days": age,
                    })
    duplicates = [
        {"keeper": {"topic": group[0]["topic"], "id": group[0]["id"]},
         "supersede": [{"topic": entry["topic"], "id": entry["id"]} for entry in group[1:]]}
        for group in duplicate_groups.values() if len(group) > 1
    ][:MAX_FINDINGS]
    usage = usage_report(conn) if conn is not None else {}
    evidence = {(row["topic"], row["entry_id"]): row for row in usage.get("topics", [])}
    candidates = []
    for entry in entries:
        if entry.get("status") != "approved":
            continue
        scored = evidence.get((entry["topic"], entry["id"]), {}).get("outcomes", [])
        # Caller judgement has precedence, as in conflicting lesson pairs.
        caller = [outcome for outcome in scored if outcome["source"] == "caller"]
        outcomes = caller or [outcome for outcome in scored if outcome["source"] != "unknown"]
        item = {"id": entry["id"], "topic": entry["topic"], "text": entry["body"],
                "evidence": "caller" if caller else "execution" if outcomes else "lexical"}
        if outcomes:
            count = sum(outcome["count"] for outcome in outcomes)
            item["score"] = sum(outcome["reward"] * outcome["count"] for outcome in outcomes) / count
        candidates.append(item)
    conflicts = detect_conflicts(candidates, similarity, sim_threshold=0.65) if detect_conflicts else []
    return {
        "report_only": True, "topics": len(topics), "entries_checked": len(entries),
        "truncated": truncated, "entry_scan_cap": MAX_AUDIT_ENTRIES,
        "conflict_check_available": detect_conflicts is not None,
        "status_counts": dict(statuses), "index_bytes": len(store.approved_index().encode("utf-8")),
        "duplicate_merge_plans": duplicates, "stale": stale,
        "conflict_candidates": [
            {"a": {"topic": item["a"]["topic"], "id": item["a"]["id"]},
             "b": {"topic": item["b"]["topic"], "id": item["b"]["id"]},
             "similarity": round(item["similarity"], 4),
             "evidence": [item["a"]["evidence"], item["b"]["evidence"]]}
            for item in conflicts[:MAX_FINDINGS]
        ],
        "usage": usage,
    }


def format_report(section):
    counts = section["status_counts"]
    return [
        "  playbooks (report-only): %s topics, %s entries checked%s, %s approved index bytes"
        % (section["topics"], section["entries_checked"],
           " (bounded sample)" if section["truncated"] else "", section["index_bytes"]),
        "    proposed=%s approved=%s rejected=%s superseded=%s"
        % tuple(counts.get(name, 0) for name in ("proposed", "approved", "rejected", "superseded")),
        "    duplicate merge plans=%s stale=%s conflict candidates=%s (owner review required)"
        % (len(section["duplicate_merge_plans"]), len(section["stale"]), len(section["conflict_candidates"])),
        "    recorded loaded turns=%s topic loads=%s; outcomes are correlations, not proof of causation"
        % (section["usage"].get("loaded_turns", 0), section["usage"].get("topic_loads", 0)),
    ]
