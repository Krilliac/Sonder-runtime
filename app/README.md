# Sonder Runtime — mobile & desktop app

A cross-platform GUI for [Sonder Runtime](../README.md). One Flutter codebase
builds an **Android APK** and desktop apps for Windows, Linux, and macOS.

**Sonder Runtime is not a standalone foundation model.** It is the runtime and
orchestration layer: it selects an inference route and supplies prompts, memory,
tools, grounding, and policy. **Ollama is the local inference host** that loads
and runs the actual base-model weights. QLoRA/LoRA adapter weight training uses
the PEFT/Hugging Face toolchain, not Ollama; only validated adapters or merged
models are deployed to Ollama for inference.

The app talks to your own `sonder_serve.py` process over its OpenAI-compatible
HTTP API, so local model requests, memory, and lessons stay on the machine you
run by default. Explicitly selected cloud tiers and invoked web tools contact
their named external services.

Client-only platforms can also start, stop, and restart their configured host
through Sonder Runtime's bounded authenticated launcher. See
[Mobile host control](../MOBILE_HOST_CONTROL.md) for host setup and security.

```
  ┌────────────┐        HTTP /v1/chat/completions        ┌────────────────────┐
  │  this app  │  ───────────────────────────────────►   │   sonder_serve.py  │
  │  (phone /  │       Bearer <api key> (optional)        │ + orchestration     │
  │   desktop) │  ◄───────────────────────────────────   │ + Ollama inference │
  └────────────┘             assistant reply              └────────────────────┘
```

## Features

- **Chat UI** with saved local chats, a chat drawer, per-chat project names,
  and conversation memory (history is threaded to the server).
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
- **Slash commands** built in — `/stats`, `/context`, `/compact`, `/todo`,
  `/commands`, `/runtime`, `/mcp`, `/learning`, `/asset`, `/artifactcheck`, `/dump`, `/permissions`, `/train`, `/pass`, `/fail`, `/help` — handled
  by the serve layer exactly like the REPL.
- **Dark / light** themes, copy-to-clipboard, selectable text.
- Works against a LAN server, a VPS, or `127.0.0.1` when the server runs on the
  same desktop machine.
- If the configured hosted/LAN server cannot be reached, chat requests retry the
  local server at `http://127.0.0.1:11435` and the assistant response starts
  with a warning that local fallback was used.

## Download a pre-built app (no toolchain needed)

Every push builds all four platforms in CI. Grab a build without installing
anything:

1. Open the repo's **Actions → build-apps** and click the latest green run.
2. Download the artifact for your platform from the run's **Summary** page:
   - `sonder-runtime-android-apk` → `sonder-runtime-android.apk`
   - `sonder-runtime-linux-x64` → `sonder-runtime-linux-x64.tar.gz`
   - `sonder-runtime-windows-x64` → `sonder-runtime-windows-x64.zip`
   - `sonder-runtime-macos` → `sonder-runtime-macos.zip`

For **permanent download links**, push a tag and CI publishes a GitHub Release
with the four files attached:

```bash
git tag app-v1.0.0
git push origin app-v1.0.0
```

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
- **macOS** — unzip and open `Sonder Runtime.app` (right-click → Open the first time,
  since the build is unsigned).

## First run

1. Configure the host launcher by following
   [Mobile host control](../MOBILE_HOST_CONTROL.md), or start the server manually
   with `bash deploy_sonder.sh --serve`.
2. Open the app → **Settings** (gear icon).
3. Enter the **Server URL** and API key, plus the **Host launcher URL** and its
   separate token. Tap **Test connection**, then **Save**.
4. Optionally enable **Allow approximate IP location** for weather/nearby prompts.
   This contacts `ipwho.is`; VPN or ISP routing can report the wrong city.
5. Start chatting.

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

The repo commits only `lib/`, `pubspec.yaml` and `test/`. Generate the native
project scaffolding locally with `flutter create`, then build:

```bash
cd app
flutter create --org com.sonder.runtime --project-name sonder_runtime .
python ../scripts/configure_flutter_networking.py . --allow-android-cleartext
python ../scripts/package_local_system.py --out app/build/local-system --zip app/assets/local-system.zip
flutter pub get

flutter run                    # dev, on any connected device/desktop
flutter build apk --release    # Android → build/app/outputs/flutter-apk/
flutter build linux --release  # Linux   → build/linux/x64/release/bundle/
flutter build windows --release
flutter build macos --release
```

Requires the [Flutter SDK](https://docs.flutter.dev/get-started/install)
(stable channel). Android builds also need a JDK (17) and the Android SDK;
Linux desktop needs `libgtk-3-dev` and friends (see the workflow for the exact
package list).
