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

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from agents import drafter  # noqa: E402
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
