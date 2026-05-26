"""Unit tests for tools/reviews_sync.py — pure processing logic only.

No live API calls. Run with:
    pytest tests/test_reviews_sync.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from tools.reviews_sync import (  # noqa: E402
    EXCERPT_LONG_LIMIT,
    EXCERPT_SHORT_LIMIT,
    STAR_MAP,
    extract_first_name,
    make_excerpt,
    process_review,
)


# --- process_review tests ---


def test_process_review_five_star_with_comment():
    comment = (
        "Great company to work with. Equipment was clean and ready to go. "
        "Owner is super helpful and walked us through everything we needed to know."
    )
    raw = {
        "name": "accounts/108/locations/72/reviews/AbCdEfG",
        "reviewer": {"displayName": "John Smith"},
        "starRating": "FIVE",
        "comment": comment,
        "createTime": "2025-11-15T14:30:00Z",
        "reviewReply": {
            "comment": "Thank you John! We appreciate the kind words.",
            "updateTime": "2025-11-16T10:00:00Z",
        },
    }
    row = process_review(raw)

    assert row["review_id"] == "accounts/108/locations/72/reviews/AbCdEfG"
    assert row["reviewer_first_name"] == "John"
    assert row["star_rating"] == "5"
    assert row["review_text"] == comment
    assert row["review_length"] == str(len(comment))
    assert row["word_count"] == str(len(comment.split()))
    assert row["review_date"] == "2025-11-15T14:30:00Z"
    assert row["owner_reply"] == "Thank you John! We appreciate the kind words."
    assert row["usable_for_social"] == "TRUE"
    # Comment exceeds both excerpt limits so both should end with the ellipsis.
    assert row["excerpt_short"].endswith("…")
    assert row["excerpt_long"].endswith("…")
    assert row["last_used_date"] == ""
    assert row["times_used"] == "0"


def test_process_review_no_comment():
    raw = {
        "name": "reviews/star-only",
        "reviewer": {"displayName": "Jane Doe"},
        "starRating": "FIVE",
        "createTime": "2025-11-15T14:30:00Z",
    }
    row = process_review(raw)

    assert row["usable_for_social"] == "FALSE"
    assert row["review_text"] == ""
    assert row["review_length"] == "0"
    assert row["word_count"] == "0"
    assert row["excerpt_short"] == ""
    assert row["excerpt_long"] == ""
    assert row["owner_reply"] == ""


def test_process_review_short_comment():
    raw = {
        "name": "reviews/short",
        "reviewer": {"displayName": "Bob"},
        "starRating": "FIVE",
        "comment": "Great service folks",  # 3 words — below USABLE_MIN_WORDS
        "createTime": "2025-11-15T14:30:00Z",
    }
    row = process_review(raw)

    assert row["word_count"] == "3"
    assert row["star_rating"] == "5"
    assert row["usable_for_social"] == "FALSE"


def test_process_review_non_five_star():
    raw = {
        "name": "reviews/four-star",
        "reviewer": {"displayName": "Alice Walker"},
        "starRating": "FOUR",
        "comment": "Pretty good overall, but pickup was a bit slow.",
        "createTime": "2025-11-15T14:30:00Z",
    }
    row = process_review(raw)

    assert row["star_rating"] == "4"
    assert row["usable_for_social"] == "FALSE"


# --- STAR_MAP test ---


def test_star_map_all_values():
    assert STAR_MAP["FIVE"] == 5
    assert STAR_MAP["FOUR"] == 4
    assert STAR_MAP["THREE"] == 3
    assert STAR_MAP["TWO"] == 2
    assert STAR_MAP["ONE"] == 1

    # Unknown enum value resolves to 0 (via process_review).
    row = process_review({
        "name": "reviews/unknown",
        "reviewer": {"displayName": "Test"},
        "starRating": "STAR_RATING_UNSPECIFIED",
        "comment": "",
        "createTime": "2025-11-15T14:30:00Z",
    })
    assert row["star_rating"] == "0"


# --- make_excerpt tests ---


def test_excerpt_short_text():
    text = "Short and sweet"
    # Well below both limits.
    assert make_excerpt(text, EXCERPT_SHORT_LIMIT) == text
    assert make_excerpt(text, EXCERPT_LONG_LIMIT) == text
    assert "…" not in make_excerpt(text, EXCERPT_SHORT_LIMIT)


def test_excerpt_long_text():
    text = "The quick brown fox jumps over the lazy dog and runs away"
    result = make_excerpt(text, 30)

    assert result.endswith("…")
    # Word-boundary truncation: no partial word before the ellipsis.
    body = result[:-1]
    assert text.startswith(body)
    # Body must be a prefix that ends on a word from `text`.
    assert body.split()[-1] in text.split()
    # The next word in the source would have exceeded the limit.
    consumed_words = body.split()
    next_word = text.split()[len(consumed_words)]
    assert len(body) + 1 + len(next_word) > 30


def test_excerpt_exact_boundary():
    text = "exactly thirty characters long"  # 30 chars
    assert len(text) == 30
    result = make_excerpt(text, 30)
    assert result == text
    assert "…" not in result


def test_excerpt_empty_text():
    assert make_excerpt("", 30) == ""
    assert make_excerpt("   ", 30) == ""
    assert make_excerpt(None, 30) == ""  # type: ignore[arg-type]


# --- first-name extraction ---


def test_first_name_extraction():
    assert extract_first_name("John Smith") == "John"
    assert extract_first_name("John") == "John"
    assert extract_first_name("") == ""
    assert extract_first_name(None) == ""
    assert extract_first_name("   ") == ""
    assert extract_first_name("Mary Jane Watson") == "Mary"


# --- trailing punctuation ---


def test_make_excerpt_trailing_punctuation():
    # The word-boundary truncation lands on "service," — the comma must be
    # stripped before the ellipsis is appended.
    text = "Great service, very fast and on time"
    result = make_excerpt(text, 15)

    assert result.endswith("…")
    assert ",…" not in result
    # Strip the trailing ellipsis and confirm no trailing punctuation remains.
    body = result[:-1]
    assert not body.endswith((",", ".", ";", ":", "!", "?", "-"))
    assert body == "Great service"


def test_make_excerpt_trailing_period():
    # Trailing period should also be stripped before the ellipsis.
    text = "Done. Now what comes next is more text"
    result = make_excerpt(text, 5)
    assert result.endswith("…")
    assert ".…" not in result


# --- entry point so the file is also runnable directly ---


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
