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
    """LLM-only failures (no deterministic counterpart) are kept."""
    llm_result = {
        "failed_checks": [{
            "check_id": "C1",
            "verdict_level": "soft_fail",
            "location": "caption",
            "description": "Multiple ideas in one post",
            "fix_instruction": "Focus on one idea.",
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
    assert any(f["check_id"] == "C1" for f in merged["failed_checks"])


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
