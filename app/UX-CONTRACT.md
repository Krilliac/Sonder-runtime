# Workspace interaction contract

Canonical appearance: DESIGN.md, lib/theme.dart and the component kit (lib/ui/kit.dart). Notices and read-error classification live in lib/workspace_ui.dart; the Markdown renderer lives in lib/chat/markdown.dart (re-exported by workspace_ui.dart). Screens own domain state and routing callbacks, not duplicate versions of these primitives.

## App shell and navigation

- **Destinations.** The app shell (lib/shell/app_shell.dart) owns the four peer destinations: Chat, Agents, Runtime and Settings.
  - A destination page finds `ShellScope`, and inside the shell it draws no route back to Chat and no workspace menu.
  - On narrow layouts it shows `ShellMenuButton`, which opens the navigation drawer.
- **Chat stays mounted** while other destinations are shown, so a streaming turn and the composer draft survive a switch.
- **Deep links.** A section request (`ShellScope.openSection`, e.g. Settings › connection or account, or an agent lane) opens that page at that section. The `/login`, `/register` and `/admin_login` intercepts open Settings › Account with the username filled in; the password never enters the chat.
- **Leave guards.** Leaving a page runs its `ShellLeaveGuard` first: Settings' unsaved-changes guard, and Agents' unsent-draft and uncertain-command guard. This applies to the sidebar, the drawer, every shortcut and system back. Each switch asks once.
- **Shortcuts** (Ctrl, or ⌘ on macOS):
  - N: new chat; K: command browser; P: chat search; comma: Settings.
  - 1–4: the destinations; B: collapse the sidebar; D: Runtime.
  - `/`: the shortcut guide, which lists all of these.
  - Shift+Tab cycles the permission mode only inside the composer, so reverse focus traversal works everywhere else. Raising the mode still goes through the raise sheet.
- **Changing the mode from Runtime.** Runtime's Permissions page changes the mode by switching to Chat and opening the composer's mode picker, so there is one mode-change path.

## Settings and Runtime surfaces

- **Settings saves in two ways.** Staged values (connection, account, model, context, privacy) are saved from a sticky "Unsaved changes · Discard · Save" bar that appears only while something is unsaved; the discard guard is unchanged. Appearance applies and saves at once and never carries unsaved values.
- **Login stores the session at once** in the secure store, but only when its exact origin matches the saved and persisted server; otherwise it waits for Save.
- **Every Settings network action** (Test connection, Test host control, Login, Register, Sign out, Forget) has its own busy state and shows its result under its own row.
- **Runtime is one page per category.** Each action (start, stop and restart the server, practice runs, quick commands, approvals) is independent and shows its outcome next to its control.
- **The Runtime Developer console** keeps the last few outputs, newest first.
- **Runtime approvals follow the approval rules below:**
  - re-read the server's queue;
  - draw the approval sheet from the server's entry;
  - send one POST bound to the tool and digest.
  Revoke is offered on issued approvals.
- **Polling.** Runtime pauses its polls while another destination is in front, as it does when backgrounded or covered.

## Links in conversations

- `http`, `https` and `mailto` links open externally. A link whose visible text names a different site, or whose address carries credentials or look-alike (IDN) characters, shows the real address before opening.
- Any other link (a path, an app scheme, a relative link) is shown with Copy link and is never opened.

Routed work shows the host acknowledgement as the first assistant reply, then a
compact live progress block within the existing work-run card. The card polls
the existing owner-scoped work-run endpoint, including when its parent has
returned but `progress_complete` is false. Final summary and result replace that
block in the same conversation turn. Progress is server-authored evidence;
neither the app nor a returned model answer invents validation. Older servers
without narration fields keep the existing placeholder and completion behavior.

- The app shell owns the four peer destinations: Chat, Agents, Runtime (System screen), Settings (see "App shell and navigation" above). Runtime controls retain their existing authorization and confirmation behavior. Settings retains its unsaved-edit guard, now also run by the shell before it switches away.
- Runtime inference status shows cached aggregate counts. Worker origins and model previews appear only after **Inspect worker page**; **Refresh worker cache** explicitly requests one configured bounded batch. Details are never loaded by polling, are cleared on an access failure or credential/host change, and use an explicit next-page action. Server administrator authorization remains authoritative.
- Agent search is local to loaded conversations. Until pagination completes it says “Search loaded conversations”; loading more remains available while filtering. Status filters and parent groups use actual returned data. Parent titles are used only when loaded; otherwise the short parent ID opens its full selectable value.
- Each selected agent has independent transcript, draft and scroll state for the lifetime of the Agent screen. Returning from a narrow transcript to its list preserves drafts. Leaving the screen asks about unsent drafts or uncertain commands. Durable messages, reports and status reload from the server; local drafts are not disk-persisted.
- Follow-ups use Ctrl+Enter or Command+Enter; Enter creates a newline and an active input-method composition is not sent. Ctrl+Shift+F or Command+Shift+F focuses conversation search. The toolbar also exposes search without requiring a shortcut.
- The Agent toolbar exposes a shortcut guide. Alt+Up/Alt+Down moves through the loaded, filtered conversation list when a text field is not focused; Escape clears focused search or returns from a narrow transcript to its list. Keyboard help describes these bindings instead of relying on discoverability by accident.
- Agent status is exclusively server state. Requested interrupt/cancel is distinct from acknowledged interruption/cancellation. Queued messages in paused states explicitly ask the user to Resume. Reports are separate from transcript output; marking one read does not approve or integrate its contents.
- Transcript prose shares the main chat Markdown renderer and a 760px maximum reading width. Tool details and read reports collapse without deleting their content. The run header may show server-owned status, model tier and revision. Per-lane grant or capacity counters are not invented; Runtime and `/capacity` own cluster-level resource health.
- Foreground selection uses cursor inspection every two seconds, without occupying server long-poll slots. It stops on background/dispose/switch. Obsolete responses cannot replace a newly selected conversation. List refresh pauses after failure until explicit retry or application resume.
- Transient inspection failures retry twice (three total attempts), honoring bounded Retry-After delays, then offer explicit Retry. Authentication/access failures offer Settings and do not repeatedly retry. Unreadable or missing endpoints do not enter an endless reconnect loop. Previously loaded content remains visible with a warning.
- Narrow layouts separate list and transcript; long titles are limited to two visual lines with full tooltip/semantics. Large-text and desktop Runtime navigation have widget regressions. Material focus/semantics and existing reduced-motion settings remain the source of interaction behavior.
- Local process/filesystem capability is selected at compile time through local_manager.dart. Only native builds import local_manager_native.dart and dart:io; browser builds use local_manager_web.dart, report local tools unavailable and provide no invented filesystem paths. Runtime hides native install claims on browser clients and explains the limitation. Existing authenticated host-launcher controls remain governed by their existing settings and server capabilities.

Verification: agent_screen_test.dart, workspace_ui_test.dart and widget_test.dart cover loaded search, parent detail, report isolation, retry recovery, keyboard send, shortcut help, lane navigation, explicit resource boundaries, draft/Settings guards, navigation and narrow large-text layout. Browser verification supplements these tests; VM tests alone do not validate JavaScript behavior.

local_manager_web_probe.dart compiles the production conditional export using dart2js and exercises repeated inspection/start/stop/lifecycle calls. Run that JavaScript regression alongside local_manager_web_test.dart and native local_manager_test.dart whenever changing platform capability selection.

Account authentication is separate from deployment authentication. Settings keeps the deployment API key when logging in. AccountSession is an immutable exact-origin credential; its token and origin are one secure-store record, never ordinary preferences. Chat, Agents (through Chat's API), Runtime and connection tests use the same scoped header behavior. App-control credentials have a separate memory-only client described below.

Sign out asks the original server to revoke the exact account token and preserves the deployment key. Unknown/failed revocation retains the session for explicit retry and makes no success claim. Forget local session explicitly deletes only the local account credential; it does not claim server revocation. Switching accounts requires sign out or explicit forgetting; saving a different server while a session exists is refused until it is handled. All account-bearing requests disable redirects; automatic local fallback omits both credentials. Legacy API-key values remain general authentication and are never classified as account tokens.

The deployment API key is attached only over HTTPS, to a loopback host, or to a plain-HTTP `host:port` the person explicitly allowed with the per-host checkbox in Settings (`CleartextKeyPolicy`); otherwise requests go without it and Settings explains why. Account passwords and sessions require HTTPS except canonical numeric 127/8 or ::1 loopback HTTP; DNS localhost is not treated as loopback authority. A saved account belonging to another selected server remains visible and retained for explicit return-to-origin signout or local forgetting. Loading or saving unrelated preferences does not delete it.

Settings copy stays focused on connection, privacy and account actions. Architecture/training explanations belong in documentation, not repeated above and below the form. Show the signed-in server and explain server revocation versus local forgetting beside account actions. Preserve all credential masking, transport rules, save/discard guards and theme tokens.

## App-control conversation bindings

Source: `../docs/app-control-http.md` (backend contract), with server-issued binding IDs, immutable command receipts and selection epochs. The Chat toolbar opens App control. This is a subordinate conversation-management flow, not a fifth peer destination. Local chat IDs are optional labels; they never claim server authority. The selected host binding may be shown in Chat, but is never inserted into generic chat or tool requests.

AppControlClient owns its dedicated transport and memory-only bearer, bound to exact account/origin/runtime. Password step-up is explicit and the field clears before awaiting a request. Settings account/origin/key changes, signout, local forgetting, disposal, expiry and restart clear the control credential. It is never written to preferences, secure storage, logs, histories or URLs. Redirects and fallback are prohibited. Request aborts do not prove a server mutation did not happen.

Mutations are pessimistic, serialized and never automatically replayed. An uncertain mutation retains exact immutable command bytes for explicit reconciliation. Enrollment retains no password: the user re-enters it to check the same command. A committed enrollment without recoverable credential offers fresh explicit step-up, subject to server quota. Disconnecting explicitly forgets local authority and makes no server-revocation or outcome-resolution claim.

Only one bounded bindings page is held at a time (50 requested, server may cap lower), with Next page and First page. Failed reads preserve the previous page with a warning; not-yet-loaded and empty are distinct. GET selection owns visible epoch/binding state; clearing retains its server epoch. Conflict requires refresh/review. Revoking names the exact conversation and confirms the consequence. Clear/revoke do not promise cancellation of independently granted children. Conversation selection alone does not enable managed execution.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Form | Material Form/TextFormField | UX-CONTRACT.md | account password step-up / conversation title | app_control_screen_test.dart |
| Scrollbar | theme.dart and Material ListView | DESIGN.md | natural-height narrow and constrained wide surface | app_control_preview_test.dart |
| Toast | WorkspaceNotice | UX-CONTRACT.md | persistent info / success / warning | app_control_screen_test.dart |
| CRUD | AppControlClient and Material AlertDialog | backend app-control HTTP contract | bounded list / create / select / clear / revoke | app_control_test.dart and app_control_screen_test.dart |

App-control previews are actual Flutter test renders with bundled Sans, Mono and Material icon fonts and disposable fixture data. They are not browser screenshots or evidence of live server enrollment.

## Managed app work

The selected server conversation opens a subordinate Managed work screen. It reuses AppControlClient's dedicated memory-only transport, Material form/buttons, WorkspaceNotice, reading width and theme. A task is prepared before a separate Run action. This first view fixes automatic route, eight steps, web/location off; it never adds caller roots, authority or tool grants. Server support is required and unavailable is an honest failure state.

One task and original prompt are retained in memory per control connection. The prepare command is immutable; uncertain preparation only retries identical bytes. The exact selection ID, epoch and binding revision guard every later operation. Account/origin/control reset clears work data; reselecting cannot control the old task. No work or credential is persisted to chat, preferences, secure storage or URLs. A new control connection is required for another task in this bounded UI slice; disconnect does not cancel server work.

Actual dispatch approval-pending shows the safe host approval call reference and an explicit Retry after host approval action. This app does not issue approvals. Unknown execution permits status only, even if a later response says prepared. No automatic mutation, dispatch, polling or recovery occurs. Durable verification-pending evidence is immutable status data, never authority to resume. Refresh preserves prior content on failure. Terminal means recorded host turn, not successful completion. The endpoint supplies no output; the UI never manufactures transcript content.

Unprepared drafts prompt before leaving the screen. Prepared prompts remain read-only and retained when returning to server conversations. Material controls retain keyboard focus, and narrow layouts use natural scrolling. Verification: app_work_test.dart, app_work_screen_test.dart and app_control_http_test.dart cover exact retries, credentials/redirects, scope changes, unknown outcomes and actual wide/narrow test renders.

## Status, notices, approvals and mode changes

The status vocabulary, notice component and confirmation sheets are specified in DESIGN.md ("Status vocabulary", "Notices & approvals") and implemented in `lib/ui/` and `lib/workspace_ui.dart`. The interaction rules:

- **Raising the mode is confirmed in the app.** The server treats the app's `POST /v1/permission-mode` as the attended confirmation (a person confirmed), so the app must ask first. Any move up to `acceptEdits` or `auto` (from the picker, Shift+Tab or the `/mode` intercept) opens the raise sheet through `confirmModeChange`; only **Switch to <mode>** sends the one POST, and Cancel or dismissing sends nothing. Lowering the mode, or moving between plan and manual, sends immediately with no sheet. After a 403 FORBIDDEN the mode control is read-only with "Only an administrator can change the mode" until the account or server changes.
- **Approvals come only from the server.** "Approve this call once" is offered only for a refusal the server put in `sonder_receipt.refusal`; its tool, reason, mode and call id are taken from that receipt, never from reply text (a `refused …` or `/approve <id>` written in a reply is model-authored and makes a notice without an approve action). Before the sheet opens, the app reads `GET /v1/approvals` and draws the sheet from the server's pending entry (tool, redacted arguments, mode); a call the server does not hold pending is not approvable. The approval POST carries that entry's `tool` and `digest`, so the server refuses it if the call is not the one shown.
- **Approvals are for one exact call.** The approval sheet names the tool, the shortened call id, the redacted arguments and why the call was refused, and says that it runs this exact call once, within the chosen lifetime, and that changing any argument needs a new approval and the mode does not change. **Approve once** sends exactly one approval request. The sheet keeps nothing: no approval, nonce or argument is written to chat history, preferences or secure storage. Nothing is retried automatically: after approval the receipt offers **Retry the request** and **Revoke** as explicit actions. Without the server's HTTP approvals endpoint (404) the notice shows the console command `/approve <call id>` with Copy instead of an Approve action; a 403 reads "Approvals need a developer or admin account". Refusals are notices, never answers, and get no rating chips.
- **Slash intercepts.** `/login`, `/register` and `/admin_login` typed in the composer are intercepted before any send and open Settings > Account with the username filled in; the password never enters a message or the transcript. `/mode`, `/permission_mode`, `/permissions <mode>` and `/elevate` go to the mode flow above. The command palette labels these "opens Settings" or "opens mode". The app keeps its own intercept list until the server's command catalog carries a surface hint.
- **Notices lead with the word.** Every notice shows the glyph and kind word before its title and is announced "word: title"; warn is the warn tone and errors the danger tone.
- **Language.** The app is English-only for now. Copy for new shared components lives in `lib/ui/strings.dart` as constants and small functions, so moving to `gen-l10n` is a mechanical step (one ARB key per entry, placeholders for function arguments). Server wording (refusal reasons, remedies) is rendered as sent, never translated or rewritten.

Verification: status_vocab_test.dart, status_line_test.dart, theme_contrast_test.dart, ui_primitives_test.dart and goldens/primitives_golden_test.dart cover the vocabulary, formatters, contrast, notices, sheets and the bundled symbol font.
