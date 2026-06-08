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
# 8-11: multipart payload assembly
# ---------------------------------------------------------------------------

def test_build_payload_facebook_with_first_comment(config):
    row = _base_row(first_comment="Book at https://tesrents.com", media_url="")
    data, files = socialbu_publish._build_multipart_payload(row, config)
    assert data["content"] == row["caption"]
    assert data["accounts[]"] == "173903"
    assert data["publish_at"] == "2026-06-01 09:00:00"
    assert data["first_comment"] == "Book at https://tesrents.com"
    assert files == {}


def test_build_payload_facebook_without_first_comment(config):
    row = _base_row(first_comment="", media_url="")
    data, files = socialbu_publish._build_multipart_payload(row, config)
    assert "first_comment" not in data
    assert files == {}


@patch("tools.socialbu_publish._download_media")
def test_build_payload_instagram_with_media(mock_dl, config):
    mock_dl.return_value = (b"fake-png-bytes", "image/png")
    row = _base_row(
        platform="instagram",
        media_url="https://example.com/photo.png",
        media_format_used="creatomate_text_overlay",
    )
    data, files = socialbu_publish._build_multipart_payload(row, config)
    assert data["accounts[]"] == "173904"
    assert "attachments" in files
    filename, body, mime = files["attachments"]
    assert filename == "STR-20260528-FB-01.png"
    assert body == b"fake-png-bytes"
    assert mime == "image/png"
    mock_dl.assert_called_once_with("https://example.com/photo.png")


def test_build_payload_gbp(config):
    row = _base_row(
        platform="gbp",
        first_comment="ignored on gbp",
        media_url="",
        media_format_used="image2_enhanced",
    )
    # Test that account ID is correct; first_comment passes through if set
    data, files = socialbu_publish._build_multipart_payload(row, config)
    assert data["accounts[]"] == "173906"
    # No media_url → empty files dict
    assert files == {}


# ---------------------------------------------------------------------------
# GBP CTA button (options[call_to_action]) — shape confirmed via live probe
# ---------------------------------------------------------------------------

def test_build_payload_gbp_call_button(config):
    """GBP row with cta_type=call attaches a CALL button and no URL."""
    row = _base_row(platform="gbp", media_url="", cta_type="call")
    data, _ = socialbu_publish._build_multipart_payload(row, config)
    assert data["options[call_to_action]"] == "CALL"
    # CALL uses the verified listing number — no URL field.
    assert "options[call_to_action_url]" not in data


def test_build_payload_gbp_learn_more_includes_url(config):
    """GBP row with cta_type=visit maps to LEARN_MORE and pulls a URL from
    config (contact.booking_url or contact.website)."""
    row = _base_row(platform="gbp", media_url="", cta_type="visit")
    data, _ = socialbu_publish._build_multipart_payload(row, config)
    assert data["options[call_to_action]"] == "LEARN_MORE"
    assert data["options[call_to_action_url]"].startswith("http")


def test_build_payload_gbp_no_cta_type_no_button(config):
    """GBP row with an unmappable/empty cta_type attaches no button field."""
    row = _base_row(platform="gbp", media_url="", cta_type="dm")
    data, _ = socialbu_publish._build_multipart_payload(row, config)
    assert "options[call_to_action]" not in data


def test_build_payload_facebook_no_cta_button(config):
    """FB rows are unaffected — no CTA-button field even with cta_type=call."""
    row = _base_row(platform="facebook", cta_type="call", media_url="")
    data, _ = socialbu_publish._build_multipart_payload(row, config)
    assert "options[call_to_action]" not in data
    assert "options[call_to_action_url]" not in data


def test_build_payload_instagram_no_cta_button(config):
    """IG rows are unaffected by the GBP CTA-button logic."""
    # Patch media download since IG requires media.
    with patch("tools.socialbu_publish._download_media") as mock_dl:
        mock_dl.return_value = (b"fake-bytes", "image/jpeg")
        row = _base_row(
            platform="instagram",
            cta_type="call",
            media_url="https://example.com/photo.jpg",
        )
        data, _ = socialbu_publish._build_multipart_payload(row, config)
    assert "options[call_to_action]" not in data


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

@patch("tools.socialbu_publish._download_media")
def test_publish_success(mock_dl, config):
    mock_dl.return_value = (b"fake-bytes", "image/jpeg")
    row = _base_row()
    with patch("tools.socialbu_publish.requests.post") as mock_post:
        mock_post.return_value = _mock_post_response(status_code=200)
        result = socialbu_publish.publish_row(row, config)

    assert result["success"] is True
    assert result["socialbu_post_id"] == "7606301"
    assert result["published_datetime"]  # non-empty ISO timestamp
    assert result["error"] is None
    assert mock_post.call_count == 1
    # Verify the request was multipart, not JSON
    _, kwargs = mock_post.call_args
    assert "data" in kwargs
    assert "files" in kwargs
    assert "json" not in kwargs
    assert kwargs["files"] is not None
    assert "attachments" in kwargs["files"]
    # No Content-Type header — requests sets the multipart boundary
    assert "Content-Type" not in kwargs["headers"]


@patch("tools.socialbu_publish._download_media")
def test_publish_422_no_retry(mock_dl, config):
    mock_dl.return_value = (b"fake-bytes", "image/jpeg")
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


@patch("tools.socialbu_publish._download_media")
def test_publish_500_retries(mock_dl, config):
    mock_dl.return_value = (b"fake-bytes", "image/jpeg")
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


@patch("tools.socialbu_publish._download_media")
def test_publish_401_no_retry(mock_dl, config):
    mock_dl.return_value = (b"fake-bytes", "image/jpeg")
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

@patch("tools.socialbu_publish._download_media")
def test_dry_run_does_not_call_api(mock_dl, config):
    mock_dl.return_value = (b"fake-bytes", "image/jpeg")
    row = _base_row()
    with patch("tools.socialbu_publish.requests.post") as mock_post:
        result = socialbu_publish.publish_row(row, config, dry_run=True)

    assert result["success"] is True
    assert result["socialbu_post_id"] is None
    assert result["published_datetime"] is None
    assert result["payload"]["content"] == row["caption"]
    assert result["payload"]["accounts[]"] == "173903"
    assert result["payload"]["has_media"] is True
    assert result["payload"]["media_filename"] == "STR-20260528-FB-01.jpg"
    assert mock_post.call_count == 0


# ---------------------------------------------------------------------------
# 21: video format produces correct extension
# ---------------------------------------------------------------------------

@patch("tools.socialbu_publish._download_media")
def test_payload_video_format(mock_dl, config):
    mock_dl.return_value = (b"fake-mp4-bytes", "video/mp4")
    row = _base_row(
        platform="facebook",
        media_url="https://example.com/clip.mp4",
        media_format_used="creatomate_video",
    )
    data, files = socialbu_publish._build_multipart_payload(row, config)
    assert "attachments" in files
    filename, body, mime = files["attachments"]
    assert filename.endswith(".mp4")
    assert body == b"fake-mp4-bytes"
    assert mime == "video/mp4"


# ---------------------------------------------------------------------------
# 22: drive ID is converted to lh3 URL
# ---------------------------------------------------------------------------

def test_drive_url_conversion():
    out = socialbu_publish._resolve_media_url("1ABCdefGhi_J4MNepG5q39CmjBRcY0MD4D")
    assert out == "https://lh3.googleusercontent.com/d/1ABCdefGhi_J4MNepG5q39CmjBRcY0MD4D"


# ---------------------------------------------------------------------------
# 23-25: _download_media
# ---------------------------------------------------------------------------

def test_download_media_success():
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "image/jpeg"}
    resp.content = b"fake-jpeg-bytes"
    resp.raise_for_status = MagicMock()
    with patch("tools.socialbu_publish.requests.get") as mock_get:
        mock_get.return_value = resp
        body, ct = socialbu_publish._download_media("https://example.com/x.jpg")
    assert body == b"fake-jpeg-bytes"
    assert ct == "image/jpeg"


def test_download_media_failure():
    import requests as _requests
    with patch("tools.socialbu_publish.requests.get") as mock_get:
        mock_get.side_effect = _requests.RequestException("boom")
        with pytest.raises(ValueError) as exc:
            socialbu_publish._download_media("https://example.com/x.jpg")
    assert "failed to download" in str(exc.value)


def test_download_media_wrong_content_type():
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "text/html"}
    resp.content = b"<html><body>not an image</body></html>"
    resp.raise_for_status = MagicMock()
    with patch("tools.socialbu_publish.requests.get") as mock_get:
        mock_get.return_value = resp
        with pytest.raises(ValueError) as exc:
            socialbu_publish._download_media("https://example.com/x.jpg")
    assert "unexpected Content-Type" in str(exc.value)


# ---------------------------------------------------------------------------
# 26-28: _media_filename
# ---------------------------------------------------------------------------

def test_media_filename_jpeg():
    assert socialbu_publish._media_filename("STR-001", "image/jpeg") == "STR-001.jpg"


def test_media_filename_png():
    assert socialbu_publish._media_filename("STR-001", "image/png") == "STR-001.png"


def test_media_filename_unknown():
    assert socialbu_publish._media_filename("STR-001", "application/octet-stream") == "STR-001.jpg"
