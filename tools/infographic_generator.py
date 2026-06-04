"""Google Gemini ("Nano Banana 2") infographic generation wrapper.

Thin wrapper around the google-genai SDK's ``client.models.generate_content()``
endpoint for the ``gemini-3.1-flash-image`` model. Accepts a fully-assembled
infographic prompt (text only — NO source image) and a local output path,
writes the generated image to disk, and returns a structured result dict.

This tool is the media path for no-focus-equipment rows (Local Connection,
Educational Tip) that would otherwise dead-end media-less. Unlike
``tools/image_generator.py`` (OpenAI gpt-image-2, image-edit shape), this is a
pure text-to-image generation against a different provider with a different
SDK call shape.

This tool never raises — all failure modes (missing key, SDK import failure,
client init failure, transient SDK errors, persistent model-access errors,
empty/refused responses, write failures) come back as
``{"success": False, "error": ...}`` so the calling agent can degrade
gracefully and continue with other tasks.

JPEG-native note: Nano Banana 2 returns a NATIVE JPEG (not PNG). The SDK's
``part.as_image().save()`` writes the raw bytes with no re-encode, so the
file extension MUST match the actual mime type. This tool reads the response
mime, picks the correct extension, writes to that path, and returns the ACTUAL
path written (so downstream upload uses the real file).
"""

from __future__ import annotations

import os
import time


from dotenv import load_dotenv


load_dotenv(override=False)


_RETRY_DELAY_SECONDS = 2.0
_IMAGE_MODEL = "gemini-3.1-flash-image"

# Mime → file extension. Nano Banana 2 returns native JPEG; PNG/WebP are
# mapped defensively in case the model ever returns them.
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_FALLBACK_EXTENSION = ".bin"


def _failure(error: str) -> dict:
    return {"success": False, "output_path": "", "error": error}


def _classify_error(exc: BaseException) -> str:
    """Classify an SDK exception by its string (no HTTP statuses on this path).

    Returns one of:
      - "access"    persistent model-access failure (permission/billing/not
                    found). Do NOT retry — the key likely lacks paid-tier
                    access.
      - "retry"     rate-limit (resource_exhausted/quota/429) or transient
                    (connection/timeout). Retry once.
      - "other"     anything else. Do NOT retry.
    """
    text = f"{type(exc).__name__}: {exc}".lower()

    access_markers = (
        "permission",
        "permission_denied",
        "billing",
        "not found",
        "not_found",
        "403",
        "404",
    )
    if any(marker in text for marker in access_markers):
        return "access"

    retry_markers = (
        "resource_exhausted",
        "quota",
        "429",
        "rate limit",
        "rate_limit",
        "connection",
        "timeout",
        "timed out",
        "unavailable",
    )
    if any(marker in text for marker in retry_markers):
        return "retry"

    return "other"


def _extension_for_mime(mime_type: str) -> str:
    return _MIME_EXTENSIONS.get((mime_type or "").lower(), _FALLBACK_EXTENSION)


def generate_infographic(prompt: str, output_path: str) -> dict:
    """Generate an infographic image from a text prompt using Gemini.

    Args:
        prompt: fully-assembled infographic prompt. The caller builds the
            prompt — this tool does NOT build prompts.
        output_path: local path to write to. The extension is NOT trusted;
            this tool appends/corrects the extension to match the returned
            mime type and returns the real path in ``output_path``.

    Returns:
        ``{"success": bool, "output_path": str, "error": str}``.
    """
    # Key handling gotcha: the .env stores the key mixed-case as
    # ``Gemini_API_KEY`` and os.getenv is case-sensitive. Read the canonical
    # name first, fall back to the mixed-case name, and pass it explicitly.
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("Gemini_API_KEY")
    if not api_key:
        return _failure(
            "Gemini API key is not set (checked GEMINI_API_KEY and "
            "Gemini_API_KEY) in the environment"
        )

    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        return _failure(
            f"google-genai SDK import failed: {type(exc).__name__}: {exc}"
        )

    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:
        return _failure(
            f"Gemini client init failed: {type(exc).__name__}: {exc}"
        )

    # Candidate A — the only generation config shape confirmed to work.
    # 1:1 / 1K yields 1024x1024 (square only for v1).
    try:
        config = types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="1:1", image_size="1K"),
        )
    except Exception as exc:
        return _failure(
            f"failed to build Gemini config: {type(exc).__name__}: {exc}"
        )

    last_error = ""
    for attempt in range(2):  # 1 initial + 1 retry
        try:
            response = client.models.generate_content(
                model=_IMAGE_MODEL,
                contents=[prompt],
                config=config,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            category = _classify_error(exc)
            if category == "access":
                return _failure(
                    f"Gemini model access denied (the API key likely lacks "
                    f"paid-tier access): {last_error}"
                )
            if attempt == 0 and category == "retry":
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            return _failure(last_error)

        # Image extraction (validated): prefer response.parts, fall back to
        # response.candidates[0].content.parts when parts is empty.
        try:
            parts = list(getattr(response, "parts", None) or [])
            if not parts:
                candidates = getattr(response, "candidates", None) or []
                if candidates:
                    content = getattr(candidates[0], "content", None)
                    parts = list(getattr(content, "parts", None) or [])
        except Exception as exc:
            return _failure(
                f"failed to read Gemini response parts: "
                f"{type(exc).__name__}: {exc}"
            )

        image_part = None
        first_text = ""
        for part in parts:
            if getattr(part, "inline_data", None) is not None:
                image_part = part
                break
            if not first_text and getattr(part, "text", None):
                first_text = part.text

        if image_part is None:
            msg = "Gemini returned no image part"
            if first_text:
                # Often a refusal — surface the leading text for diagnosis.
                msg = f"{msg} (text: {first_text[:200]})"
            return _failure(msg)

        # Save with the mime-correct extension. Nano Banana 2 returns native
        # JPEG; as_image().save() writes raw bytes with no re-encode, so the
        # extension must match the actual mime or the file is mislabeled.
        try:
            mime_type = getattr(image_part.inline_data, "mime_type", "")
            extension = _extension_for_mime(mime_type)
            base, _ = os.path.splitext(output_path)
            real_output_path = base + extension

            parent = os.path.dirname(real_output_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            image_part.as_image().save(real_output_path)
        except Exception as exc:
            return _failure(
                f"failed to write output file: {type(exc).__name__}: {exc}"
            )

        # Cost telemetry — log usage metadata when present (the agreed
        # substitute for rate-limit headers, which the SDK does not surface).
        # Never fail if it's absent.
        try:
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                print(f"[infographic_generator] usage_metadata: {usage}")
        except Exception:
            pass

        return {"success": True, "output_path": real_output_path, "error": ""}

    return _failure(last_error or "unknown failure")
