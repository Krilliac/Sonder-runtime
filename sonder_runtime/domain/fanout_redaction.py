"""Pure prompt-echo redaction for durable model-fanout receipts.

A fanout answer is written to a durable receipt, so any verbatim request
material inside it is persistent disclosure. The policy is explicit-input
and side-effect free: it takes the rendered text and the prompt and returns
the text with echoed spans replaced. Moved from ``server.py`` in the WP1
Two-Hundred-Ninety-Sixth Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import re

REDACTED_PROMPT = "<redacted prompt>"
REDACTED_ANSWER = "<redacted fanout answer>"


def redact_prompt_echo(value, prompt) -> str:
    """Remove verbatim request material before a durable receipt is written.

    Models frequently preface an answer by quoting only part of their input,
    rather than echoing the whole prompt.  The receipt is durable, so a
    full-string replacement alone would turn that small presentation choice
    into persistent disclosure.  Find qualifying spans independently, rather
    than using an order-dependent sequence alignment: models can quote prompt
    excerpts in a different order.  The scan has a fixed comparison budget;
    if a highly repetitive input exhausts it, redact the whole answer instead
    of risking disclosure or holding up a fanout worker.
    """
    rendered = str(value or "")
    question = str(prompt or "")
    if not question or not rendered:
        return rendered
    if question in rendered:
        return rendered.replace(question, "<redacted prompt>")
    seed_size, minimum_span, comparison_budget = 12, 24, 128_000
    if len(question) < seed_size or len(rendered) < seed_size:
        return rendered
    # Sampling every seed_size characters is sufficient: a shared span of at
    # least two seeds necessarily contains one complete sampled seed. Index
    # response windows once, then expand only matching candidates.
    source_seeds = {}
    for source_start in range(0, len(question) - seed_size + 1, seed_size):
        source_seeds.setdefault(question[source_start:source_start + seed_size], []).append(source_start)
    response_seeds = {}
    for response_start in range(0, len(rendered) - seed_size + 1):
        seed = rendered[response_start:response_start + seed_size]
        if seed in source_seeds:
            response_seeds.setdefault(seed, []).append(response_start)
    spans, comparisons = [], 0
    for seed, source_positions in source_seeds.items():
        for source_start in source_positions:
            for response_start in response_seeds.get(seed, ()):
                left_source, left_response = source_start, response_start
                while left_source and left_response and question[left_source - 1] == rendered[left_response - 1]:
                    comparisons += 1
                    if comparisons > comparison_budget:
                        return "<redacted fanout answer>"
                    left_source -= 1; left_response -= 1
                right_source = source_start + seed_size
                right_response = response_start + seed_size
                while (right_source < len(question) and right_response < len(rendered)
                       and question[right_source] == rendered[right_response]):
                    comparisons += 1
                    if comparisons > comparison_budget:
                        return "<redacted fanout answer>"
                    right_source += 1; right_response += 1
                size = right_response - left_response
                fragment = question[left_source:right_source]
                labeled_secret = re.search(
                    r"(?:api[ _-]?key|token|secret|password|bearer|authorization)",
                    fragment, re.IGNORECASE,
                )
                compact_credential = re.search(r"(?=.*\d)[A-Za-z0-9_./:+-]{8,}", fragment)
                if size >= minimum_span or (size >= 8 and (labeled_secret or compact_credential)):
                    spans.append((left_response, right_response))
    if not spans:
        return rendered
    # SequenceMatcher reports non-overlapping blocks, but merge defensively so
    # this stays correct if its implementation or our thresholds change.
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    parts, cursor = [], 0
    for start, end in merged:
        parts.append(rendered[cursor:start])
        parts.append("<redacted prompt>")
        cursor = end
    parts.append(rendered[cursor:])
    return "".join(parts)
