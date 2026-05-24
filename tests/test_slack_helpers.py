"""Tests for tools/slack_helpers.py — run against the live TES Rentals Slack workspace.

Requires SLACK_BOT_TOKEN in .env. Tests post to #system-health, which is a
bot-oriented channel — leftover messages are fine, no cleanup needed.

Run from project root:
    python -m pytest tests/test_slack_helpers.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Importing config_loader triggers load_dotenv(), which puts SLACK_BOT_TOKEN
# into the process environment. slack_helpers also calls load_dotenv on import
# as a backstop.
CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from tools import slack_helpers  # noqa: E402


TEST_CHANNEL = "#system-health"


def test_post_message_simple():
    response = slack_helpers.post_message(
        TEST_CHANNEL,
        "[TEST] slack_helpers test — plain text message",
    )
    assert response.get("ok") is True
    ts = response.get("ts")
    assert isinstance(ts, str) and ts


def test_post_message_with_blocks():
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*[TEST]* slack_helpers test — block message",
            },
        }
    ]
    response = slack_helpers.post_message(
        TEST_CHANNEL,
        "[TEST] slack_helpers test — block message",
        blocks=blocks,
    )
    assert response.get("ok") is True


def test_add_reaction():
    parent = slack_helpers.post_message(
        TEST_CHANNEL,
        "[TEST] slack_helpers test — reaction target",
    )
    response = slack_helpers.add_reaction(
        parent["channel"],
        parent["ts"],
        "white_check_mark",
    )
    assert response.get("ok") is True


def test_add_reaction_idempotent():
    # Post a fresh message so this test does not depend on test_add_reaction.
    parent = slack_helpers.post_message(
        TEST_CHANNEL,
        "[TEST] slack_helpers test — idempotent reaction target",
    )
    channel = parent["channel"]
    ts = parent["ts"]

    first = slack_helpers.add_reaction(channel, ts, "white_check_mark")
    assert first.get("ok") is True

    # Second call must not raise — already_reacted is swallowed.
    second = slack_helpers.add_reaction(channel, ts, "white_check_mark")
    assert isinstance(second, dict)


def test_get_channel_history():
    history = slack_helpers.get_channel_history(TEST_CHANNEL, limit=20)
    assert isinstance(history, list)
    assert len(history) > 0, "expected at least one message from prior test posts"
    for msg in history:
        assert "ts" in msg, f"message dict missing ts key: {msg!r}"


def test_channel_name_resolution():
    # Clear cache entry for #system-health so we exercise a real lookup.
    slack_helpers._CHANNEL_ID_CACHE.pop("system-health", None)

    channel_id = slack_helpers._resolve_channel_id("#system-health")
    assert isinstance(channel_id, str)
    assert channel_id.startswith("C"), (
        f"expected channel ID starting with 'C', got {channel_id!r}"
    )

    # Cache should now hold the entry.
    assert slack_helpers._CHANNEL_ID_CACHE.get("system-health") == channel_id

    # Second call returns the cached value. Verify by emptying the
    # underlying client-required state isn't even needed — just check the
    # function returns the same ID and the cache wasn't repopulated from
    # scratch.
    before_size = len(slack_helpers._CHANNEL_ID_CACHE)
    cached = slack_helpers._resolve_channel_id("#system-health")
    after_size = len(slack_helpers._CHANNEL_ID_CACHE)
    assert cached == channel_id
    assert before_size == after_size, (
        "cache size changed on second call — name was not served from cache"
    )


def test_post_message_thread_reply():
    parent = slack_helpers.post_message(
        TEST_CHANNEL,
        "[TEST] slack_helpers test — thread parent",
    )
    assert parent.get("ok") is True
    parent_ts = parent["ts"]

    reply = slack_helpers.post_message(
        TEST_CHANNEL,
        "[TEST] slack_helpers test — thread reply",
        thread_ts=parent_ts,
    )
    assert reply.get("ok") is True
    reply_ts = reply.get("ts")
    assert isinstance(reply_ts, str) and reply_ts and reply_ts != parent_ts


def test_missing_token_raises(monkeypatch):
    # Clear the cached client so get_slack_client() rebuilds and re-reads env.
    monkeypatch.setattr(slack_helpers, "_CLIENT", None)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    with pytest.raises(EnvironmentError):
        slack_helpers.get_slack_client()
    # monkeypatch restores SLACK_BOT_TOKEN and _CLIENT after the test.


def test_invalid_channel_raises():
    with pytest.raises(ValueError):
        slack_helpers._resolve_channel_id("#nonexistent_channel_xyz_test")


def test_upload_file(tmp_path):
    test_file = tmp_path / "slack_helpers_test_upload.txt"
    test_file.write_text(
        "This is a test upload from test_slack_helpers.py.\nSafe to delete.\n",
        encoding="utf-8",
    )
    response = slack_helpers.upload_file(
        TEST_CHANNEL,
        str(test_file),
        title="slack_helpers test upload",
        initial_comment="[TEST] slack_helpers test — file upload",
    )
    assert response.get("ok") is True
