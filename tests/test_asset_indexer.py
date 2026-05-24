"""Tests for agents/asset_indexer.py.

Runs against live Image Metadata sheet, live Equipment Catalog, and live Slack.
Prerequisite: Run `python tools/google_auth.py` first to generate the token.

Test 10 writes to the live catalog sheet and posts to Slack. Writes are
intentional and correct — primary_image_id and image_count values reflect
real catalog state and should persist.

Run from the project root:
    python -m tests.test_asset_indexer
"""

from __future__ import annotations

import io
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from agents import asset_indexer  # noqa: E402
from tools import sheets_helpers  # noqa: E402
from tools.config_loader import load_config  # noqa: E402


_FAILURES: list[tuple[str, str]] = []
_PASSED = 0


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  PASS  {name}")
    else:
        _FAILURES.append((name, detail))
        print(f"  FAIL  {name} — {detail}")


def run_tests() -> int:
    print(f"Loading config from: {CONFIG_FILE}")
    config = load_config()

    # 1. Config has required keys.
    image_meta_id = config.get("catalog.image_metadata_sheet_id", "")
    catalog_id = config.get("catalog.spec_sheet_id", "")
    _check(
        "1. config loads with catalog.spec_sheet_id and catalog.image_metadata_sheet_id",
        isinstance(catalog_id, str) and catalog_id != ""
        and isinstance(image_meta_id, str) and image_meta_id != "",
        f"spec_sheet_id={catalog_id!r}, image_metadata_sheet_id={image_meta_id!r}",
    )

    # Service + tab discovery — needed for the remaining tests.
    try:
        service = sheets_helpers.get_sheets_service()
        meta_tab = asset_indexer._first_tab(service, image_meta_id)
        catalog_tab = asset_indexer._first_tab(service, catalog_id)
        print(f"  (meta tab: {meta_tab!r}, catalog tab: {catalog_tab!r})")
    except Exception as exc:
        _check(
            "1b. service + tab discovery",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        print("\nCannot continue without auth.")
        return 1

    # 2. Read Image Metadata sheet.
    meta_rows: list[dict] = []
    try:
        meta_rows = sheets_helpers.read_all_rows(image_meta_id, meta_tab, service=service)
        first = meta_rows[0] if meta_rows else {}
        required_keys = {"Image_ID", "Equipment Category", "Equipment Model"}
        missing = required_keys - set(first.keys())
        _check(
            "2. read_all_rows on Image Metadata — non-empty, has Image_ID/Category/Model",
            len(meta_rows) > 0 and not missing,
            f"rows={len(meta_rows)}, missing_keys={missing}",
        )
    except Exception as exc:
        _check("2. read_all_rows on Image Metadata", False, f"{type(exc).__name__}: {exc}")

    # 3. Read Equipment Catalog.
    catalog_rows: list[dict] = []
    try:
        catalog_rows = sheets_helpers.read_all_rows(catalog_id, catalog_tab, service=service)
        first = catalog_rows[0] if catalog_rows else {}
        required_keys = {"item_id", "category", "model"}
        missing = required_keys - set(first.keys())
        _check(
            "3. read_all_rows on Equipment Catalog — non-empty, has item_id/category/model",
            len(catalog_rows) > 0 and not missing,
            f"rows={len(catalog_rows)}, missing_keys={missing}",
        )
    except Exception as exc:
        _check("3. read_all_rows on Equipment Catalog", False, f"{type(exc).__name__}: {exc}")

    # 4. Grouping logic — at least one group has multiple images.
    groups: dict[tuple[str, str], list[dict]] = {}
    try:
        groups = asset_indexer.group_metadata(meta_rows)
        multi_image_groups = [k for k, rows in groups.items() if len(rows) > 1]
        _check(
            "4. group_metadata — at least one group has multiple images",
            len(multi_image_groups) > 0,
            f"groups={len(groups)}, multi_image_groups={len(multi_image_groups)}",
        )
    except Exception as exc:
        _check("4. group_metadata", False, f"{type(exc).__name__}: {exc}")

    # 5. Matching logic — "Compact Track Loader" + "325G" matches >=2 catalog rows.
    try:
        target_key = ("compact track loader", "325g")
        catalog_pairs = [(i + 2, r) for i, r in enumerate(catalog_rows)]
        idx = asset_indexer.index_catalog(catalog_pairs)
        matches = idx.get(target_key, [])
        ids = sorted([r.get("item_id", "") for _, r in matches])
        _check(
            "5. matching — ('Compact Track Loader','325G') matches TES-004 and TES-005",
            len(matches) >= 2 and "TES-004" in ids and "TES-005" in ids,
            f"matched ids={ids}",
        )
    except Exception as exc:
        _check("5. matching logic", False, f"{type(exc).__name__}: {exc}")

    # 6. Best image selection — synthetic mixed-quality group prefers "Great shot".
    try:
        group = [
            {
                "Image_ID": "decent_id",
                "Photo Quality": "Decent",
                "Photo Context": "Equipment Working",
                "Timestamp": "5/01/2026 12:00:00",
            },
            {
                "Image_ID": "great_id",
                "Photo Quality": "Great shot",
                "Photo Context": "Equipment staged / parked",
                "Timestamp": "4/01/2026 12:00:00",
            },
        ]
        best = asset_indexer.select_best_image(group)
        _check(
            "6. select_best_image — prefers 'Great shot' over 'Decent' even with worse context",
            best.get("Image_ID") == "great_id",
            f"selected Image_ID={best.get('Image_ID')!r}",
        )
    except Exception as exc:
        _check("6. select_best_image", False, f"{type(exc).__name__}: {exc}")

    # 7. primary_image_id format — looks like a Drive file ID (no URL prefix, non-empty).
    try:
        if not groups:
            raise RuntimeError("no metadata groups available")
        sample_key = next(iter(groups))
        sample_best = asset_indexer.select_best_image(groups[sample_key])
        pid = str(sample_best.get("Image_ID", "")).strip()
        looks_like_id = (
            len(pid) > 10
            and "/" not in pid
            and "://" not in pid
            and " " not in pid
        )
        _check(
            "7. primary_image_id format — non-empty Drive ID, no URL prefix",
            looks_like_id,
            f"primary_image_id={pid!r}",
        )
    except Exception as exc:
        _check("7. primary_image_id format", False, f"{type(exc).__name__}: {exc}")

    # 8. Dry-run — completes without error, does NOT write to sheets.
    try:
        # Snapshot a witness cell (TES-001 primary_image_id) before and after.
        before_snapshot = next(
            (r.get("primary_image_id", "") for r in catalog_rows
             if r.get("item_id") == "TES-001"),
            "<missing>",
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = asset_indexer.run(dry_run=True)

        # Re-read TES-001 to confirm no write occurred.
        catalog_rows_after = sheets_helpers.read_all_rows(
            catalog_id, catalog_tab, service=service,
        )
        after_snapshot = next(
            (r.get("primary_image_id", "") for r in catalog_rows_after
             if r.get("item_id") == "TES-001"),
            "<missing>",
        )

        _check(
            "8. dry-run — completes, returns dry_run=True, does not change TES-001 row",
            result.get("status") == "success"
            and result.get("dry_run") is True
            and before_snapshot == after_snapshot,
            f"status={result.get('status')}, dry_run={result.get('dry_run')}, "
            f"before={before_snapshot!r}, after={after_snapshot!r}",
        )
    except Exception as exc:
        _check("8. dry-run", False, f"{type(exc).__name__}: {exc}")

    # 9. next_item_id generation — produces correct sequential ID.
    try:
        existing = [r.get("item_id", "") for r in catalog_rows]
        # Highest current is TES-040 (40 catalog rows). Next should be TES-041.
        nxt = asset_indexer.next_item_id(existing, prefix="TES-")

        # Synthetic edge case: gap-free incrementing.
        synthetic = ["TES-001", "TES-003", "TES-007"]
        synth_nxt = asset_indexer.next_item_id(synthetic, prefix="TES-")

        # Empty list → TES-001.
        empty_nxt = asset_indexer.next_item_id([], prefix="TES-")

        _check(
            "9. next_item_id — live next == TES-041, synthetic next == TES-008, empty == TES-001",
            nxt == "TES-041" and synth_nxt == "TES-008" and empty_nxt == "TES-001",
            f"live_next={nxt!r}, synth_next={synth_nxt!r}, empty_next={empty_nxt!r}",
        )
    except Exception as exc:
        _check("9. next_item_id", False, f"{type(exc).__name__}: {exc}")

    # 10. Full run — writes to live sheets and posts to Slack.
    # Writes are real and should persist. After run, at least one catalog row
    # must have a non-empty primary_image_id.
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = asset_indexer.run(dry_run=False)

        catalog_rows_after = sheets_helpers.read_all_rows(
            catalog_id, catalog_tab, service=service,
        )
        rows_with_image = [
            r for r in catalog_rows_after
            if str(r.get("primary_image_id", "")).strip()
        ]

        # The known match TES-004 (Compact Track Loader 325G) should now have a primary_image_id.
        tes_004 = next(
            (r for r in catalog_rows_after if r.get("item_id") == "TES-004"),
            None,
        )
        tes_004_pid = (tes_004 or {}).get("primary_image_id", "")

        _check(
            "10. full run — at least one catalog row has primary_image_id; TES-004 populated",
            result.get("status") == "success"
            and result.get("dry_run") is False
            and len(rows_with_image) > 0
            and bool(tes_004_pid.strip()),
            f"status={result.get('status')}, rows_with_image={len(rows_with_image)}, "
            f"TES-004 primary_image_id={tes_004_pid!r}",
        )
    except Exception as exc:
        _check("10. full run", False, f"{type(exc).__name__}: {exc}")

    total = _PASSED + len(_FAILURES)
    print()
    print(f"Results: {_PASSED}/{total} passed")
    if _FAILURES:
        print()
        print("Failures:")
        for fname, detail in _FAILURES:
            print(f"  - {fname}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_tests())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
