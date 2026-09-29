# Gated desktop computer use

Sonder can see and drive one application window on a Windows desktop. It
captures the window, asks the local vision model about it, and sends mouse and
keyboard input. Every part of this is off by default. Each action passes five
independent layers, and any one of them can stop it.

## Tools

The console REPL and ordinary default-model HTTP `/v1/chat/completions` turns
recognize desktop requests before workspace routing. Examples:

- `control notepad and type hello`, `open notepad and write a shopping list`,
  and `click the Save button in paint` call `computer_task` after a status check.
- `what's on my screen`, `show me my screen`, `look at my screen`, and
  `read my screen` call `screen_capture` after a status check. This observes the
  approved session window, not the whole desktop.
- `use my computer to ...` (also `control`/`drive`, and `desktop`) expresses an
  explicit desktop task, including when its contents mention code or files.
- `stop controlling`, `stop driving`, and `stop computer use` call the gated
  stop tool directly, without waiting for a model. `please` and
  `can/could/would/will you` prefixes are recognized.

Control/open/launch/start requests must name a known desktop app immediately;
click/type/press/write/enter/scroll requests need an `in/into/on/using <app>`
target. Recognized app names are Notepad, Paint, Calculator/Calc, Explorer,
Browser, Chrome, Edge, Microsoft Word, Excel, Terminal, PowerShell, Cmd,
Outlook, Discord, and Slack; executable `.exe` suffixes are accepted. For other
apps, use the explicit computer/desktop phrasing. Recognition grants no access:
the configured executable allowlist still applies.

Ordinary coding/file requests such as `type hints in foo.py`, `click handler
bug`, `open the file X`, and `open notepad.py` stay on their existing routes.
Questions about computer performance or screen documentation do too.

If configuration is disabled or the allowlist is empty, the route explains the
configuration needed. If no session is active, it asks the person to open an
allowlisted app, call `computer_use_start` with its app or window handle, approve
the pending call with `/approve <id>` at the console, and retry the identical
start call. Chat does not launch apps or synthesize that approval. Stop and
setup guidance do not depend on model availability. Structured-output requests
and explicit non-default HTTP model routes keep their existing behavior.

The general model-driven agent loop also discovers these tools through its MCP
registry fallback. Missing help entries include their actual input schemas.
Recursive entry points, system-operator operations, and admin/elevation tools
are excluded from the fallback. Every invocation still crosses the agent gate
and per-run restrictions; newly exposed generic tools stay local to protect
desktop observations and other host data from hosted models.

| Tool | Grade | What it does |
|---|---|---|
| `computer_use_status` | safe | Whether the feature is enabled, the allowlist, and the running session. |
| `window_list` | ask | Open windows of allowlisted apps. Other windows are only counted, never named. |
| `computer_use_start` | **dangerous** | Starts a driving session on one allowlisted window. A person approves it at the console. |
| `computer_use_stop` | ask | Ends the session. The kill hotkey and the Stop button end it too, outside the gate. |
| `screen_capture` | ask | Captures the session window to a PNG. With `question`, it also asks the vision model about the capture. With `controls=true`, it also reads the window's control table (see [Semantic perception](#semantic-perception-ui-automation)). |
| `ui_action` | execution | One action: `click`, `double_click`, `right_click`, `move`, `type`, `key` or `scroll`, at x/y or on a control `ref`. |
| `computer_task` | execution | The vision model drives toward a goal, one gated step at a time. |

## Layers

1. **Configuration.** `[computer_use] enabled = true` with a non-empty
   `allowed_apps` list of executable names. Without it, every tool refuses
   before touching the desktop.

   ```toml
   [computer_use]
   enabled = true
   allowed_apps = ["notepad.exe"]
   # session_ttl_seconds = 900, max_actions_per_minute = 60,
   # max_actions_per_session = 500, max_task_steps = 25, verify_clicks = true
   ```

2. **The permission gate on each tool.** `computer_use_start` is `dangerous`,
   so an unattended caller (an MCP client, the HTTP chat, an agent) is refused.
   The refusal carries a call id that a person approves with `/approve <id>`
   at the Sonder console, and the identical call then proceeds. `ui_action`
   and `computer_task` are `execution`. The operator's mode and rules decide
   them, like any host program.
3. **A live session**, re-proved before every action. It binds one top-level
   window of an allowlisted executable. The session ends instead of acting when
   any of these holds:
   - its TTL has passed, or its action budget is spent;
   - the indicator has closed;
   - someone pressed the kill hotkey or Stop;
   - the window closed, changed process, or left the allowlist;
   - **a person used the mouse or keyboard since Sonder's last action**
     (taking the controls back is itself a stop signal).
4. **The physical check.** A pointer action must land on the session window,
   at a point no other window covers. The Windows key and chords that switch
   away from the window (Alt+Tab, Ctrl+Esc, Ctrl+Shift+Esc, ...) are refused.
5. **Irreversible actions.** An action takes a second, separate `dangerous`
   decision named `computer_use_irreversible` when it looks irreversible:
   - a click whose target reads as send, delete, purchase, pay, submit, post,
     publish, transfer, uninstall, and similar;
   - Shift+Delete, or Delete in File Explorer;
   - Alt+F4;
   - Enter, or a typed newline, in a messaging app (`submit_on_enter_apps`).

   A person approves each one at the console. An allow rule or `auto` mode for
   `ui_action` never covers it.

## The indicator and the kill switch

`computer_use_start` launches a separate process that shows a red "Sonder is
driving …" bar at the top of the screen. The bar never takes focus. It has a
**Stop** button and registers the global hotkey **Ctrl+Alt+Shift+K**.

If the hotkey is already taken, the session does not start: no working kill
switch, no driving. If the bar's process exits for any reason, the next action
ends the session.

## Screen content is untrusted

Window titles, captured pixels and the vision model's reading of them come
from the screen, and any application can put text there. They reach callers
and models inside the untrusted-observation envelope
(`domain/agents/observation_prompt.py`).

Screen text is used in exactly one decision, and only to *add* friction. With
`verify_clicks` on, the vision model names the control under each click. If
that name, or the caller's `target_label`, looks irreversible, the click needs
confirmation. If the model cannot name the control, the click is treated as a
submit.

## Semantic perception (UI Automation)

Before vision, Sonder reads the session window's UI Automation tree
(`adapters/desktop/uia.py`, plain ctypes COM against the system's
`UIAutomationCore`; no extra dependency). `screen_capture(controls=true)` and
every `computer_task` step turn it into a compact control table
(`domain/computer_use/controls.py`): one line per visible, on-screen control,
at most 200 rows, with a stable `ref` (derived from the control's runtime id),
its role, name, value, enabled/checked/selected/expanded state and its centre
on the 0–1000 grid.

- **Untrusted panes.** Documents, web views (and everything inside them) and
  edit fields are marked `content-untrusted`: their text is withheld
  (`value=withheld`) and names inside them are clipped, so page text never
  reaches a planner. Password values are never shown. The whole table is
  returned inside the untrusted-observation envelope.
- **Acting by ref.** `ui_action(action=..., ref=...)` passes every layer above
  (configuration, the tool gate, the live-session re-proof, the budget, the
  irreversible gate). The control's own name joins the labels the irreversible
  check reads, so it can add a confirmation. With `verify_clicks`, a click on
  an unnamed control, or on one inside untrusted content (a page chooses its
  controls' accessible names), still gets the vision reading of an x/y click.
  Before input, after the gate and the focus change, the ref is resolved again
  and refused, with a request to re-observe, unless the control still exists
  with the same role and name, is enabled and visible, and is topmost at its
  point (`ElementFromPoint` hits the control or one of its descendants). The
  live session is then proved again and the window brought to the front
  immediately before input, as on the x/y path.
- **Patterns first.** A click uses Invoke, Toggle or SelectionItem when the
  control has one; `type` into an *empty* edit, combo box or spinner uses the
  Value pattern. `type` into a field that already holds text, `type`
  elsewhere, and `key` focus the control and then send keys, so typing by ref
  inserts like typing by x/y and never replaces what the field held. Other
  actions are a synthetic pointer action at the control's centre, with the
  usual physical check.
- **Verify.** After acting, Sonder reads the control again and reports what
  changed and whether the expected change happened (`verify.expected_met`:
  true, false, or null when the action has no observable state), plus a fresh
  control table. Values are compared, never echoed.
- **Vision fallback.** When nothing is readable (no UI Automation, a
  custom-drawn surface, an error), the table is empty and `computer_task`
  uses exactly the vision-only prompt; x/y actions are unchanged.

## Coordinates

`coords="normalized"` (the default) is a 0–1000 grid across the window. The
Qwen-VL family answers in this grid natively. Measured on Qwen3.8 27B, the
model returned (812, 873) for a button centred at (1040, 700) of a 1280×800
image: exact once rescaled, while reading the numbers as pixels missed by
about 290 px. `coords="pixels"` refers to the last `screen_capture` image and
is refused if the window was resized since that capture.

## Privacy

Captures are rendered from the session window alone with `PrintWindow`, so
overlapping windows and the indicator never appear in them. They are written
under `<SONDER_HOME>/computer_use/captures`, which keeps the newest 20. The
vision gateway refuses cloud and remote-Ollama targets, so pixels stay on this
machine.

An allowlisted window shows whatever it contains. Applications that restore
earlier sessions (Windows Notepad reopens unsaved tabs) can show old content
you did not expect. Allowlist narrowly.

## Limits

- **Windows only.** Other hosts report the desktop as unavailable.
- **Elevated windows.** An elevated window cannot receive input from a
  non-elevated runtime. Windows drops the input, and the action reports it as
  blocked.
- **Speed.** One step of `computer_task` costs one vision call, about 2 s on a
  warm Qwen3.8 27B. The first call also loads the model.
