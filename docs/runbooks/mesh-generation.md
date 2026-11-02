# Mesh generation (`mesh_generate`)

`mesh_generate` turns a text brief into a low-poly 3D model (GLB or FBX) by letting a model
write Blender build scripts and checking them against renders. It is a closed loop over
several candidates:

1. A model writes a Blender Python script (`def build():`).
2. A safety gate (AST check) screens the script. A rejected script goes back to the model with
   the reasons; that costs a round.
3. Headless Blender builds the script, joins the parts into one mesh, scales it (when
   `height_m` is set), stands it on the ground, counts triangles, exports it, and renders a
   2x2 contact sheet (front from -Y, right from +X, three-quarter, top) plus the three-quarter
   view alone.
4. A build error, or a triangle count over `max_tris`, goes back to the model as a repair prompt.
5. A vision model scores the sheet (0-10) and lists concrete issues. Below `target_score` the
   score and issues go back to the model as a revision prompt; at or above it the candidate stops.
6. Candidates are ranked: built, then within budget, then vision score, then fewer triangles.
   The best one wins and is copied to `<output_dir>/<name>.glb` (or `.fbx`), `<name>_sheet.png`,
   `<name>_view.png` and `<name>.py`.

Candidates rotate through `tiers` (default `general,code`), so a run mixes models.

## Requirements

- **Blender 4 or 5** on the machine that runs the server. Set `SONDER_BLENDER` to the full path
  of `blender.exe` (or put `blender` on `PATH`). Without it the tool returns an error payload
  naming `SONDER_BLENDER`; nothing else runs.
- **A vision model** for the critique step. It is optional: without one the loop still builds
  and repairs, but cannot revise for looks, and the report says `"vision": "unavailable: ..."`.
  Enable it with `/runtime set vision=qwen3-vl:8b` (the vision tier needs a native context
  large enough for one image; see `/runtime status`).

## Using it

```text
mesh_generate(brief="a low-poly wooden handcart with two wheels", max_tris=1500, height_m=1.2)
```

Useful arguments: `candidates` (1-6, default 3), `rounds` (1-6, default 3), `tiers`, `target_score`,
`export` (`glb` or `fbx`), `kit_path` and `api_doc_path`, `output_dir`, `timeout`, `vision`.

- `kit_path` names a Python helper module that candidate scripts may `import` (its directory
  is put on Blender's `sys.path`). Without `api_doc_path` the prompt carries a digest of the
  kit's exposed functions and first docstring lines. Which kit names are exposed is a policy, see
  "Kit exposure" below.
- Output goes under the asset workspace (`artifacts/generated/meshgen/<name>-<UTC stamp>` by
  default; `output_dir` must stay inside `SONDER_ASSET_WORKSPACE`/the checkout). Each round of each
  candidate keeps its script, model and renders in `c<candidate>_r<round>/`.
- The result is compact JSON: the winner (paths, triangle count, size, score), per-candidate
  attempts (status, triangles, score, issues, seconds) and the `vision` state. Code bodies are
  never returned; the script paths are.

## Budgets and quality

`max_tris` is a target to use, not just a ceiling: the prompt asks for roughly 60-90% of it, with
bevels on hard edges, enough segments that round parts read round (about 10 below 500 triangles,
12 below 2000, 16 below 6000, 24 below 20000, 32 above) and smooth shading on organic parts. Any
modifier type is allowed (including `NODES`); the harness bakes them and counts triangles after
modifiers, and literal counts such as subdivision `levels` are capped. The default is
3000. For hero assets pass 8000-20000 or more (the clamp is 50..200000); generation allows 8192 reply
tokens. An over-budget result is repaired with the biggest parts named. The critic also flags blocky,
low-effort results when the budget allowed more.

If a candidate repeats the same build error, or the same top critique issue with the same score and
about the same triangle count, the next prompt says so explicitly (placed before the closing
"Return ONLY ..." line) and, with more than one tier, the remaining rounds of that candidate move to
a different tier, preferring one that candidate has not used yet. The winner is credited with the
tier that produced its version, and each candidate reports `tiers_used`.

## Safety model

`mesh_generate` runs **model-written Python**. It is graded as an execution tool, the same
trust level as `run_code`, and asks or is denied in the same permission modes.

### Threat model: three layers, each with one job

| Layer | Job |
| --- | --- |
| **OS sandbox** (`sandbox_win.py`: zero-capability AppContainer, Job Object, private desktop) | **files, network, processes and the screen.** Model-written code can read and write only its run directory (and read the Blender install), has no network and cannot start a process. |
| **Secret guard** (`domain/meshgen/secret_guard.py`) | **key egress.** The model is local, so the only way a secret can leave the machine is text returned from a build in the tool result (which goes to the Claude API) or written to a log or file. Every such string and every exported model is checked; a match fails closed. |
| **AST gate** (`script_safety.py`) and harness builtins | **secret access and DoS.** Stops a script from reaching the registry or the environment (`winreg`, `ctypes`, `os`, `sys`, `subprocess` ..., and every route to unrestricted Python: text blocks, script operators, drivers, `eval`-style reflection) and from asking for absurd literal sizes. It is a first filter that turns mistakes into repair feedback, not a boundary against a determined script. |

**Known gap: the registry stays readable.** Every AppContainer is in ALL APPLICATION PACKAGES, which the
user's HKCU keys (for example `HKCU\Environment` and `RecentDocs`) grant read, so a script that gets past
the gate can read them. Closing it was tried and abandoned: LPAC (opting out of that group) makes Blender
exit with 0xC0000022 because side-by-side manifest activation needs `HKLM\...\SideBySide`, which grants
only that group and the `registryRead` capability, and with that capability added rendering crashes (no
GPU/OpenGL access) while HKCU is still readable through the same capability; a DENY ACE for the package
SID has no effect. `preflight` measures it (`registry: readable` plus a warning) and the strict-xfail test
`test_hkcu_environment_and_recentdocs_are_unreadable_from_the_sandbox` records it. The compensating control
is the secret guard below, which makes the egress path fail closed instead.

#### Secret guard

The guard builds a secret set on the host from (a) the server's `os.environ` and (b) `HKCU\Environment`
(read with `winreg`; names and values are never logged or put in an error). An entry counts when its NAME
contains KEY, TOKEN, SECRET, PASS, PWD, AUTH, CRED, SESSION, COOKIE or PRIVATE, or its VALUE has the shape
of a credential; values under 12 characters are ignored (and the shell's `PWD`/`OLDPWD`, which hold a
path, are not treated as secrets). Each secret is also matched as base64 (all three byte alignments,
standard and URL-safe), hex, reversed, URL-encoded and lowercased. Independently of the set, text with a
credential shape is a hit: `sk-ant-`, `sk-`, `ghp_`/`gho_`/`github_pat_`, `AKIA...`, `xox[bpas]-`, `AIza...`,
`-----BEGIN`, JWTs (`eyJ...`), `hf_...` and 32+ character mixed-case high-entropy tokens (hex ids and
plain lowercase slugs are not flagged).

It is applied to everything that leaves a build toward the host, the tool payload or a log: the result
JSON fields, error strings, stdout/stderr excerpts (judged before truncation), part, object, mesh and
material names, the critic's text, the model script itself, and the string metadata of the exported
GLB (the whole glTF JSON chunk: names, extras, asset) or FBX (printable runs). Vertex data is not scanned.
On a match the field becomes `[REDACTED:secret]`, the candidate gets `secret_leak_blocked: true`, its
exported files are discarded (never copied out of the run directory), the repair prompt tells the model
why, and the report gets a top-level `security` note. Neither the matched name nor the value appears
anywhere. A final pass scrubs the whole tool payload, which carries only structured fields (status, score,
tris, size, tier, round, critic issues, an error class plus a guarded, capped message); raw stderr goes only
into the local repair prompt, after the guard. If the guard cannot be built (for example the registry
environment cannot be read) `mesh_generate` refuses to run.

**Authorising a name.** `SONDER_MESHGEN_SECRET_ALLOW` is a comma-separated list of variable NAMES whose
values (in every encoding) are exempt, for example a token a particular kit legitimately prints. It is
empty by default and nothing is exempt unless listed. Only the environment variable is supported;
there is no permission-rule form of this authorisation.

**Residual risks.** Secrets embedded in vertex data stay on this machine (they are neither scanned nor
returned). The guard is pattern matching, so there is no guarantee against exotic encodings (a custom
cipher, a secret split across several fields, text rendered into an image). A secret the guard does not
know and that has no credential shape (a short password) is not detected.

Inside the sandbox the process can read and write only its own per-build run directory and read the
Blender install directory. User files, repositories, `.ssh` and credential files on disk are unreadable; there is
no network (loopback included); no second process can be started (`ActiveProcessLimit=1`); the job
has a memory limit (default 4096 MB) and is killed when its handle closes. The process is created
suspended and resumed only after `AssignProcessToJobObject` succeeded; if that or any other setup
step fails the build is refused. **Fail closed**: there is no silent fallback to running unsandboxed.

Verified with real Blender 5.2 in `tests/test_meshgen_integration.py`, each with an unsandboxed
control showing the probe really would succeed: reading a canary file outside the run dir, a TCP
connection to a loopback listener, and `wm.obj_import` of a canary file all fail in the sandbox; the
Lanternfall `lf_kit.box` builds, exports and renders through it.

Further isolation, each verified with real Blender and an unsandboxed control
(`tests/test_meshgen_sandbox_live.py`): writes outside the run dir, process spawn, symlink, junction
and hardlink escapes, loopback and external TCP, DNS, and screen capture (the build runs on a private
desktop) all fail in the sandbox. Two concurrent builds use different AppContainer profiles (three
slots, a cross-process mutex per slot) and cannot open each other's processes; the AppContainer's
own storage is wiped before every build so nothing persists between builds.

Probed and not fixable from inside this design (see also the registry gap above): `GetAsyncKeyState` returned 0 in both modes with no
key pressed (inconclusive; the private desktop is the mitigation); the POSIX `prlimit` path was not
run on this machine.

What the sandbox does not claim: it does not stop CPU use until the timeout, nor a Windows
AppContainer escape; the opt-out and non-Windows platforms have no sandbox at all.

**ACL footprint** (everything the sandbox changes on the machine, and how to undo it):

- three AppContainer profiles `Sonder.MeshGen.s0`..`s2` for the current user (created on first use);
- a private desktop per build in the current window station (closed afterwards);
- one persistent grant per profile, added once and only if missing: `(OI)(CI)(RX)` for that profile's SID on the
  Blender install directory (validated first: symlinks and shims are resolved, and the home directory,
  its ancestors and descendants, drive roots and any directory containing the kit are refused). Undo: `icacls "<blender dir>" /remove:g *<SID>` (the SID is printed by
  `icacls "<blender dir>"`; the profile itself can be removed with `DeleteAppContainerProfile`);
- per build, a fresh run directory `<candidate dir>/.run-<id>` with a `(OI)(CI)(M)` grant that is
  removed, and the directory deleted, when the build ends (also on failure/timeout). Builds run in
  parallel, each with its own run directory, grant, job and handles.

No grant is ever placed on the kit's source directory, the repository or any user directory; a run
directory that would overlap the kit's directory (or contain the user's home) is refused. The kit
file, the harness and the script are *copied* into the run directory (only the single kit file is
copied, so a kit that imports sibling modules will not work in the sandbox). Outputs are copied back
to the candidate directory afterwards.

The sandboxed process gets a minimal environment: `SYSTEMROOT`/`WINDIR`, a `PATH` of the Blender and
system directories, `TEMP`/`TMP`/`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` and
`BLENDER_USER_{CONFIG,SCRIPTS,DATAFILES,EXTENSIONS}` all inside the run directory, and nothing from the
server's environment. (Blender 5.2 has no `--temp-dir` option; it honours `TEMP`, which points into
the run directory.)

Environment variables:

| Variable | Meaning |
| --- | --- |
| `SONDER_MESHGEN_MEM_MB` | job-wide memory limit in MB (default 4096, clamped 256..65536); a build that hits it is reported as `memory limit exceeded (N MB)` |
| `SONDER_MESHGEN_UNSANDBOXED=1` | explicit opt-out: run Blender with the server's authority. The report then carries `sandbox: "unsandboxed"` and a `warning`; use only where `run_code` would be acceptable |
| `SONDER_MESHGEN_SECRET_ALLOW` | comma-separated variable NAMES exempt from the secret guard (default empty; see "Secret guard") |
| `SONDER_BLENDER` | path to `blender.exe` |

If the sandbox cannot be set up (for example the Blender grant needs an account that may edit its
ACL, run the printed `icacls ... /grant` once) `mesh_generate` returns an error naming the cause and
the opt-out; it never starts a build first. Non-Windows hosts have no sandbox here: they fail closed
and run only with `SONDER_MESHGEN_UNSANDBOXED=1` (then reported as `unsandboxed`), and the POSIX memory ceiling is a best-effort `prlimit` on Linux only (no
`preexec_fn`, which is unsafe with threads).

Cost: roughly 2 s extra per build (profile and ACL work, a second process launch, copying the
harness, script and kit into the run directory).

### Defence-in-depth layers (not the boundary)

- **AST gate** (`sonder_runtime/domain/meshgen/script_safety.py`). It no longer polices files or
  network (the sandbox does); it stops secret access and DoS:
  - `bpy` may be imported only as plain `import bpy`, and is usable only as
    `bpy.ops.<category>.<operator>(...)` with a category that cannot run Python (`mesh`, `object`,
    `transform`, `curve`, `material`, `uv`, `collection`, `image`, `render`, `export_scene`,
    `import_scene`; `wm`, `script`, `text`, `preferences`, `node`, `console` and `screen` are refused
    because their operators `eval` strings, run files or enable add-ons); `bpy.data.<collection>`
    (everything except text blocks, libraries, window managers, screens, workspaces); and
    `bpy.context.<member>` (scene, view_layer, active/selected objects, depsgraph). `bpy`, `bpy.ops`,
    `bpy.data` and `bpy.context` cannot be assigned, passed, stored, defaulted or returned, so there is
    no alias to launder a path through. Operators whose name contains `script` or `python` are refused.
    Any modifier type, node group, file path, save/load/export/import call and `path_resolve` is allowed.
  - Imports are limited to `bpy`, `bmesh`, `mathutils`, `math`, `random` and the kit module, and
    `winreg`, `_winreg`, `ctypes`, `_ctypes`, `_winapi`, `msvcrt`, `socket`, `subprocess`,
    `multiprocessing`, `os`/`nt`/`posix`/`sys`, `importlib`, `builtins`, `marshal`, `pickle`, `code`,
    `codeop`, `pty` and `signal` are refused as imports, names, attributes and strings.
  - Routes to unrestricted Python are refused as attributes, keyword arguments and strings:
    `as_module`, `blend_data`, `texts`, `text`, `driver*`, `expression`, `utils`, `app`, `handlers`,
    `preferences`, `window_manager`, `types`, `rna_type`, `id_data` and more (see
    `FORBIDDEN_ATTRIBUTES`). `open`, `exec`, `eval`, `compile`, `globals`, `vars`, `getattr`, `type`,
    `dir`, `format` and every name or attribute starting with `_` (except a bare `_`) are refused.
  - The kit module is reachable only through the exposed names described under "Kit exposure".
  - Literal numeric arguments or assignments named `segments`, `ring_count`, `vertices`, `levels`,
    `count`, `cuts`, `steps`, `resolution`, `iterations` ... are capped (see `ARGUMENT_CAPS`) and a
    literal `range()` bound above 1,000,000 is refused, so one literal cannot request a mesh or loop
    that exhausts memory or time.
  - `str.format` and `format_map` (they resolve attribute chains from string text) and any string
    literal containing `__` are refused; `%` and f-string formatting stay available.
- **Restricted builtins** inside Blender (`harness.py`): a small safe subset, and `__import__`
  limited to the same module allow-list.
- **Environment scrub**: Blender is started with an explicit allow-list environment, never a copy
  of the server's. Only `SYSTEMROOT`, `WINDIR`, `SYSTEMDRIVE`, `PATHEXT`, `PROCESSOR_ARCHITECTURE`,
  `NUMBER_OF_PROCESSORS`, `OS`, `LANG`, `LC_ALL` are copied; `PATH` is just the Blender directory
  plus the system directories; `TEMP`/`TMP`/`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` point at a
  per-build scratch directory (`<candidate dir>/.blender_profile`, deleted after every build) on the unsandboxed path (the sandbox uses the run directory). API keys, tokens and `SONDER_*` values never reach the process.
- **Kit exposure** (`sonder_runtime/domain/meshgen/kit_policy.py`). A kit is trusted code, but its
  public functions are reachable from untrusted scripts. Plain file reads and writes no longer
  exclude a function (the sandbox confines them). Exposure is an allow-list derived from the kit
  source: a function or class is excluded when anything in it touches scene-level export/import/save
  or render (`export_scene`, `import_scene`, `save*`, `ops.render`, `open_mainfile`, `read_homefile`...:
  the harness exports and renders itself, and a kit helper doing it too would clobber the harness'
  output), process control (`exit`, `exec`/`eval`/`compile`/`__import__`), or one of the modules that read
  secrets or spawn processes (`os` except path-string helpers like `os.path`, `sys`, `subprocess`, `socket`,
  `ctypes`, `winreg`, `importlib`, `builtins`, `pickle`, `marshal`, `urllib`, `http`; `system`/`popen`
  only on such receivers); location constants (`KIT_DIR`, `REPO_ROOT`, ...) and text constants are
  never exposed, and exclusion is transitive (a function that uses an excluded location is excluded).
  For the Lanternfall `lf_kit.py` this excludes `COL`, `KIT_DIR`, `PREVIEW_DIR`, `PROPS_OUT_ROOT`,
  `REPO_ROOT`, `finalize_and_export`, `render_preview`, `result_line` and `run_prop` (`reset_scene` stays
  available). The operator can override the derivation with module-level literals in the kit:
  `MESHGEN_ALLOW = ["box", "lathe"]` replaces the derived list (only public names the kit defines
  count) and `MESHGEN_EXCLUDE = ["helper"]` removes names from it. Excluded names never appear in the
  API digest or repair hints; the prompt tells the model that the harness exports and renders. As
  defence in depth the harness replaces every excluded *function* in the loaded kit module with a stub
  that raises (constants are left alone, because exposed kit functions read them, e.g. `COL`). A
  user-supplied `api_doc_path` text is passed through unchanged and is the operator's responsibility.
- **Minimal helper globals**: the injected `paint` helper is built in a fresh namespace holding five
  harmless builtins, so reflection on it reaches nothing of the harness.
- **Harness hardening**: before the script runs, `Text.as_module` and `bpy.utils.execfile`
  are replaced with functions that refuse, so a script that somehow reached a text block
  still cannot execute it with unrestricted builtins.
- **Blender flags and plumbing**: `--factory-startup --disable-autoexec`, no shell, `cwd` the
  output directory, a per-build timeout. The harness reports its result through a
  per-job, unguessable file (not stdout, which the script can write to), and every file the
  result names must resolve inside the candidate's output directory.

- **Memory ceiling for the unsandboxed path** (best effort, `blender_runner.run_limited`): a Job
  Object with a per-process committed-memory cap (default 8192 MB; brief window between start and
  assignment), or on Linux `prlimit(RLIMIT_AS)` right after start (three times the cap). The Linux
  path is written but was not exercised on this Windows host.

Without the sandbox (opt-out or a non-Windows host) none of the above is a boundary: an allowed
operator or a Blender bug can still do harm, a script can burn CPU until the timeout, and nothing
else limits what Blender itself can reach. Run the tool unsandboxed only where `run_code` would be
acceptable.

Reads are guarded: `kit_path` and `api_doc_path` go through the same guarded reader as the
file tools (`file_ops.require_read_access`), so they must sit inside the allowed file roots
(`SONDER_FILE_ROOTS`) and credential stores such as `.ssh` are refused before anything can
reach a prompt.

## Troubleshooting

- `Blender not found`: set `SONDER_BLENDER`.
- `no result; rc=N` in an attempt: Blender crashed before reporting; the `tail` lines in the
  attempt show the last output. Try a lower `max_tris` or fewer parts.
- `vision: unavailable`: the vision tier is unset, not loopback, or the model is not pulled.
  The first vision error disables critique for the rest of the run.
- Every attempt `rejected`: read the reasons in `attempts[].issues`; they name the line and the rule.
