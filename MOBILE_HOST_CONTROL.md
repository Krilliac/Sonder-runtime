# Mobile host control

The Flutter app now uses the same System page on desktop, Android, iOS, and
other client-only builds. A phone cannot create a process on a powered-off or
listener-free computer, so the host runs a small independent launcher on port
`11436`. The launcher can perform only four fixed operations: status, start,
stop, and restart of `trilobite_headless.py`. It cannot accept a command,
executable path, shell argument, setup request, Git update, or training request.

The main API remains on port `11435`. Use a different strong credential for
each service:

- `TRILOBITE_API_KEY` authenticates chat and System API requests.
- `TRILOBITE_LAUNCHER_TOKEN` authenticates host process control.

Both must contain at least 24 characters for a LAN bind.

## Windows host setup

Run these commands in a terminal opened in the repository. Generate and store
two different values; do not paste them into source files:

```bat
py trilobite_launcher.py --generate-token
setx TRILOBITE_LAUNCHER_TOKEN "PASTE_THE_LAUNCHER_TOKEN"
setx TRILOBITE_API_KEY "PASTE_A_DIFFERENT_MAIN_API_KEY"
setx TRILOBITE_AUTH_MODE "api-key"
trilobite-launcher-autostart.cmd
```

Sign out and back in so the user environment is refreshed, or set the same
variables in the current terminal for the first run. Then launch:

```bat
trilobite-launcher.cmd --host 0.0.0.0
```

The autostart installer creates a per-user Startup entry. It does not copy the
token into that entry. Remove it with:

```bat
trilobite-launcher-autostart.cmd uninstall
```

Open TCP ports `11435` and `11436` in the host firewall only for the trusted
private network or VPN. Trilobite does not change the operating-system firewall
automatically.

## Linux or macOS host setup

Set the same three environment variables in the account that will run the
launcher, then run:

```sh
TRILOBITE_LAUNCHER_HOST=0.0.0.0 ./trilobite-launcher.sh
```

Use the operating system's normal per-user service manager to start this script
at login. Keep the environment file readable only by that user. The launcher
also accepts `--cert` and `--key`, or `TRILOBITE_LAUNCHER_CERT` and
`TRILOBITE_LAUNCHER_KEY`, for TLS.

## App setup

In **Settings → Connection**, enter:

1. Main server URL, such as `http://192.168.1.20:11435`.
2. Main API key.
3. Host launcher URL, such as `http://192.168.1.20:11436`.
4. Host launcher token.

Save, open **System**, and verify that **Host Launcher** says `ready`. The
Start, Stop, and Restart controls then operate the host. Setup engine, Git
updates, and local training stay disabled on client-only devices because those
operations require direct access to host files.

## Transport and mobile packaging

HTTPS is recommended because bearer credentials sent over plain HTTP can be
observed by other devices on the network. For an intentionally trusted LAN or
VPN, CI explicitly enables Android cleartext access so existing `http://` host
URLs work. Local builds make that choice after generating the native project:

```sh
flutter create --org com.trilobite --project-name trilobite .
python ../scripts/configure_flutter_networking.py . --allow-android-cleartext
```

Omit `--allow-android-cleartext` for an HTTPS-only Android build. The same
script adds Apple's local-network usage explanation when an iOS or macOS native
project exists. Android's generated manifest already includes the required
Internet permission.

The launcher does not implement wake-on-LAN. The computer must be powered on
and the launcher service must already be running. For access away from home,
prefer a private VPN or authenticated HTTPS reverse proxy rather than exposing
either port directly to the public Internet.

## Diagnostics

From the host:

```sh
python trilobite_launcher.py --host 127.0.0.1
```

From another trusted machine, replace the token and host:

```sh
curl -H "Authorization: Bearer LAUNCHER_TOKEN" \
  http://HOST:11436/v1/launcher/status
```

If the launcher is reachable but server startup fails, inspect the launcher
response and the main server log under Trilobite's per-user `run` directory.
The usual cause is a missing model/runtime or a LAN main-server bind without a
strong `TRILOBITE_API_KEY`.
