"""Critic agent — QA gate between Drafter and Slack approval.

Evaluates a drafted Content Queue row against the Critic Checklist. Combines
deterministic pre-checks (regex / arithmetic / catalog cross-reference) with
an LLM judgment pass (OpenAI) for the checks that require reading
comprehension. Writes the verdict and full Critic output back to the row.

The Critic deliberately uses a different LLM provider than the Drafter
(Anthropic) to break correlated blind spots between writer and reviewer.

Architecture:
    Deterministic shell — data loading, regex/length/spec pre-checks,
    result merging, verdict logic, revision escalation, sheet writeback.
    LLM core — voice/content/objective judgment checks, fix-instruction
    phrasing for any code-detected violations the LLM hasn't seen.

Usage:
    python -m agents.critic --row-id STR-20260527-FB-01 [--dry-run]
    python -m agents.critic --row-id STR-20260527-FB-01 \\
        --revision-round 2 --previous-output prev.json [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from tools import sheets_helpers, skill_loader
from tools.config_loader import Config, load_config


# --- Constants ---

OPENAI_MODEL: str = "gpt-4o"
OPENAI_MAX_TOKENS: int = 4096

PLATFORM_CHAR_LIMITS: dict[str, int] = {
    "facebook": 63206,
    "instagram": 2200,
    "gbp": 1500,
}

# Per-platform caption-body target ranges (chars). Match the Drafter's
# CAPTION_TARGET_RANGE — the Critic enforces it as a soft_fail at QA time.
CAPTION_TARGET_RANGE: dict[str, tuple[int, int]] = {
    "facebook": (500, 1500),
    "instagram": (800, 1500),
    "gbp": (150, 200),
}

# GBP truncation threshold for the W3 warning.
GBP_TRUNCATION_LOWER: int = 150
GBP_TRUNCATION_UPPER: int = 200
GBP_WARNING_BAND: int = 20

CREATIVE_HOOK_MAX_WORDS: int = 7

SENTENCE_MAX_WORDS: int = 18

# Pricing detection — case-insensitive substring match. Word-boundary regex
# is applied separately for the dollar-amount pattern.
BANNED_PRICING_WORDS: tuple[str, ...] = (
    "affordable",
    "competitive",
    "budget-friendly",
    "budget friendly",
    "cheap",
    "cheapest",
)
# $25, $1,500, $250/day, $1.5k — anything starting with `$` followed by a
# digit, then any run of digits, commas, and decimals.
PRICING_DOLLAR_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")

# Emoji detection — broad enough to catch common emoji ranges, narrow enough
# to avoid false-positives on standard punctuation and accented Latin chars.
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001F77F"   # alchemical symbols
    "\U0001F780-\U0001F7FF"   # geometric shapes extended
    "\U0001F800-\U0001F8FF"   # supplemental arrows-c
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"   # chess / symbols
    "\U0001FA70-\U0001FAFF"   # symbols & pictographs extended-a
    "\U00002600-\U000026FF"   # miscellaneous symbols
    "\U00002700-\U000027BF"   # dingbats
    "\U0001F1E0-\U0001F1FF"   # flags
    "]",
    flags=re.UNICODE,
)

# Markdown detection. Order matters — check `**...**` before `*...*` so we
# don't double-count bold as italic.
MD_BOLD_RE = re.compile(r"\*\*[^*\n]+\*\*")
MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)[^*\n]+(?<!\*)\*(?!\*)")
MD_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+\S")
MD_BULLET_RE = re.compile(r"(?m)^\s*[-*+]\s+\S")

EM_DASH = "—"

# Sentence boundary for B6. Split on `.`, `!`, `?` followed by whitespace or
# end-of-string. Conservative — handles common cases without choking on
# abbreviations (the Drafter strips banned `!` so abbreviation false-positives
# are limited).
SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")

# GBP CTA button types.
GBP_BUTTON_TYPES: set[str] = {
    "CALL",
    "LEARN_MORE",
    "BOOK",
    "ORDER",
    "GET_DIRECTIONS",
    "SIGN_UP",
}
GBP_BUTTONS_REQUIRING_URL: set[str] = {"LEARN_MORE", "BOOK", "ORDER", "SIGN_UP"}

# Strong CTA verbs — only counted when they appear at the start of a clause
# (sentence start, or after "then"/"and"/"or"). This avoids false positives
# on prepositional uses like "call for the right tool" or "Call us to book"
# (where "book" follows "to" and is not the primary action).
CTA_VERBS: tuple[str, ...] = (
    "call",
    "dm",
    "message",
    "save",
    "comment",
    "visit",
    "click",
    "tap",
    "book",
    "stop",
    "swing",
    "send",
)

# Match a CTA verb at the start of a clause. A clause begins at:
#   - start of string
#   - after a sentence terminator (`.!?`) and whitespace
#   - after a clause connector (`then`, `and`, `or`, `also`, `plus`, comma)
CTA_CLAUSE_START_RE = re.compile(
    r"(?:^|(?<=[.!?]\s)|(?<=,\s)|"
    r"(?<=\bthen\s)|(?<=\band\s)|(?<=\bor\s)|"
    r"(?<=\balso\s)|(?<=\bplus\s))"
    r"(" + "|".join(CTA_VERBS) + r")\b",
    flags=re.IGNORECASE,
)

# Numeric-with-unit pattern used by G3 spec-rounding pre-check.
# Captures "12,000 lbs", "14 ft", "3.5 hours" — number, optional comma/decimal,
# optional space, unit word.
SPEC_NUMBER_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(lbs?|pounds?|tons?|ft|feet|in|inch|inches|hp|horsepower|"
    r"hr|hrs|hour|hours|gal|gallons?|psi|cc|cubic\s*feet|cu\.?\s*ft|"
    r"yards?|yds?)",
    flags=re.IGNORECASE,
)


# --- Content Queue column names ---

CQ_ROW_ID = "row_id"
CQ_STATUS = "status"
CQ_PLATFORM = "platform"
CQ_OBJECTIVE = "objective"
CQ_CONTENT_TYPE = "content_type"
CQ_FOCUS_EQUIPMENT = "focus_equipment_id"
CQ_CTA_TYPE = "cta_type"
CQ_MEDIA_FORMAT = "media_format"
CQ_MEDIA_FORMAT_USED = "media_format_used"
CQ_MEDIA_URL = "media_url"
CQ_REVIEW_ID = "review_id"

CQ_CAPTION = "caption"
CQ_CREATIVE_HOOK_TEXT = "creative_hook_text"
CQ_FIRST_COMMENT = "first_comment"
CQ_CTA_TEXT = "cta_text"
CQ_HOOK_TEXT = "hook_text"
CQ_IMAGE_OVERLAY_TEXT = "image_overlay_text"

CQ_CRITIC_SCORE = "critic_score"
CQ_CRITIC_NOTES = "critic_notes"

# --- Catalog column names ---

CAT_ITEM_ID = "item_id"
CAT_STATUS = "status"
CAT_WEIGHT = "weight"
CAT_DIG_DEPTH = "dig_depth"
CAT_REACH = "reach"
CAT_CAPACITY = "capacity"
CAT_HORSEPOWER = "horsepower"
CAT_TAIL_SWING = "tail_swing"

CATALOG_SPEC_FIELDS: tuple[str, ...] = (
    CAT_WEIGHT,
    CAT_DIG_DEPTH,
    CAT_REACH,
    CAT_CAPACITY,
    CAT_HORSEPOWER,
    CAT_TAIL_SWING,
)

ACTIVE_CATALOG_STATUSES: set[str] = {"active", "seasonal", ""}


# --- Reviews Sheet column names ---

REV_REVIEW_ID = "review_id"
REV_REVIEW_TEXT = "review_text"


# --- Categories per check ---

CATEGORY_BY_CHECK: dict[str, str] = {
    # A — non-negotiable
    "A1": "non_negotiable", "A2": "non_negotiable", "A3": "non_negotiable",
    "A4": "non_negotiable", "A5": "non_negotiable", "A6": "non_negotiable",
    # B — formatting
    "B1": "formatting", "B2": "formatting", "B3": "formatting",
    "B4": "formatting", "B5": "formatting", "B6": "formatting",
    "B7": "formatting", "B8": "formatting", "B9": "formatting",
    "B10": "formatting", "B11": "formatting",
    # C — content/voice
    "C1": "content_voice", "C2": "content_voice", "C3": "content_voice",
    "C4": "content_voice", "C5": "content_voice", "C6": "content_voice",
    "C7": "content_voice",
    # D — platform
    "D1": "platform", "D2": "platform", "D3": "platform",
    "D4": "platform", "D5": "platform", "D6": "platform",
    # E — CTA
    "E1": "cta", "E2": "cta", "E3": "cta", "E4": "cta", "E5": "cta",
    # F — objective alignment
    "F1": "objective_alignment", "F2": "objective_alignment",
    "F3": "objective_alignment", "F4": "objective_alignment",
    # G — catalog
    "G1": "catalog", "G2": "catalog", "G3": "catalog",
    # W — warnings
    "W1": "warning", "W2": "warning", "W3": "warning",
}

# Verdict level per check ID (hard_fail vs soft_fail vs warning).
VERDICT_LEVEL_BY_CHECK: dict[str, str] = {
    "A1": "hard_fail", "A2": "hard_fail", "A3": "hard_fail",
    "A4": "hard_fail", "A5": "hard_fail", "A6": "hard_fail",
    "B1": "soft_fail", "B2": "soft_fail", "B3": "soft_fail",
    "B4": "soft_fail", "B5": "soft_fail", "B6": "soft_fail",
    "B7": "soft_fail", "B8": "soft_fail", "B9": "soft_fail",
    "B10": "soft_fail", "B11": "soft_fail",
    "C1": "soft_fail", "C2": "soft_fail", "C3": "soft_fail",
    "C4": "soft_fail", "C5": "soft_fail", "C6": "soft_fail",
    "C7": "soft_fail",
    "D1": "soft_fail", "D2": "soft_fail", "D3": "soft_fail",
    "D4": "soft_fail", "D5": "soft_fail", "D6": "soft_fail",
    "E1": "soft_fail", "E2": "soft_fail", "E3": "soft_fail",
    "E4": "soft_fail", "E5": "soft_fail",
    "F1": "soft_fail", "F2": "soft_fail", "F3": "soft_fail", "F4": "soft_fail",
    "G1": "soft_fail", "G2": "soft_fail", "G3": "soft_fail",
    "W1": "warning", "W2": "warning", "W3": "warning",
}

# Checks the deterministic pre-check block evaluates. Other check IDs are
# the LLM's responsibility unless overridden.
PRE_CHECK_IDS: tuple[str, ...] = (
    "A1", "A2", "B1", "B3", "B6", "B8", "B11",
    "D1", "D5", "D6", "E2", "G3",
)

ALL_CHECK_IDS: tuple[str, ...] = tuple(
    cid for cid in VERDICT_LEVEL_BY_CHECK if not cid.startswith("W")
)


# --- Helpers ---

def _count_words(text: str) -> int:
    return len([w for w in re.split(r"\s+", (text or "").strip()) if w])


def _split_sentences(text: str) -> list[str]:
    """Split caption text into sentences for B6.

    Returns a list of non-empty, stripped sentence strings. Splits on `.!?`
    followed by whitespace or end-of-string. Newlines inside paragraphs are
    treated as line breaks (vertical stack formatting) and DO NOT split
    sentences on their own — only terminal punctuation does.
    """
    if not text:
        return []
    raw = SENTENCE_SPLIT_RE.split(text)
    return [s.strip() for s in raw if s and s.strip()]


def _make_failure(
    check_id: str,
    location: str,
    description: str,
    fix_instruction: str,
) -> dict:
    return {
        "check_id": check_id,
        "category": CATEGORY_BY_CHECK.get(check_id, "unknown"),
        "verdict_level": VERDICT_LEVEL_BY_CHECK.get(check_id, "soft_fail"),
        "location": location,
        "description": description,
        "fix_instruction": fix_instruction,
    }


def _make_warning(check_id: str, description: str) -> dict:
    return {
        "check_id": check_id,
        "description": description,
    }


# --- Deterministic pre-checks ---

def check_pricing(caption: str) -> list[dict]:
    """A1 — pricing language. Returns failures list (possibly empty)."""
    failures: list[dict] = []
    if not caption:
        return failures

    dollar_match = PRICING_DOLLAR_RE.search(caption)
    if dollar_match:
        snippet = caption[
            max(0, dollar_match.start() - 20): dollar_match.end() + 20
        ].strip()
        failures.append(_make_failure(
            check_id="A1",
            location=f"caption — near {snippet!r}",
            description=(
                f"Pricing language detected: {dollar_match.group(0)!r} "
                f"appears in the caption"
            ),
            fix_instruction=(
                f"Remove the dollar amount near {snippet!r}. Replace with "
                f"general framing (e.g., 'Call for rental rates') — no "
                f"dollar signs, no specific prices."
            ),
        ))

    lowered = caption.lower()
    for word in BANNED_PRICING_WORDS:
        if word in lowered:
            failures.append(_make_failure(
                check_id="A1",
                location=f"caption — substring {word!r}",
                description=(
                    f"Banned pricing word {word!r} appears in the caption"
                ),
                fix_instruction=(
                    f"Remove the word {word!r} from the caption. Reframe "
                    f"the value without referencing price positioning."
                ),
            ))
    return failures


def check_emoji(caption: str) -> list[dict]:
    """A2 — emoji. Returns failures list."""
    if not caption:
        return []
    matches = EMOJI_RE.findall(caption)
    if not matches:
        return []
    unique = sorted(set(matches))
    return [_make_failure(
        check_id="A2",
        location="caption",
        description=(
            f"Emoji detected: {''.join(unique)}. Brand voice forbids emoji."
        ),
        fix_instruction=(
            f"Remove all emoji from the caption ({''.join(unique)})."
        ),
    )]


def check_markdown(caption: str) -> list[dict]:
    """B1 — markdown formatting. Returns failures list."""
    failures: list[dict] = []
    if not caption:
        return failures

    if MD_BOLD_RE.search(caption):
        failures.append(_make_failure(
            check_id="B1",
            location="caption",
            description=(
                "Markdown bold (**...**) detected — renders as literal "
                "asterisks on FB/IG/GBP"
            ),
            fix_instruction=(
                "Remove the `**` markers around bold text. Convey emphasis "
                "with sentence structure or a fragment line instead."
            ),
        ))
    elif MD_ITALIC_RE.search(caption):
        # Suppressed when bold also matched (bold pattern contains italic).
        failures.append(_make_failure(
            check_id="B1",
            location="caption",
            description=(
                "Markdown italic (*...*) detected — renders as literal "
                "asterisks on social platforms"
            ),
            fix_instruction=(
                "Remove the `*` markers around italic text. Plain prose only."
            ),
        ))
    if MD_HEADING_RE.search(caption):
        failures.append(_make_failure(
            check_id="B1",
            location="caption",
            description=(
                "Markdown heading (`#`) detected at the start of a line"
            ),
            fix_instruction=(
                "Remove the leading `#` characters. Use a hook line or "
                "fragment line for emphasis instead of a heading."
            ),
        ))
    if MD_BULLET_RE.search(caption):
        failures.append(_make_failure(
            check_id="B1",
            location="caption",
            description=(
                "Markdown bullet list (`- ` or `* ` line prefix) detected — "
                "renders as a literal dash on social platforms"
            ),
            fix_instruction=(
                "Remove the bullet prefixes. Use vertical-stack formatting "
                "(fragment lines separated by blank lines) for list-like "
                "content."
            ),
        ))
    return failures


def check_em_dash(caption: str) -> list[dict]:
    """B3 — em dash. Returns failures list."""
    if not caption or EM_DASH not in caption:
        return []
    return [_make_failure(
        check_id="B3",
        location="caption",
        description=f"Em dash ({EM_DASH}) detected in caption",
        fix_instruction=(
            f"Replace each em dash ({EM_DASH}) with a comma, period, or "
            f"line break. Em dashes are banned in this brand voice."
        ),
    )]


def check_sentence_length(caption: str) -> list[dict]:
    """B6 — sentence length ≤ 18 words. Returns failures list."""
    failures: list[dict] = []
    sentences = _split_sentences(caption)
    for idx, sentence in enumerate(sentences, start=1):
        n = _count_words(sentence)
        if n > SENTENCE_MAX_WORDS:
            failures.append(_make_failure(
                check_id="B6",
                location=f"sentence {idx} of caption",
                description=(
                    f"Sentence {idx} has {n} words "
                    f"(limit: {SENTENCE_MAX_WORDS}): {sentence!r}"
                ),
                fix_instruction=(
                    f"Rewrite sentence {idx} to be {SENTENCE_MAX_WORDS} "
                    f"words or fewer. Consider splitting it into two "
                    f"sentences or trimming connecting clauses."
                ),
            ))
    return failures


def check_caption_target_range(caption: str, platform: str) -> list[dict]:
    """B8 — caption falls within per-platform target range."""
    plat = (platform or "").strip().lower()
    target = CAPTION_TARGET_RANGE.get(plat)
    if target is None:
        return []
    lo, hi = target
    n = len(caption or "")
    if n < lo:
        return [_make_failure(
            check_id="B8",
            location="caption",
            description=(
                f"Caption is {n} chars; below the {plat} target of "
                f"{lo}-{hi} chars"
            ),
            fix_instruction=(
                f"Lengthen the caption to {lo}-{hi} chars by adding "
                f"context, a fragment line, or a specific detail."
            ),
        )]
    if n > hi:
        return [_make_failure(
            check_id="B8",
            location="caption",
            description=(
                f"Caption is {n} chars; above the {plat} target of "
                f"{lo}-{hi} chars"
            ),
            fix_instruction=(
                f"Tighten the caption to {lo}-{hi} chars by removing a "
                f"sentence, merging redundant lines, or cutting "
                f"throat-clearing phrases."
            ),
        )]
    return []


def check_creative_hook_text(
    creative_hook: str,
    caption_hook: str,
) -> list[dict]:
    """B11 — creative_hook_text ≤ 7 words AND distinct from caption_hook."""
    failures: list[dict] = []
    h = (creative_hook or "").strip()
    if not h:
        # Empty creative_hook_text — treat as a soft_fail so the Drafter
        # repopulates it. (Drafter validation should already prevent this.)
        failures.append(_make_failure(
            check_id="B11",
            location="creative_hook_text field",
            description="creative_hook_text is empty",
            fix_instruction=(
                "Populate creative_hook_text with a distinct ≤7-word hook "
                "for the visual creative. It must not be a substring of "
                "caption_hook."
            ),
        ))
        return failures

    n = _count_words(h)
    if n > CREATIVE_HOOK_MAX_WORDS:
        failures.append(_make_failure(
            check_id="B11",
            location="creative_hook_text field",
            description=(
                f"creative_hook_text has {n} words "
                f"(max {CREATIVE_HOOK_MAX_WORDS}): {h!r}"
            ),
            fix_instruction=(
                f"Trim creative_hook_text to {CREATIVE_HOOK_MAX_WORDS} "
                f"words or fewer. Keep it punchy and declarative."
            ),
        ))

    ch = (caption_hook or "").strip().lower()
    lh = h.lower()
    if ch and (lh in ch or ch in lh):
        failures.append(_make_failure(
            check_id="B11",
            location="creative_hook_text field",
            description=(
                f"creative_hook_text overlaps with caption_hook: "
                f"creative={h!r}, caption_hook={caption_hook!r}"
            ),
            fix_instruction=(
                "Rewrite creative_hook_text so neither it nor caption_hook "
                "is a substring of the other. Choose a different angle for "
                "the visual hook."
            ),
        ))
    return failures


def check_caption_length(caption: str, platform: str) -> list[dict]:
    """D1 — caption within platform char limit."""
    plat = (platform or "").strip().lower()
    limit = PLATFORM_CHAR_LIMITS.get(plat)
    if limit is None:
        return []
    n = len(caption or "")
    if n > limit:
        return [_make_failure(
            check_id="D1",
            location="caption",
            description=(
                f"Caption is {n} chars; exceeds {plat} hard limit of {limit}"
            ),
            fix_instruction=(
                f"Trim the caption to ≤ {limit} chars. Cut secondary "
                f"context, examples, or repeated phrasing first."
            ),
        )]
    return []


def check_gbp_button(
    platform: str,
    cta_type: str,
    booking_url: str = "",
    website_url: str = "",
) -> list[dict]:
    """D5 + D6 — GBP button type valid and URL provided when required."""
    if (platform or "").strip().lower() != "gbp":
        return []
    failures: list[dict] = []

    # The Drafter encodes the GBP button intent via cta_type; map to button.
    cta_to_button = {
        "call": "CALL",
        "visit": "LEARN_MORE",
        "book": "BOOK",
        "directions": "GET_DIRECTIONS",
        "click": "LEARN_MORE",
    }
    button = cta_to_button.get((cta_type or "").strip().lower())
    if button is None:
        # cta_type doesn't map to a GBP button (e.g., dm/save/comment) — those
        # are not valid on GBP. Flag as D5.
        if (cta_type or "").strip().lower() not in ("", "none"):
            failures.append(_make_failure(
                check_id="D5",
                location="cta_type field",
                description=(
                    f"cta_type={cta_type!r} does not map to a valid GBP "
                    f"button type. Valid: {sorted(GBP_BUTTON_TYPES)}"
                ),
                fix_instruction=(
                    "Switch cta_type to one of call/visit/book/directions/"
                    "click (or 'none' if no button is needed)."
                ),
            ))
        return failures

    if button not in GBP_BUTTON_TYPES:
        failures.append(_make_failure(
            check_id="D5",
            location="cta button",
            description=(
                f"GBP button type {button!r} is invalid. Valid: "
                f"{sorted(GBP_BUTTON_TYPES)}"
            ),
            fix_instruction=(
                f"Set the GBP button type to one of "
                f"{sorted(GBP_BUTTON_TYPES)}."
            ),
        ))
        return failures

    if button in GBP_BUTTONS_REQUIRING_URL:
        url = (booking_url or website_url or "").strip()
        if not url:
            failures.append(_make_failure(
                check_id="D6",
                location="cta button URL",
                description=(
                    f"GBP {button} button requires a URL but none is "
                    f"configured (contact.booking_url and contact.website "
                    f"are both empty)"
                ),
                fix_instruction=(
                    f"Configure contact.booking_url or contact.website in "
                    f"business_config.yaml so the GBP {button} button has "
                    f"a destination."
                ),
            ))
    return failures


def check_one_cta(cta_text: str, caption: str = "") -> list[dict]:
    """E2 — count CTA-like phrases.

    The CTA lives in the dedicated ``cta_text`` field — that is the
    authoritative target for this check. Scanning the full caption is
    unreliable because idiomatic English ("call for", "save you time",
    "comment on") would produce false positives.

    Falls back to the last paragraph of the caption only when ``cta_text``
    is empty (some objectives intentionally omit the CTA via cta_type=none,
    in which case nothing to check).
    """
    target = (cta_text or "").strip()
    if not target:
        if not caption:
            return []
        # Last paragraph fallback when cta_text isn't populated.
        parts = re.split(r"\n\s*\n", caption.strip())
        target = parts[-1].strip() if parts else ""
        if not target:
            return []

    matches = CTA_CLAUSE_START_RE.findall(target)
    distinct_verbs = sorted({m.lower() for m in matches})
    if len(distinct_verbs) > 1:
        return [_make_failure(
            check_id="E2",
            location="cta_text field",
            description=(
                f"Multiple CTA verbs detected in cta_text: "
                f"{distinct_verbs}. Brand voice allows one CTA per post."
            ),
            fix_instruction=(
                f"Keep only one of the CTA verbs ({distinct_verbs}) in "
                f"cta_text. Remove the others; the post should have one "
                f"clear next action."
            ),
        )]
    return []


def check_spec_rounding(caption: str, catalog_item: dict | None) -> list[dict]:
    """G3 — flag numbers in the caption that don't match catalog spec values.

    Conservative: only flag when the caption number is close to a catalog
    spec value but not exact (within 10% — typical rounding range). Numbers
    far from any spec value are assumed to refer to something else
    (durations, prices, dimensions of jobsites, etc.) and the LLM is left to
    judge them.
    """
    if not caption or not catalog_item:
        return []

    spec_numbers: list[tuple[str, float]] = []
    for field in CATALOG_SPEC_FIELDS:
        raw = str(catalog_item.get(field, "") or "").strip()
        if not raw:
            continue
        # Pull the first number out of the spec field.
        m = re.search(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", raw)
        if not m:
            continue
        try:
            spec_numbers.append((field, float(m.group(1).replace(",", ""))))
        except ValueError:
            continue

    if not spec_numbers:
        return []

    failures: list[dict] = []
    for match in SPEC_NUMBER_RE.finditer(caption):
        raw_number = match.group(1)
        try:
            cap_value = float(raw_number.replace(",", ""))
        except ValueError:
            continue
        # Find the closest spec value within 10% — that's the likely
        # referent. Equal values mean the caption matches the catalog.
        for field, spec_value in spec_numbers:
            if spec_value == 0:
                continue
            diff_ratio = abs(cap_value - spec_value) / spec_value
            if 0 < diff_ratio <= 0.10:
                failures.append(_make_failure(
                    check_id="G3",
                    location=f"caption — number {match.group(0)!r}",
                    description=(
                        f"Caption states {match.group(0)!r} but catalog "
                        f"{field}={spec_value!r}. Specs must match the "
                        f"catalog exactly — no rounding."
                    ),
                    fix_instruction=(
                        f"Update the caption to use the exact catalog value "
                        f"for {field} ({spec_value!r}) instead of "
                        f"{match.group(0)!r}."
                    ),
                ))
                break  # one match per caption-number is enough.

    return failures


def check_catalog_status(catalog_item: dict | None) -> list[dict]:
    """G2 — catalog item must be active or seasonal.

    Returns empty when catalog_item is None (handled by the caller as a
    warning, not a failure).
    """
    if not catalog_item:
        return []
    status = str(catalog_item.get(CAT_STATUS, "") or "").strip().lower()
    if status in ACTIVE_CATALOG_STATUSES:
        return []
    return [_make_failure(
        check_id="G2",
        location="catalog item",
        description=(
            f"Catalog item status is {status!r}. Only 'active' or "
            f"'seasonal' items should be featured."
        ),
        fix_instruction=(
            f"Either change the focus_equipment_id to an active/seasonal "
            f"item, or update the catalog row's status if this item is "
            f"available."
        ),
    )]


def run_deterministic_checks(
    row: dict,
    catalog_item: dict | None,
    config: Config,
) -> tuple[list[dict], list[str], list[dict]]:
    """Run all deterministic pre-checks against the draft.

    Returns (failed_checks, passed_check_ids, warnings).
    """
    caption = str(row.get(CQ_CAPTION, "") or "")
    creative_hook = str(row.get(CQ_CREATIVE_HOOK_TEXT, "") or "")
    caption_hook = str(row.get(CQ_HOOK_TEXT, "") or "")
    platform = str(row.get(CQ_PLATFORM, "") or "")
    cta_type = str(row.get(CQ_CTA_TYPE, "") or "")
    media_url = str(row.get(CQ_MEDIA_URL, "") or "").strip()

    booking_url = str(config.get("contact.booking_url", "") or "")
    website_url = str(config.get("contact.website", "") or "")

    failures: list[dict] = []
    failures.extend(check_pricing(caption))
    failures.extend(check_emoji(caption))
    failures.extend(check_markdown(caption))
    failures.extend(check_em_dash(caption))
    failures.extend(check_sentence_length(caption))
    failures.extend(check_caption_target_range(caption, platform))
    failures.extend(check_creative_hook_text(creative_hook, caption_hook))
    failures.extend(check_caption_length(caption, platform))
    failures.extend(check_gbp_button(platform, cta_type, booking_url, website_url))
    failures.extend(check_one_cta(
        cta_text=str(row.get(CQ_CTA_TEXT, "") or ""),
        caption=caption,
    ))
    failures.extend(check_spec_rounding(caption, catalog_item))
    failures.extend(check_catalog_status(catalog_item))

    # Passed-check IDs are the pre-check IDs that produced no failure entries.
    failed_ids = {f["check_id"] for f in failures}
    passed_ids = [cid for cid in PRE_CHECK_IDS if cid not in failed_ids]
    # G2 is conditional on catalog_item being present.
    if catalog_item is not None and "G2" not in failed_ids:
        passed_ids.append("G2")

    warnings: list[dict] = []

    # W1 — image not attached yet.
    if not media_url:
        warnings.append(_make_warning(
            "W1",
            "No confirmed media asset attached. Required for Instagram "
            "scheduling.",
        ))

    # W3 — GBP length near truncation threshold.
    if platform.strip().lower() == "gbp":
        n = len(caption)
        if (
            GBP_TRUNCATION_LOWER - GBP_WARNING_BAND <= n
            <= GBP_TRUNCATION_UPPER + GBP_WARNING_BAND
        ):
            warnings.append(_make_warning(
                "W3",
                f"GBP caption is {n} chars — within the warning band of "
                f"the ~{GBP_TRUNCATION_LOWER}-{GBP_TRUNCATION_UPPER} char "
                f"truncation threshold. The hook may be cut off in the "
                f"panel view.",
            ))

    return failures, passed_ids, warnings


# --- LLM evaluation ---

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_llm_json(raw_text: str) -> dict:
    """Extract a JSON object from the LLM response."""
    text = (raw_text or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                f"could not locate JSON object in LLM output: {raw_text!r}"
            )
        text = text[start: end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"LLM output did not parse to an object "
            f"(got {type(parsed).__name__})"
        )
    return parsed


def build_critic_messages(
    row: dict,
    catalog_item: dict | None,
    review_text: str,
    pre_check_failures: list[dict],
    pre_check_passed: list[str],
    pre_check_warnings: list[dict],
    revision_round: int,
    previous_critic_output: dict | None,
    critic_checklist: str,
    brand_voice: str,
    platform_style: str,
    cta_skill: str,
    content_types_skill: str,
) -> tuple[str, str]:
    """Build (system_message, user_message) for the OpenAI call."""
    row_id = str(row.get(CQ_ROW_ID, "") or "")
    platform = str(row.get(CQ_PLATFORM, "") or "")

    pre_check_block = json.dumps(
        {
            "failed_checks": pre_check_failures,
            "passed_checks": pre_check_passed,
            "warnings": pre_check_warnings,
        },
        indent=2,
    )

    revision_block = ""
    if revision_round > 1 and previous_critic_output is not None:
        prev = json.dumps(
            {
                "verdict": previous_critic_output.get("verdict"),
                "failed_checks": previous_critic_output.get(
                    "failed_checks", []
                ),
            },
            indent=2,
        )
        revision_block = (
            f"\n# Previous Critic Output (revision_round={revision_round})\n\n"
            f"This draft was previously evaluated and failed the checks "
            f"below. Verify whether each previous issue was addressed in "
            f"the current draft. Note in your output any previous issues "
            f"that recur.\n\n"
            f"```json\n{prev}\n```\n"
        )

    output_format = """{
  "queue_row_id": "...",
  "platform": "...",
  "revision_round": 1,
  "verdict": "pass | soft_fail | hard_fail",
  "failed_checks": [
    {
      "check_id": "A1",
      "category": "non_negotiable",
      "verdict_level": "hard_fail",
      "location": "sentence 3 of caption",
      "description": "...",
      "fix_instruction": "..."
    }
  ],
  "warnings": [
    {"check_id": "W1", "description": "..."}
  ],
  "passed_checks": ["B1", "B2", "..."],
  "notes": ""
}"""

    catalog_block = ""
    if catalog_item is not None:
        catalog_block = (
            "\n# Catalog Item Record (for G1-G3 verification)\n\n"
            f"```json\n{json.dumps(catalog_item, indent=2)}\n```\n"
        )

    review_block = ""
    if review_text:
        review_block = (
            "\n# Featured Review (for Social Proof fidelity check)\n\n"
            "The caption should reference this review without fabricating "
            "content beyond it. Flag any claim or quote in the caption "
            "that cannot be traced to this text.\n\n"
            f"\"\"\"\n{review_text}\n\"\"\"\n"
        )

    draft_block = {
        "row_id": row_id,
        "platform": platform,
        "objective": str(row.get(CQ_OBJECTIVE, "") or ""),
        "content_type": str(row.get(CQ_CONTENT_TYPE, "") or ""),
        "focus_equipment_id": str(row.get(CQ_FOCUS_EQUIPMENT, "") or ""),
        "cta_type": str(row.get(CQ_CTA_TYPE, "") or ""),
        "media_format": str(row.get(CQ_MEDIA_FORMAT, "") or ""),
        "media_format_used": str(row.get(CQ_MEDIA_FORMAT_USED, "") or ""),
        "media_url": str(row.get(CQ_MEDIA_URL, "") or ""),
        "caption": str(row.get(CQ_CAPTION, "") or ""),
        "creative_hook_text": str(row.get(CQ_CREATIVE_HOOK_TEXT, "") or ""),
        "first_comment": str(row.get(CQ_FIRST_COMMENT, "") or ""),
        "cta_text": str(row.get(CQ_CTA_TEXT, "") or ""),
        "hook_text": str(row.get(CQ_HOOK_TEXT, "") or ""),
        "image_overlay_text": str(row.get(CQ_IMAGE_OVERLAY_TEXT, "") or ""),
    }

    system_message = (
        "You are an independent QA critic for social media drafts produced "
        "by another LLM. Your job is to evaluate the draft against the "
        "Critic Checklist and return a structured JSON verdict.\n\n"
        "Hard rules:\n"
        "1. Return valid JSON only — no markdown fences, no commentary.\n"
        "2. Every entry in `failed_checks` must include a specific, "
        "actionable `fix_instruction`. Vague feedback like 'fix the tone' "
        "is not acceptable. The Drafter must be able to act on the fix "
        "instruction without guessing.\n"
        "3. Evaluate ALL checks in the checklist. Do not short-circuit. "
        "The Drafter needs the complete picture in one pass.\n"
        "4. The deterministic pre-check results provided below have ALREADY "
        "been evaluated by code. Trust them. Do NOT re-evaluate the same "
        "checks unless you have strong evidence the code missed something. "
        "Focus your judgment on the checks that require reading "
        "comprehension (C1-C7, F1-F4, G1) and any voice/content issues.\n"
        "5. Catalog cross-reference: if a catalog record is provided, every "
        "spec claim in the caption must match the catalog exactly. Flag "
        "any rounding or inflation as G3.\n"
        "6. Social Proof fidelity: if a review is provided, the caption "
        "must not fabricate content beyond it. Flag fabricated quotes or "
        "details as A3.\n\n"
        "---\n\n"
        "# Critic Checklist\n\n"
        f"{critic_checklist}\n\n"
        "---\n\n"
        "# Brand Voice\n\n"
        f"{brand_voice}\n\n"
        "---\n\n"
        "# Platform Style\n\n"
        f"{platform_style}\n\n"
        "---\n\n"
        "# CTA Skill\n\n"
        f"{cta_skill}\n\n"
        "---\n\n"
        "# Content Type Definitions\n\n"
        f"{content_types_skill}\n"
    )

    user_message = (
        f"# Draft Under Review\n\n"
        f"```json\n{json.dumps(draft_block, indent=2)}\n```\n"
        f"{catalog_block}"
        f"{review_block}\n"
        f"# Deterministic Pre-Check Results (already evaluated by code)\n\n"
        f"```json\n{pre_check_block}\n```\n"
        f"{revision_block}\n"
        f"# Output Format\n\n"
        f"Return exactly this JSON shape (no markdown fences, no "
        f"commentary). Populate `queue_row_id`, `platform`, "
        f"`revision_round` from the draft and inputs above:\n\n"
        f"{output_format}\n\n"
        f"# Critical Reminders\n\n"
        f"- Every `failed_checks` entry needs `check_id`, `category`, "
        f"`verdict_level`, `location`, `description`, and `fix_instruction`.\n"
        f"- Use the check IDs and verdict_level values from the checklist "
        f"verbatim.\n"
        f"- Merge the pre-check failures into your `failed_checks` array — "
        f"do not drop them. Do not add duplicate entries for checks the "
        f"pre-check block already flagged.\n"
        f"- `passed_checks` is a flat list of check ID strings.\n"
        f"- `revision_round` in the output must be {revision_round}.\n"
    )

    return system_message, user_message


def call_openai(
    system_message: str,
    user_message: str,
    model: str = OPENAI_MODEL,
    max_tokens: int = OPENAI_MAX_TOKENS,
) -> str:
    """Call OpenAI Chat Completions and return the raw text response."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set in the environment")

    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError(
            f"openai SDK import failed: {type(exc).__name__}: {exc}"
        ) from exc

    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
    )
    if not response.choices:
        raise RuntimeError("OpenAI response had no choices")
    content = response.choices[0].message.content or ""
    return content


def get_llm_evaluation(
    row: dict,
    catalog_item: dict | None,
    review_text: str,
    pre_check_failures: list[dict],
    pre_check_passed: list[str],
    pre_check_warnings: list[dict],
    revision_round: int,
    previous_critic_output: dict | None,
    skills: dict[str, str],
) -> dict:
    """Call OpenAI with one JSON-only retry. Returns the parsed dict."""
    common_kwargs = dict(
        row=row,
        catalog_item=catalog_item,
        review_text=review_text,
        pre_check_failures=pre_check_failures,
        pre_check_passed=pre_check_passed,
        pre_check_warnings=pre_check_warnings,
        revision_round=revision_round,
        previous_critic_output=previous_critic_output,
        critic_checklist=skills.get("critic_checklist", ""),
        brand_voice=skills.get("brand_voice", ""),
        platform_style=skills.get("platform_style", ""),
        cta_skill=skills.get("cta", ""),
        content_types_skill=skills.get("content_types", ""),
    )
    system_msg, user_msg = build_critic_messages(**common_kwargs)

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = call_openai(system_msg, user_msg)
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                continue
            raise
        try:
            return parse_llm_json(raw)
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                user_msg = user_msg + (
                    "\n\nIMPORTANT: Return valid JSON only. No markdown "
                    "fences, no commentary. Begin your response with `{` "
                    "and end with `}`.\n"
                )
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("OpenAI retry loop exited without result")


# --- Result merging + verdict logic ---

def merge_results(
    deterministic_failures: list[dict],
    deterministic_passed: list[str],
    deterministic_warnings: list[dict],
    llm_result: dict,
) -> dict:
    """Merge deterministic results with the LLM's JSON output.

    Rule: deterministic results take precedence on conflict. A check ID that
    appears in deterministic_failures is final — even if the LLM tried to
    mark it passed. A check ID that the LLM flagged but deterministic did
    not evaluate is kept as the LLM's call.
    """
    det_failed_ids = {f["check_id"] for f in deterministic_failures}
    det_passed_ids = set(deterministic_passed)

    # Start with deterministic failures (authoritative).
    merged_failures: list[dict] = list(deterministic_failures)

    # Add LLM failures only for check IDs not already in deterministic_failures.
    llm_failures = llm_result.get("failed_checks", []) or []
    for llm_fail in llm_failures:
        if not isinstance(llm_fail, dict):
            continue
        check_id = str(llm_fail.get("check_id", "")).strip()
        if not check_id or check_id in det_failed_ids:
            continue
        # If the LLM tried to fail a check we already passed deterministically,
        # trust the code.
        if check_id in det_passed_ids:
            continue
        # Normalize the entry shape.
        merged_failures.append({
            "check_id": check_id,
            "category": str(
                llm_fail.get("category")
                or CATEGORY_BY_CHECK.get(check_id, "unknown")
            ),
            "verdict_level": str(
                llm_fail.get("verdict_level")
                or VERDICT_LEVEL_BY_CHECK.get(check_id, "soft_fail")
            ),
            "location": str(llm_fail.get("location", "") or ""),
            "description": str(llm_fail.get("description", "") or ""),
            "fix_instruction": str(llm_fail.get("fix_instruction", "") or ""),
        })

    merged_failed_ids = {f["check_id"] for f in merged_failures}

    # Passed checks = union of deterministic + LLM, minus anything in failures.
    llm_passed = llm_result.get("passed_checks", []) or []
    passed_ids: set[str] = set(deterministic_passed)
    for cid in llm_passed:
        if isinstance(cid, str) and cid not in merged_failed_ids:
            passed_ids.add(cid)

    # Warnings — concat deterministic + LLM, dedupe by check_id.
    seen_warning_ids: set[str] = set()
    merged_warnings: list[dict] = []
    for w in deterministic_warnings + (llm_result.get("warnings", []) or []):
        if not isinstance(w, dict):
            continue
        wid = str(w.get("check_id", "")).strip()
        if not wid or wid in seen_warning_ids:
            continue
        seen_warning_ids.add(wid)
        merged_warnings.append({
            "check_id": wid,
            "description": str(w.get("description", "") or ""),
        })

    return {
        "failed_checks": merged_failures,
        "passed_checks": sorted(passed_ids),
        "warnings": merged_warnings,
    }


def determine_verdict(
    failed_checks: list[dict],
    revision_round: int,
) -> tuple[str, str]:
    """Apply the verdict hierarchy. Returns (verdict, escalation_note)."""
    has_hard = any(
        f.get("verdict_level") == "hard_fail" for f in failed_checks
    )
    has_soft = any(
        f.get("verdict_level") == "soft_fail" for f in failed_checks
    )

    if has_hard:
        return "hard_fail", ""
    if has_soft:
        if revision_round >= 3:
            return (
                "hard_fail",
                "Draft failed 2 revision rounds. Remaining soft_fail "
                "issues escalated to hard_fail.",
            )
        return "soft_fail", ""
    return "pass", ""


def evaluate_draft(
    row: dict,
    catalog_item: dict | None,
    review_text: str,
    revision_round: int,
    previous_critic_output: dict | None,
    config: Config,
    skills: dict[str, str],
    llm_call: Any = None,
) -> dict:
    """Run the full Critic evaluation pipeline (without sheet writes).

    Returns the Critic Output JSON structure.

    ``llm_call`` is an optional injection point for tests — when provided,
    it is called with (row, catalog_item, review_text, pre_failures,
    pre_passed, pre_warnings, revision_round, previous_critic_output,
    skills) and must return the parsed LLM dict. When omitted, the real
    OpenAI call is used.
    """
    pre_failures, pre_passed, pre_warnings = run_deterministic_checks(
        row=row, catalog_item=catalog_item, config=config,
    )

    if llm_call is None:
        llm_result = get_llm_evaluation(
            row=row,
            catalog_item=catalog_item,
            review_text=review_text,
            pre_check_failures=pre_failures,
            pre_check_passed=pre_passed,
            pre_check_warnings=pre_warnings,
            revision_round=revision_round,
            previous_critic_output=previous_critic_output,
            skills=skills,
        )
    else:
        llm_result = llm_call(
            row, catalog_item, review_text,
            pre_failures, pre_passed, pre_warnings,
            revision_round, previous_critic_output, skills,
        )

    merged = merge_results(
        deterministic_failures=pre_failures,
        deterministic_passed=pre_passed,
        deterministic_warnings=pre_warnings,
        llm_result=llm_result,
    )
    verdict, escalation_note = determine_verdict(
        merged["failed_checks"], revision_round,
    )

    # Compose notes — start with LLM's notes (if any), then append escalation.
    llm_notes = str(llm_result.get("notes", "") or "").strip()
    notes_parts = [s for s in (llm_notes, escalation_note) if s]
    notes = " ".join(notes_parts)

    return {
        "queue_row_id": str(row.get(CQ_ROW_ID, "") or ""),
        "platform": str(row.get(CQ_PLATFORM, "") or ""),
        "revision_round": revision_round,
        "verdict": verdict,
        "failed_checks": merged["failed_checks"],
        "warnings": merged["warnings"],
        "passed_checks": merged["passed_checks"],
        "notes": notes,
    }


def validate_critic_output(output: dict) -> list[str]:
    """Return a list of validation issues, empty if the output is well-formed."""
    issues: list[str] = []
    required_keys = (
        "queue_row_id", "platform", "revision_round", "verdict",
        "failed_checks", "warnings", "passed_checks", "notes",
    )
    for key in required_keys:
        if key not in output:
            issues.append(f"missing required key: {key}")

    if output.get("verdict") not in ("pass", "soft_fail", "hard_fail"):
        issues.append(f"invalid verdict: {output.get('verdict')!r}")

    failed = output.get("failed_checks", [])
    if not isinstance(failed, list):
        issues.append("failed_checks must be a list")
    else:
        for i, entry in enumerate(failed):
            if not isinstance(entry, dict):
                issues.append(f"failed_checks[{i}] is not an object")
                continue
            for field in (
                "check_id", "location", "description", "fix_instruction",
            ):
                if not str(entry.get(field, "") or "").strip():
                    issues.append(
                        f"failed_checks[{i}] missing or empty {field!r}"
                    )

    warnings = output.get("warnings", [])
    if not isinstance(warnings, list):
        issues.append("warnings must be a list")

    passed = output.get("passed_checks", [])
    if not isinstance(passed, list):
        issues.append("passed_checks must be a list")
    else:
        for i, cid in enumerate(passed):
            if not isinstance(cid, str):
                issues.append(f"passed_checks[{i}] is not a string")

    return issues


# --- Sheet helpers ---

def _first_tab(service: Any, spreadsheet_id: str) -> str:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(title))",
    ).execute()
    sheets = meta.get("sheets", [])
    if not sheets:
        raise RuntimeError(f"spreadsheet {spreadsheet_id} has no tabs")
    return sheets[0]["properties"]["title"]


def _find_row_index(
    queue_id: str,
    queue_tab: str,
    row_id: str,
    service: Any,
) -> int | None:
    matches = sheets_helpers.find_rows_by_column_value(
        queue_id, queue_tab, CQ_ROW_ID, row_id, service=service,
    )
    if not matches:
        return None
    return matches[0][0]


def _find_row_by_id(rows: list[dict], row_id: str) -> dict | None:
    target = (row_id or "").strip()
    for r in rows:
        if str(r.get(CQ_ROW_ID, "")).strip() == target:
            return r
    return None


def _find_catalog_item(rows: list[dict], item_id: str) -> dict | None:
    target = (item_id or "").strip()
    if not target:
        return None
    for r in rows:
        if str(r.get(CAT_ITEM_ID, "")).strip() == target:
            return r
    return None


def _find_review_by_id(rows: list[dict], review_id: str) -> dict | None:
    target = (review_id or "").strip()
    if not target:
        return None
    for r in rows:
        if str(r.get(REV_REVIEW_ID, "")).strip() == target:
            return r
    return None


# --- Skill loading ---

def _skill_safe(name: str, config: Config, default: str = "") -> str:
    try:
        return skill_loader.load_skill(name, config=config)
    except Exception as exc:
        print(
            f"[critic] WARN: could not load skill {name!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return default


def load_critic_context(config: Config) -> dict[str, Any]:
    """Load run-once context: services, sheets, skills."""
    sheets_service = sheets_helpers.get_sheets_service()

    queue_id = config.get("drive.content_queue_sheet_id")
    catalog_id = config.get("catalog.spec_sheet_id")
    reviews_id = config.get("catalog.reviews_sheet_id", "")

    queue_tab = _first_tab(sheets_service, queue_id)
    catalog_tab = _first_tab(sheets_service, catalog_id)

    queue_rows = sheets_helpers.read_all_rows(
        queue_id, queue_tab, service=sheets_service,
    )
    catalog_rows = sheets_helpers.read_all_rows(
        catalog_id, catalog_tab, service=sheets_service,
    )

    reviews_tab = ""
    reviews_rows: list[dict] = []
    if reviews_id:
        try:
            reviews_tab = _first_tab(sheets_service, reviews_id)
            reviews_rows = sheets_helpers.read_all_rows(
                reviews_id, reviews_tab, service=sheets_service,
            )
        except Exception as exc:
            print(
                f"[critic] WARN: could not read Reviews Sheet: "
                f"{type(exc).__name__}: {exc} — Social Proof rows will "
                f"skip review-fidelity checks",
                file=sys.stderr,
            )

    skills = {
        "critic_checklist": _skill_safe("critic_checklist", config),
        "brand_voice": _skill_safe("brand_voice", config),
        "platform_style": _skill_safe("platform_style", config),
        "cta": _skill_safe("cta", config),
        "content_types": _skill_safe("content_types", config),
    }

    return {
        "sheets_service": sheets_service,
        "queue_id": queue_id,
        "queue_tab": queue_tab,
        "catalog_id": catalog_id,
        "catalog_tab": catalog_tab,
        "reviews_id": reviews_id,
        "reviews_tab": reviews_tab,
        "queue_rows": queue_rows,
        "catalog_rows": catalog_rows,
        "reviews_rows": reviews_rows,
        "skills": skills,
    }


# --- Per-row processing ---

def _verdict_to_status(verdict: str, current_status: str) -> str:
    """Map a verdict to the resulting status value."""
    if verdict == "pass":
        return "awaiting_approval"
    if verdict == "hard_fail":
        return "hard_fail"
    # soft_fail leaves status as-is (typically `drafted`); Make.com routes
    # back to the Drafter and increments revision_round.
    return current_status


def critique_single_row(
    row_id: str,
    context: dict[str, Any],
    config: Config,
    revision_round: int = 1,
    previous_critic_output: dict | None = None,
    dry_run: bool = False,
    llm_call: Any = None,
) -> dict:
    """Critique a single Content Queue row end-to-end."""
    row = _find_row_by_id(context["queue_rows"], row_id)
    if row is None:
        return {
            "status": "error",
            "row_id": row_id,
            "error": "row not found in Content Queue",
            "dry_run": dry_run,
        }

    actual_status = str(row.get(CQ_STATUS, "")).strip().lower()
    if actual_status != "drafted":
        return {
            "status": "skipped",
            "row_id": row_id,
            "reason": f"status is {actual_status!r}, expected 'drafted'",
            "dry_run": dry_run,
        }

    focus_id = str(row.get(CQ_FOCUS_EQUIPMENT, "")).strip()
    catalog_item: dict | None = None
    catalog_missing_warning: dict | None = None
    if focus_id:
        catalog_item = _find_catalog_item(
            context["catalog_rows"], focus_id,
        )
        if catalog_item is None:
            catalog_missing_warning = _make_warning(
                "G1",
                f"Catalog item {focus_id!r} not found — spec verification "
                f"checks (G1, G2, G3) skipped for this draft.",
            )

    review_id = str(row.get(CQ_REVIEW_ID, "")).strip()
    review_text = ""
    if review_id:
        review_data = _find_review_by_id(
            context.get("reviews_rows", []), review_id,
        )
        if review_data is not None:
            review_text = str(review_data.get(REV_REVIEW_TEXT, "") or "")

    try:
        critic_output = evaluate_draft(
            row=row,
            catalog_item=catalog_item,
            review_text=review_text,
            revision_round=revision_round,
            previous_critic_output=previous_critic_output,
            config=config,
            skills=context["skills"],
            llm_call=llm_call,
        )
    except Exception as exc:
        return {
            "status": "error",
            "row_id": row_id,
            "error": f"LLM call failed: {type(exc).__name__}: {exc}",
            "dry_run": dry_run,
        }

    if catalog_missing_warning is not None:
        # Append the catalog-missing warning if not already added.
        existing_ids = {
            str(w.get("check_id", "")).strip()
            for w in critic_output.get("warnings", []) or []
        }
        if "G1" not in existing_ids:
            critic_output.setdefault("warnings", []).append(
                catalog_missing_warning,
            )

    validation_issues = validate_critic_output(critic_output)
    if validation_issues:
        return {
            "status": "error",
            "row_id": row_id,
            "error": "Critic output failed validation",
            "validation_issues": validation_issues,
            "critic_output": critic_output,
            "dry_run": dry_run,
        }

    new_status = _verdict_to_status(critic_output["verdict"], actual_status)

    if not dry_run:
        row_number = _find_row_index(
            context["queue_id"],
            context["queue_tab"],
            row_id,
            context["sheets_service"],
        )
        if row_number is None:
            return {
                "status": "error",
                "row_id": row_id,
                "error": "could not locate row index for update",
                "dry_run": dry_run,
            }

        updates: dict[str, str] = {
            CQ_CRITIC_SCORE: critic_output["verdict"],
            CQ_CRITIC_NOTES: json.dumps(
                {
                    "revision_round": critic_output["revision_round"],
                    "failed_checks": critic_output["failed_checks"],
                    "warnings": critic_output["warnings"],
                    "passed_checks": critic_output["passed_checks"],
                    "notes": critic_output["notes"],
                },
                ensure_ascii=False,
            ),
        }
        if new_status != actual_status:
            updates[CQ_STATUS] = new_status

        try:
            headers = sheets_helpers._get_headers(
                context["queue_id"],
                context["queue_tab"],
                service=context["sheets_service"],
            )
            updates = {k: v for k, v in updates.items() if k in headers}
        except Exception as exc:
            print(
                f"[critic] WARN: could not fetch headers, attempting full "
                f"update: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

        try:
            sheets_helpers.update_cells(
                spreadsheet_id=context["queue_id"],
                tab_name=context["queue_tab"],
                row_number=row_number,
                col_updates=updates,
                service=context["sheets_service"],
            )
        except Exception as exc:
            return {
                "status": "error",
                "row_id": row_id,
                "error": (
                    f"failed to update Content Queue row: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "dry_run": dry_run,
            }

    return {
        "status": "success",
        "row_id": row_id,
        "revision_round": revision_round,
        "verdict": critic_output["verdict"],
        "new_status": new_status,
        "failed_check_count": len(critic_output["failed_checks"]),
        "warning_count": len(critic_output["warnings"]),
        "passed_check_count": len(critic_output["passed_checks"]),
        "critic_output": critic_output,
        "dry_run": dry_run,
    }


def run_single(
    row_id: str,
    revision_round: int = 1,
    previous_critic_output: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Public entry point for critiquing a single row."""
    config = load_config()
    context = load_critic_context(config)
    return critique_single_row(
        row_id=row_id,
        context=context,
        config=config,
        revision_round=revision_round,
        previous_critic_output=previous_critic_output,
        dry_run=dry_run,
    )


# --- CLI ---

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Critic agent")
    parser.add_argument(
        "--row-id",
        required=True,
        help="Content Queue row_id to evaluate.",
    )
    parser.add_argument(
        "--revision-round",
        type=int,
        default=1,
        help="Revision round (1 on first pass, 2/3 on revisions).",
    )
    parser.add_argument(
        "--previous-output",
        default=None,
        help=(
            "Path to a JSON file containing the previous Critic output. "
            "Required when --revision-round > 1 to enable comparison."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate but do not write to the Content Queue.",
    )
    args = parser.parse_args(argv)

    previous_output: dict | None = None
    if args.previous_output:
        with open(args.previous_output, "r", encoding="utf-8") as f:
            previous_output = json.load(f)

    result = run_single(
        row_id=args.row_id,
        revision_round=args.revision_round,
        previous_critic_output=previous_output,
        dry_run=args.dry_run,
    )

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
