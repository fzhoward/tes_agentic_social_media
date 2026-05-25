"""Strategist agent — LLM-based editorial planner.

Plans a rolling 7-day content calendar by writing new rows to the Content
Queue sheet. The deterministic shell gathers data (catalog, queue,
performance log, skills), checks constraints (queue depth, planning window,
objective ratio), and writes validated rows. The LLM core decides content
types, catalog items, angles, CTAs, media formats, and scheduling.

Usage:
    python -m agents.strategist [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tools import sheets_helpers, skill_loader
from tools.config_loader import Config, load_config


# --- Constants ---

ET = ZoneInfo("America/New_York")

CONTENT_TYPES: list[str] = [
    "Equipment Spotlight / Product Feature",
    "Use-Case Scenario",
    "Job Story / Field Report",
    "Educational Tip",
    "Behind-the-Scenes",
    "Local Connection",
    "Comparison / Decision Helper",
    "Social Proof / Customer Story",
    "Promotional / Offer",
]

CTA_TYPES: list[str] = [
    "call",
    "dm",
    "click",
    "comment",
    "save",
    "directions",
    "book",
    "visit",
    "none",
]

MEDIA_FORMATS: list[str] = [
    "image2_enhanced",
    "image2_text_overlay",
    "creatomate_text_overlay",
    "creatomate_video",
]

TEXT_OVERLAY_FORMATS: set[str] = {"image2_text_overlay", "creatomate_text_overlay"}

OBJECTIVES: list[str] = ["brand_awareness", "lead_generation"]

PLATFORM_SHORT: dict[str, str] = {
    "facebook": "FB",
    "instagram": "IG",
    "gbp": "GBP",
}

# Statuses that count toward queue depth / window occupancy.
QUEUED_STATUSES: set[str] = {"planned", "drafted", "critiqued", "awaiting_approval"}

# Statuses that count for the max_queue_depth gate (per spec).
DEPTH_STATUSES: set[str] = {"planned", "awaiting_approval"}

# Catalog statuses considered eligible.
ACTIVE_CATALOG_STATUSES: set[str] = {"active", "seasonal", ""}

# Performance log lookback for objective-ratio correction.
PERFORMANCE_LOOKBACK_DAYS: int = 14

# Recent queue lookback included in LLM context.
RECENT_QUEUE_LOOKBACK_DAYS: int = 14

# Anthropic model.
ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"
ANTHROPIC_MAX_TOKENS: int = 8192

# Soft cap on creatomate_video posts per platform per week.
MAX_VIDEOS_PER_PLATFORM: int = 2

# Catalog item description truncation (chars) in compact list.
CATALOG_DESC_CHARS: int = 100

# Brand-voice / CTA / platform-style truncation (chars) to manage tokens.
BRAND_VOICE_MAX_CHARS: int = 4000
CTA_MAX_CHARS: int = 2000
PLATFORM_STYLE_MAX_CHARS: int = 2000


# --- Column names ---

# Content Queue (Strategist-written fields).
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

# Catalog columns referenced by the Strategist.
CAT_ITEM_ID = "item_id"
CAT_ITEM_NAME = "item_name"
CAT_CATEGORY = "category"
CAT_MODEL = "model"
CAT_STATUS = "status"
CAT_DESCRIPTION = "description"
CAT_COMMON_JOBS = "common_jobs"
CAT_BEST_FOR = "best_for"
CAT_POST_COUNT = "post_count"
CAT_LAST_POSTED = "last_posted"
CAT_IMAGE_COUNT = "image_count"

# Performance log columns.
PL_OBJECTIVE = "objective"
PL_POSTED_DATETIME = "posted_datetime"


# --- Datetime helpers ---

def _now_et() -> datetime:
    return datetime.now(ET)


def _parse_dt(raw: str) -> datetime | None:
    """Parse an ISO 8601 string into a tz-aware datetime, or None if unparseable.

    Accepts trailing "Z" by converting to "+00:00". Naive datetimes are
    assumed to be in ET.
    """
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


def _format_iso_et(dt: datetime) -> str:
    """Format a datetime as ISO 8601 with the ET offset."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET).isoformat()


# --- Step 2: Queue depth check ---

def count_depth_by_platform(
    queue_rows: list[dict], platforms: list[str]
) -> dict[str, int]:
    """Count Content Queue rows in DEPTH_STATUSES per platform."""
    counts: dict[str, int] = {p: 0 for p in platforms}
    for row in queue_rows:
        platform = str(row.get(CQ_PLATFORM, "")).strip().lower()
        status = str(row.get(CQ_STATUS, "")).strip().lower()
        if platform in counts and status in DEPTH_STATUSES:
            counts[platform] += 1
    return counts


def check_queue_depth(
    queue_rows: list[dict],
    platforms: list[str],
    max_depth: int,
) -> tuple[list[str], list[str], dict[str, int]]:
    """Return (active_platforms, skipped_platforms, depth_counts)."""
    counts = count_depth_by_platform(queue_rows, platforms)
    active: list[str] = []
    skipped: list[str] = []
    for p in platforms:
        if counts[p] >= max_depth:
            skipped.append(p)
        else:
            active.append(p)
    return active, skipped, counts


# --- Step 3: Planning window ---

def count_planned_in_window(
    queue_rows: list[dict],
    platform: str,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Count queue rows for `platform` scheduled within [window_start, window_end].

    Uses QUEUED_STATUSES to include posts already drafted/critiqued/etc.
    """
    n = 0
    for row in queue_rows:
        if str(row.get(CQ_PLATFORM, "")).strip().lower() != platform:
            continue
        status = str(row.get(CQ_STATUS, "")).strip().lower()
        if status not in QUEUED_STATUSES:
            continue
        dt = _parse_dt(row.get(CQ_SCHEDULED, ""))
        if dt is None:
            continue
        if window_start <= dt <= window_end:
            n += 1
    return n


def posts_needed_per_platform(
    queue_rows: list[dict],
    platforms: list[str],
    posts_per_week: int,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, int]:
    """For each platform, return how many new posts are needed."""
    needed: dict[str, int] = {}
    for p in platforms:
        already = count_planned_in_window(queue_rows, p, window_start, window_end)
        remainder = max(0, posts_per_week - already)
        needed[p] = remainder
    return needed


# --- Step 4: Objective ratio correction ---

def calculate_objective_correction(
    performance_rows: list[dict],
    target_ratio: dict[str, int],
    lookback_days: int = PERFORMANCE_LOOKBACK_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute actual vs target objective split and correction direction.

    Returns:
        {
            "target": {"brand_awareness": 60, "lead_generation": 40},
            "actual": {"brand_awareness": 70, "lead_generation": 30} | None,
            "actual_counts": {"brand_awareness": 7, "lead_generation": 3},
            "lean_toward": "lead_generation" | "brand_awareness" | "balanced" | None,
            "summary": "human-readable line for the LLM",
        }
    """
    if now is None:
        now = _now_et()
    cutoff = now - timedelta(days=lookback_days)

    counts: Counter = Counter()
    for row in performance_rows:
        obj = str(row.get(PL_OBJECTIVE, "")).strip().lower()
        if obj not in OBJECTIVES:
            continue
        dt = _parse_dt(row.get(PL_POSTED_DATETIME, ""))
        if dt is None:
            continue
        if dt < cutoff:
            continue
        counts[obj] += 1

    target_brand = int(target_ratio.get("brand_awareness", 60))
    target_lead = int(target_ratio.get("lead_generation", 40))
    total_target = target_brand + target_lead
    if total_target == 0:
        target_brand, target_lead, total_target = 60, 40, 100
    target_pct = {
        "brand_awareness": round(100 * target_brand / total_target),
        "lead_generation": round(100 * target_lead / total_target),
    }

    total_actual = sum(counts.values())
    if total_actual == 0:
        return {
            "target": target_pct,
            "actual": None,
            "actual_counts": dict(counts),
            "lean_toward": None,
            "summary": (
                f"Target ratio is {target_pct['brand_awareness']}/"
                f"{target_pct['lead_generation']} brand/lead. "
                f"No recent performance data (last {lookback_days} days) — "
                f"use target ratio as-is with no correction."
            ),
        }

    actual_brand_pct = round(100 * counts.get("brand_awareness", 0) / total_actual)
    actual_lead_pct = round(100 * counts.get("lead_generation", 0) / total_actual)
    actual_pct = {
        "brand_awareness": actual_brand_pct,
        "lead_generation": actual_lead_pct,
    }

    brand_diff = actual_brand_pct - target_pct["brand_awareness"]
    if brand_diff >= 10:
        lean = "lead_generation"
    elif brand_diff <= -10:
        lean = "brand_awareness"
    else:
        lean = "balanced"

    if lean == "balanced":
        guidance = "Stay close to the target ratio for this batch."
    else:
        guidance = f"Lean toward {lean} in this batch to correct the imbalance."

    summary = (
        f"Target ratio is {target_pct['brand_awareness']}/"
        f"{target_pct['lead_generation']} brand/lead. "
        f"Last {lookback_days} days actual is {actual_brand_pct}/{actual_lead_pct} "
        f"brand/lead ({total_actual} posts). {guidance}"
    )
    return {
        "target": target_pct,
        "actual": actual_pct,
        "actual_counts": dict(counts),
        "lean_toward": lean,
        "summary": summary,
    }


# --- Step 5: Catalog filtering ---

def _parse_post_count(raw: Any) -> int:
    try:
        return int(str(raw).strip() or "0")
    except (TypeError, ValueError):
        return 0


def filter_eligible_catalog(
    catalog_rows: list[dict],
    cooldown_days: int,
    now: datetime | None = None,
) -> list[dict]:
    """Return eligible catalog items with a `_recently_posted` annotation.

    Eligibility:
      - status in {active, seasonal, "" (empty)} (case-insensitive)
      - item_name, category, description all non-empty

    Each returned dict is a shallow copy of the row plus a "_recently_posted"
    boolean flag (True if last_posted within cooldown_days of `now`).
    """
    if now is None:
        now = _now_et()
    cutoff = now - timedelta(days=cooldown_days)

    eligible: list[dict] = []
    for row in catalog_rows:
        status = str(row.get(CAT_STATUS, "")).strip().lower()
        if status not in ACTIVE_CATALOG_STATUSES:
            continue
        if not str(row.get(CAT_ITEM_NAME, "")).strip():
            continue
        if not str(row.get(CAT_CATEGORY, "")).strip():
            continue
        if not str(row.get(CAT_DESCRIPTION, "")).strip():
            continue

        last_posted_dt = _parse_dt(row.get(CAT_LAST_POSTED, ""))
        recently = bool(last_posted_dt and last_posted_dt >= cutoff)

        item = dict(row)
        item["_recently_posted"] = recently
        eligible.append(item)
    return eligible


# --- Step 6: Prompt assembly ---

def _compact_catalog_line(item: dict, max_desc: int = CATALOG_DESC_CHARS) -> str:
    """One-line summary of a catalog item for the LLM prompt."""
    desc = str(item.get(CAT_DESCRIPTION, "")).strip()
    if len(desc) > max_desc:
        desc = desc[: max_desc - 1].rstrip() + "…"
    flags = " [RECENT — avoid unless necessary]" if item.get("_recently_posted") else ""
    parts = [
        f"item_id={item.get(CAT_ITEM_ID, '')}",
        f"name={item.get(CAT_ITEM_NAME, '')}",
        f"category={item.get(CAT_CATEGORY, '')}",
        f"model={item.get(CAT_MODEL, '')}",
        f"common_jobs={item.get(CAT_COMMON_JOBS, '')}",
        f"best_for={item.get(CAT_BEST_FOR, '')}",
        f"post_count={item.get(CAT_POST_COUNT, '')}",
        f"last_posted={item.get(CAT_LAST_POSTED, '')}",
        f"image_count={item.get(CAT_IMAGE_COUNT, '')}",
        f"description={desc}",
    ]
    return " | ".join(parts) + flags


def _compact_queue_line(row: dict) -> str:
    parts = [
        f"platform={row.get(CQ_PLATFORM, '')}",
        f"when={row.get(CQ_SCHEDULED, '')}",
        f"content_type={row.get(CQ_CONTENT_TYPE, '')}",
        f"objective={row.get(CQ_OBJECTIVE, '')}",
        f"focus={row.get(CQ_FOCUS_EQUIPMENT, '')}",
        f"media_format={row.get(CQ_MEDIA_FORMAT, '')}",
    ]
    return " | ".join(parts)


def _recent_queue_lines(
    queue_rows: list[dict],
    lookback_days: int,
    now: datetime,
) -> list[str]:
    cutoff = now - timedelta(days=lookback_days)
    lines: list[str] = []
    for row in queue_rows:
        dt = _parse_dt(row.get(CQ_SCHEDULED, ""))
        if dt is None:
            continue
        if dt < cutoff:
            continue
        lines.append(_compact_queue_line(row))
    return lines


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 30].rstrip() + "\n[... truncated ...]"


def assemble_prompt(
    config: Config,
    active_platforms: list[str],
    needed: dict[str, int],
    objective_correction: dict[str, Any],
    eligible_items: list[dict],
    recent_queue_rows: list[dict],
    strategy_guidance: str,
    brand_voice: str,
    cta_skill: str,
    platform_style: str,
    window_start: datetime,
    window_end: datetime,
    min_gap_hours: int,
) -> tuple[str, str]:
    """Return (system_message, user_message) for the LLM call."""
    business_name = config.get("business.name")
    industry = config.get("business.industry")
    service_area = config.get("business.service_area")
    target_customer = config.get("business.target_customer")
    differentiator = config.get("business.differentiator")

    needed_lines = "\n".join(
        f"- {p}: {needed[p]} posts" for p in active_platforms if needed.get(p, 0) > 0
    )
    total_needed = sum(needed.get(p, 0) for p in active_platforms)

    eligible_lines = "\n".join(_compact_catalog_line(i) for i in eligible_items)
    recent_lines_list = _recent_queue_lines(
        recent_queue_rows, RECENT_QUEUE_LOOKBACK_DAYS, _now_et()
    )
    recent_lines = "\n".join(recent_lines_list) if recent_lines_list else "(none)"

    media_format_rules = (
        "Allowed media_format values (pick one per post):\n"
        "  - image2_enhanced: AI-generated photorealistic image, no text overlay. "
        "Default for most posts.\n"
        "  - image2_text_overlay: AI-generated image with bold text overlay added via "
        "OpenAI Image 2. Use for hook-driven posts.\n"
        "  - creatomate_text_overlay: Existing equipment photo from catalog with "
        "Creatomate text overlay template. Use when item has good photos and you want "
        "branded text treatment.\n"
        "  - creatomate_video: Short MP4 with motion + text overlay from Creatomate. "
        "Use sparingly. Max 2 per platform per week.\n"
        "Alternate text-overlay and non-text-overlay posts across the week so the "
        "feed reads varied."
    )

    content_type_lines = "\n".join(f"  - {ct}" for ct in CONTENT_TYPES)
    cta_lines = ", ".join(CTA_TYPES)

    output_schema = (
        "Return your plan as a single JSON array. No prose, no markdown fences. "
        "Each element is an object with EXACTLY these keys:\n"
        "  platform: one of " + ", ".join(active_platforms) + "\n"
        "  scheduled_datetime: ISO 8601 with offset, e.g. \"2026-05-27T09:00:00-04:00\". "
        "Must fall between " + _format_iso_et(window_start) + " and "
        + _format_iso_et(window_end) + ".\n"
        "  objective: brand_awareness or lead_generation\n"
        "  content_type: one of the 9 content types listed above (exact string match)\n"
        "  focus_equipment_id: a catalog item_id from the eligible list, OR empty "
        "string for content types that don't require equipment\n"
        "  angle: 1-2 sentence direction for the Drafter — the hook idea, framing, "
        "or specific aspect to highlight\n"
        "  cta_type: one of " + cta_lines + "\n"
        "  media_format: one of " + ", ".join(MEDIA_FORMATS) + "\n"
        "  draft_notes: additional context for the Drafter (seasonal tie-in, spec to "
        "highlight, experience angle). Keep it concrete."
    )

    system_message = (
        "You are the editorial planner for a local equipment rental company's social "
        "media. Your job is to plan the next 7 days of content across the active "
        "platforms. You make all creative and strategic decisions: content types, "
        "catalog items to feature, angles, CTAs, media formats, and scheduling. "
        "You return your plan as a single JSON array with no prose and no markdown "
        "fences. You never invent catalog items — pick only from the eligible list. "
        "You distribute posts evenly across the planning window."
    )

    cta_summary = _truncate(cta_skill, CTA_MAX_CHARS)
    brand_voice_summary = _truncate(brand_voice, BRAND_VOICE_MAX_CHARS)
    platform_style_summary = _truncate(platform_style, PLATFORM_STYLE_MAX_CHARS)

    user_message = f"""# Business Context

Business: {business_name}
Industry: {industry}
Service area: {service_area}
Target customer: {target_customer}
Differentiator: {differentiator}

# Planning Parameters

Planning window: {_format_iso_et(window_start)} through {_format_iso_et(window_end)} (Eastern Time)
Minimum gap between posts on the same platform: {min_gap_hours} hours
Total new posts to plan: {total_needed}
Posts needed per platform:
{needed_lines}

Distribute posts evenly across the 7-day window. Pick concrete scheduled_datetime values that respect the minimum gap. Avoid scheduling consecutive same-content-type posts on the same platform.

# Objective Ratio

{objective_correction['summary']}

# Strategy Guidance

{strategy_guidance}

# Brand Voice (excerpt)

{brand_voice_summary}

# CTA Rules (excerpt)

{cta_summary}

# Platform Style (excerpt)

{platform_style_summary}

# Available Content Types

Use only these exact strings for content_type:
{content_type_lines}

# Media Format Rules

{media_format_rules}

# Eligible Catalog Items

Pick focus_equipment_id values from this list. Items flagged [RECENT — avoid unless necessary] were posted within the cooldown window — skip them unless there's a strong reason. Items with image_count=0 are still valid (we can generate AI images).

{eligible_lines}

# Recent Content (last {RECENT_QUEUE_LOOKBACK_DAYS} days, for variety reference)

{recent_lines}

# Output

{output_schema}
"""
    return system_message, user_message


# --- Step 7: LLM call ---

def call_llm(
    system_message: str,
    user_message: str,
    model: str = ANTHROPIC_MODEL,
    max_tokens: int = ANTHROPIC_MAX_TOKENS,
) -> str:
    """Call Anthropic Claude and return the raw text response."""
    import anthropic  # imported lazily so non-LLM tests don't require it

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


# --- Step 8: Parse and validate ---

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_llm_output(raw_text: str) -> list[dict]:
    """Extract the JSON array from the LLM response."""
    text = raw_text.strip()
    text = _FENCE_RE.sub("", text).strip()
    # Find the first '[' and last ']' to be resilient to small framing prose.
    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"could not locate JSON array in LLM output: {raw_text!r}")
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError(f"LLM output did not parse to a list (got {type(parsed).__name__})")
    return parsed


def validate_post(
    post: dict,
    eligible_item_ids: set[str],
    active_platforms: set[str],
    window_start: datetime,
    window_end: datetime,
) -> tuple[bool, str]:
    """Return (is_valid, reason). reason is empty when valid."""
    if not isinstance(post, dict):
        return False, f"post is not an object: {type(post).__name__}"

    platform = str(post.get("platform", "")).strip().lower()
    if platform not in active_platforms:
        return False, f"platform {platform!r} not in active set {sorted(active_platforms)}"

    objective = str(post.get("objective", "")).strip().lower()
    if objective not in OBJECTIVES:
        return False, f"objective {objective!r} not in {OBJECTIVES}"

    content_type = str(post.get("content_type", "")).strip()
    if content_type not in CONTENT_TYPES:
        return False, f"content_type {content_type!r} not in defined set"

    focus = str(post.get("focus_equipment_id", "")).strip()
    if focus and focus not in eligible_item_ids:
        return False, f"focus_equipment_id {focus!r} not in eligible catalog"

    cta = str(post.get("cta_type", "")).strip().lower()
    if cta not in CTA_TYPES:
        return False, f"cta_type {cta!r} not in {CTA_TYPES}"

    media_format = str(post.get("media_format", "")).strip()
    if media_format not in MEDIA_FORMATS:
        return False, f"media_format {media_format!r} not in {MEDIA_FORMATS}"

    angle = str(post.get("angle", "")).strip()
    if not angle:
        return False, "angle is empty"

    dt = _parse_dt(post.get("scheduled_datetime", ""))
    if dt is None:
        return False, f"scheduled_datetime {post.get('scheduled_datetime')!r} unparseable"
    if not (window_start <= dt <= window_end):
        return False, (
            f"scheduled_datetime {dt.isoformat()} outside window "
            f"[{window_start.isoformat()}, {window_end.isoformat()}]"
        )

    return True, ""


# --- Step 9: Enforce hard constraints ---

def enforce_timing_gaps(
    posts: list[dict],
    min_gap_hours: int,
) -> list[dict]:
    """Shift overlapping same-platform posts forward to satisfy the gap.

    Mutates the `scheduled_datetime` field of posts as needed. Returns the
    posts (same list, possibly reordered by sort).
    """
    gap = timedelta(hours=min_gap_hours)
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        platform = str(p.get("platform", "")).strip().lower()
        by_platform[platform].append(p)

    for plist in by_platform.values():
        plist.sort(key=lambda r: _parse_dt(r.get("scheduled_datetime", "")) or datetime.max.replace(tzinfo=ET))
        for i in range(1, len(plist)):
            prev_dt = _parse_dt(plist[i - 1].get("scheduled_datetime", ""))
            cur_dt = _parse_dt(plist[i].get("scheduled_datetime", ""))
            if prev_dt is None or cur_dt is None:
                continue
            if cur_dt - prev_dt < gap:
                new_dt = prev_dt + gap
                plist[i]["scheduled_datetime"] = _format_iso_et(new_dt)
    return posts


def enforce_video_cap(
    posts: list[dict],
    max_per_platform: int = MAX_VIDEOS_PER_PLATFORM,
) -> tuple[list[dict], list[str]]:
    """Convert excess creatomate_video posts to creatomate_text_overlay.

    Returns (posts, warnings).
    """
    warnings: list[str] = []
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        platform = str(p.get("platform", "")).strip().lower()
        by_platform[platform].append(p)

    for platform, plist in by_platform.items():
        plist.sort(key=lambda r: _parse_dt(r.get("scheduled_datetime", "")) or datetime.max.replace(tzinfo=ET))
        video_count = 0
        for p in plist:
            if str(p.get("media_format", "")).strip() == "creatomate_video":
                video_count += 1
                if video_count > max_per_platform:
                    p["media_format"] = "creatomate_text_overlay"
                    warnings.append(
                        f"platform {platform}: converted excess creatomate_video to "
                        f"creatomate_text_overlay (scheduled_datetime="
                        f"{p.get('scheduled_datetime')})"
                    )
    return posts, warnings


# --- Step 10: Derived fields ---

def derive_text_overlay(media_format: str) -> str:
    """Return 'TRUE' if the media format embeds text, else 'FALSE'."""
    return "TRUE" if str(media_format).strip() in TEXT_OVERLAY_FORMATS else "FALSE"


def generate_row_id(platform: str, dt: datetime, seq: int) -> str:
    """STR-YYYYMMDD-{FB|IG|GBP}-NN."""
    short = PLATFORM_SHORT.get(platform.strip().lower(), platform.strip().upper()[:3])
    date_part = dt.astimezone(ET).strftime("%Y%m%d")
    return f"STR-{date_part}-{short}-{seq:02d}"


def assign_row_ids(posts: list[dict]) -> list[dict]:
    """Add row_id, status, text_overlay, source_image_id to each post in place."""
    # Sequence counters keyed by (platform, YYYYMMDD).
    seq_by_key: dict[tuple[str, str], int] = defaultdict(int)
    # Sort by scheduled datetime so sequence numbers reflect post order.
    posts_sorted = sorted(
        posts,
        key=lambda r: _parse_dt(r.get("scheduled_datetime", "")) or datetime.max.replace(tzinfo=ET),
    )
    for p in posts_sorted:
        platform = str(p.get("platform", "")).strip().lower()
        dt = _parse_dt(p.get("scheduled_datetime", ""))
        if dt is None:
            continue
        date_part = dt.astimezone(ET).strftime("%Y%m%d")
        key = (platform, date_part)
        seq_by_key[key] += 1
        p[CQ_ROW_ID] = generate_row_id(platform, dt, seq_by_key[key])
        p[CQ_STATUS] = "planned"
        p[CQ_TEXT_OVERLAY] = derive_text_overlay(p.get("media_format", ""))
        p[CQ_SOURCE_IMAGE] = ""
    return posts_sorted


# --- Step 11: Write to Content Queue ---

def write_posts_to_queue(
    posts: list[dict],
    spreadsheet_id: str,
    tab_name: str,
    service: Any,
) -> int:
    """Append each post as a new row. Returns the number of rows written."""
    headers = sheets_helpers._get_headers(spreadsheet_id, tab_name, service=service)
    written = 0
    for post in posts:
        row_dict = {h: "" for h in headers}
        for k, v in post.items():
            if k in row_dict:
                row_dict[k] = "" if v is None else str(v)
        sheets_helpers.append_row(spreadsheet_id, tab_name, row_dict, service=service)
        written += 1
    return written


# --- Helpers shared with asset_indexer ---

def _first_tab(service: Any, spreadsheet_id: str) -> str:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(title))",
    ).execute()
    sheets = meta.get("sheets", [])
    if not sheets:
        raise RuntimeError(f"spreadsheet {spreadsheet_id} has no tabs")
    return sheets[0]["properties"]["title"]


# --- Skill loading with graceful failure ---

def _try_load_skill(name: str, config: Config, default: str = "") -> str:
    try:
        return skill_loader.load_skill(name, config=config)
    except Exception as exc:
        print(
            f"[strategist] WARN: could not load skill {name!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return default


# --- Step 1-12: Orchestrator ---

def run(dry_run: bool = False) -> dict:
    """Execute the Strategist end-to-end. Returns the structured result dict."""
    config: Config = load_config()
    run_ts = _now_et()

    # Load SOP (reference only; non-fatal on failure).
    try:
        skill_loader.load_workflow("strategist", config=config)
    except Exception as exc:
        print(
            f"[strategist] WARN: could not load strategist SOP: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    # --- Load config values ---
    catalog_id = config.get("catalog.spec_sheet_id")
    queue_id = config.get("drive.content_queue_sheet_id")
    performance_id = config.get("drive.performance_log_sheet_id")
    platforms_active: list[str] = list(config.get("platforms.active"))
    max_queue_depth = int(config.get("approval.max_queue_depth", 7))
    posts_per_week = int(config.get("strategy.posts_per_week_per_platform", 7))
    lead_time_hours = int(config.get("strategy.lead_time_hours", 36))
    min_gap_hours = int(config.get("strategy.min_gap_hours", 4))
    no_repeat_days = int(
        config.get(
            "strategy.variety_constraints.no_item_repeat_within_days", 7
        )
    )
    target_ratio = config.get(
        "strategy.objective_ratio",
        {"brand_awareness": 60, "lead_generation": 40},
    )

    if not catalog_id:
        return _error_result(run_ts, "catalog.spec_sheet_id is empty in config", dry_run)
    if not queue_id:
        return _error_result(run_ts, "drive.content_queue_sheet_id is empty in config", dry_run)

    # --- Discover sheet tabs and read all data ---
    try:
        service = sheets_helpers.get_sheets_service()
        catalog_tab = _first_tab(service, catalog_id)
        queue_tab = _first_tab(service, queue_id)
        catalog_rows = sheets_helpers.read_all_rows(catalog_id, catalog_tab, service=service)
        queue_rows = sheets_helpers.read_all_rows(queue_id, queue_tab, service=service)
    except Exception as exc:
        return _error_result(
            run_ts,
            f"failed to read catalog or content queue: {type(exc).__name__}: {exc}",
            dry_run,
        )

    if not catalog_rows:
        return _error_result(run_ts, "Equipment Catalog is empty", dry_run)

    # Performance log is optional.
    performance_rows: list[dict] = []
    if performance_id:
        try:
            perf_tab = _first_tab(service, performance_id)
            performance_rows = sheets_helpers.read_all_rows(
                performance_id, perf_tab, service=service
            )
        except Exception as exc:
            print(
                f"[strategist] WARN: could not read performance log: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    # --- Step 2: queue depth ---
    active_platforms, skipped_platforms, depth_counts = check_queue_depth(
        queue_rows, platforms_active, max_queue_depth
    )

    queue_depth_warnings = [
        f"{p}: queue depth {depth_counts[p]} >= max {max_queue_depth} — skipping"
        for p in skipped_platforms
    ]

    # --- Step 3: planning window + posts needed ---
    window_start = run_ts + timedelta(hours=lead_time_hours)
    window_end = run_ts + timedelta(days=7)
    needed = posts_needed_per_platform(
        queue_rows, active_platforms, posts_per_week, window_start, window_end
    )

    total_needed = sum(needed.values())
    if total_needed == 0:
        return {
            "status": "success",
            "run_timestamp": _format_iso_et(run_ts),
            "planning_window_start": window_start.astimezone(ET).date().isoformat(),
            "planning_window_end": window_end.astimezone(ET).date().isoformat(),
            "posts_planned": 0,
            "by_platform": {p: 0 for p in platforms_active},
            "by_objective": {},
            "by_content_type": {},
            "by_media_format": {},
            "skipped_platforms": skipped_platforms,
            "queue_depth_warnings": queue_depth_warnings,
            "validation_warnings": [],
            "errors": [],
            "dry_run": dry_run,
            "note": "no posts needed — all active platforms are full for the planning window",
        }

    # --- Step 4: objective correction ---
    correction = calculate_objective_correction(
        performance_rows, target_ratio, PERFORMANCE_LOOKBACK_DAYS, run_ts
    )

    # --- Step 5: eligible catalog items ---
    eligible_items = filter_eligible_catalog(catalog_rows, no_repeat_days, run_ts)
    if not eligible_items:
        return _error_result(
            run_ts, "no eligible catalog items after filtering", dry_run
        )
    eligible_item_ids = {str(i.get(CAT_ITEM_ID, "")).strip() for i in eligible_items}

    # --- Load skills (graceful on failure) ---
    strategy_guidance = _try_load_skill("strategy_guidance", config)
    brand_voice = _try_load_skill("brand_voice", config)
    cta_skill = _try_load_skill("cta", config)
    platform_style = _try_load_skill("platform_style", config)

    if not strategy_guidance:
        print(
            "[strategist] WARN: strategy_guidance is empty — proceeding with "
            "minimal steering",
            file=sys.stderr,
        )

    # --- Step 6-7: assemble prompt and call LLM ---
    system_msg, user_msg = assemble_prompt(
        config=config,
        active_platforms=active_platforms,
        needed=needed,
        objective_correction=correction,
        eligible_items=eligible_items,
        recent_queue_rows=queue_rows,
        strategy_guidance=strategy_guidance,
        brand_voice=brand_voice,
        cta_skill=cta_skill,
        platform_style=platform_style,
        window_start=window_start,
        window_end=window_end,
        min_gap_hours=min_gap_hours,
    )

    try:
        raw_output = call_llm(system_msg, user_msg)
    except Exception as exc:
        return _error_result(
            run_ts, f"LLM API call failed: {type(exc).__name__}: {exc}", dry_run
        )

    # --- Step 8: parse + validate ---
    try:
        parsed_posts = parse_llm_output(raw_output)
    except Exception as exc:
        print(f"[strategist] raw LLM output:\n{raw_output}", file=sys.stderr)
        return _error_result(
            run_ts, f"could not parse LLM output: {type(exc).__name__}: {exc}", dry_run
        )

    validation_warnings: list[str] = []
    valid_posts: list[dict] = []
    active_set = set(active_platforms)
    for idx, post in enumerate(parsed_posts):
        ok, reason = validate_post(
            post, eligible_item_ids, active_set, window_start, window_end
        )
        if ok:
            valid_posts.append(post)
        else:
            validation_warnings.append(f"post[{idx}] dropped: {reason}")

    if not valid_posts:
        return {
            "status": "success",
            "run_timestamp": _format_iso_et(run_ts),
            "planning_window_start": window_start.astimezone(ET).date().isoformat(),
            "planning_window_end": window_end.astimezone(ET).date().isoformat(),
            "posts_planned": 0,
            "by_platform": {p: 0 for p in platforms_active},
            "by_objective": {},
            "by_content_type": {},
            "by_media_format": {},
            "skipped_platforms": skipped_platforms,
            "queue_depth_warnings": queue_depth_warnings,
            "validation_warnings": validation_warnings,
            "errors": [],
            "dry_run": dry_run,
            "note": "LLM returned no valid posts after validation",
        }

    # --- Step 9: enforce hard constraints ---
    valid_posts = enforce_timing_gaps(valid_posts, min_gap_hours)
    valid_posts, video_warnings = enforce_video_cap(
        valid_posts, MAX_VIDEOS_PER_PLATFORM
    )
    validation_warnings.extend(video_warnings)

    # --- Step 10: derived fields ---
    valid_posts = assign_row_ids(valid_posts)

    # --- Step 11: write to queue ---
    if not dry_run:
        try:
            write_posts_to_queue(valid_posts, queue_id, queue_tab, service)
        except Exception as exc:
            return _error_result(
                run_ts,
                f"failed to write to Content Queue: {type(exc).__name__}: {exc}",
                dry_run,
            )
    else:
        print(
            f"[strategist] DRY RUN — would write {len(valid_posts)} rows to Content Queue",
            file=sys.stderr,
        )

    # --- Step 12: aggregate result ---
    by_platform: Counter = Counter()
    by_objective: Counter = Counter()
    by_content_type: Counter = Counter()
    by_media_format: Counter = Counter()
    for p in valid_posts:
        by_platform[str(p.get(CQ_PLATFORM, "")).strip().lower()] += 1
        by_objective[str(p.get(CQ_OBJECTIVE, "")).strip().lower()] += 1
        by_content_type[str(p.get(CQ_CONTENT_TYPE, "")).strip()] += 1
        by_media_format[str(p.get(CQ_MEDIA_FORMAT, "")).strip()] += 1

    return {
        "status": "success",
        "run_timestamp": _format_iso_et(run_ts),
        "planning_window_start": window_start.astimezone(ET).date().isoformat(),
        "planning_window_end": window_end.astimezone(ET).date().isoformat(),
        "posts_planned": len(valid_posts),
        "by_platform": dict(by_platform),
        "by_objective": dict(by_objective),
        "by_content_type": dict(by_content_type),
        "by_media_format": dict(by_media_format),
        "skipped_platforms": skipped_platforms,
        "queue_depth_warnings": queue_depth_warnings,
        "validation_warnings": validation_warnings,
        "errors": [],
        "dry_run": dry_run,
        "posts": valid_posts if dry_run else [p[CQ_ROW_ID] for p in valid_posts],
    }


def _error_result(run_ts: datetime, message: str, dry_run: bool) -> dict:
    print(f"[strategist] ERROR: {message}", file=sys.stderr)
    return {
        "status": "error",
        "run_timestamp": _format_iso_et(run_ts),
        "posts_planned": 0,
        "errors": [message],
        "dry_run": dry_run,
    }


# --- CLI ---

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strategist agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load data, call LLM, validate output, but do not write to sheets.",
    )
    args = parser.parse_args(argv)

    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
