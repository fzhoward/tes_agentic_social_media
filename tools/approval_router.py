"""Slack interactive payload router for approval card actions.

Make.com forwards Slack ``block_actions`` payloads to this tool. The router
parses ``action_id``, verifies the target Content Queue row is still at
``awaiting_approval``, and dispatches one of five handlers:

    approve       publish via SocialBu
    reject        mark rejected, ask for reason in-thread
    edit_caption  reset to drafted (Critic re-runs after manual caption edit)
    regen_media   reset to planned, clear only media fields
    regen_all     reset to planned, clear all Drafter + Critic fields

All five handlers reply in-thread on the original approval card and add a
status reaction (✅, ❌, ✏️, 🔄, 🔁). Publish failures revert the row to
``awaiting_approval`` and post a notice to ``approval.error_channel``.

Public entry point: ``handle_action(payload, config)``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools import sheets_helpers, slack_helpers, socialbu_publish


# Fields the Drafter writes — cleared by regen_all.
DRAFTER_FIELDS: list[str] = [
    "caption",
    "creative_hook_text",
    "first_comment",
    "cta_text",
    "hook_text",
    "image_overlay_text",
    "media_url",
    "media_format_used",
    "draft_rationale",
    "revision_round",
]

# Fields the Critic writes — also cleared by regen_all.
CRITIC_FIELDS: list[str] = [
    "critic_score",
    "critic_notes",
]


def parse_action_id(action_id: str) -> tuple[str, str]:
    """Split a Slack ``action_id`` of the form ``"<action>::<row_id>"``."""
    if not action_id or "::" not in action_id:
        return ("", "")
    action_type, _, row_id = action_id.partition("::")
    return (action_type, row_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_tab_name(sheet_id: str, service: Any) -> str:
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(title))",
    ).execute()
    return meta["sheets"][0]["properties"]["title"]


def _find_row(config: Any, row_id: str):
    """Return (sheet_id, tab_name, service, row_number, row) for ``row_id``.

    ``row_number`` and ``row`` are ``None`` if no match exists.
    """
    sheet_id = config.get("drive.content_queue_sheet_id")
    service = sheets_helpers.get_sheets_service()
    tab_name = _resolve_tab_name(sheet_id, service)
    matches = sheets_helpers.find_rows_by_column_value(
        sheet_id, tab_name, "row_id", row_id, service=service,
    )
    if not matches:
        return sheet_id, tab_name, service, None, None
    row_number, row = matches[0]
    return sheet_id, tab_name, service, row_number, row


def _thread_reply(payload: dict, text: str) -> None:
    channel = (payload.get("channel") or {}).get("id")
    ts = (payload.get("message") or {}).get("ts")
    if not channel or not ts:
        return
    try:
        slack_helpers.post_message(channel, text, thread_ts=ts)
    except Exception as exc:
        print(f"[approval_router] thread reply failed: {exc}", file=sys.stderr)


def _add_reaction(payload: dict, emoji: str) -> None:
    channel = (payload.get("channel") or {}).get("id")
    ts = (payload.get("message") or {}).get("ts")
    if not channel or not ts:
        return
    try:
        slack_helpers.add_reaction(channel, ts, emoji)
    except Exception as exc:
        print(f"[approval_router] reaction failed: {exc}", file=sys.stderr)


def _post_error(config: Any, action: str, row_id: str, detail: str) -> None:
    error_channel = config.get("approval.error_channel")
    try:
        slack_helpers.post_message(
            error_channel,
            (
                f"⚠️ Approval router error\n"
                f"row_id: `{row_id}`\n"
                f"action: `{action}`\n"
                f"error: {detail}\n"
                f"timestamp: {_now_iso()}"
            ),
        )
    except Exception as exc:
        print(f"[approval_router] error post failed: {exc}", file=sys.stderr)


def _already_processed(payload: dict, action: str, row_id: str, status: str) -> dict:
    _thread_reply(
        payload,
        f"Row `{row_id}` already processed (status=`{status}`). No action taken.",
    )
    return {
        "action": action,
        "row_id": row_id,
        "success": False,
        "message": f"status is {status!r}, not awaiting_approval",
        "error": "Row already processed",
    }


def _row_not_found(action: str, row_id: str) -> dict:
    return {
        "action": action,
        "row_id": row_id,
        "success": False,
        "message": "",
        "error": f"row_id {row_id!r} not found in Content Queue",
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _publish_and_resolve(
    payload: dict,
    config: Any,
    row_id: str,
    sheet_id,
    tab_name,
    service,
    row_number,
    row,
    action: str = "approve",
) -> dict:
    """Publish a row via SocialBu and resolve the sheet + Slack side effects.

    Owns everything from the SocialBu publish onward, shared by the approve
    handler and the ``--edit-commit`` flow. The ``action`` label is carried
    through to the returned dict and the error post so each caller is
    attributed correctly. ``payload`` may be an empty dict: the reaction and
    thread helpers no-op when it lacks ``channel``/``message`` keys.
    """
    publish_result = socialbu_publish.publish_row(row, config)

    if publish_result.get("success"):
        sheets_helpers.update_cells(
            sheet_id, tab_name, row_number,
            {
                "status": "published",
                "socialbu_post_id": publish_result.get("socialbu_post_id") or "",
                "published_datetime": publish_result.get("published_datetime") or "",
            },
            service=service,
        )
        socialbu_publish.update_catalog_usage(
            focus_equipment_id=str(row.get("focus_equipment_id", "")),
            published_datetime=publish_result.get("published_datetime") or "",
            config=config,
            service=service,
        )
        _add_reaction(payload, "white_check_mark")
        return {
            "action": action,
            "row_id": row_id,
            "success": True,
            "message": (
                f"Published. socialbu_post_id="
                f"{publish_result.get('socialbu_post_id')}"
            ),
            "error": None,
        }

    error_detail = publish_result.get("error") or "publish failed"
    sheets_helpers.update_cells(
        sheet_id, tab_name, row_number,
        {"status": "awaiting_approval", "approved_datetime": ""},
        service=service,
    )
    _thread_reply(payload, f"Publish failed for `{row_id}`: {error_detail}")
    _post_error(config, action, row_id, error_detail)
    return {
        "action": action,
        "row_id": row_id,
        "success": False,
        "message": "Publish failed; status reverted to awaiting_approval",
        "error": error_detail,
    }


def _handle_approve(payload: dict, config: Any, row_id: str) -> dict:
    sheet_id, tab_name, service, row_number, row = _find_row(config, row_id)
    if row is None:
        return _row_not_found("approve", row_id)

    status = (row.get("status") or "").strip()
    if status != "awaiting_approval":
        return _already_processed(payload, "approve", row_id, status)

    approved_at = _now_iso()
    sheets_helpers.update_cells(
        sheet_id, tab_name, row_number,
        {"status": "approved", "approved_datetime": approved_at},
        service=service,
    )

    row["status"] = "approved"
    row["approved_datetime"] = approved_at

    return _publish_and_resolve(
        payload, config, row_id,
        sheet_id, tab_name, service, row_number, row,
    )


def _handle_reject(payload: dict, config: Any, row_id: str) -> dict:
    sheet_id, tab_name, service, row_number, row = _find_row(config, row_id)
    if row is None:
        return _row_not_found("reject", row_id)

    status = (row.get("status") or "").strip()
    if status != "awaiting_approval":
        return _already_processed(payload, "reject", row_id, status)

    user_name = (payload.get("user") or {}).get("name") or "unknown"

    sheets_helpers.update_cells(
        sheet_id, tab_name, row_number,
        {
            "status": "rejected",
            "rejection_reason": f"Rejected by {user_name}",
        },
        service=service,
    )

    _thread_reply(
        payload,
        "Why are you rejecting this post? Reply in this thread.",
    )
    _add_reaction(payload, "x")

    return {
        "action": "reject",
        "row_id": row_id,
        "success": True,
        "message": f"Rejected by {user_name}",
        "error": None,
    }


def _handle_edit_caption(payload: dict, config: Any, row_id: str) -> dict:
    # The threaded caption reply is captured by the /slack/events endpoint
    # (S44 Piece 2b), which commits the edit straight to publish — the row
    # already passed the Critic, and a human edit at Gate 2 is trusted, so
    # there is no re-review. Resetting to ``drafted`` here marks the row as
    # in the edit flow so --edit-commit will accept the captured caption.
    sheet_id, tab_name, service, row_number, row = _find_row(config, row_id)
    if row is None:
        return _row_not_found("edit_caption", row_id)

    status = (row.get("status") or "").strip()
    if status != "awaiting_approval":
        return _already_processed(payload, "edit_caption", row_id, status)

    sheets_helpers.update_cells(
        sheet_id, tab_name, row_number,
        {"status": "drafted"},
        service=service,
    )

    _thread_reply(
        payload,
        (
            "Reply in this thread with the revised caption. "
            "I'll apply it and publish — no re-review."
        ),
    )
    _add_reaction(payload, "pencil2")

    return {
        "action": "edit_caption",
        "row_id": row_id,
        "success": True,
        "message": "Status reset to drafted; awaiting caption revision",
        "error": None,
    }


def _handle_regen_media(payload: dict, config: Any, row_id: str) -> dict:
    sheet_id, tab_name, service, row_number, row = _find_row(config, row_id)
    if row is None:
        return _row_not_found("regen_media", row_id)

    status = (row.get("status") or "").strip()
    if status != "awaiting_approval":
        return _already_processed(payload, "regen_media", row_id, status)

    sheets_helpers.update_cells(
        sheet_id, tab_name, row_number,
        {
            "status": "planned",
            "media_url": "",
            "media_format_used": "",
        },
        service=service,
    )

    _thread_reply(payload, "Media regeneration queued. Caption preserved.")
    _add_reaction(payload, "arrows_counterclockwise")

    return {
        "action": "regen_media",
        "row_id": row_id,
        "success": True,
        "message": "Status reset to planned; media cleared, caption preserved",
        "error": None,
    }


def _handle_regen_all(payload: dict, config: Any, row_id: str) -> dict:
    sheet_id, tab_name, service, row_number, row = _find_row(config, row_id)
    if row is None:
        return _row_not_found("regen_all", row_id)

    status = (row.get("status") or "").strip()
    if status != "awaiting_approval":
        return _already_processed(payload, "regen_all", row_id, status)

    updates: dict[str, str] = {"status": "planned"}
    for field in DRAFTER_FIELDS + CRITIC_FIELDS:
        updates[field] = ""

    sheets_helpers.update_cells(
        sheet_id, tab_name, row_number, updates, service=service,
    )

    _thread_reply(
        payload,
        "Full regeneration queued. Drafter will re-run from scratch.",
    )
    _add_reaction(payload, "repeat")

    return {
        "action": "regen_all",
        "row_id": row_id,
        "success": True,
        "message": "Status reset to planned; all Drafter + Critic fields cleared",
        "error": None,
    }


_HANDLERS = {
    "approve": _handle_approve,
    "reject": _handle_reject,
    "edit_caption": _handle_edit_caption,
    "regen_media": _handle_regen_media,
    "regen_all": _handle_regen_all,
}


def handle_action(payload: dict, config: Any) -> dict:
    """Route a Slack interactive payload to the matching handler."""
    actions = payload.get("actions") or []
    if not actions:
        return {
            "action": "",
            "row_id": "",
            "success": False,
            "message": "",
            "error": "payload has no actions",
        }

    first = actions[0]
    action_id = first.get("action_id") or ""
    action_type, row_id = parse_action_id(action_id)

    if not row_id:
        row_id = first.get("value") or ""

    if not action_type:
        return {
            "action": "",
            "row_id": row_id,
            "success": False,
            "message": "",
            "error": f"could not parse action_id: {action_id!r}",
        }

    handler = _HANDLERS.get(action_type)
    if handler is None:
        return {
            "action": action_type,
            "row_id": row_id,
            "success": False,
            "message": "",
            "error": f"unknown action: {action_type!r}",
        }

    return handler(payload, config, row_id)


# ---------------------------------------------------------------------------
# Edit-commit (driven by the /slack/events endpoint via CLI)
# ---------------------------------------------------------------------------

def _edit_commit(config: Any, row_id: str, caption_text: str) -> dict:
    """Commit a captured caption edit: drafted -> approved -> publish.

    Invoked by the Slack events endpoint after it captures a threaded caption
    reply. The row must currently be at ``drafted`` (the state Edit Caption
    set); any other status is refused so a caption can't be force-committed
    onto a row outside the edit flow. The Critic is intentionally bypassed —
    the row already passed it and the human edit at Gate 2 is trusted.
    """
    sheet_id, tab_name, service, row_number, row = _find_row(config, row_id)
    if row is None:
        return _row_not_found("edit_commit", row_id)

    status = (row.get("status") or "").strip()
    if status != "drafted":
        return {
            "action": "edit_commit",
            "row_id": row_id,
            "success": False,
            "message": (
                f"status is {status!r}, not 'drafted'; refusing to commit "
                f"caption edit"
            ),
            "error": "Row not in edit flow",
        }

    approved_at = _now_iso()
    sheets_helpers.update_cells(
        sheet_id, tab_name, row_number,
        {
            "caption": caption_text,
            "status": "approved",
            "approved_datetime": approved_at,
        },
        service=service,
    )

    row["caption"] = caption_text
    row["status"] = "approved"
    row["approved_datetime"] = approved_at

    return _publish_and_resolve(
        {}, config, row_id,
        sheet_id, tab_name, service, row_number, row,
        action="edit_commit",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Route a Slack interactive payload (block_actions) "
                    "to the matching approval handler.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--payload",
        help="Slack block_actions payload as a JSON string",
    )
    mode.add_argument(
        "--edit-commit",
        action="store_true",
        help="commit a captured caption edit (status drafted -> approved) "
             "and publish via the shared helper; requires --row-id and "
             "--caption-text",
    )
    parser.add_argument(
        "--row-id",
        default=None,
        help="row_id for --edit-commit",
    )
    parser.add_argument(
        "--caption-text",
        default=None,
        help="revised caption text for --edit-commit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse the payload and print the would-be action without "
             "calling Sheets / Slack / SocialBu",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    config_path = os.environ.get("BUSINESS_CONFIG_PATH") or str(
        project_root / "business_config_tes_rentals.yaml"
    )

    from tools.config_loader import load_config

    if args.edit_commit:
        if not args.row_id or args.caption_text is None:
            print(json.dumps(
                {
                    "success": False,
                    "error": "--edit-commit requires --row-id and --caption-text",
                },
                indent=2,
            ))
            return 1
        config = load_config(config_path)
        result = _edit_commit(config, args.row_id, args.caption_text)
        print(json.dumps(result, indent=2))
        return 0 if result.get("success") else 1

    try:
        payload = json.loads(args.payload)
    except ValueError as exc:
        print(json.dumps(
            {"success": False, "error": f"--payload is not valid JSON: {exc}"},
            indent=2,
        ))
        return 1

    config = load_config(config_path)

    if args.dry_run:
        actions = payload.get("actions") or []
        action_id = (actions[0].get("action_id") if actions else "") or ""
        action_type, row_id = parse_action_id(action_id)
        print(json.dumps(
            {
                "dry_run": True,
                "action": action_type,
                "row_id": row_id,
                "would_handle": action_type in _HANDLERS,
            },
            indent=2,
        ))
        return 0

    result = handle_action(payload, config)
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
