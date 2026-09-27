"""Shared image preparation for user-provided vision inputs."""
from __future__ import annotations

import io
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

_MAX_LONG_EDGE = 1568
_SUPPORTED = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def prepare_image(path: str | Path, source: str = "user_file") -> tuple[bytes, str, dict]:
    """Return sanitized image bytes, MIME type, and non-sensitive metadata."""
    original = Path(path).expanduser().resolve()
    if not original.is_file():
        raise FileNotFoundError(f"Image not found: {original}")
    if original.suffix.lower() not in _SUPPORTED:
        raise ValueError("Supported image formats are PNG, JPEG, WebP, and GIF")

    with Image.open(original) as opened:
        frame = opened.convert("RGB")
        dimensions = frame.size
        frame.thumbnail((_MAX_LONG_EDGE, _MAX_LONG_EDGE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        frame.save(output, format="JPEG", quality=88, optimize=True)

    metadata = {
        "type": "image",
        "source": source,
        "path": str(original),
        "filename": original.name,
        "dimensions": list(dimensions),
        "mime": "image/jpeg",
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    return output.getvalue(), "image/jpeg", metadata
