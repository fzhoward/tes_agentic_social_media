"""Tests for tools/image_selector.py.

Runs against live Image Metadata sheet, live Equipment Catalog, and live
Content Queue. Prerequisite: Run `python tools/google_auth.py` first to
generate the OAuth token.

Test 7 invokes the existing test_asset_indexer suite as a subprocess to
guard against regressions from the shared-code refactor.

Run from the project root:
    python -m tests.test_image_selector
"""

from __future__ import annotations

import os
import subprocess
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

from tools import image_selector, sheets_helpers  # noqa: E402
from tools.config_loader import Config, load_config  # noqa: E402


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


def _looks_like_drive_id(s: str) -> bool:
    return (
        isinstance(s, str)
        and len(s) > 10
        and "/" not in s
        and "://" not in s
        and " " not in s
    )


def _find_catalog_row(catalog_rows: list[dict], item_id: str) -> dict | None:
    for r in catalog_rows:
        if r.get("item_id") == item_id:
            return r
    return None


# ----------------------------------------------------------------------
# Deterministic select_top_candidates tests (mocked sheets — no live calls)
# ----------------------------------------------------------------------

_IMAGE_META_ID = "fake-image-meta-sheet-id"
_QUEUE_ID = "fake-queue-sheet-id"


class _FakeSheetsService:
    """Minimal sheets service stub for image_selector._first_tab calls."""

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, fields):  # noqa: N803 — matches real API kwarg
        return self

    def execute(self):
        return {"sheets": [{"properties": {"title": "Sheet1"}}]}


def _mock_config() -> Config:
    return Config({
        "catalog": {"image_metadata_sheet_id": _IMAGE_META_ID},
        "drive": {"content_queue_sheet_id": _QUEUE_ID},
    })


def _patch_read_all_rows(rows_by_sheet_id: dict[str, list[dict]] | None = None,
                        raises: Exception | None = None):
    """Return (restore_fn, ) after patching sheets_helpers.read_all_rows.

    Returns a single restore callable. Usage:
        restore = _patch_read_all_rows({sheet_id: rows})
        try:
            ...
        finally:
            restore()
    """
    rows_by_sheet_id = rows_by_sheet_id or {}
    orig = sheets_helpers.read_all_rows

    def fake(spreadsheet_id, tab, service=None):
        if raises is not None:
            raise raises
        return rows_by_sheet_id.get(spreadsheet_id, [])

    sheets_helpers.read_all_rows = fake

    def restore():
        sheets_helpers.read_all_rows = orig

    return restore


def _make_meta_row(image_id: str, *, category: str = "Excavator",
                   model: str = "303.5", quality: str = "Great Shot",
                   context: str = "Equipment Working",
                   job_type: str = "Land Clearing",
                   notes: str = "after timber harvest",
                   timestamp: str = "1/1/2026 10:00:00") -> dict:
    return {
        "Image_ID": image_id,
        "Equipment Category": category,
        "Equipment Model": model,
        "Photo Quality": quality,
        "Photo Context": context,
        "Job Type": job_type,
        "Notes": notes,
        "Timestamp": timestamp,
    }


def _catalog_row(category: str = "Excavator", model: str = "303.5",
                 primary_image_id: str = "primary-fallback-id") -> dict:
    return {
        "category": category,
        "model": model,
        "primary_image_id": primary_image_id,
    }


def test_select_top_candidates_returns_max_5() -> None:
    rows = [_make_meta_row(f"img{i}", quality="Decent",
                           timestamp=f"{i}/1/2026 10:00:00")
            for i in range(1, 9)]
    restore = _patch_read_all_rows({_IMAGE_META_ID: rows})
    try:
        result = image_selector.select_top_candidates(
            equipment_id="eq-1",
            catalog_row=_catalog_row(),
            config=_mock_config(),
            service=_FakeSheetsService(),
            max_candidates=5,
        )
    finally:
        restore()
    candidates = result.get("candidates", [])
    ranks = [c.get("rank") for c in candidates]
    _check(
        "8. select_top_candidates — returns exactly 5 candidates from 8 "
        "matches, ranks 1..5 in order",
        len(candidates) == 5
        and ranks == [1, 2, 3, 4, 5]
        and result.get("total_available") == 8
        and result.get("error") == "",
        f"len={len(candidates)}, ranks={ranks}, "
        f"total_available={result.get('total_available')}, "
        f"error={result.get('error')!r}",
    )


def test_select_top_candidates_returns_all_when_fewer() -> None:
    rows = [_make_meta_row("a"), _make_meta_row("b"), _make_meta_row("c")]
    restore = _patch_read_all_rows({_IMAGE_META_ID: rows})
    try:
        result = image_selector.select_top_candidates(
            equipment_id="eq-1",
            catalog_row=_catalog_row(),
            config=_mock_config(),
            service=_FakeSheetsService(),
            max_candidates=5,
        )
    finally:
        restore()
    candidates = result.get("candidates", [])
    ids = {c.get("image_id") for c in candidates}
    _check(
        "9. select_top_candidates — returns all candidates when fewer than "
        "max_candidates (3 available, max=5 → returns 3)",
        len(candidates) == 3
        and ids == {"a", "b", "c"}
        and result.get("total_available") == 3,
        f"len={len(candidates)}, ids={ids}, "
        f"total_available={result.get('total_available')}",
    )


def test_select_top_candidates_empty_when_no_match() -> None:
    rows = [_make_meta_row("x", category="Skid Steer", model="262D")]
    catalog = _catalog_row(category="Excavator", model="303.5",
                           primary_image_id="primary-xyz")
    restore = _patch_read_all_rows({_IMAGE_META_ID: rows})
    try:
        result = image_selector.select_top_candidates(
            equipment_id="eq-x",
            catalog_row=catalog,
            config=_mock_config(),
            service=_FakeSheetsService(),
        )
    finally:
        restore()
    _check(
        "10. select_top_candidates — empty candidates + fallback_image_id "
        "from catalog row when no metadata matches category+model",
        result.get("candidates") == []
        and result.get("fallback_image_id") == "primary-xyz"
        and result.get("total_available") == 0,
        f"candidates={result.get('candidates')!r}, "
        f"fallback_image_id={result.get('fallback_image_id')!r}, "
        f"total_available={result.get('total_available')}",
    )


def test_select_top_candidates_metadata_fields() -> None:
    rows = [
        _make_meta_row("img-1", job_type="Land Clearing", notes="timber site",
                       context="Equipment Working"),
        _make_meta_row("img-2", job_type="Foundation", notes="",
                       context="Equipment staged / parked",
                       quality="Decent"),
        _make_meta_row("img-3", job_type="", notes="yard photo",
                       context="Finished result", quality="Decent"),
    ]
    restore = _patch_read_all_rows({_IMAGE_META_ID: rows})
    try:
        result = image_selector.select_top_candidates(
            equipment_id="eq-1",
            catalog_row=_catalog_row(),
            config=_mock_config(),
            service=_FakeSheetsService(),
        )
    finally:
        restore()
    candidates = result.get("candidates", [])
    required_keys = {"image_id", "photo_context", "job_type", "notes", "rank"}
    all_keys_present = all(required_keys.issubset(c.keys()) for c in candidates)
    ranks = [c.get("rank") for c in candidates]
    first = candidates[0] if candidates else {}
    _check(
        "11. select_top_candidates — each candidate has image_id, "
        "photo_context, job_type, notes, rank; ranks start at 1 and increment",
        len(candidates) == 3
        and all_keys_present
        and ranks == [1, 2, 3]
        and first.get("job_type") == "Land Clearing"
        and first.get("notes") == "timber site"
        and first.get("photo_context") == "Equipment Working",
        f"all_keys_present={all_keys_present}, ranks={ranks}, "
        f"first={first!r}",
    )


def test_select_top_candidates_excludes_recently_used() -> None:
    rows = [
        _make_meta_row("img-1", timestamp="1/1/2026 10:00:00"),
        _make_meta_row("img-2", timestamp="2/1/2026 10:00:00"),
        _make_meta_row("img-3", timestamp="3/1/2026 10:00:00"),
        _make_meta_row("img-4", timestamp="4/1/2026 10:00:00"),
    ]
    # Simulate img-1 and img-2 used within the rotation window for eq-1.
    from datetime import datetime, timezone
    recent_iso = datetime.now(timezone.utc).isoformat()
    queue_rows = [
        {
            "focus_equipment_id": "eq-1",
            "status": "drafted",
            "scheduled_datetime": recent_iso,
            "source_image_id": "img-1",
        },
        {
            "focus_equipment_id": "eq-1",
            "status": "drafted",
            "scheduled_datetime": recent_iso,
            "source_image_id": "img-2",
        },
    ]
    restore = _patch_read_all_rows({
        _IMAGE_META_ID: rows,
        _QUEUE_ID: queue_rows,
    })
    try:
        result = image_selector.select_top_candidates(
            equipment_id="eq-1",
            catalog_row=_catalog_row(),
            config=_mock_config(),
            service=_FakeSheetsService(),
        )
    finally:
        restore()
    ids = [c.get("image_id") for c in result.get("candidates", [])]
    _check(
        "12. select_top_candidates — excludes recently-used images "
        "(img-1 + img-2 removed; img-3 and img-4 remain, ranked by recency)",
        set(ids) == {"img-3", "img-4"}
        and len(ids) == 2
        and result.get("total_available") == 4,
        f"ids={ids}, total_available={result.get('total_available')}",
    )


def test_select_top_candidates_never_raises() -> None:
    restore = _patch_read_all_rows(
        rows_by_sheet_id={}, raises=RuntimeError("simulated sheet failure"),
    )
    try:
        result = image_selector.select_top_candidates(
            equipment_id="eq-1",
            catalog_row=_catalog_row(primary_image_id="prim-zzz"),
            config=_mock_config(),
            service=_FakeSheetsService(),
        )
    finally:
        restore()
    _check(
        "13. select_top_candidates — never raises; returns valid dict with "
        "error string + fallback_image_id when sheet read fails",
        isinstance(result, dict)
        and result.get("candidates") == []
        and result.get("error", "")
        and result.get("fallback_image_id") == "prim-zzz",
        f"result={result!r}",
    )


def run_deterministic_tests() -> None:
    """Run all mocked-sheet tests. Safe to run without credentials."""
    print()
    print("Deterministic select_top_candidates tests (mocked sheets):")
    test_select_top_candidates_returns_max_5()
    test_select_top_candidates_returns_all_when_fewer()
    test_select_top_candidates_empty_when_no_match()
    test_select_top_candidates_metadata_fields()
    test_select_top_candidates_excludes_recently_used()
    test_select_top_candidates_never_raises()


def run_tests() -> int:
    run_deterministic_tests()

    print()
    print("Live tests (require credentials):")
    print(f"Loading config from: {CONFIG_FILE}")
    config = load_config()

    catalog_id = config.get("catalog.spec_sheet_id", "")
    queue_id = config.get("drive.content_queue_sheet_id", "")
    image_meta_id = config.get("catalog.image_metadata_sheet_id", "")
    _check(
        "0. config has spec/queue/image_metadata sheet IDs",
        bool(catalog_id) and bool(queue_id) and bool(image_meta_id),
        f"spec={catalog_id!r}, queue={queue_id!r}, meta={image_meta_id!r}",
    )

    # Set up Sheets service + read catalog once for reuse.
    skip_live = False
    service = None
    catalog_rows: list[dict] = []
    try:
        service = sheets_helpers.get_sheets_service()
        catalog_tab = image_selector._first_tab(service, catalog_id)
        catalog_rows = sheets_helpers.read_all_rows(
            catalog_id, catalog_tab, service=service,
        )
        print(f"  (catalog tab: {catalog_tab!r}, rows={len(catalog_rows)})")
    except Exception as exc:
        _check("0b. service + catalog read", False, f"{type(exc).__name__}: {exc}")
        print("\nSkipping live tests — auth/catalog unavailable.")
        skip_live = True

    if skip_live:
        total = _PASSED + len(_FAILURES)
        print()
        print(f"Results: {_PASSED}/{total} passed")
        if _FAILURES:
            print()
            print("Failures:")
            for fname, detail in _FAILURES:
                print(f"  - {fname}: {detail}")
        return 1 if _FAILURES else 0

    # ----------------------------------------------------------------
    # 1. test_finds_images_for_known_item — TES-004 (Compact Track Loader 325G)
    # ----------------------------------------------------------------
    try:
        tes_004 = _find_catalog_row(catalog_rows, "TES-004")
        if tes_004 is None:
            raise RuntimeError("TES-004 not found in catalog")

        result = image_selector.select_source_image(
            equipment_id="TES-004",
            catalog_row=tes_004,
            config=config,
            service=service,
        )
        img_id = result.get("image_id", "")
        _check(
            "1. finds images for TES-004 — non-empty Drive-ID image, total_available>=4, not fallback",
            bool(img_id)
            and _looks_like_drive_id(img_id)
            and result.get("total_available", 0) >= 4
            and result.get("fallback") is False,
            f"result={result!r}",
        )
    except Exception as exc:
        _check("1. finds images for TES-004", False, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------
    # 2. test_rotation_avoids_recent — selector picks unused, or works
    # ----------------------------------------------------------------
    try:
        tes_004 = _find_catalog_row(catalog_rows, "TES-004")
        if tes_004 is None:
            raise RuntimeError("TES-004 not found in catalog")

        used = image_selector.get_recently_used_image_ids(
            "TES-004", queue_id, service,
        )
        result = image_selector.select_source_image(
            equipment_id="TES-004",
            catalog_row=tes_004,
            config=config,
            service=service,
        )
        chosen = result.get("image_id", "")

        if used:
            # If we have history, chosen should not be in used (unless filter reset).
            reason = result.get("selection_reason", "")
            ok = (chosen not in used) or ("filter reset" in reason)
            _check(
                "2. rotation — chosen image not in recent-used set (or filter reset)",
                ok,
                f"chosen={chosen!r}, used={sorted(used)}, reason={reason!r}",
            )
        else:
            # No history yet — verify selector still returns a valid image.
            _check(
                "2. rotation — no history for TES-004; selector returns valid image",
                bool(chosen) and result.get("fallback") is False,
                f"result={result!r}",
            )
    except Exception as exc:
        _check("2. rotation", False, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------
    # 3. test_fallback_to_primary_image_id — synthetic row with non-matching cat/model
    # ----------------------------------------------------------------
    try:
        fake_pid = "FAKE_DRIVE_ID_FOR_TEST_3_zzzz"
        synthetic = {
            "item_id": "TEST-SYNTH-003",
            "category": "Nonexistent Category ZZZZ",
            "model": "Nonexistent Model ZZZZ",
            "primary_image_id": fake_pid,
        }
        result = image_selector.select_source_image(
            equipment_id="TEST-SYNTH-003",
            catalog_row=synthetic,
            config=config,
            service=service,
        )
        _check(
            "3. fallback to primary_image_id when no metadata rows match",
            result.get("fallback") is True
            and result.get("image_id") == fake_pid
            and "primary_image_id" in result.get("selection_reason", ""),
            f"result={result!r}",
        )
    except Exception as exc:
        _check("3. fallback to primary_image_id", False, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------
    # 4. test_no_images_at_all — empty primary_image_id and no metadata
    # ----------------------------------------------------------------
    try:
        synthetic = {
            "item_id": "TEST-NONE-004",
            "category": "Nonexistent Category ZZZZ",
            "model": "Nonexistent Model ZZZZ",
            "primary_image_id": "",
        }
        result = image_selector.select_source_image(
            equipment_id="TEST-NONE-004",
            catalog_row=synthetic,
            config=config,
            service=service,
        )
        _check(
            "4. no images at all — image_id empty, fallback True, reason mentions 'no images'",
            result.get("image_id") == ""
            and result.get("fallback") is True
            and "no images" in result.get("selection_reason", "").lower(),
            f"result={result!r}",
        )
    except Exception as exc:
        _check("4. no images at all", False, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------
    # 5. test_all_images_used_resets_filter — monkeypatch usage to include all IDs
    # ----------------------------------------------------------------
    try:
        tes_004 = _find_catalog_row(catalog_rows, "TES-004")
        if tes_004 is None:
            raise RuntimeError("TES-004 not found in catalog")

        all_images = image_selector.get_images_for_equipment(
            tes_004, image_meta_id, service,
        )
        all_ids = {str(r.get("Image_ID", "")).strip() for r in all_images}
        all_ids.discard("")

        original = image_selector.get_recently_used_image_ids

        def _mock_all_used(*args, **kwargs):  # type: ignore[no-untyped-def]
            return set(all_ids)

        image_selector.get_recently_used_image_ids = _mock_all_used
        try:
            result = image_selector.select_source_image(
                equipment_id="TES-004",
                catalog_row=tes_004,
                config=config,
                service=service,
            )
        finally:
            image_selector.get_recently_used_image_ids = original

        chosen = result.get("image_id", "")
        reason = result.get("selection_reason", "")
        _check(
            "5. all images used — filter resets, still returns a valid image",
            len(all_ids) > 0
            and bool(chosen)
            and chosen in all_ids
            and result.get("fallback") is False
            and "filter reset" in reason,
            f"all_ids_count={len(all_ids)}, result={result!r}",
        )
    except Exception as exc:
        _check("5. all images used resets filter", False, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------
    # 6. test_ranking_prefers_quality — synthetic rows; quality beats context
    # ----------------------------------------------------------------
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
        best = image_selector.select_best_image(group)
        _check(
            "6. ranking prefers quality — 'Great shot' beats 'Decent' even with worse context",
            best.get("Image_ID") == "great_id",
            f"selected={best.get('Image_ID')!r}",
        )
    except Exception as exc:
        _check("6. ranking prefers quality", False, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------
    # 7. test_asset_indexer_still_passes — regression guard on the refactor
    # ----------------------------------------------------------------
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "tests.test_asset_indexer"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "BUSINESS_CONFIG_PATH": str(CONFIG_FILE)},
        )
        passed = proc.returncode == 0
        # Pull the summary line for the failure detail.
        summary = ""
        for line in (proc.stdout or "").splitlines():
            if line.strip().startswith("Results:"):
                summary = line.strip()
                break
        detail = (
            f"returncode={proc.returncode}, {summary}, "
            f"stderr_tail={proc.stderr[-300:] if proc.stderr else ''!r}"
        )
        _check(
            "7. tests/test_asset_indexer.py still passes after refactor",
            passed,
            detail,
        )
    except Exception as exc:
        _check(
            "7. test_asset_indexer regression",
            False,
            f"{type(exc).__name__}: {exc}",
        )

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
