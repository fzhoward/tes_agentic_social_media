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
from contextlib import redirect_stderr, redirect_stdout
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


def _spec_item() -> dict:
    """Catalog row mirroring the TES-018 live record (Conventional tail swing)."""
    return {
        strategist.CAT_ITEM_ID: "TES-018",
        strategist.CAT_ITEM_NAME: "Excavator - 50K - 210G/Zaxis 210",
        strategist.CAT_CATEGORY: "Full Size Excavator (75 - 210)",
        strategist.CAT_MODEL: "Zaxis 210",
        strategist.CAT_STATUS: "active",
        strategist.CAT_DESCRIPTION: (
            "John Deere 210G LC / Hitachi ZX210. Full-size 21-ton excavator with "
            "manual/hydraulic thumb. 6.8L PowerTech engine."
        ),
        strategist.CAT_COMMON_JOBS: "Heavy construction, demolition, land clearing",
        strategist.CAT_BEST_FOR: "Large-scale excavation, deep trenching",
        strategist.CAT_TAGS: "excavator, full-size, John Deere, 210G, Hitachi, ZX210, thumb",
        strategist.CAT_WEIGHT: "51,940 lbs",
        strategist.CAT_DIG_DEPTH: "21 feet",
        strategist.CAT_REACH: "32' 1\" at ground level",
        strategist.CAT_CAPACITY: "34,171 lbf bucket digging force / 108 gal fuel tank",
        strategist.CAT_HORSEPOWER: "159 HP",
        strategist.CAT_TAIL_SWING: "Conventional",
    }


def test_compact_catalog_line_includes_specs() -> None:
    item = _spec_item()
    line = strategist._compact_catalog_line(item)
    # Every populated spec field must appear in the prompt line so the LLM
    # has actual product attributes to ground its angle in. The whole point
    # of this test is to lock that behaviour in.
    needed_fragments = [
        "weight=51,940 lbs",
        "dig_depth=21 feet",
        "horsepower=159 HP",
        "tail_swing=Conventional",
        "capacity=34,171 lbf",
        "tags=excavator",
    ]
    missing = [frag for frag in needed_fragments if frag not in line]
    _check(
        "11. compact_catalog_line includes spec fields when non-empty",
        not missing,
        f"missing fragments: {missing}",
    )


def test_compact_catalog_line_omits_empty_specs() -> None:
    item = {
        strategist.CAT_ITEM_ID: "TES-X",
        strategist.CAT_ITEM_NAME: "Plain Item",
        strategist.CAT_CATEGORY: "Cat",
        strategist.CAT_DESCRIPTION: "minimal record",
    }
    line = strategist._compact_catalog_line(item)
    # No spec fields set, so they must not appear as empty fragments.
    bad = [frag for frag in (
        "weight=", "dig_depth=", "reach=", "capacity=",
        "horsepower=", "tail_swing=", "tags=",
    ) if frag in line]
    _check(
        "12. compact_catalog_line omits empty spec fields",
        not bad,
        f"unexpected empty spec fragments present: {bad}",
    )


def test_validate_angle_grounding_tail_swing() -> None:
    item = _spec_item()  # tail_swing=Conventional

    grounded_generic = strategist.validate_angle_grounding(
        "Show the 210G handling foundation work on a tight residential lot.",
        item,
    )
    grounded_truth = strategist.validate_angle_grounding(
        "Highlight the conventional tail swing tradeoff for large-scale digs.",
        item,
    )
    bad_reduced = strategist.validate_angle_grounding(
        "Full-size excavator power in a reduced tail swing package for urban sites.",
        item,
    )
    bad_zero = strategist.validate_angle_grounding(
        "Zero tail swing makes the 210G perfect for working close to structures.",
        item,
    )

    ok_generic, _ = grounded_generic
    ok_truth, _ = grounded_truth
    ok_reduced, vio_reduced = bad_reduced
    ok_zero, vio_zero = bad_zero

    _check(
        "13. validate_angle_grounding — tail-swing claims must match catalog field",
        ok_generic
        and ok_truth
        and not ok_reduced
        and not ok_zero
        and any("reduced tail swing" in v.lower() for v in vio_reduced)
        and any("zero tail swing" in v.lower() for v in vio_zero),
        f"generic={ok_generic}, truth={ok_truth}, reduced={ok_reduced} "
        f"({vio_reduced!r}), zero={ok_zero} ({vio_zero!r})",
    )


def test_validate_angle_grounding_numeric() -> None:
    item = _spec_item()  # weight 51,940 lbs, dig_depth 21 feet, horsepower 159 HP

    grounded_hp = strategist.validate_angle_grounding(
        "Pitch the 159 HP engine for deep utility digs.",
        item,
    )
    grounded_dig = strategist.validate_angle_grounding(
        "Reach down to 21 feet on footer cuts.",
        item,
    )
    bad_weight = strategist.validate_angle_grounding(
        "60,000 lbs of operating weight stays planted on uneven ground.",
        item,
    )
    bad_hp = strategist.validate_angle_grounding(
        "200 HP gives this 210G the headroom others lack.",
        item,
    )

    ok_hp, _ = grounded_hp
    ok_dig, _ = grounded_dig
    ok_weight, vio_weight = bad_weight
    ok_hp_bad, vio_hp_bad = bad_hp

    _check(
        "14. validate_angle_grounding — numeric spec values must appear in catalog",
        ok_hp
        and ok_dig
        and not ok_weight
        and not ok_hp_bad
        and any("60000" in v or "60,000" in v for v in vio_weight)
        and any("200" in v for v in vio_hp_bad),
        f"hp={ok_hp}, dig={ok_dig}, weight={ok_weight} ({vio_weight!r}), "
        f"hp_bad={ok_hp_bad} ({vio_hp_bad!r})",
    )


def test_apply_angle_grounding_check_replaces_and_warns() -> None:
    item = _spec_item()
    catalog_by_id = {item[strategist.CAT_ITEM_ID]: item}

    posts = [
        # Bad — false tail swing claim, must be rewritten.
        {
            strategist.CQ_ROW_ID: "STR-20260529-FB-01",
            strategist.CQ_FOCUS_EQUIPMENT: "TES-018",
            strategist.CQ_ANGLE: (
                "Full-size excavator power in a reduced tail swing package."
            ),
        },
        # Good — generic framing, must be left untouched.
        {
            strategist.CQ_ROW_ID: "STR-20260530-FB-01",
            strategist.CQ_FOCUS_EQUIPMENT: "TES-018",
            strategist.CQ_ANGLE: "Highlight the machine on tight downtown jobs.",
        },
        # No focus equipment — must be left untouched.
        {
            strategist.CQ_ROW_ID: "STR-20260601-GBP-01",
            strategist.CQ_FOCUS_EQUIPMENT: "",
            strategist.CQ_ANGLE: "Local development boom drives demand.",
        },
    ]

    warnings = strategist.apply_angle_grounding_check(posts, catalog_by_id)

    rewritten_angle = posts[0][strategist.CQ_ANGLE]
    untouched_good = posts[1][strategist.CQ_ANGLE]
    untouched_nofocus = posts[2][strategist.CQ_ANGLE]

    _check(
        "15. apply_angle_grounding_check — rewrites bad angle, warns, leaves good ones",
        len(warnings) == 1
        and rewritten_angle.startswith("Equipment spotlight —")
        and item[strategist.CAT_ITEM_NAME] in rewritten_angle
        and untouched_good == "Highlight the machine on tight downtown jobs."
        and untouched_nofocus == "Local development boom drives demand."
        and "STR-20260529-FB-01" in warnings[0],
        f"warnings={warnings!r}, rewritten={rewritten_angle!r}, "
        f"good={untouched_good!r}, nofocus={untouched_nofocus!r}",
    )


def test_compact_catalog_line_flags_zero_image_items() -> None:
    item_with_images = {
        strategist.CAT_ITEM_ID: "TES-A",
        strategist.CAT_ITEM_NAME: "Has Images",
        strategist.CAT_CATEGORY: "Cat",
        strategist.CAT_DESCRIPTION: "desc",
        strategist.CAT_IMAGE_COUNT: "3",
    }
    item_zero_int = dict(item_with_images,
                         **{strategist.CAT_ITEM_ID: "TES-B",
                            strategist.CAT_IMAGE_COUNT: "0"})
    item_empty_str = dict(item_with_images,
                          **{strategist.CAT_ITEM_ID: "TES-C",
                             strategist.CAT_IMAGE_COUNT: ""})
    item_bogus = dict(item_with_images,
                      **{strategist.CAT_ITEM_ID: "TES-D",
                         strategist.CAT_IMAGE_COUNT: "n/a"})

    line_ok = strategist._compact_catalog_line(item_with_images)
    line_zero = strategist._compact_catalog_line(item_zero_int)
    line_empty = strategist._compact_catalog_line(item_empty_str)
    line_bogus = strategist._compact_catalog_line(item_bogus)

    # Item with images: no NO IMAGES flag.
    # Items without images (zero, empty, unparseable): NO IMAGES flag present.
    _check(
        "16. compact_catalog_line — NO IMAGES flag on 0 / empty / unparseable, "
        "absent on items with images",
        "NO IMAGES" not in line_ok
        and "NO IMAGES" in line_zero
        and "NO IMAGES" in line_empty
        and "NO IMAGES" in line_bogus,
        f"ok={line_ok!r}, zero={line_zero!r}, empty={line_empty!r}, "
        f"bogus={line_bogus!r}",
    )


def test_enforce_image_coverage_drops_zero_image_media_posts() -> None:
    catalog_by_id = {
        "TES-IMG": {
            strategist.CAT_ITEM_ID: "TES-IMG",
            strategist.CAT_ITEM_NAME: "Has Images",
            strategist.CAT_IMAGE_COUNT: "5",
        },
        "TES-NOIMG": {
            strategist.CAT_ITEM_ID: "TES-NOIMG",
            strategist.CAT_ITEM_NAME: "Zero Images",
            strategist.CAT_IMAGE_COUNT: "0",
        },
        "TES-EMPTY": {
            strategist.CAT_ITEM_ID: "TES-EMPTY",
            strategist.CAT_ITEM_NAME: "Empty Count",
            strategist.CAT_IMAGE_COUNT: "",
        },
    }
    posts = [
        # Good — focus has images.
        {strategist.CQ_FOCUS_EQUIPMENT: "TES-IMG",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_SCHEDULED: "2026-05-28T09:00:00-04:00"},
        # Bad — focus has zero images and a media format is set.
        {strategist.CQ_FOCUS_EQUIPMENT: "TES-NOIMG",
         strategist.CQ_MEDIA_FORMAT: "creatomate_text_overlay",
         strategist.CQ_SCHEDULED: "2026-05-28T13:00:00-04:00"},
        # Bad — focus has empty image_count and a media format is set.
        {strategist.CQ_FOCUS_EQUIPMENT: "TES-EMPTY",
         strategist.CQ_MEDIA_FORMAT: "image2_text_overlay",
         strategist.CQ_SCHEDULED: "2026-05-28T18:00:00-04:00"},
        # Good — no focus, media format set (separate check handles video case).
        {strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_SCHEDULED: "2026-05-29T09:00:00-04:00"},
    ]

    kept, warnings = strategist.enforce_image_coverage(posts, catalog_by_id)
    kept_focuses = [p.get(strategist.CQ_FOCUS_EQUIPMENT) for p in kept]

    _check(
        "17. enforce_image_coverage — drops zero-image media posts, keeps the rest",
        len(kept) == 2
        and kept_focuses == ["TES-IMG", ""]
        and len(warnings) == 2
        and any("TES-NOIMG" in w for w in warnings)
        and any("TES-EMPTY" in w for w in warnings),
        f"kept={kept_focuses}, warnings={warnings}",
    )


def test_enforce_equipment_format_requires_focus_video() -> None:
    """Original creatomate_video focus check, now via the broader rule."""
    posts = [
        # Bad — creatomate_video with no focus_equipment_id.
        {strategist.CQ_PLATFORM: "facebook",
         strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "creatomate_video",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-28T18:15:00-04:00"},
        # Good — creatomate_video with a focus equipment.
        {strategist.CQ_PLATFORM: "facebook",
         strategist.CQ_FOCUS_EQUIPMENT: "TES-018",
         strategist.CQ_MEDIA_FORMAT: "creatomate_video",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-29T18:15:00-04:00"},
        # Bad too — image2_enhanced with no focus (broader rule catches this).
        {strategist.CQ_PLATFORM: "instagram",
         strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-30T09:00:00-04:00"},
    ]

    warnings = strategist.enforce_equipment_format_requires_focus(posts)

    bad_video = posts[0]
    good_video = posts[1]
    bad_image = posts[2]

    _check(
        "18. enforce_equipment_format_requires_focus — reassigns video and "
        "no-focus image2_enhanced rows to image2_generated; leaves "
        "video-with-focus alone",
        len(warnings) == 2
        and bad_video[strategist.CQ_MEDIA_FORMAT] == "image2_generated"
        and bad_video[strategist.CQ_TEXT_OVERLAY] == "FALSE"
        and good_video[strategist.CQ_MEDIA_FORMAT] == "creatomate_video"
        # image2_enhanced with empty focus is now reassigned to the
        # image2_generated infographic path (not a media-less dead-end) and
        # still emits a warning so downstream operators see the issue.
        and bad_image[strategist.CQ_MEDIA_FORMAT] == "image2_generated",
        f"warnings={warnings}, bad_video_fmt={bad_video[strategist.CQ_MEDIA_FORMAT]!r}, "
        f"good_video_fmt={good_video[strategist.CQ_MEDIA_FORMAT]!r}, "
        f"bad_image_fmt={bad_image[strategist.CQ_MEDIA_FORMAT]!r}",
    )


def test_enforce_equipment_format_requires_focus_all_equipment_formats() -> None:
    """All four equipment formats — image2_enhanced, image2_text_overlay,
    creatomate_text_overlay, creatomate_video — get reassigned to
    image2_generated (the infographic path) when focus_equipment_id is empty.
    Review formats are untouched (they don't need a source equipment photo)."""
    posts = [
        {strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-28T09:00:00-04:00"},
        {strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "image2_text_overlay",
         strategist.CQ_TEXT_OVERLAY: "TRUE",
         strategist.CQ_SCHEDULED: "2026-05-28T10:00:00-04:00"},
        {strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "creatomate_text_overlay",
         strategist.CQ_TEXT_OVERLAY: "TRUE",
         strategist.CQ_SCHEDULED: "2026-05-28T11:00:00-04:00"},
        {strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "creatomate_video",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-28T12:00:00-04:00"},
        # Review formats don't need focus and must NOT be touched.
        {strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "creatomate_review_image",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-28T13:00:00-04:00"},
        {strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "creatomate_review_video",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-28T14:00:00-04:00"},
    ]

    warnings = strategist.enforce_equipment_format_requires_focus(posts)

    formats_after = [p[strategist.CQ_MEDIA_FORMAT] for p in posts]
    overlay_after = [p[strategist.CQ_TEXT_OVERLAY] for p in posts]

    # All four equipment-format posts → image2_generated (and text_overlay
    # refreshed to FALSE since image2_generated is not a text-overlay format).
    equipment_all_reassigned = (
        formats_after[0] == "image2_generated"
        and formats_after[1] == "image2_generated"
        and formats_after[2] == "image2_generated"
        and formats_after[3] == "image2_generated"
    )
    text_overlay_refreshed = (
        overlay_after[0] == "FALSE"
        and overlay_after[1] == "FALSE"
        and overlay_after[2] == "FALSE"
        and overlay_after[3] == "FALSE"
    )
    review_untouched = (
        formats_after[4] == "creatomate_review_image"
        and formats_after[5] == "creatomate_review_video"
    )

    _check(
        "18b. enforce_equipment_format_requires_focus — all 4 equipment "
        "formats reassigned to image2_generated; text_overlay refreshed; "
        "review formats untouched",
        equipment_all_reassigned
        and text_overlay_refreshed
        and review_untouched
        and len(warnings) == 4,
        f"formats={formats_after}, overlays={overlay_after}, warnings={warnings}",
    )


# ----------------------------------------------------------------------
# image2_generated routing (Build 3A) — infographic path for no-focus rows
# ----------------------------------------------------------------------


def test_enforce_reassigns_to_image2_generated() -> None:
    """A no-focus equipment-format row (image2_enhanced, empty focus) is
    reassigned to image2_generated (the new default) and a warning is emitted."""
    posts = [
        {strategist.CQ_PLATFORM: "facebook",
         strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-28T09:00:00-04:00"},
    ]
    warnings = strategist.enforce_equipment_format_requires_focus(posts)
    _check(
        "36. enforce_equipment_format_requires_focus — no-focus equipment row "
        "reassigned to image2_generated with a warning",
        posts[0][strategist.CQ_MEDIA_FORMAT] == "image2_generated"
        and posts[0][strategist.CQ_TEXT_OVERLAY] == "FALSE"
        and len(warnings) == 1
        and "image2_generated" in warnings[0],
        f"post={posts[0]!r}, warnings={warnings}",
    )


def test_enforce_image2_generated_not_reassigned_again() -> None:
    """A row already on image2_generated with empty focus is NOT reassigned
    (image2_generated is not in EQUIPMENT_MEDIA_FORMATS), so there is no
    infinite-reassign — it is left untouched and emits no warning."""
    posts = [
        {strategist.CQ_PLATFORM: "facebook",
         strategist.CQ_FOCUS_EQUIPMENT: "",
         strategist.CQ_MEDIA_FORMAT: "image2_generated",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-28T09:00:00-04:00"},
    ]
    warnings = strategist.enforce_equipment_format_requires_focus(posts)
    _check(
        "37. enforce_equipment_format_requires_focus — image2_generated row with "
        "empty focus is left alone (no re-reassign, no warning)",
        posts[0][strategist.CQ_MEDIA_FORMAT] == "image2_generated"
        and posts[0][strategist.CQ_TEXT_OVERLAY] == "FALSE"
        and warnings == [],
        f"post={posts[0]!r}, warnings={warnings}",
    )


def test_enforce_focus_equipment_row_untouched() -> None:
    """Regression: an equipment-format row WITH a focus_equipment_id is not
    touched (it has the source photo it needs)."""
    posts = [
        {strategist.CQ_PLATFORM: "facebook",
         strategist.CQ_FOCUS_EQUIPMENT: "TES-018",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_TEXT_OVERLAY: "FALSE",
         strategist.CQ_SCHEDULED: "2026-05-28T09:00:00-04:00"},
    ]
    warnings = strategist.enforce_equipment_format_requires_focus(posts)
    _check(
        "38. enforce_equipment_format_requires_focus — equipment row with a "
        "focus_equipment_id is untouched (no reassign, no warning)",
        posts[0][strategist.CQ_MEDIA_FORMAT] == "image2_enhanced"
        and posts[0][strategist.CQ_FOCUS_EQUIPMENT] == "TES-018"
        and warnings == [],
        f"post={posts[0]!r}, warnings={warnings}",
    )


def test_image2_generated_constant_membership() -> None:
    """Cheap, high-value invariants on the constants that make the routing
    safe: valid format, but not equipment-reassignable, not LLM-offered, and
    not a text-overlay format."""
    f = "image2_generated"
    _check(
        "39. image2_generated constants — in MEDIA_FORMATS; NOT in "
        "EQUIPMENT_MEDIA_FORMATS / LLM_SELECTABLE_MEDIA_FORMATS / "
        "TEXT_OVERLAY_FORMATS",
        f in strategist.MEDIA_FORMATS
        and f not in strategist.EQUIPMENT_MEDIA_FORMATS
        and f not in strategist.LLM_SELECTABLE_MEDIA_FORMATS
        and f not in strategist.TEXT_OVERLAY_FORMATS,
        f"in_media={f in strategist.MEDIA_FORMATS}, "
        f"in_equipment={f in strategist.EQUIPMENT_MEDIA_FORMATS}, "
        f"in_llm={f in strategist.LLM_SELECTABLE_MEDIA_FORMATS}, "
        f"in_overlay={f in strategist.TEXT_OVERLAY_FORMATS}",
    )


def test_llm_menu_excludes_image2_generated(config) -> None:  # type: ignore[no-untyped-def]
    """The LLM-offered media_format menu (built in assemble_prompt) must NOT
    list image2_generated, while a known selectable format (image2_enhanced)
    IS listed."""
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=strategist.ET)
    window_end = datetime(2026, 6, 3, 0, 0, 0, tzinfo=strategist.ET)
    _system, user = strategist.assemble_prompt(
        config=config,
        active_platforms=["facebook"],
        needed={"facebook": 1},
        objective_correction={"summary": "(test summary)"},
        eligible_items=[],
        recent_queue_rows=[],
        strategy_guidance="(strategy)",
        brand_voice="(brand)",
        cta_skill="(cta)",
        platform_style="(platform)",
        window_start=window_start,
        window_end=window_end,
        min_gap_hours=4,
    )
    # Isolate the schema menu line so we assert on the offered list specifically.
    menu_line = next(
        (ln for ln in user.splitlines() if "media_format: one of" in ln), ""
    )
    _check(
        "40. assemble_prompt — media_format menu excludes image2_generated, "
        "includes image2_enhanced",
        menu_line != ""
        and "image2_generated" not in menu_line
        and "image2_enhanced" in menu_line
        # Belt-and-suspenders: the format never appears anywhere in the prompt.
        and "image2_generated" not in user,
        f"menu_line={menu_line!r}",
    )


def test_validate_post_accepts_image2_generated() -> None:
    """Format validation uses the FULL MEDIA_FORMATS set, so a row on
    image2_generated passes the media_format validity check."""
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=strategist.ET)
    window_end = datetime(2026, 6, 3, 0, 0, 0, tzinfo=strategist.ET)
    active = {"facebook", "instagram", "gbp"}
    eligible = {"TES-001", "TES-002"}
    post = {
        "platform": "facebook",
        "scheduled_datetime": "2026-05-28T09:00:00-04:00",
        "objective": "brand_awareness",
        "content_type": "Educational Tip",
        "focus_equipment_id": "",  # no-focus row, allowed
        "angle": "Three things to check before renting a mini excavator",
        "cta_type": "comment",
        "media_format": "image2_generated",
        "draft_notes": "",
    }
    ok, reason = strategist.validate_post(
        post, eligible, active, window_start, window_end
    )
    _check(
        "41. validate_post — accepts image2_generated as a valid media_format",
        ok,
        f"ok={ok}, reason={reason!r}",
    )


def test_derive_text_overlay_image2_generated() -> None:
    """image2_generated bakes the headline into the image (no overlay step), so
    derive_text_overlay returns the non-overlay value — same as image2_enhanced."""
    got = strategist.derive_text_overlay("image2_generated")
    _check(
        "42. derive_text_overlay — image2_generated returns FALSE "
        "(same as image2_enhanced)",
        got == "FALSE"
        and got == strategist.derive_text_overlay("image2_enhanced"),
        f"got={got!r}",
    )


def _make_review_row(
    review_id: str,
    reviewer: str = "Sam",
    usable: str = "TRUE",
    length: str = "120",
    times_used: str = "0",
    excerpt_long: str | None = None,
) -> dict:
    return {
        strategist.REV_REVIEW_ID: review_id,
        strategist.REV_REVIEWER_FIRST_NAME: reviewer,
        strategist.REV_STAR_RATING: "5",
        strategist.REV_REVIEW_TEXT: "Excellent service, very helpful.",
        strategist.REV_REVIEW_LENGTH: length,
        strategist.REV_REVIEW_DATE: "2026-05-01T10:00:00Z",
        strategist.REV_USABLE: usable,
        strategist.REV_EXCERPT_LONG: excerpt_long or "Excellent service, very helpful…",
        strategist.REV_EXCERPT_SHORT: "Excellent service…",
        strategist.REV_LAST_USED_DATE: "",
        strategist.REV_TIMES_USED: times_used,
    }


def test_filter_usable_reviews() -> None:
    rows = [
        _make_review_row("r1", usable="TRUE"),
        _make_review_row("r2", usable="FALSE"),
        _make_review_row("r3", usable="true"),  # case-insensitive
        _make_review_row("r4", usable=""),
        _make_review_row("r5", usable="TRUE"),
    ]
    out = strategist.filter_usable_reviews(rows)
    ids = sorted(r[strategist.REV_REVIEW_ID] for r in out)
    _check(
        "19. filter_usable_reviews — keeps only TRUE (case-insensitive)",
        ids == ["r1", "r3", "r5"],
        f"ids={ids}",
    )


def test_compact_review_line_contains_required_fields() -> None:
    review = _make_review_row(
        "rid-42", reviewer="Pat", length="220", times_used="1",
        excerpt_long="Great team, fair pricing.",
    )
    line = strategist._compact_review_line(review)
    needed = [
        "review_id=rid-42",
        "reviewer=Pat",
        "times_used=1",
        "review_length=220",
        "Great team",  # excerpt content present
    ]
    missing = [n for n in needed if n not in line]
    _check(
        "20. _compact_review_line includes review_id, reviewer, times_used, "
        "review_length, excerpt",
        not missing,
        f"missing fragments: {missing}; line={line!r}",
    )


def test_validate_post_with_review_id() -> None:
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=strategist.ET)
    window_end = datetime(2026, 6, 3, 0, 0, 0, tzinfo=strategist.ET)
    active = {"facebook"}
    eligible_items = {"TES-004"}
    eligible_reviews = {"rev-1", "rev-2"}

    good = {
        "platform": "facebook",
        "scheduled_datetime": "2026-05-28T09:00:00-04:00",
        "objective": "lead_generation",
        "content_type": "Social Proof / Customer Story",
        "focus_equipment_id": "",
        "angle": "Real customer review of the team.",
        "cta_type": "call",
        "media_format": "creatomate_review_image",
        "review_id": "rev-1",
    }
    ok_good, _ = strategist.validate_post(
        good, eligible_items, active, window_start, window_end,
        eligible_review_ids=eligible_reviews,
    )

    # Bad: invented review_id.
    bad = dict(good, review_id="rev-999")
    ok_bad, reason_bad = strategist.validate_post(
        bad, eligible_items, active, window_start, window_end,
        eligible_review_ids=eligible_reviews,
    )

    # OK: review_id empty (validate_post doesn't enforce content-type matching).
    no_rev = dict(good, review_id="")
    ok_no_rev, _ = strategist.validate_post(
        no_rev, eligible_items, active, window_start, window_end,
        eligible_review_ids=eligible_reviews,
    )

    _check(
        "21. validate_post — accepts known review_id, rejects unknown, allows empty",
        ok_good and not ok_bad and ok_no_rev and "rev-999" in reason_bad,
        f"good={ok_good}, bad={ok_bad} ({reason_bad!r}), no_rev={ok_no_rev}",
    )


def test_enforce_review_consistency() -> None:
    posts = [
        # 0: Social Proof + empty review_id → dropped.
        {strategist.CQ_CONTENT_TYPE: "Social Proof / Customer Story",
         strategist.CQ_REVIEW_ID: "",
         strategist.CQ_MEDIA_FORMAT: "creatomate_review_image",
         strategist.CQ_SCHEDULED: "2026-05-28T09:00:00-04:00"},
        # 1: non-Social Proof + non-empty review_id → review_id cleared, kept.
        {strategist.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
         strategist.CQ_REVIEW_ID: "stray-rev",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_SCHEDULED: "2026-05-28T13:00:00-04:00"},
        # 2: non-Social Proof + review media format → format reassigned.
        {strategist.CQ_CONTENT_TYPE: "Educational Tip",
         strategist.CQ_REVIEW_ID: "",
         strategist.CQ_MEDIA_FORMAT: "creatomate_review_image",
         strategist.CQ_SCHEDULED: "2026-05-28T18:00:00-04:00"},
        # 3: Social Proof + non-review media format → format reassigned.
        {strategist.CQ_CONTENT_TYPE: "Social Proof / Customer Story",
         strategist.CQ_REVIEW_ID: "rev-9",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_SCHEDULED: "2026-05-29T09:00:00-04:00"},
        # 4: clean Social Proof — untouched.
        {strategist.CQ_CONTENT_TYPE: "Social Proof / Customer Story",
         strategist.CQ_REVIEW_ID: "rev-10",
         strategist.CQ_MEDIA_FORMAT: "creatomate_review_video",
         strategist.CQ_SCHEDULED: "2026-05-30T09:00:00-04:00"},
    ]

    kept, warnings = strategist.enforce_review_consistency(posts)

    cleared = posts[1][strategist.CQ_REVIEW_ID]
    reassigned_tip = posts[2][strategist.CQ_MEDIA_FORMAT]
    reassigned_sp = posts[3][strategist.CQ_MEDIA_FORMAT]
    untouched = posts[4][strategist.CQ_MEDIA_FORMAT]

    _check(
        "22. enforce_review_consistency — drops empty-Social-Proof, clears stray "
        "review_id, reassigns media formats in both directions",
        len(kept) == 4
        and cleared == ""
        and reassigned_tip == strategist.DEFAULT_EQUIPMENT_MEDIA_FORMAT
        and reassigned_sp == strategist.DEFAULT_REVIEW_MEDIA_FORMAT
        and untouched == "creatomate_review_video"
        and len(warnings) == 4,
        f"kept={len(kept)}, cleared={cleared!r}, tip_fmt={reassigned_tip}, "
        f"sp_fmt={reassigned_sp}, untouched={untouched}, warnings={warnings}",
    )


def test_enforce_image_coverage_skips_review_formats() -> None:
    catalog_by_id = {
        "TES-NOIMG": {
            strategist.CAT_ITEM_ID: "TES-NOIMG",
            strategist.CAT_IMAGE_COUNT: "0",
        },
    }
    posts = [
        # Equipment post on zero-image item is still dropped.
        {strategist.CQ_FOCUS_EQUIPMENT: "TES-NOIMG",
         strategist.CQ_MEDIA_FORMAT: "image2_enhanced",
         strategist.CQ_SCHEDULED: "2026-05-28T09:00:00-04:00"},
        # Review-format post on zero-image item is KEPT (template handles it).
        {strategist.CQ_FOCUS_EQUIPMENT: "TES-NOIMG",
         strategist.CQ_MEDIA_FORMAT: "creatomate_review_image",
         strategist.CQ_SCHEDULED: "2026-05-28T13:00:00-04:00"},
    ]
    kept, warnings = strategist.enforce_image_coverage(posts, catalog_by_id)
    formats_kept = [p[strategist.CQ_MEDIA_FORMAT] for p in kept]
    _check(
        "23. enforce_image_coverage — review formats bypass the zero-image drop",
        formats_kept == ["creatomate_review_image"]
        and len(warnings) == 1,
        f"kept_formats={formats_kept}, warnings={warnings}",
    )


def test_enforce_video_cap_covers_review_video() -> None:
    base = datetime(2026, 5, 27, 9, 0, 0, tzinfo=strategist.ET)
    # 2 creatomate_video + 2 creatomate_review_video on FB → 2 over cap of 2.
    # The third and fourth video on the platform get converted; the first two
    # (whichever sort earliest) stay as videos.
    posts = [
        {"platform": "facebook", "media_format": "creatomate_video",
         "scheduled_datetime": (base + timedelta(days=0)).isoformat()},
        {"platform": "facebook", "media_format": "creatomate_review_video",
         "scheduled_datetime": (base + timedelta(days=1)).isoformat()},
        {"platform": "facebook", "media_format": "creatomate_video",
         "scheduled_datetime": (base + timedelta(days=2)).isoformat()},
        {"platform": "facebook", "media_format": "creatomate_review_video",
         "scheduled_datetime": (base + timedelta(days=3)).isoformat()},
    ]
    out, warnings = strategist.enforce_video_cap(posts, max_per_platform=2)

    formats = [p["media_format"] for p in out]
    video_total = sum(1 for f in formats if f in {"creatomate_video", "creatomate_review_video"})
    # The two converted posts must use the right replacement per original kind.
    has_text_overlay_replacement = "creatomate_text_overlay" in formats
    has_review_image_replacement = "creatomate_review_image" in formats

    _check(
        "24. enforce_video_cap — combined video cap, replacements respect kind",
        video_total == 2
        and has_text_overlay_replacement
        and has_review_image_replacement
        and len(warnings) == 2,
        f"formats={formats}, video_total={video_total}, warnings={warnings}",
    )


# ---------------------------------------------------------------------------
# Group 1 — _format_strategist_slack_summary (pure dict -> str)
# ---------------------------------------------------------------------------
# These assert on stable, load-bearing substrings (header text, counts,
# platform names, the truncation marker) rather than whole-string equality, so
# cosmetic wording tweaks don't break the suite.


def _clean_success_result() -> dict:
    """A result dict matching run()'s success shape with no problems."""
    return {
        "status": "success",
        "posts_planned": 6,
        "planning_window_start": "2026-05-27",
        "planning_window_end": "2026-06-03",
        "by_platform": {"facebook": 3, "instagram": 2, "gbp": 1},
        "by_objective": {"brand_awareness": 4, "lead_generation": 2},
        "by_content_type": {},
        "by_media_format": {},
        "skipped_platforms": [],
        "queue_depth_warnings": [],
        "validation_warnings": [],
        "errors": [],
        "dry_run": False,
    }


def test_slack_summary_clean_success() -> None:
    summary = strategist._format_strategist_slack_summary(_clean_success_result())
    _check(
        "25. slack_summary clean success — Run Complete header, count, window, "
        "by-platform breakdown",
        "Strategist — Run Complete" in summary
        and "ATTENTION" not in summary
        and "Posts planned: 6" in summary
        and "2026-05-27 to 2026-06-03" in summary
        and "facebook: 3" in summary
        and "instagram: 2" in summary,
        f"summary={summary!r}",
    )


def test_slack_summary_skipped_platforms() -> None:
    result = _clean_success_result()
    result["posts_planned"] = 4
    result["by_platform"] = {"gbp": 4}
    result["skipped_platforms"] = ["facebook", "instagram"]
    summary = strategist._format_strategist_slack_summary(result)
    _check(
        "26. slack_summary skipped platforms — ATTENTION header names skipped "
        "platforms",
        "⚠️ Strategist — ATTENTION" in summary
        and "facebook" in summary
        and "instagram" in summary,
        f"summary={summary!r}",
    )


def test_slack_summary_error_shape() -> None:
    # Build the real _error_result shape — it omits the by_* / window / warning
    # keys entirely. This proves the .get(...)-with-defaults defensive reads
    # don't KeyError on the error path.
    result = strategist._error_result(
        datetime(2026, 5, 25, 9, 0, 0, tzinfo=strategist.ET),
        "catalog.spec_sheet_id is empty in config",
        dry_run=True,
    )
    raised = False
    summary = ""
    try:
        summary = strategist._format_strategist_slack_summary(result)
    except Exception as exc:  # noqa: BLE001
        raised = True
        summary = f"{type(exc).__name__}: {exc}"
    _check(
        "27. slack_summary error shape — no KeyError on missing keys, ATTENTION "
        "header, error text surfaced",
        not raised
        and "⚠️ Strategist — ATTENTION" in summary
        and "catalog.spec_sheet_id is empty in config" in summary,
        f"raised={raised}, summary={summary!r}",
    )


def test_slack_summary_validation_warnings_with_posts() -> None:
    result = _clean_success_result()
    result["posts_planned"] = 5
    result["by_platform"] = {"facebook": 5}
    result["validation_warnings"] = [
        "post with empty focus_equipment_id had equipment media_format "
        "reassigned to 'image2_enhanced'",
    ]
    summary = strategist._format_strategist_slack_summary(result)
    _check(
        "28. slack_summary validation warnings with posts planned — ATTENTION "
        "variant surfaces the warning content alongside the planned count",
        "⚠️ Strategist — ATTENTION" in summary
        and "Validation warnings" in summary
        and "reassigned" in summary
        and "Posts planned: 5" in summary,
        f"summary={summary!r}",
    )


def test_slack_summary_truncates_long_warning_lists() -> None:
    # The code truncates each warning/error list to the first 3, then appends a
    # "… and N more" line. Supply 5 to exercise the cutoff.
    warns = [f"warning-number-{i}" for i in range(5)]
    result = _clean_success_result()
    result["posts_planned"] = 3
    result["by_platform"] = {"facebook": 3}
    result["validation_warnings"] = warns
    summary = strategist._format_strategist_slack_summary(result)
    _check(
        "29. slack_summary truncation — first 3 warnings shown, remainder folded "
        "into '… and N more', later entries omitted",
        "warning-number-0" in summary
        and "warning-number-1" in summary
        and "warning-number-2" in summary
        and "warning-number-3" not in summary
        and "warning-number-4" not in summary
        and "… and 2 more" in summary,
        f"summary={summary!r}",
    )


def test_slack_summary_zero_planned_is_problem() -> None:
    # status=success but posts_planned=0 (the "nothing to plan" shape). The
    # code treats zero-planned as a problem condition, so it leads with
    # ATTENTION even though no warning lists are populated.
    result = _clean_success_result()
    result["posts_planned"] = 0
    result["by_platform"] = {"facebook": 0, "instagram": 0, "gbp": 0}
    summary = strategist._format_strategist_slack_summary(result)
    _check(
        "30. slack_summary zero planned, no other problems — ATTENTION "
        "(zero-planned is itself a problem condition per the code)",
        "⚠️ Strategist — ATTENTION" in summary
        and "Posts planned: 0" in summary,
        f"summary={summary!r}",
    )


# ---------------------------------------------------------------------------
# Group 2 — _post_health_summary (side-effecting; the Slack post is faked)
# ---------------------------------------------------------------------------
# slack_helpers.post_message is monkeypatched onto the strategist module's
# reference and restored in a finally block so fakes never leak between tests.
# config is a minimal stub mirroring Config.get(key, default).


class _FakeConfig:
    """Minimal Config stand-in: .get(key, default) over a dict."""

    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key: str, default=None):  # type: ignore[no-untyped-def]
        return self._values.get(key, default)


def test_post_health_summary_dry_run_no_post() -> None:
    calls: list = []
    real = strategist.slack_helpers.post_message
    strategist.slack_helpers.post_message = (
        lambda *a, **k: calls.append((a, k))  # type: ignore[assignment]
    )
    try:
        cfg = _FakeConfig({"health.slack_channel": "#system-health"})
        err = io.StringIO()
        with redirect_stderr(err):
            strategist._post_health_summary(
                {"status": "success", "posts_planned": 3}, cfg, dry_run=True
            )
    finally:
        strategist.slack_helpers.post_message = real
    _check(
        "31. _post_health_summary dry-run — never posts to Slack",
        len(calls) == 0,
        f"calls={calls}",
    )


def test_post_health_summary_posts_once() -> None:
    calls: list = []
    real = strategist.slack_helpers.post_message
    strategist.slack_helpers.post_message = (
        lambda channel, text, *a, **k: calls.append((channel, text))  # type: ignore[assignment]
    )
    try:
        cfg = _FakeConfig({"health.slack_channel": "#system-health"})
        result = {
            "status": "success",
            "posts_planned": 4,
            "by_platform": {"facebook": 4},
        }
        expected = strategist._format_strategist_slack_summary(result)
        strategist._post_health_summary(result, cfg, dry_run=False)
    finally:
        strategist.slack_helpers.post_message = real
    _check(
        "32. _post_health_summary non-dry-run — posts exactly once to the "
        "configured channel with the formatted summary text",
        len(calls) == 1
        and calls[0][0] == "#system-health"
        and calls[0][1] == expected,
        f"calls={calls!r}",
    )


def test_post_health_summary_empty_channel_skips() -> None:
    calls: list = []
    real = strategist.slack_helpers.post_message
    strategist.slack_helpers.post_message = (
        lambda *a, **k: calls.append(a)  # type: ignore[assignment]
    )
    raised = False
    err = io.StringIO()
    try:
        cfg = _FakeConfig({})  # no health.slack_channel
        try:
            with redirect_stderr(err):
                strategist._post_health_summary(
                    {"status": "success", "posts_planned": 2}, cfg, dry_run=False
                )
        except Exception:  # noqa: BLE001
            raised = True
    finally:
        strategist.slack_helpers.post_message = real
    _check(
        "33. _post_health_summary empty channel — no post, no raise, WARN logged",
        len(calls) == 0 and not raised and "slack_channel" in err.getvalue(),
        f"calls={calls}, raised={raised}, stderr={err.getvalue()!r}",
    )


def test_post_health_summary_swallows_slack_failure() -> None:
    real = strategist.slack_helpers.post_message

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    strategist.slack_helpers.post_message = _boom  # type: ignore[assignment]
    raised = False
    err = io.StringIO()
    try:
        cfg = _FakeConfig({"health.slack_channel": "#system-health"})
        try:
            with redirect_stderr(err):
                strategist._post_health_summary(
                    {"status": "success", "posts_planned": 1}, cfg, dry_run=False
                )
        except Exception:  # noqa: BLE001
            raised = True
    finally:
        strategist.slack_helpers.post_message = real
    _check(
        "34. _post_health_summary — Slack exception is swallowed (a Slack outage "
        "can never fail a planning run)",
        not raised and "Slack post failed" in err.getvalue(),
        f"raised={raised}, stderr={err.getvalue()!r}",
    )


# ---------------------------------------------------------------------------
# Group 3 — run() single-exit wrapper integrity (no API, no live Slack)
# ---------------------------------------------------------------------------


def test_run_wrapper_threads_result_and_dry_run() -> None:
    # Monkeypatch the planning pass to a canned success dict and the Slack post
    # to a recorder, then call the public run(dry_run=True). The wrapper must
    # return the canned dict unchanged and, under dry-run, post nothing live.
    canned = {
        "status": "success",
        "posts_planned": 7,
        "dry_run": True,
        "by_platform": {"facebook": 7},
    }
    calls: list = []
    real_plan = strategist._run_planning
    real_post = strategist.slack_helpers.post_message
    strategist._run_planning = (
        lambda dry_run, config: dict(canned)  # type: ignore[assignment]
    )
    strategist.slack_helpers.post_message = (
        lambda *a, **k: calls.append((a, k))  # type: ignore[assignment]
    )
    try:
        err = io.StringIO()
        with redirect_stderr(err):
            out = strategist.run(dry_run=True)
    finally:
        strategist._run_planning = real_plan
        strategist.slack_helpers.post_message = real_post
    _check(
        "35. run() wrapper — returns the planning result unchanged and threads "
        "dry_run (no live Slack post)",
        out == canned and len(calls) == 0,
        f"out={out!r}, calls={calls}",
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
    test_compact_catalog_line_includes_specs()
    test_compact_catalog_line_omits_empty_specs()
    test_validate_angle_grounding_tail_swing()
    test_validate_angle_grounding_numeric()
    test_apply_angle_grounding_check_replaces_and_warns()
    test_compact_catalog_line_flags_zero_image_items()
    test_enforce_image_coverage_drops_zero_image_media_posts()
    test_enforce_equipment_format_requires_focus_video()
    test_enforce_equipment_format_requires_focus_all_equipment_formats()
    # image2_generated routing (Build 3A).
    test_enforce_reassigns_to_image2_generated()
    test_enforce_image2_generated_not_reassigned_again()
    test_enforce_focus_equipment_row_untouched()
    test_image2_generated_constant_membership()
    test_llm_menu_excludes_image2_generated(config)
    test_validate_post_accepts_image2_generated()
    test_derive_text_overlay_image2_generated()
    test_filter_usable_reviews()
    test_compact_review_line_contains_required_fields()
    test_validate_post_with_review_id()
    test_enforce_review_consistency()
    test_enforce_image_coverage_skips_review_formats()
    test_enforce_video_cap_covers_review_video()
    # Group 1 — Slack health summary formatting (pure function).
    test_slack_summary_clean_success()
    test_slack_summary_skipped_platforms()
    test_slack_summary_error_shape()
    test_slack_summary_validation_warnings_with_posts()
    test_slack_summary_truncates_long_warning_lists()
    test_slack_summary_zero_planned_is_problem()
    # Group 2 — _post_health_summary (Slack post faked, no live call).
    test_post_health_summary_dry_run_no_post()
    test_post_health_summary_posts_once()
    test_post_health_summary_empty_channel_skips()
    test_post_health_summary_swallows_slack_failure()
    # Group 3 — run() single-exit wrapper integrity (no API, no live Slack).
    test_run_wrapper_threads_result_and_dry_run()

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
