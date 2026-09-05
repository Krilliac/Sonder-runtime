# Workspace interaction contract

Canonical appearance: DESIGN.md and lib/theme.dart. Shared workspace navigation, notices, read-error classification and Markdown live in lib/workspace_ui.dart. Screens own domain state and routing callbacks, not duplicate versions of these primitives.

- Chat owns the four peer routes: Chat, Agents, Runtime (System screen), Settings. Runtime controls retain their existing authorization and confirmation behavior. Settings retains its existing unsaved-edit guard.
- Agent search is local to loaded conversations. Until pagination completes it says “Search loaded conversations”; loading more remains available while filtering. Status filters and parent groups use actual returned data. Parent titles are used only when loaded; otherwise the short parent ID opens its full selectable value.
- Each selected agent has independent transcript, draft and scroll state for the lifetime of the Agent screen. Returning from a narrow transcript to its list preserves drafts. Leaving the screen asks about unsent drafts or uncertain commands. Durable messages, reports and status reload from the server; local drafts are not disk-persisted.
- Follow-ups use Ctrl+Enter or Command+Enter; Enter creates a newline and an active input-method composition is not sent. Ctrl+Shift+F or Command+Shift+F focuses conversation search. The toolbar also exposes search without requiring a shortcut.
- Agent status is exclusively server state. Requested interrupt/cancel is distinct from acknowledged interruption/cancellation. Queued messages in paused states explicitly ask the user to Resume. Reports are separate from transcript output; marking one read does not approve or integrate its contents.
- Transcript prose shares the main chat Markdown renderer and a 760px maximum reading width. Tool details and read reports collapse without deleting their content. No timestamps, progress percentages or resource values are invented.
- Foreground selection uses cursor inspection every two seconds, without occupying server long-poll slots. It stops on background/dispose/switch. Obsolete responses cannot replace a newly selected conversation. List refresh pauses after failure until explicit retry or application resume.
- Transient inspection failures retry twice (three total attempts), honoring bounded Retry-After delays, then offer explicit Retry. Authentication/access failures offer Settings and do not repeatedly retry. Unreadable or missing endpoints do not enter an endless reconnect loop. Previously loaded content remains visible with a warning.
- Narrow layouts separate list and transcript; long titles are limited to two visual lines with full tooltip/semantics. Large-text and desktop Runtime navigation have widget regressions. Material focus/semantics and existing reduced-motion settings remain the source of interaction behavior.
- Local process/filesystem capability is selected at compile time through local_manager.dart. Only native builds import local_manager_native.dart and dart:io; browser builds use local_manager_web.dart, report local tools unavailable and provide no invented filesystem paths. Runtime hides native install claims on browser clients and explains the limitation. Existing authenticated host-launcher controls remain governed by their existing settings and server capabilities.

Verification: agent_screen_test.dart, workspace_ui_test.dart and widget_test.dart cover loaded search, parent detail, report isolation, retry recovery, keyboard send, draft/Settings guards, navigation and narrow large-text layout. Browser verification supplements these tests; VM tests alone do not validate JavaScript behavior.

local_manager_web_probe.dart compiles the production conditional export using dart2js and exercises repeated inspection/start/stop/lifecycle calls. Run that JavaScript regression alongside local_manager_web_test.dart and native local_manager_test.dart whenever changing platform capability selection.

Account authentication is separate from deployment authentication. Settings keeps the deployment API key when logging in. AccountSession is an immutable exact-origin credential; its token and origin are one secure-store record, never ordinary preferences. Chat, Agents (through Chat's API), Runtime and connection tests use the same scoped header behavior. App-control credentials have a separate memory-only client described below.

Sign out asks the original server to revoke the exact account token and preserves the deployment key. Unknown/failed revocation retains the session for explicit retry and makes no success claim. Forget local session explicitly deletes only the local account credential; it does not claim server revocation. Switching accounts requires sign out or explicit forgetting; saving a different server while a session exists is refused until it is handled. All account-bearing requests disable redirects; automatic local fallback omits both credentials. Legacy API-key values remain general authentication and are never classified as account tokens.

Account passwords and sessions require HTTPS except canonical numeric 127/8 or ::1 loopback HTTP; DNS localhost is not treated as loopback authority. A saved account belonging to another selected server remains visible and retained for explicit return-to-origin signout or local forgetting. Loading or saving unrelated preferences does not delete it.

Settings copy stays focused on connection, privacy and account actions. Architecture/training explanations belong in documentation, not repeated above and below the form. Show the signed-in server and explain server revocation versus local forgetting beside account actions. Preserve all credential masking, transport rules, save/discard guards and theme tokens.

## App-control conversation bindings

Source: `../docs/app-control-http.md` (backend contract), with server-issued binding IDs, immutable command receipts and selection epochs. The Chat toolbar opens App control. This is a subordinate conversation-management flow, not a fifth peer destination. Local chat IDs are optional labels; they never claim server authority. The selected host binding may be shown in Chat, but is never inserted into generic chat or tool requests.

AppControlClient owns its dedicated transport and memory-only bearer, bound to exact account/origin/runtime. Password step-up is explicit and the field clears before awaiting a request. Settings account/origin/key changes, signout, local forgetting, disposal, expiry and restart clear the control credential. It is never written to preferences, secure storage, logs, histories or URLs. Redirects and fallback are prohibited. Request aborts do not prove a server mutation did not happen.

Mutations are pessimistic, serialized and never automatically replayed. An uncertain mutation retains exact immutable command bytes for explicit reconciliation. Enrollment retains no password: the user re-enters it to check the same command. A committed enrollment without recoverable credential offers fresh explicit step-up, subject to server quota. Disconnecting explicitly forgets local authority and makes no server-revocation or outcome-resolution claim.

Only one bounded bindings page is held at a time (50 requested, server may cap lower), with Next page and First page. Failed reads preserve the previous page with a warning; not-yet-loaded and empty are distinct. GET selection owns visible epoch/binding state; clearing retains its server epoch. Conflict requires refresh/review. Revoking names the exact conversation and confirms the consequence. Clear/revoke do not promise cancellation of independently granted children. This slice explicitly does not enable managed execution.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Form | Material Form/TextFormField | UX-CONTRACT.md | account password step-up / conversation title | app_control_screen_test.dart |
| Scrollbar | theme.dart and Material ListView | DESIGN.md | natural-height narrow and constrained wide surface | app_control_preview_test.dart |
| Toast | WorkspaceNotice | UX-CONTRACT.md | persistent info / success / warning | app_control_screen_test.dart |
| CRUD | AppControlClient and Material AlertDialog | backend app-control HTTP contract | bounded list / create / select / clear / revoke | app_control_test.dart and app_control_screen_test.dart |

App-control previews are actual Flutter test renders with bundled Sans, Mono and Material icon fonts and disposable fixture data. They are not browser screenshots or evidence of live server enrollment.
