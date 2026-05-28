"""Tests for tools/socialbu_publish.py.

All tests are deterministic and mock HTTP — no live SocialBu API calls.

Run from the project root:
    pytest tests/test_socialbu_publish.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from tools import socialbu_publish  # noqa: E402
from tools.config_loader import load_config  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return load_config(str(CONFIG_FILE))


@pytest.fixture(autouse=True)
def _socialbu_api_key(monkeypatch):
    monkeypatch.setenv("SOCIALBU_API_KEY", "test-key-not-real")


def _base_row(**overrides) -> dict:
    row = {
        "row_id": "STR-20260528-FB-01",
        "status": "approved",
        "platform": "facebook",
        "scheduled_datetime": "2026-06-01T09:00:00+00:00",
        "caption": "Heres the right machine for tight residential lots.",
        "first_comment": "",
        "media_url": "https://lh3.googleusercontent.com/d/abc",
        "media_format_used": "creatomate_text_overlay",
    }
    row.update(overrides)
    return row


def _mock_post_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "" if json_body is None else "ok"
    resp.json.return_value = json_body or {
        "success": True,
        "posts": [
            {
                "id": 7606301,
                "published": False,
                "publish_at": "2026-06-01 09:00:00",
                "account_type": "facebook.page",
            }
        ],
    }
    return resp


# ---------------------------------------------------------------------------
# 1-3: datetime conversion
# ---------------------------------------------------------------------------

def test_convert_scheduled_datetime_utc():
    out = socialbu_publish._convert_scheduled_datetime("2026-06-01T09:00:00+00:00")
    assert out == "2026-06-01 09:00:00"


def test_convert_scheduled_datetime_eastern():
    # -04:00 → +4 hours UTC
    out = socialbu_publish._convert_scheduled_datetime("2026-05-28T21:00:00-04:00")
    assert out == "2026-05-29 01:00:00"


def test_convert_scheduled_datetime_no_tz():
    out = socialbu_publish._convert_scheduled_datetime("2026-06-01T09:00:00")
    assert out == "2026-06-01 09:00:00"


# ---------------------------------------------------------------------------
# 4-7: account ID lookup
# ---------------------------------------------------------------------------

def test_get_account_id_facebook(config):
    assert socialbu_publish._get_account_id("facebook", config) == 173903
    assert isinstance(socialbu_publish._get_account_id("facebook", config), int)


def test_get_account_id_instagram(config):
    assert socialbu_publish._get_account_id("instagram", config) == 173904


def test_get_account_id_gbp(config):
    assert socialbu_publish._get_account_id("gbp", config) == 173906


def test_get_account_id_unknown_platform(config):
    with pytest.raises(ValueError):
        socialbu_publish._get_account_id("tiktok", config)


# ---------------------------------------------------------------------------
# 8-11: payload assembly
# ---------------------------------------------------------------------------

def test_build_payload_facebook_with_first_comment(config):
    row = _base_row(first_comment="Book at https://tesrents.com")
    payload = socialbu_publish._build_payload(row, config)
    assert payload["content"] == row["caption"]
    assert payload["accounts"] == [173903]
    assert payload["publish_at"] == "2026-06-01 09:00:00"
    assert payload["first_comment"] == "Book at https://tesrents.com"


def test_build_payload_facebook_without_first_comment(config):
    row = _base_row(first_comment="")
    payload = socialbu_publish._build_payload(row, config)
    assert "first_comment" not in payload


def test_build_payload_instagram_with_media(config):
    row = _base_row(
        platform="instagram",
        media_url="https://example.com/photo.png",
        media_format_used="creatomate_text_overlay",
    )
    payload = socialbu_publish._build_payload(row, config)
    assert payload["accounts"] == [173904]
    assert payload["media"] == [{"url": "https://example.com/photo.png"}]
    assert "image" not in payload
    assert "video" not in payload


def test_build_payload_gbp(config):
    row = _base_row(
        platform="gbp",
        first_comment="ignored on gbp",
        media_url="",
        media_format_used="image2_enhanced",
    )
    # Test that account ID is correct; first_comment passes through if set
    payload = socialbu_publish._build_payload(row, config)
    assert payload["accounts"] == [173906]
    # No media_url → no media key
    assert "media" not in payload


# ---------------------------------------------------------------------------
# 12-15: validation
# ---------------------------------------------------------------------------

def test_validate_rejects_non_approved_status(config):
    row = _base_row(status="awaiting_approval")
    err = socialbu_publish._validate_before_publish(row, config)
    assert err is not None
    assert "approved" in err.lower()


def test_validate_rejects_instagram_without_media(config):
    row = _base_row(platform="instagram", media_url="")
    err = socialbu_publish._validate_before_publish(row, config)
    assert err is not None
    assert "media_url" in err.lower() or "media" in err.lower()


def test_validate_passes_facebook_without_media(config):
    row = _base_row(platform="facebook", media_url="")
    err = socialbu_publish._validate_before_publish(row, config)
    assert err is None


def test_validate_rejects_empty_caption(config):
    row = _base_row(caption="")
    err = socialbu_publish._validate_before_publish(row, config)
    assert err is not None
    assert "caption" in err.lower()


# ---------------------------------------------------------------------------
# 16-19: HTTP behavior
# ---------------------------------------------------------------------------

def test_publish_success(config):
    row = _base_row()
    with patch("tools.socialbu_publish.requests.post") as mock_post:
        mock_post.return_value = _mock_post_response(status_code=200)
        result = socialbu_publish.publish_row(row, config)

    assert result["success"] is True
    assert result["socialbu_post_id"] == "7606301"
    assert result["published_datetime"]  # non-empty ISO timestamp
    assert result["error"] is None
    assert mock_post.call_count == 1


def test_publish_422_no_retry(config):
    row = _base_row()
    resp = MagicMock()
    resp.status_code = 422
    resp.text = '{"message": "validation failed"}'

    with patch("tools.socialbu_publish.requests.post") as mock_post, \
         patch("tools.socialbu_publish.time.sleep") as mock_sleep:
        mock_post.return_value = resp
        result = socialbu_publish.publish_row(row, config)

    assert result["success"] is False
    assert "422" in (result["error"] or "")
    assert mock_post.call_count == 1
    assert mock_sleep.call_count == 0


def test_publish_500_retries(config):
    row = _base_row()
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "internal server error"
    good = _mock_post_response(status_code=200)

    with patch("tools.socialbu_publish.requests.post") as mock_post, \
         patch("tools.socialbu_publish.time.sleep"):
        mock_post.side_effect = [bad, good]
        result = socialbu_publish.publish_row(row, config)

    assert result["success"] is True
    assert result["socialbu_post_id"] == "7606301"
    assert mock_post.call_count == 2


def test_publish_401_no_retry(config):
    row = _base_row()
    resp = MagicMock()
    resp.status_code = 401
    resp.text = "unauthorized"

    with patch("tools.socialbu_publish.requests.post") as mock_post, \
         patch("tools.socialbu_publish.time.sleep") as mock_sleep:
        mock_post.return_value = resp
        result = socialbu_publish.publish_row(row, config)

    assert result["success"] is False
    assert "401" in (result["error"] or "")
    assert mock_post.call_count == 1
    assert mock_sleep.call_count == 0


# ---------------------------------------------------------------------------
# 20: dry-run never calls API
# ---------------------------------------------------------------------------

def test_dry_run_does_not_call_api(config):
    row = _base_row()
    with patch("tools.socialbu_publish.requests.post") as mock_post:
        result = socialbu_publish.publish_row(row, config, dry_run=True)

    assert result["success"] is True
    assert result["socialbu_post_id"] is None
    assert result["published_datetime"] is None
    assert result["payload"]["content"] == row["caption"]
    assert result["payload"]["accounts"] == [173903]
    assert mock_post.call_count == 0


# ---------------------------------------------------------------------------
# 21: video URL is attached under the unified media key
# ---------------------------------------------------------------------------

def test_payload_video_format(config):
    row = _base_row(
        platform="facebook",
        media_url="https://example.com/clip.mp4",
        media_format_used="creatomate_video",
    )
    payload = socialbu_publish._build_payload(row, config)
    assert payload["media"] == [{"url": "https://example.com/clip.mp4"}]
    assert "image" not in payload
    assert "video" not in payload


# ---------------------------------------------------------------------------
# 22: drive ID is converted to download URL
# ---------------------------------------------------------------------------

def test_drive_url_conversion(config):
    row = _base_row(
        platform="facebook",
        media_url="1ABCdefGhi_J4MNepG5q39CmjBRcY0MD4D",
        media_format_used="image2_enhanced",
    )
    payload = socialbu_publish._build_payload(row, config)
    expected = (
        "https://lh3.googleusercontent.com/d/"
        "1ABCdefGhi_J4MNepG5q39CmjBRcY0MD4D"
    )
    assert payload["media"] == [{"url": expected}]
