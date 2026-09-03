# WP1 Three-Hundred-Twenty-First Slice — campaign task prompt

## Boundary

The generation prompt for one campaign task (`_campaign_prompt`) now lives
in `sonder_runtime/domain/campaign_prompt.py` as `campaign_prompt`, with the
single-fenced-block contract, the repair note and the language-specific
cautions unchanged. The code-fence table is injected as `fences`, so the
domain never imports the execution adapter that owns it. `server.py` keeps
`_campaign_prompt` as a thin delegate injecting `grounding._LANG_FENCE` at
call time.

## Evidence

- `tests/test_campaign_prompt_boundary.py` verifies that the root delegate injects the grounding fences, the prompt's language, task, fence and repair-note rendering, and the task-specific language notes.
- `python -m pytest -q tests/test_campaign_prompt_boundary.py tests/test_server_helpers.py -k 'boundary or campaign'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
