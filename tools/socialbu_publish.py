"""SocialBu publisher tool.

Publishes an approved Content Queue row to Facebook, Instagram, or Google
Business Profile via the SocialBu API, then writes the result back to the
Content Queue sheet.

Public entry point: ``publish_row(row, config, dry_run=False)``.

The SocialBu API contract is fixed and pre-validated — this tool does not
probe or discover endpoints. See Session 2F-A prompt for the validated
shape of POST /posts and the required ``Accept: application/json`` header
quirk.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


load_dotenv(override=False)


_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 1.0
_HTTP_TIMEOUT = 30

_VIDEO_KEY = "video"
_IMAGE_KEY = "image"

_DRIVE_DOWNLOAD_URL_TEMPLATE = "https://lh3.googleusercontent.com/d/{file_id}"


def _get_account_id(platform: str, config: Any) -> int:
    """Map a Content Queue platform name to its SocialBu integer account ID.

    Raises:
        ValueError: if the platform is unknown or not enabled in config.
    """
    if not platform:
        raise ValueError("platform is empty")

    active = config.get("platforms.active", []) or []
    if platform not in active:
        raise ValueError(
            f"platform '{platform}' is not in platforms.active "
            f"(active: {active})"
        )

    try:
        account_block = config.get(f"platforms.accounts.{platform}")
    except KeyError as exc:
        raise ValueError(
            f"platform '{platform}' has no entry under platforms.accounts"
        ) from exc

    if not isinstance(account_block, dict):
        raise ValueError(
            f"platforms.accounts.{platform} is not a mapping: {account_block!r}"
        )

    if not account_block.get("enabled", False):
        raise ValueError(f"platform '{platform}' is not enabled in config")

    account_id = account_block.get("account_id")
    if account_id in (None, ""):
        raise ValueError(
            f"platforms.accounts.{platform}.account_id is missing"
        )

    try:
        return int(account_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"platforms.accounts.{platform}.account_id must be an integer-like "
            f"value, got {account_id!r}"
        ) from exc


def _convert_scheduled_datetime(iso_str: str) -> str:
    """Convert ISO 8601 datetime to SocialBu's ``Y-m-d H:i:s`` UTC format.

    Inputs without a timezone are treated as UTC. Inputs with a timezone
    offset are converted to UTC.
    """
    if not iso_str:
        raise ValueError("scheduled_datetime is empty")

    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _resolve_media_url(raw: str) -> str:
    """Return a publicly fetchable URL for a Content Queue ``media_url`` value.

    Strings that already start with ``http`` pass through. Otherwise the
    value is treated as a Google Drive file ID and converted to a
    ``lh3.googleusercontent.com`` direct-serving URL. This format returns
    raw image bytes on GET without redirects or interstitials, which is
    required by SocialBu's media fetcher.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return _DRIVE_DOWNLOAD_URL_TEMPLATE.format(file_id=raw)


def _media_payload_key(media_format_used: str) -> str:
    """Return ``"video"`` for video formats, ``"image"`` otherwise."""
    fmt = (media_format_used or "").lower()
    return _VIDEO_KEY if "video" in fmt else _IMAGE_KEY


def _build_payload(row: dict, config: Any) -> dict:
    """Assemble the SocialBu POST /posts request body for a Content Queue row."""
    platform = row.get("platform", "")
    account_id = _get_account_id(platform, config)
    publish_at = _convert_scheduled_datetime(row.get("scheduled_datetime", ""))

    payload: dict[str, Any] = {
        "content": row.get("caption", ""),
        "accounts": [account_id],
        "publish_at": publish_at,
    }

    first_comment = (row.get("first_comment") or "").strip()
    if first_comment:
        payload["first_comment"] = first_comment

    media_url = _resolve_media_url(row.get("media_url", ""))
    if media_url:
        payload[_media_payload_key(row.get("media_format_used", ""))] = media_url

    return payload


def _validate_before_publish(row: dict, config: Any) -> str | None:
    """Pre-flight checks. Returns an error string, or None when OK."""
    status = (row.get("status") or "").strip()
    if status != "approved":
        return f"row status is '{status}', must be 'approved' to publish"

    platform = (row.get("platform") or "").strip()
    if not platform:
        return "row.platform is empty"

    active = config.get("platforms.active", []) or []
    if platform not in active:
        return f"platform '{platform}' is not in platforms.active ({active})"

    if not (row.get("scheduled_datetime") or "").strip():
        return "row.scheduled_datetime is empty"

    if not (row.get("caption") or "").strip():
        return "row.caption is empty"

    if platform == "instagram" and not (row.get("media_url") or "").strip():
        return "instagram posts require non-empty media_url"

    return None


def _post_to_socialbu(payload: dict, api_base_url: str) -> dict:
    """POST a payload to SocialBu with retry on transient HTTP failures.

    Returns:
        ``{"success": bool, "status_code": int|None, "body": dict|None,
        "error": str|None}``
    """
    api_key = os.environ.get("SOCIALBU_API_KEY")
    if not api_key:
        return {
            "success": False,
            "status_code": None,
            "body": None,
            "error": "SOCIALBU_API_KEY is not set in the environment",
        }

    url = f"{api_base_url.rstrip('/')}/posts"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    delay = _BASE_DELAY_SECONDS
    last_error = ""
    last_status: int | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = f"network error: {type(exc).__name__}: {exc}"
            last_status = None
            if attempt < _MAX_ATTEMPTS:
                print(
                    f"[socialbu_publish] network error on attempt {attempt}; "
                    f"retrying in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                delay *= 2
                continue
            return {
                "success": False,
                "status_code": None,
                "body": None,
                "error": last_error,
            }

        status = resp.status_code
        last_status = status

        if status in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
            last_error = f"HTTP {status}: {resp.text[:200]}"
            print(
                f"[socialbu_publish] transient HTTP {status} on attempt {attempt}; "
                f"retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
            delay *= 2
            continue

        if status >= 400:
            return {
                "success": False,
                "status_code": status,
                "body": None,
                "error": f"HTTP {status}: {resp.text[:500]}",
            }

        try:
            body = resp.json()
        except ValueError as exc:
            return {
                "success": False,
                "status_code": status,
                "body": None,
                "error": f"response not JSON: {exc}",
            }

        return {
            "success": True,
            "status_code": status,
            "body": body,
            "error": None,
        }

    return {
        "success": False,
        "status_code": last_status,
        "body": None,
        "error": last_error or "retries exhausted",
    }


def _extract_post_id(response_body: Any) -> str | None:
    if not isinstance(response_body, dict):
        return None
    posts = response_body.get("posts")
    if not isinstance(posts, list) or not posts:
        return None
    first = posts[0]
    if not isinstance(first, dict):
        return None
    post_id = first.get("id")
    if post_id in (None, ""):
        return None
    return str(post_id)


def publish_row(row: dict, config: Any, *, dry_run: bool = False) -> dict:
    """Publish a single approved Content Queue row to SocialBu.

    Args:
        row: Content Queue row dict.
        config: loaded business config (Config object or dict-like).
        dry_run: if True, build and return the payload without calling the API.

    Returns:
        Dict with keys: ``success`` (bool), ``socialbu_post_id`` (str|None),
        ``published_datetime`` (str|None), ``error`` (str|None),
        ``payload`` (dict).
    """
    result: dict[str, Any] = {
        "success": False,
        "socialbu_post_id": None,
        "published_datetime": None,
        "error": None,
        "payload": {},
    }

    validation_error = _validate_before_publish(row, config)
    if validation_error:
        result["error"] = validation_error
        return result

    try:
        payload = _build_payload(row, config)
    except Exception as exc:
        result["error"] = f"failed to build payload: {type(exc).__name__}: {exc}"
        return result

    result["payload"] = payload

    if dry_run:
        result["success"] = True
        return result

    api_base_url = config.get("publishing.api_base_url")
    api_result = _post_to_socialbu(payload, api_base_url)

    if not api_result["success"]:
        result["error"] = api_result["error"]
        return result

    post_id = _extract_post_id(api_result["body"])
    if not post_id:
        result["error"] = (
            f"SocialBu response missing post id: {api_result['body']!r}"
        )
        return result

    result["success"] = True
    result["socialbu_post_id"] = post_id
    result["published_datetime"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Publish an approved Content Queue row to SocialBu.",
    )
    parser.add_argument(
        "--row-id",
        required=True,
        help="row_id of the Content Queue row to publish",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build payload and print it without calling SocialBu",
    )
    args = parser.parse_args()

    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    config_env = os.environ.get("BUSINESS_CONFIG_PATH")
    config_path = config_env or str(PROJECT_ROOT / "business_config_tes_rentals.yaml")

    from tools.config_loader import load_config
    from tools import sheets_helpers

    config = load_config(config_path)
    sheet_id = config.get("drive.content_queue_sheet_id")

    service = sheets_helpers.get_sheets_service()
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(title))",
    ).execute()
    tab_name = meta["sheets"][0]["properties"]["title"]

    matches = sheets_helpers.find_rows_by_column_value(
        sheet_id, tab_name, "row_id", args.row_id, service=service,
    )
    if not matches:
        print(json.dumps({
            "success": False,
            "error": f"row_id '{args.row_id}' not found in Content Queue",
        }, indent=2))
        return 1

    row_number, row = matches[0]
    result = publish_row(row, config, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))

    if not args.dry_run and result["success"]:
        sheets_helpers.update_cells(
            sheet_id,
            tab_name,
            row_number,
            {
                "status": "published",
                "published_datetime": result["published_datetime"],
                "socialbu_post_id": result["socialbu_post_id"],
            },
            service=service,
        )

    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
