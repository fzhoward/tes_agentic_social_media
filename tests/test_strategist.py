"""Tests for agents/strategist.py.

The first 10 tests are deterministic and run without any external API calls.
Test 11 is an integration test that calls the live Anthropic API against
live Google Sheets — it COSTS MONEY and should not run in CI. The dry-run
flag ensures no writes happen to the Content Queue.

Run from the project root:
    python -m tests.test_strategist
"""

from __future__ import annotations

import io
import os
import sys
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timedelta
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

from agents import strategist  # noqa: E402
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


def _make_queue_row(
    platform: str,
    status: str,
    scheduled: str = "",
    objective: str = "",
    content_type: str = "",
) -> dict:
    return {
        strategist.CQ_PLATFORM: platform,
        strategist.CQ_STATUS: status,
        strategist.CQ_SCHEDULED: scheduled,
        strategist.CQ_OBJECTIVE: objective,
        strategist.CQ_CONTENT_TYPE: content_type,
    }


def test_queue_depth_check() -> None:
    # FB has 7 planned posts (== max), IG has 3, GBP has 2.
    rows: list[dict] = []
    rows += [_make_queue_row("facebook", "planned") for _ in range(7)]
    rows += [_make_queue_row("instagram", "planned") for _ in range(3)]
    rows += [_make_queue_row("gbp", "awaiting_approval") for _ in range(2)]
    # Drafted should NOT count toward depth (only planned + awaiting_approval).
    rows += [_make_queue_row("instagram", "drafted") for _ in range(5)]

    active, skipped, counts = strategist.check_queue_depth(
        rows, ["facebook", "instagram", "gbp"], max_depth=7
    )
    _check(
        "1. queue_depth — FB skipped at max, IG/GBP active, drafted not counted",
        skipped == ["facebook"]
        and set(active) == {"instagram", "gbp"}
        and counts == {"facebook": 7, "instagram": 3, "gbp": 2},
        f"active={active}, skipped={skipped}, counts={counts}",
    )


def test_planning_window_calculation() -> None:
    now = datetime(2026, 5, 25, 6, 0, 0, tzinfo=strategist.ET)
    window_start = now + timedelta(hours=36)
    window_end = now + timedelta(days=7)

    # FB: 3 posts in window (planned, drafted, awaiting_approval), 1 outside.
    rows = [
        _make_queue_row(
            "facebook", "planned",
            (window_start + timedelta(hours=2)).isoformat(),
        ),
        _make_queue_row(
            "facebook", "drafted",
            (window_start + timedelta(days=2)).isoformat(),
        ),
        _make_queue_row(
            "facebook", "awaiting_approval",
            (window_start + timedelta(days=4)).isoformat(),
        ),
        _make_queue_row(
            "facebook", "planned",
            (window_end + timedelta(days=2)).isoformat(),  # outside
        ),
        # Wrong status — should not count.
        _make_queue_row(
            "facebook", "rejected",
            (window_start + timedelta(hours=5)).isoformat(),
        ),
    ]
    # IG: 0 posts.
    needed = strategist.posts_needed_per_platform(
        rows,
        ["facebook", "instagram"],
        posts_per_week=7,
        window_start=window_start,
        window_end=window_end,
    )
    _check(
        "2. planning_window — FB needs 4 (7 - 3 in window), IG needs 7",
        needed == {"facebook": 4, "instagram": 7},
        f"needed={needed}",
    )


def test_objective_ratio_correction() -> None:
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=strategist.ET)
    # 8 brand_awareness, 2 lead_generation in last 14 days. 80/20 actual.
    rows = []
    for i in range(8):
        rows.append({
            strategist.PL_OBJECTIVE: "brand_awareness",
            strategist.PL_POSTED_DATETIME: (now - timedelta(days=1 + i)).isoformat(),
        })
    for i in range(2):
        rows.append({
            strategist.PL_OBJECTIVE: "lead_generation",
            strategist.PL_POSTED_DATETIME: (now - timedelta(days=1 + i)).isoformat(),
        })
    # Add one row outside the lookback window — should be ignored.
    rows.append({
        strategist.PL_OBJECTIVE: "lead_generation",
        strategist.PL_POSTED_DATETIME: (now - timedelta(days=30)).isoformat(),
    })

    result = strategist.calculate_objective_correction(
        rows,
        target_ratio={"brand_awareness": 60, "lead_generation": 40},
        lookback_days=14,
        now=now,
    )
    _check(
        "3. objective_correction — 80/20 actual leans toward lead_generation",
        result["lean_toward"] == "lead_generation"
        and result["actual"] == {"brand_awareness": 80, "lead_generation": 20}
        and result["target"] == {"brand_awareness": 60, "lead_generation": 40},
        f"result={result}",
    )


def test_objective_ratio_no_data() -> None:
    result = strategist.calculate_objective_correction(
        [],
        target_ratio={"brand_awareness": 60, "lead_generation": 40},
        lookback_days=14,
        now=datetime(2026, 5, 25, 12, 0, 0, tzinfo=strategist.ET),
    )
    _check(
        "4. objective_correction empty log — None lean, default target ratio",
        result["lean_toward"] is None
        and result["actual"] is None
        and result["target"] == {"brand_awareness": 60, "lead_generation": 40},
        f"result={result}",
    )


def test_catalog_filtering(config) -> None:  # type: ignore[no-untyped-def]
    catalog_id = config.get("catalog.spec_sheet_id")
    if not catalog_id:
        _check("5. catalog_filtering — config has spec_sheet_id", False, "missing")
        return
    try:
        service = sheets_helpers.get_sheets_service()
        catalog_tab = strategist._first_tab(service, catalog_id)
        catalog_rows = sheets_helpers.read_all_rows(
            catalog_id, catalog_tab, service=service
        )
    except Exception as exc:
        _check("5. catalog_filtering — read live catalog", False,
               f"{type(exc).__name__}: {exc}")
        return

    eligible = strategist.filter_eligible_catalog(
        catalog_rows, cooldown_days=7,
        now=datetime(2026, 5, 25, 12, 0, 0, tzinfo=strategist.ET),
    )

    # Always-true filter invariants regardless of catalog size.
    all_have_description = all(
        str(i.get(strategist.CAT_DESCRIPTION, "")).strip() for i in eligible
    )
    all_have_valid_status = all(
        str(i.get(strategist.CAT_STATUS, "")).strip().lower()
        in strategist.ACTIVE_CATALOG_STATUSES
        for i in eligible
    )

    # If the catalog has rows, also exercise a synthetic case to prove
    # exclusion rules trigger correctly even when the live catalog is empty.
    synthetic = [
        {strategist.CAT_ITEM_ID: "TES-A", strategist.CAT_ITEM_NAME: "A",
         strategist.CAT_CATEGORY: "Cat", strategist.CAT_STATUS: "active",
         strategist.CAT_DESCRIPTION: "desc"},
        {strategist.CAT_ITEM_ID: "TES-B", strategist.CAT_ITEM_NAME: "B",
         strategist.CAT_CATEGORY: "Cat", strategist.CAT_STATUS: "retired",
         strategist.CAT_DESCRIPTION: "desc"},  # excluded: status
        {strategist.CAT_ITEM_ID: "TES-C", strategist.CAT_ITEM_NAME: "C",
         strategist.CAT_CATEGORY: "Cat", strategist.CAT_STATUS: "active",
         strategist.CAT_DESCRIPTION: ""},  # excluded: no description
        {strategist.CAT_ITEM_ID: "TES-D", strategist.CAT_ITEM_NAME: "D",
         strategist.CAT_CATEGORY: "Cat", strategist.CAT_STATUS: "seasonal",
         strategist.CAT_DESCRIPTION: "ok"},  # kept
    ]
    syn_eligible = strategist.filter_eligible_catalog(
        synthetic, cooldown_days=7,
        now=datetime(2026, 5, 25, 12, 0, 0, tzinfo=strategist.ET),
    )
    syn_ids = sorted(i.get(strategist.CAT_ITEM_ID) for i in syn_eligible)

    # Live invariants must hold; synthetic must pick exactly the two valid rows.
    invariants_hold = all_have_description and all_have_valid_status
    synthetic_correct = syn_ids == ["TES-A", "TES-D"]

    catalog_empty_note = ""
    if len(catalog_rows) == 0:
        catalog_empty_note = (
            " [INFO: live catalog sheet has 0 data rows — filter invariants "
            "verified on synthetic data only. Config catalog.spec_sheet_id "
            "may need to be updated to point at the populated sheet.]"
        )

    _check(
        "5. catalog_filtering — invariants on live catalog, exclusion on synthetic"
        + catalog_empty_note,
        invariants_hold and synthetic_correct,
        f"live_eligible={len(eligible)}, total_catalog={len(catalog_rows)}, "
        f"invariants_hold={invariants_hold}, synthetic_ids={syn_ids}",
    )


def test_row_id_generation() -> None:
    dt = datetime(2026, 5, 27, 9, 0, 0, tzinfo=strategist.ET)
    fb_id = strategist.generate_row_id("facebook", dt, 1)
    ig_id = strategist.generate_row_id("instagram", dt, 12)
    gbp_id = strategist.generate_row_id("gbp", dt, 3)

    # Test assign_row_ids on a batch with same platform same date.
    posts = [
        {"platform": "facebook", "scheduled_datetime": dt.isoformat(),
         "media_format": "image2_enhanced"},
        {"platform": "facebook",
         "scheduled_datetime": (dt + timedelta(hours=5)).isoformat(),
         "media_format": "creatomate_video"},
        {"platform": "instagram", "scheduled_datetime": dt.isoformat(),
         "media_format": "image2_text_overlay"},
    ]
    assigned = strategist.assign_row_ids(posts)
    fb_seq = [p[strategist.CQ_ROW_ID] for p in assigned if p["platform"] == "facebook"]
    ig_seq = [p[strategist.CQ_ROW_ID] for p in assigned if p["platform"] == "instagram"]

    _check(
        "6. row_id_generation — formats and sequence are correct",
        fb_id == "STR-20260527-FB-01"
        and ig_id == "STR-20260527-IG-12"
        and gbp_id == "STR-20260527-GBP-03"
        and fb_seq == ["STR-20260527-FB-01", "STR-20260527-FB-02"]
        and ig_seq == ["STR-20260527-IG-01"],
        f"single: fb={fb_id} ig={ig_id} gbp={gbp_id}; "
        f"assigned fb_seq={fb_seq}, ig_seq={ig_seq}",
    )


def test_text_overlay_derivation() -> None:
    cases = [
        ("image2_enhanced", "FALSE"),
        ("image2_text_overlay", "TRUE"),
        ("creatomate_text_overlay", "TRUE"),
        ("creatomate_video", "FALSE"),
    ]
    results = [strategist.derive_text_overlay(fmt) for fmt, _ in cases]
    expected = [val for _, val in cases]
    _check(
        "7. text_overlay_derivation — TRUE only for text-overlay formats",
        results == expected,
        f"results={results}, expected={expected}",
    )


def test_timing_gap_enforcement() -> None:
    base = datetime(2026, 5, 27, 9, 0, 0, tzinfo=strategist.ET)
    # Two FB posts 2 hours apart — should be shifted to min_gap_hours (4h).
    posts = [
        {"platform": "facebook",
         "scheduled_datetime": base.isoformat()},
        {"platform": "facebook",
         "scheduled_datetime": (base + timedelta(hours=2)).isoformat()},
        {"platform": "instagram",
         "scheduled_datetime": base.isoformat()},  # Different platform, unaffected
        {"platform": "facebook",
         "scheduled_datetime": (base + timedelta(hours=3)).isoformat()},  # also too close
    ]
    enforced = strategist.enforce_timing_gaps(posts, min_gap_hours=4)
    fb_dts = sorted(
        strategist._parse_dt(p["scheduled_datetime"])
        for p in enforced if p["platform"] == "facebook"
    )
    gaps = [(fb_dts[i + 1] - fb_dts[i]).total_seconds() / 3600
            for i in range(len(fb_dts) - 1)]
    all_gaps_ok = all(g >= 4.0 - 1e-6 for g in gaps)
    _check(
        "8. timing_gap_enforcement — same-platform posts shifted to min_gap_hours",
        all_gaps_ok and len(fb_dts) == 3,
        f"fb_dts={[d.isoformat() for d in fb_dts]}, gaps_hours={gaps}",
    )


def test_video_cap_enforcement() -> None:
    base = datetime(2026, 5, 27, 9, 0, 0, tzinfo=strategist.ET)
    # 4 creatomate_video posts on FB. Cap is 2 → 2 converted.
    posts = [
        {"platform": "facebook", "media_format": "creatomate_video",
         "scheduled_datetime": (base + timedelta(days=i)).isoformat()}
        for i in range(4)
    ]
    # Add IG videos under the cap — should stay.
    posts.extend([
        {"platform": "instagram", "media_format": "creatomate_video",
         "scheduled_datetime": (base + timedelta(days=i)).isoformat()}
        for i in range(2)
    ])
    out, warnings = strategist.enforce_video_cap(posts, max_per_platform=2)

    fb_videos = [p for p in out if p["platform"] == "facebook"
                 and p["media_format"] == "creatomate_video"]
    fb_converted = [p for p in out if p["platform"] == "facebook"
                    and p["media_format"] == "creatomate_text_overlay"]
    ig_videos = [p for p in out if p["platform"] == "instagram"
                 and p["media_format"] == "creatomate_video"]

    _check(
        "9. video_cap_enforcement — FB capped at 2 videos, IG unchanged, warnings emitted",
        len(fb_videos) == 2 and len(fb_converted) == 2
        and len(ig_videos) == 2 and len(warnings) == 2,
        f"fb_videos={len(fb_videos)}, fb_converted={len(fb_converted)}, "
        f"ig_videos={len(ig_videos)}, warnings={len(warnings)}",
    )


def test_validate_post_object() -> None:
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=strategist.ET)
    window_end = datetime(2026, 6, 3, 0, 0, 0, tzinfo=strategist.ET)
    active = {"facebook", "instagram", "gbp"}
    eligible = {"TES-001", "TES-002", "TES-004"}

    good = {
        "platform": "facebook",
        "scheduled_datetime": "2026-05-28T09:00:00-04:00",
        "objective": "brand_awareness",
        "content_type": "Equipment Spotlight / Product Feature",
        "focus_equipment_id": "TES-004",
        "angle": "Highlight zero tail swing in tight residential lots",
        "cta_type": "comment",
        "media_format": "creatomate_text_overlay",
        "draft_notes": "Use the dig depth spec as a concrete detail.",
    }
    ok_good, reason_good = strategist.validate_post(
        good, eligible, active, window_start, window_end
    )

    # Bad: unknown content_type
    bad_ct = dict(good, content_type="Random Made-Up Type")
    ok_ct, _ = strategist.validate_post(
        bad_ct, eligible, active, window_start, window_end
    )

    # Bad: unknown focus_equipment_id
    bad_focus = dict(good, focus_equipment_id="TES-999")
    ok_focus, _ = strategist.validate_post(
        bad_focus, eligible, active, window_start, window_end
    )

    # Bad: scheduled outside window
    bad_dt = dict(good, scheduled_datetime="2026-07-01T09:00:00-04:00")
    ok_dt, _ = strategist.validate_post(
        bad_dt, eligible, active, window_start, window_end
    )

    # Bad: invalid platform
    bad_platform = dict(good, platform="tiktok")
    ok_platform, _ = strategist.validate_post(
        bad_platform, eligible, active, window_start, window_end
    )

    # Bad: empty angle
    bad_angle = dict(good, angle="")
    ok_angle, _ = strategist.validate_post(
        bad_angle, eligible, active, window_start, window_end
    )

    # OK: empty focus_equipment_id is allowed
    no_focus = dict(good, focus_equipment_id="",
                    content_type="Educational Tip")
    ok_no_focus, _ = strategist.validate_post(
        no_focus, eligible, active, window_start, window_end
    )

    _check(
        "10. validate_post — accepts valid, rejects bad content_type / focus / "
        "datetime / platform / angle; allows empty focus",
        ok_good and not ok_ct and not ok_focus and not ok_dt
        and not ok_platform and not ok_angle and ok_no_focus,
        f"good={ok_good} ({reason_good!r}), bad_ct={ok_ct}, bad_focus={ok_focus}, "
        f"bad_dt={ok_dt}, bad_platform={ok_platform}, bad_angle={ok_angle}, "
        f"no_focus={ok_no_focus}",
    )


def test_dry_run_full(config) -> None:  # type: ignore[no-untyped-def]
    # =================================================================
    # INTEGRATION TEST — calls the live Anthropic API. COSTS MONEY.
    # Do not run this in CI. Safe to run locally; dry_run=True ensures
    # no writes to the Content Queue.
    # =================================================================
    queue_id = config.get("drive.content_queue_sheet_id")
    if not queue_id:
        _check("11. dry_run_full — config has content_queue_sheet_id", False, "missing")
        return

    try:
        service = sheets_helpers.get_sheets_service()
        queue_tab = strategist._first_tab(service, queue_id)
        before = sheets_helpers.read_all_rows(queue_id, queue_tab, service=service)
    except Exception as exc:
        _check("11. dry_run_full — read pre-snapshot", False,
               f"{type(exc).__name__}: {exc}")
        return

    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = strategist.run(dry_run=True)
    except Exception as exc:
        _check("11. dry_run_full — run completes", False,
               f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return

    after = sheets_helpers.read_all_rows(queue_id, queue_tab, service=service)
    no_writes = len(before) == len(after)

    status_ok = result.get("status") == "success"
    dry_ok = result.get("dry_run") is True
    posts_planned = result.get("posts_planned", 0)
    counts_ok = isinstance(posts_planned, int) and posts_planned >= 0

    fields_ok = True
    if posts_planned > 0 and isinstance(result.get("posts"), list):
        required_keys = {
            strategist.CQ_ROW_ID,
            strategist.CQ_STATUS,
            strategist.CQ_PLATFORM,
            strategist.CQ_SCHEDULED,
            strategist.CQ_OBJECTIVE,
            strategist.CQ_CONTENT_TYPE,
            strategist.CQ_ANGLE,
            strategist.CQ_CTA_TYPE,
            strategist.CQ_MEDIA_FORMAT,
            strategist.CQ_TEXT_OVERLAY,
        }
        for post in result["posts"]:
            if not isinstance(post, dict):
                continue
            missing = required_keys - set(post.keys())
            if missing:
                fields_ok = False
                print(f"    missing keys on a post: {missing}")
                break

    _check(
        "11. dry_run_full — status success, dry_run True, no writes, fields populated",
        status_ok and dry_ok and counts_ok and no_writes and fields_ok,
        f"status={result.get('status')}, dry_run={result.get('dry_run')}, "
        f"posts_planned={posts_planned}, before_count={len(before)}, "
        f"after_count={len(after)}, fields_ok={fields_ok}, "
        f"warnings={result.get('validation_warnings', [])[:3]}, "
        f"skipped={result.get('skipped_platforms')}, "
        f"errors={result.get('errors')}",
    )


def run_tests() -> int:
    print(f"Loading config from: {CONFIG_FILE}")
    config = load_config()

    print()
    print("Deterministic tests (no API calls):")
    test_queue_depth_check()
    test_planning_window_calculation()
    test_objective_ratio_correction()
    test_objective_ratio_no_data()
    test_catalog_filtering(config)
    test_row_id_generation()
    test_text_overlay_derivation()
    test_timing_gap_enforcement()
    test_video_cap_enforcement()
    test_validate_post_object()

    print()
    print("Integration test (calls Anthropic API — COSTS MONEY):")
    test_dry_run_full(config)

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
