# trilobite — mobile & desktop app

A cross-platform GUI client for [trilobite](../README.md). One Flutter
codebase builds an **Android APK** and **desktop apps** for Windows, Linux and
macOS. The app is a thin chat front-end: it talks to your own
`trilobite_serve.py` server over its OpenAI-compatible HTTP API, so your model,
memory and lessons stay on the machine *you* run by default. Explicitly selected
cloud tiers and invoked web tools contact their named external services.

```
  ┌────────────┐        HTTP /v1/chat/completions        ┌────────────────────┐
  │  this app  │  ───────────────────────────────────►   │  trilobite_serve.py │
  │  (phone /  │       Bearer <api key> (optional)        │  + the local loop   │
  │   desktop) │  ◄───────────────────────────────────   │  + Ollama model     │
  └────────────┘             assistant reply              └────────────────────┘
```

## Features

- **Chat UI** with saved local chats, a chat drawer, per-chat project names,
  and conversation memory (history is threaded to the server).
- **Model picker** in the title bar — switch which LLM answers per conversation
  (`trilobite` local student, or any cloud/paid tier the server exposes). The list is
  fetched from the server's `/v1/models`. Cloud models answer *clean* (teacher mode)
  and their good outcomes still feed the local model's learning.
- **Settings**: server URL + optional API key, default model/tier, optional hosted
  tiers opt-in, approximate IP-location opt-in, account register/login for hosted
  deployments, with a one-tap *Test connection*.
- **Grounded web/weather**: explicit current-web requests use visible tools;
  weather uses Open-Meteo. When approximate location is enabled, the app asks
  `ipwho.is` for a city/region only on location-dependent prompts, strips the raw
  IP, and labels the result as approximate.
- **Universal artifact shortcuts**: the command menu can generate verified Office
  suites plus synchronized AVI video, animated GIF, MIDI, SRT/WebVTT caption,
  and EDL timeline media kits locally without waiting for CI or downloading
  third-party creative assets.
- **System panel**: view server status, context health meters, master/subagent
  activity, the live workbench checklist and exact action evidence, visible task
  state, the shared local runtime-policy revision/model aliases/execution lanes,
  atomic MCP source/tool convergence and fail-closed refresh errors, permission
  rules, grounded outcome coverage, lesson provenance, distillation yield,
  memory-hygiene meters, command inventory, improvement
  recommendations, learning stats and exposed models, run `/stats`, `/context`,
  `/compact`, `/todo`, `/commands`, `/runtime`, `/mcp`, `/learning`, `/asset`, `/artifactcheck`, `/dump`, `/permissions`, `/quality`,
  `/inventory`, `/privacy`, `/embeddings`, `/improve`,
  `/agents`, `/capacity`, `/agentcancel`, `/agentretry`, `/train 10` and `/help`, start/stop the bundled desktop server,
  launch endless training, and pull updates from Git.
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
  shows interrupted work from any local Trilobite process, and offers a confirmed
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
   - `trilobite-android-apk` → `trilobite-android.apk`
   - `trilobite-linux-x64` → `trilobite-linux-x64.tar.gz`
   - `trilobite-windows-x64` → `trilobite-windows-x64.zip`
   - `trilobite-macos` → `trilobite-macos.zip`

For **permanent download links**, push a tag and CI publishes a GitHub Release
with the four files attached:

```bash
git tag app-v1.0.0
git push origin app-v1.0.0
```

## Bundled system

Desktop downloads include a `local-system` folder beside the app. The System
panel can use that folder to set up the local engine, start/stop the server,
launch endless training, run common status/training commands and pull updates
from Git. Desktop app startup requests the bundled server automatically and app
shutdown requests all `trilobite_serve.py` instances to stop unless Settings
has **Keep local server running after app closes** enabled. With that option on,
the server is launched in background mode so it can keep serving headless after
the GUI exits.

If an older app build's **Update from Git** button aborts because local files
would be overwritten, run `trilobite-safe-update.cmd` from the bundled
`local-system` folder once. Newer app builds call that same safe updater from
the button.

Runtime state is shared outside the install folder. By default the bundled
server uses `%LOCALAPPDATA%\trilobite` on Windows, `$XDG_DATA_HOME/trilobite`
or `~/.local/share/trilobite` on Linux, and the equivalent app data home on
macOS. Set `TRILOBITE_HOME` to force every install/server to use a specific
shared memory folder.

Android builds include the same payload as `local-system.zip` inside the APK,
but Android still connects to a desktop or LAN server because it cannot launch
the Python/Ollama runtime directly.

### Installing

- **Android** — copy `trilobite-android.apk` to your phone and open it. You'll
  need to allow *"install unknown apps"* for your file manager/browser once.
  The APK is release-built and debug-signed, so it installs directly (it is not
  a Play Store upload).
- **Linux** — `tar xzf trilobite-linux-x64.tar.gz && ./trilobite`
- **Windows** — unzip and run `trilobite.exe`.
- **macOS** — unzip and open `trilobite.app` (right-click → Open the first time,
  since the build is unsigned).

## First run

1. Start the server on your machine: `bash deploy_trilobite.sh --serve`
   (prints the URL + API key), or `python trilobite_serve.py` for a local run.
   For phone access, bind it to your LAN: `TRILOBITE_HOST=0.0.0.0 python trilobite_serve.py`.
2. Open the app → **Settings** (gear icon).
3. Enter the **Server URL** (e.g. `http://192.168.1.20:11435`) and the **API
   key** if the server has auth enabled. Tap **Test connection**, then **Save**.
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
is cloned from Flutter stable only when `.tooling/flutter` is missing.

The repo commits only `lib/`, `pubspec.yaml` and `test/`. Generate the native
project scaffolding locally with `flutter create`, then build:

```bash
cd app
flutter create --org com.trilobite --project-name trilobite .
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
