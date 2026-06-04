"""Tests for agents/drafter.py.

The first 8 tests are deterministic and run without any external API calls.
Test 9 is an integration test that calls the live Anthropic API against
live Google Sheets — it COSTS MONEY and should not run in CI. The dry-run
flag ensures no writes happen to the Content Queue.

Run from the project root:
    python -m tests.test_drafter            # deterministic only
    python -m tests.test_drafter --live      # also runs test 9
"""

from __future__ import annotations

import io
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

# Snapshot whether we are running under pytest BEFORE our own guarded import of
# pytest below (which would otherwise put "pytest" in sys.modules and fool the
# check). Under a real pytest run, pytest has already imported this module, so
# "pytest" is in sys.modules here. Under the native runner it is not.
_UNDER_PYTEST = "pytest" in sys.modules

try:
    import pytest  # type: ignore
except ImportError:  # native runner must work without pytest installed
    pytest = None  # type: ignore

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from agents import critic, drafter  # noqa: E402
from tools import creatomate_helpers, sheets_helpers  # noqa: E402
from tools.config_loader import Config, load_config  # noqa: E402


_FAILURES: list[tuple[str, str]] = []
_PASSED = 0


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  PASS  {name}")
    else:
        # Under pytest, raise so the failure is reported by the test runner.
        # The native runner accumulates and never raises.
        if _UNDER_PYTEST:
            raise AssertionError(f"{name} — {detail}")
        _FAILURES.append((name, detail))
        print(f"  FAIL  {name} — {detail}")


# ----------------------------------------------------------------------
# Deterministic tests
# ----------------------------------------------------------------------

def test_caption_assembly() -> None:
    hook = "Tight residential lot? Zero tail swing changes the game on jobsites."
    body = "Reaches the back corner without bumping the fence.\n\nLeaves grass and pavers untouched."
    cta = "Call 904-452-0888 to book."
    full = drafter.assemble_caption(hook, body, cta)
    expected = hook + "\n\n" + body + "\n\n" + cta
    _check(
        "1. caption_assembly — hook + body + cta joined by \\n\\n",
        full == expected,
        f"expected={expected!r}, got={full!r}",
    )


def test_caption_assembly_no_cta() -> None:
    hook = "Heres what real jobsite reach looks like."
    body = "12 feet of dig depth.\n\nNo more reaching for the spoils pile."
    full = drafter.assemble_caption(hook, body, "")
    expected = hook + "\n\n" + body
    no_trailing_sep = not full.endswith("\n\n")
    _check(
        "2. caption_assembly_no_cta — hook + body only, no trailing separator",
        full == expected and no_trailing_sep,
        f"got={full!r}",
    )


def test_overlay_hook_validation_valid() -> None:
    ok_3, reason_3 = drafter.validate_overlay_hook("Zero tail swing wins", "TRUE")
    ok_7, reason_7 = drafter.validate_overlay_hook(
        "Tight lot, full reach, no swing damage", "TRUE",
    )
    _check(
        "3. overlay_hook validation — 3 to 7 words passes when TRUE",
        ok_3 and ok_7,
        f"3-word: ok={ok_3} reason={reason_3!r}; "
        f"7-word: ok={ok_7} reason={reason_7!r}",
    )


def test_overlay_hook_validation_too_long() -> None:
    too_long = "This hook has eight words which exceeds the limit"
    assert drafter.count_words(too_long) == 9, drafter.count_words(too_long)
    ok, reason = drafter.validate_overlay_hook(too_long, "TRUE")
    _check(
        "4. overlay_hook validation — 8+ words fails",
        (not ok) and "3-7" in reason,
        f"ok={ok}, reason={reason!r}, word_count={drafter.count_words(too_long)}",
    )


def test_overlay_hook_validation_empty_when_required() -> None:
    ok, reason = drafter.validate_overlay_hook("", "TRUE")
    _check(
        "5. overlay_hook validation — empty fails when TRUE",
        (not ok) and "empty" in reason.lower(),
        f"ok={ok}, reason={reason!r}",
    )


def test_overlay_hook_validation_empty_when_not_required() -> None:
    ok, reason = drafter.validate_overlay_hook("", "FALSE")
    # Also test that a populated hook fails when FALSE.
    ok_bad, reason_bad = drafter.validate_overlay_hook("Some hook here", "FALSE")
    _check(
        "6. overlay_hook validation — empty passes when FALSE, "
        "populated fails when FALSE",
        ok and (not ok_bad) and "FALSE" in reason_bad,
        f"empty: ok={ok} reason={reason!r}; "
        f"populated: ok={ok_bad} reason={reason_bad!r}",
    )


def test_overlay_hook_validation_video_format_allows_any_hook() -> None:
    """When text_overlay=FALSE and media_format is a video format, a populated
    hook is allowed (Creatomate video templates need Hook-Text regardless of
    the image-overlay flag). Non-video formats still reject populated hooks
    when text_overlay=FALSE."""
    # Video format + populated hook (any length): allowed
    ok_short, _ = drafter.validate_overlay_hook(
        "Tight site reach", "FALSE", "creatomate_video",
    )
    ok_long, _ = drafter.validate_overlay_hook(
        "Plenty of words that would normally exceed seven words limit",
        "FALSE",
        "creatomate_video",
    )
    ok_empty, _ = drafter.validate_overlay_hook(
        "", "FALSE", "creatomate_video",
    )
    ok_review_video, _ = drafter.validate_overlay_hook(
        "Real feedback from a real rental",
        "FALSE",
        "creatomate_review_video",
    )
    # Non-video format with populated hook + FALSE still fails:
    ok_bad, reason_bad = drafter.validate_overlay_hook(
        "Tight site reach", "FALSE", "image2_enhanced",
    )
    _check(
        "6b. overlay_hook validation — video formats allow any hook when FALSE; "
        "non-video formats still reject populated hooks",
        ok_short and ok_long and ok_empty and ok_review_video
        and (not ok_bad) and "FALSE" in reason_bad,
        f"video_short={ok_short}, video_long={ok_long}, "
        f"video_empty={ok_empty}, review_video={ok_review_video}, "
        f"non_video_populated={ok_bad} ({reason_bad!r})",
    )


def test_derive_video_hook_from_caption() -> None:
    """Prefers caption_hook, takes the first clause, caps at max_words."""
    short = drafter._derive_video_hook(
        caption_hook="Tight residential lot? Zero tail swing changes the game.",
        angle="(angle text)",
    )
    long = drafter._derive_video_hook(
        caption_hook="One single very long opening clause without sentence punctuation here",
        angle="(angle text)",
    )
    _check(
        "6c. _derive_video_hook — caption_hook first clause, truncates to 7 words",
        short == "Tight residential lot"
        and long == "One single very long opening clause without",
        f"short={short!r}, long={long!r}",
    )


def test_derive_video_hook_falls_back_to_angle() -> None:
    """Falls back to angle when caption_hook is empty."""
    derived = drafter._derive_video_hook(
        caption_hook="",
        angle="Show the machine on a tight residential lot. With clay soil.",
    )
    _check(
        "6d. _derive_video_hook — falls back to angle when caption_hook empty",
        derived == "Show the machine on a tight residential",
        f"got={derived!r}",
    )


def test_derive_video_hook_strips_banned_chars() -> None:
    """Strips '!' and em-dash from the derived hook per brand voice rules."""
    derived = drafter._derive_video_hook(
        caption_hook="Wow! What a machine!",
        angle="",
    )
    derived_em = drafter._derive_video_hook(
        caption_hook="Real reach — no excuses",
        angle="",
    )
    _check(
        "6e. _derive_video_hook — strips banned chars (! and em-dash)",
        derived == "Wow What a machine"
        and derived_em == "Real reach no excuses",
        f"banged={derived!r}, em_dashed={derived_em!r}",
    )


def test_derive_video_hook_empty_input() -> None:
    """Both inputs empty returns empty string (no fallback to magic text)."""
    derived = drafter._derive_video_hook(caption_hook="", angle="")
    _check(
        "6f. _derive_video_hook — empty caption and angle returns empty string",
        derived == "",
        f"got={derived!r}",
    )


def test_banned_language_check() -> None:
    clean = (
        "Compact track loader handles tight lots, soft ground, and mixed material. "
        "Built for the kind of small-site work that wears bigger machines out."
    )
    bad_phrase = "This skid steer is an absolute game changer for your worksite."
    bad_phrase_alt = "We're a one-stop shop for all your equipment rental needs."

    clean_hits = drafter.contains_banned_language(clean)
    bad_hits = drafter.contains_banned_language(bad_phrase)
    bad_alt_hits = drafter.contains_banned_language(bad_phrase_alt)

    _check(
        "7. banned_language check — clean passes, 'game changer' / "
        "'one-stop shop' caught",
        clean_hits == []
        and "game changer" in bad_hits
        and "one-stop shop" in bad_alt_hits,
        f"clean_hits={clean_hits}, bad_hits={bad_hits}, "
        f"bad_alt_hits={bad_alt_hits}",
    )


def test_platform_char_limit() -> None:
    short = "x" * 1000
    long_ig = "x" * 2300  # over IG's 2200 cap
    long_fb = "x" * 1000  # well under FB's 63206 cap

    ok_short, _ = drafter.validate_caption_length(short, "instagram")
    ok_long_ig, reason_ig = drafter.validate_caption_length(long_ig, "instagram")
    ok_long_fb, _ = drafter.validate_caption_length(long_fb, "facebook")
    ok_unknown, _ = drafter.validate_caption_length(short, "tiktok")

    _check(
        "8. platform char limit — IG over limit fails, under-limit passes, "
        "unknown platform fails",
        ok_short and (not ok_long_ig) and "2200" in reason_ig
        and ok_long_fb and (not ok_unknown),
        f"short_ig={ok_short}, long_ig={ok_long_ig} ({reason_ig!r}), "
        f"long_fb={ok_long_fb}, unknown={ok_unknown}",
    )


def test_build_drafter_messages_anti_fabrication() -> None:
    """The system message must explicitly forbid invented stories, prices,
    and local statistics. Locks in the four anti-fabrication rules so a
    future prompt edit can't silently drop them.
    """
    config = load_config()
    row = {
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        drafter.CQ_CTA_TYPE: "save",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "image2_enhanced",
        drafter.CQ_ANGLE: "Show the machine on a tight residential lot.",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "",
    }
    system_msg, user_msg = drafter.build_drafter_messages(
        row=row,
        catalog_item=None,
        config=config,
        brand_voice="(brand voice text)",
        hook_skill="(hook skill text)",
        cta_skill="(cta skill text)",
        platform_style="(platform style text)",
        few_shot_library="(few shot library text)",
        strategy_guidance="(strategy guidance text)",
    )

    sys_lower = system_msg.lower()
    user_lower = user_msg.lower()

    # Each of the four anti-fabrication rules must show up by signature phrase
    # in the system message.
    required_phrases = [
        "anti-fabrication",
        "customer stories",         # rule 1: no invented stories
        "dollar amounts",           # rule 2: no invented prices
        "local statistics",         # rule 3: no invented local stats
        "specific factual claim",   # rule 4: trace-back requirement
    ]
    missing_sys = [p for p in required_phrases if p not in sys_lower]

    # And the user-message critical-instructions section must carry a
    # reinforcement line (we want this rule echoed at the prompt tail).
    user_reinforced = "anti-fabrication" in user_lower

    _check(
        "9. build_drafter_messages — anti-fabrication rules present in "
        "system message + reinforced in user message",
        not missing_sys and user_reinforced,
        f"missing_sys={missing_sys}, user_reinforced={user_reinforced}",
    )


# ----------------------------------------------------------------------
# Review media pipeline (deterministic, mocks creatomate/sheets helpers)
# ----------------------------------------------------------------------


def test_find_review_by_id() -> None:
    rows = [
        {"review_id": "abc", "reviewer_first_name": "Carrie"},
        {"review_id": "def", "reviewer_first_name": "Mike"},
    ]
    found = drafter._find_review_by_id(rows, "def")
    missing = drafter._find_review_by_id(rows, "xyz")
    empty = drafter._find_review_by_id(rows, "")
    _check(
        "10. _find_review_by_id — finds match, returns None for missing/empty",
        found is not None
        and found.get("reviewer_first_name") == "Mike"
        and missing is None
        and empty is None,
        f"found={found!r}, missing={missing!r}, empty={empty!r}",
    )


def test_render_review_image_builds_modifications() -> None:
    """_render_review_image passes Review-Text / Reviewer-Name / Star-Rating
    and omits Equipment-Photo when the template doesn't list it."""
    captured: dict = {}

    def fake_render(template_id, modifications, output_path, **kwargs):
        captured["template_id"] = template_id
        captured["modifications"] = modifications
        return {
            "success": True, "output_path": output_path,
            "render_id": "test", "error": "",
        }

    config = Config({
        "creatomate": {
            "review_image": {
                "templates": {
                    "bold_quote_card": {
                        "id": "tpl-bold-quote-123",
                        "name": "Bold Quote Card",
                    },
                },
            },
        },
    })
    review = {
        "review_id": "rev-001",
        "reviewer_first_name": "Carrie",
        "excerpt_long": "Great rental, machine was clean and ready.",
        "review_text": "Long full review text (not the excerpt — not used here).",
    }

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        result = drafter._render_review_image(
            row_id="TEST-001",
            review_data=review,
            source_image_url="",
            config=config,
        )
    finally:
        creatomate_helpers.render_template = orig

    mods = captured.get("modifications", {})
    _check(
        "11. _render_review_image — Review-Text/Reviewer-Name/Star-Rating "
        "set; no Equipment-Photo when template lacks extra_dynamic_fields",
        result.get("success") is True
        and captured.get("template_id") == "tpl-bold-quote-123"
        and mods.get("Review-Text", {}).get("text")
            == "Great rental, machine was clean and ready."
        and mods.get("Reviewer-Name", {}).get("text") == "Carrie"
        and mods.get("Star-Rating", {}).get("text") == "★★★★★"
        and "Equipment-Photo" not in mods,
        f"result={result!r}, mods={mods!r}",
    )


def test_render_review_image_includes_equipment_photo_when_supported() -> None:
    """Equipment-Photo populated only when the template lists it in
    extra_dynamic_fields AND a source URL is available."""
    captured: dict = {}

    def fake_render(template_id, modifications, output_path, **kwargs):
        captured["modifications"] = modifications
        return {
            "success": True, "output_path": output_path,
            "render_id": "x", "error": "",
        }

    config = Config({
        "creatomate": {
            "review_image": {
                "templates": {
                    "photo_testimonial": {
                        "id": "tpl-photo-testimonial",
                        "name": "Photo Testimonial",
                        "extra_dynamic_fields": ["Equipment-Photo"],
                    },
                },
            },
        },
    })
    review = {
        "review_id": "rev-002",
        "reviewer_first_name": "Carrie",
        "excerpt_long": "Solid rental",
    }
    source_url = "https://drive.google.com/uc?id=ABC&export=download"

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        drafter._render_review_image(
            row_id="TEST-002",
            review_data=review,
            source_image_url=source_url,
            config=config,
        )
    finally:
        creatomate_helpers.render_template = orig

    mods = captured.get("modifications", {})
    _check(
        "12. _render_review_image — Equipment-Photo populated on "
        "photo_testimonial when source URL provided",
        mods.get("Equipment-Photo", {}).get("source") == source_url,
        f"mods={mods!r}",
    )


def test_render_review_video_builds_modifications() -> None:
    """_render_review_video sends Review-Text + Reviewer-Name only (no
    Star-Rating — video templates use individual Star-N elements)."""
    captured: dict = {}

    def fake_render(template_id, modifications, output_path, **kwargs):
        captured["template_id"] = template_id
        captured["modifications"] = modifications
        captured["output_path"] = output_path
        return {
            "success": True, "output_path": output_path,
            "render_id": "v", "error": "",
        }

    config = Config({
        "creatomate": {
            "review_video": {
                "templates": {
                    "star_cascade": {
                        "id": "video-tpl-1",
                        "name": "Star Cascade",
                    },
                },
            },
        },
    })
    review = {
        "review_id": "rev-003",
        "reviewer_first_name": "Mike",
        "excerpt_long": "Excellent service",
    }

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        result = drafter._render_review_video(
            row_id="TEST-003",
            review_data=review,
            source_image_url="",
            config=config,
        )
    finally:
        creatomate_helpers.render_template = orig

    mods = captured.get("modifications", {})
    _check(
        "13. _render_review_video — Review-Text/Reviewer-Name only; "
        "no Star-Rating; output path ends .mp4",
        result.get("success") is True
        and captured.get("template_id") == "video-tpl-1"
        and mods.get("Review-Text", {}).get("text") == "Excellent service"
        and mods.get("Reviewer-Name", {}).get("text") == "Mike"
        and "Star-Rating" not in mods
        and captured.get("output_path", "").endswith(".mp4"),
        f"result={result!r}, mods={mods!r}, "
        f"output_path={captured.get('output_path')!r}",
    )


def test_generate_review_media_video_falls_back_to_image() -> None:
    """When creatomate_review_video render fails, fall back to
    creatomate_review_image with the same review data."""
    call_log: list[str] = []

    def fake_render(template_id, modifications, output_path, **kwargs):
        if output_path.endswith(".mp4"):
            call_log.append("video-fail")
            return {
                "success": False, "output_path": "",
                "render_id": "", "error": "simulated video failure",
            }
        call_log.append("image-ok")
        return {
            "success": True, "output_path": output_path,
            "render_id": "x", "error": "",
        }

    config = Config({
        "creatomate": {
            "review_video": {"templates": {"star_cascade": {"id": "vid"}}},
            "review_image": {"templates": {"bold_quote_card": {"id": "img"}}},
        },
    })
    review = {"review_id": "x", "reviewer_first_name": "Y", "excerpt_long": "Z"}

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        result = drafter._generate_review_media(
            row={"row_id": "TEST-004"},
            media_format="creatomate_review_video",
            review_data=review,
            image_id="",
            config=config,
        )
    finally:
        creatomate_helpers.render_template = orig

    chain = result.get("fallback_chain", [])
    _check(
        "14. _generate_review_media — review_video failure falls back to review_image",
        result.get("success") is True
        and result.get("media_format_used") == "creatomate_review_image"
        and call_log == ["video-fail", "image-ok"]
        and len(chain) == 1
        and chain[0][0] == "creatomate_review_video",
        f"result={result!r}, call_log={call_log!r}",
    )


def test_generate_review_media_image_no_fallback() -> None:
    """creatomate_review_image failure does NOT trigger a fallback — it
    doesn't need a source photo, so there's nothing to fall back to."""
    call_log: list[str] = []

    def fake_render(template_id, modifications, output_path, **kwargs):
        call_log.append(output_path)
        return {
            "success": False, "output_path": "",
            "render_id": "", "error": "simulated failure",
        }

    config = Config({
        "creatomate": {
            "review_image": {"templates": {"bold_quote_card": {"id": "img"}}},
        },
    })
    review = {"review_id": "x", "reviewer_first_name": "Y", "excerpt_long": "Z"}

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        result = drafter._generate_review_media(
            row={"row_id": "TEST-005"},
            media_format="creatomate_review_image",
            review_data=review,
            image_id="",
            config=config,
        )
    finally:
        creatomate_helpers.render_template = orig

    _check(
        "15. _generate_review_media — review_image failure has no fallback (1 attempt only)",
        result.get("success") is False
        and len(call_log) == 1
        and len(result.get("fallback_chain", [])) == 1,
        f"result={result!r}, call_log={call_log!r}",
    )


def test_generate_review_media_missing_review_data() -> None:
    """When review_data is None, skip media generation and report it
    without calling Creatomate."""
    call_log: list[str] = []

    def fake_render(*args, **kwargs):
        call_log.append("called")
        return {"success": True, "output_path": "", "render_id": "", "error": ""}

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        result = drafter._generate_review_media(
            row={"row_id": "TEST-006"},
            media_format="creatomate_review_image",
            review_data=None,
            image_id="",
            config=Config({}),
        )
    finally:
        creatomate_helpers.render_template = orig

    _check(
        "16. _generate_review_media — None review_data returns failure "
        "without calling Creatomate",
        result.get("success") is False
        and "no review data" in result.get("error", "").lower()
        and call_log == [],
        f"result={result!r}, call_log={call_log!r}",
    )


def test_update_review_usage_writes_correct_cells() -> None:
    """update_review_usage finds the row by review_id and writes incremented
    times_used + today's last_used_date."""
    captured: dict = {}

    def fake_find(spreadsheet_id, tab_name, column_name, value, service=None):
        return [(7, {
            "review_id": "rev-001",
            "times_used": "2",
            "last_used_date": "2026-04-01",
        })]

    def fake_update(spreadsheet_id, tab_name, row_number, col_updates, service=None):
        captured["spreadsheet_id"] = spreadsheet_id
        captured["tab_name"] = tab_name
        captured["row_number"] = row_number
        captured["col_updates"] = col_updates

    orig_find = sheets_helpers.find_rows_by_column_value
    orig_update = sheets_helpers.update_cells
    sheets_helpers.find_rows_by_column_value = fake_find
    sheets_helpers.update_cells = fake_update
    try:
        ok = drafter.update_review_usage(
            review_id="rev-001",
            reviews_sheet_id="sheet-123",
            reviews_tab="Reviews",
            service=None,
        )
    finally:
        sheets_helpers.find_rows_by_column_value = orig_find
        sheets_helpers.update_cells = orig_update

    updates = captured.get("col_updates", {})
    last_used = updates.get("last_used_date", "")
    _check(
        "17. update_review_usage — increments times_used, sets today's last_used_date",
        ok is True
        and captured.get("spreadsheet_id") == "sheet-123"
        and captured.get("tab_name") == "Reviews"
        and captured.get("row_number") == 7
        and updates.get("times_used") == "3"
        and len(last_used) == 10  # ISO YYYY-MM-DD
        and last_used.count("-") == 2,
        f"ok={ok}, captured={captured!r}",
    )


def test_update_review_usage_missing_review() -> None:
    """When review_id not in sheet, return False without writing."""
    write_calls: list = []

    def fake_find(spreadsheet_id, tab_name, column_name, value, service=None):
        return []

    def fake_update(*args, **kwargs):
        write_calls.append(args)

    orig_find = sheets_helpers.find_rows_by_column_value
    orig_update = sheets_helpers.update_cells
    sheets_helpers.find_rows_by_column_value = fake_find
    sheets_helpers.update_cells = fake_update
    try:
        ok = drafter.update_review_usage(
            review_id="missing-id",
            reviews_sheet_id="sheet-123",
            reviews_tab="Reviews",
            service=None,
        )
    finally:
        sheets_helpers.find_rows_by_column_value = orig_find
        sheets_helpers.update_cells = orig_update

    _check(
        "18. update_review_usage — missing review returns False, no write",
        ok is False and write_calls == [],
        f"ok={ok}, write_calls={write_calls!r}",
    )


def test_creative_hook_text_validation_max_words() -> None:
    """creative_hook_text must be ≤ 7 words. 8+ words fails."""
    ok_short, _ = drafter.validate_creative_hook_text(
        "Tight site reach", "Caption hook is a different idea.",
    )
    ok_seven, _ = drafter.validate_creative_hook_text(
        "Real reach without tearing up the lot",
        "Caption hook is a different idea.",
    )
    too_long = "This hook has eight clear words which exceeds"
    ok_long, reason_long = drafter.validate_creative_hook_text(
        too_long, "Caption hook is a different idea.",
    )
    ok_empty, reason_empty = drafter.validate_creative_hook_text(
        "", "Some caption hook.",
    )
    _check(
        "20. validate_creative_hook_text — 1-7 words pass, 8+ fails, empty fails",
        ok_short and ok_seven
        and (not ok_long) and "7" in reason_long
        and (not ok_empty) and "empty" in reason_empty.lower(),
        f"short={ok_short}, seven={ok_seven}, long_ok={ok_long} ({reason_long!r}), "
        f"empty_ok={ok_empty} ({reason_empty!r})",
    )


def test_creative_hook_text_validation_distinct_from_caption() -> None:
    """creative_hook_text must not be a substring of caption_hook
    (or vice versa), case-insensitive."""
    # Identical → fail.
    ok_identical, reason_id = drafter.validate_creative_hook_text(
        "Tight site reach", "Tight site reach",
    )
    # creative is substring of caption → fail.
    ok_sub_a, reason_a = drafter.validate_creative_hook_text(
        "Tight site reach",
        "Tight site reach without tearing up your lawn.",
    )
    # caption is substring of creative → fail.
    ok_sub_b, reason_b = drafter.validate_creative_hook_text(
        "Real reach. Tight site. No swing damage",
        "Real reach",
    )
    # Different phrasing → pass.
    ok_distinct, _ = drafter.validate_creative_hook_text(
        "Zero swing wins",
        "Watch the back fence stay intact while we dig.",
    )
    # Empty caption_hook should not block creative (validation handled by
    # caption_hook=empty check elsewhere).
    ok_empty_caption, _ = drafter.validate_creative_hook_text(
        "Zero swing wins", "",
    )
    _check(
        "21. validate_creative_hook_text — substring overlap with caption_hook "
        "fails in both directions; distinct phrasing passes",
        (not ok_identical) and "overlap" in reason_id.lower()
        and (not ok_sub_a) and "overlap" in reason_a.lower()
        and (not ok_sub_b) and "overlap" in reason_b.lower()
        and ok_distinct and ok_empty_caption,
        f"identical={ok_identical}, sub_a={ok_sub_a}, sub_b={ok_sub_b}, "
        f"distinct={ok_distinct}, empty_caption={ok_empty_caption}",
    )


def test_validate_llm_output_requires_creative_hook_text() -> None:
    """validate_llm_output flags missing/invalid creative_hook_text as a
    validation issue."""
    base = {
        "caption_hook": "Open with the angle here.",
        "caption_body": "Body content line one.\n\nLine two.",
        "cta_text": "",
        "image_overlay_hook": "",
        "first_comment": "",
        "draft_rationale": "rationale",
    }

    # Missing creative_hook_text.
    parsed_missing = dict(base)
    issues_missing, _ = drafter.validate_llm_output(
        parsed_missing, "FALSE", "facebook", "brand_awareness",
        "save", "image2_enhanced",
    )
    has_missing_issue = any("creative_hook_text" in i.lower() for i in issues_missing)

    # Valid creative_hook_text passes the creative-hook check.
    parsed_ok = dict(base, creative_hook_text="Zero swing wins")
    issues_ok, _ = drafter.validate_llm_output(
        parsed_ok, "FALSE", "facebook", "brand_awareness",
        "save", "image2_enhanced",
    )
    has_no_creative_issue = not any(
        "creative_hook_text" in i.lower() for i in issues_ok
    )

    _check(
        "22. validate_llm_output — flags missing creative_hook_text; clean "
        "creative_hook_text passes",
        has_missing_issue and has_no_creative_issue,
        f"missing_issues={issues_missing}, ok_issues={issues_ok}",
    )


def test_validate_creative_hook_allows_excerpt_sourced_overlap() -> None:
    """validate_creative_hook_text permits a creative/caption overlap when the
    overlapping (contained) hook is a substring of the selected review_excerpt.
    Reproduces the live Larry case — faithful quotation of the review."""
    excerpt = (
        "When my skid steer died mid-job, I figured just go ahead and call "
        "T.E.S. Rentals — best decision all week."
    )
    ok, reason = drafter.validate_creative_hook_text(
        "Just go ahead and call",
        "Just go ahead and call us today for a quote",
        review_excerpt=excerpt,
    )
    _check(
        "61. validate_creative_hook_text — overlap allowed when overlapping "
        "phrase is a substring of the selected review_excerpt",
        ok,
        f"ok={ok}, reason={reason!r}",
    )


def test_validate_creative_hook_blocks_non_excerpt_overlap() -> None:
    """validate_creative_hook_text still flags the same overlap when the phrase
    is NOT in the selected excerpt (empty or unrelated). Non-quoted overlap
    stays blocked."""
    # Empty excerpt → identical to pre-exception behavior.
    ok_empty, reason_empty = drafter.validate_creative_hook_text(
        "Just go ahead and call",
        "Just go ahead and call us today for a quote",
        review_excerpt="",
    )
    # Unrelated excerpt that does not contain the phrase.
    ok_other, reason_other = drafter.validate_creative_hook_text(
        "Just go ahead and call",
        "Just go ahead and call us today for a quote",
        review_excerpt="Great machines, fair prices, quick turnaround every time.",
    )
    _check(
        "62. validate_creative_hook_text — overlap still blocked when phrase is "
        "absent from the excerpt (empty or unrelated)",
        (not ok_empty) and "overlap" in reason_empty.lower()
        and (not ok_other) and "overlap" in reason_other.lower(),
        f"empty_ok={ok_empty} ({reason_empty!r}), "
        f"other_ok={ok_other} ({reason_other!r})",
    )


def test_validate_llm_output_passes_review_post_with_quoted_overlap() -> None:
    """validate_llm_output emits no overlap issue for a social-proof post whose
    caption_hook and creative_hook_text overlap on excerpt-sourced text and
    whose review_excerpt contains that text."""
    parsed = {
        "caption_hook": "Just go ahead and call us today for a quote",
        "caption_body": "Body content line one.\n\nLine two.",
        "cta_text": "",
        "image_overlay_hook": "",
        "creative_hook_text": "Just go ahead and call",
        "review_excerpt": (
            "When my skid steer died mid-job, I figured just go ahead and "
            "call T.E.S. Rentals — best decision all week."
        ),
        "first_comment": "",
        "draft_rationale": "rationale",
    }
    issues, _ = drafter.validate_llm_output(
        parsed, "FALSE", "facebook", "brand_awareness",
        "save", "image2_enhanced",
    )
    has_overlap_issue = any("overlap" in i.lower() for i in issues)
    _check(
        "63. validate_llm_output — review post with excerpt-sourced hook "
        "overlap passes (no overlap issue)",
        not has_overlap_issue,
        f"issues={issues}",
    )


def test_build_drafter_messages_describes_creative_hook_text() -> None:
    """Verify the LLM prompt instructs the model to produce a distinct,
    ≤7-word creative_hook_text and includes it in the output schema."""
    config = load_config()
    row = {
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        drafter.CQ_CTA_TYPE: "save",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "creatomate_video",
        drafter.CQ_ANGLE: "Show the machine on a tight residential lot.",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "",
    }
    _, user_msg = drafter.build_drafter_messages(
        row=row,
        catalog_item=None,
        config=config,
        brand_voice="(brand voice text)",
        hook_skill="(hook skill text)",
        cta_skill="(cta skill text)",
        platform_style="(platform style text)",
        few_shot_library="(few shot library text)",
        strategy_guidance="(strategy guidance text)",
    )

    lower = user_msg.lower()
    _check(
        "23. build_drafter_messages — describes creative_hook_text "
        "(distinct, ≤7 words) and includes it in the output schema",
        "creative_hook_text" in user_msg
        and "distinct" in lower
        and "7" in user_msg,
        f"contains_field={'creative_hook_text' in user_msg}, "
        f"says_distinct={'distinct' in lower}, has_7={'7' in user_msg}",
    )


def test_generate_media_creatomate_text_overlay_uses_creative_hook_text() -> None:
    """Hook-Text on a creatomate_text_overlay render must be populated from
    creative_hook_text, NOT from image_overlay_hook or the caption hook."""
    captured: dict = {}

    def fake_render(template_id, modifications, output_path, **kwargs):
        captured["template_id"] = template_id
        captured["modifications"] = modifications
        return {
            "success": True, "output_path": output_path,
            "render_id": "ok", "error": "",
        }

    config = Config({
        "creatomate": {
            "equipment_post_image": {
                "templates": {
                    "diagonal_slash": {"id": "tpl-diagonal-slash"},
                },
            },
        },
    })
    row = {
        drafter.CQ_ROW_ID: "TEST-CH-01",
        drafter.CQ_PLATFORM: "instagram",
        drafter.CQ_MEDIA_FORMAT: "creatomate_text_overlay",
        drafter.CQ_TEXT_OVERLAY: "TRUE",
        drafter.CQ_ANGLE: "Tight lot reach",
    }

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        result = drafter.generate_media(
            row=row,
            image_id="drive-image-id",
            overlay_hook="Old overlay hook text",
            caption_hook="Caption hook line that opens the post.",
            image_prompt_universal="",
            image_prompt_social="",
            config=config,
            drive_service=None,
            review_data=None,
            creative_hook_text="Zero swing wins",
        )
    finally:
        creatomate_helpers.render_template = orig

    mods = captured.get("modifications", {})
    hook_text_value = mods.get("Hook-Text", {}).get("text", "")
    _check(
        "24. generate_media (creatomate_text_overlay) — Hook-Text is "
        "creative_hook_text, not the caption hook or image_overlay_hook",
        result.get("success") is True
        and hook_text_value == "Zero swing wins"
        and "Caption hook" not in hook_text_value
        and "Old overlay hook" not in hook_text_value,
        f"hook_text_value={hook_text_value!r}, result={result!r}",
    )


def test_generate_media_creatomate_video_uses_creative_hook_text() -> None:
    """Same contract on creatomate_video: Hook-Text comes from
    creative_hook_text, not _derive_video_hook on the caption hook."""
    captured: dict = {}

    def fake_render(template_id, modifications, output_path, **kwargs):
        captured["template_id"] = template_id
        captured["modifications"] = modifications
        return {
            "success": True, "output_path": output_path,
            "render_id": "ok", "error": "",
        }

    config = Config({
        "creatomate": {
            "equipment_post_video": {
                "templates": {
                    "slow_push": {"id": "tpl-slow-push"},
                },
            },
        },
    })
    row = {
        drafter.CQ_ROW_ID: "TEST-CH-02",
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_MEDIA_FORMAT: "creatomate_video",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_ANGLE: "Show the back-fence clearance",
    }

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        result = drafter.generate_media(
            row=row,
            image_id="drive-image-id",
            overlay_hook="",
            caption_hook=(
                "Tight residential lot? Zero tail swing changes the game."
            ),
            image_prompt_universal="",
            image_prompt_social="",
            config=config,
            drive_service=None,
            review_data=None,
            creative_hook_text="Back fence stays intact",
        )
    finally:
        creatomate_helpers.render_template = orig

    mods = captured.get("modifications", {})
    hook_text_value = mods.get("Hook-Text", {}).get("text", "")
    _check(
        "25. generate_media (creatomate_video) — Hook-Text uses "
        "creative_hook_text, not _derive_video_hook on caption_hook",
        result.get("success") is True
        and hook_text_value == "Back fence stays intact"
        # Make sure we didn't fall through to the derived hook from caption_hook.
        and "Tight residential lot" not in hook_text_value,
        f"hook_text_value={hook_text_value!r}, result={result!r}",
    )


def test_validate_review_excerpt_substring() -> None:
    """validate_review_excerpt enforces the verbatim-substring rule."""
    review = (
        "The 50G was ready when I arrived. Zeb walked me through the "
        "controls. Solid rental overall."
    )
    # Exact substring → pass.
    ok_sub, _ = drafter.validate_review_excerpt(
        "Zeb walked me through the controls", review, max_chars=None,
    )
    # Paraphrased (matches none of the review verbatim) → fail.
    ok_para, reason_para = drafter.validate_review_excerpt(
        "Zeb showed me how the machine worked", review, max_chars=None,
    )
    # Reordered/combined disjoint phrases → fail (not a contiguous substring).
    ok_combined, reason_combined = drafter.validate_review_excerpt(
        "Solid rental, Zeb walked me through the controls",
        review, max_chars=None,
    )
    # Empty → fail.
    ok_empty, reason_empty = drafter.validate_review_excerpt(
        "", review, max_chars=None,
    )
    _check(
        "26. validate_review_excerpt — verbatim substring passes; "
        "paraphrase, recombined fragments, and empty all fail",
        ok_sub
        and (not ok_para) and "substring" in reason_para.lower()
        and (not ok_combined) and "substring" in reason_combined.lower()
        and (not ok_empty) and "empty" in reason_empty.lower(),
        f"sub={ok_sub}, para={ok_para} ({reason_para!r}), "
        f"combined={ok_combined} ({reason_combined!r}), "
        f"empty={ok_empty} ({reason_empty!r})",
    )


def test_validate_review_excerpt_max_chars() -> None:
    """validate_review_excerpt enforces the template character budget."""
    review = "x" * 200
    # Within budget.
    ok_under, _ = drafter.validate_review_excerpt(
        "x" * 100, review, max_chars=127,
    )
    # Equal to budget.
    ok_equal, _ = drafter.validate_review_excerpt(
        "x" * 127, review, max_chars=127,
    )
    # Over budget.
    ok_over, reason_over = drafter.validate_review_excerpt(
        "x" * 150, review, max_chars=127,
    )
    # max_chars=None disables the length check.
    ok_no_limit, _ = drafter.validate_review_excerpt(
        "x" * 500, "x" * 500, max_chars=None,
    )
    _check(
        "27. validate_review_excerpt — respects template max_chars; "
        "max_chars=None skips the length check",
        ok_under and ok_equal
        and (not ok_over) and "127" in reason_over
        and ok_no_limit,
        f"under={ok_under}, equal={ok_equal}, "
        f"over={ok_over} ({reason_over!r}), no_limit={ok_no_limit}",
    )


def test_review_excerpt_does_not_start_with_first_word_heuristic() -> None:
    """Heuristic guarding against the old first-N-chars behavior. A real
    LLM-selected excerpt typically does NOT start with the first word of
    the review — the first word is usually a generic opener ("Great",
    "Highly", "Very"). A skill-following excerpt picks the proof phrase
    further in. This is a heuristic, not absolute: the test wires up the
    full validation flow with a known-good (skill-compliant) excerpt and
    confirms validation passes; if a test ever rewires this to the literal
    first-chars behavior, the assertion below catches it.
    """
    review = (
        "Great company to work with. They delivered the mini excavator "
        "right on time and it was exactly the machine we needed."
    )
    first_word = review.split()[0]

    # Skill-compliant excerpt: starts inside the proof phrase, not at the opener.
    good_excerpt = "delivered the mini excavator right on time"
    # Old first-N-chars behavior would start with the first word.
    legacy_excerpt = review[:40]

    ok_good, _ = drafter.validate_review_excerpt(
        good_excerpt, review, max_chars=80,
    )
    starts_with_first_word_good = good_excerpt.lower().startswith(
        first_word.lower()
    )

    # The legacy form is still a valid substring (and short enough), so
    # validation alone wouldn't catch it — the heuristic check is the
    # signal. This documents the failure mode the skill is meant to fix.
    ok_legacy, _ = drafter.validate_review_excerpt(
        legacy_excerpt, review, max_chars=80,
    )
    starts_with_first_word_legacy = legacy_excerpt.lower().startswith(
        first_word.lower()
    )

    _check(
        "28. review excerpt heuristic — skill-compliant excerpt does NOT "
        "start with the review's first word; the legacy first-N-chars "
        "form does (documents the old failure mode)",
        ok_good and not starts_with_first_word_good
        and ok_legacy and starts_with_first_word_legacy,
        f"good_valid={ok_good}, good_starts_first={starts_with_first_word_good}, "
        f"legacy_valid={ok_legacy}, legacy_starts_first={starts_with_first_word_legacy}",
    )


def test_select_review_template_max_chars_reads_config() -> None:
    """_select_review_template_max_chars returns the per-template budget
    from config, and None for non-review formats."""
    config = Config({
        "creatomate": {
            "review_image": {
                "templates": {
                    "only_one": {
                        "id": "tpl-a",
                        "max_review_text_chars": 168,
                    },
                },
            },
            "review_video": {
                "templates": {
                    "only_one": {
                        "id": "tpl-v",
                        "max_review_text_chars": 127,
                    },
                },
            },
        },
    })
    img_budget = drafter._select_review_template_max_chars(
        "creatomate_review_image", "ROW-XYZ", config,
    )
    vid_budget = drafter._select_review_template_max_chars(
        "creatomate_review_video", "ROW-XYZ", config,
    )
    non_review = drafter._select_review_template_max_chars(
        "image2_enhanced", "ROW-XYZ", config,
    )
    _check(
        "29. _select_review_template_max_chars — reads per-template budget; "
        "returns None for non-review media_format",
        img_budget == 168 and vid_budget == 127 and non_review is None,
        f"img={img_budget}, vid={vid_budget}, non_review={non_review}",
    )


def test_render_review_image_uses_passed_excerpt() -> None:
    """When an excerpt is passed to _render_review_image, that value
    becomes Review-Text — NOT review_data[excerpt_long]."""
    captured: dict = {}

    def fake_render(template_id, modifications, output_path, **kwargs):
        captured["modifications"] = modifications
        return {
            "success": True, "output_path": output_path,
            "render_id": "x", "error": "",
        }

    config = Config({
        "creatomate": {
            "review_image": {
                "templates": {
                    "bold_quote_card": {"id": "tpl-bqc"},
                },
            },
        },
    })
    review = {
        "review_id": "rev-X",
        "reviewer_first_name": "Carrie",
        "excerpt_long": "FALLBACK EXCERPT — should not appear",
    }

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        drafter._render_review_image(
            row_id="TEST-RX-01",
            review_data=review,
            source_image_url="",
            config=config,
            excerpt="The skid steer was clean and ready to go.",
        )
    finally:
        creatomate_helpers.render_template = orig

    mods = captured.get("modifications", {})
    rt = mods.get("Review-Text", {}).get("text", "")
    _check(
        "30. _render_review_image — passed excerpt becomes Review-Text "
        "(does NOT use excerpt_long fallback)",
        rt == "The skid steer was clean and ready to go."
        and "FALLBACK" not in rt,
        f"Review-Text={rt!r}",
    )


def test_render_review_video_uses_passed_excerpt() -> None:
    """Same contract for video templates."""
    captured: dict = {}

    def fake_render(template_id, modifications, output_path, **kwargs):
        captured["modifications"] = modifications
        return {
            "success": True, "output_path": output_path,
            "render_id": "x", "error": "",
        }

    config = Config({
        "creatomate": {
            "review_video": {
                "templates": {
                    "star_cascade": {"id": "tpl-sc"},
                },
            },
        },
    })
    review = {
        "review_id": "rev-Y",
        "reviewer_first_name": "Mike",
        "excerpt_long": "FALLBACK EXCERPT — should not appear",
    }

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        drafter._render_review_video(
            row_id="TEST-RV-01",
            review_data=review,
            source_image_url="",
            config=config,
            excerpt="Came through on a last-minute weekend rental.",
        )
    finally:
        creatomate_helpers.render_template = orig

    mods = captured.get("modifications", {})
    rt = mods.get("Review-Text", {}).get("text", "")
    _check(
        "31. _render_review_video — passed excerpt becomes Review-Text",
        rt == "Came through on a last-minute weekend rental."
        and "FALLBACK" not in rt,
        f"Review-Text={rt!r}",
    )


def test_render_review_image_falls_back_to_excerpt_long_when_excerpt_empty() -> None:
    """When the Drafter passes an empty excerpt (validation failure +
    empty fallback edge case), the renderer falls back to
    review_data[excerpt_long]."""
    captured: dict = {}

    def fake_render(template_id, modifications, output_path, **kwargs):
        captured["modifications"] = modifications
        return {
            "success": True, "output_path": output_path,
            "render_id": "x", "error": "",
        }

    config = Config({
        "creatomate": {
            "review_image": {
                "templates": {"bold_quote_card": {"id": "tpl"}},
            },
        },
    })
    review = {
        "review_id": "rev-Z",
        "reviewer_first_name": "Pat",
        "excerpt_long": "Backup excerpt from Reviews Sheet",
    }

    orig = creatomate_helpers.render_template
    creatomate_helpers.render_template = fake_render
    try:
        drafter._render_review_image(
            row_id="TEST-FB-01",
            review_data=review,
            source_image_url="",
            config=config,
            excerpt="",  # validation-failure fallback path
        )
    finally:
        creatomate_helpers.render_template = orig

    mods = captured.get("modifications", {})
    rt = mods.get("Review-Text", {}).get("text", "")
    _check(
        "32. _render_review_image — empty excerpt falls back to excerpt_long",
        rt == "Backup excerpt from Reviews Sheet",
        f"Review-Text={rt!r}",
    )


def test_build_drafter_messages_injects_excerpt_skill_and_budget() -> None:
    """When review_data is present, the LLM user message must include the
    review_excerpt_selection skill text and the per-template character
    budget passed via review_excerpt_max_chars."""
    config = load_config()
    row = {
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Social Proof / Customer Story",
        drafter.CQ_CTA_TYPE: "comment",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "creatomate_review_image",
        drafter.CQ_ANGLE: "Feature Carrie's review",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "",
    }
    review = {
        "review_id": "rev-001",
        "reviewer_first_name": "Carrie",
        "review_text": "Solid rental experience.",
        "excerpt_long": "Solid rental experience",
        "star_rating": "5",
    }
    skill_text = (
        "SKILL_SIGNATURE_LINE — pick the proof, not the pleasantry"
    )
    _, user_msg = drafter.build_drafter_messages(
        row=row,
        catalog_item=None,
        config=config,
        brand_voice="(brand)",
        hook_skill="(hook)",
        cta_skill="(cta)",
        platform_style="(plat)",
        few_shot_library="(fs)",
        strategy_guidance="(strat)",
        review_data=review,
        review_excerpt_skill=skill_text,
        review_excerpt_max_chars=168,
    )
    _check(
        "33. build_drafter_messages — injects review_excerpt_selection skill "
        "text and max_review_text_chars budget into the user message",
        "SKILL_SIGNATURE_LINE" in user_msg
        and "168" in user_msg
        and "review_excerpt" in user_msg
        and "Review Excerpt Selection" in user_msg,
        f"has_skill={'SKILL_SIGNATURE_LINE' in user_msg}, "
        f"has_budget={'168' in user_msg}, "
        f"has_field={'review_excerpt' in user_msg}",
    )


def test_build_drafter_messages_includes_review_block() -> None:
    """When review_data is provided, the user message carries a Social Proof
    review block with reviewer name and full review text. When omitted, the
    block must not appear."""
    config = load_config()
    row = {
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Social Proof / Customer Story",
        drafter.CQ_CTA_TYPE: "comment",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "creatomate_review_image",
        drafter.CQ_ANGLE: "Feature Carrie's review of the 50G",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "",
    }
    review = {
        "review_id": "rev-001",
        "reviewer_first_name": "Carrie",
        "review_text": (
            "The 50G was ready when I arrived. Zeb walked me through the "
            "controls. Solid rental overall."
        ),
        "excerpt_long": "The 50G was ready when I arrived",
        "star_rating": "5",
    }
    common_args = dict(
        catalog_item=None,
        config=config,
        brand_voice="(brand voice text)",
        hook_skill="(hook skill text)",
        cta_skill="(cta skill text)",
        platform_style="(platform style text)",
        few_shot_library="(few shot library text)",
        strategy_guidance="(strategy guidance text)",
    )

    _sys_w, user_w = drafter.build_drafter_messages(
        row=row, review_data=review, **common_args,
    )
    _sys_o, user_o = drafter.build_drafter_messages(
        row=row, review_data=None, **common_args,
    )

    _check(
        "19. build_drafter_messages — review_data injects block w/ reviewer "
        "name + full review; omitted when review_data is None",
        "Review Featured in This Post" in user_w
        and "Carrie" in user_w
        and "The 50G was ready when I arrived. Zeb walked me through the "
            "controls." in user_w
        and "Review Featured in This Post" not in user_o,
        f"with_block={'Review Featured' in user_w}, "
        f"without_block_absent={'Review Featured' not in user_o}",
    )


# ----------------------------------------------------------------------
# Sentence-length enforcement (B6) — Session 17
# ----------------------------------------------------------------------

def _make_long_sentence(n_words: int) -> str:
    """Build a sentence with exactly n_words words (no terminal period)."""
    return " ".join(["word"] * n_words)


def test_split_sentences_matches_critic() -> None:
    """Drafter and Critic must agree on sentence splitting — both run B6 with
    the same regex, stripping, and filtering."""
    samples = [
        "First sentence. Second sentence.",
        "Is this right? Yes it is.",
        "First part of the sentence\n\nthat continues here.",
        "",
        "Just a fragment",
    ]
    mismatches: list[tuple[str, list[str], list[str]]] = []
    for s in samples:
        d = drafter._split_sentences(s)
        c = critic._split_sentences(s)
        if d != c:
            mismatches.append((s, d, c))
    _check(
        "34. _split_sentences matches critic for canonical sample inputs",
        not mismatches,
        f"mismatches={mismatches!r}",
    )


def test_sentence_max_words_matches_critic() -> None:
    """The 18-word cap must be identical on both sides."""
    _check(
        "35. SENTENCE_MAX_WORDS matches critic and equals 18",
        drafter.SENTENCE_MAX_WORDS == critic.SENTENCE_MAX_WORDS == 18,
        f"drafter={drafter.SENTENCE_MAX_WORDS}, "
        f"critic={critic.SENTENCE_MAX_WORDS}",
    )


def test_sentence_split_re_matches_critic() -> None:
    """The split regex pattern must be identical on both sides."""
    _check(
        "36. SENTENCE_SPLIT_RE pattern matches critic",
        drafter.SENTENCE_SPLIT_RE.pattern == critic.SENTENCE_SPLIT_RE.pattern,
        f"drafter={drafter.SENTENCE_SPLIT_RE.pattern!r}, "
        f"critic={critic.SENTENCE_SPLIT_RE.pattern!r}",
    )


def test_find_violations_clean_caption() -> None:
    """All sentences ≤18 words → empty violations."""
    parsed = {
        "caption_hook": "Tight residential lot? Zero tail swing changes the game.",
        "caption_body": (
            "Reaches the back corner without bumping the fence.\n\n"
            "Leaves grass and pavers untouched."
        ),
        "cta_text": "Call to book.",
    }
    violations = drafter.find_sentence_length_violations(parsed)
    _check(
        "37. find_sentence_length_violations — clean caption returns []",
        violations == [],
        f"violations={violations!r}",
    )


def test_find_violations_long_caption_body() -> None:
    """A 22-word body sentence produces one violation with correct metadata."""
    long_sentence = _make_long_sentence(22)
    parsed = {
        "caption_hook": "Short hook.",
        "caption_body": f"{long_sentence}.",
        "cta_text": "",
    }
    violations = drafter.find_sentence_length_violations(parsed)
    ok = (
        len(violations) == 1
        and violations[0]["field"] == "caption_body"
        and violations[0]["word_count"] == 22
        and violations[0]["text"] == long_sentence
    )
    _check(
        "38. find_sentence_length_violations — 22-word caption_body sentence "
        "produces one correctly-described violation",
        ok,
        f"violations={violations!r}",
    )


def test_find_violations_long_caption_hook() -> None:
    """A 20-word hook sentence produces one violation on caption_hook."""
    long_sentence = _make_long_sentence(20)
    parsed = {
        "caption_hook": f"{long_sentence}.",
        "caption_body": "Short body.",
        "cta_text": "",
    }
    violations = drafter.find_sentence_length_violations(parsed)
    ok = (
        len(violations) == 1
        and violations[0]["field"] == "caption_hook"
        and violations[0]["word_count"] == 20
    )
    _check(
        "39. find_sentence_length_violations — 20-word caption_hook produces "
        "one violation on caption_hook",
        ok,
        f"violations={violations!r}",
    )


def test_find_violations_cta_text_excluded() -> None:
    """A 25-word cta_text sentence is intentionally not flagged — CTAs follow
    standardized brand voice patterns that may legitimately exceed 18 words."""
    long_sentence = _make_long_sentence(25)
    parsed = {
        "caption_hook": "Short hook.",
        "caption_body": "Short body.",
        "cta_text": f"{long_sentence}.",
    }
    violations = drafter.find_sentence_length_violations(parsed)
    _check(
        "40. find_sentence_length_violations — cta_text excluded even when "
        "long",
        violations == [],
        f"violations={violations!r}",
    )


def test_find_violations_multiple() -> None:
    """One hook violation + two body violations = three violations total."""
    hook_long = _make_long_sentence(19)
    body_long_1 = _make_long_sentence(21)
    body_long_2 = _make_long_sentence(24)
    parsed = {
        "caption_hook": f"{hook_long}.",
        "caption_body": (
            f"{body_long_1}. Short clean sentence in the middle. "
            f"{body_long_2}."
        ),
        "cta_text": "",
    }
    violations = drafter.find_sentence_length_violations(parsed)
    fields = [v["field"] for v in violations]
    counts = sorted(v["word_count"] for v in violations)
    _check(
        "41. find_sentence_length_violations — counts violations across "
        "caption_hook + caption_body",
        len(violations) == 3
        and fields.count("caption_hook") == 1
        and fields.count("caption_body") == 2
        and counts == [19, 21, 24],
        f"violations={violations!r}",
    )


def test_find_violations_boundary_18_words() -> None:
    """Exactly 18 words → no violation. 19 words → violation."""
    parsed_18 = {
        "caption_hook": "Short hook.",
        "caption_body": f"{_make_long_sentence(18)}.",
        "cta_text": "",
    }
    parsed_19 = {
        "caption_hook": "Short hook.",
        "caption_body": f"{_make_long_sentence(19)}.",
        "cta_text": "",
    }
    v18 = drafter.find_sentence_length_violations(parsed_18)
    v19 = drafter.find_sentence_length_violations(parsed_19)
    _check(
        "42. find_sentence_length_violations — boundary: 18 passes, 19 fails",
        v18 == [] and len(v19) == 1 and v19[0]["word_count"] == 19,
        f"v18={v18!r}, v19={v19!r}",
    )


def test_rewrite_long_sentences_success() -> None:
    """A successful mocked rewrite replaces the violating sentence and leaves
    no remaining violations."""
    import json as _json

    long_sentence = _make_long_sentence(22)
    parsed = {
        "caption_hook": "Short hook.",
        "caption_body": f"{long_sentence}.",
        "cta_text": "",
    }
    violations = drafter.find_sentence_length_violations(parsed)

    def fake_call(system_message, user_message, max_tokens=1024):
        return _json.dumps({
            "rewrites": [
                {
                    "field": "caption_body",
                    "original": long_sentence,
                    "rewritten": "Short rewrite. Second clean sentence.",
                },
            ],
        })

    orig = drafter.call_anthropic
    drafter.call_anthropic = fake_call
    try:
        updated, warnings = drafter.rewrite_long_sentences(violations, parsed)
    finally:
        drafter.call_anthropic = orig

    remaining = drafter.find_sentence_length_violations(updated)
    _check(
        "43. rewrite_long_sentences — successful rewrite eliminates the "
        "violation, returns no warnings",
        remaining == [] and warnings == []
        and "Short rewrite" in updated["caption_body"],
        f"remaining={remaining!r}, warnings={warnings!r}, "
        f"updated_body={updated['caption_body']!r}",
    )


def test_rewrite_long_sentences_still_long_retry() -> None:
    """First attempt returns a still-too-long rewrite; second attempt succeeds.
    Confirms the retry path works."""
    import json as _json

    original_long = _make_long_sentence(22)
    still_long = _make_long_sentence(20)
    parsed = {
        "caption_hook": "Short hook.",
        "caption_body": f"{original_long}.",
        "cta_text": "",
    }
    violations = drafter.find_sentence_length_violations(parsed)

    call_count = {"n": 0}

    def fake_call(system_message, user_message, max_tokens=1024):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _json.dumps({
                "rewrites": [
                    {
                        "field": "caption_body",
                        "original": original_long,
                        "rewritten": still_long,
                    },
                ],
            })
        return _json.dumps({
            "rewrites": [
                {
                    "field": "caption_body",
                    "original": still_long,
                    "rewritten": "Final short rewrite.",
                },
            ],
        })

    orig = drafter.call_anthropic
    drafter.call_anthropic = fake_call
    try:
        updated, warnings = drafter.rewrite_long_sentences(violations, parsed)
    finally:
        drafter.call_anthropic = orig

    remaining = drafter.find_sentence_length_violations(updated)
    _check(
        "44. rewrite_long_sentences — retries when first attempt is still "
        "long, succeeds on second attempt",
        call_count["n"] == 2
        and remaining == []
        and "Final short rewrite" in updated["caption_body"],
        f"calls={call_count['n']}, remaining={remaining!r}, "
        f"updated_body={updated['caption_body']!r}, warnings={warnings!r}",
    )


def test_rewrite_long_sentences_max_attempts_exhausted() -> None:
    """If both attempts return sentences that still exceed the limit, the
    function returns the best result with an exhausted-attempts warning and
    does not raise. The Critic remains the safety net."""
    import json as _json

    original_long = _make_long_sentence(22)
    still_long_1 = _make_long_sentence(21)
    still_long_2 = _make_long_sentence(20)
    parsed = {
        "caption_hook": "Short hook.",
        "caption_body": f"{original_long}.",
        "cta_text": "",
    }
    violations = drafter.find_sentence_length_violations(parsed)

    call_count = {"n": 0}

    def fake_call(system_message, user_message, max_tokens=1024):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _json.dumps({
                "rewrites": [
                    {
                        "field": "caption_body",
                        "original": original_long,
                        "rewritten": still_long_1,
                    },
                ],
            })
        return _json.dumps({
            "rewrites": [
                {
                    "field": "caption_body",
                    "original": still_long_1,
                    "rewritten": still_long_2,
                },
            ],
        })

    orig = drafter.call_anthropic
    drafter.call_anthropic = fake_call
    try:
        updated, warnings = drafter.rewrite_long_sentences(violations, parsed)
    finally:
        drafter.call_anthropic = orig

    remaining = drafter.find_sentence_length_violations(updated)
    exhausted_warning_present = any(
        "gave up" in w or "exhausted" in w.lower() for w in warnings
    )
    _check(
        "45. rewrite_long_sentences — exhausts max attempts without raising, "
        "returns best result with warning",
        call_count["n"] == 2
        and len(remaining) == 1
        and exhausted_warning_present,
        f"calls={call_count['n']}, remaining={remaining!r}, "
        f"warnings={warnings!r}",
    )


def test_rewrite_preserves_other_fields() -> None:
    """Rewriting caption_body must not change caption_hook, cta_text,
    image_overlay_hook, creative_hook_text, first_comment, or draft_rationale."""
    import json as _json

    long_sentence = _make_long_sentence(22)
    parsed = {
        "caption_hook": "Original short hook.",
        "caption_body": f"{long_sentence}.",
        "cta_text": "Original CTA.",
        "image_overlay_hook": "Original overlay hook",
        "creative_hook_text": "Original creative hook",
        "first_comment": "Original first comment",
        "draft_rationale": "Original rationale",
    }
    violations = drafter.find_sentence_length_violations(parsed)

    def fake_call(system_message, user_message, max_tokens=1024):
        return _json.dumps({
            "rewrites": [
                {
                    "field": "caption_body",
                    "original": long_sentence,
                    "rewritten": "Short rewrite. Done.",
                },
            ],
        })

    orig = drafter.call_anthropic
    drafter.call_anthropic = fake_call
    try:
        updated, _warnings = drafter.rewrite_long_sentences(violations, parsed)
    finally:
        drafter.call_anthropic = orig

    preserved = all(
        updated[k] == parsed[k]
        for k in (
            "caption_hook",
            "cta_text",
            "image_overlay_hook",
            "creative_hook_text",
            "first_comment",
            "draft_rationale",
        )
    )
    _check(
        "46. rewrite_long_sentences — only caption_body changes; all other "
        "fields are preserved",
        preserved and updated["caption_body"] != parsed["caption_body"],
        f"updated={updated!r}",
    )


def test_prompt_includes_sentence_length_rule() -> None:
    """Smoke test: both the system message and the Critical Instructions
    block in the user message must mention the 18-word sentence cap."""
    config = load_config()
    row = {
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        drafter.CQ_CTA_TYPE: "save",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "image2_enhanced",
        drafter.CQ_ANGLE: "Show the machine on a tight residential lot.",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "",
    }
    system_msg, user_msg = drafter.build_drafter_messages(
        row=row,
        catalog_item=None,
        config=config,
        brand_voice="(brand voice text)",
        hook_skill="(hook skill text)",
        cta_skill="(cta skill text)",
        platform_style="(platform style text)",
        few_shot_library="(few shot library text)",
        strategy_guidance="(strategy guidance text)",
    )
    _check(
        "47. build_drafter_messages — sentence-length rule (18 words) "
        "appears in both system message and user message",
        "18 words" in system_msg and "18 words" in user_msg,
        f"sys_has='{('18 words' in system_msg)}', "
        f"user_has='{('18 words' in user_msg)}'",
    )


# ----------------------------------------------------------------------
# LLM-assisted image selection tests
# ----------------------------------------------------------------------

import json as _json  # local alias to avoid touching existing imports

from tools import image_selector as _image_selector  # noqa: E402


def _sample_candidates() -> list[dict]:
    return [
        {"image_id": "img_a", "photo_context": "Equipment Working",
         "job_type": "Land Clearing",
         "notes": "1 week rental cleaning up after timber harvest",
         "rank": 1},
        {"image_id": "img_b", "photo_context": "Equipment staged / parked",
         "job_type": "", "notes": "", "rank": 2},
        {"image_id": "img_c", "photo_context": "Finished result",
         "job_type": "Residential foundation",
         "notes": "Tight backyard access, had to remove fence panel",
         "rank": 3},
    ]


def test_prompt_includes_image_candidates_block() -> None:
    """When 2+ candidates are passed, the user message must contain the
    'Available Source Images' selection block listing each candidate's
    Photo Context."""
    config = load_config()
    row = {
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        drafter.CQ_CTA_TYPE: "save",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "creatomate_text_overlay",
        drafter.CQ_ANGLE: "Tight residential lot reach.",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "eq-1",
    }
    candidates = _sample_candidates()
    _, user_msg = drafter.build_drafter_messages(
        row=row,
        catalog_item={"item_name": "Mini Ex", "category": "Excavator",
                      "model": "303.5", "description": "compact"},
        config=config,
        brand_voice="(bv)",
        hook_skill="(hook)",
        cta_skill="(cta)",
        platform_style="(ps)",
        few_shot_library="(fs)",
        strategy_guidance="(sg)",
        image_candidates=candidates,
    )
    has_block_header = "Available Source Images" in user_msg
    all_contexts_present = all(
        c["photo_context"] in user_msg for c in candidates
    )
    has_selection_instruction = "selected_image_id" in user_msg
    _check(
        "48. build_drafter_messages — image candidates block lists all "
        "Photo Contexts and references selected_image_id",
        has_block_header and all_contexts_present and has_selection_instruction,
        f"has_block_header={has_block_header}, "
        f"all_contexts_present={all_contexts_present}, "
        f"has_selection_instruction={has_selection_instruction}",
    )


def test_prompt_single_candidate_shows_context() -> None:
    """1 candidate: prompt must use 'Source Image Context' (not the
    selection block) and include the candidate's metadata."""
    config = load_config()
    row = {
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        drafter.CQ_CTA_TYPE: "save",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "creatomate_text_overlay",
        drafter.CQ_ANGLE: "Tight residential lot reach.",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "eq-1",
    }
    single = [_sample_candidates()[0]]
    _, user_msg = drafter.build_drafter_messages(
        row=row,
        catalog_item={"item_name": "Mini Ex"},
        config=config,
        brand_voice="(bv)",
        hook_skill="(hook)",
        cta_skill="(cta)",
        platform_style="(ps)",
        few_shot_library="(fs)",
        strategy_guidance="(sg)",
        image_candidates=single,
    )
    _check(
        "49. build_drafter_messages — single candidate shows 'Source Image "
        "Context' block (not selection block) with metadata",
        "Source Image Context" in user_msg
        and "Available Source Images" not in user_msg
        and single[0]["photo_context"] in user_msg
        and single[0]["job_type"] in user_msg
        and single[0]["notes"] in user_msg,
        f"has_context_header={'Source Image Context' in user_msg}, "
        f"no_selection_block="
        f"{'Available Source Images' not in user_msg}, "
        f"has_metadata=(ctx={single[0]['photo_context'] in user_msg}, "
        f"jt={single[0]['job_type'] in user_msg}, "
        f"notes={single[0]['notes'] in user_msg})",
    )


def test_prompt_no_candidates_no_image_block() -> None:
    """image_candidates=None: no image block appears in the prompt."""
    config = load_config()
    row = {
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        drafter.CQ_CTA_TYPE: "save",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "image2_enhanced",
        drafter.CQ_ANGLE: "Tight residential lot reach.",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "",
    }
    _, user_msg = drafter.build_drafter_messages(
        row=row,
        catalog_item=None,
        config=config,
        brand_voice="(bv)",
        hook_skill="(hook)",
        cta_skill="(cta)",
        platform_style="(ps)",
        few_shot_library="(fs)",
        strategy_guidance="(sg)",
        image_candidates=None,
    )
    _check(
        "50. build_drafter_messages — no candidates: neither 'Available "
        "Source Images' nor 'Source Image Context' appears in the prompt",
        "Available Source Images" not in user_msg
        and "Source Image Context" not in user_msg,
        f"has_selection={'Available Source Images' in user_msg}, "
        f"has_context={'Source Image Context' in user_msg}",
    )


def _build_mock_drafter_context() -> dict:
    """Minimal context dict for invoking draft_single_row with mocks."""
    catalog_row = {
        "item_id": "eq-1",
        "item_name": "Mini Excavator",
        "category": "Excavator",
        "model": "303.5",
        "description": "compact mini excavator",
        "primary_image_id": "primary-fallback",
    }
    queue_row = {
        drafter.CQ_ROW_ID: "TEST-LLM-IMG-01",
        drafter.CQ_STATUS: "planned",
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_OBJECTIVE: "brand_awareness",
        drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        drafter.CQ_CTA_TYPE: "save",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
        drafter.CQ_MEDIA_FORMAT: "creatomate_text_overlay",
        drafter.CQ_ANGLE: "Tight residential lot reach.",
        drafter.CQ_DRAFT_NOTES: "",
        drafter.CQ_FOCUS_EQUIPMENT: "eq-1",
        drafter.CQ_SOURCE_IMAGE: "",
        drafter.CQ_SCHEDULED: "",
        drafter.CQ_REVIEW_ID: "",
    }
    return {
        "sheets_service": None,
        "drive_service": None,
        "queue_id": "fake-queue-id",
        "queue_tab": "Sheet1",
        "catalog_id": "fake-catalog-id",
        "catalog_tab": "Sheet1",
        "reviews_id": "",
        "reviews_tab": "",
        "queue_rows": [queue_row],
        "catalog_rows": [catalog_row],
        "reviews_rows": [],
        "skills": {
            "brand_voice": "(bv)",
            "hook_creation": "(hook)",
            "cta": "(cta)",
            "platform_style": "(ps)",
            "image_prompt_universal": "",
            "image_prompt_social": "",
            "few_shot_library": "(fs)",
            "strategy_guidance": "(sg)",
            "review_excerpt_selection": "",
        },
    }


def _valid_llm_output(selected_image_id: str) -> dict:
    """Build a validation-passing LLM output JSON dict.

    Tuned for: platform=facebook, objective=brand_awareness, cta_type=save,
    text_overlay=FALSE, media_format=creatomate_text_overlay — so cta_text
    can be empty and image_overlay_hook must be empty.
    """
    return {
        "caption_hook": "Tight residential lot here.",
        "caption_body": "Zero swing fits where others cannot.",
        "cta_text": "",
        "image_overlay_hook": "",
        "creative_hook_text": "Zero swing wins",
        "review_excerpt": "",
        "selected_image_id": selected_image_id,
        "first_comment": "",
        "draft_rationale": "test rationale",
    }


def _run_draft_with_mocks(
    *,
    candidates: list[dict],
    llm_output: dict,
    queue_row_overrides: dict | None = None,
    revision_round: int = 1,
    previous_output: dict | None = None,
) -> tuple[dict, dict]:
    """Drive draft_single_row through a mocked LLM + candidate selector.

    Returns (result_dict, call_counters) where call_counters tracks how
    many times the mocks were invoked. The captured `last_user_msg` in
    counters lets tests inspect what was sent to the LLM (e.g., to
    verify revision_block injection).
    """
    context = _build_mock_drafter_context()
    if queue_row_overrides:
        context["queue_rows"][0].update(queue_row_overrides)

    counters = {
        "select_top_candidates": 0,
        "select_source_image": 0,
        "call_anthropic": 0,
        "generate_media": 0,
    }

    def fake_select_top_candidates(*args, **kwargs):
        counters["select_top_candidates"] += 1
        return {
            "candidates": list(candidates),
            "total_available": len(candidates),
            "fallback_image_id": "primary-fallback",
            "error": "",
        }

    def fake_select_source_image(*args, **kwargs):
        counters["select_source_image"] += 1
        return {
            "image_id": "primary-fallback",
            "selection_reason": "deterministic single image (mock)",
            "total_available": 1,
            "fallback": False,
        }

    def fake_call_anthropic(system_message, user_message, *args, **kwargs):
        counters["call_anthropic"] += 1
        counters["last_user_msg"] = user_message
        counters["last_system_msg"] = system_message
        return _json.dumps(llm_output)

    def fake_generate_media(*args, **kwargs):
        counters["generate_media"] += 1
        return {
            "success": True,
            "output_path": "/tmp/fake.png",
            "media_format_used": "creatomate_text_overlay",
            "fallback_chain": [],
            "error": "",
        }

    orig_top = _image_selector.select_top_candidates
    orig_src = _image_selector.select_source_image
    orig_call = drafter.call_anthropic
    orig_gen = drafter.generate_media
    _image_selector.select_top_candidates = fake_select_top_candidates
    _image_selector.select_source_image = fake_select_source_image
    drafter.call_anthropic = fake_call_anthropic
    drafter.generate_media = fake_generate_media
    try:
        result = drafter.draft_single_row(
            row_id="TEST-LLM-IMG-01",
            context=context,
            config=load_config(),
            dry_run=True,
            revision_round=revision_round,
            previous_output=previous_output,
        )
    finally:
        _image_selector.select_top_candidates = orig_top
        _image_selector.select_source_image = orig_src
        drafter.call_anthropic = orig_call
        drafter.generate_media = orig_gen

    return result, counters


def test_llm_image_pick_valid() -> None:
    """LLM returns selected_image_id matching a candidate (img_b, rank 2).
    Drafter must swap image_id and update selection_reason."""
    candidates = _sample_candidates()
    result, counters = _run_draft_with_mocks(
        candidates=candidates,
        llm_output=_valid_llm_output(selected_image_id="img_b"),
    )
    src = result.get("source_image", {}) if isinstance(result, dict) else {}
    _check(
        "51. draft_single_row — LLM-selected image (img_b) is applied; "
        "selection_reason indicates LLM selection",
        result.get("status") == "success"
        and src.get("image_id") == "img_b"
        and "LLM-selected" in src.get("selection_reason", "")
        and counters["select_top_candidates"] == 1
        and counters["call_anthropic"] >= 1,
        f"status={result.get('status')}, source_image={src!r}, "
        f"counters={counters}, error={result.get('error')!r}, "
        f"validation_issues={result.get('validation_issues')!r}",
    )


def test_llm_image_pick_invalid_falls_back() -> None:
    """LLM returns an image_id not in the candidate list. Drafter keeps the
    top-ranked default (img_a, rank 1)."""
    candidates = _sample_candidates()
    result, counters = _run_draft_with_mocks(
        candidates=candidates,
        llm_output=_valid_llm_output(selected_image_id="bogus-id-zzz"),
    )
    src = result.get("source_image", {}) if isinstance(result, dict) else {}
    _check(
        "52. draft_single_row — invalid LLM selected_image_id falls back to "
        "top-ranked candidate (img_a) with original selection_reason",
        result.get("status") == "success"
        and src.get("image_id") == "img_a"
        and "LLM-selected" not in src.get("selection_reason", ""),
        f"status={result.get('status')}, source_image={src!r}, "
        f"counters={counters}, error={result.get('error')!r}",
    )


def test_llm_image_pick_empty_uses_default() -> None:
    """LLM returns selected_image_id="". Drafter keeps top-ranked default."""
    candidates = _sample_candidates()
    result, counters = _run_draft_with_mocks(
        candidates=candidates,
        llm_output=_valid_llm_output(selected_image_id=""),
    )
    src = result.get("source_image", {}) if isinstance(result, dict) else {}
    _check(
        "53. draft_single_row — empty LLM selected_image_id keeps "
        "top-ranked candidate (img_a)",
        result.get("status") == "success"
        and src.get("image_id") == "img_a"
        and "LLM-selected" not in src.get("selection_reason", ""),
        f"status={result.get('status')}, source_image={src!r}, "
        f"counters={counters}, error={result.get('error')!r}",
    )


def test_strategist_preassigned_image_skips_candidates() -> None:
    """When source_image_id is pre-assigned on the queue row,
    select_top_candidates must NOT be called and the pre-assigned image
    is used directly."""
    candidates = _sample_candidates()  # would be returned IF called
    result, counters = _run_draft_with_mocks(
        candidates=candidates,
        llm_output=_valid_llm_output(selected_image_id="img_c"),
        queue_row_overrides={drafter.CQ_SOURCE_IMAGE: "preset-strategist-id"},
    )
    src = result.get("source_image", {}) if isinstance(result, dict) else {}
    _check(
        "54. draft_single_row — pre-assigned source_image_id skips "
        "select_top_candidates and ignores LLM pick",
        result.get("status") == "success"
        and src.get("image_id") == "preset-strategist-id"
        and counters["select_top_candidates"] == 0
        and "pre-assigned" in src.get("selection_reason", "").lower(),
        f"status={result.get('status')}, source_image={src!r}, "
        f"counters={counters}, error={result.get('error')!r}",
    )


# ----------------------------------------------------------------------
# Session 25 — Drafter revision-input contract
# ----------------------------------------------------------------------

def _common_build_kwargs() -> dict:
    """Minimal kwargs for build_drafter_messages used by revision tests."""
    return dict(
        row={
            drafter.CQ_PLATFORM: "facebook",
            drafter.CQ_OBJECTIVE: "brand_awareness",
            drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
            drafter.CQ_CTA_TYPE: "save",
            drafter.CQ_TEXT_OVERLAY: "FALSE",
            drafter.CQ_MEDIA_FORMAT: "image2_enhanced",
            drafter.CQ_ANGLE: "Show the machine on a tight residential lot.",
            drafter.CQ_DRAFT_NOTES: "",
            drafter.CQ_FOCUS_EQUIPMENT: "",
        },
        catalog_item=None,
        config=load_config(),
        brand_voice="(brand voice)",
        hook_skill="(hook)",
        cta_skill="(cta)",
        platform_style="(platform style)",
        few_shot_library="(few shots)",
        strategy_guidance="(strategy guidance)",
    )


def test_round1_unchanged_when_no_revision_block() -> None:
    """build_drafter_messages with no revision_block (default and explicit
    empty) produces an identical user message — no regression on round 1."""
    kwargs = _common_build_kwargs()
    _sys_a, user_a = drafter.build_drafter_messages(**kwargs)
    _sys_b, user_b = drafter.build_drafter_messages(
        revision_block="", **kwargs,
    )
    no_block_text = "Previous Critic Output" not in user_a
    _check(
        "55. build_drafter_messages — round-1 user message unchanged by "
        "default and explicit empty revision_block, no Critic block injected",
        user_a == user_b and no_block_text,
        f"equal={user_a == user_b}, no_block_text={no_block_text}",
    )


def test_revision_block_appended_at_tail() -> None:
    """With a non-empty revision_block, the user message ends with that
    block and is byte-identical to round 1 up to the append point."""
    kwargs = _common_build_kwargs()
    _sys_a, baseline = drafter.build_drafter_messages(**kwargs)

    block = (
        "\n\n# Previous Critic Output (revision_round=2)\n\n"
        "Sample block body.\n"
    )
    _sys_b, with_block = drafter.build_drafter_messages(
        revision_block=block, **kwargs,
    )

    ends_with_block = with_block.endswith(block)
    prefix_matches = with_block[: len(baseline)] == baseline
    appended_only_block = with_block == baseline + block

    _check(
        "56. build_drafter_messages — revision_block is appended verbatim "
        "at the tail of the user message; round-1 prefix is byte-identical",
        ends_with_block and prefix_matches and appended_only_block,
        f"ends_with_block={ends_with_block}, "
        f"prefix_matches={prefix_matches}, "
        f"appended_only_block={appended_only_block}",
    )


def test_build_revision_block_contains_fix_instruction() -> None:
    """build_revision_block must surface the previous Critic's
    fix_instruction text (the Drafter cannot revise without it)."""
    prev = {
        "verdict": "soft_fail",
        "failed_checks": [
            {
                "check_id": "C2",
                "category": "non_negotiable",
                "verdict_level": "soft_fail",
                "location": "caption body sentence 2",
                "description": "tone drifts from rugged to corporate",
                "fix_instruction": (
                    "Rewrite sentence 2 in plain operator voice; "
                    "drop the word 'leverage'."
                ),
            }
        ],
    }
    block = drafter.build_revision_block(prev, revision_round=2)
    has_header = "Previous Critic Output (revision_round=2)" in block
    has_fix_instruction = (
        "Rewrite sentence 2 in plain operator voice" in block
    )
    has_check_id = "C2" in block

    _check(
        "57. build_revision_block — block contains the previous "
        "fix_instruction, check_id, and the revision_round header",
        has_header and has_fix_instruction and has_check_id,
        f"has_header={has_header}, "
        f"has_fix_instruction={has_fix_instruction}, "
        f"has_check_id={has_check_id}",
    )

    empty = drafter.build_revision_block(None, revision_round=2)
    _check(
        "57b. build_revision_block — returns empty string when no "
        "previous output is given",
        empty == "",
        f"got={empty!r}",
    )


def test_status_gate_allows_drafted_on_revision() -> None:
    """A row with status=drafted MUST proceed past the status gate when
    revision_round > 1 — that is the whole point of accepting Critic-driven
    revisions back into the Drafter."""
    candidates = _sample_candidates()
    prev = {
        "verdict": "soft_fail",
        "failed_checks": [
            {
                "check_id": "C2",
                "fix_instruction": "tighten the hook.",
                "description": "hook is too soft",
            }
        ],
    }
    result, counters = _run_draft_with_mocks(
        candidates=candidates,
        llm_output=_valid_llm_output(selected_image_id="img_a"),
        queue_row_overrides={drafter.CQ_STATUS: "drafted"},
        revision_round=2,
        previous_output=prev,
    )

    proceeded = (
        result.get("status") == "success"
        and counters["call_anthropic"] >= 1
    )
    user_msg = counters.get("last_user_msg", "")
    block_in_user = (
        "Previous Critic Output (revision_round=2)" in user_msg
        and "tighten the hook" in user_msg
    )
    _check(
        "58. draft_single_row — drafted row IS allowed on revision_round=2; "
        "LLM is called with the revision_block appended to user message",
        proceeded and block_in_user,
        f"status={result.get('status')}, "
        f"calls={counters.get('call_anthropic')}, "
        f"block_in_user={block_in_user}, "
        f"error={result.get('error')!r}",
    )


def test_status_gate_rejects_drafted_round1() -> None:
    """Round-1 behavior preserved: a row already in status=drafted is
    skipped when revision_round=1 (no Critic output) — only `planned`
    rows are eligible for first-pass drafting."""
    candidates = _sample_candidates()
    result, counters = _run_draft_with_mocks(
        candidates=candidates,
        llm_output=_valid_llm_output(selected_image_id="img_a"),
        queue_row_overrides={drafter.CQ_STATUS: "drafted"},
        revision_round=1,
        previous_output=None,
    )
    skipped = result.get("status") == "skipped"
    no_llm_call = counters.get("call_anthropic", 0) == 0
    reason = str(result.get("reason", ""))
    mentions_status = "'drafted'" in reason or "drafted" in reason

    _check(
        "59. draft_single_row — drafted row is still skipped on "
        "revision_round=1 (round-1 planned-only behavior preserved)",
        skipped and no_llm_call and mentions_status,
        f"status={result.get('status')}, "
        f"calls={counters.get('call_anthropic')}, "
        f"reason={reason!r}",
    )


def test_cli_revision_requires_previous_output() -> None:
    """The CLI must reject --revision-round > 1 when --previous-output is
    missing. argparse.error() raises SystemExit with code 2."""
    err_buf = io.StringIO()
    raised = False
    code: int | None = None
    try:
        with redirect_stdout(io.StringIO()):
            from contextlib import redirect_stderr
            with redirect_stderr(err_buf):
                drafter.main([
                    "--row-id", "FAKE-ROW-01",
                    "--revision-round", "2",
                ])
    except SystemExit as exc:
        raised = True
        code = int(exc.code) if isinstance(exc.code, int) else 2

    err_text = err_buf.getvalue()
    mentions_previous_output = "--previous-output" in err_text
    mentions_revision_round = "--revision-round" in err_text

    _check(
        "60. drafter CLI — --revision-round > 1 without --previous-output "
        "exits with an argparse error mentioning both flags",
        raised
        and code == 2
        and mentions_previous_output
        and mentions_revision_round,
        f"raised={raised}, code={code}, "
        f"mentions_previous_output={mentions_previous_output}, "
        f"mentions_revision_round={mentions_revision_round}, "
        f"stderr={err_text!r}",
    )


# ----------------------------------------------------------------------
# Integration test
# ----------------------------------------------------------------------

def _find_planned_row(queue_rows: list[dict]) -> dict | None:
    for r in queue_rows:
        if str(r.get(drafter.CQ_STATUS, "")).strip().lower() == "planned":
            return r
    return None


def test_drafter_dry_run(config) -> None:  # type: ignore[no-untyped-def]
    # =================================================================
    # INTEGRATION TEST — calls the live Anthropic API. COSTS MONEY.
    # Do not run this in CI. Safe to run locally; dry_run=True ensures
    # no writes to the Content Queue or Drive.
    # =================================================================
    queue_id = config.get("drive.content_queue_sheet_id")
    if not queue_id:
        _check("10. drafter_dry_run — config has content_queue_sheet_id", False, "missing")
        return

    try:
        service = sheets_helpers.get_sheets_service()
        queue_tab = drafter._first_tab(service, queue_id)
        before = sheets_helpers.read_all_rows(queue_id, queue_tab, service=service)
    except Exception as exc:
        _check(
            "10. drafter_dry_run — read pre-snapshot",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        return

    target = _find_planned_row(before)
    if target is None:
        _check(
            "10. drafter_dry_run — locate a planned row",
            False,
            "no rows with status=planned in Content Queue — "
            "run the Strategist first or add a test row",
        )
        return

    row_id = str(target.get(drafter.CQ_ROW_ID, "")).strip()
    if not row_id:
        _check(
            "10. drafter_dry_run — planned row has row_id",
            False,
            f"row missing row_id: {target!r}",
        )
        return

    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = drafter.run_single(row_id, dry_run=True)
    except Exception as exc:
        _check(
            "10. drafter_dry_run — run_single completes",
            False,
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
        return

    # Verify no writes happened.
    after = sheets_helpers.read_all_rows(queue_id, queue_tab, service=service)
    same_len = len(before) == len(after)

    # Confirm the target row's status is still 'planned'.
    status_still_planned = False
    for r in after:
        if str(r.get(drafter.CQ_ROW_ID, "")).strip() == row_id:
            status_still_planned = (
                str(r.get(drafter.CQ_STATUS, "")).strip().lower() == "planned"
            )
            break

    status_ok = result.get("status") in ("success", "skipped")
    dry_ok = result.get("dry_run") is True

    success_body_ok = True
    if result.get("status") == "success":
        success_body_ok = (
            isinstance(result.get("caption_length"), int)
            and result["caption_length"] > 0
            and isinstance(result.get("caption"), str)
            and len(result["caption"]) > 0
        )

    _check(
        "10. drafter_dry_run — status ok, dry_run True, no writes, "
        "row status preserved",
        status_ok and dry_ok and same_len and status_still_planned
        and success_body_ok,
        f"status={result.get('status')}, dry_run={result.get('dry_run')}, "
        f"before_count={len(before)}, after_count={len(after)}, "
        f"status_still_planned={status_still_planned}, "
        f"caption_length={result.get('caption_length')}, "
        f"error={result.get('error')}, "
        f"validation_issues={result.get('validation_issues')}",
    )


# ----------------------------------------------------------------------
# Infographic media branch tests (generate_media / _generate_infographic_media
# / _build_infographic_prompt). The Gemini tool is ALWAYS mocked — no real
# generate_infographic call is ever made.
# ----------------------------------------------------------------------

def _infographic_config() -> Config:
    """Config carrying the brand_visuals.colors block the prompt builder reads."""
    return Config({
        "brand_visuals": {
            "colors": {
                "primary": "#1A1A1A",
                "primary_light": "#FFFFFF",
                "secondary": "#3A4A55",
                "accent": "#E0A82E",
                "neutral_light": "#F2F3F4",
                "neutral_dark": "#70777E",
            },
        },
    })


def test_generate_media_routes_image2_generated_to_infographic() -> None:
    """A row with media_format='image2_generated' and NO source image routes to
    the infographic branch and returns the tool's success result."""
    captured: dict = {}

    def fake_generate_infographic(prompt, output_path):
        captured["prompt"] = prompt
        captured["output_path"] = output_path
        # Tool corrects extension to native JPEG and returns the real path.
        return {
            "success": True,
            "output_path": output_path + ".jpg",
            "error": "",
        }

    row = {
        drafter.CQ_ROW_ID: "TEST-INFO-01",
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_MEDIA_FORMAT: "image2_generated",
        drafter.CQ_CONTENT_TYPE: "Educational Tip",
        drafter.CQ_FOCUS_EQUIPMENT: "",
        drafter.CQ_ANGLE: "How to pick the right machine for soft ground.",
    }

    orig = drafter.infographic_generator.generate_infographic
    drafter.infographic_generator.generate_infographic = fake_generate_infographic
    try:
        result = drafter.generate_media(
            row=row,
            image_id="",
            overlay_hook="",
            caption_hook="",
            image_prompt_universal="",
            image_prompt_social="",
            config=_infographic_config(),
            drive_service=None,
            review_data=None,
            creative_hook_text="Match the machine",
        )
    finally:
        drafter.infographic_generator.generate_infographic = orig

    _check(
        "64. generate_media — image2_generated routes to infographic branch "
        "and returns success with the tool's returned path",
        result.get("success") is True
        and result.get("media_format_used") == "image2_generated"
        and result.get("output_path") == captured.get("output_path", "") + ".jpg"
        and result.get("fallback_chain") == [],
        f"result={result!r}, captured_path={captured.get('output_path')!r}",
    )


def test_generate_media_infographic_above_image_id_guard() -> None:
    """The infographic branch sits ABOVE the `if not image_id` guard: an empty
    image_id with media_format='image2_generated' yields the infographic
    success, NOT the 'no source image available' failure."""
    def fake_generate_infographic(prompt, output_path):
        return {"success": True, "output_path": "/tmp/x_infographic.jpg",
                "error": ""}

    row = {
        drafter.CQ_ROW_ID: "TEST-INFO-02",
        drafter.CQ_PLATFORM: "instagram",
        drafter.CQ_MEDIA_FORMAT: "image2_generated",
        drafter.CQ_CONTENT_TYPE: "Local Connection",
        drafter.CQ_FOCUS_EQUIPMENT: "",
    }

    orig = drafter.infographic_generator.generate_infographic
    drafter.infographic_generator.generate_infographic = fake_generate_infographic
    try:
        result = drafter.generate_media(
            row=row,
            image_id="",
            overlay_hook="",
            caption_hook="",
            image_prompt_universal="",
            image_prompt_social="",
            config=_infographic_config(),
            drive_service=None,
            review_data=None,
            creative_hook_text="Rain is coming",
        )
    finally:
        drafter.infographic_generator.generate_infographic = orig

    _check(
        "65. generate_media — infographic branch sits above the image_id guard "
        "(empty image_id does NOT hit 'no source image available')",
        result.get("success") is True
        and result.get("media_format_used") == "image2_generated"
        and result.get("error") == ""
        and "no source image available" not in str(result.get("fallback_chain")),
        f"result={result!r}",
    )


def test_generate_media_review_still_routes_to_review() -> None:
    """Regression: a REVIEW_MEDIA_FORMATS row still routes to review media —
    the infographic branch did not displace the review bypass, and the
    infographic tool is never called."""
    info_calls: list[str] = []

    def fake_generate_infographic(prompt, output_path):
        info_calls.append(output_path)
        return {"success": True, "output_path": output_path, "error": ""}

    def fake_render(template_id, modifications, output_path, **kwargs):
        return {"success": True, "output_path": output_path,
                "render_id": "ok", "error": ""}

    config = Config({
        "creatomate": {
            "review_image": {"templates": {"bold_quote_card": {"id": "img"}}},
        },
    })
    review = {"review_id": "x", "reviewer_first_name": "Y", "excerpt_long": "Z"}

    orig_info = drafter.infographic_generator.generate_infographic
    orig_render = creatomate_helpers.render_template
    drafter.infographic_generator.generate_infographic = fake_generate_infographic
    creatomate_helpers.render_template = fake_render
    try:
        result = drafter.generate_media(
            row={drafter.CQ_ROW_ID: "TEST-INFO-03"},
            image_id="",
            overlay_hook="",
            caption_hook="",
            image_prompt_universal="",
            image_prompt_social="",
            config=config,
            drive_service=None,
            review_data=review,
            creative_hook_text="",
            review_excerpt="Z",
        )
        # media_format on the row drives review routing.
        result_review = drafter.generate_media(
            row={drafter.CQ_ROW_ID: "TEST-INFO-03",
                 drafter.CQ_MEDIA_FORMAT: "creatomate_review_image"},
            image_id="",
            overlay_hook="",
            caption_hook="",
            image_prompt_universal="",
            image_prompt_social="",
            config=config,
            drive_service=None,
            review_data=review,
            creative_hook_text="",
            review_excerpt="Z",
        )
    finally:
        drafter.infographic_generator.generate_infographic = orig_info
        creatomate_helpers.render_template = orig_render

    _check(
        "66. generate_media — review format still routes to review media "
        "(infographic branch did not displace the review bypass)",
        result_review.get("success") is True
        and result_review.get("media_format_used") == "creatomate_review_image"
        and info_calls == [],
        f"result_review={result_review!r}, info_calls={info_calls!r}",
    )


def test_generate_media_equipment_still_routes_to_image2() -> None:
    """Regression: a normal equipment row (image2_enhanced, with image_id) still
    routes to the existing image2 path; the infographic tool is never called."""
    info_calls: list[str] = []

    def fake_generate_infographic(prompt, output_path):
        info_calls.append(output_path)
        return {"success": True, "output_path": output_path, "error": ""}

    def fake_download(image_id, out, service=None, **kwargs):
        return out

    def fake_generate_image(src_local, prompt, output_path, **kwargs):
        return {"success": True, "output_path": output_path, "error": ""}

    row = {
        drafter.CQ_ROW_ID: "TEST-INFO-04",
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_MEDIA_FORMAT: "image2_enhanced",
        drafter.CQ_CONTENT_TYPE: "Equipment Spotlight / Product Feature",
        drafter.CQ_TEXT_OVERLAY: "FALSE",
    }

    orig_info = drafter.infographic_generator.generate_infographic
    orig_dl = drafter.drive_helpers.download_file
    orig_img = drafter.image_generator.generate_image
    drafter.infographic_generator.generate_infographic = fake_generate_infographic
    drafter.drive_helpers.download_file = fake_download
    drafter.image_generator.generate_image = fake_generate_image
    try:
        result = drafter.generate_media(
            row=row,
            image_id="drive-image-id",
            overlay_hook="",
            caption_hook="A hook line.",
            image_prompt_universal="(universal)",
            image_prompt_social="(social)",
            config=Config({}),
            drive_service=None,
            review_data=None,
            creative_hook_text="",
        )
    finally:
        drafter.infographic_generator.generate_infographic = orig_info
        drafter.drive_helpers.download_file = orig_dl
        drafter.image_generator.generate_image = orig_img

    _check(
        "67. generate_media — normal equipment row still routes to the image2 "
        "path (infographic tool never called)",
        result.get("success") is True
        and result.get("media_format_used") == "image2_enhanced"
        and info_calls == [],
        f"result={result!r}, info_calls={info_calls!r}",
    )


def test_generate_media_infographic_tool_failure() -> None:
    """Tool failure: generate_infographic returns success=False → generate_media
    returns success False with a fallback_chain entry and never raises."""
    def fake_generate_infographic(prompt, output_path):
        return {"success": False, "output_path": "",
                "error": "Gemini model access denied"}

    row = {
        drafter.CQ_ROW_ID: "TEST-INFO-05",
        drafter.CQ_PLATFORM: "facebook",
        drafter.CQ_MEDIA_FORMAT: "image2_generated",
        drafter.CQ_CONTENT_TYPE: "Educational Tip",
        drafter.CQ_FOCUS_EQUIPMENT: "",
    }

    orig = drafter.infographic_generator.generate_infographic
    drafter.infographic_generator.generate_infographic = fake_generate_infographic
    raised = False
    try:
        try:
            result = drafter.generate_media(
                row=row,
                image_id="",
                overlay_hook="",
                caption_hook="",
                image_prompt_universal="",
                image_prompt_social="",
                config=_infographic_config(),
                drive_service=None,
                review_data=None,
                creative_hook_text="Match the machine",
            )
        except Exception:
            raised = True
            result = {}
    finally:
        drafter.infographic_generator.generate_infographic = orig

    chain = result.get("fallback_chain", [])
    _check(
        "68. generate_media — infographic tool failure returns success False "
        "with fallback_chain entry and does not raise",
        raised is False
        and result.get("success") is False
        and result.get("media_format_used") == ""
        and len(chain) == 1
        and chain[0][0] == "image2_generated"
        and "access denied" in chain[0][1]
        and "access denied" in result.get("error", ""),
        f"raised={raised}, result={result!r}",
    )


def test_build_infographic_prompt_style_blocks_differ() -> None:
    """Educational Tip → diagrammatic/process language; Local Connection →
    thematic/environmental (dry-vs-wet) language; the two prompts differ."""
    config = _infographic_config()
    edu = drafter._build_infographic_prompt(
        content_type="Educational Tip",
        creative_hook_text="Match the machine",
        caption_concept="Pick the right machine for soft ground.",
        config=config,
    )
    local = drafter._build_infographic_prompt(
        content_type="Local Connection",
        creative_hook_text="Rain is coming",
        caption_concept="Wet season is starting in the area.",
        config=config,
    )
    edu_l = edu.lower()
    local_l = local.lower()
    _check(
        "69. _build_infographic_prompt — Educational Tip uses diagrammatic/"
        "process language; Local Connection uses thematic/environmental "
        "(dry-vs-wet) language; they differ",
        ("diagrammatic" in edu_l and "process" in edu_l)
        and ("thematic" in local_l or "environmental" in local_l)
        and ("dry" in local_l and "wet" in local_l)
        and edu != local,
        f"edu_diagrammatic={'diagrammatic' in edu_l}, "
        f"local_thematic={'thematic' in local_l or 'environmental' in local_l}, "
        f"local_dry_wet={'dry' in local_l and 'wet' in local_l}, "
        f"differ={edu != local}",
    )


def test_build_infographic_prompt_unknown_content_type_fallback() -> None:
    """Unknown content_type falls back to the diagrammatic block (no crash)."""
    config = _infographic_config()
    raised = False
    prompt = ""
    try:
        prompt = drafter._build_infographic_prompt(
            content_type="Some Brand New Type",
            creative_hook_text="Generic headline",
            caption_concept="Some concept.",
            config=config,
        )
    except Exception:
        raised = True
    _check(
        "70. _build_infographic_prompt — unknown content_type falls back to "
        "the diagrammatic block without crashing",
        raised is False and "diagrammatic" in prompt.lower(),
        f"raised={raised}, has_diagrammatic="
        f"{'diagrammatic' in prompt.lower()}",
    )


def test_build_infographic_prompt_headline_verbatim() -> None:
    """The creative_hook_text appears verbatim and the builder instructs the
    rendered headline to read EXACTLY that string."""
    config = _infographic_config()
    headline = "Zero swing changes the game"
    prompt = drafter._build_infographic_prompt(
        content_type="Educational Tip",
        creative_hook_text=headline,
        caption_concept="Tight lots need a zero-swing machine.",
        config=config,
    )
    _check(
        "71. _build_infographic_prompt — creative_hook_text embedded verbatim "
        "with an EXACT-headline instruction",
        headline in prompt and "exactly" in prompt.lower(),
        f"headline_present={headline in prompt}, "
        f"has_exactly={'exactly' in prompt.lower()}",
    )


def test_build_infographic_prompt_brand_colors_from_config() -> None:
    """Brand color hexes are pulled from config and appear in the prompt."""
    config = _infographic_config()
    prompt = drafter._build_infographic_prompt(
        content_type="Educational Tip",
        creative_hook_text="Match the machine",
        caption_concept="Concept.",
        config=config,
    )
    _check(
        "72. _build_infographic_prompt — accent (#E0A82E) and primary (#1A1A1A) "
        "hexes from config appear in the prompt",
        "#E0A82E" in prompt and "#1A1A1A" in prompt,
        f"accent_present={'#E0A82E' in prompt}, "
        f"primary_present={'#1A1A1A' in prompt}",
    )


def test_build_infographic_prompt_hard_negatives_and_whitelist() -> None:
    """The prompt forbids logos/wordmarks/URLs and photorealism, and states the
    text whitelist (headline + at most 2 short labels)."""
    config = _infographic_config()
    prompt = drafter._build_infographic_prompt(
        content_type="Educational Tip",
        creative_hook_text="Match the machine",
        caption_concept="Concept.",
        config=config,
    )
    p = prompt.lower()
    _check(
        "73. _build_infographic_prompt — hard negatives (logos/wordmarks/URLs, "
        "no photorealism) and text whitelist (headline + ≤2 labels) present",
        "logo" in p
        and "wordmark" in p
        and "url" in p
        and "photorealism" in p
        and "whitelist" in p
        and "2" in prompt,
        f"logo={'logo' in p}, wordmark={'wordmark' in p}, url={'url' in p}, "
        f"photorealism={'photorealism' in p}, whitelist={'whitelist' in p}",
    )


def test_build_infographic_prompt_caption_is_concept_not_lettered() -> None:
    """A county name / URL in the caption is used as CONCEPT input, but the
    builder carries the explicit constraint that county names / URLs are never
    lettered into the image."""
    config = _infographic_config()
    concept = (
        "Serving Bradford County contractors — book at https://tesrents.com "
        "before the wet season."
    )
    prompt = drafter._build_infographic_prompt(
        content_type="Local Connection",
        creative_hook_text="Beat the wet season",
        caption_concept=concept,
        config=config,
    )
    p = prompt.lower()
    # Robust: assert the explicit "never letter county names / URL" constraint
    # exists rather than trying to prove a negative across the whole string.
    has_constraint = (
        "county" in p
        and "concept input only" in p
        and ("never be lettered" in p or "never lettered" in p)
    )
    _check(
        "74. _build_infographic_prompt — caption county/URL is concept input "
        "only, with an explicit never-letter-into-image constraint",
        has_constraint and "concept" in p,
        f"has_constraint={has_constraint}, has_concept={'concept' in p}",
    )


if pytest is not None:
    # Live Anthropic-API integration test; costs money. Skip under pytest so it
    # is never collected/run there. The native runner calls it directly with a
    # loaded config (the skip wrapper leaves the function directly callable).
    test_drafter_dry_run = pytest.mark.skip(
        reason="live Anthropic API integration test; costs money. "
        "Run via the native runner: python3 tests/test_drafter.py --live"
    )(test_drafter_dry_run)


def run_tests(run_live: bool) -> int:
    print(f"Loading config from: {CONFIG_FILE}")
    config = load_config()

    print()
    print("Deterministic tests (no API calls):")
    test_caption_assembly()
    test_caption_assembly_no_cta()
    test_overlay_hook_validation_valid()
    test_overlay_hook_validation_too_long()
    test_overlay_hook_validation_empty_when_required()
    test_overlay_hook_validation_empty_when_not_required()
    test_overlay_hook_validation_video_format_allows_any_hook()
    test_derive_video_hook_from_caption()
    test_derive_video_hook_falls_back_to_angle()
    test_derive_video_hook_strips_banned_chars()
    test_derive_video_hook_empty_input()
    test_banned_language_check()
    test_platform_char_limit()
    test_build_drafter_messages_anti_fabrication()
    test_find_review_by_id()
    test_render_review_image_builds_modifications()
    test_render_review_image_includes_equipment_photo_when_supported()
    test_render_review_video_builds_modifications()
    test_generate_review_media_video_falls_back_to_image()
    test_generate_review_media_image_no_fallback()
    test_generate_review_media_missing_review_data()
    test_update_review_usage_writes_correct_cells()
    test_update_review_usage_missing_review()
    test_build_drafter_messages_includes_review_block()
    test_creative_hook_text_validation_max_words()
    test_creative_hook_text_validation_distinct_from_caption()
    test_validate_llm_output_requires_creative_hook_text()
    test_validate_creative_hook_allows_excerpt_sourced_overlap()
    test_validate_creative_hook_blocks_non_excerpt_overlap()
    test_validate_llm_output_passes_review_post_with_quoted_overlap()
    test_build_drafter_messages_describes_creative_hook_text()
    test_generate_media_creatomate_text_overlay_uses_creative_hook_text()
    test_generate_media_creatomate_video_uses_creative_hook_text()
    test_validate_review_excerpt_substring()
    test_validate_review_excerpt_max_chars()
    test_review_excerpt_does_not_start_with_first_word_heuristic()
    test_select_review_template_max_chars_reads_config()
    test_render_review_image_uses_passed_excerpt()
    test_render_review_video_uses_passed_excerpt()
    test_render_review_image_falls_back_to_excerpt_long_when_excerpt_empty()
    test_build_drafter_messages_injects_excerpt_skill_and_budget()
    test_split_sentences_matches_critic()
    test_sentence_max_words_matches_critic()
    test_sentence_split_re_matches_critic()
    test_find_violations_clean_caption()
    test_find_violations_long_caption_body()
    test_find_violations_long_caption_hook()
    test_find_violations_cta_text_excluded()
    test_find_violations_multiple()
    test_find_violations_boundary_18_words()
    test_rewrite_long_sentences_success()
    test_rewrite_long_sentences_still_long_retry()
    test_rewrite_long_sentences_max_attempts_exhausted()
    test_rewrite_preserves_other_fields()
    test_prompt_includes_sentence_length_rule()
    test_prompt_includes_image_candidates_block()
    test_prompt_single_candidate_shows_context()
    test_prompt_no_candidates_no_image_block()
    test_llm_image_pick_valid()
    test_llm_image_pick_invalid_falls_back()
    test_llm_image_pick_empty_uses_default()
    test_strategist_preassigned_image_skips_candidates()
    test_round1_unchanged_when_no_revision_block()
    test_revision_block_appended_at_tail()
    test_build_revision_block_contains_fix_instruction()
    test_status_gate_allows_drafted_on_revision()
    test_status_gate_rejects_drafted_round1()
    test_cli_revision_requires_previous_output()
    test_generate_media_routes_image2_generated_to_infographic()
    test_generate_media_infographic_above_image_id_guard()
    test_generate_media_review_still_routes_to_review()
    test_generate_media_equipment_still_routes_to_image2()
    test_generate_media_infographic_tool_failure()
    test_build_infographic_prompt_style_blocks_differ()
    test_build_infographic_prompt_unknown_content_type_fallback()
    test_build_infographic_prompt_headline_verbatim()
    test_build_infographic_prompt_brand_colors_from_config()
    test_build_infographic_prompt_hard_negatives_and_whitelist()
    test_build_infographic_prompt_caption_is_concept_not_lettered()

    if run_live:
        print()
        print("Integration test (calls Anthropic API — COSTS MONEY):")
        test_drafter_dry_run(config)
    else:
        print()
        print("Integration test skipped (pass --live to run it; costs money).")

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
    run_live = "--live" in sys.argv[1:]
    try:
        sys.exit(run_tests(run_live))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
