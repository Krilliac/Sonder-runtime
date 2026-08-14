"""Exercise every read-only slash command against a running sonder_serve.

Why this exists: the desktop app advertises a command palette, and the server
accepts commands it never lists. Nothing proved the two agree, or that a
listed command actually answers rather than falling through to the model and
being improvised. This drives each one and classifies the reply.

Read-only by construction. Commands that write files, spawn work, mutate
accounts, or change policy are listed in MUTATING and never sent -- running
them unattended against someone's real store is not a test, it is damage.

Usage:
    python scripts/probe_slash_commands.py [PORT] [--json OUT]
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Anything that creates, deletes, executes, trains, spends money, or changes
# who you are. Never probed.
MUTATING = {
    "/delete", "/write", "/edit", "/append", "/mkdir",
    "/run", "/runproject", "/runprogram", "/runscript", "/runwindow",
    "/runconsole", "/runnew",
    "/train", "/forge", "/game", "/gamegen", "/gamefleet", "/gamesuite",
    "/gamecampaign",
    "/asset", "/assetgen", "/assets", "/artifact", "/groundartifact",
    "/verifyartifact",
    "/selfmod", "/selfmodify",
    # These mutate the source checkout itself.  A read-only contract probe
    # must never fast-forward a clean runtime merely because it accepts the
    # alias as a slash command.
    "/update", "/updatesource",
    "/admin", "/register", "/login", "/setaccount", "/accounts", "/whoami",
    "/autopilot", "/auto", "/goal", "/goals", "/plan", "/work", "/agent",
    "/master", "/agentcancel", "/cancelagents", "/agentretry", "/retryagent",
    "/qualityfix", "/privacyfix", "/embedfix",
    "/strict", "/filepolicy", "/contextsize", "/ctxsize", "/compact",
    "/compaction", "/dump", "/task", "/learn", "/prefer", "/preference",
    "/emotion", "/emotions", "/mood",
    # Feedback verbs mutate the learning loop's outcome ledger.
    "/pass", "/fail", "/good", "/bad", "/accept", "/accepted", "/edited",
    "/used", "/copied",
    # Need an argument to mean anything; sending them bare proves nothing.
    "/read", "/find", "/grep", "/search", "/inspect", "/inspectimage",
    "/image", "/programfind", "/scriptfind", "/weather", "/trace",
}


def discover_commands() -> list[str]:
    """Every command literal in sonder_serve._handle_slash."""
    src = (ROOT / "sonder_serve.py").read_text(encoding="utf-8", errors="ignore")
    start = src.index("def _handle_slash")
    end = src.index("def _handle_feedback")
    return sorted({m for m in re.findall(r'"(/[a-z0-9]+)"', src[start:end])})


def palette_commands() -> set[str]:
    """Commands the desktop app offers in its slash palette."""
    src = (ROOT / "app/lib/chat_screen.dart").read_text(
        encoding="utf-8", errors="ignore")
    start = src.index("_quickCommands")
    end = src.index("};", start)
    return {m.split()[0] for m in re.findall(r"'(/[^']+)'", src[start:end])}


def ask(port: int, text: str, timeout: int) -> tuple[str, str]:
    body = json.dumps({
        "model": "sonder", "stream": False,
        "messages": [{"role": "user", "content": text}],
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
        return "ok", payload["choices"][0]["message"]["content"] or ""
    except urllib.error.HTTPError as exc:
        return "http_%d" % exc.code, exc.read(2048).decode("utf-8", "replace")
    except Exception as exc:  # timeout, refused, malformed
        return "error", repr(exc)


def classify(command: str, status: str, reply: str) -> str:
    """A handled command answers directly. A fallthrough gets improvised by
    the model, which is the failure this probe exists to catch."""
    if status != "ok":
        return status
    text = reply.strip()
    if not text:
        return "empty"
    # The model, handed an unknown slash token, explains or asks about it
    # instead of executing it. Handled commands emit reports, not prose
    # addressed to the reader.
    lowered = text.lower()
    tells = (
        "i'm not sure", "i am not sure", "it seems", "could you", "would you",
        "as an ai", "i don't have", "i do not have", "appears to be a",
        "let me know", "here's an example", "here is an example",
    )
    if any(t in lowered for t in tells):
        return "fellthrough?"
    return "handled"


def main() -> int:
    port = 11435
    out_path = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--json" and i + 1 < len(args):
            out_path = args[i + 1]
        elif a.isdigit():
            port = int(a)

    handled = discover_commands()
    palette = palette_commands()
    probe = [c for c in handled if c not in MUTATING]

    print("server-recognised commands : %d" % len(handled))
    print("offered by the app palette : %d" % len(palette))
    missing = sorted(palette - set(handled))
    print("in palette, NOT recognised : %d %s"
          % (len(missing), missing if missing else ""))
    print("probing (read-only)        : %d" % len(probe))
    print()

    rows = []
    for i, cmd in enumerate(probe, 1):
        t0 = time.time()
        status, reply = ask(port, cmd, timeout=90)
        verdict = classify(cmd, status, reply)
        rows.append({
            "command": cmd, "verdict": verdict, "seconds": round(time.time() - t0, 1),
            "chars": len(reply), "preview": reply.strip()[:120].replace("\n", " "),
        })
        print("  [%2d/%2d] %-18s %-12s %5.1fs  %s"
              % (i, len(probe), cmd, verdict, rows[-1]["seconds"],
                 rows[-1]["preview"][:70].encode("ascii", "replace").decode()))

    print()
    tally = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print("summary:", ", ".join("%s=%d" % kv for kv in sorted(tally.items())))

    if out_path:
        Path(out_path).write_text(
            json.dumps({"missing_from_server": missing, "results": rows},
                       indent=2),
            encoding="utf-8")
        print("wrote", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
