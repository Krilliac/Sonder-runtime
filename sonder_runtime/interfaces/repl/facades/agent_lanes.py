"""Bounded local-console lane controls and terminal-safe presentation."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
import uuid

from ....application.context import local_owner_context

PAGE_SIZE = 20
HELP = """/lanes list [cursor]
/lanes show <lane-id> [cursor]
/lanes message <lane-id> <text>
/lanes interrupt|resume|cancel <lane-id>
/lanes archive <lane-id>
/lanes reports <lane-id> [cursor]
/lanes ack <lane-id> <report-id> [reports-page-cursor]
Messages preserve pasted multiline text. Controls are cooperative requests.
/agents shows legacy activity; /lanes shows durable conversations.
Status, tier and revision are read from the server-owned lane record. Per-lane
capacity counters are not exposed here.
Use /capacity for the bounded cluster view.
Resource values are never inferred from a lane's status."""
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z")


def terminal_text(value, *, width=80, limit=16000):
    """Render controls visibly; retain line breaks and wrap by terminal cells."""
    raw = str(value or "")
    clipped = len(raw) > limit
    raw = raw[:limit]
    safe = []
    for ch in raw:
        if ch == "\n":
            safe.append(ch)
        elif ch == "\t":
            safe.append("    ")
        elif unicodedata.category(ch) in {"Cc", "Cf", "Cs"}:
            safe.append(("\\x%02x" if ord(ch) < 256 else "\\u%04x") % ord(ch))
        else:
            safe.append(ch)
    width = max(12, min(160, int(width)))
    output, row, cells = [], "", 0
    for ch in "".join(safe) + ("\n[output truncated]" if clipped else ""):
        size = (
            0
            if unicodedata.combining(ch)
            else (2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1)
        )
        if ch == "\n":
            output.append(row)
            row, cells = "", 0
        else:
            if cells + size > width:
                output.append(row)
                row, cells = "", 0
            row += ch
            cells += size
    output.append(row)
    return "\n".join(output)


def _id(value):
    if not _ID.fullmatch(value):
        raise ValueError("IDs must be 1..160 letters, digits, hyphens or underscores")
    return value


def _cursor(value="0"):
    if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > 2147483647:
        raise ValueError("cursor must be an integer from 0 to 2147483647")
    return int(value)


def parse(argument):
    if not isinstance(argument, str) or len(argument) > 9000:
        raise ValueError("lane command exceeds 9000 characters")
    parts = argument.strip().split(None, 2)
    action = parts[0].lower() if parts else "list"
    if action == "help":
        if len(parts) != 1:
            raise ValueError("use /lanes help")
        return action, {}
    if action == "message":
        if len(parts) != 3 or not parts[2].strip() or len(parts[2]) > 8000:
            raise ValueError(
                "use /lanes message <lane-id> <text up to 8000 characters>"
            )
        return action, {"lane_id": _id(parts[1]), "content": parts[2]}
    fields = argument.strip().split()
    fields = fields or ["list"]
    if action == "list" and len(fields) <= 2:
        return action, {"cursor": _cursor(fields[1] if len(fields) > 1 else "0")}
    if action in {"show", "reports"} and 2 <= len(fields) <= 3:
        return action, {
            "lane_id": _id(fields[1]),
            "cursor": _cursor(fields[2] if len(fields) > 2 else "0"),
        }
    if action in {"interrupt", "resume", "cancel", "archive"} and len(fields) == 2:
        return action, {"lane_id": _id(fields[1])}
    if action == "ack" and 3 <= len(fields) <= 4:
        return action, {
            "lane_id": _id(fields[1]),
            "report_id": _id(fields[2]),
            "cursor": _cursor(fields[3] if len(fields) > 3 else "0"),
        }
    raise ValueError("unknown action or invalid arguments; use /lanes help")


class LaneConsoleFacade:
    """No root imports or bearer credentials; injected callbacks own composition."""

    def __init__(self, application_factory, approve):
        self._factory = application_factory
        self._approve = approve

    @staticmethod
    def _inside(lane, context):
        raw = lane.get("workspace_root")
        if not isinstance(raw, str) or not raw:
            return False
        root = Path(raw)
        # Durable roots were canonical when admitted. Fail closed on retargeting.
        return (
            root.is_absolute()
            and root.is_dir()
            and root.resolve() == root
            and any(root.is_relative_to(granted) for granted in context.workspace_roots)
        )

    def run(self, argument="", *, width=80):
        try:
            action, args = parse(argument)
            if action == "help":
                return terminal_text(HELP, width=width)
            application = self._factory()
            roots = tuple(
                Path(root).resolve()
                for root in application.config.state.workspace_roots
            )
            roots = tuple(root for root in roots if root.is_dir())
            if not roots:
                raise PermissionError("no available configured workspace roots")
            context = local_owner_context(
                correlation_id="console-lanes-" + uuid.uuid4().hex,
                source="repl",
                workspace_roots=roots,
                timeout_seconds=60,
                remote_ollama_allowed=application.config.ollama.allow_remote,
            )
            service = application.agent_lanes()
            text = self._run(service, context, action, args)
        except (ValueError, TypeError) as exc:
            text = "Invalid lane command: " + str(exc)
        except KeyError:
            text = (
                "Lane or report not found. Use /lanes list, then /lanes show <lane-id>."
            )
        except PermissionError as exc:
            text = "Lane access refused: " + str(exc)
        except Exception as exc:
            text = (
                "Lane service unavailable ("
                + type(exc).__name__
                + "). Retry /lanes list."
            )
        return terminal_text(text, width=width)

    def _lane(self, service, context, lane_id, cursor=None):
        result = service.read_view(
            context,
            lane_id=lane_id,
            cursor=cursor or 0,
            limit=PAGE_SIZE,
            transcript=cursor is not None,
        )
        if not self._inside(result["lane"], context):
            raise PermissionError(
                "lane is outside the current configured workspace roots"
            )
        return result

    @staticmethod
    def _summary(lane):
        status = lane.get("status", "unknown")
        tier = lane.get("tier") or "unavailable"
        revision = lane.get("revision", "unknown")
        return (
            f"{lane['id']}  {status}  revision {revision}\n"
            f"  execution: {status} · tier {tier} · revision {revision}\n"
            f"  {str(lane.get('title') or lane.get('task') or '(untitled)')[:200]}\n"
            f"  workspace: {lane.get('workspace_root', 'unknown')}\n"
            "  resources: unavailable per lane; use /capacity"
        )

    @staticmethod
    def _next(result, command):
        return (
            "\nNext page: " + command + " " + str(result["next_cursor"])
            if result["has_more"]
            else "\nEnd of page stream."
        )

    def _run(self, service, context, action, args):
        if action == "list":
            result = service.read_view(context, cursor=args["cursor"], limit=PAGE_SIZE)
            rows = [lane for lane in result["lanes"] if self._inside(lane, context)]
            text = "\n\n".join(self._summary(lane) for lane in rows)
            if not rows:
                text = (
                    "No visible lanes on this page."
                    if result["source_count"] or args["cursor"]
                    else "No durable lanes yet."
                )
            if len(rows) != result["source_count"]:
                text += "\nPage filtered to current workspace roots; hidden rows still advance the cursor."
            return (
                text
                + self._next(result, "/lanes list")
                + "\nInspect: /lanes show <lane-id>"
            )
        lane_result = self._lane(
            service,
            context,
            args["lane_id"],
            args.get("cursor", 0) if action == "show" else None,
        )
        lane = lane_result["lane"]
        if action == "show":
            text = self._summary(lane) + "\n\nConversation events (bounded page):"
            for event in lane_result["events"]:
                text += f"\n\n  {event['sequence']} {event['event_type']}"
                payload = event.get("payload") or {}
                content = payload.get("content")
                if isinstance(content, str):
                    text += "\n  " + content[:500].replace("\n", "\n  ")
                    if len(content) > 500:
                        text += "\n  [event text truncated]"
            if not lane_result["events"]:
                text += "\nNo events on this page."
            return (
                text
                + self._next(lane_result, "/lanes show " + lane["id"])
                + (
                    "\nSteer: /lanes message "
                    + lane["id"]
                    + " <text>\nReports: /lanes reports "
                    + lane["id"]
                    + "\nControls: /lanes interrupt|resume|cancel "
                    + lane["id"]
                )
            )
        if action in {"reports", "ack"}:
            result = service.reports(
                lane["parent_session_id"],
                context,
                cursor=args["cursor"],
                limit=PAGE_SIZE,
            )
            reports = [
                report
                for report in result["reports"]
                if report["lane_id"] == lane["id"]
            ]
            if action == "reports":
                blocks = [
                    f"{report['id']}  {'acknowledged' if report['acknowledged'] else 'unacknowledged'}\n"
                    + (
                        str(report["summary"])[:400]
                        + (
                            "\n[report text truncated]"
                            if len(str(report["summary"])) > 400
                            else ""
                        )
                    )
                    + "\nAck: /lanes ack "
                    + lane["id"]
                    + " "
                    + report["id"]
                    + " "
                    + str(args["cursor"])
                    for report in reports
                ]
                text = "\n\n".join(blocks) or "No reports for this lane on this page."
                text += "\nPage is filtered to this lane; cursor follows its actual parent report stream."
                return text + self._next(result, "/lanes reports " + lane["id"])
            if not any(report["id"] == args["report_id"] for report in reports):
                raise KeyError(args["report_id"])
        # Serialize once. Neither the callback nor mutable argument dictionaries can substitute effects.
        command = {
            "action": action,
            "lane_id": lane["id"],
            "workspace_root": lane["workspace_root"],
            "workspace_roots": [str(root) for root in context.workspace_roots],
            "principal_id": context.principal_id,
            "author": "user",
            "remote_ollama_allowed": context.remote_ollama_allowed,
            "command_id": "console-" + uuid.uuid4().hex,
        }
        if action == "message":
            command["content"] = args["content"]
        if action == "ack":
            command.update(
                report_id=args["report_id"], parent_session_id=lane["parent_session_id"]
            )
        encoded = json.dumps(command)
        if len(encoded) > 16000:
            raise ValueError(
                "exact approval detail exceeds display bound; shorten the message"
            )
        approval_arguments = json.loads(encoded)
        # A transport attempt nonce is not part of the operator's semantic grant.
        # Keeping it out lets an unchanged unattended retry consume its approval.
        approval_arguments.pop("command_id")
        allowed, reason = self._approve(approval_arguments)
        if not allowed:
            return "Lane action refused: " + str(reason)
        command = json.loads(encoded)
        if context.expired or context.cancellation.cancelled:
            raise PermissionError("console request expired or cancelled")
        current_config = self._factory().config
        configured = tuple(
            Path(root).resolve() for root in current_config.state.workspace_roots
        )
        if current_config.ollama.allow_remote != context.remote_ollama_allowed:
            raise PermissionError("remote model permission changed after approval")
        if set(configured) != set(context.workspace_roots):
            raise PermissionError("configured workspace roots changed after approval")
        live = self._lane(service, context, command["lane_id"])["lane"]
        if live["workspace_root"] != command["workspace_root"]:
            raise PermissionError("lane workspace changed after approval")
        options = {"command_id": command["command_id"], "context": context}
        if action == "message":
            receipt = service.send_message(
                command["lane_id"], content=command["content"], author="user", **options
            )
        elif action == "ack":
            receipt = service.ack_report(
                command["report_id"],
                parent_session_id=command["parent_session_id"],
                **options,
            )
        elif action == "archive":
            receipt = service.archive(command["lane_id"], **options)
        else:
            receipt = service.control(
                command["lane_id"], action, author="user", **options
            )
        return (
            f"Recorded {action}: {receipt['lane']['id']}\n"
            f"State: {receipt['lane']['status']}  revision {receipt['lane']['revision']}\n"
            "Recorded requests are not proof that an in-flight effect has stopped.\n"
            "Inspect: /lanes show " + receipt["lane"]["id"]
        )
