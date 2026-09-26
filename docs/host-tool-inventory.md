# Host tool inventory

Sonder keeps an inventory of the developer tools installed on its host: compilers,
build systems, test runners, linters, debuggers, package managers, runtimes,
containers, version control, database clients, media and document tools, cloud
CLIs, editors, and shells. Agents and operators can see which tools exist, where
they were found, and which version a fixed probe reported. They do not have to
guess, or run `which` and `--version` by hand.

The inventory is read-only evidence. It never runs a caller-chosen command.

## What is recorded

Each tool has one record with these fields:

- name, category, and absolute path
- discovery source, and whether the tool is on `PATH`
- version text and version status
- up to four alternative installations
- up to eight metadata details, such as the MSVC toolset, `vcvars64.bat`, or the
  Windows SDK version

The host-owned registry lives in `sonder_runtime/domain/host_tools/registry.py`.
It lists every inventoried tool and the only argv that may be launched to learn
its version. It has 14 categories:

`compiler`, `build_system`, `test_runner`, `linter_formatter`,
`debugger_profiler`, `package_manager`, `runtime`, `container_vm`, `vcs`,
`db_client`, `media_doc`, `cloud_cli`, `editor_ide`, `shell`

Version statuses:

| Status | Meaning |
|---|---|
| `ok` | The fixed version probe exited 0; the version was parsed from its output |
| `from_metadata` | The version came from vswhere, the SDK directory, `py -0p`, `pkgutil`, `xcodebuild`, or an app bundle's `Info.plist` |
| `not_probed` | The registry defines no safe version query (see below), or a Windows batch launcher's arguments were not batch-safe |
| `deferred` | The discovery budget or the probe cap ran out before this probe |
| `timeout`, `failed`, `output_limit` | The bounded probe timed out, exited non-zero or could not start, or exceeded its output limit |
| `project_local_not_probed` | The path is inside a configured project file root, so it is never executed |
| `alias_not_probed` | A Windows `WindowsApps` execution alias, such as the Store `python.exe` stub, so it is never executed |

## Discovery sources per OS

Search order:

1. Absolute `PATH` entries, deduplicated. Relative entries, `.`, and entries
   containing NUL are skipped.
2. Platform extra directories.
3. Metadata discoverers.

| OS | Extra directories | Metadata (fixed argv only) |
|---|---|---|
| Linux | `~/.local/bin`, `~/.cargo/bin`, `~/go/bin`, `/usr/local/go/bin`, `~/.dotnet(/tools)`, `/snap/bin`, flatpak exports, `/opt/*/bin` (≤64), `~/.nvm/versions/node/*/bin` (≤16, newest first), `~/.pyenv/shims`, `~/.sdkman/candidates/*/current/bin` (≤32), `/usr/lib/jvm/*/bin` (≤16), linuxbrew, JetBrains Toolbox scripts | none |
| macOS | Homebrew `<prefix>/bin`, `<prefix>/sbin`, `<prefix>/opt/*/bin` (≤256) | `brew --prefix`; `xcode-select -p`; `pkgutil --pkg-info=com.apple.pkg.CLTools_Executables`; `xcodebuild -version` (only when the developer dir is inside `Xcode.app`); `/Applications` bundles read through `Info.plist` and never launched |
| Windows | scoop shims (`%SCOOP%` or `%USERPROFILE%\scoop`), Chocolatey `bin`, winget `Links`, `%ProgramFiles%\{Git\cmd,CMake\bin,nodejs,LLVM\bin,dotnet,Docker\...\bin}`, `%USERPROFILE%\.cargo\bin`, `%LOCALAPPDATA%\Programs\Python\Python3*` (≤8), MSYS2 | vswhere at its fixed Program Files path, run as `-products * -format json -utf8 -nologo`; registry `KitsRoot10` for the Windows SDK; App Paths (HKLM, then HKCU); `py -0p` |

Metadata sources on Windows:

- **vswhere** gives the Visual Studio installations. From each one, `cl.exe`,
  `link.exe`, `MSBuild.exe` and `devenv.exe` are found by file existence. The
  MSVC version comes from `Microsoft.VCToolsVersion.default.txt`. The
  compiler, linker and IDE are never launched.
- **The Windows SDK** gives the newest `bin\10.0.*` version, plus `rc`,
  `signtool`, and the SDK debuggers `cdb` and `windbg`.

## Never executed

These tools are recorded with presence and a status, and are never started:

- **GUI and IDE launchers:** `idea`, `pycharm`, `clion`, `rider`, `goland`,
  `devenv`, `code`, `cursor`, `subl`, `zed`, `blender`, `inkscape`.
- **Tools whose version query may contact the network, self-update, start a
  daemon, or download a toolchain:** `az`, `gcloud`, `firebase`, `heroku`,
  `vercel`, `netlify`, `flyctl`, `doctl`, `wrangler`, `bazelisk`, `sbt`,
  `pyright`, `multipass`, `scoop`, `nuget`.
- **Tools whose version comes from metadata:** `cl`, `link`, `nmake`, `wsl`,
  `windows-sdk`, `xcode`, `xcode-clt`.
- **Tools without a safe version switch:** `sh`, `cmd`, `powershell`, `gofmt`,
  `erl`, `sqlcmd`, `7z`, `nssm`.
- **Any path inside a project file root.** This is a binary a model could have
  planted.
- **A WindowsApps alias.**
- **A `.bat` or `.cmd` launcher whose arguments are not batch-safe.** Every
  argument must match `^[A-Za-z0-9_./:=,+@-]*$`, and the launcher path must not
  contain `%`, `!`, `^`, `&`, `|`, `<`, `>` or `"`.

Every other probe launches exactly `(<discovered path>, *version_args)` from the
registry:

- no shell
- stdin closed
- 3 s and 2,000 characters of output per probe
- whole process-tree termination on timeout or overflow
- at most 160 probes, 4 workers, and 30 s per discovery
- a neutral working directory (`/` on POSIX, `%SystemRoot%` on Windows), never
  the server's current directory. Many toolchains read project files from the
  current directory before printing a version: a `go.mod` `toolchain` line, a
  `.yarnrc` `yarnPath`, a `rust-toolchain.toml`, Maven's `.mvn/jvm.config`,
  and on Windows a `.cmd` shim resolving a bare command from the current
  directory. A project checkout as the working directory would let project
  files choose what a version probe runs.
- executables are looked up in one directory at a time, with a fixed
  Windows extension list (`.com`, `.exe`, `.bat`, `.cmd`). The current
  directory is never searched and the environment's `PATHEXT` is ignored.

The same guards apply to the metadata commands: a `brew`, `xcodebuild` or
`vswhere` inside a project file root is not run.

The probe environment is the scrubbed child environment (secrets, control
variables and `SONDER_*` removed), with these values pinned: `NO_COLOR=1`,
`TERM=dumb`, `CI=1`, `CHECKPOINT_DISABLE=1`, the .NET/Homebrew/npm/pip/gh
update-check and telemetry opt-outs, `GOTOOLCHAIN=local` (no toolchain
download), `NoDefaultCurrentDirectoryInExePath=1` (no current-directory
command search in `cmd.exe`), and the spec's own host-owned constants.
For example, terraform gets `CHECKPOINT_DISABLE=1`.

## Snapshot, TTL and refresh

- The snapshot is stored in `host-tools.json` in the state home (`SONDER_HOME`).
  It is written atomically with owner-only permissions and holds at most
  1 MiB and 512 tools.
- A snapshot is fresh for 24 hours. Discovery is lazy: it runs on the first
  request that needs a snapshot, never at startup composition.
- A refresh reuses a previous version when the tool's path and identity (file
  size and modification time) are unchanged. A `full` refresh re-probes
  everything.
- If a refresh fails, the previous snapshot is kept and gets the note
  `refresh failed: <Type>`.
- Loading tolerates damage: a missing, oversized, symlinked, corrupt or
  schema-invalid file is ignored.

The snapshot is never trusted for execution. A model may be able to write the
state home, so every lookup that could lead to a launch re-runs the host
executable guard (`adapters/host_tools/guards.py`). The path must be:

- absolute
- an existing regular executable
- not project-local
- not a WindowsApps alias

The structured test-run lane also calls `require_host_executable` again at
launch.

## Redaction

Every path shown to a model or returned over HTTP goes through `redact_path`:

- a workspace or file root becomes `[WORKSPACE]`
- the home directory becomes `~`
- a Windows `C:\Users\<name>` becomes `%USERPROFILE%`
- any remaining path segment equal to the user name becomes `<user>`

Version text passes through the runtime credential redactor. Notes and details
are redacted too. Every text field in a view is also reduced to printable
characters, so a tampered snapshot cannot carry line breaks or terminal
control sequences into model-visible output.

The model context line (below) contains no paths at all. The local REPL
operator sees full paths.

## Model context

Once a snapshot exists, `environment_probe.agent_brief()`, which is sent with
agent prompts, gains a suffix such as:

```text
 | capabilities: compilers: clang 18.1, gcc 13.3; build: cmake 3.28, make 4.3; tests: ctest 3.28, pytest 9.0; +33 more
```

The summary:

- is one line of at most 480 characters
- is built from the cached snapshot only; building it never discovers
- leaves out specialist tools such as `sccache`, `ccache`, `doxygen`, `xperf`
  and `wpaexporter`

## HTTP API

Both endpoints are admin-only. They return `Cache-Control: no-store` and admit
at most two requests at a time; a third concurrent request gets
`429 TOOL_INVENTORY_BUSY`.

| Request | Body | Result |
|---|---|---|
| `GET /v1/tools/inventory[?category=<category>][&name=<tool>]` | none; a body gets 400 | A redacted `tool_inventory` view; discovers only when no fresh snapshot exists |
| `POST /v1/tools/inventory/refresh` | `{}` or `{"full": true}`, at most 1 KiB | Forces rediscovery, then returns the view |

The view has these keys: `snapshot_digest`, `created_at`, `age_seconds`,
`stale`, `os`, `machine`, `counts`, `tools`, `filtered_by`, `notes` and
`truncated`. Each entry in `tools` has `name`, `category`, `version`,
`version_status`, `source`, `on_path`, `path`, `alternatives` and `details`.

Errors:

| Status | Code | When |
|---|---|---|
| 400 | `INVALID_TOOL_INVENTORY_QUERY` | Unknown or repeated query keys, `refresh` on GET, a category outside the enum, or a name outside `^[A-Za-z0-9+._-]{1,64}$` |
| 401 | — | Not authenticated |
| 403 | `FORBIDDEN` | Not an administrator |
| 429 | `TOOL_INVENTORY_BUSY` | Two inventory requests are already running |
| 413 | `TOOL_INVENTORY_TOO_LARGE` | The view is over 256 KiB; filter by category or name |
| 503 | `TOOL_INVENTORY_UNAVAILABLE` | The inventory is not composed in this runtime, or discovery failed with no previous snapshot |

The Flutter app reads these routes in Runtime > Host tools
(`app/lib/api/tools_inventory.dart`, `app/lib/runtime/host_tools_panel.dart`).
It loads the list only when the section's Details disclosure opens, shows 401
and 403 as "Needs an administrator account", shows 429 as a warning with
Retry, and on 413 asks for a category. Both routes get a 90 s client timeout,
because a GET also runs a full discovery on first use and after the refresh
window. The app's test fixtures
(`app/test/fixtures/server/tool_inventory_*.json`) come from this facade and
`serve.py`, and `tests/test_app_tool_inventory_fixtures.py` fails when they
drift from the wire format.

## Related surfaces

- `toolchain_status` and `/toolstatus` keep their exact behaviour. They now use
  the same packaged bounded runner (`adapters/host_tools/bounded_process.py`).
  The legacy `VERSION_ARGUMENTS` table must agree with the registry, and a
  drift test enforces that.
- The typed/native `tool_inventory` tool, REPL `/tools`, structured test runs,
  and the output digest are composed by the developer-tools lanes. See
  [the tools wiki page](wiki/10-tools-and-languages.md).

## Known limitations

- **The project-local guard ignores roots that are the filesystem root, the
  home directory, or an ancestor of home.** Treating them as project roots
  would mark every installed tool as planted. As a result, a home-wide file root
  is not defended: a binary planted anywhere under home and placed first on
  `PATH` would be probed with its fixed version argument.
- **Version text is evidence, not attestation.** Any executable can print any
  banner. A matching version does not prove what the binary is.
- **Discovery reads the environment of the runtime process.** A service started
  with a minimal `PATH` finds fewer tools until the extra directories cover
  them.
- **Discovery never runs the tools listed under "Never executed".** Their
  records can therefore show presence with no version.
