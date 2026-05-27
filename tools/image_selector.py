"""Image selector — pick the best source image for a social media post.

Implements rotation-aware selection: reads the Image Metadata sheet, filters
images that match a catalog item's category + model, removes images used in
the last 14 days (per Content Queue history), and ranks the remaining
candidates by quality > context > recency.

This module is the home of the shared image-ranking primitives. Asset
Indexer imports them from here to avoid duplication.

Public:
    select_source_image(equipment_id, catalog_row, config, service=None) -> dict
    select_best_image(image_rows) -> dict
    group_metadata(meta_rows) -> dict[(category, model), list[dict]]

This tool never raises — all failure modes return a valid result dict so
the Drafter agent can degrade gracefully.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from tools import sheets_helpers
from tools.config_loader import Config


# --- Image Metadata sheet columns ---
META_IMAGE_ID = "Image_ID"
META_CATEGORY = "Equipment Category"
META_MODEL = "Equipment Model"
META_TIMESTAMP = "Timestamp"
META_PHOTO_QUALITY = "Photo Quality"
META_PHOTO_CONTEXT = "Photo Context"
META_JOB_TYPE = "Job Type"
META_NOTES = "Notes"

# --- Content Queue columns (for rotation history) ---
CQ_FOCUS_EQUIPMENT = "focus_equipment_id"
CQ_STATUS = "status"
CQ_SCHEDULED = "scheduled_datetime"
CQ_SOURCE_IMAGE = "source_image_id"

# --- Catalog columns (read on the caller-supplied row) ---
CAT_CATEGORY = "category"
CAT_MODEL = "model"
CAT_PRIMARY_IMAGE_ID = "primary_image_id"

# Rolling rotation window in days.
ROTATION_DAYS = 14

# Ranking weights — higher == better. Quality dominates context.
_QUALITY_RANK = {
    "great shot": 2,
    "decent": 1,
}
_CONTEXT_RANK = {
    "equipment working": 3,
    "finished result": 2,
    "before (job site before work)": 2,
    "equipment staged / parked": 1,
    "on the trailer": 0,
}


# --- Shared primitives ---

def _norm(value: Any) -> str:
    """Lowercase + strip for case-insensitive match keys."""
    return ("" if value is None else str(value)).strip().lower()


def _parse_timestamp(raw: str) -> float:
    """Best-effort timestamp parse for tie-breaking. Unparseable -> -inf."""
    if not raw:
        return float("-inf")
    raw = raw.strip()
    formats = (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return float("-inf")


def _image_rank(row: dict) -> tuple[int, int, float]:
    """Sort key for an image row. Higher tuple == better."""
    quality = _QUALITY_RANK.get(_norm(row.get(META_PHOTO_QUALITY)), 0)
    context = _CONTEXT_RANK.get(_norm(row.get(META_PHOTO_CONTEXT)), 0)
    recency = _parse_timestamp(row.get(META_TIMESTAMP, ""))
    return (quality, context, recency)


def select_best_image(image_rows: list[dict]) -> dict:
    """Return the highest-ranked image row from a non-empty list."""
    if not image_rows:
        raise ValueError("select_best_image called with empty list")
    return max(image_rows, key=_image_rank)


def group_metadata(meta_rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group metadata rows by (lowercased category, lowercased model).

    Rows missing category, model, or Image_ID are skipped — they cannot match.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in meta_rows:
        cat = _norm(row.get(META_CATEGORY))
        model = _norm(row.get(META_MODEL))
        if not cat or not model:
            continue
        if not str(row.get(META_IMAGE_ID, "")).strip():
            continue
        groups[(cat, model)].append(row)
    return dict(groups)


def _first_tab(service: Any, spreadsheet_id: str) -> str:
    """Return the title of the first tab in the given spreadsheet."""
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(title))",
    ).execute()
    sheets = meta.get("sheets", [])
    if not sheets:
        raise RuntimeError(f"spreadsheet {spreadsheet_id} has no tabs")
    return sheets[0]["properties"]["title"]


# --- Selector-specific helpers ---

def _parse_iso_dt(raw: str) -> datetime | None:
    """Parse ISO 8601 with optional offset. Naive values assumed UTC."""
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
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_images_for_equipment(
    catalog_row: dict,
    image_metadata_sheet_id: str,
    service: Any,
) -> list[dict]:
    """Return image metadata rows whose category+model match the catalog row."""
    target_cat = _norm(catalog_row.get(CAT_CATEGORY))
    target_model = _norm(catalog_row.get(CAT_MODEL))
    if not target_cat or not target_model:
        return []

    tab = _first_tab(service, image_metadata_sheet_id)
    meta_rows = sheets_helpers.read_all_rows(
        image_metadata_sheet_id, tab, service=service,
    )

    matches: list[dict] = []
    for r in meta_rows:
        if _norm(r.get(META_CATEGORY)) != target_cat:
            continue
        if _norm(r.get(META_MODEL)) != target_model:
            continue
        if not str(r.get(META_IMAGE_ID, "")).strip():
            continue
        matches.append(r)
    return matches


def get_recently_used_image_ids(
    equipment_id: str,
    content_queue_sheet_id: str,
    service: Any,
    now: datetime | None = None,
    window_days: int = ROTATION_DAYS,
) -> set[str]:
    """Return source_image_id values used for `equipment_id` in the last
    `window_days`. Considers any Content Queue row with non-empty status
    and a parseable scheduled_datetime within the window.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    tab = _first_tab(service, content_queue_sheet_id)
    queue_rows = sheets_helpers.read_all_rows(
        content_queue_sheet_id, tab, service=service,
    )

    used: set[str] = set()
    target = str(equipment_id).strip()
    for r in queue_rows:
        if str(r.get(CQ_FOCUS_EQUIPMENT, "")).strip() != target:
            continue
        if not str(r.get(CQ_STATUS, "")).strip():
            continue
        dt = _parse_iso_dt(r.get(CQ_SCHEDULED, ""))
        if dt is None:
            continue
        if dt < cutoff:
            continue
        img_id = str(r.get(CQ_SOURCE_IMAGE, "")).strip()
        if img_id:
            used.add(img_id)
    return used


def _fallback(primary_image_id: str, why: str) -> dict:
    """Build a fallback result dict for Step 5."""
    pid = (primary_image_id or "").strip()
    if pid:
        return {
            "image_id": pid,
            "selection_reason": f"fallback to primary_image_id ({why})",
            "total_available": 0,
            "fallback": True,
        }
    return {
        "image_id": "",
        "selection_reason": f"no images available ({why})",
        "total_available": 0,
        "fallback": True,
    }


# --- Public entry point ---

def select_source_image(
    equipment_id: str,
    catalog_row: dict,
    config: Config,
    service: Any = None,
) -> dict:
    """Select the best source image for the given equipment item.

    Returns a dict:
        image_id: Drive file ID (empty if none available)
        selection_reason: human-readable explanation
        total_available: count of metadata rows matching this item
        fallback: True if we returned primary_image_id or an empty string
    """
    image_meta_id = (config.get("catalog.image_metadata_sheet_id", "") or "").strip()
    queue_id = (config.get("drive.content_queue_sheet_id", "") or "").strip()
    primary_image_id = str(catalog_row.get(CAT_PRIMARY_IMAGE_ID, "")).strip()

    # Step 5 short-circuit: no Image Metadata configured.
    if not image_meta_id:
        return _fallback(primary_image_id, "no metadata rows")

    if service is None:
        try:
            service = sheets_helpers.get_sheets_service()
        except Exception as exc:
            print(
                f"[image_selector] WARN: could not build Sheets service: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return _fallback(primary_image_id, "sheets service unavailable")

    # Step 1: find candidates matching this catalog row.
    try:
        candidates = get_images_for_equipment(catalog_row, image_meta_id, service)
    except Exception as exc:
        print(
            f"[image_selector] WARN: Image Metadata sheet read failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return _fallback(primary_image_id, "image_metadata read failed")

    if not candidates:
        return _fallback(primary_image_id, "no metadata rows")

    total_available = len(candidates)

    # Step 2: gather recent usage from Content Queue.
    used_ids: set[str] = set()
    if queue_id:
        try:
            used_ids = get_recently_used_image_ids(
                equipment_id, queue_id, service,
            )
        except Exception as exc:
            print(
                f"[image_selector] WARN: Content Queue read failed; "
                f"skipping rotation: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            used_ids = set()

    # Step 3: filter out recently used. Reset if filter empties the list.
    filter_reset = False
    unused = [
        r for r in candidates
        if str(r.get(META_IMAGE_ID, "")).strip() not in used_ids
    ]
    if not unused:
        print(
            f"[image_selector] all {total_available} images for "
            f"{equipment_id} used in last {ROTATION_DAYS} days; filter reset",
            file=sys.stderr,
        )
        unused = candidates
        filter_reset = True

    # Step 4: rank and select.
    best = select_best_image(unused)
    best_id = str(best.get(META_IMAGE_ID, "")).strip()

    if filter_reset:
        reason = "highest-ranked image (all recently used, filter reset)"
    elif total_available == 1:
        reason = "only image available"
    else:
        reason = "highest-ranked unused image"

    return {
        "image_id": best_id,
        "selection_reason": reason,
        "total_available": total_available,
        "fallback": False,
    }


def _empty_candidates_result(
    primary_image_id: str,
    error: str,
) -> dict:
    """Build a candidates result dict with no candidates."""
    return {
        "candidates": [],
        "total_available": 0,
        "fallback_image_id": (primary_image_id or "").strip(),
        "error": error,
    }


def select_top_candidates(
    equipment_id: str,
    catalog_row: dict,
    config: Config,
    service: Any = None,
    max_candidates: int = 5,
) -> dict:
    """Return the top N image candidates with metadata for LLM-assisted selection.

    Uses the same filtering and ranking pipeline as select_source_image:
    1. Read Image Metadata sheet, filter by category+model
    2. Remove recently-used images (rotation window)
    3. Rank by quality > context > recency
    4. Return the top `max_candidates` (or all if fewer exist)

    Returns a dict:
        candidates: list of dicts, each with:
            image_id: Drive file ID
            photo_context: str (e.g. "Equipment Working")
            job_type: str (e.g. "Land Clearing")
            notes: str (free text, may be empty)
            rank: int (1 = highest ranked by deterministic scoring)
        total_available: int
        fallback_image_id: str (primary_image_id from catalog, for fallback)
        error: str (empty if no error)
    """
    image_meta_id = (config.get("catalog.image_metadata_sheet_id", "") or "").strip()
    queue_id = (config.get("drive.content_queue_sheet_id", "") or "").strip()
    primary_image_id = str(catalog_row.get(CAT_PRIMARY_IMAGE_ID, "")).strip()

    if not image_meta_id:
        return _empty_candidates_result(primary_image_id, "no image metadata sheet configured")

    if service is None:
        try:
            service = sheets_helpers.get_sheets_service()
        except Exception as exc:
            print(
                f"[image_selector] WARN: could not build Sheets service: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return _empty_candidates_result(primary_image_id, "sheets service unavailable")

    try:
        candidates = get_images_for_equipment(catalog_row, image_meta_id, service)
    except Exception as exc:
        print(
            f"[image_selector] WARN: Image Metadata sheet read failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return _empty_candidates_result(primary_image_id, "image_metadata read failed")

    if not candidates:
        return _empty_candidates_result(primary_image_id, "no metadata rows")

    total_available = len(candidates)

    used_ids: set[str] = set()
    if queue_id:
        try:
            used_ids = get_recently_used_image_ids(
                equipment_id, queue_id, service,
            )
        except Exception as exc:
            print(
                f"[image_selector] WARN: Content Queue read failed; "
                f"skipping rotation: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            used_ids = set()

    unused = [
        r for r in candidates
        if str(r.get(META_IMAGE_ID, "")).strip() not in used_ids
    ]
    if not unused:
        print(
            f"[image_selector] all {total_available} images for "
            f"{equipment_id} used in last {ROTATION_DAYS} days; filter reset",
            file=sys.stderr,
        )
        unused = candidates

    ranked = sorted(unused, key=_image_rank, reverse=True)
    top = ranked[: max(1, max_candidates)]

    out_candidates: list[dict] = []
    for idx, row in enumerate(top, start=1):
        out_candidates.append({
            "image_id": str(row.get(META_IMAGE_ID, "")).strip(),
            "photo_context": str(row.get(META_PHOTO_CONTEXT, "") or "").strip(),
            "job_type": str(row.get(META_JOB_TYPE, "") or "").strip(),
            "notes": str(row.get(META_NOTES, "") or "").strip(),
            "rank": idx,
        })

    return {
        "candidates": out_candidates,
        "total_available": total_available,
        "fallback_image_id": primary_image_id,
        "error": "",
    }
