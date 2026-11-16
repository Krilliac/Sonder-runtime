"""Short markdown summary of a report (pure; at most 60 lines)."""
from __future__ import annotations

from typing import Any

MAX_LINES = 60


def _num(value: float | None) -> str:
    return "n/a" if value is None else "%.3f" % value


def _row(label: str, conf: dict[str, Any]) -> str:
    return "| %s | %d | %s | %s | %d/%d/%d/%d | %d/%d | %d | %d |" % (
        label, conf["n"], _num(conf["precision"]), _num(conf["recall"]), conf["tp"], conf["fp"],
        conf["fn"], conf["tn"], conf["label_uncertain"], conf["model_uncertain"], conf["unparsed"],
        conf["model_error"] + conf["source_error"] + conf["context_error"])


_HEADER = ("| %s | n | precision | recall | tp/fp/fn/tn | uncertain (label/model) | unparsed | errors |",
           "|---|---|---|---|---|---|---|---|")


def render_summary(report: dict[str, Any]) -> str:
    lines = ["# Security eval: %s" % report.get("requested_model", "?")]
    if not report.get("complete", False):
        lines.append("> INCOMPLETE: stopped (%s), %s item(s) remaining; call again with the same "
                     "out_dir to resume." % (report.get("stopped_reason", "?"), report.get("remaining", "?")))
    lines.append("Mode %s, helper v%s, config %s, arms: %s, rows: %s." % (
        report.get("mode", "?"), report.get("helper_version", "?"), report.get("config_digest", "?"),
        ", ".join(report.get("arms", ())) or "none", report.get("rows", 0)))
    served = report.get("served_models") or {}
    lines.append("Served model: %s [source: %s]." % (
        ", ".join("%s (%d rows)" % (name, count) for name, count in sorted(served.items())) or "none yet",
        report.get("served_model_source", "?")))
    tokens = report.get("tokens") or {}
    lines.append("Tokens: in %s, out %s, measured on %s of %s rows; the rest are gateway estimates or absent." % (
        tokens.get("tokens_in", 0), tokens.get("tokens_out", 0), tokens.get("rows_measured", 0), tokens.get("rows", 0)))
    outcomes = report.get("outcomes") or {}
    if outcomes:
        lines.append("Outcomes: " + ", ".join("%s %d" % (name, count) for name, count in sorted(outcomes.items())) + ".")
    scores = report.get("scores") or {}
    overall = scores.get("overall") or {}
    if overall:
        lines += ["", "## Per arm", _HEADER[0] % "arm", _HEADER[1]]
        lines += [_row(arm, conf) for arm, conf in overall.items()]
    body = []
    for name, values in (scores.get("groups") or {}).items():
        body += ["", "## Per %s" % name, _HEADER[0] % name, _HEADER[1]]
        for value, arms in values.items():
            body += [_row("%s / %s" % (value, arm), conf) for arm, conf in arms.items()]
    paired = report.get("paired")
    tail = []
    if paired:
        tail = ["", "## Paired", "McNemar %s vs %s: n=%d, b=%d, c=%d, p=%.3f (b: %s only right; c: %s only right; "
                "%d item(s) not comparable)." % (paired["arm_a"], paired["arm_b"], paired["n_pairs"], paired["b"],
                                                  paired["c"], paired["p_value"], paired["arm_a"], paired["arm_b"],
                                                  paired["excluded"])]
    chunks = report.get("chunk_scores") or {}
    for arm, item in chunks.items():
        tail.append("Chunk %s: precision floor %s, labelled precision %s, recall %s (%d chunks, %d failed)." % (
            arm, _num(item["precision_floor"]), _num(item["precision_labelled"]), _num(item["recall"]),
            item["chunks"], item["failed_chunks"]))
    room = MAX_LINES - len(lines) - len(tail) - 1
    if len(body) > room:
        body = body[:max(0, room)] + ["| ... | see report.json for the remaining groups |"]
    return "\n".join(lines + body + tail) + "\n"
