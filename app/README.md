# trilobite — mobile & desktop app

A cross-platform GUI client for [trilobite](../README.md). One Flutter
codebase builds an **Android APK** and **desktop apps** for Windows, Linux and
macOS. The app is a thin chat front-end: it talks to your own
`trilobite_serve.py` server over its OpenAI-compatible HTTP API, so your model,
memory and lessons stay on the machine *you* run — nothing goes to a third
party.

```
  ┌────────────┐        HTTP /v1/chat/completions        ┌────────────────────┐
  │  this app  │  ───────────────────────────────────►   │  trilobite_serve.py │
  │  (phone /  │       Bearer <api key> (optional)        │  + the local loop   │
  │   desktop) │  ◄───────────────────────────────────   │  + Ollama model     │
  └────────────┘             assistant reply              └────────────────────┘
```

## Features

- **Chat UI** with conversation memory (history is threaded to the server).
- **Model picker** in the title bar — switch which LLM answers per conversation
  (`trilobite` local student, or any cloud/paid tier the server exposes). The list is
  fetched from the server's `/v1/models`. Cloud models answer *clean* (teacher mode)
  and their good outcomes still feed the local model's learning.
- **Settings**: server URL + optional API key, with a one-tap *Test connection*.
- **Slash commands** built in — `/stats`, `/train`, `/pass`, `/fail`, `/help` —
  handled by the serve layer exactly like the REPL.
- **Dark / light** themes, copy-to-clipboard, selectable text.
- Works against a LAN server, a VPS, or `127.0.0.1` when the server runs on the
  same desktop machine.

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
4. Start chatting.

## Build it yourself

The repo commits only `lib/`, `pubspec.yaml` and `test/`. Generate the native
project scaffolding locally with `flutter create`, then build:

```bash
cd app
flutter create --org com.trilobite --project-name trilobite .
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
