"""``mesh_generate``: model-driven 3D asset generation in headless Blender.

Closed loop over several candidates: the model writes a Blender build script,
a safety gate screens it, headless Blender builds and renders it, a vision
model critiques the render, and the script is repaired or revised.  The best
candidate wins.  See ``docs/runbooks/mesh-generation.md``.

The tool executes model-written Python inside Blender, so it is graded like
``run_code`` (the AST gate and restricted builtins are defence in depth, not a
sandbox).  server.py is size-capped, so the tool registers itself from here.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from ..adapters.filesystem import file_ops
from ..adapters.meshgen import blender_runner
from ..application.meshgen.loop import MeshRequest, run as run_loop
from ..domain.common.errors import SonderError
from ..domain.meshgen.critique import critique_prompt
from ..domain.meshgen.prompts import kit_api_digest
from ..domain.meshgen.script_safety import kit_public_names
from ..platform.runtime_threads import ThreadPoolExecutor
from .legacy_root import runtime

MAX_PARALLEL_BUILDS = 3
MAX_API_DOC_CHARS = 12_000
MAX_KIT_BYTES = 512_000
_GENERATE_TIMEOUT_CAP = 600
_BUILD_TIMEOUT = 180
MIN_BUILD_SECONDS = 5
GENERATE_NUM_PREDICT = 8192
GENERATE_NUM_CTX = 24576  # room for the previous script plus an 8k-token reply


def _json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _slug(text: str, fallback: str = "mesh") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:40].strip("-")
    return slug or fallback


def _clamp(value, low, high, default):
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _read_text(path: str, limit: int, label: str) -> tuple[Path, str]:
    """Read a caller-named file through the same guard the file tools use.

    ``require_read_access`` confines the path to the allowed roots and refuses credential
    stores and Sonder control-plane files, so a kit or API doc path cannot smuggle a secret
    into a model prompt.
    """
    try:
        resolved = file_ops.require_read_access(path)
    except (PermissionError, ValueError, OSError) as exc:
        raise SonderError("%s refused: %s" % (label, exc)) from exc
    if not resolved.is_file():
        raise SonderError("%s not found: %s" % (label, resolved))
    if resolved.stat().st_size > limit:
        raise SonderError("%s is larger than %d bytes: %s" % (label, limit, resolved))
    return resolved, resolved.read_text(encoding="utf-8", errors="replace")


def _output_root(rt, output_dir: str, slug: str) -> Path:
    assetgen = rt.assetgen
    workspace = Path(os.path.realpath(assetgen.workspace_root()))
    base = Path(output_dir.strip()) if output_dir.strip() else Path(assetgen.default_asset_root()) / "meshgen"
    if not base.is_absolute():
        base = workspace / base
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(os.path.abspath(base)) / ("%s-%s" % (slug, stamp))
    real = Path(os.path.realpath(target))
    if real != workspace and workspace not in real.parents:
        raise SonderError("mesh output must stay inside the asset workspace: %s" % output_dir)
    return target


def _inside(path, root: Path) -> bool:
    real = os.path.realpath(str(path))
    base = os.path.realpath(str(root))
    try:
        return os.path.commonpath([base, real]) == base
    except ValueError:
        return False


def _candidate_summary(candidate: dict) -> dict:
    best = candidate.get("best") or {}
    summary = {
        "index": candidate["index"], "tier": candidate["tier"], "built": bool(candidate.get("built")),
        "best_round": best.get("round"), "best_score": best.get("score"), "best_tris": best.get("tris"),
        "attempts": candidate.get("attempts", []),
    }
    if candidate.get("error"):
        summary["error"] = candidate["error"]
    return summary


def generate_mesh(brief, name="", kit_path="", api_doc_path="", max_tris=3000, height_m=0.0,
                  candidates=3, rounds=3, tiers="general,code", vision=True, target_score=8,
                  export="glb", output_dir="", timeout=1200, *, rt=None, runner=blender_runner) -> dict:
    """Run the loop and return the compact report; raises ``SonderError`` on bad input."""
    brief = str(brief or "").strip()
    if not brief:
        raise SonderError("brief is required")
    blender = runner.locate_blender()
    if not blender:
        raise SonderError("Blender not found: set SONDER_BLENDER to blender.exe or put blender on PATH "
                          "(see docs/runbooks/mesh-generation.md)")
    rt = rt or runtime()
    candidates = _clamp(candidates, 1, 6, 3)
    rounds = _clamp(rounds, 1, 6, 3)
    max_tris = _clamp(max_tris, 50, 200_000, 3000)
    timeout = _clamp(timeout, 30, 7200, 1200)
    target_score = _clamp(target_score, 0, 10, 8)
    try:
        height = max(0.0, float(height_m or 0.0))
    except (TypeError, ValueError):
        height = 0.0
    export = "fbx" if str(export or "").lower() == "fbx" else "glb"
    tier_list = tuple(item.strip() for item in str(tiers or "").split(",") if item.strip()) or ("general",)
    known = getattr(rt, "TIERS", None)
    if known:
        unknown = [tier for tier in tier_list if tier not in known]
        if unknown:
            raise SonderError("unknown tier(s): %s (available: %s)" % (", ".join(unknown), ", ".join(sorted(known))))
    slug = _slug(name or brief)

    kit_module = None
    kit_names = frozenset()
    api_doc = ""
    kit_file = None
    if str(kit_path or "").strip():
        kit_file, kit_source = _read_text(kit_path.strip(), MAX_KIT_BYTES, "kit")
        kit_module = kit_file.stem
        if kit_file.suffix != ".py" or not kit_module.isidentifier() or kit_module.startswith("_"):
            raise SonderError("kit_path must be a .py file with a plain module name: %s" % kit_file)
        kit_names = kit_public_names(kit_source)
        if str(api_doc_path or "").strip():
            api_doc = _read_text(api_doc_path.strip(), MAX_API_DOC_CHARS * 4, "api doc")[1][:MAX_API_DOC_CHARS]
        else:
            api_doc = kit_api_digest(kit_source)

    root = _output_root(rt, str(output_dir or ""), slug)
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    critique_text = critique_prompt(brief, max_tris=max_tris)
    vision_state = {"lock": threading.Lock(), "dead": "", "disabled": not vision}

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("mesh_generate timed out after %ds" % timeout)
        return left

    def generate(tier, prompt):
        return rt._generate_text(prompt, tier=tier, temperature=0.3, num_predict=GENERATE_NUM_PREDICT, num_ctx=GENERATE_NUM_CTX,
                                 timeout=int(min(remaining(), _GENERATE_TIMEOUT_CAP)))

    def build(index, round_index, code):
        left = remaining()
        if left < MIN_BUILD_SECONDS:
            raise TimeoutError("mesh_generate has under %ds left" % MIN_BUILD_SECONDS)
        stem = "c%d_r%d" % (index, round_index)
        code_path = root / (stem + ".py")
        code_path.write_text(code, encoding="utf-8")
        result = runner.build_candidate(
            blender, str(code_path), str(root / stem), name=slug,
            kit_path=str(kit_file) if kit_file else None, max_tris=max_tris, height_m=height,
            export=export, render=True, timeout=int(max(MIN_BUILD_SECONDS, min(left, _BUILD_TIMEOUT))),
        )
        files = dict(result.get("files") or {})
        files["code"] = str(code_path)
        return dict(result, files=files)

    def critique(sheet):
        with vision_state["lock"]:
            if vision_state["dead"]:
                return None
        try:
            return rt._vision_analyze_impl(sheet, critique_text, extra_roots=str(root))
        except Exception as exc:  # noqa: BLE001 - vision is optional; report why it is off
            with vision_state["lock"]:
                vision_state["dead"] = vision_state["dead"] or "%s: %s" % (type(exc).__name__, str(exc)[:200])
            return None

    def map_parallel(fn, items):
        def guarded(item):
            try:
                return fn(item)
            except Exception as exc:  # noqa: BLE001 - one candidate's crash must not sink the others
                return "%s: %s" % (type(exc).__name__, exc)

        workers = max(1, min(len(items), MAX_PARALLEL_BUILDS))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="meshgen") as pool:
            return list(pool.map(guarded, items))

    request = MeshRequest(
        brief=brief, max_tris=max_tris, height_m=height, kit_module=kit_module, kit_names=kit_names, api_doc=api_doc,
        candidates=candidates, rounds=rounds, tiers=tier_list, target_score=target_score, vision=bool(vision),
    )
    report = run_loop(request, generate=generate, build=build, critique=critique, map_parallel=map_parallel)
    if vision_state["dead"]:
        report["vision"] = "unavailable: %s" % vision_state["dead"]
    payload = {
        "ok": bool(report.get("ok")), "name": slug, "output_dir": str(root), "blender": blender,
        "vision": report["vision"], "elapsed_s": report["elapsed_s"],
        "winner": report.get("winner"),
        "candidates": [_candidate_summary(item) for item in report["candidates"]],
    }
    winner = report.get("winner")
    if winner:
        final = {}
        for key, suffix in (("model", ".%s" % export), ("sheet", "_sheet.png"), ("view", "_view.png"),
                            ("code", ".py")):
            source = (winner.get("files") or {}).get(key)
            if source and _inside(source, root) and os.path.isfile(source):
                target = root / (slug + suffix)
                shutil.copyfile(source, target)
                final[key] = str(target)
        payload["final"] = final
    else:
        payload["error"] = "no candidate produced a model; see candidates[].attempts for the failures"
    return payload


def register(mcp, record) -> None:
    """Register ``mesh_generate``; ``record`` is the server's ``_record_direct_tool``."""

    def run(name, args, body):
        started = time.time()
        try:
            payload = body()
        except SonderError as exc:
            payload = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a tool reports, it never crashes the server
            payload = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        output = _json(payload)
        record(name, args, ok=bool(payload.get("ok")), started=started,
               summary=str(payload.get("error") or "ok")[:200], output=output)
        return output

    @mcp.tool()
    def mesh_generate(brief: str, name: str = "", kit_path: str = "", api_doc_path: str = "",
                      max_tris: int = 3000, height_m: float = 0.0, candidates: int = 3, rounds: int = 3,
                      tiers: str = "general,code", vision: bool = True, target_score: int = 8,
                      export: str = "glb", output_dir: str = "", timeout: int = 1200) -> str:
        """Generate a low-poly 3D model (GLB/FBX) from a brief with a model-written Blender script. Runs candidates through build, render, vision critique and revision, and keeps the best. Needs Blender (SONDER_BLENDER); executes model-written Python like run_code."""
        args = {"brief": brief, "name": name, "kit_path": kit_path, "api_doc_path": api_doc_path,
                "max_tris": max_tris, "height_m": height_m, "candidates": candidates, "rounds": rounds,
                "tiers": tiers, "vision": vision, "target_score": target_score, "export": export,
                "output_dir": output_dir, "timeout": timeout}
        return run("mesh_generate", args, lambda: generate_mesh(**args))


__all__ = ["generate_mesh", "register"]
