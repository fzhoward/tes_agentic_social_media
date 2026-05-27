"""OpenAI image generation wrapper.

Thin wrapper around the OpenAI Python SDK's ``client.images.edit()`` endpoint
for the gpt-image-2 model. Accepts a fully-assembled prompt and a local
source image path, writes the generated PNG to a local output path, and
returns a structured result dict.

This tool never raises — all failure modes (missing key, missing source
file, transient HTTP errors, persistent API errors, decode failures) come
back as ``{"success": False, "error": ...}`` so the calling agent can
degrade gracefully and continue with other tasks.
"""

from __future__ import annotations

import base64
import io
import os
import time
from typing import Any

from dotenv import load_dotenv


load_dotenv(override=False)


_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
_RETRY_DELAY_SECONDS = 2.0
_IMAGE_MODEL = "gpt-image-2"

# Preprocessing limits. Phone photos straight off an iPhone routinely exceed
# the OpenAI Image API's input ceiling — the API returns a 400 with
# "Invalid image file or mode" on oversized files. We resize + recompress to
# stay under those limits before posting.
_MAX_SIDE_PX = 2048
_PRIMARY_QUALITY = 85
_FALLBACK_QUALITY = 70
_SIZE_BUDGET_BYTES = 4 * 1024 * 1024  # 4 MB


def _failure(error: str) -> dict:
    return {"success": False, "output_path": "", "error": error}


def _status_code_of(exc: BaseException) -> Any:
    return (
        getattr(exc, "status_code", None)
        or getattr(exc, "http_status", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
    )


def _preprocess_for_openai(source_image_path: str) -> io.BytesIO:
    """Resize + recompress a source image so the OpenAI Image API accepts it.

    1. Convert non-RGB modes (RGBA, palette, grayscale) to RGB so the result
       can be JPEG-encoded.
    2. If the longest side exceeds ``_MAX_SIDE_PX``, scale the image down
       proportionally.
    3. Encode as JPEG at quality 85 into a BytesIO buffer.
    4. If the encoded result exceeds ``_SIZE_BUDGET_BYTES``, re-encode at
       quality 70.

    The returned buffer is positioned at the start and has ``.name`` set so
    the OpenAI SDK can derive the multipart filename / MIME type.
    """
    from PIL import Image  # local import — Pillow listed in requirements.txt

    img = Image.open(source_image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")

    longest = max(img.size)
    if longest > _MAX_SIDE_PX:
        scale = _MAX_SIDE_PX / longest
        new_size = (
            max(1, int(round(img.size[0] * scale))),
            max(1, int(round(img.size[1] * scale))),
        )
        img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_PRIMARY_QUALITY, optimize=True)
    if buf.tell() > _SIZE_BUDGET_BYTES:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_FALLBACK_QUALITY, optimize=True)

    buf.seek(0)
    buf.name = "source.jpg"
    return buf


def generate_image(
    source_image_path: str,
    prompt: str,
    output_path: str,
    size: str = "1024x1536",
) -> dict:
    """Generate an image from a source photo using OpenAI gpt-image-2.

    Args:
        source_image_path: local path to the source photo.
        prompt: fully-assembled image prompt (preamble + section).
        output_path: local path to write the generated image to.
        size: output dimensions string (default ``"1024x1536"`` for 4:5).

    Returns:
        ``{"success": bool, "output_path": str, "error": str}``.
    """
    if not source_image_path or not os.path.isfile(source_image_path):
        return _failure(f"source image not found: {source_image_path!r}")

    if not os.environ.get("OPENAI_API_KEY"):
        return _failure("OPENAI_API_KEY is not set in the environment")

    try:
        from openai import OpenAI
    except Exception as exc:
        return _failure(f"openai SDK import failed: {type(exc).__name__}: {exc}")

    try:
        client = OpenAI()
    except Exception as exc:
        return _failure(
            f"OpenAI client init failed: {type(exc).__name__}: {exc}"
        )

    try:
        image_buffer = _preprocess_for_openai(source_image_path)
    except Exception as exc:
        return _failure(
            f"failed to preprocess source image: "
            f"{type(exc).__name__}: {exc}"
        )

    last_error = ""
    for attempt in range(2):  # 1 initial + 1 retry
        try:
            image_buffer.seek(0)
            response = client.images.edit(
                model=_IMAGE_MODEL,
                image=image_buffer,
                prompt=prompt,
                size=size,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 0 and _status_code_of(exc) in _TRANSIENT_HTTP_STATUSES:
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            return _failure(last_error)

        try:
            b64 = response.data[0].b64_json
            if not b64:
                return _failure("OpenAI response had empty b64_json")
            payload = base64.b64decode(b64)
        except Exception as exc:
            return _failure(
                f"failed to decode response image data: "
                f"{type(exc).__name__}: {exc}"
            )

        try:
            parent = os.path.dirname(output_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(output_path, "wb") as out:
                out.write(payload)
        except Exception as exc:
            return _failure(
                f"failed to write output file: "
                f"{type(exc).__name__}: {exc}"
            )

        return {"success": True, "output_path": output_path, "error": ""}

    return _failure(last_error or "unknown failure")
