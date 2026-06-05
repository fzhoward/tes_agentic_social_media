"""Tests for tools/approval_card.py.

All Slack and Sheets I/O is mocked — no live API calls.

Run from the project root:
    pytest tests/test_approval_card.py -v
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import approval_card  # noqa: E402
from tools import slack_helpers, sheets_helpers  # noqa: E402
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
    # shallow override
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
        "objective": "Top-of-funnel awareness",
        "content_type": "equipment_post",
        "caption": "Here's the right machine for tight residential lots.",
        "creative_hook_text": "Skid steer beats backhoe",
        "first_comment": "Book at https://tesrents.com",
        "cta_text": "Call (904) 452-0888",
        "hook_text": "When the lot is too small",
        "media_url": "https://example.com/image.png",
        "media_format_used": "creatomate_text_overlay",
    }
    row.update(overrides)
    return row


def _flatten_text(blocks: list[dict]) -> str:
    """Concatenate every text string nested anywhere in a Block Kit list."""
    parts: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "text" and isinstance(v, str):
                    parts.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks)
    return "\n".join(parts)


def _find_block(blocks: list[dict], block_type: str) -> dict | None:
    for b in blocks:
        if b.get("type") == block_type:
            return b
    return None


# ---------------------------------------------------------------------------
# 1: Facebook full
# ---------------------------------------------------------------------------

def test_build_blocks_facebook_full():
    row = _base_row()
    config = _make_config()
    fallback, blocks = approval_card.build_approval_blocks(row, config)

    text = _flatten_text(blocks)
    assert "🔵 Facebook" in text
    assert row["caption"] in text
    assert row["cta_text"] in text
    assert row["first_comment"] in text
    assert row["row_id"] in text  # header has row_id

    actions = _find_block(blocks, "actions")
    assert actions is not None
    assert len(actions["elements"]) == 5

    assert isinstance(fallback, str) and fallback
    assert row["row_id"] in fallback


# ---------------------------------------------------------------------------
# 2: Instagram
# ---------------------------------------------------------------------------

def test_build_blocks_instagram():
    row = _base_row(platform="instagram")
    _, blocks = approval_card.build_approval_blocks(row, _make_config())
    text = _flatten_text(blocks)
    assert "📸 Instagram" in text
    assert "🔵" not in text


# ---------------------------------------------------------------------------
# 3: GBP, short caption
# ---------------------------------------------------------------------------

def test_build_blocks_gbp_short_caption():
    short = "Short GBP update."
    row = _base_row(platform="gbp", caption=short)
    _, blocks = approval_card.build_approval_blocks(row, _make_config())
    text = _flatten_text(blocks)
    assert "📍 GBP" in text
    # Caption appears unchanged (no ellipsis truncation)
    assert short in text
    assert "…" not in short  # sanity
    # No mid-string ellipsis added to caption
    caption_section_blocks = [
        b for b in blocks
        if b.get("type") == "section"
        and (b.get("text") or {}).get("text", "").startswith(short)
    ]
    assert caption_section_blocks, "expected a section containing the short caption"


# ---------------------------------------------------------------------------
# 4: long caption truncation
# ---------------------------------------------------------------------------

def test_build_blocks_long_caption_truncated():
    long_caption = "x" * 5000
    row = _base_row(caption=long_caption)
    _, blocks = approval_card.build_approval_blocks(row, _make_config())
    text = _flatten_text(blocks)
    # Truncated text appears (the trimmed-and-ellipsis form), not the full 5000
    assert long_caption not in text
    assert "…" in text


# ---------------------------------------------------------------------------
# 5: missing optional fields
# ---------------------------------------------------------------------------

def test_build_blocks_missing_optional_fields():
    row = _base_row(
        first_comment="",
        cta_text="",
        media_url="",
        creative_hook_text="",
        media_format_used="",
    )
    _, blocks = approval_card.build_approval_blocks(row, _make_config())
    text = _flatten_text(blocks)
    assert "First comment:" not in text
    assert "CTA:" not in text
    assert "View media" not in text
    assert "Hook text:" not in text
    assert "Media format:" not in text
    # Caption + actions still present
    actions = _find_block(blocks, "actions")
    assert actions is not None
    assert len(actions["elements"]) == 5


# ---------------------------------------------------------------------------
# 6: action_ids contain row_id
# ---------------------------------------------------------------------------

def test_action_ids_contain_row_id():
    row = _base_row(row_id="STR-20260601-IG-03")
    _, blocks = approval_card.build_approval_blocks(row, _make_config())
    actions = _find_block(blocks, "actions")
    assert actions is not None

    expected_prefixes = {
        "approve", "reject", "edit_caption", "regen_media", "regen_all",
    }
    seen = set()
    for elt in actions["elements"]:
        action_id = elt["action_id"]
        assert action_id.endswith("::STR-20260601-IG-03"), action_id
        prefix, _, _ = action_id.partition("::")
        seen.add(prefix)
        assert elt["value"] == "STR-20260601-IG-03"

    assert seen == expected_prefixes


# ---------------------------------------------------------------------------
# Image block (S43 Piece 2)
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import hmac  # noqa: E402

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _image_blocks(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b.get("type") == "image"]


def test_image_block_present_when_configured(monkeypatch):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-secret")
    config = _make_config(approval={"media_base_url": "https://executor.tessys.org"})
    row = _base_row()

    _, blocks = approval_card.build_approval_blocks(row, config)

    images = _image_blocks(blocks)
    assert len(images) == 1
    image = images[0]

    prefix = f"https://executor.tessys.org/media/{row['row_id']}/"
    assert image["image_url"].startswith(prefix)
    token = image["image_url"][len(prefix):]
    assert _TOKEN_RE.match(token), token

    assert isinstance(image["alt_text"], str) and image["alt_text"]


def test_image_url_token_matches_executor_signing(monkeypatch):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-secret")
    config = _make_config(approval={"media_base_url": "https://executor.tessys.org"})
    row = _base_row()

    _, blocks = approval_card.build_approval_blocks(row, config)
    image = _image_blocks(blocks)[0]

    expected = hmac.new(
        b"test-secret", row["row_id"].encode(), hashlib.sha256,
    ).hexdigest()[:32]
    assert image["image_url"].endswith(f"/{expected}")
    assert f"/media/{row['row_id']}/{expected}" in image["image_url"]

    # Pin the two helpers directly together: the card's URL builder must
    # produce the same token the executor's route verifies with. Skip if the
    # executor's web deps (flask) aren't installed in this environment — the
    # token contract above is already verified independently.
    try:
        from tools import executor  # noqa: E402
    except ImportError:
        pytest.skip("tools.executor unavailable (flask not installed)")
    built = approval_card._media_image_url(
        row["row_id"], "https://executor.tessys.org",
    )
    assert built.endswith(executor._sign_media_token(row["row_id"]))


def test_no_image_block_when_base_url_unconfigured(monkeypatch):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-secret")
    config = _make_config()  # no media_base_url
    row = _base_row()

    _, blocks = approval_card.build_approval_blocks(row, config)

    assert _image_blocks(blocks) == []
    actions = _find_block(blocks, "actions")
    assert actions is not None
    assert len(actions["elements"]) == 5


def test_no_image_block_when_secret_unset(monkeypatch):
    monkeypatch.delenv("MEDIA_URL_SECRET", raising=False)
    config = _make_config(approval={"media_base_url": "https://executor.tessys.org"})
    row = _base_row()

    _, blocks = approval_card.build_approval_blocks(row, config)

    assert _image_blocks(blocks) == []
    actions = _find_block(blocks, "actions")
    assert actions is not None
    assert len(actions["elements"]) == 5


def test_no_image_block_when_row_has_no_media_url(monkeypatch):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-secret")
    config = _make_config(approval={"media_base_url": "https://executor.tessys.org"})
    row = _base_row(media_url="")

    _, blocks = approval_card.build_approval_blocks(row, config)

    assert _image_blocks(blocks) == []
    actions = _find_block(blocks, "actions")
    assert actions is not None
    assert len(actions["elements"]) == 5


def test_media_image_url_helper(monkeypatch):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-secret")
    url = approval_card._media_image_url(
        "STR-20260528-FB-01", "https://executor.tessys.org",
    )
    assert url is not None
    prefix = "https://executor.tessys.org/media/STR-20260528-FB-01/"
    assert url.startswith(prefix)
    assert _TOKEN_RE.match(url[len(prefix):])

    # Empty base_url → None.
    assert approval_card._media_image_url("STR-20260528-FB-01", "") is None

    # Empty row_id → None.
    assert approval_card._media_image_url("", "https://executor.tessys.org") is None

    # Secret unset → None.
    monkeypatch.delenv("MEDIA_URL_SECRET", raising=False)
    assert approval_card._media_image_url(
        "STR-20260528-FB-01", "https://executor.tessys.org",
    ) is None


# ---------------------------------------------------------------------------
# 7: post_approval_card calls Slack
# ---------------------------------------------------------------------------

def test_post_approval_card_calls_slack(monkeypatch):
    row = _base_row()
    config = _make_config()

    monkeypatch.setattr(
        slack_helpers, "get_channel_history",
        lambda channel, limit=20: [],
    )

    captured = {}

    def fake_post_message(channel, text, blocks=None, thread_ts=None):
        captured["channel"] = channel
        captured["text"] = text
        captured["blocks"] = blocks
        captured["thread_ts"] = thread_ts
        return {"ok": True, "ts": "1234.5678", "channel": "C123"}

    monkeypatch.setattr(slack_helpers, "post_message", fake_post_message)

    response = approval_card.post_approval_card(row, config)

    assert response.get("ok") is True
    assert captured["channel"] == "#approvals"
    assert isinstance(captured["blocks"], list) and len(captured["blocks"]) >= 4
    assert row["row_id"] in captured["text"]


# ---------------------------------------------------------------------------
# 8: duplicate card prevention
# ---------------------------------------------------------------------------

def test_duplicate_card_prevention(monkeypatch):
    row = _base_row()
    config = _make_config()

    monkeypatch.setattr(
        slack_helpers, "get_channel_history",
        lambda channel, limit=20: [
            {
                "ts": "1700.0",
                "text": f"Approval needed: [{row['row_id']}] previously posted",
                "blocks": [],
            }
        ],
    )

    post_calls = []

    def fake_post_message(*args, **kwargs):
        post_calls.append((args, kwargs))
        return {"ok": True, "ts": "x"}

    monkeypatch.setattr(slack_helpers, "post_message", fake_post_message)

    response = approval_card.post_approval_card(row, config)

    assert response.get("skipped") is True
    assert response.get("ok") is False
    assert len(post_calls) == 0


# ---------------------------------------------------------------------------
# slack_message_ts writeback (S44 Piece 1)
# ---------------------------------------------------------------------------

def _writeback_harness(monkeypatch, *, post_response, card_exists=False,
                       find_result=None, find_raises=None, update_raises=None):
    """Wire post_approval_card's dependencies for writeback tests.

    Returns a calls dict recording find_rows_by_column_value / update_cells
    invocations so each test can assert on them.
    """
    monkeypatch.setattr(
        slack_helpers, "get_channel_history",
        lambda channel, limit=20: (
            [{"ts": "1.0", "text": "STR-20260528-FB-01 already here", "blocks": []}]
            if card_exists else []
        ),
    )
    monkeypatch.setattr(
        slack_helpers, "post_message",
        lambda channel, text, blocks=None, thread_ts=None: post_response,
    )

    calls = {"find": [], "update": []}

    monkeypatch.setattr(
        sheets_helpers, "get_sheets_service", lambda: MagicMock(name="service"),
    )
    monkeypatch.setattr(
        approval_card, "_resolve_tab_name",
        lambda sheet_id, service: "ContentQueue",
    )

    def fake_find(sheet_id, tab_name, column_name, value, service=None):
        calls["find"].append((sheet_id, tab_name, column_name, value))
        if find_raises is not None:
            raise find_raises
        return find_result if find_result is not None else []

    def fake_update(sheet_id, tab_name, row_number, updates, service=None):
        calls["update"].append((sheet_id, tab_name, row_number, updates))
        if update_raises is not None:
            raise update_raises
        return None

    monkeypatch.setattr(sheets_helpers, "find_rows_by_column_value", fake_find)
    monkeypatch.setattr(sheets_helpers, "update_cells", fake_update)

    return calls


def test_writeback_happy_path(monkeypatch):
    row = _base_row()
    config = _make_config()
    post_response = {"ok": True, "ts": "1700000000.000100", "channel": "C123"}
    calls = _writeback_harness(
        monkeypatch,
        post_response=post_response,
        find_result=[(7, dict(row))],
    )

    response = approval_card.post_approval_card(row, config)

    # Return contract unchanged: original post response handed back as-is.
    assert response is post_response

    # Located the row by row_id.
    assert len(calls["find"]) == 1
    sheet_id, tab_name, column_name, value = calls["find"][0]
    assert sheet_id == "fake-sheet-id"
    assert column_name == "row_id"
    assert value == row["row_id"]

    # update_cells called exactly once with the ts for the located row.
    assert len(calls["update"]) == 1
    u_sheet, u_tab, u_rownum, u_updates = calls["update"][0]
    assert u_rownum == 7
    assert u_updates == {"slack_message_ts": "1700000000.000100"}


def test_writeback_skipped_card_no_writeback(monkeypatch):
    row = _base_row()
    config = _make_config()
    calls = _writeback_harness(
        monkeypatch,
        post_response={"ok": True, "ts": "1700000000.000100"},
        card_exists=True,
    )

    response = approval_card.post_approval_card(row, config)

    assert response.get("skipped") is True
    assert response.get("ok") is False
    assert calls["find"] == []
    assert calls["update"] == []


def test_writeback_no_ts_no_writeback(monkeypatch):
    row = _base_row()
    config = _make_config()
    post_response = {"ok": False, "error": "rate_limited"}  # no "ts" key
    calls = _writeback_harness(
        monkeypatch,
        post_response=post_response,
        find_result=[(7, dict(row))],
    )

    response = approval_card.post_approval_card(row, config)

    assert response is post_response
    assert calls["find"] == []
    assert calls["update"] == []


def test_writeback_row_not_found(monkeypatch):
    row = _base_row()
    config = _make_config()
    post_response = {"ok": True, "ts": "1700000000.000100"}
    calls = _writeback_harness(
        monkeypatch,
        post_response=post_response,
        find_result=[],  # no matching row
    )

    response = approval_card.post_approval_card(row, config)

    # No exception, post response returned, no update attempted.
    assert response is post_response
    assert len(calls["find"]) == 1
    assert calls["update"] == []


def test_writeback_raises_is_swallowed(monkeypatch):
    row = _base_row()
    config = _make_config()
    post_response = {"ok": True, "ts": "1700000000.000100"}

    # find raises → swallowed, response still returned.
    calls = _writeback_harness(
        monkeypatch,
        post_response=post_response,
        find_raises=RuntimeError("sheets boom"),
    )
    response = approval_card.post_approval_card(row, config)
    assert response is post_response
    assert calls["update"] == []

    # update raises → swallowed, response still returned.
    calls = _writeback_harness(
        monkeypatch,
        post_response=post_response,
        find_result=[(7, dict(row))],
        update_raises=RuntimeError("update boom"),
    )
    response = approval_card.post_approval_card(row, config)
    assert response is post_response
    assert len(calls["update"]) == 1  # attempted, then raised + swallowed


# ---------------------------------------------------------------------------
# 9-11: CLI --reschedule flag
# ---------------------------------------------------------------------------

def test_cli_reschedule_dry_run(monkeypatch, capsys):
    config = _make_config()
    monkeypatch.setattr(
        "tools.config_loader.load_config", lambda path=None: config,
    )

    captured = {}

    def fake_reschedule(cfg, *, dry_run=False):
        captured["config"] = cfg
        captured["dry_run"] = dry_run
        return [{"row_id": "STR-1", "action": "reschedule", "dry_run": True}]

    monkeypatch.setattr(
        approval_card, "reschedule_missed_approvals", fake_reschedule,
    )

    monkeypatch.setattr(sys, "argv", ["approval_card", "--reschedule", "--dry-run"])

    rc = approval_card._cli()

    assert rc == 0
    assert captured["dry_run"] is True
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == [{"row_id": "STR-1", "action": "reschedule", "dry_run": True}]


def test_cli_reschedule_not_dry_run(monkeypatch, capsys):
    config = _make_config()
    monkeypatch.setattr(
        "tools.config_loader.load_config", lambda path=None: config,
    )

    captured = {}

    def fake_reschedule(cfg, *, dry_run=False):
        captured["dry_run"] = dry_run
        return []

    monkeypatch.setattr(
        approval_card, "reschedule_missed_approvals", fake_reschedule,
    )

    monkeypatch.setattr(sys, "argv", ["approval_card", "--reschedule"])

    rc = approval_card._cli()

    assert rc == 0
    assert captured["dry_run"] is False
    capsys.readouterr()  # drain output


# ---------------------------------------------------------------------------
# 12-15: Critic block rendering
# ---------------------------------------------------------------------------

def _critic_notes(warnings: list[dict]) -> str:
    return json.dumps({
        "revision_round": 1,
        "failed_checks": [],
        "warnings": warnings,
        "passed_checks": ["B1", "B2"],
        "notes": "",
    })


def test_critic_block_warnings_with_fix_instruction():
    warnings = [
        {
            "check_id": "C4",
            "description": "Caption is generic about the featured machine.",
            "fix_instruction": "Name the mini excavator and tie it to a job.",
            "location": "caption body",
        },
        {
            "check_id": "W1",
            "description": "No confirmed media asset attached.",
        },
    ]
    row = _base_row(
        critic_score="soft_fail",
        critic_notes=_critic_notes(warnings),
    )
    _, blocks = approval_card.build_approval_blocks(row, _make_config())
    text = _flatten_text(blocks)
    assert "Critic — 2 flagged for your review" in text
    assert "(score: soft_fail)" in text
    assert "*C4*" in text and "*W1*" in text
    # fix_instruction renders as an indented italic sub-line
    assert "↳ _Name the mini excavator and tie it to a job._" in text
    # the warning with no fix_instruction has no sub-line arrow for itself
    assert text.count("↳") == 1


def test_critic_block_clean_row():
    row = _base_row(
        critic_score="pass",
        critic_notes=_critic_notes([]),
    )
    _, blocks = approval_card.build_approval_blocks(row, _make_config())
    text = _flatten_text(blocks)
    assert "✅ *Critic: clean* (score: pass)" in text
    assert "flagged for your review" not in text


def test_critic_block_omitted_when_critic_did_not_run():
    # Empty critic_notes AND no critic_score → no Critic block at all.
    row = _base_row(critic_score="", critic_notes="")
    _, blocks = approval_card.build_approval_blocks(row, _make_config())
    text = _flatten_text(blocks)
    assert "Critic" not in text
    assert approval_card._critic_block(row) is None


def test_critic_block_unparseable_notes_with_score_renders_clean():
    # Malformed JSON but a score present → treat as clean (Critic ran).
    row = _base_row(critic_score="pass", critic_notes="{not valid json")
    block = approval_card._critic_block(row)
    assert block is not None
    assert "Critic: clean" in block["text"]["text"]


def test_critic_block_truncates_oversized_warning_list():
    # Many long warnings must not exceed Slack's section text limit.
    warnings = [
        {
            "check_id": f"C{i}",
            "description": "x" * 500,
            "fix_instruction": "y" * 500,
        }
        for i in range(40)
    ]
    row = _base_row(
        critic_score="soft_fail",
        critic_notes=_critic_notes(warnings),
    )
    block = approval_card._critic_block(row)
    assert block is not None
    section_text = block["text"]["text"]
    assert len(section_text) <= 3000
    assert "…(more warnings — see sheet)" in section_text


def test_cli_default_path_unchanged(monkeypatch, capsys):
    """Regression: invoking with neither --reschedule nor --row-id calls
    post_pending_approvals."""
    config = _make_config()
    monkeypatch.setattr(
        "tools.config_loader.load_config", lambda path=None: config,
    )

    reschedule_calls = []

    def fake_reschedule(cfg, *, dry_run=False):
        reschedule_calls.append(dry_run)
        return []

    monkeypatch.setattr(
        approval_card, "reschedule_missed_approvals", fake_reschedule,
    )

    captured = {}

    def fake_post_pending(cfg, *, dry_run=False):
        captured["dry_run"] = dry_run
        return [{"row_id": "STR-X", "ok": True, "ts": "1.0"}]

    monkeypatch.setattr(
        approval_card, "post_pending_approvals", fake_post_pending,
    )

    monkeypatch.setattr(sys, "argv", ["approval_card", "--dry-run"])

    rc = approval_card._cli()

    assert rc == 0
    assert captured["dry_run"] is True
    assert reschedule_calls == []  # reschedule path NOT taken
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == [{"row_id": "STR-X", "ok": True, "ts": "1.0"}]
