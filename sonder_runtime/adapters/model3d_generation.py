"""Local Hunyuan3D-2 adapter + headless Blender studio preview renderer for Sonder chat & tools.

Generates real 3D meshes (.glb/.obj) from text prompts or local images via a loopback/HTTPS
Hunyuan3D API server (such as the local Blender-compatible Hunyuan3D-2 server on :8081),
optionally synthesising a crisp studio concept image via SONDER_IMAGE_API_URL (e.g. SDXL-Turbo)
so text-to-3D skips heavy T2I reloads, optionally yielding loopback llama-server VRAM before
inference, and rendering a 2x2 turnaround preview PNG via headless Blender (SONDER_BLENDER).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import io
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from sonder_runtime.adapters.inference import ollama_endpoint
from sonder_runtime.application.model3d_generation import (
    DEFAULT_3D_FACE_COUNT,
    DEFAULT_3D_OCTREE_RESOLUTION,
    DEFAULT_3D_STEPS,
    DEFAULT_3D_TIMEOUT_SECONDS,
    GeneratedModel3DResult,
    MAX_INLINE_PREVIEW_BYTES,
    Model3DBackend,
    format_model3d_markdown,
    is_safe_model_filename,
    parse_model3d_prompt,
    slug_from_3d_prompt,
)
import sonder_runtime.platform.paths as runtime_paths

logger = logging.getLogger(__name__)

_DEFAULT_HUNYUAN3D_URL = "http://127.0.0.1:8081"
_MAX_GLB_RESPONSE_BYTES = 128 * 1024 * 1024  # 128 MiB cap
_PROBE_TIMEOUT_SECONDS = 2.0
_PREVIEW_SCRIPT = Path(__file__).resolve().parent / "meshgen" / "preview_glb.py"
_DOWNLOAD_TICKET_KEY = secrets.token_bytes(32)
_DOWNLOAD_TICKET_TTL_SECONDS = 24 * 3600


def mint_model_download_ticket(filename: str, *, ttl_seconds: int = _DOWNLOAD_TICKET_TTL_SECONDS) -> str:
    """Mint a single-file, time-bounded HMAC-SHA256 download ticket for ``filename``."""
    if not is_safe_model_filename(filename):
        raise ValueError("invalid generated model filename")
    exp = int(time.time()) + max(60, int(ttl_seconds))
    sig = hmac.new(_DOWNLOAD_TICKET_KEY, f"{filename}:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def verify_model_download_ticket(filename: str, ticket: str) -> bool:
    """Verify a single-file HMAC-SHA256 download ticket in constant time."""
    if not is_safe_model_filename(filename) or not ticket or "." not in ticket:
        return False
    exp_s, _, sig = str(ticket).partition(".")
    if not exp_s.isdigit() or len(sig) != 32:
        return False
    exp = int(exp_s)
    if exp < int(time.time()):
        return False
    expected = hmac.new(_DOWNLOAD_TICKET_KEY, f"{filename}:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def attach_signed_download_url(markdown: str, filename: str, *, origin: str) -> str:
    """Upgrade a relative ``/v1/generated-models/<filename>`` link to a clickable signed URL."""
    base = str(origin or "").strip().rstrip("/")
    if not base or not is_safe_model_filename(filename):
        return markdown
    ticket = mint_model_download_ticket(filename)
    rel = f"(/v1/generated-models/{filename})"
    signed = f"({base}/v1/generated-models/{filename}?dl={ticket})"
    return markdown.replace(rel, signed)


def upgrade_markdown_download_links(markdown: str, *, origin: str) -> str:
    """Upgrade any ``(/v1/generated-models/<filename>)`` links in *markdown* to signed URLs."""
    if not origin or "(/v1/generated-models/" not in str(markdown or ""):
        return markdown
    import re

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if not is_safe_model_filename(name):
            return match.group(0)
        ticket = mint_model_download_ticket(name)
        return f"({origin.rstrip('/')}/v1/generated-models/{name}?dl={ticket})"

    return re.sub(r"\(/v1/generated-models/([a-z0-9][a-z0-9._-]{0,120}\.(?:glb|obj|png))\)", _repl, markdown)


def generated_models_dir() -> Path:
    """Persistent output directory for generated 3D models and their preview images."""
    state_home = os.environ.get("SONDER_STATE_HOME", "").strip()
    if state_home:
        return Path(state_home) / "generated-models"
    return runtime_paths.default_home() / "generated-models"


def resolve_generated_model_path(filename: str) -> Path | None:
    """Safely resolve a flat generated-model basename under ``generated_models_dir()``."""
    safe_name = os.path.basename(str(filename or "").strip())
    if not is_safe_model_filename(safe_name):
        return None
    root = os.path.realpath(str(generated_models_dir()))
    fullpath = os.path.normpath(os.path.join(root, safe_name))
    if not fullpath.startswith(root + os.sep):
        return None
    if os.path.dirname(fullpath) != root or not os.path.isfile(fullpath):
        return None
    return Path(fullpath)


def read_generated_model_bytes(filename: str) -> tuple[str, bytes] | None:
    """Safely read a generated model file under ``generated_models_dir()``."""
    safe_name = os.path.basename(str(filename or "").strip())
    if not is_safe_model_filename(safe_name):
        return None
    root = os.path.realpath(str(generated_models_dir()))
    fullpath = os.path.normpath(os.path.join(root, safe_name))
    if not fullpath.startswith(root + os.sep):
        return None
    if os.path.dirname(fullpath) != root or not os.path.isfile(fullpath):
        return None
    with open(fullpath, "rb") as handle:
        return safe_name, handle.read()


def _validate_api_url(raw: str, *, label: str = "SONDER_3D_API_URL") -> str:
    url = str(raw or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{label} must be an http(s) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not contain embedded credentials")
    if parsed.scheme == "http" and not ollama_endpoint.is_loopback(url):
        raise ValueError(f"Non-loopback {label} must use https")
    return url


def _loopback_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _probe_hunyuan3d(base_url: str, timeout: float = _PROBE_TIMEOUT_SECONDS) -> dict | None:
    opener = _loopback_opener()
    for path in ("/health", "/docs"):
        req = urllib.request.Request(f"{base_url}{path}", method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    raw = resp.read(64 * 1024)
                    if path == "/health":
                        try:
                            data = json.loads(raw.decode("utf-8", errors="replace"))
                            if isinstance(data, dict):
                                return data
                        except Exception:
                            pass
                    return {"status": "ok"}
        except Exception:
            continue
    return None


def discover_3d_backend() -> Model3DBackend:
    """Resolve the active local Hunyuan3D backend."""
    raw = os.environ.get("SONDER_3D_API_URL", "").strip()
    candidate = raw or _DEFAULT_HUNYUAN3D_URL
    try:
        base_url = _validate_api_url(candidate)
    except ValueError as exc:
        return Model3DBackend(
            kind="unavailable",
            endpoint="",
            model_id="hunyuan3d-2",
            detail=str(exc),
        )
    info = _probe_hunyuan3d(base_url)
    if info is not None:
        model_label = str(info.get("subfolder") or info.get("model") or "hunyuan3d-2mini").strip()
        return Model3DBackend(
            kind="hunyuan3d",
            endpoint=base_url,
            model_id=model_label,
            tex_enabled=bool(info.get("tex_enabled", True)),
            t23d_enabled=bool(info.get("t23d_enabled", True)),
            detail="healthy",
        )
    return Model3DBackend(
        kind="unavailable",
        endpoint=base_url,
        model_id="hunyuan3d-2",
        detail=f"Hunyuan3D server at {base_url} is not responding",
    )


def _yield_llamaserver_vram() -> list[str]:
    """Politely unload loaded models on a loopback llama-server router before heavy 3D GPU work."""
    raw = os.environ.get("SONDER_3D_YIELD_LLAMASERVER_URL", "").strip()
    if not raw:
        return []
    try:
        base_url = _validate_api_url(raw, label="SONDER_3D_YIELD_LLAMASERVER_URL")
    except ValueError:
        return []
    if not ollama_endpoint.is_loopback(base_url):
        return []
    opener = _loopback_opener()
    unloaded: list[str] = []
    try:
        with opener.open(urllib.request.Request(f"{base_url}/models", method="GET"), timeout=3.0) as resp:
            payload = json.loads(resp.read(512 * 1024).decode("utf-8", errors="replace"))
    except Exception:
        return []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        is_loaded = isinstance(status, dict) and status.get("value") == "loaded"
        model_id = str(entry.get("id") or "").strip()
        if not is_loaded or not model_id:
            continue
        req = urllib.request.Request(
            f"{base_url}/models/unload",
            data=json.dumps({"model": model_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(req, timeout=15.0) as resp:
                resp.read(16 * 1024)
            unloaded.append(model_id)
        except Exception as exc:
            logger.warning("Could not unload %s from %s before 3D generation: %s", model_id, base_url, exc)
    if unloaded:
        # Brief pause so CUDA driver releases VRAM pages before PyTorch allocates.
        time.sleep(0.5)
    return unloaded


def _studio_concept_prompt(prompt: str) -> str:
    return (
        f"{prompt}, single isolated 3D object, full view centered on a solid plain white studio background, "
        "clean neutral studio lighting, crisp silhouette, high detail, no text, no watermark, no background clutter"
    )


def _generate_concept_image_bytes(prompt: str) -> tuple[bytes, str] | None:
    """Use configured local fast image API (e.g. SDXL-Turbo) to synthesize a clean concept PNG."""
    if not os.environ.get("SONDER_IMAGE_API_URL", "").strip():
        return None
    import sonder_runtime.adapters.image_generation as _image_gen

    backend = _image_gen.discover_image_backend()
    if backend.kind not in ("openai_api", "image_api") or not backend.endpoint:
        return None
    try:
        raw, mime = _image_gen._call_openai_image_api(
            backend, _studio_concept_prompt(prompt), timeout=45.0,
        )
        if raw:
            return raw, mime or "image/png"
    except Exception as exc:
        logger.warning("Concept image generation failed; falling back to Hunyuan3D text pipeline: %s", exc)
    return None


def _compress_preview_for_inline(png_bytes: bytes) -> tuple[bytes, str]:
    """Ensure preview image fits well under Flutter's 2 MiB inline markdown limit."""
    if len(png_bytes) <= MAX_INLINE_PREVIEW_BYTES:
        return png_bytes, "image/png"
    try:
        pil_image = importlib.import_module("PIL.Image")

        with pil_image.open(io.BytesIO(png_bytes)) as img:
            rgb = img.convert("RGB")
            rgb.thumbnail((768, 768))
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=86, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        return png_bytes[:MAX_INLINE_PREVIEW_BYTES], "image/png"


def _find_blender_executable() -> str:
    configured = os.environ.get("SONDER_BLENDER", "").strip()
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("blender")
    return found or ""


def render_glb_preview(model_path: Path, sheet_path: Path, *, tile_px: int = 384, timeout: int = 90) -> dict:
    """Render a 2x2 studio turnaround PNG and extract mesh stats via headless Blender."""
    blender_exe = _find_blender_executable()
    if not blender_exe or not _PREVIEW_SCRIPT.is_file():
        return {"ok": False, "error": "Blender executable not configured (set SONDER_BLENDER)"}

    with tempfile.TemporaryDirectory(prefix="sonder-3d-preview-") as tmpdir:
        job_path = Path(tmpdir) / "job.json"
        result_path = Path(tmpdir) / "result.json"
        job_path.write_text(
            json.dumps({
                "model_path": str(model_path),
                "sheet_path": str(sheet_path),
                "result_path": str(result_path),
                "tile_px": int(tile_px),
            }),
            encoding="utf-8",
        )
        cmd = [
            blender_exe,
            "-b",
            "--factory-startup",
            "--disable-autoexec",
            "--python",
            str(_PREVIEW_SCRIPT),
            "--",
            str(job_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(15, int(timeout)),
                check=False,
            )
        except Exception as exc:
            return {"ok": False, "error": f"Blender preview failed: {exc}"}

        if result_path.is_file():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        stderr_tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return {"ok": False, "error": f"Blender exited {proc.returncode}: {stderr_tail}"}


def _read_guarded_input_image(image_path: str) -> bytes:
    import sonder_runtime.adapters.filesystem.file_ops as file_ops

    resolved = file_ops.require_read_access(image_path)
    if not resolved.is_file():
        raise FileNotFoundError(f"Input image not found: {resolved}")
    raw = resolved.read_bytes()
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise ValueError("Input image is empty or exceeds 20 MiB")
    return raw


def generate_chat_3d_model(
    prompt: str,
    *,
    image_path: str = "",
    texture: bool | None = None,
    steps: int | None = None,
    octree_resolution: int | None = None,
    face_count: int | None = None,
    seed: int | None = None,
    file_format: str = "",
    timeout: float | None = None,
) -> GeneratedModel3DResult:
    """Generate a 3D model via local Hunyuan3D-2, render a Blender studio preview, and return chat markdown."""
    started = time.perf_counter()
    parsed = parse_model3d_prompt(prompt)
    clean_prompt = parsed.prompt
    effective_image_path = str(image_path or parsed.image_path or "").strip()
    default_tex = os.environ.get("SONDER_3D_DEFAULT_TEXTURE", "1").strip().lower() not in ("0", "false", "no", "off")
    use_texture = texture if texture is not None else (parsed.texture if parsed.texture is not None else default_tex)
    eff_steps = steps if steps is not None else (parsed.steps if parsed.steps is not None else DEFAULT_3D_STEPS)
    eff_octree = (
        octree_resolution
        if octree_resolution is not None
        else (parsed.octree_resolution if parsed.octree_resolution is not None else DEFAULT_3D_OCTREE_RESOLUTION)
    )
    eff_faces = face_count if face_count is not None else (parsed.face_count if parsed.face_count is not None else DEFAULT_3D_FACE_COUNT)
    eff_format = (file_format or parsed.file_format or "glb").strip().lower().lstrip(".")
    if eff_format not in ("glb", "obj"):
        eff_format = "glb"
    if seed is not None:
        eff_seed = int(seed)
    elif parsed.seed is not None:
        eff_seed = parsed.seed
    else:
        digest = hashlib.sha256(f"{clean_prompt}:{time.time_ns()}".encode("utf-8")).digest()
        eff_seed = int.from_bytes(digest[:4], "little") & 0x7FFFFFFF

    eff_timeout = float(
        timeout
        if timeout is not None
        else os.environ.get("SONDER_3D_TIMEOUT_SECONDS", str(DEFAULT_3D_TIMEOUT_SECONDS))
    )

    backend = discover_3d_backend()
    if backend.kind != "hunyuan3d":
        raise RuntimeError(
            f"Local Hunyuan3D server is unavailable ({backend.detail}). "
            "Start the Hunyuan3D server or configure SONDER_3D_API_URL."
        )
    if use_texture and not backend.tex_enabled:
        use_texture = False

    out_dir = generated_models_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = slug_from_3d_prompt(clean_prompt)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = secrets.token_hex(3)
    stem = f"{slug}-{stamp}-{suffix}"
    model_filename = f"{stem}.{eff_format}"
    model_path = out_dir / model_filename
    preview_path = out_dir / f"{stem}_preview.png"
    concept_path = out_dir / f"{stem}_concept.png"

    concept_bytes: bytes | None = None
    concept_mime = "image/png"
    notes: list[str] = []

    if effective_image_path:
        concept_bytes = _read_guarded_input_image(effective_image_path)
    else:
        concept = _generate_concept_image_bytes(clean_prompt)
        if concept is not None:
            concept_bytes, concept_mime = concept
            concept_path.write_bytes(concept_bytes)
        elif not backend.t23d_enabled:
            raise RuntimeError(
                "Hunyuan3D server has --enable_t23d disabled and SONDER_IMAGE_API_URL is not configured "
                "for text-to-image concept synthesis."
            )

    payload: dict = {
        "texture": bool(use_texture),
        "num_inference_steps": int(eff_steps),
        "octree_resolution": int(eff_octree),
        "face_count": int(eff_faces),
        "seed": int(eff_seed),
        "type": eff_format,
    }
    if concept_bytes is not None:
        payload["image"] = base64.b64encode(concept_bytes).decode("ascii")
    else:
        payload["text"] = clean_prompt

    # Yield shared loopback LLM VRAM right before heavy Hunyuan3D GPU allocation.
    unloaded = _yield_llamaserver_vram()
    if unloaded:
        notes.append(f"Temporarily yielded GPU VRAM ({', '.join(unloaded)}; auto-reloads on next chat).")

    req = urllib.request.Request(
        f"{backend.endpoint}/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/octet-stream"},
        method="POST",
    )
    opener = _loopback_opener()
    try:
        with opener.open(req, timeout=max(30.0, eff_timeout)) as resp:
            glb_bytes = resp.read(_MAX_GLB_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        err_body = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"Hunyuan3D generation failed (HTTP {exc.code}): {err_body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Hunyuan3D request failed: {exc}") from exc

    if not glb_bytes:
        raise RuntimeError("Hunyuan3D returned an empty model payload")
    if len(glb_bytes) > _MAX_GLB_RESPONSE_BYTES:
        raise RuntimeError("Hunyuan3D model exceeds 128 MiB safety cap")

    model_path.write_bytes(glb_bytes)

    # Render a real 2x2 studio turnaround preview of the generated 3D model via headless Blender.
    preview_b64 = ""
    preview_mime = "image/png"
    tris = 0
    verts = 0
    materials = 0
    render_info = render_glb_preview(model_path, preview_path)
    if render_info.get("ok") and preview_path.is_file():
        raw_preview, preview_mime = _compress_preview_for_inline(preview_path.read_bytes())
        preview_b64 = base64.b64encode(raw_preview).decode("ascii")
        tris = int(render_info.get("tris") or 0)
        verts = int(render_info.get("verts") or 0)
        materials = int(render_info.get("materials") or 0)
    else:
        if render_info.get("error"):
            logger.warning("Blender 3D preview render skipped: %s", render_info["error"])
            notes.append("Rendered concept preview (Blender 2x2 turnaround unavailable).")

    concept_b64 = ""
    if not preview_b64 and concept_bytes:
        raw_concept, concept_mime = _compress_preview_for_inline(concept_bytes)
        concept_b64 = base64.b64encode(raw_concept).decode("ascii")

    elapsed = time.perf_counter() - started
    download_url = f"/v1/generated-models/{model_filename}"
    markdown = format_model3d_markdown(
        prompt=clean_prompt,
        model_filename=model_filename,
        download_url=download_url,
        model_path=str(model_path),
        model_bytes=len(glb_bytes),
        preview_b64=preview_b64,
        preview_mime=preview_mime,
        concept_b64=concept_b64,
        concept_mime=concept_mime,
        model_id=backend.model_id,
        textured=use_texture,
        tris=tris,
        verts=verts,
        elapsed_seconds=elapsed,
        note=" ".join(notes),
    )
    return GeneratedModel3DResult(
        markdown=markdown,
        model_id=backend.model_id,
        backend_kind=backend.kind,
        prompt=clean_prompt,
        model_path=str(model_path),
        model_filename=model_filename,
        download_url=download_url,
        preview_path=str(preview_path) if preview_path.is_file() else "",
        concept_path=str(concept_path) if concept_path.is_file() else "",
        model_bytes=len(glb_bytes),
        tris=tris,
        verts=verts,
        materials=materials,
        textured=use_texture,
        elapsed_seconds=elapsed,
        note=" ".join(notes),
    )
