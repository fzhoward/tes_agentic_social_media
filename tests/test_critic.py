"""Tests for agents/critic.py.

All tests are deterministic and run without external API calls. The
integration test mocks the OpenAI response so the full pipeline (load row →
run deterministic checks → call LLM → merge results → write back) can be
exercised in CI without spending money.

Run from the project root:
    python -m pytest tests/test_critic.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from agents import critic  # noqa: E402
from tools import sheets_helpers  # noqa: E402
from tools.config_loader import Config  # noqa: E402


# --- Fixtures ---

@pytest.fixture
def base_row() -> dict:
    """A clean drafted Content Queue row that should pass deterministic checks."""
    return {
        critic.CQ_ROW_ID: "TEST-CRIT-01",
        critic.CQ_STATUS: "drafted",
        critic.CQ_PLATFORM: "facebook",
        critic.CQ_OBJECTIVE: "brand_awareness",
        critic.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        critic.CQ_FOCUS_EQUIPMENT: "",
        critic.CQ_CTA_TYPE: "save",
        critic.CQ_MEDIA_FORMAT: "image2_enhanced",
        critic.CQ_MEDIA_FORMAT_USED: "image2_enhanced",
        critic.CQ_MEDIA_URL: "drive-id-123",
        critic.CQ_REVIEW_ID: "",
        critic.CQ_CAPTION: (
            "Tight residential lot? Pick the right machine first.\n\n"
            "Zero tail swing keeps the back fence intact.\n\n"
            "Operators clear the lot, finish the dig, and move on with "
            "the day.\n\n"
            "No torn-up grass.\n\nNo fence damage.\n\n"
            "No callbacks the next week from a frustrated homeowner.\n\n"
            "We stage the machine close to your jobsite the night "
            "before whenever possible.\n\n"
            "Morning starts feel routine, not rushed.\n\n"
            "The crew steps off the trailer and gets straight to "
            "work.\n\nWorth thinking about before your next residential "
            "dig job, especially the ones with narrow side yards.\n\n"
            "Save this post for the next residential dig."
        ),
        critic.CQ_CREATIVE_HOOK_TEXT: "Zero swing wins",
        critic.CQ_FIRST_COMMENT: "",
        critic.CQ_CTA_TEXT: "Save this post for the next residential dig.",
        critic.CQ_HOOK_TEXT: (
            "Tight residential lot? Pick the right machine first."
        ),
        critic.CQ_IMAGE_OVERLAY_TEXT: "",
    }


@pytest.fixture
def base_config() -> Config:
    """Minimal Config with contact info populated for GBP button checks."""
    return Config({
        "contact": {
            "booking_url": "https://tesrents.com/book",
            "website": "https://tesrents.com",
        },
    })


@pytest.fixture
def base_catalog_item() -> dict:
    return {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_STATUS: "active",
        critic.CAT_WEIGHT: "11,800 lbs",
        critic.CAT_DIG_DEPTH: "12 ft",
        critic.CAT_REACH: "18 ft",
    }


# --- Deterministic pre-check tests ---

def test_pricing_catches_dollar_amount() -> None:
    """A1 — `$250` and `$1,500/week` are flagged."""
    failures = critic.check_pricing(
        "Reserve the 50G today, rentals start at $250/day for the weekend."
    )
    assert any(f["check_id"] == "A1" for f in failures), failures
    assert any("$250" in f["description"] for f in failures), failures

    failures = critic.check_pricing("Weekly bundle at $1,500/week.")
    assert any(f["check_id"] == "A1" for f in failures)


def test_pricing_catches_banned_words() -> None:
    """A1 — 'affordable' and 'budget-friendly' are flagged."""
    for word in ("affordable", "competitive", "budget-friendly", "cheap"):
        failures = critic.check_pricing(
            f"We are the most {word} option in the area."
        )
        assert any(f["check_id"] == "A1" for f in failures), word


def test_pricing_does_not_false_positive_on_dollar_word() -> None:
    """The pricing regex requires `$` followed by digits — the bare word
    'dollar' is not pricing language."""
    failures = critic.check_pricing(
        "The owner saved a few dollars by choosing the right machine."
    )
    # 'dollars' alone is NOT in the banned-pricing-word list — pass.
    assert failures == []


def test_emoji_catches_common_emoji() -> None:
    """A2 — common emoji are flagged."""
    failures = critic.check_emoji("Great job 🔥 on this site")
    assert any(f["check_id"] == "A2" for f in failures), failures

    failures = critic.check_emoji("✅ Done")
    assert any(f["check_id"] == "A2" for f in failures), failures


def test_emoji_does_not_flag_punctuation_or_accents() -> None:
    """A2 — standard punctuation and accented Latin chars don't trip the
    emoji detector."""
    clean_text = (
        "The 50G is a solid choice. It's compact, capable, and ready "
        "for tight lots. Carrie's review confirms it (5 stars)."
    )
    assert critic.check_emoji(clean_text) == []


def test_markdown_bold_flagged() -> None:
    """B1 — markdown bold (`**text**`) is flagged."""
    failures = critic.check_markdown("This is **bold** text in a caption.")
    assert any(f["check_id"] == "B1" for f in failures), failures


def test_markdown_heading_flagged() -> None:
    """B1 — markdown heading prefix is flagged."""
    failures = critic.check_markdown("# Caption Heading\n\nBody text here.")
    assert any(f["check_id"] == "B1" for f in failures), failures


def test_markdown_bullet_flagged() -> None:
    """B1 — markdown bullet list is flagged."""
    failures = critic.check_markdown("- Item one\n- Item two\n- Item three")
    assert any(f["check_id"] == "B1" for f in failures), failures


def test_markdown_clean_caption_passes() -> None:
    """B1 — vertical-stack formatting (no markdown) passes."""
    clean = "First fragment.\n\nSecond line.\n\nThird line."
    assert critic.check_markdown(clean) == []


def test_em_dash_flagged() -> None:
    """B3 — em dash (U+2014) is flagged."""
    failures = critic.check_em_dash(
        "The 50G — a zero-tail-swing machine — fits anywhere."
    )
    assert any(f["check_id"] == "B3" for f in failures), failures


def test_em_dash_hyphen_does_not_flag() -> None:
    """B3 — regular hyphens and en dashes don't trip the em-dash check."""
    assert critic.check_em_dash("zero-tail-swing - hyphen, en–dash") == []


def test_check_exclamation_clean() -> None:
    """B2 — caption with no exclamation point passes."""
    assert critic.check_exclamation(
        "Tight residential lot. Pick the right machine first."
    ) == []


def test_check_exclamation_found() -> None:
    """B2 — caption containing `!` is flagged."""
    failures = critic.check_exclamation("Great job on this site!")
    assert any(f["check_id"] == "B2" for f in failures), failures
    assert failures[0]["verdict_level"] == "soft_fail"


def test_check_exclamation_multiple() -> None:
    """B2 — multiple `!` chars still produce exactly one failure entry."""
    failures = critic.check_exclamation("Wow! Amazing! Fantastic!")
    b2 = [f for f in failures if f["check_id"] == "B2"]
    assert len(b2) == 1, failures


def test_check_exclamation_description_counts_occurrences() -> None:
    """B2 — description reports the literal number of `!` characters."""
    failures = critic.check_exclamation("Best deal! Call now!")
    assert len(failures) == 1
    assert "2 occurrences" in failures[0]["description"]


def test_check_exclamation_empty() -> None:
    """B2 — empty string is a no-op."""
    assert critic.check_exclamation("") == []


def test_check_hashtags_clean() -> None:
    """B4 — caption with no `#` passes."""
    assert critic.check_hashtags(
        "Compact excavator on a residential lot."
    ) == []


def test_check_hashtags_found() -> None:
    """B4 — caption containing `#equipment` is flagged."""
    failures = critic.check_hashtags("Best machine for the job #equipment")
    assert any(f["check_id"] == "B4" for f in failures), failures
    assert failures[0]["verdict_level"] == "soft_fail"


def test_check_hashtags_multiple() -> None:
    """B4 — multiple hashtags still produce exactly one failure entry."""
    failures = critic.check_hashtags(
        "Heavy lift today #excavator #rental #jobsite"
    )
    b4 = [f for f in failures if f["check_id"] == "B4"]
    assert len(b4) == 1, failures


def test_check_hashtags_empty() -> None:
    """B4 — empty string is a no-op."""
    assert critic.check_hashtags("") == []


def test_check_hashtags_no_false_positive() -> None:
    """B4 — bare `#` not followed by a word char does not trip the check."""
    assert critic.check_hashtags("Number # of units delivered: 3") == []


def test_check_hashtags_numeric_ranking_not_flagged() -> None:
    """B4 — `#1` numeric ranking is not a hashtag (no letters after `#`)."""
    assert critic.check_hashtags("We are #1 in the area") == []


def test_check_hashtags_description_lists_matches() -> None:
    """B4 — failure description names each detected hashtag."""
    failures = critic.check_hashtags("Rent today #construction #TES")
    assert len(failures) == 1
    desc = failures[0]["description"]
    assert "#construction" in desc
    assert "#TES" in desc


# --- B5 / B7 / B9 deterministic line-structure tests ---

REAL_STR_20260531_CAPTION = (
    "Bigger excavator does not always mean a faster job.\n\n"
    "Most people default to the largest machine they think they can "
    "afford.\n\n"
    "That logic works against you more often than it helps.\n\n"
    "Here is how to actually think through the decision.\n\n"
    "Start with access, not power.\n\n"
    "Residential lots in North Florida are tight.\n\n"
    "Narrow gates, fences, soft grass, and close utility lines are the "
    "real constraints.\n\n"
    "A machine that cannot reach the dig area is useless at any size.\n\n"
    "Next, match the machine to the actual dig depth.\n\n"
    "Most residential drainage, footer, and utility work does not require "
    "a large excavator.\n\n"
    "A compact machine with the right dig depth handles the job cleanly."
    "\n\n"
    "Transport matters too.\n\n"
    "A larger machine may require a bigger trailer or a separate haul."
    "\n\n"
    "That adds time, cost, and coordination before you ever break "
    "ground.\n\n"
    "The sweet spot is the smallest machine that meets your depth "
    "requirement and fits your site.\n\n"
    "Not the biggest one that will fit on the truck.\n\n"
    "Getting this right reduces rental time, protects your site, and "
    "avoids access problems mid-job.\n\n"
    "Save this for when you are planning your next dig."
)


def test_check_vertical_stack_clean_passes() -> None:
    """B5 — every content line followed by a blank line passes."""
    caption = "Line one.\n\nLine two.\n\nLine three."
    assert critic.check_vertical_stack(caption) == []


def test_check_vertical_stack_adjacent_lines_fails() -> None:
    """B5 — two content lines directly adjacent fails."""
    caption = "Line one.\nLine two without blank.\n\nLine three."
    failures = critic.check_vertical_stack(caption)
    assert any(f["check_id"] == "B5" for f in failures), failures
    assert failures[0]["verdict_level"] == "soft_fail"


def test_check_vertical_stack_handles_crlf() -> None:
    """B5 — CRLF line endings are normalized; properly stacked still passes."""
    caption = "Line one.\r\n\r\nLine two.\r\n\r\nLine three."
    assert critic.check_vertical_stack(caption) == []


def test_check_vertical_stack_empty_no_op() -> None:
    """B5 — empty caption is a no-op."""
    assert critic.check_vertical_stack("") == []
    assert critic.check_vertical_stack("   \n\n  ") == []


def test_check_vertical_stack_real_caption_passes() -> None:
    """B5 — the real STR-20260531-FB-01 caption passes."""
    assert critic.check_vertical_stack(REAL_STR_20260531_CAPTION) == []


def test_check_fragment_lines_two_fragments_passes() -> None:
    """B7 — caption with two short lines passes."""
    caption = (
        "Stop overbuying.\n\n"
        "Pick the right machine for the site you actually have, "
        "not the biggest one you can fit on a trailer.\n\n"
        "Access first.\n\n"
        "Depth second."
    )
    assert critic.check_fragment_lines(caption) == []


def test_check_fragment_lines_zero_fragments_fails() -> None:
    """B7 — caption with no short lines fails."""
    caption = (
        "Renting a compact excavator on a tight residential lot "
        "saves you both time and headache.\n\n"
        "Operators clear the site quickly and finish the dig without "
        "tearing up the lawn or denting the fence."
    )
    failures = critic.check_fragment_lines(caption)
    assert any(f["check_id"] == "B7" for f in failures), failures
    assert failures[0]["verdict_level"] == "soft_fail"


def test_check_fragment_lines_one_fragment_fails() -> None:
    """B7 — exactly one fragment line is not enough; fails."""
    caption = (
        "Access first.\n\n"
        "Renting a compact excavator on a tight residential lot "
        "saves you both time and headache."
    )
    failures = critic.check_fragment_lines(caption)
    assert any(f["check_id"] == "B7" for f in failures), failures
    assert "1 fragment line" in failures[0]["description"]


def test_check_fragment_lines_real_caption_passes() -> None:
    """B7 — the real STR-20260531-FB-01 caption has >=2 fragment lines."""
    assert critic.check_fragment_lines(REAL_STR_20260531_CAPTION) == []


def test_check_fragment_lines_empty_no_op() -> None:
    """B7 — empty caption is a no-op."""
    assert critic.check_fragment_lines("") == []


def test_check_fragment_lines_fix_instruction_states_count() -> None:
    """B7 — fix_instruction must state the current count and how many more
    are needed, plus the literal mechanical rule (own line, ≤5 words)."""
    caption = (
        "Access first.\n\n"
        "Renting a compact excavator on a tight residential lot "
        "saves you both time and headache."
    )
    failures = critic.check_fragment_lines(caption)
    assert failures and failures[0]["check_id"] == "B7"
    fix = failures[0]["fix_instruction"]
    # current count and the exact gap
    assert "you currently have 1" in fix
    assert "Add 1 more" in fix
    # mechanical rule
    assert "own line" in fix
    assert "5 words or fewer" in fix


def test_check_fragment_lines_fix_instruction_zero() -> None:
    """B7 — when there are zero qualifying fragment lines, fix_instruction
    reports needing 2 more."""
    caption = (
        "Renting a compact excavator on a tight residential lot "
        "saves you both time and headache.\n\n"
        "Operators clear the site quickly and finish the dig without "
        "tearing up the lawn or denting the fence."
    )
    failures = critic.check_fragment_lines(caption)
    assert failures and failures[0]["check_id"] == "B7"
    fix = failures[0]["fix_instruction"]
    assert "you currently have 0" in fix
    assert "Add 2 more" in fix


def test_check_hook_duplication_unique_opening_passes() -> None:
    """B9 — unique opening line passes."""
    caption = "Tight lot, big problem.\n\nPick the right machine first.\n\nSave this."
    assert critic.check_hook_duplication(caption) == []


def test_check_hook_duplication_repeated_opening_fails() -> None:
    """B9 — the opening line repeated later fails."""
    caption = (
        "Tight lot, big problem.\n\n"
        "Pick the right machine first.\n\n"
        "Tight lot, big problem.\n\n"
        "Save this."
    )
    failures = critic.check_hook_duplication(caption)
    assert any(f["check_id"] == "B9" for f in failures), failures
    assert failures[0]["verdict_level"] == "soft_fail"


def test_check_hook_duplication_punctuation_insensitive() -> None:
    """B9 — punctuation/case differences still count as duplication."""
    caption = (
        "Tight lot, big problem!\n\n"
        "Pick the right machine first.\n\n"
        "tight lot big problem\n\n"
        "Save this."
    )
    failures = critic.check_hook_duplication(caption)
    assert any(f["check_id"] == "B9" for f in failures), failures


def test_check_hook_duplication_single_line_no_op() -> None:
    """B9 — fewer than two content lines is a no-op."""
    assert critic.check_hook_duplication("Only one line.") == []
    assert critic.check_hook_duplication("") == []


def test_check_hook_duplication_real_caption_passes() -> None:
    """B9 — the real STR-20260531-FB-01 caption has a unique opening line."""
    assert critic.check_hook_duplication(REAL_STR_20260531_CAPTION) == []


def test_run_deterministic_real_caption_passes_b5_b7_b9(
    base_row, base_config,
) -> None:
    """End-to-end: the real STR-20260531-FB-01 caption produces no B5/B7/B9
    failures and the IDs land in passed_checks."""
    base_row[critic.CQ_CAPTION] = REAL_STR_20260531_CAPTION
    failures, passed, _ = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    failed_ids = {f["check_id"] for f in failures}
    assert "B5" not in failed_ids, failures
    assert "B7" not in failed_ids, failures
    assert "B9" not in failed_ids, failures
    assert "B5" in passed
    assert "B7" in passed
    assert "B9" in passed


def test_sentence_length_splits_correctly() -> None:
    """B6 — sentences split on `.`, `!`, `?` and word count is per sentence."""
    text = (
        "Short sentence. "
        "This sentence has more than eighteen words because we are "
        "stretching it intentionally to exceed the brand voice limit."
    )
    failures = critic.check_sentence_length(text)
    assert any(f["check_id"] == "B6" for f in failures), failures
    # The first sentence (2 words) should NOT trigger; sentence 2 should.
    descriptions = " ".join(f["description"] for f in failures).lower()
    assert "sentence 2" in descriptions, descriptions


def test_sentence_length_clean_passes() -> None:
    """B6 — all sentences ≤ 18 words pass."""
    text = "Short one. Another short one. Eight or nine words is fine here."
    assert critic.check_sentence_length(text) == []


def test_creative_hook_text_seven_words_passes() -> None:
    """B11 — 7 words is the limit; 7-word hook passes."""
    failures = critic.check_creative_hook_text(
        creative_hook="Real reach without tearing up the lot",
        caption_hook="Different opening line entirely",
    )
    assert failures == []


def test_creative_hook_text_eight_words_fails() -> None:
    """B11 — 8+ words is a soft_fail."""
    failures = critic.check_creative_hook_text(
        creative_hook="Real reach without tearing up the lot anywhere",
        caption_hook="Different opening line entirely",
    )
    assert any(f["check_id"] == "B11" for f in failures)
    assert any("8 words" in f["description"] for f in failures)


def test_creative_hook_text_overlap_fails() -> None:
    """B11 — creative_hook_text must be distinct from caption_hook."""
    # Identical → fail
    failures = critic.check_creative_hook_text(
        creative_hook="Zero swing wins",
        caption_hook="Zero swing wins",
    )
    assert any(f["check_id"] == "B11" for f in failures)

    # creative is substring of caption → fail
    failures = critic.check_creative_hook_text(
        creative_hook="Zero swing wins",
        caption_hook="Zero swing wins on tight residential lots.",
    )
    assert any(f["check_id"] == "B11" for f in failures)


def test_caption_length_over_gbp_limit_fails() -> None:
    """D1 — caption longer than 1,500 chars on GBP fails."""
    caption = "a" * 1600
    failures = critic.check_caption_length(caption, "gbp")
    assert any(f["check_id"] == "D1" for f in failures)


def test_caption_length_within_limit_passes() -> None:
    """D1 — caption within the platform limit passes."""
    assert critic.check_caption_length("a" * 1000, "facebook") == []


def test_caption_target_range_low_on_gbp() -> None:
    """B8 — caption below the GBP target (150-200) fails."""
    failures = critic.check_caption_target_range("short", "gbp")
    assert any(f["check_id"] == "B8" for f in failures)


def test_gbp_button_call_no_url_required() -> None:
    """D5/D6 — GBP CALL button does not require a URL."""
    failures = critic.check_gbp_button("gbp", "call", booking_url="", website_url="")
    assert failures == []


def test_gbp_button_learn_more_requires_url() -> None:
    """D6 — LEARN_MORE (mapped from cta_type=visit) requires a URL."""
    failures = critic.check_gbp_button("gbp", "visit", booking_url="", website_url="")
    assert any(f["check_id"] == "D6" for f in failures)


def test_gbp_button_invalid_cta_type() -> None:
    """D5 — cta_type=dm doesn't map to a GBP button."""
    failures = critic.check_gbp_button("gbp", "dm")
    assert any(f["check_id"] == "D5" for f in failures)


def test_gbp_button_non_gbp_platform_no_check() -> None:
    """D5/D6 only fire on GBP — Facebook/Instagram skip them."""
    assert critic.check_gbp_button("facebook", "dm") == []
    assert critic.check_gbp_button("instagram", "save") == []


def test_one_cta_flags_multiple_verbs_in_cta_text() -> None:
    """E2 — two distinct CTA verbs in cta_text is flagged."""
    cta_text = "Save this post then call us to book a rental."
    failures = critic.check_one_cta(cta_text=cta_text)
    assert any(f["check_id"] == "E2" for f in failures), failures


def test_one_cta_single_cta_passes() -> None:
    """E2 — a single CTA in cta_text passes."""
    cta_text = "Call us to book the weekend slot."
    assert critic.check_one_cta(cta_text=cta_text) == []


def test_one_cta_idiomatic_phrase_in_caption_not_flagged() -> None:
    """E2 — idiomatic 'call for' in the body of the caption (not the
    actual CTA) must not be flagged as a second CTA.

    This is the case that motivated checking cta_text rather than the
    whole caption.
    """
    cta_text = "Save this post for the next residential dig."
    caption = (
        "Tight residential lot rentals call for the right tool.\n\n"
        + cta_text
    )
    assert critic.check_one_cta(cta_text=cta_text, caption=caption) == []


# --- E1: CTA is last element (deterministic) — exhaustive matrix.
# The 4 original smoke tests (pass-at-end, fail-on-sign-off, empty-cta_text,
# locate-miss) are FOLDED IN here — they are subsumed by cases 1, 3, 10/11
# and 12 below and removed as standalone tests to avoid literal duplicates.
# Each case calls check_cta_last_element directly: a FAIL is a one-element
# list with check_id == "E1"; a PASS is [].


def _assert_e1_pass(cta_text: str, caption: str) -> None:
    assert critic.check_cta_last_element(
        cta_text=cta_text, caption=caption
    ) == []


def _assert_e1_fail(cta_text: str, caption: str, trailing_quote: str) -> dict:
    """Assert a single E1 failure and return it.

    Also assert the failure carries a non-empty location + fix_instruction and
    that the actual trailing text is quoted in both location and description.
    """
    failures = critic.check_cta_last_element(cta_text=cta_text, caption=caption)
    assert len(failures) == 1, failures
    f = failures[0]
    assert f["check_id"] == "E1"
    assert f["location"].strip()
    assert f["fix_instruction"].strip()
    assert trailing_quote in f["location"], f["location"]
    assert trailing_quote in f["description"], f["description"]
    return f


# Group 1 — Core pass/fail (the locked behavior).

def test_e1_cta_exactly_at_end_passes() -> None:
    """1. CTA is the final content of the caption, nothing after → PASS."""
    cta = "Call us in the first comment."
    _assert_e1_pass(cta, "We just got the new excavator in.\n\n" + cta)


def test_e1_trailing_sentence_after_cta_fails() -> None:
    """2. A full sentence follows the located CTA → FAIL."""
    cta = "Call us in the first comment."
    caption = "New loader in the yard.\n\n" + cta + " We are open all weekend."
    _assert_e1_fail(cta, caption, "We are open all weekend.")


def test_e1_trailing_signoff_own_line_fails() -> None:
    """3. Sign-off on its OWN line after the CTA → FAIL.

    The case the owner specifically cares about: CTA, blank line, sign-off.
    """
    cta = "Call us in the first comment."
    caption = "New loader in the yard.\n\n" + cta + "\n\n— the T.E.S. crew"
    _assert_e1_fail(cta, caption, "— the T.E.S. crew")


def test_e1_trailing_signoff_same_line_fails() -> None:
    """4. Sign-off on the SAME line/block as the CTA → FAIL.

    This is the case Option A's anchor catches that Option B's last-block
    model would miss — lock it.
    """
    cta = "Call us today."
    _assert_e1_fail(cta, cta + " — the T.E.S. crew", "— the T.E.S. crew")


def test_e1_trailing_whitespace_only_passes() -> None:
    """5. Only trailing whitespace/newlines/tabs after the CTA → PASS.

    Exercises the caption[last.end():].strip() emptiness test.
    """
    cta = "Send us a message to get started."
    _assert_e1_pass(cta, "Booking up fast.\n\n" + cta + "  \t\n\n")


# Group 2 — Option-A specifics (the parts most likely to regress).

def test_e1_whitespace_flex_locates_and_passes() -> None:
    """6a. cta_text uses single spaces; the caption renders the same CTA
    across a newline and with a doubled space. The \\s+ flexibility must
    still LOCATE it, so a clean end-of-caption CTA → PASS."""
    cta = "Send us a message to get started."
    rendered = "Send us a message\nto get  started."
    _assert_e1_pass(cta, "Booking up fast.\n\n" + rendered)


def test_e1_whitespace_flex_locates_and_fails_with_trailing() -> None:
    """6b. Same whitespace-flex locate, but with trailing content after the
    rendered CTA → FAIL. Proves the newline/space difference does not cause a
    spurious miss in either direction."""
    cta = "Send us a message to get started."
    rendered = "Send us a message\nto get  started."
    caption = "Booking up fast.\n\n" + rendered + "\n\n— the T.E.S. crew"
    _assert_e1_fail(cta, caption, "— the T.E.S. crew")


def test_e1_case_insensitive_locate_passes() -> None:
    """7. cta_text differs in case from the caption rendering → still locates."""
    caption = "New machine in.\n\nCall us in the first comment."
    _assert_e1_pass("CALL US IN THE FIRST COMMENT.", caption)


def test_e1_recurring_phrase_uses_final_occurrence_passes() -> None:
    """8a. The CTA phrase recurs earlier as a legitimate callback and again as
    the actual closing CTA. The FINAL occurrence is at the end → PASS.

    Note the content sitting BETWEEN the early occurrence and the real CTA
    ("We replied... / Here's the new loader") must NOT false-FAIL — E1 only
    inspects what follows the final occurrence.
    """
    cta = "Send us a message."
    caption = (
        "Last spring? Send us a message. We replied within the hour.\n\n"
        "Here's the new loader.\n\n"
        "Send us a message."
    )
    _assert_e1_pass(cta, caption)


def test_e1_recurring_phrase_final_occurrence_trailing_fails() -> None:
    """8b. Mirror of 8a: the FINAL occurrence has trailing content → FAIL."""
    cta = "Send us a message."
    caption = (
        "Last spring? Send us a message. We replied within the hour.\n\n"
        "Send us a message.\n\nSee you out there."
    )
    _assert_e1_fail(cta, caption, "See you out there.")


def test_e1_special_regex_chars_treated_as_literals_passes() -> None:
    """9a. cta_text contains regex-significant characters (?, (), .). Tokens
    are escaped, so they match as literals and still locate → PASS."""
    cta = "Ready to dig? Reserve the 50G (this weekend)."
    _assert_e1_pass(cta, "Zero tail swing.\n\n" + cta)


def test_e1_special_regex_chars_treated_as_literals_fails_with_trailing() -> None:
    """9b. Same regex-significant cta_text with trailing content → FAIL.
    Guards against an escaping regression in either direction."""
    cta = "Ready to dig? Reserve the 50G (this weekend)."
    _assert_e1_fail(cta, cta + " Limited slots remain.", "Limited slots remain.")


# Group 3 — PASS-by-design silent cases (intent documented in-test).

def test_e1_empty_cta_text_cta_type_none_passes() -> None:
    """10. Empty cta_text → PASS.

    Real-world case: the objective intentionally omits a CTA (cta_type ==
    "none"). The detector takes no cta_type param — empty cta_text alone
    resolves to PASS, so cta_type never changes the outcome.
    """
    _assert_e1_pass("", "Just sharing a look at the yard today.")


def test_e1_empty_cta_text_nonempty_caption_passes() -> None:
    """11. Empty cta_text, non-empty caption → PASS.

    E1 is silent here BY DESIGN: it is the "is the CTA last" check, NOT "is
    there a CTA" (that is E3/A5). The silence is intentional, not a gap.
    """
    _assert_e1_pass("", "We just got the new excavator in. Stop by anytime.")


def test_e1_locate_miss_passes() -> None:
    """12. cta_text present but NOT locatable (rephrased/garbled) → PASS.

    Locate-miss resolves to PASS deliberately: a false E1 failure would
    re-introduce exactly the flip behavior the detector exists to remove,
    masked as deterministic.
    """
    cta = "Reserve your weekend rental online."
    _assert_e1_pass(cta, "We just got the new excavator in. Give us a shout.")


def test_e1_both_empty_passes() -> None:
    """13. Both cta_text and caption empty → PASS (degenerate, no crash)."""
    _assert_e1_pass("", "")


# Group 4 — Integration through run_deterministic_checks / merge.

def test_e1_in_passed_ids_on_clean_row(base_row, base_config) -> None:
    """14. E1 appears in passed IDs on a clean row whose caption ends with
    its CTA (E1 is in PRE_CHECK_IDS)."""
    failures, passed, _ = critic.run_deterministic_checks(
        base_row, None, base_config
    )
    assert "E1" in passed
    assert not any(f["check_id"] == "E1" for f in failures)


def test_merge_blocks_llm_e1_hallucination() -> None:
    """15. A deterministic E1 pass overrides an LLM-reported E1 failure.

    Mirrors the B2/B4 hallucination-block pattern: an LLM E1 failure on a row
    the detector passed must be discarded via det_passed_ids.
    """
    llm_result = {
        "failed_checks": [{
            "check_id": "E1",
            "category": "cta",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "CTA is not last (hallucinated)",
            "fix_instruction": "Move the CTA to the end.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=["E1"],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "E1" for f in merged["failed_checks"])
    assert "E1" in merged["passed_checks"]


def test_e1_deterministic_fail_gates_soft_fail(base_row, base_config) -> None:
    """16. An E1 deterministic FAIL still gates — it drives a soft_fail.

    A trailing sign-off after the row's CTA → E1 in run_deterministic_checks
    failures at soft_fail tier, and that failure drives a soft_fail verdict.
    """
    row = dict(base_row)
    row[critic.CQ_CAPTION] = (
        base_row[critic.CQ_CAPTION] + "\n\n— the T.E.S. crew"
    )
    failures, _, _ = critic.run_deterministic_checks(row, None, base_config)
    e1 = [f for f in failures if f["check_id"] == "E1"]
    assert len(e1) == 1, failures
    assert e1[0]["verdict_level"] == "soft_fail"
    verdict, _ = critic.determine_verdict(e1, revision_round=1)
    assert verdict == "soft_fail"


def test_spec_rounding_catches_12000_vs_11800(base_catalog_item) -> None:
    """G3 — caption claims 12,000 lbs but catalog says 11,800 lbs."""
    caption = "The 50G weighs about 12,000 lbs on the trailer."
    failures = critic.check_spec_rounding(caption, base_catalog_item)
    assert any(f["check_id"] == "G3" for f in failures), failures


def test_spec_rounding_exact_match_passes(base_catalog_item) -> None:
    """G3 — caption stating the exact catalog weight passes."""
    caption = "Weighing in at 11,800 lbs, the 50G ships on a small trailer."
    assert critic.check_spec_rounding(caption, base_catalog_item) == []


def test_spec_rounding_no_catalog_passes() -> None:
    """G3 — no catalog item means no comparison, pass."""
    caption = "The machine weighs about 12,000 lbs on the trailer."
    assert critic.check_spec_rounding(caption, None) == []


def test_catalog_status_active_passes(base_catalog_item) -> None:
    """G2 — active item passes."""
    assert critic.check_catalog_status(base_catalog_item) == []


def test_catalog_status_inactive_fails() -> None:
    """G2 — inactive item fails."""
    item = {critic.CAT_ITEM_ID: "EQ-X", critic.CAT_STATUS: "inactive"}
    failures = critic.check_catalog_status(item)
    assert any(f["check_id"] == "G2" for f in failures)


def test_catalog_status_none_no_check() -> None:
    """G2 — no catalog item means no check; handled as warning by caller."""
    assert critic.check_catalog_status(None) == []


# --- Full deterministic flow ---

def test_clean_draft_no_failures(base_row, base_config) -> None:
    """A clean draft produces no deterministic failures."""
    failures, passed, warnings = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    assert failures == [], failures
    # Passed checks should include the standard pre-check IDs.
    assert "A1" in passed
    assert "A2" in passed
    assert "B1" in passed
    assert "B11" in passed


def test_pricing_draft_hard_fails(base_row, base_config) -> None:
    """Caption with a dollar amount fails A1 (hard_fail)."""
    base_row[critic.CQ_CAPTION] = (
        base_row[critic.CQ_CAPTION] + "\n\nRentals start at $250/day."
    )
    failures, _, _ = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    a1 = [f for f in failures if f["check_id"] == "A1"]
    assert a1, failures
    assert a1[0]["verdict_level"] == "hard_fail"


def test_emoji_draft_hard_fails(base_row, base_config) -> None:
    """Caption with an emoji fails A2 (hard_fail)."""
    base_row[critic.CQ_CAPTION] = base_row[critic.CQ_CAPTION] + " 🔥"
    failures, _, _ = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    a2 = [f for f in failures if f["check_id"] == "A2"]
    assert a2, failures
    assert a2[0]["verdict_level"] == "hard_fail"


def test_markdown_draft_soft_fails(base_row, base_config) -> None:
    """Caption with markdown bold fails B1 (soft_fail)."""
    base_row[critic.CQ_CAPTION] = "Here is **bold** text in the caption body."
    failures, _, _ = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    b1 = [f for f in failures if f["check_id"] == "B1"]
    assert b1, failures
    assert b1[0]["verdict_level"] == "soft_fail"


def test_em_dash_draft_soft_fails(base_row, base_config) -> None:
    """Caption with em dashes fails B3 (soft_fail)."""
    base_row[critic.CQ_CAPTION] = (
        "The 50G — a zero-tail-swing machine — fits tight lots."
    )
    failures, _, _ = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    b3 = [f for f in failures if f["check_id"] == "B3"]
    assert b3
    assert b3[0]["verdict_level"] == "soft_fail"


def test_gbp_caption_over_limit_soft_fails(base_row, base_config) -> None:
    """A GBP draft with a caption over 1,500 chars fails D1."""
    base_row[critic.CQ_PLATFORM] = "gbp"
    base_row[critic.CQ_CAPTION] = "a" * 1600
    failures, _, _ = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    d1 = [f for f in failures if f["check_id"] == "D1"]
    assert d1
    assert d1[0]["verdict_level"] == "soft_fail"


def test_creative_hook_over_seven_words_soft_fails(base_row, base_config) -> None:
    """B11 — creative_hook_text with 8+ words is a soft_fail."""
    base_row[critic.CQ_CREATIVE_HOOK_TEXT] = (
        "Real reach without tearing up the lot anywhere"
    )
    failures, _, _ = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    b11 = [f for f in failures if f["check_id"] == "B11"]
    assert b11
    assert b11[0]["verdict_level"] == "soft_fail"


def test_w1_warning_when_no_media_url(base_row, base_config) -> None:
    """W1 fires as a warning when media_url is empty."""
    base_row[critic.CQ_MEDIA_URL] = ""
    _, _, warnings = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    w1 = [w for w in warnings if w["check_id"] == "W1"]
    assert w1, warnings


def test_w3_warning_when_gbp_near_truncation(base_row, base_config) -> None:
    """W3 fires when a GBP caption is within the truncation warning band."""
    base_row[critic.CQ_PLATFORM] = "gbp"
    base_row[critic.CQ_CAPTION] = "a" * 175  # within 150-200 band
    _, _, warnings = critic.run_deterministic_checks(
        row=base_row, catalog_item=None, config=base_config,
    )
    w3 = [w for w in warnings if w["check_id"] == "W3"]
    assert w3, warnings


# --- Verdict logic tests ---

def test_verdict_pass_no_failures() -> None:
    """No failures → pass."""
    verdict, note = critic.determine_verdict([], revision_round=1)
    assert verdict == "pass"
    assert note == ""


def test_verdict_soft_fail_single() -> None:
    """A single soft_fail → soft_fail."""
    verdict, _ = critic.determine_verdict(
        [{"check_id": "B1", "verdict_level": "soft_fail"}],
        revision_round=1,
    )
    assert verdict == "soft_fail"


def test_verdict_multiple_soft_fails_stay_soft() -> None:
    """Multiple soft_fails do NOT aggregate to hard_fail."""
    failures = [
        {"check_id": "B1", "verdict_level": "soft_fail"},
        {"check_id": "B3", "verdict_level": "soft_fail"},
        {"check_id": "D1", "verdict_level": "soft_fail"},
    ]
    verdict, _ = critic.determine_verdict(failures, revision_round=1)
    assert verdict == "soft_fail"


def test_verdict_mixed_hard_and_soft() -> None:
    """Any hard_fail wins regardless of soft_fails present."""
    failures = [
        {"check_id": "A1", "verdict_level": "hard_fail"},
        {"check_id": "B1", "verdict_level": "soft_fail"},
    ]
    verdict, _ = critic.determine_verdict(failures, revision_round=1)
    assert verdict == "hard_fail"


def test_verdict_revision_round_3_escalates() -> None:
    """revision_round=3 with soft_fail → hard_fail (escalation)."""
    failures = [{"check_id": "B1", "verdict_level": "soft_fail"}]
    verdict, note = critic.determine_verdict(failures, revision_round=3)
    assert verdict == "hard_fail"
    assert "2 revision rounds" in note


def test_verdict_revision_round_2_does_not_escalate() -> None:
    """revision_round=2 still allows soft_fail (escalation is at round 3)."""
    failures = [{"check_id": "B1", "verdict_level": "soft_fail"}]
    verdict, _ = critic.determine_verdict(failures, revision_round=2)
    assert verdict == "soft_fail"


# --- Result merging tests ---

def test_merge_deterministic_takes_precedence() -> None:
    """Deterministic failure on A1 stays even if LLM tries to pass it."""
    det_failures = [{
        "check_id": "A1",
        "category": "non_negotiable",
        "verdict_level": "hard_fail",
        "location": "caption",
        "description": "$ detected",
        "fix_instruction": "remove the dollar",
    }]
    llm_result = {
        "failed_checks": [],
        "passed_checks": ["A1", "B1", "B3"],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=det_failures,
        deterministic_passed=["A2", "B1"],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert any(f["check_id"] == "A1" for f in merged["failed_checks"])
    # A1 must not appear in passed_checks since it failed deterministically.
    assert "A1" not in merged["passed_checks"]


def test_merge_llm_can_add_new_failures() -> None:
    """LLM-only failures (no deterministic counterpart) are kept.

    Uses a still-gating LLM check (C6) — C1-C4 and F1-F3 now route to
    warnings and are no longer suitable as a generic "kept failure" example.
    """
    llm_result = {
        "failed_checks": [{
            "check_id": "C6",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "Positions the business as the cheapest option.",
            "fix_instruction": "Drop the cheap-price framing.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=["A1"],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert any(f["check_id"] == "C6" for f in merged["failed_checks"])


def test_merge_blocks_llm_b2_hallucination() -> None:
    """Deterministic pass on B2 must block any LLM B2 failure entry."""
    llm_result = {
        "failed_checks": [{
            "check_id": "B2",
            "category": "formatting",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "Exclamation point detected (hallucinated)",
            "fix_instruction": "Remove the exclamation points.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=["B2"],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "B2" for f in merged["failed_checks"])
    assert "B2" in merged["passed_checks"]


def test_merge_blocks_llm_b4_hallucination() -> None:
    """Deterministic pass on B4 must block any LLM B4 failure entry."""
    llm_result = {
        "failed_checks": [{
            "check_id": "B4",
            "category": "formatting",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "Hashtag detected (hallucinated)",
            "fix_instruction": "Remove all hashtags.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=["B4"],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "B4" for f in merged["failed_checks"])
    assert "B4" in merged["passed_checks"]


def test_merge_blocks_llm_b5_hallucination() -> None:
    """Deterministic pass on B5 must block any LLM B5 failure entry."""
    llm_result = {
        "failed_checks": [{
            "check_id": "B5",
            "category": "formatting",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "Dense paragraph detected (hallucinated)",
            "fix_instruction": "Add blank lines between content lines.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=["B5"],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "B5" for f in merged["failed_checks"])
    assert "B5" in merged["passed_checks"]


def test_merge_blocks_llm_b7_hallucination() -> None:
    """Deterministic pass on B7 must block any LLM B7 failure entry."""
    llm_result = {
        "failed_checks": [{
            "check_id": "B7",
            "category": "formatting",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "No fragment lines (hallucinated)",
            "fix_instruction": "Add short fragment lines.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=["B7"],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "B7" for f in merged["failed_checks"])
    assert "B7" in merged["passed_checks"]


def test_merge_blocks_llm_b9_hallucination() -> None:
    """Deterministic pass on B9 must block any LLM B9 failure entry."""
    llm_result = {
        "failed_checks": [{
            "check_id": "B9",
            "category": "formatting",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "Caption opens with its own hook (hallucinated)",
            "fix_instruction": "Remove the duplicated hook.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=["B9"],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "B9" for f in merged["failed_checks"])
    assert "B9" in merged["passed_checks"]


def test_merge_routes_warning_tier_to_warnings() -> None:
    """LLM failed_checks entry with verdict_level=warning routes to warnings,
    not failed_checks, and does not gate the verdict."""
    llm_result = {
        "failed_checks": [{
            "check_id": "C7",
            "category": "content_voice",
            "verdict_level": "warning",
            "location": "caption",
            "description": "Voice could be more direct",
            "fix_instruction": "Simplify the language.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=[],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "C7" for f in merged["failed_checks"])
    warning_ids = [w["check_id"] for w in merged["warnings"]]
    assert "C7" in warning_ids
    # The warning entry should carry the original description.
    c7 = next(w for w in merged["warnings"] if w["check_id"] == "C7")
    assert "Voice could be more direct" in c7["description"]


def test_merge_routes_warning_tier_via_registry_lookup() -> None:
    """If LLM omits verdict_level on C7, the warning tier is resolved from
    VERDICT_LEVEL_BY_CHECK and the entry still routes to warnings."""
    llm_result = {
        "failed_checks": [{
            "check_id": "C7",
            "location": "caption",
            "description": "Marketing-agency cadence detected",
            "fix_instruction": "Remove hype language.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=[],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "C7" for f in merged["failed_checks"])
    assert any(w["check_id"] == "C7" for w in merged["warnings"])


def test_merge_warning_tier_excluded_from_passed_checks() -> None:
    """A warning-tier check the LLM ALSO listed in passed_checks must not
    appear in the merged passed_checks once it has been routed to warnings."""
    llm_result = {
        "failed_checks": [{
            "check_id": "C7",
            "verdict_level": "warning",
            "location": "caption",
            "description": "Hype detected",
            "fix_instruction": "Remove hype.",
        }],
        "passed_checks": ["C7", "C1"],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=[],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert "C7" not in merged["passed_checks"]
    assert "C1" in merged["passed_checks"]


def test_merge_warnings_deduplicated() -> None:
    """Warnings from both sources merge and deduplicate by check_id."""
    det_warnings = [{
        "check_id": "W1", "description": "Code-detected W1",
    }]
    llm_result = {
        "failed_checks": [],
        "passed_checks": [],
        "warnings": [
            {"check_id": "W1", "description": "LLM-detected W1 dup"},
            {"check_id": "W2", "description": "Thin input"},
        ],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=[],
        deterministic_warnings=det_warnings,
        llm_result=llm_result,
    )
    warning_ids = [w["check_id"] for w in merged["warnings"]]
    assert warning_ids.count("W1") == 1
    assert "W2" in warning_ids
    # Deterministic version wins on duplicate.
    w1 = next(w for w in merged["warnings"] if w["check_id"] == "W1")
    assert w1["description"] == "Code-detected W1"


# --- C4 model-naming false-negative suppression (Session 29) ---

def test_model_name_in_caption_matches_model_field() -> None:
    """`_model_name_in_caption` returns True when the catalog `model`
    appears in the caption."""
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "Compact Track Loader",
    }
    caption = "Tight residential lot. The John Deere 325G fits the gate."
    assert critic._model_name_in_caption(caption, catalog_item) is True


def test_model_name_in_caption_matches_item_name_field() -> None:
    """Match via `item_name` when `model` doesn't appear but `item_name`
    does."""
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "Compact Track Loader",
    }
    caption = "A compact track loader handles the dig cleanly."
    assert critic._model_name_in_caption(caption, catalog_item) is True


def test_model_name_in_caption_absent_returns_false() -> None:
    """Neither model nor item_name in the caption → False."""
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "Compact Track Loader",
    }
    caption = "Pick the right machine for a tight residential lot."
    assert critic._model_name_in_caption(caption, catalog_item) is False


def test_model_name_in_caption_no_catalog_returns_false() -> None:
    """No catalog item → False."""
    assert critic._model_name_in_caption("any caption", None) is False


def test_model_name_in_caption_empty_caption_returns_false() -> None:
    """Empty caption → False."""
    catalog_item = {critic.CAT_MODEL: "John Deere 325G"}
    assert critic._model_name_in_caption("", catalog_item) is False


def test_model_name_in_caption_case_and_punctuation_insensitive() -> None:
    """Casing and surrounding punctuation don't defeat the match."""
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "",
    }
    caption = "Even on wet clay, the JOHN DEERE 325G! holds the line."
    assert critic._model_name_in_caption(caption, catalog_item) is True


def _c_failure(check_id: str, *, verdict_level: str = "soft_fail",
               subreason: str | None = None) -> dict:
    """Build an LLM C-tier failure entry for merge_results tests."""
    entry = {
        "check_id": check_id,
        "category": "content_voice",
        "verdict_level": verdict_level,
        "location": f"caption — {check_id}",
        "description": f"{check_id} concern.",
        "fix_instruction": f"Fix {check_id}.",
    }
    if subreason is not None:
        entry["subreason"] = subreason
    return entry


def test_merge_c1_c4_soft_fail_route_to_warnings() -> None:
    """C1-C4 are warning-tier: an LLM failure for any of them routes to
    warnings and out of failed_checks, even when the LLM (following the
    unchanged checklist) explicitly reports verdict_level='soft_fail'.

    This is the regression guard for the Session 32 bug: the table value is
    only a fallback in resolved_level, so without the code-side warning
    guard an LLM-reported soft_fail would keep gating.
    """
    for check_id in ("C1", "C2", "C3", "C4"):
        llm_result = {
            "failed_checks": [_c_failure(check_id, verdict_level="soft_fail")],
            "passed_checks": [],
            "warnings": [],
        }
        merged = critic.merge_results(
            deterministic_failures=[],
            deterministic_passed=[],
            deterministic_warnings=[],
            llm_result=llm_result,
        )
        assert not any(
            f["check_id"] == check_id for f in merged["failed_checks"]
        ), f"{check_id} should not gate"
        assert any(
            w["check_id"] == check_id for w in merged["warnings"]
        ), f"{check_id} should route to warnings"


def test_merge_c4_specificity_routes_to_warning_regardless_of_subreason() -> None:
    """C4 no longer gates on any subreason — model_naming and specificity
    both route to warnings now that C4 is warning-tier."""
    for subreason in ("model_naming", "specificity"):
        llm_result = {
            "failed_checks": [
                _c_failure("C4", verdict_level="soft_fail", subreason=subreason),
            ],
            "passed_checks": [],
            "warnings": [],
        }
        merged = critic.merge_results(
            deterministic_failures=[],
            deterministic_passed=[],
            deterministic_warnings=[],
            llm_result=llm_result,
        )
        assert not any(f["check_id"] == "C4" for f in merged["failed_checks"])
        assert any(w["check_id"] == "C4" for w in merged["warnings"])


def test_merge_warning_carries_fix_instruction_and_location() -> None:
    """A C-tier failure routed to a warning preserves fix_instruction and
    location so the approval card can show the owner how to fix it."""
    llm_result = {
        "failed_checks": [{
            "check_id": "C4",
            "category": "content_voice",
            "verdict_level": "soft_fail",
            "subreason": "specificity",
            "location": "sentence 2 of caption",
            "description": "Reads general.",
            "fix_instruction": "Add a concrete job type and site condition.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=[],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    c4 = [w for w in merged["warnings"] if w["check_id"] == "C4"]
    assert len(c4) == 1
    assert c4[0]["fix_instruction"] == "Add a concrete job type and site condition."
    assert c4[0]["location"] == "sentence 2 of caption"


def test_merge_soft_fail_check_still_gates() -> None:
    """A genuinely soft_fail-tier LLM check (e.g. C6) still gates — the
    warning guard only demotes table-designated warning-tier checks. (F1-F3
    were demoted to warning in a later session, so C6 is now the canonical
    still-gating LLM check.)"""
    llm_result = {
        "failed_checks": [{
            "check_id": "C6",
            "category": "content_voice",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "Positions the business as the cheapest option.",
            "fix_instruction": "Drop the cheap-price framing.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=[],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert any(f["check_id"] == "C6" for f in merged["failed_checks"])
    assert not any(w["check_id"] == "C6" for w in merged["warnings"])


# --- F1-F3 objective-alignment demotion (later session) ---
#
# F1 (content type matches assignment), F2 (objective alignment), and F3
# (advisory post delivers standalone value) were demoted from soft_fail to
# warning. Like C1-C4, the checklist still tells the LLM these are soft_fail
# (to keep its judgment strict), so the only thing that demotes them is the
# code-side warning guard in merge_results. These tests lock that guard
# against the exact failure mode S32 §2 caught: an LLM that reports its own
# verdict_level='soft_fail'. A4/A5 still hard-gate the mechanical half of
# objective alignment, so demoting F2 loses no non-negotiable enforcement.

def _f_failure(check_id: str, *, verdict_level: str = "soft_fail") -> dict:
    """Build an LLM F-tier (objective-alignment) failure entry."""
    return {
        "check_id": check_id,
        "category": "objective_alignment",
        "verdict_level": verdict_level,
        "location": f"caption — {check_id}",
        "description": f"{check_id} objective-alignment concern.",
        "fix_instruction": f"Fix {check_id}.",
    }


def test_merge_f1_f3_soft_fail_route_to_warnings() -> None:
    """F1-F3 are warning-tier: an LLM failure for any of them routes to
    warnings and out of failed_checks, even when the LLM (following the
    unchanged checklist) explicitly reports verdict_level='soft_fail'. The
    table value is only a fallback in resolved_level, so this proves the
    code-side warning guard fires — not the trivial omitted-verdict path."""
    for check_id in ("F1", "F2", "F3"):
        llm_result = {
            "failed_checks": [_f_failure(check_id, verdict_level="soft_fail")],
            "passed_checks": [],
            "warnings": [],
        }
        merged = critic.merge_results(
            deterministic_failures=[],
            deterministic_passed=[],
            deterministic_warnings=[],
            llm_result=llm_result,
        )
        assert not any(
            f["check_id"] == check_id for f in merged["failed_checks"]
        ), f"{check_id} should not gate"
        assert any(
            w["check_id"] == check_id for w in merged["warnings"]
        ), f"{check_id} should route to warnings"
        # The routed warning must not drive the verdict to soft_fail.
        verdict, _ = critic.determine_verdict(
            merged["failed_checks"], revision_round=1,
        )
        assert verdict == "pass", f"{check_id} must not gate the verdict"


def test_merge_f1_f3_route_to_warnings_without_explicit_verdict_level() -> None:
    """When the LLM omits verdict_level, F1-F3 still resolve to warning via
    VERDICT_LEVEL_BY_CHECK and route out of failed_checks (fallback path)."""
    for check_id in ("F1", "F2", "F3"):
        llm_result = {
            "failed_checks": [{
                "check_id": check_id,
                "location": "caption",
                "description": f"{check_id} concern.",
                "fix_instruction": f"Fix {check_id}.",
            }],
            "passed_checks": [],
            "warnings": [],
        }
        merged = critic.merge_results(
            deterministic_failures=[],
            deterministic_passed=[],
            deterministic_warnings=[],
            llm_result=llm_result,
        )
        assert not any(f["check_id"] == check_id for f in merged["failed_checks"])
        assert any(w["check_id"] == check_id for w in merged["warnings"])


def test_merge_f_warning_carries_fix_instruction_and_location() -> None:
    """An F-tier failure routed to a warning preserves fix_instruction and
    location so the approval card can show the owner how to fix it."""
    for check_id in ("F1", "F2", "F3"):
        llm_result = {
            "failed_checks": [{
                "check_id": check_id,
                "category": "objective_alignment",
                "verdict_level": "soft_fail",
                "location": f"sentence 2 of caption ({check_id})",
                "description": f"{check_id} reads off-objective.",
                "fix_instruction": f"Re-anchor for {check_id}.",
            }],
            "passed_checks": [],
            "warnings": [],
        }
        merged = critic.merge_results(
            deterministic_failures=[],
            deterministic_passed=[],
            deterministic_warnings=[],
            llm_result=llm_result,
        )
        warn = [w for w in merged["warnings"] if w["check_id"] == check_id]
        assert len(warn) == 1
        assert warn[0]["fix_instruction"] == f"Re-anchor for {check_id}."
        assert warn[0]["location"] == f"sentence 2 of caption ({check_id})"


def test_evaluate_draft_f2_routes_to_warning_not_gating(
    base_row, base_config,
) -> None:
    """End-to-end: an F2 objective-alignment failure (LLM verdict_level=
    'soft_fail') routes to a warning and the overall verdict is pass — F2 no
    longer gates."""
    base_row[critic.CQ_FOCUS_EQUIPMENT] = "EQ-001"
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_STATUS: "active",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "Compact Track Loader",
    }

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [{
                "check_id": "F2",
                "category": "objective_alignment",
                "verdict_level": "soft_fail",
                "location": "caption",
                "description": "Caption drifts from the stated objective.",
                "fix_instruction": "Re-anchor the caption to the objective.",
            }],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=catalog_item,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    failed_ids = {f["check_id"] for f in output["failed_checks"]}
    warning_ids = {w["check_id"] for w in output["warnings"]}
    assert "F2" not in failed_ids, output
    assert "F2" in warning_ids, output
    assert output["verdict"] == "pass", output
    f2_warn = next(w for w in output["warnings"] if w["check_id"] == "F2")
    assert f2_warn["fix_instruction"] == "Re-anchor the caption to the objective."


def test_evaluate_draft_f_demotion_does_not_loosen_c6(
    base_row, base_config,
) -> None:
    """Guardrail: with both an F2 warning-tier failure and a C6 soft_fail in
    the same LLM result, F2 routes to a warning while C6 still gates the
    verdict to soft_fail. Proves the F demotion did not reach C6."""
    base_row[critic.CQ_FOCUS_EQUIPMENT] = "EQ-001"
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_STATUS: "active",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "Compact Track Loader",
    }

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [
                {
                    "check_id": "F2",
                    "category": "objective_alignment",
                    "verdict_level": "soft_fail",
                    "location": "caption",
                    "description": "Off-objective.",
                    "fix_instruction": "Re-anchor.",
                },
                {
                    "check_id": "C6",
                    "category": "content_voice",
                    "verdict_level": "soft_fail",
                    "location": "caption",
                    "description": "Cheapest-option framing.",
                    "fix_instruction": "Drop the cheap-price framing.",
                },
            ],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=catalog_item,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    failed_ids = {f["check_id"] for f in output["failed_checks"]}
    warning_ids = {w["check_id"] for w in output["warnings"]}
    assert "F2" in warning_ids and "F2" not in failed_ids, output
    assert "C6" in failed_ids, output
    assert output["verdict"] == "soft_fail", output


def test_evaluate_draft_c4_routes_to_warning_focus_and_no_focus(
    base_row, base_config,
) -> None:
    """End-to-end: a C4 specificity failure (verdict_level='soft_fail' from
    the LLM) routes to a warning and does NOT gate — identically whether or
    not focus_equipment_id is set. The pre-S32 distinction (focus rows gate
    C4, no-focus rows downgrade) is gone; C4 is warning-tier on all rows."""
    base_row[critic.CQ_CAPTION] = (
        "Tight residential lot. The John Deere 325G fits the gate.\n\n"
        "Zero tail swing.\n\n"
        "No torn-up grass.\n\n"
        "Operators stage the machine the night before whenever possible "
        "so morning starts feel routine, not rushed.\n\n"
        "Save this post for the next residential dig."
    )
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_STATUS: "active",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "Compact Track Loader",
    }

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [{
                "check_id": "C4",
                "category": "content_voice",
                "verdict_level": "soft_fail",
                "subreason": "specificity",
                "location": "caption",
                "description": "No job-type or site-condition specifics.",
                "fix_instruction": "Add concrete job type and site detail.",
            }],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    # focus row (focus_equipment_id set, catalog item present) and no-focus
    # row (empty focus, no catalog) must behave identically for C4.
    for focus_id, catalog in (("EQ-001", catalog_item), ("", None)):
        base_row[critic.CQ_FOCUS_EQUIPMENT] = focus_id
        output = critic.evaluate_draft(
            row=base_row,
            catalog_item=catalog,
            review_text="",
            revision_round=1,
            previous_critic_output=None,
            config=base_config,
            skills={},
            llm_call=fake_llm,
        )
        failed_ids = {f["check_id"] for f in output["failed_checks"]}
        warning_ids = {w["check_id"] for w in output["warnings"]}
        assert "C4" not in failed_ids, (focus_id, output)
        assert "C4" in warning_ids, (focus_id, output)
        # fix_instruction survives onto the routed warning.
        c4_warn = next(w for w in output["warnings"] if w["check_id"] == "C4")
        assert c4_warn["fix_instruction"] == "Add concrete job type and site detail."


def test_system_message_c4_subreason_instruction_present(base_row) -> None:
    """Rule #7 must tell the LLM to tag C4 failures with a subreason and
    not to raise C4 on model-naming grounds when the model is already in
    the caption."""
    system_msg = _build_system_message(base_row)
    assert "subreason" in system_msg
    assert "model_naming" in system_msg
    assert "specificity" in system_msg


# --- Output validation tests ---

def test_output_schema_valid() -> None:
    """A well-formed Critic output produces no validation issues."""
    output = {
        "queue_row_id": "TEST",
        "platform": "facebook",
        "revision_round": 1,
        "verdict": "pass",
        "failed_checks": [],
        "warnings": [],
        "passed_checks": ["A1", "B1"],
        "notes": "",
    }
    assert critic.validate_critic_output(output) == []


def test_output_schema_missing_field() -> None:
    """Missing required fields are reported."""
    output = {"verdict": "pass"}
    issues = critic.validate_critic_output(output)
    assert any("queue_row_id" in i for i in issues)
    assert any("failed_checks" in i for i in issues)


def test_output_schema_failed_check_missing_fix() -> None:
    """A failed_checks entry with no fix_instruction is flagged."""
    output = {
        "queue_row_id": "TEST",
        "platform": "facebook",
        "revision_round": 1,
        "verdict": "soft_fail",
        "failed_checks": [{
            "check_id": "B1",
            "location": "caption",
            "description": "markdown found",
            "fix_instruction": "",
        }],
        "warnings": [],
        "passed_checks": [],
        "notes": "",
    }
    issues = critic.validate_critic_output(output)
    assert any("fix_instruction" in i for i in issues)


def test_output_schema_invalid_verdict() -> None:
    """An unknown verdict string is flagged."""
    output = {
        "queue_row_id": "TEST",
        "platform": "facebook",
        "revision_round": 1,
        "verdict": "kinda_okay",
        "failed_checks": [],
        "warnings": [],
        "passed_checks": [],
        "notes": "",
    }
    issues = critic.validate_critic_output(output)
    assert any("invalid verdict" in i for i in issues)


# --- LLM JSON parsing ---

def test_parse_llm_json_strips_fences() -> None:
    """JSON wrapped in ```json fences is unwrapped."""
    raw = '```json\n{"verdict": "pass"}\n```'
    parsed = critic.parse_llm_json(raw)
    assert parsed == {"verdict": "pass"}


def test_parse_llm_json_locates_object_in_prose() -> None:
    """A JSON object embedded in surrounding prose is extracted."""
    raw = 'Sure thing. Here is the result: {"verdict": "soft_fail"} cheers.'
    parsed = critic.parse_llm_json(raw)
    assert parsed == {"verdict": "soft_fail"}


def test_parse_llm_json_raises_on_garbage() -> None:
    """Garbage input raises ValueError."""
    with pytest.raises(ValueError):
        critic.parse_llm_json("just some text, no JSON here")


# --- evaluate_draft (with injected LLM) ---

def test_evaluate_draft_pass_clean_with_mock_llm(
    base_row, base_config,
) -> None:
    """A clean draft with a mock LLM returning no extra issues → pass."""

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "pass",
            "failed_checks": [],
            "warnings": [],
            "passed_checks": list(critic.ALL_CHECK_IDS),
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=None,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "pass", output
    assert output["failed_checks"] == []


def test_evaluate_draft_hard_fail_pricing(
    base_row, base_config,
) -> None:
    """Caption with `$250` → hard_fail even if LLM tries to pass it."""
    base_row[critic.CQ_CAPTION] = (
        base_row[critic.CQ_CAPTION] + "\n\nRentals start at $250/day."
    )

    def fake_llm(*args, **kwargs):
        return {
            "queue_row_id": "TEST-CRIT-01",
            "platform": "facebook",
            "revision_round": 1,
            "verdict": "pass",
            "failed_checks": [],
            "warnings": [],
            "passed_checks": ["A1"],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=None,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "hard_fail"
    assert any(f["check_id"] == "A1" for f in output["failed_checks"])


def test_evaluate_draft_no_exclamation_passes_b2(
    base_row, base_config,
) -> None:
    """End-to-end: clean caption (no `!`, no `#`) → final verdict has no
    B2 or B4 in failed_checks even when the LLM hallucinates both.

    This is the regression guard for the Session 19 hallucination bug.
    """

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [
                {
                    "check_id": "B2",
                    "category": "formatting",
                    "verdict_level": "soft_fail",
                    "location": "caption",
                    "description": "Exclamation point detected",
                    "fix_instruction": "Remove the exclamation points.",
                },
                {
                    "check_id": "B4",
                    "category": "formatting",
                    "verdict_level": "soft_fail",
                    "location": "caption",
                    "description": "Hashtag detected",
                    "fix_instruction": "Remove all hashtags.",
                },
            ],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    # Sanity: the base_row caption truly contains no `!` or `#word`.
    assert "!" not in base_row[critic.CQ_CAPTION]
    assert not __import__("re").search(r"#\w", base_row[critic.CQ_CAPTION])

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=None,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    failed_ids = {f["check_id"] for f in output["failed_checks"]}
    assert "B2" not in failed_ids, output
    assert "B4" not in failed_ids, output
    assert "B2" in output["passed_checks"]
    assert "B4" in output["passed_checks"]


def test_evaluate_draft_revision_round_3_escalation(
    base_row, base_config,
) -> None:
    """revision_round=3 escalates a soft_fail to hard_fail."""
    base_row[critic.CQ_CAPTION] = "Body with **markdown bold** in it."

    def fake_llm(*args, **kwargs):
        return {
            "queue_row_id": "TEST-CRIT-01",
            "platform": "facebook",
            "revision_round": 3,
            "verdict": "soft_fail",
            "failed_checks": [],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=None,
        review_text="",
        revision_round=3,
        previous_critic_output={"failed_checks": []},
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "hard_fail"
    assert "2 revision rounds" in output["notes"]


# --- C7 (voice anti-pattern) tier & routing ---

def test_c7_warning_does_not_gate_verdict(base_row, base_config) -> None:
    """A row whose only LLM-flagged issue is C7 must verdict `pass` —
    C7 is warning-tier and never blocks."""

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [{
                "check_id": "C7",
                "category": "content_voice",
                "verdict_level": "warning",
                "location": "entire caption",
                "description": (
                    "Influencer cadence detected: 'Let's crush this.'"
                ),
                "fix_instruction": "Remove the rally-cry phrasing.",
            }],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=None,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "pass", output
    assert not any(f["check_id"] == "C7" for f in output["failed_checks"])
    assert any(w["check_id"] == "C7" for w in output["warnings"])


def test_c7_clean_voice_no_warning(base_row, base_config) -> None:
    """A clean, plainspoken caption with no anti-patterns: LLM passes C7,
    nothing about C7 appears in failed_checks or warnings."""

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "pass",
            "failed_checks": [],
            "warnings": [],
            "passed_checks": list(critic.ALL_CHECK_IDS),
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=None,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "pass"
    assert not any(f["check_id"] == "C7" for f in output["failed_checks"])
    assert not any(w["check_id"] == "C7" for w in output["warnings"])
    assert "C7" in output["passed_checks"]


def test_c7_verdict_level_is_warning() -> None:
    """Registry sanity: C7 is warning-tier."""
    assert critic.VERDICT_LEVEL_BY_CHECK["C7"] == "warning"


# --- Integration test with mocked LLM and sheets ---

def test_critique_single_row_writes_back(
    base_row, base_config, monkeypatch,
) -> None:
    """Full pipeline: load row → run pre-checks → call LLM → write back.

    Uses monkeypatched sheet helpers so no Google Sheets calls happen.
    """
    captured_updates: dict = {}

    def fake_find(queue_id, queue_tab, column_name, value, service=None):
        return [(5, dict(base_row))]

    def fake_update(spreadsheet_id, tab_name, row_number, col_updates,
                    service=None):
        captured_updates["row_number"] = row_number
        captured_updates["col_updates"] = col_updates

    def fake_headers(spreadsheet_id, tab_name, service=None):
        return [
            critic.CQ_ROW_ID, critic.CQ_STATUS, critic.CQ_CAPTION,
            critic.CQ_CRITIC_SCORE, critic.CQ_CRITIC_NOTES,
        ]

    monkeypatch.setattr(
        sheets_helpers, "find_rows_by_column_value", fake_find,
    )
    monkeypatch.setattr(sheets_helpers, "update_cells", fake_update)
    monkeypatch.setattr(sheets_helpers, "_get_headers", fake_headers)

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "pass",
            "failed_checks": [],
            "warnings": [],
            "passed_checks": list(critic.ALL_CHECK_IDS),
            "notes": "",
        }

    context = {
        "sheets_service": None,
        "queue_id": "queue-123",
        "queue_tab": "Sheet1",
        "catalog_id": "catalog-123",
        "catalog_tab": "Catalog",
        "reviews_id": "",
        "reviews_tab": "",
        "queue_rows": [base_row],
        "catalog_rows": [],
        "reviews_rows": [],
        "skills": {},
    }

    result = critic.critique_single_row(
        row_id=base_row[critic.CQ_ROW_ID],
        context=context,
        config=base_config,
        revision_round=1,
        previous_critic_output=None,
        dry_run=False,
        llm_call=fake_llm,
    )

    assert result["status"] == "success", result
    assert result["verdict"] == "pass"
    assert result["new_status"] == "awaiting_approval"

    updates = captured_updates.get("col_updates", {})
    assert updates.get(critic.CQ_CRITIC_SCORE) == "pass"
    assert critic.CQ_CRITIC_NOTES in updates

    # critic_notes holds the full Critic output JSON.
    parsed_notes = json.loads(updates[critic.CQ_CRITIC_NOTES])
    assert parsed_notes["revision_round"] == 1
    assert isinstance(parsed_notes["failed_checks"], list)
    assert isinstance(parsed_notes["warnings"], list)
    assert isinstance(parsed_notes["passed_checks"], list)
    assert "notes" in parsed_notes

    assert updates.get(critic.CQ_STATUS) == "awaiting_approval"


def test_critique_single_row_dry_run_no_writes(
    base_row, base_config, monkeypatch,
) -> None:
    """Dry-run does NOT write to the sheet."""
    update_called = []
    monkeypatch.setattr(
        sheets_helpers,
        "update_cells",
        lambda *a, **kw: update_called.append(True),
    )

    def fake_llm(*args, **kwargs):
        return {
            "queue_row_id": base_row[critic.CQ_ROW_ID],
            "platform": "facebook",
            "revision_round": 1,
            "verdict": "pass",
            "failed_checks": [],
            "warnings": [],
            "passed_checks": list(critic.ALL_CHECK_IDS),
            "notes": "",
        }

    context = {
        "sheets_service": None,
        "queue_id": "queue-123",
        "queue_tab": "Sheet1",
        "catalog_id": "catalog-123",
        "catalog_tab": "Catalog",
        "reviews_id": "",
        "reviews_tab": "",
        "queue_rows": [base_row],
        "catalog_rows": [],
        "reviews_rows": [],
        "skills": {},
    }

    result = critic.critique_single_row(
        row_id=base_row[critic.CQ_ROW_ID],
        context=context,
        config=base_config,
        revision_round=1,
        previous_critic_output=None,
        dry_run=True,
        llm_call=fake_llm,
    )

    assert result["status"] == "success"
    assert result["dry_run"] is True
    assert update_called == [], "dry_run should not call update_cells"


def test_critique_skips_non_drafted_row(base_row, base_config) -> None:
    """Critic returns early if the row is not in status=drafted."""
    base_row[critic.CQ_STATUS] = "planned"
    context = {
        "sheets_service": None,
        "queue_id": "queue-123",
        "queue_tab": "Sheet1",
        "catalog_id": "catalog-123",
        "catalog_tab": "Catalog",
        "reviews_id": "",
        "reviews_tab": "",
        "queue_rows": [base_row],
        "catalog_rows": [],
        "reviews_rows": [],
        "skills": {},
    }
    result = critic.critique_single_row(
        row_id=base_row[critic.CQ_ROW_ID],
        context=context,
        config=base_config,
        revision_round=1,
        previous_critic_output=None,
        dry_run=False,
    )
    assert result["status"] == "skipped", result
    assert "planned" in result["reason"]


def test_critique_handles_missing_row(base_config) -> None:
    """An unknown row_id returns a clean error result."""
    context = {
        "sheets_service": None,
        "queue_id": "queue-123",
        "queue_tab": "Sheet1",
        "catalog_id": "catalog-123",
        "catalog_tab": "Catalog",
        "reviews_id": "",
        "reviews_tab": "",
        "queue_rows": [],
        "catalog_rows": [],
        "reviews_rows": [],
        "skills": {},
    }
    result = critic.critique_single_row(
        row_id="NOT-A-REAL-ROW",
        context=context,
        config=base_config,
        revision_round=1,
        previous_critic_output=None,
        dry_run=False,
    )
    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


# --- System-message wiring tests for C4 conditional (Session 27) ---

def _build_system_message(row: dict) -> str:
    """Helper: build the Critic system message for a given row."""
    system_msg, _user_msg = critic.build_critic_messages(
        row=row,
        catalog_item=None,
        review_text="",
        pre_check_failures=[],
        pre_check_passed=[],
        pre_check_warnings=[],
        revision_round=1,
        previous_critic_output=None,
        critic_checklist="",
        brand_voice="",
        platform_style="",
        cta_skill="",
        content_types_skill="",
    )
    return system_msg


def test_system_message_c4_conditional_clause_present(base_row) -> None:
    """The system message must instruct the LLM that C4 is conditional on
    focus_equipment_id and that it must not invent equipment."""
    system_msg = _build_system_message(base_row)

    assert "C4" in system_msg
    assert "focus_equipment_id" in system_msg
    # The reinforcement must explicitly direct the LLM not to fail C4 on
    # missing model and not to fabricate equipment.
    assert "do NOT fail C4" in system_msg
    assert "invent" in system_msg


def test_system_message_c4_clause_mentions_empty_focus(base_row) -> None:
    """When focus_equipment_id is empty, the system message must tell the LLM
    not to require a named machine model."""
    row = dict(base_row)
    row[critic.CQ_FOCUS_EQUIPMENT] = ""
    system_msg = _build_system_message(row)

    # The clause itself is row-independent (it's part of the static hard
    # rules), but the assertion proves the LLM is told how to handle the
    # empty case.
    assert "focus_equipment_id is empty" in system_msg
    assert "named machine model" in system_msg
    assert "site condition" in system_msg or "job type" in system_msg


# --- Session 31: C-check fate decisions ---
# C5 demoted to warning everywhere; C3/C4 demoted to warning on no-focus
# rows; C1/C2/C6 still gate. See Session 31 prompt for the full rationale.


def test_c5_verdict_level_is_warning() -> None:
    """Registry sanity: after Session 31, C5 is warning-tier (was soft_fail)."""
    assert critic.VERDICT_LEVEL_BY_CHECK["C5"] == "warning"


def test_merge_routes_c5_to_warnings() -> None:
    """LLM C5 failure routes to warnings, not failed_checks — same path as C7."""
    llm_result = {
        "failed_checks": [{
            "check_id": "C5",
            "category": "content_voice",
            "verdict_level": "warning",
            "location": "caption",
            "description": "Bare 'North Florida' name-drop adds no meaning.",
            "fix_instruction": "Either ground the location reference or drop it.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=[],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "C5" for f in merged["failed_checks"])
    warning_ids = [w["check_id"] for w in merged["warnings"]]
    assert "C5" in warning_ids


def test_merge_routes_c5_to_warnings_via_registry_lookup() -> None:
    """If LLM omits verdict_level on C5, the warning tier is resolved from
    VERDICT_LEVEL_BY_CHECK and the entry still routes to warnings."""
    llm_result = {
        "failed_checks": [{
            "check_id": "C5",
            "location": "caption",
            "description": "Geographic filler.",
            "fix_instruction": "Tie the place name to a recognizable detail.",
        }],
        "passed_checks": [],
        "warnings": [],
    }
    merged = critic.merge_results(
        deterministic_failures=[],
        deterministic_passed=[],
        deterministic_warnings=[],
        llm_result=llm_result,
    )
    assert not any(f["check_id"] == "C5" for f in merged["failed_checks"])
    assert any(w["check_id"] == "C5" for w in merged["warnings"])


def test_evaluate_draft_c5_only_passes(base_row, base_config) -> None:
    """A draft whose only LLM-flagged issue is C5 → verdict `pass`."""

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [{
                "check_id": "C5",
                "category": "content_voice",
                "verdict_level": "warning",
                "location": "caption",
                "description": "Geographic reference is filler.",
                "fix_instruction": "Drop the bare North Florida mention.",
            }],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=None,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "pass", output
    assert not any(f["check_id"] == "C5" for f in output["failed_checks"])
    assert any(w["check_id"] == "C5" for w in output["warnings"])


def test_merge_c3_c4_route_to_warnings_no_focus_context() -> None:
    """C3 and C4 route to warnings regardless of any focus context. The
    no-focus-vs-focus distinction was removed in S32 — merge_results no
    longer takes a focus flag and treats C3/C4 as warning-tier always."""
    for check_id in ("C3", "C4"):
        llm_result = {
            "failed_checks": [_c_failure(check_id, verdict_level="soft_fail")],
            "passed_checks": [],
            "warnings": [],
        }
        merged = critic.merge_results(
            deterministic_failures=[],
            deterministic_passed=[],
            deterministic_warnings=[],
            llm_result=llm_result,
        )
        assert not any(
            f["check_id"] == check_id for f in merged["failed_checks"]
        )
        assert any(w["check_id"] == check_id for w in merged["warnings"])


def test_merge_c1_c4_route_to_warnings_without_explicit_verdict_level() -> None:
    """When the LLM omits verdict_level, the table fallback still classifies
    C1-C4 as warning-tier and routes them out of failed_checks."""
    for check_id in ("C1", "C2", "C3", "C4"):
        llm_result = {
            "failed_checks": [{
                "check_id": check_id,
                "category": "content_voice",
                "location": "caption",
                "description": f"{check_id} concern.",
                "fix_instruction": f"Fix {check_id}.",
            }],
            "passed_checks": [],
            "warnings": [],
        }
        merged = critic.merge_results(
            deterministic_failures=[],
            deterministic_passed=[],
            deterministic_warnings=[],
            llm_result=llm_result,
        )
        assert not any(
            f["check_id"] == check_id for f in merged["failed_checks"]
        )
        assert any(w["check_id"] == check_id for w in merged["warnings"])


def test_evaluate_draft_no_focus_c4_c5_route_to_warnings(
    base_row, base_config,
) -> None:
    """End-to-end no-focus row: LLM emits C4 specificity + C5 → final verdict
    not gated, both routed to warnings."""
    base_row[critic.CQ_FOCUS_EQUIPMENT] = ""  # no focus

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [
                {
                    "check_id": "C4",
                    "category": "content_voice",
                    "verdict_level": "soft_fail",
                    "subreason": "specificity",
                    "location": "caption",
                    "description": "Reads general.",
                    "fix_instruction": "Add a concrete decision frame.",
                },
                {
                    "check_id": "C5",
                    "category": "content_voice",
                    "verdict_level": "warning",
                    "location": "caption",
                    "description": "Geographic filler.",
                    "fix_instruction": "Ground or drop the place name.",
                },
            ],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=None,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "pass", output
    failed_ids = {f["check_id"] for f in output["failed_checks"]}
    assert "C4" not in failed_ids
    assert "C5" not in failed_ids
    warning_ids = {w["check_id"] for w in output["warnings"]}
    assert "C4" in warning_ids
    assert "C5" in warning_ids


def test_evaluate_draft_c6_still_gates(
    base_row, base_config,
) -> None:
    """C6 (cheap-price positioning) stays soft_fail in S32 — an LLM C6
    failure still gates the verdict to soft_fail. This confirms the warning
    demotion is scoped to C1-C4 and did not loosen C6."""
    base_row[critic.CQ_FOCUS_EQUIPMENT] = "EQ-001"
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_STATUS: "active",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "Compact Track Loader",
    }

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [{
                "check_id": "C6",
                "category": "content_voice",
                "verdict_level": "soft_fail",
                "location": "caption",
                "description": "Positions the business as the cheapest option.",
                "fix_instruction": "Drop the cheap-price framing.",
            }],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=catalog_item,
        review_text="",
        revision_round=1,
        previous_critic_output=None,
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "soft_fail", output
    assert any(f["check_id"] == "C6" for f in output["failed_checks"])


def test_c6_soft_fail_escalates_at_round_3(
    base_row, base_config,
) -> None:
    """The escalation logic is unchanged: a surviving soft_fail (C6) at
    revision_round 3 still escalates to hard_fail. (C1-C4 can no longer
    reach this path since they route to warnings.)"""
    base_row[critic.CQ_FOCUS_EQUIPMENT] = "EQ-001"
    catalog_item = {
        critic.CAT_ITEM_ID: "EQ-001",
        critic.CAT_STATUS: "active",
        critic.CAT_MODEL: "John Deere 325G",
        critic.CAT_ITEM_NAME: "Compact Track Loader",
    }

    def fake_llm(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ):
        return {
            "queue_row_id": row[critic.CQ_ROW_ID],
            "platform": row[critic.CQ_PLATFORM],
            "revision_round": revision_round,
            "verdict": "soft_fail",
            "failed_checks": [{
                "check_id": "C6",
                "category": "content_voice",
                "verdict_level": "soft_fail",
                "location": "caption",
                "description": "Positions the business as the cheapest option.",
                "fix_instruction": "Drop the cheap-price framing.",
            }],
            "warnings": [],
            "passed_checks": [],
            "notes": "",
        }

    output = critic.evaluate_draft(
        row=base_row,
        catalog_item=catalog_item,
        review_text="",
        revision_round=3,
        previous_critic_output={"failed_checks": []},
        config=base_config,
        skills={},
        llm_call=fake_llm,
    )
    assert output["verdict"] == "hard_fail", output
    assert "2 revision rounds" in output["notes"]
