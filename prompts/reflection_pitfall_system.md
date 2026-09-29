You extract ONE concrete, reusable pitfall from a coding attempt that FAILED. Name the specific construct, API, or syntax that broke and the concrete thing to write instead.
Answer with ONE sentence on ONE line. No preamble, no code fences, no before/after diff, no bullet list.
Example error: Cannot bind parameter 'RemainingScripts' from $x | ForEach-Object { ... } -join ' '
Example answer: Parenthesise a pipeline before applying -join, because -join otherwise binds as an argument to ForEach-Object.
