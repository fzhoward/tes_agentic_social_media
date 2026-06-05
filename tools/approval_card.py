"""Slack approval card builder and poster.

Reads Content Queue rows whose status is ``awaiting_approval``, renders a
Slack Block Kit message for each, and posts to the ``#approvals`` channel
(configured under ``approval.slack_channel``).

The card is a delivery artifact only — it carries enough context for the
owner to make a call, plus five action buttons that fire through Make.com
to ``tools.approval_router.handle_action``.

This tool also exposes ``reschedule_missed_approvals``, which scans for
overdue approval rows, slides each one 24h forward, and auto-rejects after
``approval.auto_reject_after_misses`` consecutive misses.

Public entry points:
    build_approval_blocks(row, config) -> (fallback_text, blocks)
    post_approval_card(row, config) -> Slack API response
    post_pending_approvals(config, *, dry_run=False) -> list[dict]
    reschedule_missed_approvals(config, *, dry_run=False) -> list[dict]
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tools import sheets_helpers, slack_helpers


_PLATFORM_BADGES = {
    "facebook": "🔵 Facebook",
    "instagram": "📸 Instagram",
    "gbp": "📍 GBP",
}

_MAX_CAPTION_LEN = 2900
_MAX_FIRST_COMMENT_LEN = 200
_MAX_WARNING_TEXT_LEN = 280
_CHANNEL_HISTORY_LIMIT = 50

# Slack mrkdwn section text caps at 3000 chars; stop well short so the
# assembled warning block can never trip the API limit.
_MAX_WARNING_BLOCK_LEN = 2800

_RESCHEDULE_TAG_RE = re.compile(r"\[RESCHEDULED:\s*(\d+)\]")


def _media_image_url(row_id: str, base_url: str) -> str | None:
    """Build the signed executor media URL for a row, or None if not buildable.

    Mirrors tools/executor.py::_sign_media_token exactly (same secret env var,
    same HMAC-SHA256, same 32-char hex truncation). Returns None when the
    secret or base_url is missing so the caller can omit the image block.
    """
    secret = os.environ.get("MEDIA_URL_SECRET", "")
    if not secret or not base_url or not row_id:
        return None
    token = hmac.new(secret.encode(), row_id.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{base_url.rstrip('/')}/media/{row_id}/{token}"


def _platform_badge(platform: str) -> str:
    key = (platform or "").strip().lower()
    return _PLATFORM_BADGES.get(key, platform or "—")


def _format_scheduled(iso_str: str) -> str:
    if not iso_str:
        return "—"
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a %Y-%m-%d %H:%M UTC")


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _parse_critic_notes(raw: Any) -> dict | None:
    """Parse the row's ``critic_notes`` JSON string defensively.

    Returns the parsed dict, or ``None`` on any missing/empty/malformed
    value so the card can never raise while rendering the Critic block.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _critic_block(row: dict) -> dict | None:
    """Build the Critic section block from a row, or ``None`` to omit it.

    Reads the structured Critic output from ``critic_notes`` (JSON) and the
    verdict from ``critic_score``. Renders flagged warnings (with their
    fix instructions) for the owner to review, a clean line when the Critic
    ran with no warnings, or nothing at all when the Critic never ran.
    """
    critic_score = (row.get("critic_score") or "").strip()
    notes = _parse_critic_notes(row.get("critic_notes"))

    if notes is None:
        # Critic output absent or unparseable. Only claim "clean" if the
        # Critic demonstrably ran (a score is present); otherwise omit.
        if not critic_score:
            return None
        return {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✅ *Critic: clean* (score: {critic_score})",
            },
        }

    warnings = notes.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = []

    if not warnings:
        if not critic_score:
            return None
        return {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✅ *Critic: clean* (score: {critic_score})",
            },
        }

    score_suffix = f" (score: {critic_score})" if critic_score else ""
    header = (
        f"⚠️ *Critic — {len(warnings)} flagged for your review*{score_suffix}"
    )
    lines: list[str] = [header]
    truncated = False
    for w in warnings:
        if not isinstance(w, dict):
            continue
        check_id = str(w.get("check_id", "") or "").strip() or "—"
        description = _truncate(
            str(w.get("description", "") or "").strip(),
            _MAX_WARNING_TEXT_LEN,
        )
        bullet = f"• *{check_id}* — {description}"
        fix = _truncate(
            str(w.get("fix_instruction", "") or "").strip(),
            _MAX_WARNING_TEXT_LEN,
        )
        candidate = bullet + (f"\n    ↳ _{fix}_" if fix else "")
        # Stop before the assembled text could exceed Slack's section limit.
        if (
            sum(len(ln) + 1 for ln in lines) + len(candidate) + 1
            > _MAX_WARNING_BLOCK_LEN
        ):
            truncated = True
            break
        lines.append(candidate)

    if truncated:
        lines.append("…(more warnings — see sheet)")

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    }


def build_approval_blocks(row: dict, config: Any) -> tuple[str, list[dict]]:
    """Build the (fallback_text, blocks) tuple for a Slack approval card."""
    base_url = config.get("approval.media_base_url", "")
    row_id = row.get("row_id", "")
    platform = row.get("platform", "")
    content_type = row.get("content_type", "")
    badge = _platform_badge(platform)
    scheduled = _format_scheduled(row.get("scheduled_datetime", ""))
    objective = (row.get("objective") or "").strip()
    caption = _truncate(row.get("caption", ""), _MAX_CAPTION_LEN)

    fallback_text = f"Approval needed: [{row_id}] {badge} — {content_type}"

    header_text = f"📋 {row_id} — {badge} — {content_type}".strip()

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        }
    ]

    schedule_lines: list[str] = [f"🗓 *Scheduled:* {scheduled}"]
    if objective:
        schedule_lines.append(f"🎯 *Objective:* {objective}")
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(schedule_lines)},
    })

    if caption:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": caption},
        })

    critic_block = _critic_block(row)
    if critic_block is not None:
        blocks.append(critic_block)

    media_url_id = (row.get("media_url") or "").strip()
    image_url = _media_image_url(row.get("row_id", ""), base_url) if media_url_id else None
    if image_url:
        blocks.append({
            "type": "image",
            "image_url": image_url,
            "alt_text": f"Generated media for {row.get('row_id', '')}".strip(),
        })

    media_lines: list[str] = []
    media_format_used = (row.get("media_format_used") or "").strip()
    if media_format_used:
        media_lines.append(f"*Media format:* {media_format_used}")
    cta_text = (row.get("cta_text") or "").strip()
    if cta_text:
        media_lines.append(f"*CTA:* {cta_text}")
    first_comment = (row.get("first_comment") or "").strip()
    if first_comment:
        media_lines.append(
            f"*First comment:* {_truncate(first_comment, _MAX_FIRST_COMMENT_LEN)}"
        )
    creative_hook_text = (row.get("creative_hook_text") or "").strip()
    if creative_hook_text:
        media_lines.append(f"*Hook text:* {creative_hook_text}")
    if media_lines:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(media_lines)},
        })

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                "style": "primary",
                "action_id": f"approve::{row_id}",
                "value": row_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                "style": "danger",
                "action_id": f"reject::{row_id}",
                "value": row_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✏️ Edit Caption", "emoji": True},
                "action_id": f"edit_caption::{row_id}",
                "value": row_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔄 Regen Media", "emoji": True},
                "action_id": f"regen_media::{row_id}",
                "value": row_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔁 Regen All", "emoji": True},
                "action_id": f"regen_all::{row_id}",
                "value": row_id,
            },
        ],
    })

    blocks.append({"type": "divider"})

    return fallback_text, blocks


def _card_exists_for_row(channel: str, row_id: str) -> bool:
    """Return True if a recent channel message references ``row_id``."""
    if not row_id:
        return False
    try:
        history = slack_helpers.get_channel_history(
            channel, limit=_CHANNEL_HISTORY_LIMIT,
        )
    except Exception:
        return False
    for msg in history:
        text = msg.get("text", "") or ""
        if row_id in text:
            return True
        blocks = msg.get("blocks") or []
        if row_id in json.dumps(blocks):
            return True
    return False


def post_approval_card(row: dict, config: Any) -> dict:
    """Build and post an approval card for a single row.

    Skips posting (returns a ``{"ok": False, "skipped": True}`` dict) if a
    card for ``row_id`` already exists in recent channel history.
    """
    channel = config.get("approval.slack_channel")
    row_id = row.get("row_id", "")

    if _card_exists_for_row(channel, row_id):
        return {
            "ok": False,
            "skipped": True,
            "reason": "card already posted",
            "row_id": row_id,
        }

    fallback_text, blocks = build_approval_blocks(row, config)
    response = slack_helpers.post_message(channel, fallback_text, blocks=blocks)

    ts = response.get("ts") if isinstance(response, dict) else None
    if ts:
        # Best-effort: persist the card's Slack ts onto the row so threaded
        # replies (Edit Caption) can be mapped back via thread_ts. A failure
        # here must never sink the post — the post succeeding is the contract.
        try:
            sheet_id = config.get("drive.content_queue_sheet_id")
            service = sheets_helpers.get_sheets_service()
            tab_name = _resolve_tab_name(sheet_id, service)
            matches = sheets_helpers.find_rows_by_column_value(
                sheet_id, tab_name, "row_id", row_id, service=service,
            )
            if not matches:
                print(
                    f"[approval_card] slack_message_ts writeback: "
                    f"no row found for {row_id}",
                    file=sys.stderr,
                )
            else:
                row_number, _ = matches[0]
                sheets_helpers.update_cells(
                    sheet_id, tab_name, row_number,
                    {"slack_message_ts": ts},
                    service=service,
                )
        except Exception as exc:
            print(
                f"[approval_card] slack_message_ts writeback failed for "
                f"{row_id}: {exc}",
                file=sys.stderr,
            )

    return response


def _resolve_tab_name(sheet_id: str, service: Any) -> str:
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(title))",
    ).execute()
    return meta["sheets"][0]["properties"]["title"]


def post_pending_approvals(config: Any, *, dry_run: bool = False) -> list[dict]:
    """Find all awaiting_approval rows and post cards for any that don't
    already have one in the channel."""
    sheet_id = config.get("drive.content_queue_sheet_id")
    service = sheets_helpers.get_sheets_service()
    tab_name = _resolve_tab_name(sheet_id, service)

    rows = sheets_helpers.read_rows_filtered(
        sheet_id, tab_name, "status", "awaiting_approval", service=service,
    )

    results: list[dict] = []
    for row in rows:
        row_id = row.get("row_id", "")
        if dry_run:
            fallback_text, blocks = build_approval_blocks(row, config)
            results.append({
                "row_id": row_id,
                "dry_run": True,
                "fallback_text": fallback_text,
                "blocks": blocks,
            })
            continue
        response = post_approval_card(row, config)
        results.append({
            "row_id": row_id,
            "ok": response.get("ok"),
            "skipped": response.get("skipped", False),
            "ts": response.get("ts"),
        })
    return results


# ---------------------------------------------------------------------------
# Missed-approval rescheduler
# ---------------------------------------------------------------------------

def _parse_reschedule_count(draft_notes: str) -> int:
    if not draft_notes:
        return 0
    matches = _RESCHEDULE_TAG_RE.findall(draft_notes)
    if not matches:
        return 0
    try:
        return int(matches[-1])
    except (ValueError, TypeError):
        return 0


def _update_reschedule_count(draft_notes: str, new_count: int) -> str:
    tag = f"[RESCHEDULED: {new_count}]"
    if not draft_notes:
        return tag
    if _RESCHEDULE_TAG_RE.search(draft_notes):
        return _RESCHEDULE_TAG_RE.sub(tag, draft_notes)
    return f"{draft_notes} {tag}".strip()


def _parse_iso(scheduled: str) -> datetime | None:
    if not scheduled:
        return None
    s = scheduled.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def reschedule_missed_approvals(
    config: Any,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    """Find overdue awaiting_approval rows; reschedule +24h or auto-reject.

    A row is considered "missed" when its ``scheduled_datetime`` has passed
    while still at ``awaiting_approval`` status. Each miss increments a
    ``[RESCHEDULED: N]`` counter embedded in ``draft_notes``. When the new
    counter would meet ``approval.auto_reject_after_misses`` (default 3),
    the row is auto-rejected instead.

    Returns one dict per row describing what was done (or would be done in
    dry-run mode). Rows still in the future are skipped silently.
    """
    sheet_id = config.get("drive.content_queue_sheet_id")
    error_channel = config.get("approval.error_channel")
    auto_reject_after = int(config.get("approval.auto_reject_after_misses", 3))

    service = sheets_helpers.get_sheets_service()
    tab_name = _resolve_tab_name(sheet_id, service)

    matches = sheets_helpers.find_rows_by_column_value(
        sheet_id, tab_name, "status", "awaiting_approval", service=service,
    )

    current_time = now or datetime.now(timezone.utc)
    results: list[dict] = []

    for row_number, row in matches:
        row_id = row.get("row_id", "")
        scheduled_dt = _parse_iso(row.get("scheduled_datetime", ""))
        if scheduled_dt is None or scheduled_dt >= current_time:
            continue

        current_count = _parse_reschedule_count(row.get("draft_notes", ""))
        new_count = current_count + 1

        if new_count >= auto_reject_after:
            result = {
                "row_id": row_id,
                "action": "auto_reject",
                "miss_count": new_count,
            }
            if dry_run:
                result["dry_run"] = True
                results.append(result)
                continue
            sheets_helpers.update_cells(
                sheet_id, tab_name, row_number,
                {
                    "status": "rejected",
                    "rejection_reason": (
                        f"Auto-rejected: {auto_reject_after} missed approval windows"
                    ),
                },
                service=service,
            )
            try:
                slack_helpers.post_message(
                    error_channel,
                    f"⚠️ Auto-rejected {row_id} after {auto_reject_after} "
                    f"missed approval windows.",
                )
            except Exception as exc:
                result["slack_error"] = str(exc)
            results.append(result)
            continue

        new_scheduled = (current_time + timedelta(hours=24)).isoformat()
        new_notes = _update_reschedule_count(
            row.get("draft_notes", ""), new_count,
        )
        result = {
            "row_id": row_id,
            "action": "reschedule",
            "miss_count": new_count,
            "new_scheduled_datetime": new_scheduled,
        }
        if dry_run:
            result["dry_run"] = True
            results.append(result)
            continue
        sheets_helpers.update_cells(
            sheet_id, tab_name, row_number,
            {
                "scheduled_datetime": new_scheduled,
                "draft_notes": new_notes,
            },
            service=service,
        )
        refreshed = dict(row)
        refreshed["scheduled_datetime"] = new_scheduled
        refreshed["draft_notes"] = new_notes
        try:
            post_approval_card(refreshed, config)
        except Exception as exc:
            result["card_error"] = str(exc)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Post Slack approval cards for awaiting_approval rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build cards and print JSON without posting to Slack",
    )
    parser.add_argument(
        "--row-id",
        default=None,
        help="post a card for a single row only (default: all pending)",
    )
    parser.add_argument(
        "--reschedule",
        action="store_true",
        help="scan for overdue awaiting_approval rows, slide each +24h, "
             "and auto-reject after the configured miss limit",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    config_path = os.environ.get("BUSINESS_CONFIG_PATH") or str(
        project_root / "business_config_tes_rentals.yaml"
    )

    from tools.config_loader import load_config

    config = load_config(config_path)

    if args.reschedule:
        results = reschedule_missed_approvals(config, dry_run=args.dry_run)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    if args.row_id:
        sheet_id = config.get("drive.content_queue_sheet_id")
        service = sheets_helpers.get_sheets_service()
        tab_name = _resolve_tab_name(sheet_id, service)
        matches = sheets_helpers.find_rows_by_column_value(
            sheet_id, tab_name, "row_id", args.row_id, service=service,
        )
        if not matches:
            print(
                json.dumps(
                    {"success": False, "error": f"row_id {args.row_id!r} not found"},
                    indent=2,
                )
            )
            return 1
        _, row = matches[0]
        if args.dry_run:
            fallback_text, blocks = build_approval_blocks(row, config)
            print(json.dumps(
                {"row_id": args.row_id, "fallback_text": fallback_text, "blocks": blocks},
                indent=2, ensure_ascii=False,
            ))
            return 0
        response = post_approval_card(row, config)
        print(json.dumps(
            {
                "row_id": args.row_id,
                "ok": response.get("ok"),
                "skipped": response.get("skipped", False),
                "ts": response.get("ts"),
            },
            indent=2,
        ))
        return 0

    results = post_pending_approvals(config, dry_run=args.dry_run)
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
