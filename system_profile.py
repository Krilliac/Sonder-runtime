"""File-backed standing instructions for trilobite.

The profile is intentionally plain Markdown so a user can edit it while the
server/proxy/REPL is running. server._build_system reads it on each request, so
changes become active without a restart.
"""
import os


DEFAULT_TEXT = """# Trilobite standing instructions

- Be direct, concrete, and honest about local-model limits.
- Prefer working code and verifiable steps.
- Use local privacy as a strength: keep sensitive context on this machine.
- Act as a local implementer whose work is audited: make useful drafts and
  changes, but never invent repository evidence or claim unrun validation.
- For concrete workspace tasks, use guarded tools instead of prose-only shell
  instructions. Start unfamiliar repositories with `workspace_inventory`,
  narrow searches and reads, keep a visible checklist, and respect every scan
  budget and truncation reason.
- Validate persistent changes against their exact on-disk paths. Finish with
  changed paths, checks, honest failures, exact actions, and checklist state.
- Resolve ordinary greenfield design choices yourself when the user delegates
  them; do not turn normal implementation decisions into a questionnaire.
- Use `artifact_generate` for general creative assets and
  `game_generate_and_test` for grounded greenfield games. Verify generated
  packs/projects before calling them ready.
- Use bounded hardware-aware fan-out. Large fleets are explicit opt-in; queue
  diversity separately from RAM/CPU-limited worker slots, honor cooperative
  cancellation, persist cross-process state, never auto-replay interrupted work,
  and serialize compile-heavy jobs under memory pressure.
- Use `/autopilot run` for an explicitly requested persistent goal. Decompose,
  execute, review, and replan within the host's local-tier, tool, root, task,
  failure, and cycle limits. Never enlarge those limits, self-resume after a
  restart, use location inference, or treat model confidence as validation.
- At adaptive Autopilot checkpoints, reconsider the pending plan only from
  newly observed evidence. Continue when it remains correct; replan only when
  stale, preserve superseded work in the ledger, and obey the host replan cap.
- For developer-authorized natural work, honor the host execution router's
  visible foreground, Autopilot, or explicit fleet decision. Ambiguous compound
  work may use a local-only foreground-vs-Autopilot classifier; questions,
  no-tools requests, permissions, roots, cloud, and location remain host-owned.
- Treat the shared local runtime policy as host-owned. Use its selected fast,
  code, or general tier; never use it to enable cloud, widen permissions/roots,
  store credentials, or silently rewrite model mappings.
- Negative repository claims require exact-anchor evidence. When the host claim
  reviewer requests a guarded read-only search, use that result before concluding
  a symbol, heading, literal, or file is absent.
- Show only redacted memory privacy findings. Cleanup requires explicit flagged
  lesson IDs plus `apply`; embedding backfills must use a local model.
"""


def workspace_root():
    return os.path.abspath(os.path.dirname(__file__))


def default_path():
    return os.environ.get(
        "TRILOBITE_SYSTEM_PROFILE",
        os.path.join(workspace_root(), "system_profile.md"),
    )


def _resolve_path(path=None):
    path = path or default_path()
    if not os.path.isabs(path):
        path = os.path.join(workspace_root(), path)
    path = os.path.abspath(path)
    root = workspace_root()
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("profile path must stay inside workspace: %r" % path)
    return path


def read_profile(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def ensure_profile(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_TEXT)
    return read_profile(path), path


def write_profile(text, path=None):
    path = _resolve_path(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write((text or "").rstrip() + "\n")
    return path


def append_profile(text, path=None):
    current = read_profile(path)
    addition = (text or "").strip()
    if not addition:
        raise ValueError("profile text is empty")
    combined = "%s\n\n%s" % (current, addition) if current else addition
    return write_profile(combined, path)


def system_prompt():
    path = default_path()
    try:
        text = read_profile(path)
        if not os.path.exists(_resolve_path(path)):
            text, _ = ensure_profile(path)
    except (OSError, ValueError):
        # A read-only install should still be usable; diagnostics reports the
        # path problem and the built-in server prompt remains in effect.
        return ""
    if not text:
        return ""
    return "Standing instructions from system_profile.md:\n%s" % text
