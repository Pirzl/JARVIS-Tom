"""Generate or transform images with Gemini's image model."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path

from core import gemini
from core import undo
from memory.config_manager import get_gemini_key

_MODEL = "gemini-2.5-flash-image"
_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3"}
_QUOTA_COOLDOWN_SECONDS = 900
_quota_blocked_until = 0.0
_quota_lock = threading.Lock()


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (text[:50] or "image")


def _output_dir() -> Path:
    path = Path.home() / "Pictures" / "JARVIS"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _image_part(path: Path):
    from google.genai import types
    return types.Part.from_bytes(data=path.read_bytes(), mime_type="image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png")


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "QUOTA" in text


def _quota_is_blocked() -> bool:
    with _quota_lock:
        return time.monotonic() < _quota_blocked_until


def _block_quota() -> None:
    global _quota_blocked_until
    with _quota_lock:
        _quota_blocked_until = time.monotonic() + _QUOTA_COOLDOWN_SECONDS


def _quota_message() -> str:
    return (
        "Gemini image generation is currently out of quota (429). "
        "Jarvis will not retry it for 15 minutes. Wait for the quota to reset "
        "or use an API key/project with image-generation billing enabled. "
        "The AFC notice is an SDK warning, not the cause of this failure."
    )


def generate_image(parameters: dict, player=None, speak=None) -> str:
    prompt = str(parameters.get("prompt", "")).strip()
    if not prompt:
        return "Please provide a description for the image."
    ratio = str(parameters.get("aspect_ratio", "1:1")).strip()
    if ratio not in _ASPECT_RATIOS:
        ratio = "1:1"
    source = str(parameters.get("edit_source", "")).strip()
    source_path = Path(source).expanduser().resolve() if source else None
    if source_path and not source_path.is_file():
        return f"Edit source not found: {source}"

    api_key = get_gemini_key()
    if not api_key:
        return "Image generation is unavailable because the Gemini API key is not configured."
    if _quota_is_blocked():
        return _quota_message()

    try:
        from google.genai import types

        contents = []
        if source_path:
            contents.append(_image_part(source_path))
            contents.append(f"Edit or transform the supplied image: {prompt}")
        else:
            contents.append(prompt)
        client = gemini.client(timeout_ms=60000, key=api_key)
        response = client.models.generate_content(
            model=_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=ratio),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        image_data = None
        for part in response.parts or []:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                image_data = part.inline_data.data
                break
        if not image_data:
            return "Gemini returned no image. Try a more specific visual description."

        output = _output_dir() / f"{datetime.now():%Y-%m-%d-%H%M%S}-{_slug(prompt)}.png"
        output.write_bytes(image_data)
        undo.push_undo(f"generated image {output.name}", lambda: _remove(output))
        if player and hasattr(player, "show_content"):
            player.show_content("GENERATED IMAGE", f"Saved to: {output}\n\nPrompt: {prompt}")
        return f"Generated image saved to {output}. It is ready to view."
    except Exception as exc:
        if _is_quota_error(exc):
            _block_quota()
            return _quota_message()
        return f"Image generation failed: {exc}"


def _remove(path: Path) -> str:
    if path.exists():
        path.unlink()
        return f"Removed {path.name}."
    return "The generated image was already removed."


TOOL = {
    "name": "generate_image",
    "description": (
        "Generate an image from a visual prompt, or edit an image supplied by the "
        "user with edit_source. Use for illustrations, diagrams, mockups, and "
        "visualising descriptions. Do not generate images of real named people. "
        "After success, report the saved path briefly."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {"type": "STRING", "description": "What the image should show."},
            "aspect_ratio": {"type": "STRING", "description": "1:1, 16:9, 9:16, or 4:3."},
            "edit_source": {"type": "STRING", "description": "Optional path to an image to transform."},
        },
        "required": ["prompt"],
    },
    "handler": generate_image,
}
