"""Tests for tools/sheets_helpers.py.

Validates Sheets operations against real TES Rentals sheets.
Prerequisite: Run `python tools/google_auth.py` first to generate the token.

Run from the project root:
    python -m tests.test_sheets_helpers
"""

from __future__ import annotations

import os
import sys
import traceback
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

from tools.config_loader import load_config  # noqa: E402
from tools import sheets_helpers  # noqa: E402


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


def _expect_raises(name: str, exc_type: type, fn) -> Exception | None:
    try:
        result = fn()
    except exc_type as e:
        _check(name, True, str(e))
        return e
    except Exception as e:
        _check(
            name,
            False,
            f"expected {exc_type.__name__}, got {type(e).__name__}: {e}",
        )
        return None
    _check(
        name,
        False,
        f"expected {exc_type.__name__}, got result: {result!r}",
    )
    return None


def _first_sheet_meta(service, spreadsheet_id: str) -> tuple[str, int]:
    """Return (title, numeric sheet_id) of the first tab."""
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(title,sheetId))",
    ).execute()
    sheets = meta.get("sheets", [])
    if not sheets:
        raise RuntimeError(f"spreadsheet {spreadsheet_id} has no tabs")
    props = sheets[0]["properties"]
    return props["title"], int(props["sheetId"])


def _delete_row(service, spreadsheet_id: str, sheet_grid_id: int, row_number: int) -> None:
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [{
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_grid_id,
                        "dimension": "ROWS",
                        "startIndex": row_number - 1,
                        "endIndex": row_number,
                    }
                }
            }]
        },
    ).execute()


class _BoomService:
    """A service stub that raises if any attribute is accessed.

    Used to prove a code path didn't call the API.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected API access: .{name}")


def run_tests() -> int:
    print(f"Loading config from: {CONFIG_FILE}")
    config = load_config()

    # Service creation
    try:
        service = sheets_helpers.get_sheets_service()
    except RuntimeError as exc:
        _check(
            "0. Service creation — get_sheets_service() returns a service",
            False,
            f"{exc} (run 'python tools/google_auth.py' first)",
        )
        print("\nCannot continue without an authenticated Sheets service.")
        return 1
    _check(
        "0. Service creation — get_sheets_service() returns a service",
        service is not None,
    )

    catalog_sheet_id = config.get("catalog.spec_sheet_id")
    content_queue_id = config.get("drive.content_queue_sheet_id")
    performance_log_id = config.get("drive.performance_log_sheet_id")

    # Discover tab names
    try:
        catalog_tab, catalog_grid = _first_sheet_meta(service, catalog_sheet_id)
        queue_tab, queue_grid = _first_sheet_meta(service, content_queue_id)
        perf_tab, _perf_grid = _first_sheet_meta(service, performance_log_id)
        print(f"  (catalog tab: {catalog_tab!r}, queue tab: {queue_tab!r}, perf tab: {perf_tab!r})")
    except Exception as e:
        _check(
            "0b. Tab name discovery — read first tab of each sheet",
            False,
            f"{type(e).__name__}: {e}",
        )
        return 1

    # 5. col_letter (run early — pure function, no API)
    try:
        cases = [
            (0, "A"), (1, "B"), (25, "Z"),
            (26, "AA"), (27, "AB"), (51, "AZ"), (52, "BA"),
        ]
        bad = [(i, e, sheets_helpers._col_letter(i)) for i, e in cases
               if sheets_helpers._col_letter(i) != e]
        _check(
            "5. _col_letter conversion -- 0->A, 25->Z, 26->AA, 27->AB, 51->AZ, 52->BA",
            not bad,
            f"mismatches: {bad}" if bad else "",
        )
    except Exception as e:
        _check("5. _col_letter conversion", False, f"{type(e).__name__}: {e}")

    # Seed: append a test row to Equipment Catalog so tests 1/3/4 have data
    # to exercise against. (The live catalog is currently empty.) Cleaned up
    # at the end of the catalog tests.
    catalog_test_id = "TEST_DELETE_ME"
    catalog_test_category = "TEST_CATEGORY"
    catalog_test_row_num: int | None = None
    try:
        catalog_test_row_num = sheets_helpers.append_row(
            catalog_sheet_id,
            catalog_tab,
            {
                "item_id": catalog_test_id,
                "item_name": "Test Item (sheets_helpers)",
                "category": catalog_test_category,
                "status": "test",
                "description": "Inserted by test_sheets_helpers — safe to delete.",
            },
            service=service,
        )
        print(f"  (seeded catalog row {catalog_test_row_num} for tests 1/3/4)")
    except Exception as e:
        print(f"  WARN  could not seed catalog test row: {type(e).__name__}: {e}")

    # 1. Read Equipment Catalog
    catalog_rows: list[dict] = []
    try:
        catalog_rows = sheets_helpers.read_all_rows(
            catalog_sheet_id, catalog_tab, service=service
        )
        expected_keys = {"item_id", "item_name", "category"}
        first = catalog_rows[0] if catalog_rows else {}
        missing = expected_keys - set(first.keys())
        _check(
            "1. read_all_rows on Equipment Catalog -- non-empty list with expected headers",
            len(catalog_rows) > 0 and not missing,
            f"rows={len(catalog_rows)}, missing_keys={missing}",
        )
    except Exception as e:
        _check("1. read_all_rows on Equipment Catalog", False, f"{type(e).__name__}: {e}")

    # 2. Read Content Queue
    # The live Content Queue schema has drifted from PLACEHOLDER_REGISTRY.md
    # (extra columns like cta_text, hook_text, critic_verdict; renamed
    # image_url -> media_url; etc.). We check the core columns that agents
    # actually depend on instead of the full registry list, and surface any
    # discrepancies as informational output.
    try:
        queue_rows = sheets_helpers.read_all_rows(
            content_queue_id, queue_tab, service=service
        )
        core_queue_cols = {
            "row_id", "status", "platform", "scheduled_datetime", "objective",
            "content_type", "focus_equipment_id", "media_format",
            "caption", "first_comment", "draft_notes",
            "critic_notes", "published_datetime",
        }
        headers = sheets_helpers._get_headers(
            content_queue_id, queue_tab, service=service
        )
        missing_cols = core_queue_cols - set(headers)
        _check(
            "2. read_all_rows on Content Queue -- returns list and contains core columns",
            isinstance(queue_rows, list) and not missing_cols,
            f"rows={len(queue_rows) if isinstance(queue_rows, list) else 'N/A'}, "
            f"missing_core_cols={missing_cols}",
        )
    except Exception as e:
        _check("2. read_all_rows on Content Queue", False, f"{type(e).__name__}: {e}")

    # 3. read_rows_filtered by category (uses seeded TEST_CATEGORY)
    try:
        filtered = sheets_helpers.read_rows_filtered(
            catalog_sheet_id, catalog_tab, "category", catalog_test_category,
            service=service,
        )
        all_match = all(r.get("category") == catalog_test_category for r in filtered)
        _check(
            f"3. read_rows_filtered by category={catalog_test_category!r} -- all rows match",
            len(filtered) > 0 and all_match,
            f"got {len(filtered)} rows, all_match={all_match}",
        )
    except Exception as e:
        _check("3. read_rows_filtered by category", False, f"{type(e).__name__}: {e}")

    # 4. find_rows_by_column_value by item_id (uses seeded test row)
    try:
        matches = sheets_helpers.find_rows_by_column_value(
            catalog_sheet_id, catalog_tab, "item_id", catalog_test_id,
            service=service,
        )
        shapes_ok = all(
            isinstance(t, tuple) and len(t) == 2
            and isinstance(t[0], int) and t[0] >= 2
            and isinstance(t[1], dict)
            for t in matches
        )
        id_matches = all(row.get("item_id") == catalog_test_id for _, row in matches)
        _check(
            f"4. find_rows_by_column_value item_id={catalog_test_id!r} -- returns (row_num, dict) with row>=2",
            len(matches) > 0 and shapes_ok and id_matches,
            f"matches={len(matches)}, shapes_ok={shapes_ok}, id_matches={id_matches}",
        )
    except Exception as e:
        _check("4. find_rows_by_column_value", False, f"{type(e).__name__}: {e}")

    # Cleanup the seeded catalog row.
    if catalog_test_row_num is not None:
        try:
            _delete_row(service, catalog_sheet_id, catalog_grid, catalog_test_row_num)
            print(f"  (cleanup) deleted row {catalog_test_row_num} from Equipment Catalog")
        except Exception as e:
            print(f"  WARN  cleanup failed for catalog row {catalog_test_row_num}: {e}")

    # 6. Append a test row to Content Queue, verify on re-read, then delete.
    test_row_id = "TEST_DELETE_ME"
    appended_row_num: int | None = None
    try:
        appended_row_num = sheets_helpers.append_row(
            content_queue_id,
            queue_tab,
            {
                "row_id": test_row_id,
                "status": "test",
                "platform": "facebook",
                "objective": "brand_awareness",
                "draft_notes": "sheets_helpers test row — safe to delete",
            },
            service=service,
        )
        _check(
            "6a. append_row — returns positive row number",
            isinstance(appended_row_num, int) and appended_row_num >= 2,
            f"got {appended_row_num!r}",
        )

        re_read = sheets_helpers.find_rows_by_column_value(
            content_queue_id, queue_tab, "row_id", test_row_id, service=service,
        )
        _check(
            "6b. append_row — appended row visible on re-read",
            len(re_read) >= 1 and any(r.get("row_id") == test_row_id for _, r in re_read),
            f"re_read={[rn for rn, _ in re_read]}",
        )
    except Exception as e:
        _check("6. append_row + re-read", False, f"{type(e).__name__}: {e}")

    # 7. update_cells on the appended row, verify new value.
    update_target_row = appended_row_num
    if update_target_row is not None:
        try:
            sheets_helpers.update_cells(
                content_queue_id,
                queue_tab,
                update_target_row,
                {"status": "updated", "draft_notes": "post-update content"},
                service=service,
            )
            after = sheets_helpers.find_rows_by_column_value(
                content_queue_id, queue_tab, "row_id", test_row_id, service=service,
            )
            after_row = next((r for rn, r in after if rn == update_target_row), None)
            _check(
                "7. update_cells — values read back after update",
                after_row is not None
                and after_row.get("status") == "updated"
                and after_row.get("draft_notes") == "post-update content",
                f"after_row={after_row}",
            )
        except Exception as e:
            _check("7. update_cells", False, f"{type(e).__name__}: {e}")
    else:
        _check("7. update_cells", False, "skipped — append failed")

    # 8. update_cells with bad column raises ValueError
    if update_target_row is not None:
        _expect_raises(
            "8. update_cells with nonexistent column raises ValueError",
            ValueError,
            lambda: sheets_helpers.update_cells(
                content_queue_id,
                queue_tab,
                update_target_row,
                {"this_column_does_not_exist": "x"},
                service=service,
            ),
        )
    else:
        _check("8. update_cells with nonexistent column", False, "skipped — append failed")

    # Cleanup: delete the test row.
    if update_target_row is not None:
        try:
            _delete_row(service, content_queue_id, queue_grid, update_target_row)
            print(f"  (cleanup) deleted row {update_target_row} from Content Queue")
        except Exception as e:
            print(f"  WARN  cleanup failed for row {update_target_row}: {e}")

    # 9. Empty Performance Log — read_all_rows returns [] (or non-error list)
    try:
        perf_rows = sheets_helpers.read_all_rows(
            performance_log_id, perf_tab, service=service,
        )
        _check(
            "9. read_all_rows on Performance Log — returns a list (possibly empty) without error",
            isinstance(perf_rows, list),
            f"got {type(perf_rows).__name__} with {len(perf_rows) if isinstance(perf_rows, list) else 'N/A'} rows",
        )
    except Exception as e:
        _check("9. read_all_rows on empty/sparse Performance Log", False, f"{type(e).__name__}: {e}")

    # 10. Header caching — second call must not hit the API.
    try:
        sheets_helpers._invalidate_header_cache()
        first_headers = sheets_helpers._get_headers(
            catalog_sheet_id, catalog_tab, service=service,
        )
        # Pass a service that would raise on any access. If caching works,
        # _get_headers returns from cache without touching service.
        second_headers = sheets_helpers._get_headers(
            catalog_sheet_id, catalog_tab, service=_BoomService(),
        )
        _check(
            "10. _get_headers caching — second call served from cache (no API access)",
            first_headers == second_headers and len(first_headers) > 0,
            f"first_len={len(first_headers)}, second_len={len(second_headers)}",
        )
    except AssertionError as e:
        _check(
            "10. _get_headers caching",
            False,
            f"second call hit the API: {e}",
        )
    except Exception as e:
        _check("10. _get_headers caching", False, f"{type(e).__name__}: {e}")

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
