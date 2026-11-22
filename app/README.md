# Sonder Runtime — mobile & desktop app

A cross-platform GUI for [Sonder Runtime](../README.md). One Flutter codebase
builds an **Android APK** and desktop apps for Windows, Linux, and macOS.

**Sonder Runtime is not a standalone foundation model.** It is the runtime and
orchestration layer: it selects an inference route and supplies prompts, memory,
tools, grounding, and policy. **Ollama is the local inference host** that loads
and runs the actual base-model weights. QLoRA/LoRA adapter weight training uses
the PEFT/Hugging Face toolchain, not Ollama; only validated adapters or merged
models are deployed to Ollama for inference.

The app talks to your own Sonder Runtime server (started with
`python -m sonder_runtime serve`; the HTTP adapter is
`sonder_runtime/interfaces/http/serve.py`, its lifecycle code
`sonder_runtime/adapters/web/lifecycle.py`) over its OpenAI-compatible
HTTP API, so local model requests, memory, and lessons stay on the machine you
run by default. Explicitly selected cloud tiers and invoked web tools contact
their named external services.

Client-only platforms can also start, stop, and restart their configured host
through Sonder Runtime's bounded authenticated launcher. See
[Mobile host control](../MOBILE_HOST_CONTROL.md) for host setup and security.

```
  ┌────────────┐        HTTP /v1/chat/completions        ┌────────────────────┐
  │  this app  │  ───────────────────────────────────►   │   sonder serve     │
  │  (phone /  │       Bearer <api key> (optional)        │ + orchestration     │
  │   desktop) │  ◄───────────────────────────────────   │ + Ollama inference │
  └────────────┘             assistant reply              └────────────────────┘
```

## Features

- **Chat UI** with saved local chats, a chat drawer, per-chat project names,
  and conversation memory (history is threaded to the server). Each completed
  turn preserves the server's content-free response receipt, usage totals,
  aggregate activity status, resolved model/tier, and cache result in a
  collapsed **Response details** panel. These diagnostics survive local chat
  history without being replayed to the model; local error bubbles are likewise
  excluded from later model-visible history.
- **Inference picker** in the title bar — select the `sonder` local route or a
  model/tier exposed by the server. The list comes from `/v1/models`. Grounded
  outcomes feed memory and training-data preparation; actual adapter-weight
  updates happen only in an explicit PEFT training run.
- **Settings**: server URL + optional API key, default inference route/model, optional hosted
  tiers opt-in, approximate IP-location opt-in, account register/login for hosted
  deployments, with a one-tap *Test connection*.
- **Cross-platform host controls**: the shared System page shows launcher and
  exact main-server identity and gives Android/iOS the same Start, Stop, and
  Restart controls as desktop without exposing a remote shell. Long first-run
  starts continue on the host as persistent operations; the app polls progress
  and resumes an active operation after returning to the page.
- **Grounded web/weather**: explicit current-web requests use visible tools;
  weather uses Open-Meteo. When approximate location is enabled, the app asks
  `ipwho.is` for a city/region only on location-dependent prompts, strips the raw
  IP, and labels the result as approximate.
- **Universal artifact shortcuts**: the command menu can generate verified Office
  suites plus synchronized AVI video, animated GIF, MIDI, SRT/WebVTT caption,
  EDL timeline media kits, and self-contained animated humanoid GLBs with a
  17-bone hierarchy, full morph frames, and sequenced clips locally without
  waiting for CI or downloading third-party creative assets.
- **System panel**: view server status, context health meters, master/subagent
  activity, the live workbench checklist and exact action evidence, visible task
  state, the shared local runtime-policy revision/model aliases/execution lanes,
  atomic MCP source/tool convergence and fail-closed refresh errors, permission
  rules, grounded outcome coverage, lesson provenance, distillation yield,
  memory-hygiene meters, command inventory, improvement
  recommendations, learning stats and exposed models, run `/stats`, `/context`,
  `/compact`, `/todo`, `/commands`, `/runtime`, `/mcp`, `/learning`, `/asset`, `/artifactcheck`, `/dump`, `/permissions`, `/quality`,
  `/inventory`, `/privacy`, `/embeddings`, `/improve`,
  `/agents`, `/capacity`, `/agentcancel`, `/agentretry`, `/train 10` (grounded
  practice, not a weight update) and `/help`, start/stop the bundled desktop server,
  launch the grounded practice loop, and pull updates from Git.
- **Live execution feed contract**: the existing `GET /v1/sonder/status` response may add
  `execution.feed` with schema v1 fields `schema_version`, `runtime_id`, `known`,
  `active_responses`, bounded `events`, `truncated`, `redaction_applied`,
  `oldest_seq`, `next_seq`, `dropped_events`, `sequence_gap`, `limits`, `error`,
  and `bytes`. Common event fields are `response_id`,
  `response_status`, `seq`, `ts`, `elapsed_ms`, `kind`, and `phase`; model,
  tool, file, and other events add their documented typed fields. Preview
  descriptors use `state` (`available`, `unavailable`, or `disabled`), `text`,
  `chars`, `truncated`, and `redacted`. The client honors the advertised event
  limit up to a hard maximum of 32, renders at most 12, and re-redacts/truncates
  preview text to 300 characters. It never infers this projection from raw
  activity or starts another poll; older servers show the feed as unavailable.
  Available `sequence_gap` or `dropped_events` metadata is authoritative; older
  payloads fall back to per-response sequence inference. Preview text is plain
  selectable text, never Markdown.
- **Persistent Autopilot workspace**: compose a high-level goal, choose guarded
  workspace or observe-only policy, enable/disable public web access, plan or run
  it with adaptive review or a static plan, then inspect its persisted success
  gates, task ledger, cycle/failure/replan budgets, evidence checkpoints, events,
  and evidence-backed end report. Active runs can be paused or cancelled;
  interrupted/paused work resumes only after an explicit tap.
- **Automatic execution decisions**: concrete developer work entered in chat is
  visibly routed to the foreground workbench, persistent Autopilot, or an
  explicitly requested hardware-bounded fleet. Ambiguous multi-stage work gets a
  local-only mode decision; questions and `no tools` requests stay ordinary chat.
- **Live footer**: chat shows context %, active agents, project scope, token
  estimates, selected model and latest agent activity while work is running.
- **Restart-safe fleets**: the System panel reads the shared private fleet ledger,
  shows interrupted work from any local Sonder Runtime process, and offers a confirmed
  local retry without silently replaying work after a crash.
- **Host developer tools** (Runtime > Host tools, administrators only): a
  read-only view of the server's host tool inventory (`GET /v1/tools/inventory`)
  grouped by category, with each tool's version or why no version was probed and
  its redacted path. A category picker narrows the list, and **Rediscover** asks
  the server to probe again (`POST /v1/tools/inventory/refresh`). The app has no
  view of build, fix or debug runs: the server offers no list route for them.
- **Slash commands** built in — `/stats`, `/context`, `/compact`, `/todo`,
  `/commands`, `/runtime`, `/mcp`, `/learning`, `/asset`, `/artifactcheck`, `/dump`, `/permissions`, `/train`, `/pass`, `/fail`, `/help` — handled
  by the serve layer exactly like the REPL.
- **Accessible command and progress UX** — command discovery searches names,
  aliases, tool names, usage, and parameters; rows publish full assistive labels.
  In-flight turns announce a live `Working...` status, and structured server
  errors retain bounded request IDs/codes/retry hints under **Error details**.
- **Agent conversation navigation** — the Agents surface keeps independent
  transcripts and drafts, exposes a shortcut guide, supports Escape and
  Alt+Arrow conversation navigation, and shows server-owned status, tier, and
  revision details. Per-lane resource counters remain explicitly unavailable;
  Runtime and `/capacity` provide the bounded cluster view.
- **Dark / light** themes, copy-to-clipboard, selectable text.
- Works against a LAN server, a VPS, or `127.0.0.1` when the server runs on the
  same desktop machine.
- If the configured hosted/LAN server cannot be reached, chat requests retry the
  local server at `http://127.0.0.1:11435` and the assistant response starts
  with a warning that local fallback was used.

## Sonder Inference and Observatory

**Runtime → Inference & Observatory** (rail item **Inference**) reads the
runtime's admin-only `GET /v1/sonder/ecosystem` (`sonder.runtime.ecosystem/1`)
on each Runtime refresh and shows:

- the provider bindings: the default generation provider, one chip per tier,
  and the embedding provider;
- Sonder Inference's state as a word: **ready**, **degraded**, **unavailable**,
  **unknown** or **not configured**, with its version, base URL, models and
  measured identity (backend, model, quantization, context; digests shortened,
  each with a copy button). A mock backend carries a **SYNTHETIC** chip: its
  output is not a quality or performance signal;
- the fallback: without one, requests fail while Sonder Inference is down;
  with `SONDER_INFERENCE_FALLBACK=ollama`, Ollama serves only requests that
  never reached Sonder Inference;
- the runtime's Observatory live export: on or off, subscribers, emitted,
  dropped and retained events, allowed origins and any warnings (such as a
  missing CORS origin).

A 401/403 reads "Administrator authorization is required."; a 404 means the
runtime does not report this status (an older build, or neither live export
nor provider status is available); a payload with another schema is shown as
unsupported instead of failing the page. Every state is spoken as text, never
by colour alone.

To bind Sonder Inference, set `SONDER_MODEL_BACKEND=sonder-inference` (or a
per-tier `SONDER_<TIER>_PROVIDER`), `SONDER_EMBEDDING_PROVIDER=ollama` and
`SONDER_INFERENCE_BASE_URL` on the runtime host, then restart Sonder Runtime.
The embedding provider must be set explicitly: it defaults to
`SONDER_MODEL_BACKEND`, and Sonder Inference serves no embeddings.

**Open Observatory** (desktop) starts Sonder Observatory with one
`--connect <url>` per URL the runtime published. The executable is, in order:
**Settings → Observatory executable**, then `SONDER_OBSERVATORY_BIN`, then
`sonder-observatory` on `PATH`. On macOS the path may be the Observatory's
`.app` bundle, started with `open -n -a <bundle> --args --connect …`. A
configured path that does not exist is reported, never skipped. Without an executable, a configured **Observatory web
URL** opens as `<url>?fixture=0&connect=<url>&connect=<url>` (URL-encoded)
with `xdg-open`, `open` or `cmd start`; the web URL is unset by default
(`http://127.0.0.1:4173/` is the Observatory's `npm run preview` address).
Browser, Android and iOS builds show that link to copy instead. **Copy connect
URLs** copies the list either way.

The app never passes a token or API key to the Observatory: not in its
arguments, not in the URL, and credential-like variables (`*TOKEN*`,
`*API_KEY*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`) are removed from the
environment it starts with. Connect URLs with credentials, a query or a
fragment are dropped. Launching is disabled while the app talks to a
non-loopback runtime, because producer telemetry is served on loopback on the
runtime host. Because no credential is passed, a launched Observatory reads
the runtime's admin-gated telemetry only in local-open mode on loopback;
otherwise it asks for a token itself, and the panel says so next to **Open
Observatory** whenever the app uses an API key or account session.

A hosted (HTTPS, non-loopback) Observatory web URL still opens in this
device's browser and connects to the loopback URLs above. Its origin must be
on the runtime's route-scoped exact-match `SONDER_OBSERVATORY_ORIGINS`
(`[observability].live_export_origins`) allowlist (and Sonder
Inference's `--cors-origin`), and browsers may block a public page from
reading loopback addresses.

## Download a pre-built app (no toolchain needed)

Every push to `main`, every pull request, and every manual `build-apps` run
builds all four platforms in CI. These are CI artifacts, not releases; GitHub
may require sign-in to download them and expires them according to repository
retention policy. Grab a recent build without installing anything:

1. Open the repo's **Actions → build-apps** and click the latest green run.
2. Download the artifact for your platform from the run's **Summary** page:
   - `sonder-runtime-android-apk` → `sonder-runtime-android.apk`
   - `sonder-runtime-linux-x64` → `sonder-runtime-linux-x64.tar.gz`
   - `sonder-runtime-windows-x64` → `sonder-runtime-windows-x64.zip`
   - `sonder-runtime-macos` → `sonder-runtime-macos.zip`

The README's `app-latest` links are a mutable prerelease snapshot and may lag
`main`. A permanent versioned release is published only from an
`app-vMAJOR.MINOR.PATCH` tag after the runtime, Flutter, tag, and full Git SHA
pass the release policy. This checkout is currently a development identity
(`0.9.0.dev0` runtime versus `1.0.0+1` Flutter) and is intentionally not
release-ready. Maintainers must follow the
[release version policy](../docs/runbooks/release-version-policy.md) and
[publication runbook](../docs/runbooks/publish-release.md); do not create a
release tag merely to obtain permanent links.

## Bundled system

Desktop downloads include a `local-system` folder beside the app. The System
panel can use that folder to set up the local runtime, start/stop the server,
launch grounded practice, run common status/training commands, and pull updates
from Git. Desktop app startup requests the bundled server automatically and app
shutdown stops only the process that app instance started, unless Settings has
**Keep local server running after app closes** enabled. It does not terminate an
independently managed server. With that option on, the server is launched in
background mode so it can keep serving headless after the GUI exits.

Windows, Linux, and macOS launchers prefer a sealed engine payload under
`local-system/engine/<platform>-<architecture>/`. Such a payload contains a
portable Python runtime with `mcp`, an Ollama runtime, and a complete model-store
subset. **Setup host runtime** verifies all declared sizes, SHA-256 hashes, Ollama
manifests, and referenced blobs, then runs without pip or model-registry access.
If no engine payload is included, the app reports **Host runtimes; downloads may
be needed** and retains the smaller installed-Python/Ollama fallback.

If an older app build's **Update from Git** button aborts because local files
would be overwritten, run `sonder-safe-update.cmd` from the bundled
`local-system` folder once. Newer app builds call that same safe updater from
the button.

Runtime state is shared outside the install folder. By default the bundled
server uses `%LOCALAPPDATA%\sonder` on Windows, `$XDG_DATA_HOME/sonder`
or `~/.local/share/sonder` on Linux, and the equivalent app data home on
macOS. Set `SONDER_HOME` to force every install/server to use a specific
shared memory folder.

The Flutter app uses the new `com.sonder.runtime` application identity and
`sonder_*` preference keys. Mobile operating systems therefore treat it as a
separate clean installation from any pre-rename build; configure its endpoint
and credentials before removing an older installation that still contains data
you need.

Android builds include the same payload as `local-system.zip` inside the APK.
Android cannot execute that Python/Ollama payload directly, so its System page
uses the authenticated launcher already running on the configured computer.

### Installing

- **Android** — copy `sonder-runtime-android.apk` to your phone and open it. You'll
  need to allow *"install unknown apps"* for your file manager/browser once.
  The APK is release-built and debug-signed, so it installs directly (it is not
  a Play Store upload).
- **Linux** — `tar xzf sonder-runtime-linux-x64.tar.gz && ./sonder`
- **Windows** — unzip and run `sonder.exe`.
- **macOS** — unzip and open `Sonder.app` (right-click → Open the first time,
  since the build is unsigned).

## First run

Checked against the code on 2026-11-20.

1. **Start a server the app can reach.**
   - Same desktop: the desktop app starts its bundled server by itself (see
     [Bundled system](#bundled-system)). To run a server by hand from a
     checkout, use a launcher or the module directly. It listens on
     `127.0.0.1:11435` by default.

     ```powershell
     .\sonder-serve.cmd
     # or, without the sonder_headless.py engine bootstrap step (the Ollama preflight still runs; add --skip-ollama to skip it):
     .\venv\Scripts\python.exe -m sonder_runtime serve
     ```

     ```bash
     ./sonder-serve.sh
     # or: ./venv/bin/python -m sonder_runtime serve
     ```

     Check it from a second terminal; expect a small JSON body whose `status` is `alive`:

     ```bash
     curl http://127.0.0.1:11435/live
     ```
   - Android or another remote client: use the
     [server-private installer](../docs/runbooks/install-server-private.md), a
     TLS reverse proxy ([secure remote access](../docs/runbooks/secure-remote-access.md),
     which covers Caddy and nginx), and [Mobile host control](../MOBILE_HOST_CONTROL.md).
     The loopback service must never be port-forwarded.
   - The old `deploy_sonder.sh --serve` route is not supported: the script
     needs root and writes loopback development systemd units, not the
     production remote-client setup; use the server-private installer above.
2. Open the app, then **Settings** (gear icon).
3. Enter the **Server URL** and API key, plus the **Host launcher URL** and its
   separate token. Tap **Test connection**, then **Save**. A launcher URL must
   be an `http(s)` origin without credentials or a path; a non-loopback
   launcher needs HTTPS and a token of at least 24 characters, or Settings
   reports the error and the launcher controls stay off
   (`app/lib/settings.dart`, `launcherConfigurationError`).
4. Optionally enable **Allow approximate IP location** for weather/nearby prompts.
   This contacts `ipwho.is`; VPN or ISP routing can report the wrong city.
5. Start chatting.

**First administrator.** On a server that uses accounts, the first account
registered over HTTP becomes the administrator only if it presents the
bootstrap secret the operator configured. The server does not generate or print
one: the operator sets `SONDER_BOOTSTRAP_SECRET` (at least 16 characters) in the
server's environment before the first registration, then types that value into
the app, which sends it in the `X-Sonder-Bootstrap-Secret` header. A missing or wrong
secret is refused with HTTP 403 and "first-admin bootstrap is not authorized".
Steps are in [Configure authentication](../docs/runbooks/configure-authentication.md).

### Connecting a phone to the PC

Which `Host` names the server accepts, and the exact `421 HOST_NOT_ALLOWED`
rules, are in
[HTTP API & lifecycle](../docs/wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed).
In short: any IP address works on any port, `localhost` and the PC's own names
work, and any other name (a Tailscale or proxy name) works whenever the server
requires an API key or accounts. Only an unauthenticated `local-open` server
refuses such a name with 421, which the app shows as *"The server at
mypc.local refused this address"*; the fix is to enter the PC's IP address or
add the name to `[server].allowed_hosts` / `SONDER_ALLOWED_HOSTS` on the PC
and restart the server.

Pick one of these, in order of simplicity:

- **Use the PC's IP behind your TLS proxy**, for example
  `https://192.0.2.10` on the proxy's HTTPS port 443 (a documentation address;
  use your PC's own). An IP address cannot get a publicly trusted certificate,
  so this works only where the client trusts your own CA, which Android apps
  do not by default and this app does not change. For a phone use a proxy name
  with a public certificate (next bullet). Never open or port-forward the
  runtime's own port 11435.
- **Use a proxy name** such as Tailscale Serve, Caddy or a Cloudflare tunnel.
  These terminate TLS, which account sign-in needs. For Caddy, see
  [secure remote access](../docs/runbooks/secure-remote-access.md#phones-and-other-clients-that-cannot-trust-a-private-ca)
  (a DNS-01 certificate for a name you own works on a private network); that page
  also explains `tls_terminated_by_proxy` and `trusted_proxy_cidrs`, which keep
  sign-in rate limits per device instead of per proxy.
- **USB or the Android emulator.** Run `adb reverse tcp:11435 tcp:11435` on
  the PC, then use `http://127.0.0.1:11435` in a development build. The
  emulator's `10.0.2.2` alias also works, as an IP literal, but needs the
  plain-HTTP key exception below.

**API key over plain HTTP.** The app sends the deployment API key only over
HTTPS, to a loopback host (`localhost`, `127.x.x.x`, `::1`), or to a plain-HTTP
`host:port` you have allowed
(`app/lib/api/transport.dart`, `CleartextKeyPolicy`). For any other plain-HTTP
server (for example `http://192.0.2.10:11435` or the `10.0.2.2` alias), tick
the checkbox **Send the API key to host:port over unencrypted HTTP**, which
appears under the server and key fields when the app would otherwise withhold
the key. Without the exception the key is not sent, and a server that requires
a key answers `401` (a local-open server needs none). Settings also has an
**API key over plain HTTP** section; it appears once at least one host is
allowed, and is the place to review hosts and turn one off to stop sending the
key there. Release Android builds also refuse plain `http://`
traffic altogether; see [Mobile host control](../MOBILE_HOST_CONTROL.md#transport-and-mobile-packaging)
for the development-only cleartext opt-in.

**Account sign-in** is stricter than the key: account passwords and sessions
need HTTPS, and plain HTTP is accepted only for the numeric loopback addresses
`127.x.x.x` and `::1` (which is what `adb reverse` gives a development build).
The name `localhost` does not count as loopback for sign-in.

## Build it yourself

From the repository root on Windows, the repo-local builder keeps Flutter under `.tooling/flutter`, so
subsequent builds reuse the SDK and package directly into `app/build` without
waiting for CI artifacts:

```powershell
powershell -NoProfile -File .\scripts\build_flutter_local.ps1 -Target windows
```

The command packages the current tracked local system, analyzes/tests the app,
builds Release, and places the runnable bundle at
`app\build\windows\x64\runner\Release\` with `local-system` beside it. The SDK
is cloned from Flutter stable only when `.tooling/flutter` is missing. When a
verified bundle already exists at
`app\build\engine-bundles\windows-x86_64`, the builder automatically reuses it
instead of reassembling or downloading the offline engine. Pass `-CodeOnly` to
intentionally omit that existing engine from a Windows rebuild.

To build the Windows app with a sealed offline engine from locally installed
`qwen2.5-coder:1.5b` and `nomic-embed-text`, use:

```powershell
powershell -NoProfile -File .\scripts\build_flutter_local.ps1 `
  -Target windows -AssembleOfflineEngine
```

This intentionally creates a multi-gigabyte local artifact. Build a reusable
bundle separately with `scripts\assemble_engine_bundle.py`, then pass its path
with `-EngineBundle` to avoid assembling it on every app build. The build keeps
the Flutter/Android `local-system.zip` code-only and attaches the large sealed
engine only to the desktop sibling folder, avoiding a duplicate embedded copy.

The repo commits only `lib/`, `test/`, `web/`, `pubspec.yaml`, the resolved
`pubspec.lock`, the pinned `.flutter-version` and the rendered app icons in
`assets/brand/` (regenerate them with `scripts/generate_app_icons.py` only
when the S mark changes). Generate the native project scaffolding locally with
`flutter create`, brand it, then build:

```bash
cd app
flutter create --org com.sonder.runtime --project-name sonder_runtime .
python ../scripts/install_app_branding.py .     # Sonder icons + display name
python ../scripts/configure_flutter_networking.py .
python ../scripts/package_local_system.py --out app/build/local-system --zip app/assets/local-system.zip
flutter pub get

flutter run                    # dev, on any connected device/desktop
flutter build apk --release    # Android → build/app/outputs/flutter-apk/
flutter build linux --release  # Linux   → build/linux/x64/release/bundle/
flutter build windows --release
flutter build macos --release
```

Android production builds deny cleartext traffic. The development-only
opt-in (`--allow-android-cleartext-for-development`, or
`-AllowAndroidCleartextForDevelopment` on the PowerShell helper) is documented in
[Mobile host control](../MOBILE_HOST_CONTROL.md#transport-and-mobile-packaging);
never use it for distributed APKs.

### Web build (served by the runtime)

`web/` is committed (unlike the other platform folders). Publish it for the
runtime to serve at `/app/` with `python scripts/build_web_app.py` from a
linked worktree (in a main checkout pass `--allow-main-checkout`); see
[Serving the web app](../docs/wiki/03-configuration.md#serving-the-web-app).
In a browser the app talks only to the origin that served it, keeps API keys
and account sessions in memory (a reload means signing in again), and stores
chat history in the browser's `localStorage` in plain text. Local-model and
launcher features are unavailable there.

Requires the [Flutter SDK](https://docs.flutter.dev/get-started/install)
at the stable version pinned in `app/.flutter-version` (3.47.5 on 2026-11-20; CI installs exactly
that version and runs `flutter pub get --enforce-lockfile`, so a dependency
change must update `pubspec.lock` in the same commit). Android builds also need a JDK (17) and the Android SDK;
Linux desktop needs `libgtk-3-dev` and friends (see the workflow for the exact
package list).

Account sessions use platform secure storage independently of the deployment API key. Login no longer replaces the deployment key, allowing servers configured with `auth_mode=both`. Sign out uses the exact-session `/v1/sonder/logout` endpoint; failed or unknown revocation offers retry or explicit local forgetting. A local forget cannot revoke a copied token. Account credentials are origin-bound, and authenticated API requests do not follow redirects. A changed server must be handled explicitly in Settings.

The Chat toolbar's **App control** action manages server conversation bindings through the dedicated `/v1/app-control` routes. Enrollment requires an explicit project and password check. Its credential stays only in memory and never enters chat, tools, preferences, secure storage or fallback requests. Account/server changes, expiry, disconnect and app closure clear it. Unknown outcomes retain the exact command for an explicit check; enrollment checks require the password again. Creating/selecting/clearing/revoking a binding does not enable managed execution. The server's app-control feature is disabled by default (`AppControlConfig.enabled = False`, `sonder_runtime/platform/app_control_config.py`). See [the interaction contract](UX-CONTRACT.md#app-control-conversation-bindings).
