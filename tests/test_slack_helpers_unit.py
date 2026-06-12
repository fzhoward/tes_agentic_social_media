"""Unit tests for tools/slack_helpers.py — no live Slack API calls.

All Slack SDK interactions are mocked. Run from project root:
    python -m pytest tests/test_slack_helpers_unit.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.slack_helpers as slack_helpers  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client() -> MagicMock:
    """Return a MagicMock that mimics a slack_sdk.WebClient."""
    client = MagicMock()
    # chat_postMessage returns a response-like object whose .data is a dict.
    post_response = MagicMock()
    post_response.data = {"ok": True, "ts": "1234567890.000001", "channel": "C123"}
    client.chat_postMessage.return_value = post_response
    return client


# ---------------------------------------------------------------------------
# post_message — channel resolution tests
# ---------------------------------------------------------------------------

class TestPostMessageChannelResolution:
    """post_message must resolve '#name' to a channel ID before calling chat_postMessage."""

    def test_hash_prefixed_name_is_resolved_to_id(self, monkeypatch):
        """chat_postMessage must receive 'C123', not '#approvals'."""
        client = _make_mock_client()
        monkeypatch.setattr(slack_helpers, "_CLIENT", client)

        # Patch _resolve_channel_id so '#approvals' → 'C123' without a live API call.
        monkeypatch.setattr(
            slack_helpers,
            "_resolve_channel_id",
            lambda ch: "C123" if ch == "#approvals" else ch,
        )

        slack_helpers.post_message("#approvals", "hello world")

        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C123", (
            f"Expected resolved ID 'C123', got {call_kwargs['channel']!r}"
        )
        assert call_kwargs["channel"] != "#approvals", (
            "chat_postMessage was called with the raw channel name, not the resolved ID"
        )

    def test_hash_prefixed_name_resolved_with_blocks(self, monkeypatch):
        """Resolution works correctly when optional blocks kwarg is present."""
        client = _make_mock_client()
        monkeypatch.setattr(slack_helpers, "_CLIENT", client)
        monkeypatch.setattr(
            slack_helpers,
            "_resolve_channel_id",
            lambda ch: "C999" if ch == "#approvals" else ch,
        )

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
        slack_helpers.post_message("#approvals", "fallback", blocks=blocks)

        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C999"
        assert call_kwargs["blocks"] == blocks

    def test_raw_channel_id_passes_through_unchanged(self, monkeypatch):
        """A bare channel ID (no '#') is returned as-is by _resolve_channel_id."""
        client = _make_mock_client()
        monkeypatch.setattr(slack_helpers, "_CLIENT", client)

        # _resolve_channel_id returns the value unchanged when no '#' prefix.
        # We don't patch it here — we let the real implementation run so we
        # also verify the passthrough branch is correct.
        # Clear the module-level client cache so our mock is used.
        monkeypatch.setattr(slack_helpers, "_CLIENT", client)

        slack_helpers.post_message("C456", "direct id")

        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C456"

    def test_resolution_uses_conversations_list(self, monkeypatch):
        """End-to-end: _resolve_channel_id fetches conversations.list and maps the name."""
        client = _make_mock_client()
        monkeypatch.setattr(slack_helpers, "_CLIENT", client)

        # Populate the conversations.list response with our fake channel.
        list_response = MagicMock()
        list_response.get = lambda key, default=None: (
            [{"name": "approvals", "id": "C123"}] if key == "channels"
            else ({} if key == "response_metadata" else default)
        )
        client.conversations_list.return_value = list_response

        # Clear the cache so the lookup actually runs.
        slack_helpers._CHANNEL_ID_CACHE.pop("approvals", None)

        slack_helpers.post_message("#approvals", "via list lookup")

        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C123", (
            f"Expected 'C123' from conversations.list, got {call_kwargs['channel']!r}"
        )
