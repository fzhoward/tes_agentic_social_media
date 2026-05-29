"""Drafter agent — LLM caption writer with media-asset pipeline.

Takes a single `planned` Content Queue row and produces the finished draft:
caption (hook + body + CTA), media asset (image or video), and first comment
(when applicable). Writes results back to the row and flips status to
`drafted`.

The Drafter executes — it does not make strategic decisions. The Strategist
already chose platform, objective, content type, media format, CTA type,
focus equipment, and angle. The Drafter's job is to write the caption and
produce the media at the quality bar set by the brand voice and the skill
library.

Architecture:
    Deterministic shell — data loading, image selection, source download,
    image/video generation, Drive upload, sheet writes, validation.
    LLM core — caption writing (hook + body + CTA phrasing) and image
    overlay hook text.

Usage:
    python -m agents.drafter --row-id STR-20260527-FB-01 [--dry-run]
    python -m agents.drafter --all-planned [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tools import (
    creatomate_helpers,
    drive_helpers,
    image_generator,
    image_selector,
    sheets_helpers,
    skill_loader,
)
from tools.config_loader import Config, load_config


# --- Constants ---

ET = ZoneInfo("America/New_York")

ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
ANTHROPIC_MAX_TOKENS: int = 4096

TMP_DIR: str = ".tmp"

# Platform caption character limits (full caption, not feed-truncation).
PLATFORM_CHAR_LIMITS: dict[str, int] = {
    "facebook": 63206,
    "instagram": 2200,
    "gbp": 1500,
}

# Feed truncation points (the "above the fold" length).
PLATFORM_FEED_TRUNCATION: dict[str, int] = {
    "facebook": 477,
    "instagram": 125,
    "gbp": 175,
}

# Human-readable platform names used when substituting {{PLATFORM}} into
# image prompts. Falls back to .title() for any unmapped key.
PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "gbp": "Google Business Profile",
}

# Per-platform target caption length ranges (full caption: hook + body + CTA).
# Enforced via prompt-level instruction in build_drafter_messages — the Critic
# enforces it at QA time.
CAPTION_TARGET_RANGE: dict[str, tuple[int, int]] = {
    "facebook": (500, 1500),
    "instagram": (800, 1500),
    "gbp": (150, 200),
}

# Strict output rules per CTA type. Enforced via prompt-level instruction in
# build_drafter_messages so the LLM doesn't drift into hybrid CTAs (e.g.,
# adding a phone number to a "save" CTA because it saw one in the brand voice).
CTA_OUTPUT_RULES: dict[str, str] = {
    "call": "must include the phone number; must not include other actions",
    "dm": "must direct to DM/message; must not include phone number or URL",
    "save": "must direct reader to save the post; must NOT include phone number, URL, or any other action",
    "comment": "must ask a question or prompt engagement; must not include phone number or URL",
    "click": "must direct to first comment for the link; must not include phone number",
    "visit": "must direct to website; must not include phone number",
    "book": "must direct to booking URL; must not include phone number",
    "directions": "must direct to Google Maps/listing; must not include phone number",
    "none": "no CTA at all; caption ends with the last content line (cta_text must be empty string)",
}

# Phrases the brand voice forbids. Lowercased substring match.
BANNED_PHRASES: tuple[str, ...] = (
    "game changer",
    "game-changer",
    "one-stop shop",
    "one stop shop",
    "best in class",
    "best-in-class",
    "world class",
    "world-class",
    "synergy",
    "leverage",
    "unleash",
    "unlock the power",
    "next level",
    "next-level",
    "revolutionary",
    "cutting edge",
    "cutting-edge",
    "state of the art",
    "state-of-the-art",
    "elevate your",
    "supercharge",
    "ninja",
    "rockstar",
    "literally",
    "absolutely crushing",
)

# Banned punctuation tokens — exclamation points and em dashes per brand voice.
BANNED_CHARS: tuple[str, ...] = ("!", "—")

# Media formats whose Creatomate template has a built-in text track that
# needs Hook-Text populated regardless of the image-overlay flag.
VIDEO_MEDIA_FORMATS: set[str] = {"creatomate_video", "creatomate_review_video"}

# Media formats rendered from a Reviews-Sheet row rather than an equipment photo.
REVIEW_MEDIA_FORMATS: set[str] = {"creatomate_review_image", "creatomate_review_video"}

# Content type that pairs with review media formats.
SOCIAL_PROOF_CONTENT_TYPE: str = "Social Proof / Customer Story"


# --- Reviews Sheet column names ---

REV_REVIEW_ID = "review_id"
REV_REVIEWER_FIRST_NAME = "reviewer_first_name"
REV_STAR_RATING = "star_rating"
REV_REVIEW_TEXT = "review_text"
REV_EXCERPT_LONG = "excerpt_long"
REV_LAST_USED_DATE = "last_used_date"
REV_TIMES_USED = "times_used"

# Glyph passed to the review-image template's Star-Rating element. The Reviews
# Sheet filter only marks 5-star reviews usable, so this is a constant.
REVIEW_STAR_RATING_GLYPH: str = "★★★★★"


# --- Content Queue column names (per PLACEHOLDER_REGISTRY.md) ---

# Strategist-written (Drafter reads).
CQ_ROW_ID = "row_id"
CQ_STATUS = "status"
CQ_PLATFORM = "platform"
CQ_SCHEDULED = "scheduled_datetime"
CQ_OBJECTIVE = "objective"
CQ_CONTENT_TYPE = "content_type"
CQ_FOCUS_EQUIPMENT = "focus_equipment_id"
CQ_ANGLE = "angle"
CQ_CTA_TYPE = "cta_type"
CQ_MEDIA_FORMAT = "media_format"
CQ_TEXT_OVERLAY = "text_overlay"
CQ_SOURCE_IMAGE = "source_image_id"
CQ_DRAFT_NOTES = "draft_notes"
CQ_REVIEW_ID = "review_id"

# Drafter-written.
CQ_CAPTION = "caption"
CQ_CREATIVE_HOOK_TEXT = "creative_hook_text"
CQ_FIRST_COMMENT = "first_comment"
CQ_CTA_TEXT = "cta_text"
CQ_HOOK_TEXT = "hook_text"
CQ_IMAGE_OVERLAY_TEXT = "image_overlay_text"
CQ_MEDIA_URL = "media_url"
CQ_MEDIA_FORMAT_USED = "media_format_used"
CQ_DRAFT_RATIONALE = "draft_rationale"
CQ_REVISION_ROUND = "revision_round"


# Media formats that render a Creatomate equipment template whose Hook-Text
# field should be populated from creative_hook_text.
CREATOMATE_EQUIPMENT_FORMATS: set[str] = {
    "creatomate_text_overlay",
    "creatomate_video",
}

# Max words allowed in creative_hook_text — hard limit, enforced in
# post-processing per Session 15 spec. Bold typography on visual creatives
# falls apart past this length.
CREATIVE_HOOK_MAX_WORDS: int = 7

# Sentence-length enforcement — must match critic.py values exactly so both
# agents measure the same way.
SENTENCE_MAX_WORDS: int = 18
SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")


# --- Catalog column names ---

CAT_ITEM_ID = "item_id"
CAT_ITEM_NAME = "item_name"
CAT_CATEGORY = "category"
CAT_MODEL = "model"
CAT_DESCRIPTION = "description"
CAT_COMMON_JOBS = "common_jobs"
CAT_BEST_FOR = "best_for"
CAT_WEIGHT = "weight"
CAT_DIG_DEPTH = "dig_depth"
CAT_REACH = "reach"
CAT_CAPACITY = "capacity"
CAT_HORSEPOWER = "horsepower"
CAT_TAIL_SWING = "tail_swing"

# Spec fields included in the prompt when non-empty.
SPEC_FIELDS: tuple[str, ...] = (
    CAT_WEIGHT,
    CAT_DIG_DEPTH,
    CAT_REACH,
    CAT_CAPACITY,
    CAT_HORSEPOWER,
    CAT_TAIL_SWING,
    CAT_COMMON_JOBS,
    CAT_BEST_FOR,
)


# --- Few-shot selection ---

MIN_FEW_SHOTS: int = 5
MAX_FEW_SHOTS: int = 8


# --- Datetime helpers ---

def _now_et() -> datetime:
    return datetime.now(ET)


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt


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
    """Return the 1-indexed sheet row number for `row_id`, or None."""
    matches = sheets_helpers.find_rows_by_column_value(
        queue_id, queue_tab, CQ_ROW_ID, row_id, service=service,
    )
    if not matches:
        return None
    return matches[0][0]


# --- Validation primitives ---

def count_words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for B6 enforcement.

    Must match critic.py's _split_sentences exactly — same regex, same
    stripping, same filtering. Both agents must agree on what constitutes
    a sentence and how many words it contains.
    """
    if not text:
        return []
    raw = SENTENCE_SPLIT_RE.split(text)
    return [s.strip() for s in raw if s and s.strip()]


def find_sentence_length_violations(parsed: dict) -> list[dict]:
    """Find sentences exceeding SENTENCE_MAX_WORDS in caption_hook and caption_body.

    Does NOT check cta_text — CTAs follow standardized brand voice patterns
    that may legitimately exceed 18 words.

    Returns a list of dicts with keys: field, sentence_index, word_count, text.
    Empty list means no violations.
    """
    violations: list[dict] = []
    for field in ("caption_hook", "caption_body"):
        text = str(parsed.get(field, "") or "").strip()
        if not text:
            continue
        for idx, sentence in enumerate(_split_sentences(text), start=1):
            n = count_words(sentence)
            if n > SENTENCE_MAX_WORDS:
                violations.append({
                    "field": field,
                    "sentence_index": idx,
                    "word_count": n,
                    "text": sentence,
                })
    return violations


def rewrite_long_sentences(
    violations: list[dict],
    parsed: dict,
    max_attempts: int = 2,
) -> tuple[dict, list[str]]:
    """Rewrite sentences that exceed SENTENCE_MAX_WORDS.

    Sends ONLY the violating sentences to the LLM with a focused rewrite
    instruction. Replaces the offending sentences in `parsed` in-place and
    returns the updated dict plus a list of warning strings.

    Up to `max_attempts` rewrite rounds. If violations remain after all
    attempts, returns the best result and lets the Critic catch remaining
    issues.
    """
    warnings: list[str] = []
    current = dict(parsed)
    remaining = list(violations)

    rewrite_system = (
        "You are rewriting sentences to be "
        f"{SENTENCE_MAX_WORDS} words or fewer while preserving meaning "
        "and the brand voice. The brand voice is short, punchy, "
        "declarative — match that tone. Split long sentences into two "
        "shorter ones rather than trimming meaning. No emoji, no "
        "exclamation points, no em dashes, no markdown. "
        "Return valid JSON only."
    )

    for attempt in range(1, max_attempts + 1):
        if not remaining:
            break

        sentences_payload = [
            {
                "field": v["field"],
                "original": v["text"],
                "word_count": v["word_count"],
            }
            for v in remaining
        ]
        rewrite_user = (
            f"Rewrite each of the following sentences to be "
            f"{SENTENCE_MAX_WORDS} words or fewer. Preserve the meaning "
            f"and the punchy, declarative brand voice. If a sentence "
            f"needs more than {SENTENCE_MAX_WORDS} words to make its "
            f"point, split it into two shorter sentences and return "
            f"the result as a single string (the two sentences joined "
            f"by a space).\n\n"
            f"Target 8-12 words per sentence.\n\n"
            f"Sentences to rewrite:\n"
            f"{json.dumps(sentences_payload, indent=2)}\n\n"
            f"Return JSON with this exact shape:\n"
            f'{{"rewrites": [{{"field": "...", "original": "...", '
            f'"rewritten": "..."}}]}}'
        )

        try:
            raw = call_anthropic(
                rewrite_system, rewrite_user, max_tokens=1024,
            )
            rewrite_parsed = parse_llm_json(raw)
        except Exception as exc:
            warnings.append(
                f"sentence-length rewrite attempt {attempt} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            break

        rewrites = rewrite_parsed.get("rewrites", [])
        if not isinstance(rewrites, list):
            warnings.append(
                f"sentence-length rewrite attempt {attempt} returned "
                f"unexpected shape (no rewrites list)"
            )
            break

        for rw in rewrites:
            if not isinstance(rw, dict):
                continue
            field = str(rw.get("field", "")).strip()
            original = str(rw.get("original", "")).strip()
            rewritten = str(rw.get("rewritten", "")).strip()
            if field not in ("caption_hook", "caption_body"):
                continue
            if not original or not rewritten:
                continue
            field_text = str(current.get(field, "") or "")
            if original in field_text:
                current[field] = field_text.replace(original, rewritten, 1)
            else:
                warnings.append(
                    f"sentence-length rewrite: could not locate original "
                    f"sentence in {field} (rewrite skipped): {original!r}"
                )

        remaining = find_sentence_length_violations(current)
        if remaining and attempt < max_attempts:
            warnings.append(
                f"sentence-length rewrite attempt {attempt} left "
                f"{len(remaining)} violation(s); retrying"
            )

    if remaining:
        warnings.append(
            f"sentence-length enforcement gave up after {max_attempts} "
            f"attempt(s) with {len(remaining)} violation(s) remaining — "
            f"Critic will catch any that slipped through"
        )

    return current, warnings


def contains_banned_language(text: str) -> list[str]:
    """Return the banned phrases found in `text` (case-insensitive)."""
    if not text:
        return []
    lowered = text.lower()
    return [p for p in BANNED_PHRASES if p in lowered]


def contains_banned_chars(text: str) -> list[str]:
    if not text:
        return []
    return [c for c in BANNED_CHARS if c in text]


def clean_overlay_text(text: str) -> str:
    """Strip trailing sentence-fragment punctuation from overlay text.

    Overlay text is a 3-7 word hook fragment — trailing periods, commas,
    semicolons, and colons read as incomplete sentences when rendered into
    Creatomate templates. Internal punctuation (intentional commas like
    "Before you rent, read this") is preserved. Trailing '?' and '!' are
    left alone — '!' is already banned elsewhere, '?' is a valid hook ending.
    """
    if not text:
        return text
    return text.rstrip().rstrip(".,;:").rstrip()


def validate_overlay_hook(
    hook: str,
    text_overlay_flag: str,
    media_format: str = "",
) -> tuple[bool, str]:
    """Return (ok, reason) for the image_overlay_hook field.

    Required: 3-7 words when text_overlay_flag == 'TRUE'.
    Required: empty when text_overlay_flag == 'FALSE' AND media_format is
    not a video format. Video templates have a built-in Hook-Text track, so
    a populated hook is allowed even when the image overlay is off — the
    Drafter will pass it through to Creatomate.
    """
    flag = str(text_overlay_flag).strip().upper()
    h = (hook or "").strip()
    fmt = str(media_format).strip().lower()
    if flag == "TRUE":
        if not h:
            return False, "image_overlay_hook is empty but text_overlay is TRUE"
        n = count_words(h)
        if n < 3:
            return False, f"image_overlay_hook has {n} words (must be 3-7)"
        if n > 7:
            return False, f"image_overlay_hook has {n} words (must be 3-7)"
        return True, ""
    if flag == "FALSE":
        if h and fmt not in VIDEO_MEDIA_FORMATS:
            return False, "image_overlay_hook is set but text_overlay is FALSE"
        return True, ""
    return False, f"text_overlay flag must be TRUE or FALSE, got {flag!r}"


def validate_review_excerpt(
    excerpt: str,
    review_text: str,
    max_chars: int | None,
) -> tuple[bool, str]:
    """Return (ok, reason) for the LLM-selected review excerpt.

    Rules:
      - Non-empty.
      - Verbatim substring of `review_text`. Any rewriting, paraphrasing, or
        combining of disjoint phrases fails this check.
      - Length ≤ ``max_chars`` when ``max_chars`` is provided.

    Caller decides what to do on failure (the Drafter falls back to the
    Reviews Sheet's `excerpt_long` and logs a warning).
    """
    e = (excerpt or "").strip()
    if not e:
        return False, "review_excerpt is empty"
    if not (review_text or ""):
        return False, "review_text is empty — cannot validate substring"
    if e not in review_text:
        return False, (
            "review_excerpt is not a verbatim substring of review_text"
        )
    if max_chars is not None and len(e) > max_chars:
        return False, (
            f"review_excerpt length {len(e)} exceeds template "
            f"max_review_text_chars={max_chars}"
        )
    return True, ""


def validate_creative_hook_text(
    creative_hook: str,
    caption_hook: str,
) -> tuple[bool, str]:
    """Return (ok, reason) for the creative_hook_text field.

    Rules:
      - Non-empty.
      - At most 7 words.
      - Distinct from caption_hook: neither string is a case-insensitive
        substring of the other. Same-text counts as overlap.
    """
    h = (creative_hook or "").strip()
    if not h:
        return False, "creative_hook_text is empty"
    n = count_words(h)
    if n > CREATIVE_HOOK_MAX_WORDS:
        return False, (
            f"creative_hook_text has {n} words "
            f"(must be ≤{CREATIVE_HOOK_MAX_WORDS})"
        )
    ch = (caption_hook or "").strip().lower()
    lh = h.lower()
    if ch and (lh in ch or ch in lh):
        return False, (
            "creative_hook_text overlaps with caption_hook — it must be a "
            "distinct hook, not a substring of caption_hook (or vice versa)"
        )
    return True, ""


def validate_caption_length(caption: str, platform: str) -> tuple[bool, str]:
    """Return (ok, reason) for caption length vs platform limit."""
    platform_key = (platform or "").strip().lower()
    limit = PLATFORM_CHAR_LIMITS.get(platform_key)
    if limit is None:
        return False, f"unknown platform for char limit: {platform!r}"
    n = len(caption or "")
    if n > limit:
        return False, f"caption length {n} exceeds {platform_key} limit of {limit}"
    return True, ""


def assemble_caption(hook: str, body: str, cta: str) -> str:
    """Assemble the final caption: hook + body + CTA, joined by \\n\\n.

    Empty CTA is omitted entirely (no trailing separator).
    """
    parts: list[str] = []
    h = (hook or "").strip()
    b = (body or "").strip()
    c = (cta or "").strip()
    if h:
        parts.append(h)
    if b:
        parts.append(b)
    if c:
        parts.append(c)
    return "\n\n".join(parts)


def validate_llm_output(
    parsed: dict,
    text_overlay_flag: str,
    platform: str,
    objective: str,
    cta_type: str,
    media_format: str = "",
) -> tuple[list[str], str]:
    """Return (issues, assembled_caption).

    Empty issues == validation passed.
    """
    issues: list[str] = []

    hook = str(parsed.get("caption_hook", "") or "").strip()
    body = str(parsed.get("caption_body", "") or "").strip()
    cta_text = str(parsed.get("cta_text", "") or "").strip()
    overlay = str(parsed.get("image_overlay_hook", "") or "").strip()
    creative_hook = str(parsed.get("creative_hook_text", "") or "").strip()

    if not hook:
        issues.append("caption_hook is empty")
    if not body:
        issues.append("caption_body is empty")

    ok_creative, reason_creative = validate_creative_hook_text(creative_hook, hook)
    if not ok_creative:
        issues.append(reason_creative)

    cta_required = (
        str(objective).strip().lower() == "lead_generation"
        and str(cta_type).strip().lower() != "none"
    )
    if cta_required and not cta_text:
        issues.append(
            f"cta_text is empty but objective={objective!r} and "
            f"cta_type={cta_type!r} require one"
        )

    ok, reason = validate_overlay_hook(overlay, text_overlay_flag, media_format)
    if not ok:
        issues.append(reason)

    full_caption = assemble_caption(hook, body, cta_text)

    for field, value in (
        ("caption_hook", hook),
        ("caption_body", body),
        ("cta_text", cta_text),
        ("image_overlay_hook", overlay),
        ("creative_hook_text", creative_hook),
    ):
        bad = contains_banned_language(value)
        if bad:
            issues.append(f"{field} contains banned phrases: {bad}")
        bad_chars = contains_banned_chars(value)
        if bad_chars:
            issues.append(f"{field} contains banned chars: {bad_chars}")

    ok_len, reason_len = validate_caption_length(full_caption, platform)
    if not ok_len:
        issues.append(reason_len)

    return issues, full_caption


# --- LLM prompt assembly ---

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_llm_json(raw_text: str) -> dict:
    """Extract a JSON object from the LLM response."""
    text = raw_text.strip()
    text = _FENCE_RE.sub("", text).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                f"could not locate JSON object in LLM output: {raw_text!r}"
            )
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"LLM output did not parse to an object (got {type(parsed).__name__})"
        )
    return parsed


def select_few_shots(
    library_text: str,
    content_type: str,
    objective: str,
    min_n: int = MIN_FEW_SHOTS,
    max_n: int = MAX_FEW_SHOTS,
) -> str:
    """Select 5-8 few-shot examples from the library text.

    Heuristic: split the library on top-level markdown headers (## or ---),
    score each chunk on content-type match then objective match, return the
    top N joined back together. Falls back to returning the whole library
    truncated if no chunks are found (still useful context).
    """
    if not library_text:
        return ""

    chunks: list[str] = []
    for raw in re.split(r"\n(?=##\s|---\s*\n)", library_text):
        chunk = raw.strip()
        if chunk and len(chunk) > 50:
            chunks.append(chunk)

    if not chunks:
        return library_text[:6000]

    ct_lower = (content_type or "").lower()
    obj_lower = (objective or "").lower()

    def _score(chunk: str) -> tuple[int, int, int]:
        lowered = chunk.lower()
        ct_hit = 1 if ct_lower and ct_lower in lowered else 0
        obj_hit = 1 if obj_lower and obj_lower in lowered else 0
        # Tiebreaker: shorter chunks first (denser, fewer tokens).
        return (ct_hit, obj_hit, -len(chunk))

    scored = sorted(chunks, key=_score, reverse=True)
    keep = scored[:max_n]
    if len(keep) < min_n:
        keep = scored[:min_n]
    return "\n\n---\n\n".join(keep)


def extract_platform_section(platform_style_text: str, platform: str) -> str:
    """Pull out the section of platform_style for the target platform.

    Best-effort: scans for a heading containing the platform name and returns
    everything up to the next top-level heading. Returns the full text
    truncated if no match — still useful steering for the LLM.
    """
    if not platform_style_text:
        return ""
    plat = (platform or "").strip().lower()
    aliases = {
        "facebook": ("facebook", "fb"),
        "instagram": ("instagram", "ig"),
        "gbp": ("gbp", "google business", "google business profile"),
    }
    needles = aliases.get(plat, (plat,))

    lines = platform_style_text.splitlines()
    in_section = False
    section: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_heading = stripped.startswith("#") or stripped.startswith("##")
        if is_heading:
            if in_section:
                break
            lower_line = stripped.lower()
            if any(n in lower_line for n in needles):
                in_section = True
                section.append(line)
                continue
        elif in_section:
            section.append(line)

    if not section:
        return platform_style_text[:2000]
    out = "\n".join(section).strip()
    return out if out else platform_style_text[:2000]


def extract_cta_section(cta_text: str, cta_type: str) -> str:
    """Pull the section of the CTA skill for the assigned cta_type."""
    if not cta_text or not cta_type:
        return cta_text[:1500] if cta_text else ""
    needle = cta_type.strip().lower()
    lines = cta_text.splitlines()
    in_section = False
    section: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_heading = stripped.startswith("#") or stripped.startswith("##")
        if is_heading:
            if in_section:
                break
            if needle in stripped.lower():
                in_section = True
                section.append(line)
                continue
        elif in_section:
            section.append(line)
    if not section:
        return cta_text[:1500]
    out = "\n".join(section).strip()
    return out if out else cta_text[:1500]


def extract_hook_section(
    hook_text: str,
    objective: str,
) -> str:
    """Pull hook-creation guidance relevant to social captions + objective."""
    if not hook_text:
        return ""
    intent_word = (
        "lead generation" if objective == "lead_generation" else "brand awareness"
    )
    lines = hook_text.splitlines()
    kept: list[str] = []
    relevant_keywords = (
        "social",
        "caption",
        "image overlay",
        "overlay",
        "scoring",
        "rubric",
        intent_word,
    )
    skip_section = False
    for line in lines:
        stripped = line.strip().lower()
        is_heading = stripped.startswith("#")
        if is_heading:
            skip_section = not any(kw in stripped for kw in relevant_keywords)
        if not skip_section:
            kept.append(line)
    if not kept:
        return hook_text[:3000]
    out = "\n".join(kept).strip()
    if len(out) > 4000:
        out = out[:4000] + "\n[... truncated ...]"
    return out


def extract_content_type_section(
    strategy_text: str,
    content_type: str,
) -> str:
    """Pull the definition for the assigned content_type from Strategy Guidance."""
    if not strategy_text or not content_type:
        return ""
    needle = content_type.strip().lower()
    lines = strategy_text.splitlines()
    in_section = False
    section: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_heading = stripped.startswith("#")
        if is_heading:
            if in_section:
                break
            if needle in stripped.lower():
                in_section = True
                section.append(line)
                continue
        elif in_section:
            section.append(line)
    if not section:
        return ""
    out = "\n".join(section).strip()
    if len(out) > 1500:
        out = out[:1500] + "\n[... truncated ...]"
    return out


def extract_image_prompt_section(image_prompt_social_text: str, text_overlay: bool) -> str:
    """Extract Section A (with overlay) or Section B (without) from image_prompt_social."""
    if not image_prompt_social_text:
        return ""
    target = "section a" if text_overlay else "section b"
    other = "section b" if text_overlay else "section a"
    lines = image_prompt_social_text.splitlines()
    in_section = False
    section: list[str] = []
    for line in lines:
        stripped = line.strip().lower()
        if not in_section:
            if target in stripped and stripped.startswith("#"):
                in_section = True
                section.append(line)
        else:
            # Stop at the start of the other section.
            if other in stripped and stripped.startswith("#"):
                break
            section.append(line)
    if section:
        return "\n".join(section).strip()
    # Fall back to the full text — better to over-include than under-include.
    return image_prompt_social_text.strip()


def build_equipment_block(catalog_item: dict | None) -> str:
    """Compact, single-block equipment context for the LLM."""
    if not catalog_item:
        return "(no focus equipment for this post)"
    parts = [
        f"item_name: {catalog_item.get(CAT_ITEM_NAME, '')}",
        f"category: {catalog_item.get(CAT_CATEGORY, '')}",
        f"model: {catalog_item.get(CAT_MODEL, '')}",
        f"description: {catalog_item.get(CAT_DESCRIPTION, '')}",
    ]
    for field in SPEC_FIELDS:
        v = str(catalog_item.get(field, "")).strip()
        if v:
            parts.append(f"{field}: {v}")
    return "\n".join(parts)


def _format_candidate_line(candidate: dict) -> str:
    """Format a single image-candidate descriptor line for the LLM prompt."""
    pc = candidate.get("photo_context", "").strip() or "(none)"
    jt = candidate.get("job_type", "").strip() or "(none)"
    nt = candidate.get("notes", "").strip() or "(none)"
    return f"Photo Context: {pc} | Job Type: {jt} | Notes: {nt}"


def build_image_candidates_block(image_candidates: list[dict] | None) -> str:
    """Build the image-context block injected into the user message.

    - 0 candidates / None: returns empty string (no block).
    - 1 candidate: returns a context line so the LLM knows what the photo shows.
    - 2+ candidates: returns the selection block, asking the LLM to pick.
    """
    if not image_candidates:
        return ""

    if len(image_candidates) == 1:
        line = _format_candidate_line(image_candidates[0])
        return (
            "# Source Image Context\n\n"
            "The source image for this post shows:\n"
            f"{line}\n\n"
            "Write your caption to complement this visual.\n"
        )

    lines = []
    for c in image_candidates:
        rank = c.get("rank", "?")
        lines.append(
            f"Candidate {rank} (rank {rank}): {_format_candidate_line(c)}"
        )
    candidates_listing = "\n".join(lines)
    return (
        "# Available Source Images\n\n"
        "The following images are available for this post. Select the best-fit "
        "image for the assigned angle and content type. Return your choice as "
        "`selected_image_id` in the JSON output.\n\n"
        f"{candidates_listing}\n\n"
        "Pick the image whose context best matches the post's angle. If no "
        "image is a strong fit, pick rank 1 (the default).\n"
    )


def build_revision_block(
    previous_output: dict | None,
    revision_round: int,
) -> str:
    """Return the revision block appended to the round-2+ user message.

    Empty string when there is no previous Critic output (round 1).
    The shape mirrors the Critic's own round-2 phrasing for consistency.
    """
    if not previous_output:
        return ""
    prev = json.dumps(previous_output, indent=2, ensure_ascii=False)
    return (
        f"\n\n# Previous Critic Output (revision_round={revision_round})\n\n"
        "Your previous draft was reviewed by an independent QA critic and "
        "produced the verdict below. You MUST address every `failed_checks` "
        "entry in your new draft. Each entry includes the specific defect "
        "(`description`) and a directive (`fix_instruction`) your revision "
        "must implement. Do NOT regress any check that previously passed.\n\n"
        f"```json\n{prev}\n```\n"
    )


def build_drafter_messages(
    row: dict,
    catalog_item: dict | None,
    config: Config,
    brand_voice: str,
    hook_skill: str,
    cta_skill: str,
    platform_style: str,
    few_shot_library: str,
    strategy_guidance: str,
    retry_nudge: bool = False,
    review_data: dict | None = None,
    review_excerpt_skill: str = "",
    review_excerpt_max_chars: int | None = None,
    image_candidates: list[dict] | None = None,
    revision_block: str = "",
) -> tuple[str, str]:
    """Build (system_message, user_message) for the Anthropic call."""
    platform = str(row.get(CQ_PLATFORM, "")).strip().lower()
    objective = str(row.get(CQ_OBJECTIVE, "")).strip().lower()
    content_type = str(row.get(CQ_CONTENT_TYPE, "")).strip()
    cta_type = str(row.get(CQ_CTA_TYPE, "")).strip().lower()
    text_overlay = str(row.get(CQ_TEXT_OVERLAY, "")).strip().upper()
    media_format = str(row.get(CQ_MEDIA_FORMAT, "")).strip()
    angle = str(row.get(CQ_ANGLE, "")).strip()
    draft_notes = str(row.get(CQ_DRAFT_NOTES, "")).strip()
    focus_id = str(row.get(CQ_FOCUS_EQUIPMENT, "")).strip()

    phone = config.get("contact.phone", "")
    website = config.get("contact.website", "")
    booking_url = config.get("contact.booking_url", "")
    maps_url = config.get("contact.google_maps_url", "")

    char_limit = PLATFORM_CHAR_LIMITS.get(platform, 2200)
    feed_trunc = PLATFORM_FEED_TRUNCATION.get(platform, 125)

    platform_display = {"gbp": "GBP"}.get(platform, platform.title() or platform)
    target_range = CAPTION_TARGET_RANGE.get(platform)
    if target_range is not None:
        target_min, target_max = target_range
        caption_target_block = (
            f"Caption length target: your total caption (hook + body + CTA "
            f"combined) for this {platform_display} post must be "
            f"{target_min}–{target_max} characters. This is a hard "
            f"target, not a guideline. Drafts outside this range will be "
            f"rejected."
        )
        caption_target_reinforcement = (
            f"- Caption length: your total caption for this "
            f"{platform_display} post must be {target_min}–{target_max} "
            f"characters. Treat this as a hard constraint."
        )
    else:
        caption_target_block = ""
        caption_target_reinforcement = ""

    platform_section = extract_platform_section(platform_style, platform)
    cta_section = extract_cta_section(cta_skill, cta_type)

    cta_rules_list = "\n".join(
        f"- {ct} → {rule}" for ct, rule in CTA_OUTPUT_RULES.items()
    )
    cta_rule_for_this_post = CTA_OUTPUT_RULES.get(
        cta_type,
        "(unknown CTA type — follow the CTA Rules section above exactly)",
    )
    cta_constraint_block = (
        f"The assigned CTA type is \"{cta_type}\". Your CTA must follow the "
        f"rules for this type exactly. Do not combine CTA types or add "
        f"actions not specified for this type.\n\n"
        f"Rules per CTA type:\n"
        f"{cta_rules_list}\n\n"
        f"For this post (cta_type={cta_type}): {cta_rule_for_this_post}"
    )
    cta_constraint_reinforcement = (
        f"- CTA type is {cta_type} — follow the output rules for this type "
        f"strictly. Do not add phone numbers, URLs, or other actions unless "
        f"the CTA type explicitly requires them."
    )
    hook_section = extract_hook_section(hook_skill, objective)
    content_type_section = extract_content_type_section(
        strategy_guidance, content_type,
    )
    few_shots = select_few_shots(few_shot_library, content_type, objective)
    equipment_block = build_equipment_block(catalog_item)

    is_link_post = cta_type == "click"
    overlay_needed = text_overlay == "TRUE"

    image_candidates_block = build_image_candidates_block(image_candidates)
    has_multi_candidates = bool(image_candidates) and len(image_candidates) > 1

    if review_data:
        review_block = (
            "# Review Featured in This Post\n\n"
            "This is a Social Proof post featuring a real customer review. The "
            "caption should reference this review naturally — you may quote it "
            "directly or frame it with your own context. Use the reviewer's "
            "first name.\n\n"
            "Anti-paraphrase rule: the caption must not attribute statements, "
            "sentiments, or conclusions to the reviewer that do not appear in "
            "the original review text. For example, if the review says 'Great "
            "service and equipment,' do not write 'William says he'll never "
            "rent anywhere else' — that sentiment is not in the review. You "
            "may frame the review ('William's five-star review speaks for "
            "itself') but you must not put words in the reviewer's mouth.\n\n"
            "The review_id is a system reference, not for the caption.\n\n"
            f"Reviewer first name: {review_data.get(REV_REVIEWER_FIRST_NAME, '')}\n"
            f"Star rating: {review_data.get(REV_STAR_RATING, '')}\n"
            f"Full review text:\n\"\"\"\n"
            f"{review_data.get(REV_REVIEW_TEXT, '')}\n"
            f"\"\"\"\n"
        )

        budget_line = (
            f"Character budget for the selected excerpt on this template: "
            f"{review_excerpt_max_chars} chars. The excerpt MUST fit within "
            f"this budget; if the best phrase would exceed it, pick the "
            f"next-best phrase that fits."
            if isinstance(review_excerpt_max_chars, int) and review_excerpt_max_chars > 0
            else (
                "Character budget for the selected excerpt is not configured "
                "for this template — keep the excerpt short and self-contained."
            )
        )
        excerpt_skill_section = (review_excerpt_skill or "").strip() or (
            "(no review_excerpt_selection skill text loaded — apply the general "
            "principle: pick the verbatim substring of the review that best "
            "justifies the 5-star rating)"
        )
        review_block += (
            "\n# Review Excerpt Selection\n\n"
            "A Creatomate template will display a short excerpt of this review "
            "as the visual centerpiece. You must select the portion of the "
            "review that best communicates why the reviewer gave 5 stars and "
            "output it as `review_excerpt` in the JSON. The excerpt must be a "
            "VERBATIM substring of the Full review text shown above — do not "
            "rearrange, combine, or paraphrase. You may trim from either end.\n\n"
            f"{budget_line}\n\n"
            f"{excerpt_skill_section}\n"
        )
    else:
        review_block = ""

    system_message = (
        "You are a social media copywriter for a local equipment rental "
        "company. You write short, punchy, platform-native captions that "
        "follow strict brand voice rules. You also generate hooks for both "
        "the caption opening and the image text overlay.\n\n"
        "Anti-fabrication rules — these are non-negotiable:\n"
        "1. Never invent customer stories, testimonials, anecdotes, or "
        "quotes — even paraphrased or anonymized ones. If no real customer "
        "story is provided in the inputs, do not create one. Framings like "
        "\"last month a customer used this for...\", \"we had a guy who...\", "
        "or \"one of our regulars said...\" are off-limits unless the story "
        "is explicitly handed to you in the assignment or equipment details.\n"
        "2. Never include specific dollar amounts, price ranges, hourly "
        "rates, or cost estimates (for example \"$150-300 per hour\", "
        "\"around $2,000 to rent\", \"saves you $400 a day\") unless that "
        "exact value appears verbatim in the catalog data provided. This is "
        "separate from the broader pricing-language ban — this rule covers "
        "invented numbers that would not trip the banned-phrase filter.\n"
        "3. Never invent local statistics, development counts, construction "
        "figures, project counts, permit numbers, or demographic claims (for "
        "example \"three new subdivisions broke ground last month\", \"over "
        "200 new developments in Clay County\", \"permits are up 30 percent "
        "this year\"). Local references must stay general and verifiable — "
        "phrases like \"North Florida's building season\", \"Clay County's "
        "heavy clay soil\", or \"hurricane season\" are fine. Specific "
        "counts, percentages, and statistics about the area are not.\n"
        "4. Every specific factual claim in the caption (spec values, model "
        "names, capacities, dimensions, place names, numbers, attribution) "
        "must trace back either to the catalog data shown in the assignment "
        "or to general common knowledge that any reader could verify. If you "
        "cannot point to where a claim comes from, do not include it. When "
        "in doubt, write general framing instead of a specific number.\n\n"
        "Sentence length rule: every sentence in caption_hook and caption_body "
        "must be 18 words or fewer. No exceptions. If a sentence needs more "
        "than 18 words to make its point, split it into two shorter sentences. "
        "Short, punchy sentences match the brand voice. Target 8-12 words per "
        "sentence.\n\n"
        "Return your output as valid JSON only. No markdown fences, no "
        "commentary before or after the JSON."
    )

    output_format = """{
  "caption_hook": "Opening line of the caption — the scroll-stopper",
  "caption_body": "The body of the caption. Each content line separated by \\n\\n for vertical stack formatting. Does NOT include the hook or CTA.",
  "cta_text": "The CTA line — last element of the caption. Empty string if cta_type is none.",
  "image_overlay_hook": "3-7 word text for image overlay. Empty string if text_overlay is FALSE.",
  "creative_hook_text": "1-7 word bold typographic hook for the Creatomate Hook-Text field. Punchy, declarative. Distinct hook — not a substring of caption_hook or image_overlay_hook. No trailing punctuation except '?'.",
  "review_excerpt": "Verbatim substring of Full review text. Selected per the Review Excerpt Selection criteria. Fits within the template character budget. Empty string when this is not a Social Proof post.",
  "selected_image_id": "The image_id of the candidate you selected. Empty string if no candidates were provided or only one was available.",
  "first_comment": "URL with short prefix for link posts. Empty string if not a link post.",
  "draft_rationale": "1-2 sentence note on the angle taken and any quality concerns."
}"""

    link_target = booking_url or website

    user_message = f"""# Assignment

platform: {platform}
objective: {objective}
content_type: {content_type}
focus_equipment_id: {focus_id or '(none)'}
angle: {angle}
cta_type: {cta_type}
media_format: {media_format}
text_overlay: {text_overlay}
draft_notes: {draft_notes or '(none)'}

# Equipment Details

{equipment_block}

{image_candidates_block}{review_block}# Brand Voice (full)

{brand_voice}

# CTA Rules (extracted for cta_type={cta_type!r})

{cta_section}

CTA destinations available for this business:
  phone: {phone}
  website: {website}
  booking_url: {booking_url}
  google_maps_url: {maps_url}

# CTA Output Constraint (cta_type={cta_type})

{cta_constraint_block}

# Platform Constraints ({platform})

Full caption character limit: {char_limit}
Feed truncation point (above the fold): ~{feed_trunc} chars
Link behavior: {'in-caption button (GBP)' if platform == 'gbp' else 'first comment for FB/IG'}

{caption_target_block}

{platform_section}

# Content Type Definition ({content_type})

{content_type_section or '(no specific guidance loaded — use general best practices for this content type)'}

# Hook Creation Direction

{hook_section}

# Creative Hook Text Direction

The `creative_hook_text` field is a SEPARATE hook used as the Hook-Text overlay
on Creatomate equipment-post templates. It is NOT a truncation, copy, or
substring of `caption_hook` or `image_overlay_hook`. Generate it with the same
hook creation skill above, but optimize for bold typography on a still or
video creative:

- Hard limit: 1-7 words (≤7 is non-negotiable).
- Punchy and declarative. Read it as a headline, not a sentence opener.
- No trailing periods, commas, semicolons, or colons. A trailing `?` is allowed.
- Concrete, specific, and easy to read at a glance.
- Must be a DISTINCT hook from caption_hook: neither string should be a
  substring of the other. If your first draft of `creative_hook_text` repeats
  any portion of `caption_hook`, rewrite it from a different angle.

# Few-Shot Examples (selected for content_type={content_type!r}, objective={objective!r})

{few_shots}

# Output Format

Return exactly this JSON shape (no markdown fences, no commentary):

{output_format}

# Critical Instructions

- `caption_body` does NOT include the hook or the CTA. Those are separate fields.
- The final assembled caption will be: caption_hook + \\n\\n + caption_body + \\n\\n + cta_text
- No markdown. No emoji. No exclamation points. No em dashes (use commas, periods, or line breaks instead).
- Vertical stack formatting in `caption_body`: every content line followed by a blank line (\\n\\n).
- `image_overlay_hook` is 3-7 words max. Required when text_overlay is {text_overlay}; {'must be populated' if overlay_needed else 'must be empty string'}.
- `creative_hook_text` is 1-7 words max and is ALWAYS populated. It must be distinct from `caption_hook` (no substring overlap in either direction) and must not end with `.`, `,`, `;`, or `:` (a trailing `?` is fine).
- `review_excerpt` is populated ONLY for Social Proof posts (a review block appears above when so). When such a block is shown, the excerpt MUST be a verbatim substring of the full review text (no rewriting, no paraphrasing) and must fit within the character budget given in the excerpt-selection section. Otherwise `review_excerpt` must be an empty string.
- `selected_image_id`: when multiple source images are listed above, return the image_id of the candidate whose context best matches this post's angle. When only one image is available or no candidates are listed, return empty string.
- `first_comment` is populated {'(link post — include the URL ' + link_target + ' with a short lead-in prefix)' if is_link_post else '(empty string — this is not a link post)'}.
- The phone number for call CTAs is: {phone}
- Never include pricing language (per pricing policy).
- Anti-fabrication: no invented customer stories, no invented dollar amounts, no invented local statistics. See the rules in the system message. When in doubt, use general framing instead of a specific number or anecdote.
- Every sentence in caption_hook and caption_body must be 18 words or fewer. Split long sentences rather than trimming meaning. Target 8-12 words per sentence for punchy, readable copy.
{caption_target_reinforcement}
{cta_constraint_reinforcement}
- All text must pass the brand voice quality checklist.
"""

    if revision_block:
        user_message += revision_block

    if retry_nudge:
        user_message += (
            "\n\nIMPORTANT: Return valid JSON only. No markdown fences, no "
            "commentary. Begin your response with `{` and end with `}`.\n"
        )

    return system_message, user_message


# --- LLM call ---

def call_anthropic(
    system_message: str,
    user_message: str,
    model: str = ANTHROPIC_MODEL,
    max_tokens: int = ANTHROPIC_MAX_TOKENS,
) -> str:
    """Call Anthropic Claude and return the raw text response."""
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment")

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_message,
        messages=[{"role": "user", "content": user_message}],
    )
    text_parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    return "".join(text_parts)


def get_llm_draft(
    row: dict,
    catalog_item: dict | None,
    config: Config,
    skills: dict[str, str],
    review_data: dict | None = None,
    review_excerpt_max_chars: int | None = None,
    image_candidates: list[dict] | None = None,
    revision_block: str = "",
) -> dict:
    """Call the LLM with one JSON-only retry. Returns the parsed dict."""
    common_kwargs = dict(
        row=row,
        catalog_item=catalog_item,
        config=config,
        brand_voice=skills["brand_voice"],
        hook_skill=skills["hook_creation"],
        cta_skill=skills["cta"],
        platform_style=skills["platform_style"],
        few_shot_library=skills["few_shot_library"],
        strategy_guidance=skills["strategy_guidance"],
        review_data=review_data,
        review_excerpt_skill=skills.get("review_excerpt_selection", ""),
        review_excerpt_max_chars=review_excerpt_max_chars,
        image_candidates=image_candidates,
        revision_block=revision_block,
    )
    system_msg, user_msg = build_drafter_messages(**common_kwargs)

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = call_anthropic(system_msg, user_msg)
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
                # Rebuild with a JSON-only nudge.
                system_msg, user_msg = build_drafter_messages(
                    retry_nudge=True, **common_kwargs,
                )
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("LLM retry loop exited without result")


# --- Image prompt assembly ---

def assemble_image_prompt(
    image_prompt_universal: str,
    image_prompt_social: str,
    text_overlay: bool,
    overlay_hook: str,
    platform: str,
) -> str:
    """Combine universal preamble + Section A/B with runtime substitutions."""
    section = extract_image_prompt_section(image_prompt_social, text_overlay)
    if text_overlay:
        section = section.replace("[HOOK_TEXT]", overlay_hook or "")
    platform_key = (platform or "").strip().lower()
    platform_display = PLATFORM_DISPLAY_NAMES.get(
        platform_key, platform_key.title() or platform_key,
    )
    section = section.replace("{{PLATFORM}}", platform_display)
    universal = (image_prompt_universal or "").replace(
        "{{PLATFORM}}", platform_display,
    )
    if universal and section:
        return f"{universal.strip()}\n\n{section.strip()}"
    return (universal or section).strip()


# --- Creatomate template rotation ---

def _template_keys_sorted(templates: dict | None) -> list[str]:
    if not isinstance(templates, dict):
        return []
    return sorted(templates.keys())


def select_template(
    templates: dict | None,
    row_id: str,
) -> tuple[str, str]:
    """Return (template_key, template_id), or ('', '') if no templates.

    Selection is deterministic but rotates across the catalog: hash of row_id
    modulo template count. This keeps the same row reproducible while
    spreading templates across the feed.
    """
    keys = _template_keys_sorted(templates)
    if not keys or templates is None:
        return "", ""
    digest = hashlib.sha1(row_id.encode("utf-8")).digest()
    idx = digest[0] % len(keys)
    key = keys[idx]
    template = templates[key]
    tid = ""
    if isinstance(template, dict):
        tid = str(template.get("id", "")).strip()
    return key, tid


def _source_image_url(image_id: str) -> str:
    """Public-style Drive URL for Creatomate to fetch."""
    return f"https://drive.google.com/uc?id={image_id}&export=download"


def _select_review_template(
    templates: dict | None,
    row_id: str,
) -> tuple[str, str, list[str]]:
    """Return (template_key, template_id, extra_dynamic_fields) for a review template.

    Wraps select_template so callers can also inspect ``extra_dynamic_fields``
    (used to decide whether to populate ``Equipment-Photo``).
    """
    key, tid = select_template(templates, row_id)
    extra: list[str] = []
    if isinstance(templates, dict):
        template = templates.get(key, {})
        if isinstance(template, dict):
            raw = template.get("extra_dynamic_fields", []) or []
            if isinstance(raw, list):
                extra = [str(f).strip() for f in raw if str(f).strip()]
    return key, tid, extra


def _select_review_template_max_chars(
    media_format: str,
    row_id: str,
    config: Config,
) -> int | None:
    """Return the ``max_review_text_chars`` for the template that will render
    for this row, or None if the format isn't a review format or the
    template lacks the setting.

    Selection is deterministic on ``row_id`` (same hash → same template),
    so this can be called before rendering to determine the character
    budget passed into the LLM prompt.
    """
    config_path = {
        "creatomate_review_image": "creatomate.review_image.templates",
        "creatomate_review_video": "creatomate.review_video.templates",
    }.get(media_format)
    if not config_path:
        return None
    templates = config.get(config_path, {}) or {}
    key, _tid = select_template(templates, row_id)
    if not key or not isinstance(templates, dict):
        return None
    template = templates.get(key, {})
    if not isinstance(template, dict):
        return None
    raw = template.get("max_review_text_chars")
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _render_review_image(
    row_id: str,
    review_data: dict,
    source_image_url: str,
    config: Config,
    excerpt: str | None = None,
) -> dict:
    """Render a Creatomate review-image template. Returns a result dict.

    ``excerpt`` is the Review-Text value to inject; when omitted or empty,
    falls back to ``review_data[REV_EXCERPT_LONG]`` (legacy behavior).
    """
    templates = config.get("creatomate.review_image.templates", {}) or {}
    key, tid, extra_fields = _select_review_template(templates, row_id)
    if not tid:
        return {
            "success": False,
            "output_path": "",
            "error": "no review_image templates configured",
            "template_key": "",
        }

    review_text_value = (excerpt or "").strip() or str(
        review_data.get(REV_EXCERPT_LONG, "")
    )

    text_fields = {
        "Review-Text": review_text_value,
        "Reviewer-Name": str(review_data.get(REV_REVIEWER_FIRST_NAME, "")),
        "Star-Rating": REVIEW_STAR_RATING_GLYPH,
    }
    image_fields: dict[str, str] = {}
    if source_image_url and "Equipment-Photo" in extra_fields:
        image_fields["Equipment-Photo"] = source_image_url

    mods = creatomate_helpers.build_modifications(
        image_fields=image_fields,
        text_fields=text_fields,
    )
    output_path = os.path.join(TMP_DIR, f"{row_id}_review.png")
    res = creatomate_helpers.render_template(tid, mods, output_path)
    return {
        "success": bool(res.get("success")),
        "output_path": res.get("output_path", ""),
        "error": res.get("error", ""),
        "template_key": key,
    }


def _render_review_video(
    row_id: str,
    review_data: dict,
    source_image_url: str,
    config: Config,
    excerpt: str | None = None,
) -> dict:
    """Render a Creatomate review-video template. Returns a result dict.

    ``excerpt`` is the Review-Text value to inject; when omitted or empty,
    falls back to ``review_data[REV_EXCERPT_LONG]`` (legacy behavior).
    """
    templates = config.get("creatomate.review_video.templates", {}) or {}
    key, tid, extra_fields = _select_review_template(templates, row_id)
    if not tid:
        return {
            "success": False,
            "output_path": "",
            "error": "no review_video templates configured",
            "template_key": "",
        }

    review_text_value = (excerpt or "").strip() or str(
        review_data.get(REV_EXCERPT_LONG, "")
    )

    text_fields = {
        "Review-Text": review_text_value,
        "Reviewer-Name": str(review_data.get(REV_REVIEWER_FIRST_NAME, "")),
    }
    image_fields: dict[str, str] = {}
    if source_image_url and "Equipment-Photo" in extra_fields:
        image_fields["Equipment-Photo"] = source_image_url

    mods = creatomate_helpers.build_modifications(
        image_fields=image_fields,
        text_fields=text_fields,
    )
    output_path = os.path.join(TMP_DIR, f"{row_id}_review.mp4")
    res = creatomate_helpers.render_template(tid, mods, output_path)
    return {
        "success": bool(res.get("success")),
        "output_path": res.get("output_path", ""),
        "error": res.get("error", ""),
        "template_key": key,
    }


def _generate_review_media(
    row: dict,
    media_format: str,
    review_data: dict | None,
    image_id: str,
    config: Config,
    excerpt: str | None = None,
) -> dict:
    """Render a review media asset. Same return shape as generate_media.

    ``excerpt`` is the Review-Text value to inject into the chosen template
    (typically the LLM-selected excerpt validated against the original review
    text). When empty/None, the renderers fall back to
    ``review_data[REV_EXCERPT_LONG]``.
    """
    row_id = str(row.get(CQ_ROW_ID, "")).strip() or "unknown"

    if not review_data:
        return {
            "success": False,
            "output_path": "",
            "media_format_used": "",
            "fallback_chain": [(media_format, "no review data available")],
            "error": "no review data available",
            "template_key": "",
        }

    source_image_url = _source_image_url(image_id) if image_id else ""
    chain: list[tuple[str, str]] = []

    if media_format == "creatomate_review_image":
        res = _render_review_image(
            row_id, review_data, source_image_url, config, excerpt=excerpt,
        )
        if res.get("success"):
            return {
                "success": True,
                "output_path": res["output_path"],
                "media_format_used": "creatomate_review_image",
                "fallback_chain": chain,
                "error": "",
                "template_key": res.get("template_key", ""),
            }
        chain.append(("creatomate_review_image", res.get("error", "render failed")))
        return {
            "success": False,
            "output_path": "",
            "media_format_used": "",
            "fallback_chain": chain,
            "error": res.get("error", "render failed"),
            "template_key": "",
        }

    if media_format == "creatomate_review_video":
        video_res = _render_review_video(
            row_id, review_data, source_image_url, config, excerpt=excerpt,
        )
        if video_res.get("success"):
            return {
                "success": True,
                "output_path": video_res["output_path"],
                "media_format_used": "creatomate_review_video",
                "fallback_chain": chain,
                "error": "",
                "template_key": video_res.get("template_key", ""),
            }
        chain.append((
            "creatomate_review_video",
            video_res.get("error", "render failed"),
        ))
        print(
            f"[drafter] {row_id}: creatomate_review_video failed "
            f"({video_res.get('error')}) — falling back to creatomate_review_image",
            file=sys.stderr,
        )
        # Note: the fallback uses the same excerpt budget — strict callers may
        # want to re-validate against the review_image template's
        # max_review_text_chars, but for now we keep the LLM-selected excerpt
        # since the templates' budgets are close and the alternative is the
        # generic excerpt_long fallback.
        image_res = _render_review_image(
            row_id, review_data, source_image_url, config, excerpt=excerpt,
        )
        if image_res.get("success"):
            return {
                "success": True,
                "output_path": image_res["output_path"],
                "media_format_used": "creatomate_review_image",
                "fallback_chain": chain,
                "error": "",
                "template_key": image_res.get("template_key", ""),
            }
        chain.append((
            "creatomate_review_image",
            image_res.get("error", "render failed"),
        ))
        return {
            "success": False,
            "output_path": "",
            "media_format_used": "",
            "fallback_chain": chain,
            "error": "all review media generation attempts failed",
            "template_key": "",
        }

    return {
        "success": False,
        "output_path": "",
        "media_format_used": "",
        "fallback_chain": [(media_format, "unknown review media format")],
        "error": f"unknown review media format: {media_format!r}",
        "template_key": "",
    }


def _derive_video_hook(
    caption_hook: str,
    angle: str,
    max_words: int = 7,
) -> str:
    """Derive a short Hook-Text string for a Creatomate video template.

    Used when media_format=creatomate_video and the LLM left
    image_overlay_hook empty because text_overlay=FALSE. Video templates
    still need Hook-Text populated to drive their built-in text track.

    Strategy: prefer the LLM-written caption_hook (the scroll-stopping
    opening line); fall back to the angle when caption_hook is empty.
    Take the first clause (split on '.', '?', ';', or newline), trim
    fragment punctuation and banned chars, and cap at max_words.
    """
    source = (caption_hook or "").strip() or (angle or "").strip()
    if not source:
        return ""
    first = re.split(r"[.?\n;]", source, maxsplit=1)[0].strip()
    first = first.rstrip(".,;:")
    for ch in BANNED_CHARS:
        first = first.replace(ch, "")
    first = first.strip()
    words = first.split()
    if len(words) > max_words:
        words = words[:max_words]
    return " ".join(words)


# --- Media generation ---

def _ensure_tmp() -> None:
    os.makedirs(TMP_DIR, exist_ok=True)


def generate_media(
    row: dict,
    image_id: str,
    overlay_hook: str,
    caption_hook: str,
    image_prompt_universal: str,
    image_prompt_social: str,
    config: Config,
    drive_service: Any,
    review_data: dict | None = None,
    creative_hook_text: str = "",
    review_excerpt: str = "",
) -> dict:
    """Generate the media asset for `row`. Returns a result dict.

    Result keys:
        success: bool
        output_path: local path of the generated/rendered file ('' on skip)
        media_format_used: which pipeline actually succeeded
        fallback_chain: list of (format, reason) tuples for each attempt
        error: '' on success
    """
    _ensure_tmp()

    row_id = str(row.get(CQ_ROW_ID, "")).strip() or "unknown"
    platform = str(row.get(CQ_PLATFORM, "")).strip().lower()
    media_format = str(row.get(CQ_MEDIA_FORMAT, "")).strip()
    text_overlay = str(row.get(CQ_TEXT_OVERLAY, "")).strip().upper() == "TRUE"

    # Review media has its own pipeline — driven by Reviews Sheet data, not an
    # equipment photo. Source image is optional (only photo_testimonial and
    # photo_reveal templates use it).
    if media_format in REVIEW_MEDIA_FORMATS:
        return _generate_review_media(
            row=row,
            media_format=media_format,
            review_data=review_data,
            image_id=image_id,
            config=config,
            excerpt=review_excerpt or None,
        )

    fallback_chain: list[tuple[str, str]] = []

    if not image_id:
        return {
            "success": False,
            "output_path": "",
            "media_format_used": "",
            "fallback_chain": [(media_format, "no source image available")],
            "error": "no source image available",
        }

    source_image_url = _source_image_url(image_id)

    # Order of fallbacks: stay within the family until we have to step down.
    if media_format == "creatomate_video":
        attempt_order = [
            "creatomate_video",
            "creatomate_text_overlay",
            "image2_text_overlay",
        ]
    elif media_format == "creatomate_text_overlay":
        attempt_order = [
            "creatomate_text_overlay",
            "image2_text_overlay",
        ]
    elif media_format == "image2_text_overlay":
        attempt_order = ["image2_text_overlay", "image2_enhanced"]
    else:
        attempt_order = ["image2_enhanced"]

    # Download source once for image2 attempts.
    source_local_path: str | None = None

    def _ensure_source_downloaded() -> str:
        nonlocal source_local_path
        if source_local_path:
            return source_local_path
        out = os.path.join(TMP_DIR, f"{row_id}_source.jpg")
        path = drive_helpers.download_file(image_id, out, service=drive_service)
        source_local_path = path
        return path

    for fmt in attempt_order:
        if fmt == "creatomate_video":
            templates = config.get(
                "creatomate.equipment_post_video.templates", {},
            ) or {}
            key, tid = select_template(templates, row_id)
            if not tid:
                fallback_chain.append((fmt, "no video templates configured"))
                continue
            # Hook-Text on Creatomate equipment templates is populated from
            # creative_hook_text — a distinct ≤7-word hook generated alongside
            # the caption. Fall back to overlay_hook and then to a derived
            # video hook only when creative_hook_text is missing (legacy or
            # validation-skipped paths).
            video_hook = (creative_hook_text or "").strip()
            if not video_hook:
                video_hook = (overlay_hook or "").strip()
            if not video_hook:
                video_hook = _derive_video_hook(
                    caption_hook=caption_hook,
                    angle=str(row.get(CQ_ANGLE, "")),
                )
            mods = creatomate_helpers.build_modifications(
                image_fields={"Equipment-Photo": source_image_url},
                text_fields={"Hook-Text": video_hook},
            )
            output_path = os.path.join(TMP_DIR, f"{row_id}_rendered.mp4")
            res = creatomate_helpers.render_template(tid, mods, output_path)
            if res.get("success"):
                return {
                    "success": True,
                    "output_path": res["output_path"],
                    "media_format_used": fmt,
                    "fallback_chain": fallback_chain,
                    "error": "",
                    "template_key": key,
                }
            fallback_chain.append((fmt, res.get("error", "render failed")))
            print(
                f"[drafter] {row_id}: creatomate_video failed "
                f"({res.get('error')}) — falling back",
                file=sys.stderr,
            )
            continue

        if fmt == "creatomate_text_overlay":
            templates = config.get(
                "creatomate.equipment_post_image.templates", {},
            ) or {}
            key, tid = select_template(templates, row_id)
            if not tid:
                fallback_chain.append((fmt, "no image templates configured"))
                continue
            # Prefer creative_hook_text (distinct ≤7-word hook designed for
            # bold typography) over overlay_hook (which mirrors image2 in-image
            # text) for the Hook-Text element.
            hook_text_value = (
                (creative_hook_text or "").strip() or (overlay_hook or "")
            )
            mods = creatomate_helpers.build_modifications(
                image_fields={"Equipment-Photo": source_image_url},
                text_fields={"Hook-Text": hook_text_value},
            )
            output_path = os.path.join(TMP_DIR, f"{row_id}_rendered.png")
            res = creatomate_helpers.render_template(tid, mods, output_path)
            if res.get("success"):
                return {
                    "success": True,
                    "output_path": res["output_path"],
                    "media_format_used": fmt,
                    "fallback_chain": fallback_chain,
                    "error": "",
                    "template_key": key,
                }
            fallback_chain.append((fmt, res.get("error", "render failed")))
            print(
                f"[drafter] {row_id}: creatomate_text_overlay failed "
                f"({res.get('error')}) — falling back",
                file=sys.stderr,
            )
            continue

        if fmt in ("image2_text_overlay", "image2_enhanced"):
            try:
                src_local = _ensure_source_downloaded()
            except Exception as exc:
                fallback_chain.append(
                    (fmt, f"source download failed: {type(exc).__name__}: {exc}")
                )
                print(
                    f"[drafter] {row_id}: source download failed: {exc}",
                    file=sys.stderr,
                )
                continue

            prompt = assemble_image_prompt(
                image_prompt_universal=image_prompt_universal,
                image_prompt_social=image_prompt_social,
                text_overlay=(fmt == "image2_text_overlay"),
                overlay_hook=overlay_hook if fmt == "image2_text_overlay" else "",
                platform=platform,
            )
            output_path = os.path.join(TMP_DIR, f"{row_id}_generated.png")
            res = image_generator.generate_image(src_local, prompt, output_path)
            if res.get("success"):
                return {
                    "success": True,
                    "output_path": res["output_path"],
                    "media_format_used": fmt,
                    "fallback_chain": fallback_chain,
                    "error": "",
                    "template_key": "",
                }
            fallback_chain.append((fmt, res.get("error", "image gen failed")))
            print(
                f"[drafter] {row_id}: {fmt} failed ({res.get('error')}) — "
                f"falling back",
                file=sys.stderr,
            )
            continue

    # Every attempt failed.
    return {
        "success": False,
        "output_path": "",
        "media_format_used": "",
        "fallback_chain": fallback_chain,
        "error": "all media generation attempts failed",
        "template_key": "",
    }


# --- Drive upload ---

def upload_media_to_drive(
    local_path: str,
    row_id: str,
    config: Config,
    drive_service: Any,
) -> str:
    """Upload the generated media file. Returns Drive file ID or ''."""
    folder_id = config.get("drive.generated_images_folder_id", "")
    if not folder_id:
        print(
            f"[drafter] {row_id}: drive.generated_images_folder_id not set — "
            f"skipping upload",
            file=sys.stderr,
        )
        return ""
    ext = os.path.splitext(local_path)[1].lstrip(".") or "png"
    file_name = f"{row_id}_media.{ext}"
    try:
        return drive_helpers.upload_file(
            local_path=local_path,
            folder_id=folder_id,
            file_name=file_name,
            service=drive_service,
        )
    except Exception as exc:
        print(
            f"[drafter] {row_id}: Drive upload failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return ""


def update_review_usage(
    review_id: str,
    reviews_sheet_id: str,
    reviews_tab: str,
    service: Any,
    now: datetime | None = None,
) -> bool:
    """Increment times_used and set last_used_date on the matching review row.

    Called after a Social Proof post's media is successfully generated, so the
    Strategist's rotation logic (prefer unused reviews, then oldest-used) sees
    the row as recently used. Failures are logged and return False — they do
    not abort the Drafter's main flow.
    """
    if not (review_id or "").strip() or not reviews_sheet_id or not reviews_tab:
        return False

    try:
        matches = sheets_helpers.find_rows_by_column_value(
            reviews_sheet_id, reviews_tab, REV_REVIEW_ID, review_id,
            service=service,
        )
    except Exception as exc:
        print(
            f"[drafter] WARN: could not look up review {review_id!r} for "
            f"usage update: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False

    if not matches:
        print(
            f"[drafter] WARN: review {review_id!r} not in Reviews Sheet — "
            f"skipping usage update",
            file=sys.stderr,
        )
        return False

    row_number, row_data = matches[0]
    try:
        current_count = int(str(row_data.get(REV_TIMES_USED, "0") or "0").strip())
    except (TypeError, ValueError):
        current_count = 0
    new_count = current_count + 1
    today = (now or _now_et()).astimezone(ET).date().isoformat()

    try:
        sheets_helpers.update_cells(
            spreadsheet_id=reviews_sheet_id,
            tab_name=reviews_tab,
            row_number=row_number,
            col_updates={
                REV_TIMES_USED: str(new_count),
                REV_LAST_USED_DATE: today,
            },
            service=service,
        )
        return True
    except Exception as exc:
        print(
            f"[drafter] WARN: could not write review usage for "
            f"{review_id!r}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False


# --- Per-row processing ---

def _skill_safe(name: str, config: Config, default: str = "") -> str:
    try:
        return skill_loader.load_skill(name, config=config)
    except Exception as exc:
        print(
            f"[drafter] WARN: could not load skill {name!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return default


def load_drafter_context(config: Config) -> dict[str, Any]:
    """Load all run-once context: services, sheets, skills."""
    sheets_service = sheets_helpers.get_sheets_service()
    drive_service = drive_helpers.get_drive_service()

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

    # Reviews are optional — Social Proof rows need them, but other rows
    # don't. A missing/unreadable Reviews Sheet should not block the run.
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
                f"[drafter] WARN: could not read Reviews Sheet: "
                f"{type(exc).__name__}: {exc} — Social Proof rows will "
                f"skip media generation",
                file=sys.stderr,
            )

    skills = {
        "brand_voice": _skill_safe("brand_voice", config),
        "hook_creation": _skill_safe("hook_creation", config),
        "cta": _skill_safe("cta", config),
        "platform_style": _skill_safe("platform_style", config),
        "image_prompt_universal": _skill_safe("image_prompt_universal", config),
        "image_prompt_social": _skill_safe("image_prompt_social", config),
        "few_shot_library": _skill_safe("few_shot_library", config),
        "strategy_guidance": _skill_safe("strategy_guidance", config),
        "review_excerpt_selection": _skill_safe("review_excerpt_selection", config),
    }

    return {
        "sheets_service": sheets_service,
        "drive_service": drive_service,
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


def _find_row_by_id(queue_rows: list[dict], row_id: str) -> dict | None:
    target = row_id.strip()
    for r in queue_rows:
        if str(r.get(CQ_ROW_ID, "")).strip() == target:
            return r
    return None


def _find_catalog_item(catalog_rows: list[dict], item_id: str) -> dict | None:
    target = item_id.strip()
    if not target:
        return None
    for r in catalog_rows:
        if str(r.get(CAT_ITEM_ID, "")).strip() == target:
            return r
    return None


def _find_review_by_id(reviews_rows: list[dict], review_id: str) -> dict | None:
    target = (review_id or "").strip()
    if not target:
        return None
    for r in reviews_rows:
        if str(r.get(REV_REVIEW_ID, "")).strip() == target:
            return r
    return None


def draft_single_row(
    row_id: str,
    context: dict[str, Any],
    config: Config,
    dry_run: bool = False,
    revision_round: int = 1,
    previous_output: dict | None = None,
) -> dict:
    """Draft a single Content Queue row end-to-end."""
    row = _find_row_by_id(context["queue_rows"], row_id)
    if row is None:
        return {
            "status": "error",
            "row_id": row_id,
            "error": "row not found in Content Queue",
            "dry_run": dry_run,
        }

    actual_status = str(row.get(CQ_STATUS, "")).strip().lower()
    is_revision = revision_round > 1
    allowed = {"planned"} if not is_revision else {"planned", "drafted"}
    if actual_status not in allowed:
        return {
            "status": "skipped",
            "row_id": row_id,
            "reason": (
                f"status is {actual_status!r}, expected one of "
                f"{sorted(allowed)} (revision_round={revision_round})"
            ),
            "dry_run": dry_run,
        }

    focus_id = str(row.get(CQ_FOCUS_EQUIPMENT, "")).strip()
    catalog_item: dict | None = None
    if focus_id:
        catalog_item = _find_catalog_item(context["catalog_rows"], focus_id)
        if catalog_item is None:
            return {
                "status": "error",
                "row_id": row_id,
                "error": f"catalog item {focus_id!r} not found",
                "dry_run": dry_run,
            }

    # Look up review data for Social Proof rows. If review_id is set but the
    # row isn't found, log and continue with review_data=None — the caption
    # can still draft from the angle, but media generation will skip.
    review_id = str(row.get(CQ_REVIEW_ID, "")).strip()
    review_data: dict | None = None
    if review_id:
        review_data = _find_review_by_id(
            context.get("reviews_rows", []), review_id,
        )
        if review_data is None:
            print(
                f"[drafter] {row_id}: review_id {review_id!r} not found in "
                f"Reviews Sheet — media generation will skip; caption will "
                f"draft from the angle",
                file=sys.stderr,
            )

    # --- Step 2: select source image ---
    media_format = str(row.get(CQ_MEDIA_FORMAT, "")).strip()
    existing_image_id = str(row.get(CQ_SOURCE_IMAGE, "")).strip()
    image_candidates: list[dict] = []
    fallback_image_id: str = ""

    if existing_image_id:
        image_result = {
            "image_id": existing_image_id,
            "selection_reason": "pre-assigned by Strategist",
            "total_available": 0,
            "fallback": False,
        }
    elif catalog_item is not None and media_format not in REVIEW_MEDIA_FORMATS:
        candidates_result = image_selector.select_top_candidates(
            equipment_id=focus_id,
            catalog_row=catalog_item,
            config=config,
            service=context["sheets_service"],
            max_candidates=5,
        )
        image_candidates = candidates_result.get("candidates", [])
        fallback_image_id = candidates_result.get("fallback_image_id", "")

        if image_candidates:
            image_result = {
                "image_id": image_candidates[0]["image_id"],
                "selection_reason": "top-ranked candidate (pending LLM selection)",
                "total_available": candidates_result.get("total_available", 0),
                "fallback": False,
            }
        elif fallback_image_id:
            image_result = {
                "image_id": fallback_image_id,
                "selection_reason": "fallback to primary_image_id (no candidates)",
                "total_available": 0,
                "fallback": True,
            }
        else:
            image_result = {
                "image_id": "",
                "selection_reason": "no images available",
                "total_available": 0,
                "fallback": True,
            }
    elif catalog_item is not None:
        # Review media path — keep deterministic single-image selection.
        image_result = image_selector.select_source_image(
            equipment_id=focus_id,
            catalog_row=catalog_item,
            config=config,
            service=context["sheets_service"],
        )
    else:
        image_result = {
            "image_id": "",
            "selection_reason": "no focus equipment — no image selected",
            "total_available": 0,
            "fallback": False,
        }

    image_id = str(image_result.get("image_id", "")).strip()
    if (
        not image_id
        and media_format != ""
        and media_format not in REVIEW_MEDIA_FORMATS
    ):
        print(
            f"[drafter] {row_id}: no source image available — "
            f"caption will be generated but media will be skipped",
            file=sys.stderr,
        )

    # --- Step 3b: pre-select the review template's character budget ---
    # The Drafter does deterministic template rotation by row_id, so the same
    # row_id used in _render_review_* later resolves to the same template
    # here. Reading max_review_text_chars now lets us pass the budget to the
    # LLM as a hard constraint on the selected excerpt.
    review_excerpt_max_chars = (
        _select_review_template_max_chars(media_format, row_id, config)
        if media_format in REVIEW_MEDIA_FORMATS
        else None
    )

    # --- Step 4-5: caption via LLM ---
    revision_block = build_revision_block(previous_output, revision_round)
    try:
        parsed = get_llm_draft(
            row=row,
            catalog_item=catalog_item,
            config=config,
            skills=context["skills"],
            review_data=review_data,
            review_excerpt_max_chars=review_excerpt_max_chars,
            image_candidates=image_candidates,
            revision_block=revision_block,
        )
    except Exception as exc:
        return {
            "status": "error",
            "row_id": row_id,
            "error": f"LLM call failed: {type(exc).__name__}: {exc}",
            "dry_run": dry_run,
        }

    parsed["image_overlay_hook"] = clean_overlay_text(
        str(parsed.get("image_overlay_hook", ""))
    )
    parsed["creative_hook_text"] = clean_overlay_text(
        str(parsed.get("creative_hook_text", ""))
    )

    # --- Step 5b: enforce sentence length (B6) ---
    violations = find_sentence_length_violations(parsed)
    sentence_rewrite_warnings: list[str] = []
    if violations:
        parsed, sentence_rewrite_warnings = rewrite_long_sentences(
            violations, parsed,
        )
        for w in sentence_rewrite_warnings:
            print(f"[drafter] {row_id}: {w}", file=sys.stderr)

    issues, full_caption = validate_llm_output(
        parsed=parsed,
        text_overlay_flag=str(row.get(CQ_TEXT_OVERLAY, "")),
        platform=str(row.get(CQ_PLATFORM, "")),
        objective=str(row.get(CQ_OBJECTIVE, "")),
        cta_type=str(row.get(CQ_CTA_TYPE, "")),
        media_format=str(row.get(CQ_MEDIA_FORMAT, "")),
    )
    if issues:
        return {
            "status": "error",
            "row_id": row_id,
            "error": "LLM output failed validation",
            "validation_issues": issues,
            "llm_output": parsed,
            "dry_run": dry_run,
        }

    caption_hook = str(parsed.get("caption_hook", "")).strip()
    caption_body = str(parsed.get("caption_body", "")).strip()
    cta_text = str(parsed.get("cta_text", "")).strip()
    overlay_hook = str(parsed.get("image_overlay_hook", "")).strip()
    creative_hook_text_val = str(parsed.get("creative_hook_text", "")).strip()
    first_comment = str(parsed.get("first_comment", "")).strip()
    draft_rationale = str(parsed.get("draft_rationale", "")).strip()

    # --- Step 5c: apply LLM image selection ---
    if image_candidates and len(image_candidates) > 1:
        llm_image_pick = str(parsed.get("selected_image_id", "")).strip()
        valid_ids = {c["image_id"] for c in image_candidates}
        if llm_image_pick and llm_image_pick in valid_ids:
            image_id = llm_image_pick
            image_result["image_id"] = llm_image_pick
            image_result["selection_reason"] = "LLM-selected from candidates"
        elif llm_image_pick:
            print(
                f"[drafter] {row_id}: LLM selected_image_id "
                f"{llm_image_pick!r} not in candidate list — "
                f"keeping top-ranked default",
                file=sys.stderr,
            )

    # --- Step 6b: review-excerpt selection + validation ---
    # When this is a Social Proof post, the LLM returned a `review_excerpt`
    # field. Validate it is a verbatim substring of the original review text
    # and fits within the template's character budget. On failure, fall back
    # to excerpt_long from the Reviews Sheet (legacy behavior) and log a
    # warning so the owner can spot recurring selection issues.
    selected_review_excerpt: str = ""
    review_excerpt_warning: str = ""
    if media_format in REVIEW_MEDIA_FORMATS and review_data is not None:
        llm_excerpt = str(parsed.get("review_excerpt", "")).strip()
        ok_excerpt, reason_excerpt = validate_review_excerpt(
            llm_excerpt,
            str(review_data.get(REV_REVIEW_TEXT, "")),
            review_excerpt_max_chars,
        )
        if ok_excerpt:
            selected_review_excerpt = llm_excerpt
        else:
            fallback_excerpt = str(review_data.get(REV_EXCERPT_LONG, ""))
            review_excerpt_warning = (
                f"review_excerpt validation failed ({reason_excerpt}); "
                f"falling back to Reviews Sheet excerpt_long"
            )
            print(
                f"[drafter] {row_id}: {review_excerpt_warning}",
                file=sys.stderr,
            )
            selected_review_excerpt = fallback_excerpt

    # --- Step 7: media generation ---
    media_result = generate_media(
        row=row,
        image_id=image_id,
        overlay_hook=overlay_hook,
        caption_hook=caption_hook,
        image_prompt_universal=context["skills"]["image_prompt_universal"],
        image_prompt_social=context["skills"]["image_prompt_social"],
        config=config,
        drive_service=context["drive_service"],
        review_data=review_data,
        creative_hook_text=creative_hook_text_val,
        review_excerpt=selected_review_excerpt,
    )

    media_generated = bool(media_result.get("success"))
    media_format_used = media_result.get("media_format_used", "")
    fallback_chain = media_result.get("fallback_chain", []) or []
    fallback_used = len(fallback_chain) > 0 and media_generated

    # --- Step 8: upload media ---
    media_drive_id = ""
    if media_generated and not dry_run:
        media_drive_id = upload_media_to_drive(
            local_path=media_result["output_path"],
            row_id=row_id,
            config=config,
            drive_service=context["drive_service"],
        )

    # --- Step 9: write to sheet ---
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
            CQ_STATUS: "drafted",
            CQ_CAPTION: full_caption,
            CQ_CREATIVE_HOOK_TEXT: creative_hook_text_val,
            CQ_FIRST_COMMENT: first_comment,
            CQ_CTA_TEXT: cta_text,
            CQ_HOOK_TEXT: caption_hook,
            CQ_IMAGE_OVERLAY_TEXT: overlay_hook,
            CQ_MEDIA_URL: media_drive_id,
            CQ_MEDIA_FORMAT_USED: media_format_used,
            CQ_DRAFT_RATIONALE: draft_rationale,
            CQ_REVISION_ROUND: str(revision_round),
        }
        if image_id:
            updates[CQ_SOURCE_IMAGE] = image_id

        # Filter to only columns that actually exist on the sheet — keeps the
        # Drafter resilient if the live sheet has fewer columns than the
        # registry lists.
        try:
            headers = sheets_helpers._get_headers(
                context["queue_id"],
                context["queue_tab"],
                service=context["sheets_service"],
            )
            updates = {k: v for k, v in updates.items() if k in headers}
        except Exception as exc:
            print(
                f"[drafter] WARN: could not fetch headers, attempting full "
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

    # --- Step 10: write back review usage on successful Social Proof posts ---
    review_usage_updated = False
    if (
        not dry_run
        and media_generated
        and review_id
        and review_data is not None
    ):
        review_usage_updated = update_review_usage(
            review_id=review_id,
            reviews_sheet_id=context.get("reviews_id", ""),
            reviews_tab=context.get("reviews_tab", ""),
            service=context["sheets_service"],
        )

    return {
        "status": "success",
        "row_id": row_id,
        "review_id": review_id,
        "review_usage_updated": review_usage_updated,
        "platform": str(row.get(CQ_PLATFORM, "")),
        "content_type": str(row.get(CQ_CONTENT_TYPE, "")),
        "focus_equipment_id": focus_id,
        "caption_length": len(full_caption),
        "media_format_used": media_format_used,
        "media_generated": media_generated,
        "source_image": {
            "image_id": image_id,
            "total_available": image_result.get("total_available", 0),
            "fallback": bool(image_result.get("fallback")),
            "selection_reason": image_result.get("selection_reason", ""),
        },
        "media_drive_id": media_drive_id,
        "fallback_used": fallback_used,
        "fallback_chain": fallback_chain,
        "caption": full_caption,
        "creative_hook_text": creative_hook_text_val,
        "review_excerpt": selected_review_excerpt,
        "review_excerpt_warning": review_excerpt_warning,
        "first_comment": first_comment,
        "draft_rationale": draft_rationale,
        "dry_run": dry_run,
    }


# --- All-planned mode ---

def find_planned_rows_in_window(
    queue_rows: list[dict],
    lead_time_hours: int,
    now: datetime | None = None,
) -> list[dict]:
    """Return all rows with status=planned whose scheduled_datetime is within
    `lead_time_hours` from now (inclusive on both ends)."""
    if now is None:
        now = _now_et()
    window_end = now + timedelta(hours=lead_time_hours)
    out: list[dict] = []
    for r in queue_rows:
        if str(r.get(CQ_STATUS, "")).strip().lower() != "planned":
            continue
        dt = _parse_dt(r.get(CQ_SCHEDULED, ""))
        if dt is None:
            continue
        if now <= dt <= window_end:
            out.append(r)
    out.sort(key=lambda r: _parse_dt(r.get(CQ_SCHEDULED, "")) or now)
    return out


def run_single(
    row_id: str,
    dry_run: bool = False,
    revision_round: int = 1,
    previous_output: dict | None = None,
) -> dict:
    """Public entry point for drafting a single row."""
    config = load_config()
    context = load_drafter_context(config)
    return draft_single_row(
        row_id,
        context,
        config,
        dry_run=dry_run,
        revision_round=revision_round,
        previous_output=previous_output,
    )


def run_all_planned(dry_run: bool = False, limit: int | None = None) -> dict:
    """Public entry point for batch drafting."""
    config = load_config()
    context = load_drafter_context(config)

    lead_time = int(config.get("strategy.lead_time_hours", 36))
    planned = find_planned_rows_in_window(context["queue_rows"], lead_time)
    if limit is not None:
        planned = planned[:limit]

    results: list[dict] = []
    rows_drafted = 0
    rows_skipped = 0
    rows_errored = 0
    for row in planned:
        rid = str(row.get(CQ_ROW_ID, "")).strip()
        if not rid:
            rows_errored += 1
            results.append({
                "status": "error",
                "row_id": "",
                "error": "row has empty row_id",
                "dry_run": dry_run,
            })
            continue
        res = draft_single_row(rid, context, config, dry_run=dry_run)
        results.append(res)
        if res.get("status") == "success":
            rows_drafted += 1
            # Refresh local cache so subsequent picks see updated status.
            row[CQ_STATUS] = "drafted"
        elif res.get("status") == "skipped":
            rows_skipped += 1
        else:
            rows_errored += 1

    return {
        "status": "success",
        "rows_processed": len(planned),
        "rows_drafted": rows_drafted,
        "rows_skipped": rows_skipped,
        "rows_errored": rows_errored,
        "results": results,
        "dry_run": dry_run,
    }


# --- CLI ---

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drafter agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--row-id",
        help="Draft a single Content Queue row by its row_id.",
    )
    group.add_argument(
        "--all-planned",
        action="store_true",
        help="Draft all planned rows scheduled within the lead-time window.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on rows processed in --all-planned mode (for testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate caption + media but do not write to sheets or Drive.",
    )
    parser.add_argument(
        "--revision-round",
        type=int,
        default=1,
        help=(
            "Revision round. >1 indicates a Critic-driven revision; "
            "requires --previous-output. Applies to --row-id only."
        ),
    )
    parser.add_argument(
        "--previous-output",
        default=None,
        help=(
            "Path to a JSON file containing the previous Critic output "
            "(failed_checks + fix_instructions). Required when "
            "--revision-round > 1. Applies to --row-id only."
        ),
    )
    args = parser.parse_args(argv)

    if args.all_planned and (
        args.revision_round != 1 or args.previous_output is not None
    ):
        parser.error(
            "--all-planned does not support --revision-round or "
            "--previous-output (revisions are always single-row)"
        )
    if args.revision_round > 1 and not args.previous_output:
        parser.error(
            "--revision-round > 1 requires --previous-output"
        )
    if args.previous_output and args.revision_round <= 1:
        parser.error(
            "--previous-output requires --revision-round > 1"
        )

    previous_output: dict | None = None
    if args.previous_output:
        try:
            with open(args.previous_output, "r", encoding="utf-8") as f:
                previous_output = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(
                f"could not load --previous-output "
                f"{args.previous_output!r}: {type(exc).__name__}: {exc}"
            )

    if args.row_id:
        result = run_single(
            args.row_id,
            dry_run=args.dry_run,
            revision_round=args.revision_round,
            previous_output=previous_output,
        )
    else:
        result = run_all_planned(dry_run=args.dry_run, limit=args.limit)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
