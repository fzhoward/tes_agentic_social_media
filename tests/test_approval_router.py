"""Tests for tools/approval_router.py and the missed-approval rescheduler
in tools/approval_card.py.

All Sheets, Slack, and SocialBu I/O is mocked — no live API calls.

Run from the project root:
    pytest tests/test_approval_router.py -v
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import approval_card, approval_router  # noqa: E402
from tools import sheets_helpers, slack_helpers, socialbu_publish  # noqa: E402
from tools.config_loader import Config  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> Config:
    data = {
        "approval": {
            "slack_channel": "#approvals",
            "error_channel": "#system-errors",
            "auto_reject_after_misses": 3,
        },
        "drive": {
            "content_queue_sheet_id": "fake-sheet-id",
        },
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k].update(v)
        else:
            data[k] = v
    return Config(data)


def _base_row(**overrides) -> dict:
    row = {
        "row_id": "STR-20260528-FB-01",
        "status": "awaiting_approval",
        "platform": "facebook",
        "scheduled_datetime": "2026-06-01T09:00:00+00:00",
        "objective": "Top-of-funnel",
        "content_type": "equipment_post",
        "caption": "Heres the right machine for tight residential lots.",
        "creative_hook_text": "Skid steer beats backhoe",
        "first_comment": "Book at https://tesrents.com",
        "cta_text": "Call (904) 452-0888",
        "hook_text": "When the lot is too small",
        "image_overlay_text": "Tight Lots",
        "media_url": "https://example.com/image.png",
        "media_format_used": "creatomate_text_overlay",
        "draft_rationale": "fits the angle",
        "revision_round": "1",
        "draft_notes": "",
        "critic_score": "pass",
        "critic_notes": "{}",
    }
    row.update(overrides)
    return row


def _base_payload(action_id: str, row_id: str | None = None) -> dict:
    """Build a Slack block_actions payload skeleton."""
    if row_id is None:
        _, _, row_id = action_id.partition("::")
    return {
        "type": "block_actions",
        "actions": [
            {
                "action_id": action_id,
                "value": row_id,
                "block_id": "abc",
            }
        ],
        "channel": {"id": "C123CHAN"},
        "message": {"ts": "1700000000.000100"},
        "user": {"id": "U123USER", "name": "zeb"},
    }


def _wire_sheets(monkeypatch, *, rows: list[dict], tab_name: str = "Queue"):
    """Stub out sheets_helpers + service so the router can run end-to-end.

    ``rows`` is the list of dicts that ``find_rows_by_column_value`` returns
    matches from (matched against ``row_id`` column).

    Returns a ``calls`` dict that captures every update_cells invocation as
    ``{"updates": [(row_number, col_updates), ...]}``.
    """
    calls = {"updates": []}

    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": tab_name}}]
    }
    monkeypatch.setattr(
        sheets_helpers, "get_sheets_service", lambda: fake_service,
    )

    def fake_find(spreadsheet_id, tab, column_name, value, service=None):
        matches: list[tuple[int, dict]] = []
        for i, r in enumerate(rows, start=2):
            if str(r.get(column_name, "")) == str(value):
                matches.append((i, dict(r)))
        return matches

    monkeypatch.setattr(
        sheets_helpers, "find_rows_by_column_value", fake_find,
    )

    def fake_read_rows_filtered(spreadsheet_id, tab, column_name, value, service=None):
        return [dict(r) for r in rows if str(r.get(column_name, "")) == str(value)]

    monkeypatch.setattr(
        sheets_helpers, "read_rows_filtered", fake_read_rows_filtered,
    )

    def fake_update(spreadsheet_id, tab, row_number, col_updates, service=None):
        calls["updates"].append((row_number, dict(col_updates)))

    monkeypatch.setattr(sheets_helpers, "update_cells", fake_update)

    return calls


def _wire_slack(monkeypatch):
    """Stub Slack helpers; return a ``calls`` dict capturing every call."""
    calls = {"post": [], "react": [], "history": []}

    def fake_post(channel, text, blocks=None, thread_ts=None):
        calls["post"].append({
            "channel": channel,
            "text": text,
            "blocks": blocks,
            "thread_ts": thread_ts,
        })
        return {"ok": True, "ts": "1700000001.0", "channel": channel}

    monkeypatch.setattr(slack_helpers, "post_message", fake_post)

    def fake_react(channel, ts, name):
        calls["react"].append({"channel": channel, "ts": ts, "name": name})
        return {"ok": True}

    monkeypatch.setattr(slack_helpers, "add_reaction", fake_react)

    def fake_history(channel, limit=20):
        return []

    monkeypatch.setattr(slack_helpers, "get_channel_history", fake_history)

    return calls


# ---------------------------------------------------------------------------
# 9-10: action_id parsing
# ---------------------------------------------------------------------------

def test_parse_action_id_approve():
    assert approval_router.parse_action_id("approve::STR-20260528-FB-01") == (
        "approve", "STR-20260528-FB-01",
    )


def test_parse_action_id_reject():
    assert approval_router.parse_action_id("reject::STR-20260528-IG-02") == (
        "reject", "STR-20260528-IG-02",
    )


# ---------------------------------------------------------------------------
# 11: approve success
# ---------------------------------------------------------------------------

def test_approve_success(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    def fake_publish(target_row, config, *, dry_run=False):
        return {
            "success": True,
            "socialbu_post_id": "7606301",
            "published_datetime": "2026-06-01T08:55:00+00:00",
            "error": None,
            "payload": {},
        }

    monkeypatch.setattr(socialbu_publish, "publish_row", fake_publish)

    payload = _base_payload(f"approve::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["success"] is True
    assert result["action"] == "approve"
    assert result["row_id"] == row["row_id"]

    # Two updates: first to "approved", second to "published".
    statuses = [u[1].get("status") for u in sheets["updates"]]
    assert statuses == ["approved", "published"]
    last_update = sheets["updates"][-1][1]
    assert last_update["socialbu_post_id"] == "7606301"
    assert last_update["published_datetime"] == "2026-06-01T08:55:00+00:00"

    # ✅ reaction added
    assert any(r["name"] == "white_check_mark" for r in slack["react"])


# ---------------------------------------------------------------------------
# 12: approve already processed
# ---------------------------------------------------------------------------

def test_approve_already_processed(monkeypatch):
    row = _base_row(status="published")
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    publish_calls = []

    def fake_publish(target_row, config, *, dry_run=False):
        publish_calls.append(target_row)
        return {"success": True}

    monkeypatch.setattr(socialbu_publish, "publish_row", fake_publish)

    payload = _base_payload(f"approve::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["success"] is False
    assert "already processed" in (result["error"] or "").lower()
    assert publish_calls == []
    assert sheets["updates"] == []
    # Thread reply explaining the row was already processed
    assert any(
        p["thread_ts"] == payload["message"]["ts"]
        and row["row_id"] in p["text"]
        for p in slack["post"]
    )


# ---------------------------------------------------------------------------
# 13: approve, publish fails
# ---------------------------------------------------------------------------

def test_approve_publish_fails(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    def fake_publish(target_row, config, *, dry_run=False):
        return {
            "success": False,
            "socialbu_post_id": None,
            "published_datetime": None,
            "error": "HTTP 422: validation failed",
            "payload": {},
        }

    monkeypatch.setattr(socialbu_publish, "publish_row", fake_publish)

    payload = _base_payload(f"approve::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["success"] is False
    assert "422" in (result["error"] or "")

    # First update sets approved, second reverts to awaiting_approval.
    statuses = [u[1].get("status") for u in sheets["updates"]]
    assert statuses == ["approved", "awaiting_approval"]

    # An error post went to #system-errors.
    assert any(p["channel"] == "#system-errors" for p in slack["post"])


# ---------------------------------------------------------------------------
# 14: reject
# ---------------------------------------------------------------------------

def test_reject_updates_status(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    payload = _base_payload(f"reject::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["success"] is True
    assert sheets["updates"], "expected an update_cells call"
    _, col_updates = sheets["updates"][-1]
    assert col_updates["status"] == "rejected"
    assert "zeb" in col_updates["rejection_reason"]
    assert any(r["name"] == "x" for r in slack["react"])


# ---------------------------------------------------------------------------
# 15: edit_caption
# ---------------------------------------------------------------------------

def test_edit_caption_resets_to_drafted(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    payload = _base_payload(f"edit_caption::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["success"] is True
    assert sheets["updates"], "expected an update_cells call"
    _, col_updates = sheets["updates"][-1]
    assert col_updates["status"] == "drafted"

    # Thread reply asking owner to reply with new caption
    thread_replies = [
        p for p in slack["post"]
        if p["thread_ts"] == payload["message"]["ts"]
    ]
    assert thread_replies
    assert "revised caption" in thread_replies[0]["text"].lower()


# ---------------------------------------------------------------------------
# 16: regen_media preserves caption
# ---------------------------------------------------------------------------

def test_regen_media_preserves_caption(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    _wire_slack(monkeypatch)

    payload = _base_payload(f"regen_media::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["success"] is True
    assert sheets["updates"], "expected an update_cells call"
    _, col_updates = sheets["updates"][-1]
    assert col_updates["status"] == "planned"
    # Media fields cleared
    assert col_updates["media_url"] == ""
    assert col_updates["media_format_used"] == ""
    # Caption-related fields NOT touched
    for preserved in [
        "caption", "creative_hook_text", "first_comment", "cta_text",
        "hook_text", "image_overlay_text", "draft_rationale", "revision_round",
        "critic_score", "critic_notes",
    ]:
        assert preserved not in col_updates, (
            f"regen_media must not clear {preserved!r}; "
            f"updates={list(col_updates.keys())}"
        )


# ---------------------------------------------------------------------------
# 17: regen_all clears all Drafter + Critic fields
# ---------------------------------------------------------------------------

def test_regen_all_clears_all_fields(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    _wire_slack(monkeypatch)

    payload = _base_payload(f"regen_all::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["success"] is True
    _, col_updates = sheets["updates"][-1]
    assert col_updates["status"] == "planned"
    for field in approval_router.DRAFTER_FIELDS + approval_router.CRITIC_FIELDS:
        assert col_updates.get(field) == "", (
            f"regen_all must clear {field!r} (got {col_updates.get(field)!r})"
        )


# ---------------------------------------------------------------------------
# 18: regen_all cleared field set matches exactly
# ---------------------------------------------------------------------------

def test_regen_all_clears_correct_fields(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    _wire_slack(monkeypatch)

    payload = _base_payload(f"regen_all::{row['row_id']}")
    approval_router.handle_action(payload, _make_config())

    _, col_updates = sheets["updates"][-1]
    cleared = {k for k, v in col_updates.items() if v == "" and k != "status"}
    expected = set(approval_router.DRAFTER_FIELDS) | set(approval_router.CRITIC_FIELDS)
    assert cleared == expected, (
        f"regen_all cleared {cleared!r}, expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# 19: error posts to #system-errors
# ---------------------------------------------------------------------------

def test_error_posts_to_system_errors(monkeypatch):
    row = _base_row()
    _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    def fake_publish(target_row, config, *, dry_run=False):
        return {
            "success": False,
            "socialbu_post_id": None,
            "published_datetime": None,
            "error": "network exploded",
            "payload": {},
        }

    monkeypatch.setattr(socialbu_publish, "publish_row", fake_publish)

    payload = _base_payload(f"approve::{row['row_id']}")
    approval_router.handle_action(payload, _make_config())

    error_posts = [p for p in slack["post"] if p["channel"] == "#system-errors"]
    assert error_posts, "expected an error post to #system-errors"
    assert "network exploded" in error_posts[0]["text"]
    assert row["row_id"] in error_posts[0]["text"]


# ---------------------------------------------------------------------------
# 20: reschedule a single missed row
# ---------------------------------------------------------------------------

def test_reschedule_missed_single(monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    row = _base_row(scheduled_datetime=past, draft_notes="")
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    results = approval_card.reschedule_missed_approvals(_make_config())

    assert len(results) == 1
    assert results[0]["action"] == "reschedule"
    assert results[0]["miss_count"] == 1

    # Find the reschedule update specifically rather than assuming it is the
    # last update_cells call. Adjusted for the S44 Piece 1 slack_message_ts
    # writeback: post_approval_card now appends an extra update_cells
    # (slack_message_ts) when the fresh card is re-posted, so the reschedule
    # update is no longer guaranteed to be sheets["updates"][-1].
    reschedule_updates = [
        u for _, u in sheets["updates"] if "scheduled_datetime" in u
    ]
    assert reschedule_updates, "expected a reschedule update with scheduled_datetime"
    col_updates = reschedule_updates[-1]
    assert "[RESCHEDULED: 1]" in col_updates["draft_notes"]

    # A fresh approval card was posted to #approvals.
    approvals_posts = [p for p in slack["post"] if p["channel"] == "#approvals"]
    assert approvals_posts, "expected a fresh approval card after rescheduling"


# ---------------------------------------------------------------------------
# 21: auto-reject after 3 misses
# ---------------------------------------------------------------------------

def test_reschedule_missed_auto_reject(monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    # Two prior misses already recorded → this miss makes it the third.
    row = _base_row(
        scheduled_datetime=past,
        draft_notes="[RESCHEDULED: 2]",
    )
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    results = approval_card.reschedule_missed_approvals(_make_config())

    assert len(results) == 1
    assert results[0]["action"] == "auto_reject"

    _, col_updates = sheets["updates"][-1]
    assert col_updates["status"] == "rejected"
    assert "Auto-rejected" in col_updates["rejection_reason"]

    error_posts = [p for p in slack["post"] if p["channel"] == "#system-errors"]
    assert error_posts, "expected a #system-errors notice on auto-reject"
    assert row["row_id"] in error_posts[0]["text"]


# ---------------------------------------------------------------------------
# 22: not yet missed → no action
# ---------------------------------------------------------------------------

def test_reschedule_not_yet_missed(monkeypatch):
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    row = _base_row(scheduled_datetime=future)
    sheets = _wire_sheets(monkeypatch, rows=[row])
    _wire_slack(monkeypatch)

    results = approval_card.reschedule_missed_approvals(_make_config())

    assert results == []
    assert sheets["updates"] == []


# ===========================================================================
# S44 Piece 2a — _publish_and_resolve refactor parity + --edit-commit
# ===========================================================================

def _publish_success(*, post_id="7606301", when="2026-06-01T08:55:00+00:00"):
    def fake_publish(target_row, config, *, dry_run=False):
        return {
            "success": True,
            "socialbu_post_id": post_id,
            "published_datetime": when,
            "error": None,
            "payload": {},
        }
    return fake_publish


def _publish_failure(*, error="HTTP 422: validation failed"):
    def fake_publish(target_row, config, *, dry_run=False):
        return {
            "success": False,
            "socialbu_post_id": None,
            "published_datetime": None,
            "error": error,
            "payload": {},
        }
    return fake_publish


def _run_edit_commit_cli(monkeypatch, capsys, config, row_id, caption_text):
    """Drive ``approval_router._cli()`` in --edit-commit mode.

    Returns ``(return_code, parsed_json_result)``.
    """
    monkeypatch.setattr(
        "tools.config_loader.load_config", lambda path=None: config,
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "approval_router", "--edit-commit",
            "--row-id", row_id,
            "--caption-text", caption_text,
        ],
    )
    rc = approval_router._cli()
    out = capsys.readouterr().out
    return rc, json.loads(out)


# --- A. Approve-path parity (refactor safety) ------------------------------
# test_approve_success (success) and test_approve_publish_fails (revert +
# error post) above already cover the approve path through the extracted
# _publish_and_resolve helper. This adds the two A2 assertions those tests
# don't make: approved_datetime is cleared on revert, and a thread reply is
# posted to the card thread on publish failure.

def test_approve_publish_fail_reverts_datetime_and_thread_replies(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    monkeypatch.setattr(
        socialbu_publish, "publish_row", _publish_failure(error="boom"),
    )

    payload = _base_payload(f"approve::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["action"] == "approve"
    assert result["success"] is False

    # Revert clears approved_datetime.
    revert = sheets["updates"][-1][1]
    assert revert["status"] == "awaiting_approval"
    assert revert["approved_datetime"] == ""

    # _thread_reply fired on the card thread about the failure.
    thread_replies = [
        p for p in slack["post"]
        if p["thread_ts"] == payload["message"]["ts"]
    ]
    assert any("Publish failed" in p["text"] for p in thread_replies)

    # _post_error fired to #system-errors.
    assert any(p["channel"] == "#system-errors" for p in slack["post"])


# --- B. _publish_and_resolve tolerates an empty payload --------------------

def test_publish_and_resolve_empty_payload_no_slack_side_effects(monkeypatch):
    row = _base_row(status="approved")
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    monkeypatch.setattr(
        socialbu_publish, "publish_row", _publish_success(post_id="999"),
    )

    # Empty-dict payload: the reaction/thread helpers must no-op, not raise.
    result = approval_router._publish_and_resolve(
        {}, _make_config(), row["row_id"],
        "fake-sheet-id", "Queue", sheets_helpers.get_sheets_service(), 2, row,
    )

    assert result["success"] is True
    assert result["action"] == "approve"
    # No reaction and no thread/error post fired from an empty payload.
    assert slack["react"] == []
    assert slack["post"] == []
    # Published write still happened.
    assert sheets["updates"][-1][1]["status"] == "published"


# --- C. --edit-commit happy path -------------------------------------------

def test_edit_commit_happy_path(monkeypatch, capsys):
    row = _base_row(status="drafted")
    sheets = _wire_sheets(monkeypatch, rows=[row])
    _wire_slack(monkeypatch)

    publish_calls = []

    def fake_publish(target_row, config, *, dry_run=False):
        publish_calls.append(dict(target_row))
        return {
            "success": True,
            "socialbu_post_id": "7606301",
            "published_datetime": "2026-06-01T08:55:00+00:00",
            "error": None,
            "payload": {},
        }

    monkeypatch.setattr(socialbu_publish, "publish_row", fake_publish)

    rc, result = _run_edit_commit_cli(
        monkeypatch, capsys, _make_config(), row["row_id"], "new caption",
    )

    assert rc == 0
    assert result["success"] is True
    assert result["action"] == "edit_commit"

    # First update wrote caption + approved + approved_datetime.
    first = sheets["updates"][0][1]
    assert first["caption"] == "new caption"
    assert first["status"] == "approved"
    assert first.get("approved_datetime")

    # publish_row was called with the edited caption in the row.
    assert len(publish_calls) == 1
    assert publish_calls[0]["caption"] == "new caption"

    # Final write set the published state.
    assert sheets["updates"][-1][1]["status"] == "published"
    assert sheets["updates"][-1][1]["socialbu_post_id"] == "7606301"


# --- D. --edit-commit status guard (not 'drafted') -------------------------

def test_edit_commit_status_guard(monkeypatch, capsys):
    row = _base_row(status="awaiting_approval")  # NOT drafted
    sheets = _wire_sheets(monkeypatch, rows=[row])
    _wire_slack(monkeypatch)

    publish_calls = []

    def fake_publish(target_row, config, *, dry_run=False):
        publish_calls.append(target_row)
        return {"success": True}

    monkeypatch.setattr(socialbu_publish, "publish_row", fake_publish)

    rc, result = _run_edit_commit_cli(
        monkeypatch, capsys, _make_config(), row["row_id"], "new caption",
    )

    assert rc == 1
    assert result["success"] is False
    assert result["action"] == "edit_commit"
    # Message names the actual status that blocked the commit.
    assert "awaiting_approval" in result["message"]
    # No caption write, no publish.
    assert sheets["updates"] == []
    assert publish_calls == []


# --- E. --edit-commit row not found ----------------------------------------

def test_edit_commit_row_not_found(monkeypatch, capsys):
    sheets = _wire_sheets(monkeypatch, rows=[])  # no matching row
    _wire_slack(monkeypatch)

    publish_calls = []

    def fake_publish(target_row, config, *, dry_run=False):
        publish_calls.append(target_row)
        return {"success": True}

    monkeypatch.setattr(socialbu_publish, "publish_row", fake_publish)

    rc, result = _run_edit_commit_cli(
        monkeypatch, capsys, _make_config(), "STR-NOPE-01", "new caption",
    )

    assert rc == 1
    assert result["success"] is False
    assert result["action"] == "edit_commit"
    assert "not found" in (result["error"] or "").lower()
    assert sheets["updates"] == []
    assert publish_calls == []


# --- F. --edit-commit publish failure reverts ------------------------------

def test_edit_commit_publish_failure_reverts(monkeypatch, capsys):
    row = _base_row(status="drafted")
    sheets = _wire_sheets(monkeypatch, rows=[row])
    _wire_slack(monkeypatch)

    monkeypatch.setattr(
        socialbu_publish, "publish_row",
        _publish_failure(error="HTTP 500: socialbu down"),
    )

    rc, result = _run_edit_commit_cli(
        monkeypatch, capsys, _make_config(), row["row_id"], "new caption",
    )

    assert rc == 1
    assert result["success"] is False
    assert result["action"] == "edit_commit"

    # The shared helper's failure-revert applies to the edit-commit path too:
    # status back to awaiting_approval, approved_datetime cleared.
    revert = sheets["updates"][-1][1]
    assert revert["status"] == "awaiting_approval"
    assert revert["approved_datetime"] == ""


# --- G. edit_caption reply text (no Critic re-run wording) ------------------

def test_edit_caption_reply_text_drops_critic_rerun(monkeypatch):
    row = _base_row()
    sheets = _wire_sheets(monkeypatch, rows=[row])
    slack = _wire_slack(monkeypatch)

    payload = _base_payload(f"edit_caption::{row['row_id']}")
    result = approval_router.handle_action(payload, _make_config())

    assert result["success"] is True
    # Status still resets to drafted.
    assert sheets["updates"][-1][1]["status"] == "drafted"

    thread_replies = [
        p for p in slack["post"]
        if p["thread_ts"] == payload["message"]["ts"]
    ]
    assert thread_replies
    text = thread_replies[0]["text"]
    # Old misleading wording is gone (no Critic re-run promise).
    assert "re-run the Critic" not in text
    assert "Critic" not in text
    # New wording present.
    assert "no re-review" in text.lower()
