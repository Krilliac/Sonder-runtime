# Client surface contracts

Sonder Runtime exposes the same execution facts through the Flutter app, the
terminal REPL, and the loopback browser dashboard without moving prompts,
answers, credentials, or provider diagnostics into metadata.

## Flutter chat responses

The app consumes the OpenAI-compatible assistant content plus these optional,
additive fields:

- `sonder_receipt`: bounded request ID, elapsed milliseconds, resolved model
  and tier, and deterministic request-cache `hit`/`miss` state.
- `usage`: prompt, completion, and total token counts.
- `sonder_activity`: aggregate status plus model/tool call counts.
- `sonder_reasoning`: shown only when the server explicitly exposes it.

Receipt, usage, and activity values are stored in local chat history under
`response_metadata` and rendered under **Response details**. They are never
included in `ChatMessage.toWire()`. A cache hit is labelled **cached replay** so
the source of the response is visible. Older servers may omit every extension;
the answer still renders normally.

HTTP errors retain a bounded type, code, correlation/request ID, HTTP status,
and `Retry-After` value when present. Raw response bodies are not persisted.
Error bubbles and their client diagnostics are excluded from subsequent model
history because they are not assistant answers. The app does not automatically
retry or replay a failed action.

## REPL JSON Lines

`python -m sonder_runtime repl --json` runs the ordinary REPL dispatcher and
permission gates but replaces terminal presentation with one JSON object per
stdout line:

```json
{"schema":"sonder.repl-output.v1","seq":1,"event":"output","text":"..."}
```

The mode suppresses the startup banner, input prompt, animation, and ANSI
styling. `seq` starts at one and increases for the life of the process. Empty
output lines are represented explicitly. stderr remains stderr so shell tools
can separate machine output from process diagnostics.

PowerShell example:

```powershell
@('/stats', '/exit') |
  python -m sonder_runtime repl --json |
  ForEach-Object { $_ | ConvertFrom-Json }
```

JSON mode does not echo submitted input, persist additional history, introduce
retries, or change command interpretation. In particular, it never turns a
failed or interrupted effectful request into an automatic replay.

## Loopback log dashboard

The browser page remains unauthenticated only under the existing direct
loopback-only gate. It reads the same redacted `/v1/local/server-log` projection
and adds no new route or authority.

The page exposes keyboard-focusable pause, refresh, copy, and follow controls;
status changes use an ARIA polite live region. Polls never overlap, time out
after five seconds, back off to at most 30 seconds after failures, and pause
while the page is hidden. Log text is assigned with `textContent`, never HTML.
