# Build tools security

This is the trust model of `build_model`, `build_job` and `build_fix`, and the
mechanism behind each boundary. The functional contract is in
[C++ build model, build jobs and the build-fix loop](../architecture/CPP-BUILD-FIX.md).

## What the model controls

The model names members of a parsed build model: a target, a config, a
platform, a preset, a compile unit or a generator from a closed list. It
never supplies argv, environment entries, executable paths or build scripts.

- The host renders every argv from a closed template set, or from an operator
  profile.
- Any value that starts with `-` or `/`, or contains a shell or MSBuild
  metacharacter, is refused. The typed executor checks this itself before
  anything is planned (`INVALID_INPUT`): the gateway's schema check does not
  evaluate `pattern`. Paths refuse a leading `-` or `@` and UNC or device
  prefixes; editable globs must be project-relative with no `..` segment.
- Utility targets, custom targets and VS Makefile/Utility projects are refused
  (`UTILITY_TARGET_REFUSED`) unless the operator lists them in
  `SONDER_BUILD_UTILITY_TARGETS`. This includes `install`, `package` and
  `deploy`.
- A refused plan is refused when the call is authorized. No prompt and no
  approval request is ever created for it (`build:plan-refused`).

## Approvals and modes

`build_job` and `build_fix` start host processes, so they are graded
`execution`. They are declared in `permission_modes.NATIVE_EXECUTION_TOOLS`,
because they are native-only names. `build_fix_restore` is graded `mutation`.
The model reader and the two result tools are graded `safe`. Cancelling one's
own job through a result tool only stops work.

An approval binds to the planned command. The one-shot ledger digests the call
together with its `resolved_command`: template id, command digest, target,
config, platform, world and network. An approval of one target never runs
another.

`allow_network=true` is decided separately, on the grade name `build_network`.
An operator can deny network builds with one rule and still allow builds. An
approval of a build never covers its network.

## The build-fix grant

One approval of `build_fix` mints an in-process grant. The grant lets the
fix's own typed source writes proceed unattended. It is the smallest authority
that makes a background repair possible under `manual` mode.

The grant is bound to three things:

- **The principal and the job.** The approval is claimed only by the request
  that was approved, and only by the same principal. The fix service issues the
  grant for that job from the approval (one approval, at most one grant); it is
  bound to the job id and revoked when the job ends.
- **The edit scope.** A covered path must be all of these:
  - absolute;
  - reached without links;
  - inside the project root and outside the build directory;
  - allowed by `EditScope`.

  `EditScope` refuses build scripts, build-time tool sources, generated files
  and denied names such as `.env` and key files.
- **Budgets.** At most 6 distinct files, the plan's write count and 400
  changed lines per write, counted by the evaluator before the write; the loop
  holds its total to 400 changed lines.

The grant never:

- adds roots;
- honours guard knobs (`extra_roots`, `bypass`, `developer_authorized`);
- lifts `plan` mode;
- overrides an explicit deny rule, a lost effect fence or a missing privilege.
  The grant answers only the mode's unattended ask: before it admits a call,
  the evaluator asks the permission modes (without recording or spending an
  approval) and keeps any other refusal;
- creates, deletes or renames files;
- covers the network unless the fix was approved with it.

Every write the grant admits is receipted with
`policy_match=...build_fix_grant:<plan_digest>`.

An expired, foreign or out-of-scope token is not an error. The call falls
through to normal grading, and that refuses it unattended.

## What still runs on the host

A build executes the project's own custom commands. Those commands may run
tools built from the same tree: shader compilers, generators, asset cookers.
This is why the fix loop never edits the sources of build-time tool targets.

The receipt reports `isolation_truth=unverified`. Host builds are not
contained. Network enforcement (`unshare -rn` on Linux) is reported
separately and is not a security boundary.

## HTTP

The `/v1/build/*` routes require developer authority. They run as the
authenticated principal, with `source="http"`, through the typed gateway. The
HTTP caller has nobody at a console, so under `manual` mode a build is refused
and the response names the remedies. Job ids are owner-scoped: another
principal's job reads as `JOB_NOT_FOUND`.

## clangd

clangd runs only when the inventory resolves it. It runs as its own process
group with a scrubbed environment. HOME and the cache point at a private
per-session directory, which is removed at close.

Its flags are:

- `--background-index=false`: nothing is written into the project;
- `--enable-config=false`, unless `SONDER_BUILD_CLANGD_CONFIG=1`;
- `--log=error`;
- `--clang-tidy=false`.

There is no `--query-driver`, so clangd never runs the project's compilers.

Frames are bounded:

- header block: 8 KiB;
- header lines: 8;
- content: 4 MiB.

An oversized or malformed frame, a timeout or an idle period kills the process
tree.

## Operator profiles

`SONDER_BUILD_PROFILES` is read only when the file meets all of these:

- it is a regular file reached without a link;
- it is owned by the runtime user;
- its mode has no group or other bits.

On Windows the file is ignored until its ACL can be verified. Profiles can
name only `configure`, `build` and `compile_one`. Their `-D` entries pass the
define grammar, and the banned CMake flags are refused.
