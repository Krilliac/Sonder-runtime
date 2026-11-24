"""Media generation slash and natural-language chat dispatch with sanitized error handling."""
from __future__ import annotations

import logging
from typing import Any, Callable


def format_media_generation_error(
    kind: str,
    exc: Exception,
    *,
    logger: logging.Logger,
) -> str:
    """Log media generation failures server-side and return a sanitized chat message."""
    logger.warning("Chat %s generation failed (%s)", kind, type(exc).__name__, exc_info=True)
    lowered = str(exc or "").lower()
    if kind == "3d":
        if "unavailable" in lowered or "not responding" in lowered or "connection refused" in lowered:
            return (
                "3D model generation is currently unavailable because the local Hunyuan3D "
                "service is not running. Start the Hunyuan3D server or check "
                "`SONDER_3D_API_URL`, then try again."
            )
        if "budget" in lowered or "cap" in lowered or "exceed" in lowered:
            return (
                "3D model generation produced a payload exceeding the configured safety "
                "size limit. Try a simpler prompt or lower face count."
            )
        return (
            "3D model generation failed before a model could be produced. "
            "Check the server logs for details and try again."
        )
    if kind == "gif":
        return (
            "GIF generation failed before an animation could be produced. "
            "Check the server logs for details and try again."
        )
    if "inline byte budget" in lowered or "byte limit" in lowered:
        return (
            "Image generation produced an image exceeding the inline size budget. "
            "Try again or configure a smaller output resolution."
        )
    return (
        "Image generation failed before an image could be produced. "
        "Check the server logs for details and try again."
    )


def run_media_slash(
    kind: str,
    prompt: str,
    generator: Callable[[str], Any],
    *,
    logger: logging.Logger,
) -> str:
    """Execute a media slash command and return markdown or a sanitized error string."""
    try:
        result = generator(prompt)
        return str(result.markdown)
    except (RuntimeError, ValueError, OSError) as exc:
        return format_media_generation_error(kind, exc, logger=logger)


def run_chat_media_route(
    *,
    prompt: str,
    model: str,
    allow_media_route: bool,
    explicit_image_model: bool,
    intents: Any,
    image_generation: Any,
    model3d_generation: Any,
    logger: logging.Logger,
) -> tuple[str | None, str, str]:
    """Dispatch natural-language 3D, GIF, or image chat requests safely."""
    model3d_prompt = intents.classify_3d_model_generation(prompt) if allow_media_route else None
    gif_prompt = (
        None
        if model3d_prompt
        else (intents.classify_gif_generation(prompt) if (explicit_image_model or allow_media_route) else None)
    )
    if model3d_prompt:
        try:
            res = model3d_generation.generate_chat_3d_model(model3d_prompt)
            return str(res.markdown), str(res.model_id), "3d"
        except (RuntimeError, ValueError, OSError) as exc:
            return format_media_generation_error("3d", exc, logger=logger), model, "3d"
    if gif_prompt:
        try:
            res = image_generation.generate_chat_gif(
                gif_prompt,
                model_override=model if explicit_image_model else "",
            )
            return str(res.markdown), str(res.model_id), "image"
        except (RuntimeError, ValueError, OSError) as exc:
            return format_media_generation_error("gif", exc, logger=logger), model, "image"
    image_prompt = (
        prompt
        if explicit_image_model
        else (intents.classify_image_generation(prompt) if allow_media_route else None)
    )
    if image_prompt:
        try:
            res = image_generation.generate_chat_image(
                image_prompt,
                model_override=model if explicit_image_model else "",
            )
            return str(res.markdown), str(res.model_id), "image"
        except (RuntimeError, ValueError, OSError) as exc:
            return format_media_generation_error("image", exc, logger=logger), model, "image"
    return None, model, ""
