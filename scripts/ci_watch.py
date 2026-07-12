"""Tiny GitHub Actions status helper for this repo.

Requires GitHub CLI (`gh`) when run for real, but parsing/formatting stays pure
Python so the behavior is easy to test.
"""

import argparse
import json
import subprocess
import sys

DEFAULT_FIELDS = "databaseId,name,status,conclusion,headSha,event,workflowName,url,createdAt"
DEFAULT_REPO = "Krilliac/Sonder-runtime"


def _short_sha(value):
    return (value or "")[:7] or "-"


def load_runs(text):
    data = json.loads(text or "[]")
    if not isinstance(data, list):
        raise ValueError("gh run list returned non-list JSON")
    runs = []
    for item in data:
        if not isinstance(item, dict):
            continue
        runs.append({
            "id": item.get("databaseId"),
            "name": item.get("name") or item.get("workflowName") or "run",
            "workflow": item.get("workflowName") or item.get("name") or "workflow",
            "status": item.get("status") or "unknown",
            "conclusion": item.get("conclusion") or "",
            "sha": item.get("headSha") or "",
            "url": item.get("url") or "",
            "created": item.get("createdAt") or "",
        })
    return runs


def summarize(runs):
    failing = [
        r for r in runs
        if r["status"] == "completed" and r["conclusion"] not in ("success", "skipped")
    ]
    running = [r for r in runs if r["status"] != "completed"]
    successful = [
        r for r in runs
        if r["status"] == "completed" and r["conclusion"] == "success"
    ]
    return {
        "total": len(runs),
        "failing": len(failing),
        "running": len(running),
        "successful": len(successful),
    }


def format_runs(runs):
    summary = summarize(runs)
    lines = [
        "github actions",
        "  total=%(total)s running=%(running)s failing=%(failing)s successful=%(successful)s"
        % summary,
    ]
    for run in runs:
        state = run["status"]
        if run["conclusion"]:
            state += "/" + run["conclusion"]
        lines.append(
            "  %(workflow)-14s %(state)-22s %(sha)s %(url)s" % {
                "workflow": run["workflow"],
                "state": state,
                "sha": _short_sha(run["sha"]),
                "url": run["url"],
            }
        )
    return "\n".join(lines)


def run_gh(repo, branch, limit):
    cmd = [
        "gh", "run", "list",
        "--repo", repo,
        "--branch", branch,
        "--limit", str(limit),
        "--json", DEFAULT_FIELDS,
    ]
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show recent GitHub Actions status.")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        text = run_gh(args.repo, args.branch, args.limit)
        print(format_runs(load_runs(text)))
    except FileNotFoundError:
        print("ERROR: gh is not installed or not on PATH.", file=sys.stderr)
        return 127
    except subprocess.CalledProcessError as exc:
        print((exc.stderr or str(exc)).strip(), file=sys.stderr)
        return exc.returncode or 1
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
