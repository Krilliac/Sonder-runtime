"""Compress old conversation turns into a running summary, and title sessions.

Both run on the cheap `fast` tier via the same offload_fn the reflection loop uses.
Mirrors reflection.distill's shape. Callers must handle transport errors (URLError);
these functions assume offload_fn either returns text or raises.
"""

SUMMARY_SYSTEM = (
    "You maintain a running summary of an ongoing coding conversation. Output a "
    "concise summary (terse bullet points, <=150 words) capturing decisions made, "
    "code/APIs settled on, and open threads. No preamble, no markdown headers."
)

TITLE_SYSTEM = (
    "You write a short 3-6 word title for a coding conversation. Output only the "
    "title text: no quotes, no trailing punctuation, no preamble."
)


def summarize(old_summary, turns, offload_fn):
    """Fold `turns` [(task, response), ...] into `old_summary`, returning the update.

    Incremental: the previous summary is included so already-summarized context is
    preserved without re-reading the full transcript.
    """
    convo = "\n".join(
        "USER: %s\nASSISTANT: %s" % (t, r) for t, r in turns
    )
    prior = ("PREVIOUS SUMMARY:\n%s\n\n" % old_summary) if old_summary else ""
    prompt = (
        "%sNEW TURNS TO FOLD IN:\n%s\n\n"
        "Produce the updated running summary." % (prior, convo)
    )
    text = offload_fn(
        prompt=prompt, tier="fast", system=SUMMARY_SYSTEM,
        temperature=0.0, num_predict=256,
    )
    return (text or "").strip()


def make_title(first_prompt, offload_fn, max_len=60):
    """A short title from the session's first prompt. Falls back to a truncation."""
    fallback = (first_prompt or "").strip().splitlines()[0][:40] if first_prompt else "session"
    try:
        text = offload_fn(
            prompt="Title this coding request: %s" % first_prompt,
            tier="fast", system=TITLE_SYSTEM, temperature=0.0, num_predict=16,
        )
    except Exception:
        return fallback
    if not text:
        return fallback
    # First line, THEN strip wrapping quotes (a trailing quote can sit before \n).
    title = text.strip().splitlines()[0] if text.strip() else ""
    title = title.strip().strip('"').strip("'").strip()
    return title[:max_len] if title else fallback
