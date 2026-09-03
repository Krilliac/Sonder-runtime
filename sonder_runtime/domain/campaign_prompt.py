"""Pure generation prompt for one campaign task.

The campaign asks a model for a complete runnable program in one fenced
block, with a repair note after a failed attempt and a few language-specific
cautions. The code-fence table is injected by the caller. Moved from
``server.py`` in the WP1 Three-Hundred-Twenty-First Slice with its behaviour
byte-for-byte intact.
"""
from __future__ import annotations


def campaign_prompt(language, task_name, task_text, repair_note="", *, fences):
    """Build the bounded generation prompt for one campaign task.

    ``fences`` maps a language to its code-fence label; it is injected so the
    prompt stays free of the execution adapter that owns the table.
    """
    fence = fences.get(language, language)
    repair = ("\nPrevious attempt failed:\n%s\nFix it." % repair_note) if repair_note else ""
    language_note = ""
    if language == "powershell" and task_name == "string":
        language_note = (
            " PowerShell arrays print one item per line; when building a string from "
            "characters, reverse by index/order and join explicitly with -join; do not "
            "sort the characters."
        )
    if language == "powershell" and task_name == "list":
        language_note = (
            " In PowerShell, use Measure-Object -Sum or a simple loop to sum numeric "
            "arrays; do not use Invoke-Expression for arithmetic."
        )
    if language == "cpp" and task_name == "string":
        language_note = (
            " In C++, include <algorithm> before using std::reverse, or reverse the "
            "string manually."
        )
    return (
        "Write a complete runnable %s program for this task: %s.\n"
        "Return only one ```%s code block. Do not use interactive input. "
        "The program must terminate quickly.%s%s" % (
            language, task_text, fence, language_note, repair)
    )
