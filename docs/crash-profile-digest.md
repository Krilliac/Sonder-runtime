# Crash and profile digests

Sonder turns crash captures and profiler output from native builds (MSVC,
clang, gcc; CMake, Ninja or MSBuild) into two small typed results:

- `CrashReport` (`sonder.crash_report/1`): exception, crashing thread and
  frames, modules with identity, cause hints, a stable signature.
- `ProfileDigest` (`sonder.profile_digest/1`): hot functions and paths, frame
  spikes, allocation hot spots.

The model, the REPL, HTTP and the fix hand-off only ever see these results.
Nobody sees raw debugger output or raw capture bytes. Every string copied out
of a capture (function, module, thread and annotation names) is labelled
"from the crashed process, untrusted".

Related pages:
- Structured test runs, `docs/structured-test-runs.md` (`/test`, `test_run`)
- [Build diagnostics and output digest](build-diagnostics-digest.md) (`/digest`)
- Host tool inventory, `docs/host-tool-inventory.md` (`/tools`)
- [Tools & Languages](wiki/10-tools-and-languages.md)

## Two tiers

| Tier | What runs | Examples |
|---|---|---|
| 0, pure | Python readers only; no process is launched | minidumps (Windows, Breakpad, Crashpad), ELF cores, ASan/UBSan/TSan text, valgrind memcheck XML, macOS `.ips`; callgrind, Chrome trace JSON, Tracy/WPA/PIX/Superluminal CSV, `heaptrack_print` and `perf report` text |
| 1, host tools | a durable, permission-gated job built from a host-owned argv template | cdb, gdb, lldb, eu-stack, rust `minidump-stackwalk` (with a `dump_syms` pre-step), `llvm-symbolizer`; `perf report`, `heaptrack_print`, `tracy-csvexport`, xperf |

Tier 0 always runs first. Tier-1 frames are merged over it (debugger frames
win); module identity and versions always come from the pure reader.

## Console commands

```
/crash <dump|core|log|dir> [--exe P] [--sym DIR]... [--engine E] [--repro NAME] [--symbols-online]
/crash triage <path|dir>      pure read; a folder of up to 64 captures is bucketed by signature
/crash symbols on|off         symbol-server consent for this console session
/crash fix <run_id|last>      fatal diagnostics, local source excerpt, repro test
/crash status|result|cancel <run_id>
/profile <capture> [--exe P] [--budget MS] [--top N] [--thread T] [--frame-zone Z]
/profile status|result|cancel <run_id>
```

Plain language reaches the same commands for whole-turn requests that name a
capture file: "analyze the crash dump dumps/game.dmp" runs `/crash`,
"summarize the trace out.json" runs `/profile`, and "fix that crash" runs
`/crash fix last`. A broader request ("profile startup", "analyze the crash
dump x.dmp and fix the bug") goes to the agent instead.

Ctrl+C while a run is being waited on cancels it; the job's process tree is
killed.

`/crash` and `/profile` are graded as execution (`crash_digest`,
`profile_capture_digest`): they ask in manual mode and are refused in plan
mode. Pure formats never start a job.

### Model tools

| Tool | Grade | Notes |
|---|---|---|
| `crash_triage` | safe | pure; a directory gives the bucket table |
| `crash_digest` | execution | `symbol_server=true` from a model call is always refused (`SYMBOL_SERVER_NEEDS_CONSOLE`) |
| `profile_digest` | safe | pure formats only; a binary capture gives `CAPTURE_NEEDS_HOST_TOOL` |
| `profile_capture_digest` | execution | host profiler run |
| `debug_run_result` | safe | owner-checked; no cancel |

### HTTP (admin only)

The routes use the same admin guard as `/v1/tools/inventory`:

- `POST /v1/tools/crash-triage`
- `POST /v1/tools/crash-digest`
- `POST /v1/tools/profile-digest`
- `POST /v1/tools/profile-capture-digest`
- `GET /v1/tools/debug-runs/<run_id>[?wait_seconds=N]`
- `POST /v1/tools/debug-runs/<run_id>/cancel`

Non-admin callers get 403. `symbol_server=true` gets 403
`SYMBOL_SERVER_NEEDS_CONSOLE`. Another principal's run gets 404
`JOB_NOT_FOUND`. `crash-digest` and `profile-capture-digest` launch host
tools, so the permission modes grade each call first, as they grade an
unattended typed-gateway call from HTTP (the same plan binding, deny rules and
`plan` mode). A refusal is 403 `PERMISSION_DENIED` with the `call_id` an
operator can approve once. Payloads are compact JSON of at most 48,000 bytes; failures
carry `{"ok": false, "error_code": ...}`.

## Windows setup (MSVC)

1. Install **Debugging Tools for Windows** from the Windows SDK installer.
   Sonder uses `cdb.exe` from `<KitsRoot10>\Debuggers\<x64|arm64|x86>`, picked
   by host architecture. The WinDbg Store app under `WindowsApps` is refused.
2. Optional: the **Windows Performance Toolkit** (`xperf`, `wpr`,
   `wpaexporter`) for ETL captures. xperf support is experimental.
3. Run `/tools refresh` and check that `cdb` is listed.
4. Point `--sym` at your local PDB directories, for example the build tree
   `out\build\x64-RelWithDebInfo`. Up to 8 directories. Mapped network drives
   and UNC paths are refused as symbol directories.
5. Studio symbol shares: set `SONDER_SYMBOL_STORES` to a `|`-separated list of
   at most 4 stores. Each is an `https://` URL (no port, or 443) or a UNC
   symstore share. Stores come from configuration only, never from a tool
   argument.
6. Symbol-server downloads (the Microsoft server and your stores) need all of:
   - the attended console (`/crash ... --symbols-online`);
   - consent: `SONDER_SYMBOL_SERVER_CONSENT=1`, or `/crash symbols on` for this
     session;
   - a "y" to the exact resolved command, which is shown first (engines,
     argv with placeholders, stores, input sha256);
   - a permission mode other than plan or readonly.

   The same request from a model, MCP, HTTP, a fleet or autopilot is refused.

What cdb is told, and why:
- `-sflags 0x022802B7`: never probe the CodeView or image paths recorded in the
  dump (they can be UNC, which is SMB egress), no prompts, deferred loads,
  line info. `LOAD_ANYTHING` is not set, so a PDB from a different build shows
  as `symbols=mismatch` instead of wrong frames.
- The image path is only your `--sym` directories.
- Managed dumps (clr.dll, coreclr.dll or mscorwks.dll loaded) are refused for
  cdb (`ENGINE_REFUSED_MANAGED_DUMP`): dbgeng would load a DAC, which is code.

Build-agent source paths such as `C:\agent\_work\N\s\src\...` are mapped onto
your local checkout by bounded suffix matching, so frames and the fix brief
point at local files.

## Linux and macOS

- ELF cores: gdb, lldb or eu-stack (`apt-get install -y elfutils`).
- Windows minidumps on Linux: install the rust walker and symbol dumper, then
  refresh the inventory:

  ```sh
  cargo install minidump-stackwalk dump_syms
  ```

  `dump_syms` runs once per GUID-verified module; `minidump-stackwalk --json`
  walks with CFI from those symbols. `--symbols-url` is never passed.
- `llvm-symbolizer` is used only on PE+PDB pairs whose GUID and age were
  verified in Python (staged side by side so its exe-directory lookup wins),
  or on ELF files whose build-id matches the capture. It always gets
  `--no-debuginfod` and `DEBUGINFOD_URLS=""`.
- Profiling: `perf report` needs `perf record -e cpu-clock -g` in VMs without
  a PMU; `apt-get install -y heaptrack` for `heaptrack_print`.
- On Linux each Tier-1 step runs in a no-network user namespace
  (`unshare --user --map-current-user --net --`) when the inventory has
  `unshare` and a one-time probe passes.

## Platform matrix

| Platform | Pure (Tier 0) | Host engines | Egress isolation |
|---|---|---|---|
| Windows | all crash and profile text formats; PE/PDB identity | cdb, llvm-symbolizer, minidump-stackwalk, tracy-csvexport, xperf (experimental) | none (flags, symopt and environment only) |
| Linux | all, including Windows minidumps (module+offset signatures) | gdb, lldb, eu-stack, minidump-stackwalk, llvm-symbolizer, perf, heaptrack_print | netns |
| macOS | all, including `.ips` | lldb | none |

Refused everywhere: native Perfetto protobuf (convert with `traceconv json`),
ETL outside Windows (export CSV with WPA), and any symbol download without
console consent.

## Receipts

Every host run records a receipt: input label and sha256, engines, tool path
identities, the argv with `{nonce}`/`{rundir}` placeholders (redacted), network
on or off, the symbol stores shown to the operator, `egress_isolation`
(`netns`, `none` or `n/a`), staging mode, and output-limit or truncation flags.
The command digest covers the placeholder argv, tool identity, input sha256,
engines and the network flag, so what you approved is what runs.

Staged inputs, symbol staging and tool caches are deleted when the run ends;
only `result.json` is kept (the last 32 runs, under the state directory).

## Crash to fix

`/crash fix <run_id|last>` prints a brief:

- one GNU-style line per mapped in-project frame of the crashing thread:
  `src/game/player.cpp:42:7: fatal error: EXCEPTION_ACCESS_VIOLATION read 0x0 in Player::update [null_deref] (from the crashed process, untrusted) [CRASH:EXCEPTION_ACCESS_VIOLATION]`.
  The build error counter and the diagnostics parser read these lines like
  compiler output (severity fatal, same file and line);
- a source excerpt of at most 40 lines from your local checkout, read through
  the guarded file reader and labelled untrusted;
- a repro test: the `--repro NAME` you gave (checked with the ctest selector
  grammar), or else the newest of your own `/test` reports whose crashed test
  binary is the crashed process.

Nothing is launched to find a repro. You (or the model) fix the code and run
the existing build and test tools.

When strategy tracing is on, each later console `/test` of exactly that repro
(same runner and selector), run in the same checkout `/crash fix` ran in
(the same workspace root), that finishes as passed or failed is recorded as
one attempt of this crash in the strategy trace, with the metric
`crash_reproduced` (1 while the repro still crashes, 0 once it passes,
minimized) and the handoff's `CRASH_REPRODUCED` failure while it still
crashes. The console prints one line such as
`crash repro passes: crash_reproduced 1 -> 0 (attempt 2)`. Attempts are
numbered from the trace's own history of this crash in this checkout, so a
restarted console continues the run; at most 12 are recorded. A run that
errored, found no tests, timed out or was cancelled records nothing, and
with tracing off nothing is recorded or shown. A `/test` of the same test in
another checkout records nothing, since it says nothing about this crash.
Only the console `/test` path
records attempts; a test run through another surface does not.

## Never done

- No CMake File API query files are written; the source tree is never written.
- No `vcvars` or other `.bat`/`.cmd` file is executed; the tool environment is
  built from scratch.
- The crashed program is never executed; it is only a debugger argument.
- No locals: no `bt full`, no `kP`, no variable values.
- No debugger extension, script or init file is loaded from the capture's
  directory (`-nx`, `--no-lldbinit`, `set auto-load off`, no `.load`).
- No `perf script`, lldb `script` or heaptrack scripts.
