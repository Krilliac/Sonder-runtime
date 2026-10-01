"""Owner-facing playbook management commands shared by CLI and REPL."""
from __future__ import annotations

import json
import shlex
from typing import Any

_STORE_FACTORY = None
_NOTE_CONTEXT_FACTORY = None
_APPROVE_FACTORY = None
_RELOAD_FACTORY = None
_RECONCILE_FACTORY = None


def configure_store_factory(factory, note_context_factory=None, approve_factory=None, reload_factory=None, reconcile_factory=None) -> None:
    """Install the composition-root store factory for this surface."""
    global _STORE_FACTORY, _NOTE_CONTEXT_FACTORY, _APPROVE_FACTORY, _RELOAD_FACTORY, _RECONCILE_FACTORY
    _STORE_FACTORY = factory
    _NOTE_CONTEXT_FACTORY = note_context_factory
    _APPROVE_FACTORY = approve_factory
    _RELOAD_FACTORY = reload_factory
    _RECONCILE_FACTORY = reconcile_factory


def _store(*, config=None, home=None):
    if _STORE_FACTORY is None:
        raise RuntimeError("playbook surface is not configured")
    return _STORE_FACTORY(config=config, home=home)


def _payload(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))
        return
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def execute(command: str, args, *, config=None, home=None) -> int:
    """Execute one parsed ``playbooks`` command and return a CLI status."""
    store = _store(config=config, home=home)
    action = str(command or "list")
    try:
        if action == "list":
            if _RECONCILE_FACTORY is not None:
                _RECONCILE_FACTORY(store)
            result = store.list_topics()
        elif action == "show":
            result = store.show(args.topic, args.entry_id, approved_only=False)
            if result is None:
                _payload({"error": "entry_not_found", "entry_id": args.entry_id}, args.json)
                return 1
        elif action in {"approve", "reject"}:
            result = store.show(args.topic, args.entry_id, approved_only=False)
            if result is None:
                raise KeyError(args.entry_id)
            if action == "approve":
                if _APPROVE_FACTORY is None:
                    raise RuntimeError("playbook approval surface is not configured")
                digest = _APPROVE_FACTORY(result)
            else:
                digest = None
            result = store.review(args.topic, args.entry_id, "approved" if action == "approve" else "rejected", expected_digest=digest)
        elif action == "edit":
            changes = {
                name: value for name, value in {
                    "title": args.title, "body": args.body, "evidence": args.evidence,
                    "category": args.category, "supersedes": args.supersedes,
                    "triggers": args.triggers,
                }.items() if value is not None
            }
            if not changes:
                raise ValueError("edit requires at least one field")
            result = store.edit(args.topic, args.entry_id, **changes)
        elif action == "rm":
            result = {"removed": store.remove(args.topic, args.entry_id)}
            if not result["removed"]:
                return 1
        elif action == "merge":
            result = store.merge_duplicates(args.topic, apply=args.apply)
        else:
            raise ValueError("unknown playbooks action: %s" % action)
    except (KeyError, ValueError, OSError) as exc:
        _payload({"error": str(exc)}, args.json)
        return 2
    _payload(result, args.json)
    return 0


def run_repl_command(text: str, *, home=None) -> str:
    """Parse a compact owner command after ``/playbooks`` in the REPL."""
    tokens = shlex.split(text)
    action = tokens[0] if tokens else "list"
    if action == "reload":
        if _RELOAD_FACTORY is None:
            return "playbook surface is not configured for reload"
        if _RECONCILE_FACTORY is not None:
            _RECONCILE_FACTORY(_store(home=home))
        _RELOAD_FACTORY()
        return "playbook index reloaded"
    if action == "correction":
        if len(tokens) < 5 or "--body" not in tokens:
            return "usage: /playbooks correction <topic> <category> <title> --body <text>"
        body_index = tokens.index("--body")
        if body_index + 1 >= len(tokens):
            return "usage: /playbooks correction <topic> <category> <title> --body <text>"
        if _STORE_FACTORY is None or _NOTE_CONTEXT_FACTORY is None:
            return "playbook surface is not configured for owner corrections"
        with _NOTE_CONTEXT_FACTORY(tainted=False, owner_correction=True, provenance={"surface": "repl", "actor": "owner"}):
            entry = _store(home=home).note(tokens[1], tokens[2], " ".join(tokens[3:body_index]),
                                           tokens[body_index + 1], tainted=False,
                                           owner_correction=True,
                                           provenance={"surface": "repl", "actor": "owner"})
        return json.dumps({"id": entry["id"], "status": entry["status"]}, sort_keys=True)
    if action == "list":
        class Args: json = False
        execute("list", Args(), home=home)
        return ""
    if len(tokens) < 2 or action not in {"show", "approve", "reject", "rm", "edit"}:
        return "usage: /playbooks list|show|approve|reject|rm|edit <topic> <entry-id>"
    if action == "show" and len(tokens) == 2:
        class Args:
            json = False
            topic = tokens[1]
            entry_id = None
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            execute(action, Args(), home=home)
        return output.getvalue().rstrip()
    if action == "edit":
        if len(tokens) < 4:
            return "usage: /playbooks edit <topic> <entry-id> --body <text>"
        class Args:
            json = False
            topic = tokens[1]
            entry_id = tokens[2]
            title = body = evidence = category = supersedes = None
            triggers = None
        if "--body" in tokens:
            index = tokens.index("--body")
            Args.body = " ".join(tokens[index + 1:])
        else:
            return "usage: /playbooks edit <topic> <entry-id> --body <text>"
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            execute(action, Args(), home=home)
        return output.getvalue().rstrip()
    if len(tokens) < 3:
        return "usage: /playbooks " + action + " <topic> <entry-id>"
    class Args:
        json = False
        topic = tokens[1]
        entry_id = tokens[2]
    # Capture the ordinary CLI renderer without adding a second formatter.
    from contextlib import redirect_stdout
    from io import StringIO
    output = StringIO()
    with redirect_stdout(output):
        execute(action, Args(), home=home)
    return output.getvalue().rstrip()


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("playbooks", help="owner-curated playbook notes")
    parser.add_argument("--config")
    parser.add_argument("--secrets")
    parser.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE")
    nested = parser.add_subparsers(dest="playbooks_command", required=True)
    listed = nested.add_parser("list", help="list playbook topics")
    listed.add_argument("--json", action="store_true")
    merge = nested.add_parser("merge", help="plan exact duplicate supersession (owner opt-in)")
    merge.add_argument("topic")
    merge.add_argument("--apply", action="store_true")
    merge.add_argument("--json", action="store_true")
    for action in ("show", "approve", "reject", "rm"):
        child = nested.add_parser(action)
        child.add_argument("topic")
        child.add_argument("entry_id", nargs="?" if action == "show" else None)
        child.add_argument("--json", action="store_true")
    edit = nested.add_parser("edit")
    edit.add_argument("topic")
    edit.add_argument("entry_id")
    edit.add_argument("--title")
    edit.add_argument("--body")
    edit.add_argument("--evidence")
    edit.add_argument("--category")
    edit.add_argument("--supersedes")
    edit.add_argument("--triggers", nargs="*")
    edit.add_argument("--json", action="store_true")
    parser.set_defaults(func=_dispatch)


def _dispatch(args) -> int:
    return execute(args.playbooks_command, args)


__all__ = ["add_parser", "configure_store_factory", "execute", "run_repl_command"]
