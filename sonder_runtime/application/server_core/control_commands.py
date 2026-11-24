"""Pure argument parsers and usage builders for ``control_command``."""
from __future__ import annotations

import json
from typing import Any


def parse_control_kwargs(cmd: str, arg: str) -> tuple[dict[str, Any], str]:
    """Parse slash-command text into ``(kwargs, usage_error)``.

    The actual tool/helper call stays directly in ``server.control_command`` so
    ``command_catalog``'s static AST walk observes every branch and tool edge.
    """
    stripped = arg.strip()
    if cmd in ("/search", "/grep"):
        parts = [part.strip() for part in arg.split("|", 2)]
        if not parts or not parts[0]:
            return {}, "usage: /search <text> | <root> | <glob>"
        return {
            "query": parts[0],
            "root": parts[1] if len(parts) > 1 and parts[1] else ".",
            "glob": parts[2] if len(parts) > 2 and parts[2] else "*",
        }, ""
    if cmd in ("/scripts", "/scriptfind"):
        parts = [part.strip() for part in arg.split("|", 1)]
        return {
            "query": parts[0] or "*",
            "root": parts[1] if len(parts) > 1 and parts[1] else ".",
        }, ""
    if cmd in ("/schema", "/schema_validate"):
        parts = [part.strip() for part in arg.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return {}, "usage: /schema <data path> | <schema path or inline JSON>"
        if parts[1].startswith("{"):
            return {"path": parts[0], "schema_json": parts[1]}, ""
        return {"path": parts[0], "schema_path": parts[1]}, ""
    if cmd in ("/logic", "/logic_verify"):
        parts = [part.strip() for part in arg.split("|", 1)]
        if not parts or not parts[0]:
            return {}, "usage: /logic <formula> [| <formula_b for equivalence>]"
        if len(parts) == 2 and parts[1]:
            return {"formula": parts[0], "action": "equivalent", "formula_b": parts[1]}, ""
        return {"formula": parts[0], "action": "satisfiable"}, ""
    if cmd in ("/regex", "/regex_lab"):
        parts = [part.strip() for part in arg.split("|", 1)]
        if not parts or not parts[0]:
            return {}, "usage: /regex <pattern> [| <sample text>]"
        return {"pattern": parts[0], "text": parts[1] if len(parts) == 2 else ""}, ""
    if cmd in ("/graph", "/code_graph"):
        parts = [part.strip() for part in arg.split("|", 1)]
        if len(parts) == 2 and parts[1]:
            return {"root": parts[0] or ".", "action": "impact", "target": parts[1]}, ""
        return {"root": stripped or ".", "action": "summary"}, ""
    if cmd in ("/sdiff", "/diff_semantic"):
        parts = [part.strip() for part in arg.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return {}, "usage: /sdiff <path_a> | <path_b>"
        return {"path_a": parts[0], "path_b": parts[1]}, ""
    if cmd in ("/reason", "/reasoning_board"):
        if not stripped or stripped.lower() == "summary":
            return {"action": "summary"}, ""
        if stripped.lower() == "audit":
            return {"action": "audit"}, ""
        if stripped.lower() == "clear":
            return {"action": "clear"}, ""
        board_args = [part.strip() for part in stripped.split("|", 1)]
        if (
            board_args[0].lower() in {"init", "add_fact", "add_hypothesis", "add_decision"}
            and len(board_args) == 2
        ):
            return {"action": board_args[0].lower(), "text": board_args[1]}, ""
        return {"action": "add_fact", "text": stripped}, ""
    if cmd in ("/symcheck", "/symbol_verify"):
        parts = [part.strip() for part in arg.split("|", 1)]
        if not parts or not parts[0]:
            return {}, "usage: /symcheck <module> [| <symbol>]"
        return {"module": parts[0], "symbol": parts[1] if len(parts) == 2 else ""}, ""
    if cmd in ("/citecheck", "/citation_audit"):
        parts = [part.strip() for part in arg.split("|", 1)]
        if len(parts) == 2 and parts[1]:
            return {"root": parts[0] or ".", "text": parts[1]}, ""
        if stripped:
            return {"root": ".", "text": stripped}, ""
        return {}, "usage: /citecheck [<root> |] <text with file:line citations>"
    if cmd in ("/consensus", "/consistency_check"):
        parts = [part.strip() for part in arg.split("||") if part.strip()]
        if len(parts) < 2:
            return {}, "usage: /consensus <candidate 1> || <candidate 2> [|| ...]"
        return {"action": "compare_candidates", "candidates_json": json.dumps(parts)}, ""
    if cmd in ("/fallback", "/plan_fallback"):
        parts = [part.strip() for part in arg.split("|", 2)]
        if not parts or not parts[0]:
            return {}, "usage: /fallback <failed_tool> [| <error> [| <goal>]]"
        return {
            "failed_tool": parts[0],
            "error": parts[1] if len(parts) > 1 else "",
            "goal": parts[2] if len(parts) > 2 else "",
        }, ""
    if cmd in ("/shield", "/prompt_shield"):
        if not stripped:
            return {}, "usage: /shield <text>  OR  /shield path:<file>"
        if stripped.lower().startswith("path:"):
            return {"path": stripped[5:].strip()}, ""
        return {"text": stripped}, ""
    if cmd in ("/diagnose", "/error_diagnose"):
        parts = [part.strip() for part in arg.split("||", 1)]
        if not parts or not parts[0]:
            return {}, "usage: /diagnose <error_or_traceback> [|| <root>]"
        return {"error_text": parts[0], "root": parts[1] if len(parts) > 1 else ""}, ""
    if cmd in ("/audit", "/code_audit"):
        if not stripped:
            return {}, "usage: /audit <path>  OR  /audit code:<snippet>"
        if stripped.lower().startswith("code:"):
            return {"code": stripped[5:].strip()}, ""
        return {"path": stripped}, ""
    if cmd in ("/transform", "/data_transform"):
        parts = [part.strip() for part in arg.split("|")]
        if not parts or not parts[0]:
            return {}, "usage: /transform <action> | <data> [| <output_format_or_expr>]"
        act = parts[0]
        payload = parts[1] if len(parts) > 1 else ""
        third = parts[2] if len(parts) > 2 else ""
        if act in ("query", "cron"):
            return {"action": act, "data": payload, "expression": third or payload}, ""
        if act in ("encode", "decode"):
            return {"action": act, "data": payload, "encoding": third or "base64"}, ""
        return {"action": act, "data": payload, "output_format": third or "json"}, ""
    if cmd in ("/learntool", "/learn_tool", "/discovertool"):
        parts = [part.strip() for part in arg.split("|", 2)]
        if not parts or not parts[0]:
            return {}, "usage: /learntool <tool> [| <goal> [| <subcommand>]]"
        return {
            "tool": parts[0],
            "goal": parts[1] if len(parts) > 1 else "",
            "subcommand": parts[2] if len(parts) > 2 else "",
        }, ""
    if cmd == "/runprogram":
        parts = [part.strip() for part in arg.split("|", 2)]
        if not parts or not parts[0]:
            return {}, "usage: /runprogram <program> | <args-json> | <cwd>"
        return {
            "program": parts[0],
            "args_json": parts[1] if len(parts) > 1 and parts[1] else "[]",
            "cwd": parts[2] if len(parts) > 2 and parts[2] else ".",
        }, ""
    if cmd == "/runscript":
        parts = [part.strip() for part in arg.split("|", 2)]
        if not parts or not parts[0]:
            return {}, "usage: /runscript <path> | <args-json> | <cwd>"
        return {
            "path": parts[0],
            "args_json": parts[1] if len(parts) > 1 and parts[1] else "[]",
            "cwd": parts[2] if len(parts) > 2 else "",
        }, ""
    if cmd in ("/privacy", "/privacyreview"):
        try:
            return {"sample_limit": int(stripped or 20)}, ""
        except ValueError:
            return {}, "usage: /privacy [sample-limit]"
    if cmd == "/privacyfix":
        repair_arg = stripped
        apply = False
        if repair_arg.lower().startswith("apply "):
            apply = True
            repair_arg = repair_arg[6:].strip()
        if not repair_arg:
            return {}, "usage: /privacyfix [apply] <lesson-id[,lesson-id...]>"
        return {"lesson_ids_json": repair_arg, "apply": apply}, ""
    if cmd in ("/embeddings", "/embedfix"):
        embed_parts = stripped.split()
        apply = bool(embed_parts and embed_parts[0].lower() == "apply")
        if apply:
            embed_parts = embed_parts[1:]
        try:
            limit = int(embed_parts[0]) if embed_parts else 25
        except ValueError:
            return {}, "usage: /embeddings [apply] [limit]"
        return {"limit": limit, "apply": apply}, ""
    if cmd in ("/asset", "/assets", "/assetgen", "/artifact"):
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            return {}, "usage: /asset <name> <free-form brief>"
        return {"name": parts[0], "brief": parts[1]}, ""
    if cmd in ("/artifactcheck", "/verifyartifact", "/groundartifact"):
        if not stripped:
            return {}, "usage: /artifactcheck <path> [| recipe]"
        artifact_path, separator, recipe = arg.partition("|")
        return {
            "path": artifact_path.strip(),
            "recipe": recipe.strip() if separator else "auto",
        }, ""
    if cmd in ("/game", "/gamegen"):
        parts = stripped.split(None, 2)
        if len(parts) != 3 or "|" not in parts[2]:
            return {}, "usage: /game <language> <2d|2.5d|3d> <name> | <concept>"
        game_name, _, concept = parts[2].partition("|")
        return {
            "name": game_name.strip(),
            "concept": concept.strip(),
            "language": parts[0],
            "dimension": parts[1],
        }, ""
    return {}, ""


def parse_capacity_arg(arg: str) -> tuple[int | None, str]:
    """Parse ``/capacity [requested-agents]`` into ``(count, usage_error)``."""
    try:
        return int(arg.strip() or 0), ""
    except ValueError:
        return None, "usage: /capacity [requested-agents]"


def parse_agentretry_arg(arg: str) -> tuple[tuple[str, str] | None, str]:
    """Parse ``/agentretry <master-id|prefix> [tier]`` into ``((id, tier), usage_error)``."""
    parts = arg.strip().split(None, 1)
    if not parts:
        return None, "usage: /agentretry <master-id|prefix> [tier]"
    return (parts[0], parts[1] if len(parts) > 1 else ""), ""
