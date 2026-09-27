"""Attach a user-provided image to the current Live exchange."""
from __future__ import annotations

import json
from pathlib import Path

from core.images import prepare_image


def see_file(parameters: dict, player=None, speak=None) -> str:
    path = str(parameters.get("path", "")).strip()
    if not path:
        return "No image path was provided."
    try:
        _, _, metadata = prepare_image(path, source="user_file")
        question = str(parameters.get("question", "What is in this image?"))
        return "[IMAGE_FILE_ACTIVE] " + json.dumps(
            {"metadata": metadata, "question": question}, ensure_ascii=True
        )
    except Exception as exc:
        return f"Could not attach image: {exc}"


TOOL = {
    "name": "see_file",
    "description": (
        "Attach and inspect a user-provided image file. Use when the user says "
        "look at, inspect, read, or analyze an image path. Supported formats are "
        "PNG, JPEG, WebP, and GIF. The image is attached to this same exchange; "
        "do not answer until it arrives."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {"type": "STRING", "description": "Path to the image file."},
            "question": {"type": "STRING", "description": "What to inspect in the image."},
        },
        "required": ["path"],
    },
    "handler": see_file,
}
