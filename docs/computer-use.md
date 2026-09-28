# Gated desktop computer use

Sonder can see and drive one application window on a Windows desktop. It
captures the window, asks the local vision model about it, and sends mouse and
keyboard input. Every part of this is off by default. Each action passes five
independent layers, and any one of them can stop it.

## Tools

| Tool | Grade | What it does |
|---|---|---|
| `computer_use_status` | safe | Whether the feature is enabled, the allowlist, and the running session. |
| `window_list` | ask | Open windows of allowlisted apps. Other windows are only counted, never named. |
| `computer_use_start` | **dangerous** | Starts a driving session on one allowlisted window. A person approves it at the console. |
| `computer_use_stop` | ask | Ends the session. The kill hotkey and the Stop button end it too, outside the gate. |
| `screen_capture` | ask | Captures the session window to a PNG. With `question`, it also asks the vision model about the capture. |
| `ui_action` | execution | One action: `click`, `double_click`, `right_click`, `move`, `type`, `key` or `scroll`. |
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
