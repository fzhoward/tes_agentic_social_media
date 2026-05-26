"""Creatomate render API wrapper.

Submits a render against a Creatomate template, polls for completion, and
downloads the resulting file to a local output path. This tool is fully
isolated from the Drafter's prompt-assembly and template-selection logic —
it just executes a render and reports the result.

Two public entry points:

  - ``build_modifications(image_fields, text_fields)`` — convert the
    Drafter's per-element values into the Creatomate ``modifications``
    payload shape.

  - ``render_template(template_id, modifications, output_path)`` — submit a
    render, poll until done or timeout, and download the result. Never
    raises; always returns ``{"success": bool, ...}``.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests
from dotenv import load_dotenv


load_dotenv(override=False)


_API_BASE_URL = "https://api.creatomate.com/v1"
_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
_RETRY_DELAY_SECONDS = 2.0
_HTTP_TIMEOUT_SUBMIT = 30
_HTTP_TIMEOUT_POLL = 30
_HTTP_TIMEOUT_DOWNLOAD = 120


def _failure(error: str, render_id: str = "") -> dict:
    return {
        "success": False,
        "output_path": "",
        "render_id": render_id,
        "error": error,
    }


def build_modifications(
    image_fields: dict[str, str] | None,
    text_fields: dict[str, str] | None,
) -> dict:
    """Build a Creatomate ``modifications`` dict from Drafter inputs.

    Args:
        image_fields: ``{element_name: image_url}`` mappings. Each becomes
            ``{element_name: {"source": image_url}}``.
        text_fields: ``{element_name: text_value}`` mappings. Each becomes
            ``{element_name: {"text": text_value}}``.

    Returns:
        Dict suitable to pass as ``modifications`` to ``render_template()``.
    """
    result: dict[str, dict[str, Any]] = {}
    for name, url in (image_fields or {}).items():
        result[name] = {"source": url}
    for name, value in (text_fields or {}).items():
        result[name] = {"text": value}
    return result


def _modifications_to_payload(modifications: dict) -> dict[str, Any]:
    """Convert ``{name: {field: value, ...}}`` to Creatomate's flat object form.

    Creatomate's render API expects ``modifications`` as an object where keys
    are either ``"Element-Name"`` (the default property) or
    ``"Element-Name.property"`` (a specific property like ``source`` or
    ``text``). We flatten the nested-dict input we accept from the Drafter
    into that dotted form.
    """
    payload: dict[str, Any] = {}
    for name, fields in modifications.items():
        if isinstance(fields, dict):
            for field, value in fields.items():
                payload[f"{name}.{field}"] = value
        else:
            payload[name] = fields
    return payload


def render_template(
    template_id: str,
    modifications: dict,
    output_path: str,
    poll_interval: float = 2.0,
    max_wait: float = 120.0,
) -> dict:
    """Render a Creatomate template and download the result.

    Args:
        template_id: Creatomate template UUID.
        modifications: ``{element_name: {field: value}}`` dict
            (as produced by ``build_modifications``).
        output_path: local path to write the rendered file to.
        poll_interval: seconds between status polls (default ``2.0``).
        max_wait: maximum seconds to wait before timing out (default ``120.0``).

    Returns:
        ``{"success": bool, "output_path": str, "render_id": str, "error": str}``.
    """
    api_key = os.environ.get("CREATOMATE_API_KEY")
    if not api_key:
        return _failure("CREATOMATE_API_KEY is not set in the environment")

    if not template_id:
        return _failure("template_id is empty")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "template_id": template_id,
        "modifications": _modifications_to_payload(modifications or {}),
    }

    # --- Step 1: submit render ---
    submit_url = f"{_API_BASE_URL}/renders"
    submit_resp: requests.Response | None = None
    submit_error = ""
    for attempt in range(2):
        try:
            submit_resp = requests.post(
                submit_url,
                json=payload,
                headers=headers,
                timeout=_HTTP_TIMEOUT_SUBMIT,
            )
        except Exception as exc:
            submit_error = f"submit network error: {type(exc).__name__}: {exc}"
            if attempt == 0:
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            return _failure(submit_error)

        if submit_resp.status_code in _TRANSIENT_HTTP_STATUSES and attempt == 0:
            submit_error = (
                f"submit HTTP {submit_resp.status_code}: "
                f"{submit_resp.text[:200]}"
            )
            time.sleep(_RETRY_DELAY_SECONDS)
            continue
        break

    if submit_resp is None:
        return _failure(submit_error or "submit failed with no response")
    if submit_resp.status_code >= 400:
        return _failure(
            f"submit HTTP {submit_resp.status_code}: "
            f"{submit_resp.text[:500]}"
        )

    try:
        body = submit_resp.json()
    except Exception as exc:
        return _failure(
            f"submit response not JSON: {type(exc).__name__}: {exc}"
        )

    first = body[0] if isinstance(body, list) else body
    if not isinstance(first, dict):
        return _failure(f"unexpected submit response shape: {body!r}")

    render_id = str(first.get("id", "")).strip()
    if not render_id:
        return _failure(f"submit response missing render id: {first!r}")

    # --- Step 2: poll for completion ---
    poll_url = f"{_API_BASE_URL}/renders/{render_id}"
    deadline = time.monotonic() + max_wait
    poll_body: dict[str, Any] = {}
    while True:
        if time.monotonic() > deadline:
            return _failure(
                f"render did not complete within {max_wait}s",
                render_id=render_id,
            )

        try:
            poll_resp = requests.get(
                poll_url,
                headers=headers,
                timeout=_HTTP_TIMEOUT_POLL,
            )
        except Exception as exc:
            # Transient network error on poll — wait and retry without
            # resetting the deadline.
            time.sleep(poll_interval)
            continue

        if poll_resp.status_code in _TRANSIENT_HTTP_STATUSES:
            time.sleep(poll_interval)
            continue
        if poll_resp.status_code >= 400:
            return _failure(
                f"poll HTTP {poll_resp.status_code}: "
                f"{poll_resp.text[:300]}",
                render_id=render_id,
            )

        try:
            poll_body = poll_resp.json()
        except Exception as exc:
            return _failure(
                f"poll response not JSON: {type(exc).__name__}: {exc}",
                render_id=render_id,
            )

        if isinstance(poll_body, list):
            poll_body = poll_body[0] if poll_body else {}
        if not isinstance(poll_body, dict):
            return _failure(
                f"unexpected poll response shape: {poll_body!r}",
                render_id=render_id,
            )

        status = str(poll_body.get("status", "")).lower()
        if status == "succeeded":
            break
        if status == "failed":
            err_msg = (
                poll_body.get("error_message")
                or poll_body.get("error")
                or "render failed"
            )
            return _failure(
                f"render failed: {err_msg}",
                render_id=render_id,
            )

        time.sleep(poll_interval)

    # --- Step 3: download the rendered file ---
    file_url = poll_body.get("url", "")
    if not file_url:
        return _failure(
            "render succeeded but response had no url",
            render_id=render_id,
        )

    try:
        dl_resp = requests.get(file_url, timeout=_HTTP_TIMEOUT_DOWNLOAD)
        dl_resp.raise_for_status()
    except Exception as exc:
        return _failure(
            f"download failed: {type(exc).__name__}: {exc}",
            render_id=render_id,
        )

    try:
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(output_path, "wb") as out:
            out.write(dl_resp.content)
    except Exception as exc:
        return _failure(
            f"failed to write output file: {type(exc).__name__}: {exc}",
            render_id=render_id,
        )

    return {
        "success": True,
        "output_path": output_path,
        "render_id": render_id,
        "error": "",
    }
