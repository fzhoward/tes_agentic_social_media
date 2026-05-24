"""Asset Indexer agent — sync Equipment Catalog with Drive images.

Reads the Image Metadata sheet and Equipment Catalog, matches images to
catalog items by category + model, updates primary_image_id and image_count,
creates new catalog rows for unmatched equipment, and posts a summary to Slack.

No LLM calls. Pure deterministic matching logic.

Usage:
    python -m agents.asset_indexer [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

from tools import sheets_helpers, skill_loader, slack_helpers
from tools.config_loader import Config, load_config


# --- Metadata column names (Image Metadata sheet) ---
META_IMAGE_ID = "Image_ID"
META_CATEGORY = "Equipment Category"
META_MODEL = "Equipment Model"
META_TIMESTAMP = "Timestamp"
META_PHOTO_QUALITY = "Photo Quality"
META_PHOTO_CONTEXT = "Photo Context"

# --- Catalog column names ---
CAT_ITEM_ID = "item_id"
CAT_ITEM_NAME = "item_name"
CAT_CATEGORY = "category"
CAT_MODEL = "model"
CAT_STATUS = "status"
CAT_PRIMARY_IMAGE_ID = "primary_image_id"
CAT_IMAGE_COUNT = "image_count"
CAT_TAGS = "tags"
CAT_POST_COUNT = "post_count"

# Ranking weights for "best image" selection.
# Higher rank == better. Quality dominates context.
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
    """Sort key for an image row. Higher == better."""
    quality = _QUALITY_RANK.get(_norm(row.get(META_PHOTO_QUALITY)), 0)
    context = _CONTEXT_RANK.get(_norm(row.get(META_PHOTO_CONTEXT)), 0)
    recency = _parse_timestamp(row.get(META_TIMESTAMP, ""))
    return (quality, context, recency)


def select_best_image(image_rows: list[dict]) -> dict:
    """Return the best image row from a non-empty group."""
    if not image_rows:
        raise ValueError("select_best_image called with empty list")
    return max(image_rows, key=_image_rank)


def group_metadata(meta_rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group metadata rows by (lowercased category, lowercased model).

    Rows missing either category or model are skipped — they cannot match.
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


def index_catalog(
    cat_rows: list[tuple[int, dict]],
) -> dict[tuple[str, str], list[tuple[int, dict]]]:
    """Index catalog rows by (lowercased category, lowercased model)."""
    idx: dict[tuple[str, str], list[tuple[int, dict]]] = defaultdict(list)
    for row_num, row in cat_rows:
        cat = _norm(row.get(CAT_CATEGORY))
        model = _norm(row.get(CAT_MODEL))
        if not cat or not model:
            continue
        idx[(cat, model)].append((row_num, row))
    return dict(idx)


def next_item_id(existing_ids: list[str], prefix: str = "TES-") -> str:
    """Generate the next sequential item ID after the highest existing one."""
    max_n = 0
    for raw in existing_ids:
        s = str(raw).strip()
        if not s.startswith(prefix):
            continue
        tail = s[len(prefix):]
        if tail.isdigit():
            n = int(tail)
            if n > max_n:
                max_n = n
    return f"{prefix}{max_n + 1:03d}"


def _build_new_row(
    category: str,
    model: str,
    primary_image_id: str,
    image_count: int,
    item_id: str,
    catalog_headers: list[str],
) -> dict:
    """Construct a new catalog row dict. Empty string for unset fields."""
    row = {h: "" for h in catalog_headers}
    row[CAT_ITEM_ID] = item_id
    row[CAT_ITEM_NAME] = f"{category} - {model}"
    row[CAT_CATEGORY] = category
    row[CAT_MODEL] = model
    row[CAT_STATUS] = "active"
    row[CAT_PRIMARY_IMAGE_ID] = primary_image_id
    row[CAT_IMAGE_COUNT] = str(image_count)
    row[CAT_TAGS] = f"{category.lower()}, {model.lower()}"
    if CAT_POST_COUNT in row:
        row[CAT_POST_COUNT] = "0"
    return row


def _format_slack_summary(
    matched_catalog_items: int,
    total_catalog_items: int,
    images_indexed: int,
    updated_ids: list[str],
    new_items: list[tuple[str, str]],
    unmatched_groups: list[tuple[str, str, int]],
) -> str:
    """Build the plain-text Slack summary message."""
    lines = ["Asset Indexer — Run Complete"]
    lines.append(f"• Catalog items matched: {matched_catalog_items} of {total_catalog_items}")
    lines.append(f"• Images indexed: {images_indexed}")
    if updated_ids:
        lines.append(f"• Items updated: {len(updated_ids)} ({', '.join(updated_ids)})")
    else:
        lines.append("• Items updated: 0")
    if new_items:
        names = ", ".join(f"{iid} \"{name}\"" for iid, name in new_items)
        lines.append(f"• New items created: {len(new_items)} ({names})")
    else:
        lines.append("• New items created: 0")
    if unmatched_groups:
        details = "; ".join(f"{cat} / {model} ({n} img)" for cat, model, n in unmatched_groups)
        lines.append(f"• Unmatched image groups: {len(unmatched_groups)} ({details})")
    else:
        lines.append("• Unmatched image groups: 0")

    if new_items:
        lines.append("")
        lines.append("⚠️ New catalog items need manual entry:")
        for iid, name in new_items:
            lines.append(
                f"• {iid} \"{name}\" — needs: description, weight, specs, common_jobs, best_for"
            )

    return "\n".join(lines)


def run(dry_run: bool = False) -> dict:
    """Execute the Asset Indexer end-to-end. Returns the structured result dict."""
    config: Config = load_config()

    # SOP is reference documentation, not executable code. Loading it confirms
    # the file is reachable and the loader is wired up correctly.
    try:
        skill_loader.load_workflow("asset_indexer", config=config)
        print("[asset_indexer] loaded asset_indexer SOP from Drive", file=sys.stderr)
    except Exception as exc:
        print(
            f"[asset_indexer] WARN: could not load asset_indexer SOP: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    image_meta_id = config.get("catalog.image_metadata_sheet_id")
    catalog_id = config.get("catalog.spec_sheet_id")
    health_channel = config.get("health.slack_channel")

    if not image_meta_id:
        raise RuntimeError("catalog.image_metadata_sheet_id is empty in config")
    if not catalog_id:
        raise RuntimeError("catalog.spec_sheet_id is empty in config")

    sheets_service = sheets_helpers.get_sheets_service()

    # Tab discovery — first tab of each sheet.
    meta_tab = _first_tab(sheets_service, image_meta_id)
    catalog_tab = _first_tab(sheets_service, catalog_id)

    meta_rows = sheets_helpers.read_all_rows(image_meta_id, meta_tab, service=sheets_service)
    catalog_rows_raw = sheets_helpers.read_all_rows(catalog_id, catalog_tab, service=sheets_service)
    catalog_headers = sheets_helpers._get_headers(catalog_id, catalog_tab, service=sheets_service)

    # Pair each catalog row with its 1-indexed sheet row number (row 2 = first data row).
    catalog_rows: list[tuple[int, dict]] = [
        (i + 2, row) for i, row in enumerate(catalog_rows_raw)
    ]

    meta_groups = group_metadata(meta_rows)
    catalog_index = index_catalog(catalog_rows)

    # Build update list and new items list.
    updates: list[tuple[int, dict]] = []
    updated_ids: list[str] = []
    new_items: list[tuple[str, str]] = []
    new_rows_to_append: list[dict] = []
    unmatched_groups: list[tuple[str, str, int]] = []
    matched_catalog_items = 0

    # Track in-memory item_ids so we don't collide when appending multiple new items.
    existing_ids = [r.get(CAT_ITEM_ID, "") for _, r in catalog_rows]

    # Sort groups deterministically so output is stable.
    for key in sorted(meta_groups.keys()):
        images = meta_groups[key]
        category_norm, model_norm = key

        best = select_best_image(images)
        primary_image_id = str(best.get(META_IMAGE_ID, "")).strip()
        image_count = len(images)

        matches = catalog_index.get(key, [])
        if matches:
            for row_num, row in matches:
                updates.append((row_num, {
                    CAT_PRIMARY_IMAGE_ID: primary_image_id,
                    CAT_IMAGE_COUNT: str(image_count),
                }))
                updated_ids.append(row.get(CAT_ITEM_ID, ""))
                matched_catalog_items += 1
        else:
            # Recover the original (non-lowercased) category/model from the
            # first image row so the new catalog row reads naturally.
            display_category = str(best.get(META_CATEGORY, "")).strip()
            display_model = str(best.get(META_MODEL, "")).strip()
            new_id = next_item_id(existing_ids)
            existing_ids.append(new_id)

            new_row = _build_new_row(
                category=display_category,
                model=display_model,
                primary_image_id=primary_image_id,
                image_count=image_count,
                item_id=new_id,
                catalog_headers=catalog_headers,
            )
            new_rows_to_append.append(new_row)
            new_items.append((new_id, new_row[CAT_ITEM_NAME]))
            unmatched_groups.append((display_category, display_model, image_count))

    total_images_indexed = sum(len(rows) for rows in meta_groups.values())

    if not dry_run:
        if updates:
            sheets_helpers.batch_update_cells(
                catalog_id, catalog_tab, updates, service=sheets_service
            )
        for new_row in new_rows_to_append:
            sheets_helpers.append_row(
                catalog_id, catalog_tab, new_row, service=sheets_service
            )

        # Slack summary — always posted.
        summary = _format_slack_summary(
            matched_catalog_items=matched_catalog_items,
            total_catalog_items=len(catalog_rows),
            images_indexed=total_images_indexed,
            updated_ids=updated_ids,
            new_items=new_items,
            unmatched_groups=unmatched_groups,
        )
        try:
            slack_helpers.post_message(health_channel, summary)
        except Exception as exc:
            print(
                f"[asset_indexer] WARN: Slack post failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    else:
        # Dry-run: print what would have happened, do not write.
        print("[asset_indexer] DRY RUN — no writes performed", file=sys.stderr)
        print(
            f"[asset_indexer] would update {len(updates)} catalog rows "
            f"({len(updated_ids)} item_ids)",
            file=sys.stderr,
        )
        print(
            f"[asset_indexer] would append {len(new_rows_to_append)} new catalog rows",
            file=sys.stderr,
        )

    return {
        "status": "success",
        "matched_items": matched_catalog_items,
        "total_catalog_items": len(catalog_rows),
        "total_images": total_images_indexed,
        "items_updated": updated_ids,
        "new_items_created": [iid for iid, _ in new_items],
        "unmatched_groups": [
            {"category": cat, "model": model, "image_count": n}
            for cat, model, n in unmatched_groups
        ],
        "dry_run": dry_run,
    }


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Asset Indexer agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and match, but do not write to sheets or post to Slack.",
    )
    args = parser.parse_args(argv)

    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
