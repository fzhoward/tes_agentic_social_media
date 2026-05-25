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


def run_tests() -> int:
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
    try:
        service = sheets_helpers.get_sheets_service()
        catalog_tab = image_selector._first_tab(service, catalog_id)
        catalog_rows = sheets_helpers.read_all_rows(
            catalog_id, catalog_tab, service=service,
        )
        print(f"  (catalog tab: {catalog_tab!r}, rows={len(catalog_rows)})")
    except Exception as exc:
        _check("0b. service + catalog read", False, f"{type(exc).__name__}: {exc}")
        print("\nCannot continue without auth/catalog.")
        return 1

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
