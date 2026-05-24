"""Skill and workflow loader with placeholder injection.

Loads skill files and workflow SOPs from Google Drive by file ID (looked up
in business config under skills.* and workflows.*), resolves {{TOKEN}}
placeholders against business config values, and returns the resolved text.

Cache: module-level dict, keyed as "skill:<name>" or "workflow:<name>".
No TTL — cache clears when the process restarts (or via clear_cache()).
"""

from __future__ import annotations

import re
import sys
from typing import Any

from tools import drive_helpers
from tools.config_loader import Config, load_config


PLACEHOLDER_MAP: dict[str, str] = {
    # Business Identity
    "BUSINESS_NAME": "business.name",
    "BUSINESS_SHORT_NAME": "business.short_name",
    "INDUSTRY": "business.industry",
    "SERVICE_AREA": "business.service_area",
    "SERVICE_AREA_SHORT": "business.service_area_short",
    "TARGET_CUSTOMER": "business.target_customer",
    "DIFFERENTIATOR": "business.differentiator",

    # Owner
    "OWNER_NAME": "owner.name",
    "OWNER_ROLE": "owner.role",
    "OWNER_EXPERIENCE": "owner.experience_summary",

    # Contact
    "PHONE": "contact.phone",
    "WEBSITE": "contact.website",
    "BOOKING_URL": "contact.booking_url",
    "EMAIL": "contact.email",
    "GOOGLE_MAPS_URL": "contact.google_maps_url",

    # Strategy
    "POSTS_PER_WEEK_PER_PLATFORM": "strategy.posts_per_week_per_platform",
    "LEAD_TIME_HOURS": "strategy.lead_time_hours",
    "MIN_GAP_HOURS": "strategy.min_gap_hours",
    "PRICING_POLICY": "strategy.pricing_in_posts",

    # Approval / Slack
    "MAX_QUEUE_DEPTH": "approval.max_queue_depth",
    "SLACK_APPROVALS_CHANNEL": "approval.slack_channel",
    "SLACK_ERROR_CHANNEL": "approval.error_channel",

    # Brand Visuals
    "BRAND_FEEL": "brand_visuals.feel",
    "VISUAL_CONTEXT": "brand_visuals.visual_context",
    "TYPOGRAPHY_STYLE": "brand_visuals.typography_style",

    # Catalog
    "PRIMARY_SUBJECT": "catalog.primary_subject",
}


_PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")

_CACHE: dict[str, str] = {}


def _inject_placeholders(text: str, config: Config) -> str:
    """Replace all known {{TOKEN}} occurrences in text with config values.

    Unknown tokens are left in place and logged once (per token) to stderr.
    """
    warned: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in PLACEHOLDER_MAP:
            value = config.get(PLACEHOLDER_MAP[token])
            return str(value)
        if token not in warned:
            print(
                f"[skill_loader] Unknown placeholder: {{{{{token}}}}} "
                f"— leaving in place",
                file=sys.stderr,
            )
            warned.add(token)
        return match.group(0)

    return _PLACEHOLDER_PATTERN.sub(_replace, text)


def _load_resolved(
    cache_key: str,
    file_id: str,
    config: Config,
) -> str:
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    raw_text = drive_helpers.read_file_content(file_id)
    resolved = _inject_placeholders(raw_text, config)
    _CACHE[cache_key] = resolved
    return resolved


def load_skill(skill_name: str, config: Config | None = None) -> str:
    if config is None:
        config = load_config()

    file_id = config.get(f"skills.{skill_name}")
    return _load_resolved(f"skill:{skill_name}", file_id, config)


def load_workflow(workflow_name: str, config: Config | None = None) -> str:
    if config is None:
        config = load_config()

    file_id = config.get(f"workflows.{workflow_name}")
    return _load_resolved(f"workflow:{workflow_name}", file_id, config)


def clear_cache() -> None:
    _CACHE.clear()


def _cache_snapshot() -> dict[str, str]:
    """Return a shallow copy of the cache. Test-only helper."""
    return dict(_CACHE)
