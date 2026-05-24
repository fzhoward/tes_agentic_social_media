"""Slack utilities for posting messages, uploading files, reading history, and reacting.

Authentication: reads SLACK_BOT_TOKEN from the environment (loaded from .env).
The slack_sdk WebClient is cached at module level — one client per process.

Channel resolution: helpers accept either a channel name ("#system-health") or
a Slack channel ID. Names are resolved to IDs internally via
conversations.list and cached for the lifetime of the process. The
reactions.add and conversations.history endpoints require channel IDs at the
API level; resolution is transparent to the caller.

Unicode emoji note: the Slack API normalizes unicode emoji to shortcodes
server-side. A message posted containing "🟢" comes back from
conversations.history with ":large_green_circle:" in its text. Any code that
reads message text and compares it to expected strings must account for this.

This tool is a delivery mechanism, not a message designer. Callers that need
rich messages build their own Block Kit JSON and pass it as `blocks=`.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


load_dotenv(override=False)


_CLIENT: WebClient | None = None
_CHANNEL_ID_CACHE: dict[str, str] = {}


def get_slack_client() -> WebClient:
    """Return an authenticated slack_sdk.WebClient, cached at module level.

    Raises:
        EnvironmentError: if SLACK_BOT_TOKEN is not set in the environment.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise EnvironmentError(
            "SLACK_BOT_TOKEN is not set in the environment. "
            "Add it to .env before calling Slack helpers."
        )

    _CLIENT = WebClient(token=token)
    return _CLIENT


def _resolve_channel_id(channel: str) -> str:
    """Resolve a channel name to a channel ID, caching the mapping.

    If `channel` does not start with '#', it is assumed to already be a
    channel ID and returned unchanged.

    Raises:
        ValueError: if `channel` is a name not found in the workspace.
    """
    if not channel.startswith("#"):
        return channel

    name = channel[1:]
    if name in _CHANNEL_ID_CACHE:
        return _CHANNEL_ID_CACHE[name]

    client = get_slack_client()
    cursor: str | None = None
    while True:
        response = client.conversations_list(
            limit=200,
            cursor=cursor,
            types="public_channel",
            exclude_archived=True,
        )
        for ch in response.get("channels", []) or []:
            ch_name = ch.get("name")
            ch_id = ch.get("id")
            if ch_name and ch_id:
                _CHANNEL_ID_CACHE[ch_name] = ch_id
        if name in _CHANNEL_ID_CACHE:
            return _CHANNEL_ID_CACHE[name]
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break

    raise ValueError(f"Slack channel not found: {channel}")


def post_message(
    channel: str,
    text: str,
    blocks: list[dict] | None = None,
    thread_ts: str | None = None,
) -> dict:
    """Post a message to a channel.

    Args:
        channel: channel name (e.g., "#approvals") or channel ID.
        text: fallback text (Slack requires this even when blocks are present).
        blocks: optional list of Block Kit block dicts. When provided, these
            render as the rich message and `text` becomes the notification/fallback.
        thread_ts: optional parent message timestamp to reply in-thread.

    Returns:
        The full Slack API response as a dict (includes ok, ts, channel, message).

    Raises:
        slack_sdk.errors.SlackApiError on API failure.
    """
    client = get_slack_client()
    kwargs: dict[str, Any] = {"channel": channel, "text": text}
    if blocks is not None:
        kwargs["blocks"] = blocks
    if thread_ts is not None:
        kwargs["thread_ts"] = thread_ts
    response = client.chat_postMessage(**kwargs)
    return dict(response.data)


def upload_file(
    channel: str,
    filepath: str,
    title: str | None = None,
    initial_comment: str | None = None,
) -> dict:
    """Upload a file to a channel via files_upload_v2.

    Args:
        channel: channel name or channel ID.
        filepath: local path to the file to upload.
        title: optional display title shown in Slack.
        initial_comment: optional message posted alongside the file.

    Returns:
        The full Slack API response as a dict.

    Raises:
        slack_sdk.errors.SlackApiError on API failure.
    """
    client = get_slack_client()
    resolved = _resolve_channel_id(channel)
    kwargs: dict[str, Any] = {"channel": resolved, "file": filepath}
    if title is not None:
        kwargs["title"] = title
    if initial_comment is not None:
        kwargs["initial_comment"] = initial_comment
    response = client.files_upload_v2(**kwargs)
    return dict(response.data)


def add_reaction(channel: str, timestamp: str, emoji_name: str) -> dict:
    """Add an emoji reaction to a message.

    The reactions.add endpoint requires a channel ID; `channel` is resolved
    via `_resolve_channel_id` if a name is passed.

    Args:
        channel: channel name or channel ID.
        timestamp: the `ts` of the message to react to.
        emoji_name: emoji shortcode WITHOUT surrounding colons
            (e.g., "white_check_mark", not ":white_check_mark:").

    Returns:
        The full Slack API response as a dict. If the reaction already exists
        on the message, Slack returns `already_reacted`; this is swallowed
        and the error response dict is returned instead of raising.

    Raises:
        slack_sdk.errors.SlackApiError for any error other than already_reacted.
    """
    client = get_slack_client()
    resolved = _resolve_channel_id(channel)
    try:
        response = client.reactions_add(
            channel=resolved,
            timestamp=timestamp,
            name=emoji_name,
        )
        return dict(response.data)
    except SlackApiError as exc:
        if exc.response is not None and exc.response.get("error") == "already_reacted":
            return dict(exc.response.data) if exc.response.data else {"ok": False, "error": "already_reacted"}
        raise


def get_channel_history(channel: str, limit: int = 20) -> list[dict]:
    """Return recent messages from a channel, newest first.

    Note on emoji normalization: the Slack API normalizes unicode emoji to
    shortcodes server-side. A message posted containing "🟢" will come back
    from conversations.history with ":large_green_circle:" in its text.
    Callers comparing message text against expected strings must account for
    this — either by posting shortcodes in the first place, or by normalizing
    on read.

    Args:
        channel: channel name or channel ID.
        limit: max number of messages (default 20, capped at 200).

    Returns:
        List of message dicts (newest first). Each dict includes at minimum
        a `ts` key, and typically `text`, `user`, and `blocks` when present.
    """
    client = get_slack_client()
    resolved = _resolve_channel_id(channel)
    if limit > 200:
        limit = 200
    response = client.conversations_history(channel=resolved, limit=limit)
    messages = response.get("messages", []) or []
    return [dict(m) for m in messages]
