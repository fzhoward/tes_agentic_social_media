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
        _check("9. drafter_dry_run — config has content_queue_sheet_id", False, "missing")
        return

    try:
        service = sheets_helpers.get_sheets_service()
        queue_tab = drafter._first_tab(service, queue_id)
        before = sheets_helpers.read_all_rows(queue_id, queue_tab, service=service)
    except Exception as exc:
        _check(
            "9. drafter_dry_run — read pre-snapshot",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        return

    target = _find_planned_row(before)
    if target is None:
        _check(
            "9. drafter_dry_run — locate a planned row",
            False,
            "no rows with status=planned in Content Queue — "
            "run the Strategist first or add a test row",
        )
        return

    row_id = str(target.get(drafter.CQ_ROW_ID, "")).strip()
    if not row_id:
        _check(
            "9. drafter_dry_run — planned row has row_id",
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
            "9. drafter_dry_run — run_single completes",
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
        "9. drafter_dry_run — status ok, dry_run True, no writes, "
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
    test_banned_language_check()
    test_platform_char_limit()

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
