"""Tests for tools/creatomate_helpers.py.

The first two tests are deterministic and do NOT hit the Creatomate API.
Test 3 submits a live render to Creatomate using a known template and
the in-platform placeholder asset, then downloads the result. It costs
money and is opt-in via ``--live`` on the command line.

Run from the project root:
    python -m tests.test_creatomate_helpers                # deterministic only
    python -m tests.test_creatomate_helpers --live          # also runs test 3
"""

from __future__ import annotations

import os
import sys
import tempfile
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

from tools import creatomate_helpers  # noqa: E402
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


def test_build_modifications() -> None:
    image_fields = {
        "Equipment-Photo": "https://creatomate.com/files/assets/abc-123",
    }
    text_fields = {
        "Hook-Text": "Wrong machine costs more than the rental",
    }
    result = creatomate_helpers.build_modifications(image_fields, text_fields)
    expected = {
        "Equipment-Photo": {"source": "https://creatomate.com/files/assets/abc-123"},
        "Hook-Text": {"text": "Wrong machine costs more than the rental"},
    }
    # Also verify None inputs produce {}.
    empty = creatomate_helpers.build_modifications(None, None)
    _check(
        "1. build_modifications — wraps image fields as 'source' and text as 'text'",
        result == expected and empty == {},
        f"result={result!r}, empty={empty!r}",
    )


def test_render_missing_api_key() -> None:
    saved_key = os.environ.pop("CREATOMATE_API_KEY", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "out.png")
            result = creatomate_helpers.render_template(
                template_id="5f7e02d4-5e52-4d4a-96f1-41a8f55c19ac",
                modifications={"Hook-Text": {"text": "hello"}},
                output_path=output,
            )
            _check(
                "2. render_template — missing CREATOMATE_API_KEY returns success=False gracefully",
                result.get("success") is False
                and "creatomate_api_key" in result.get("error", "").lower()
                and not os.path.exists(output),
                f"result={result!r}",
            )
    finally:
        if saved_key is not None:
            os.environ["CREATOMATE_API_KEY"] = saved_key


def test_render_template_live(config) -> None:  # type: ignore[no-untyped-def]
    # ⚠️ Calls Creatomate API — costs money. Do not run in CI.
    if not os.environ.get("CREATOMATE_API_KEY"):
        _check(
            "3. render_template LIVE — skipped (CREATOMATE_API_KEY not set)",
            False,
            "set CREATOMATE_API_KEY in .env to run this test",
        )
        return

    template_id = config.get(
        "creatomate.equipment_post_image.templates.diagonal_slash.id"
    )
    placeholder_uuid = config.get(
        "creatomate.assets.placeholder_equipment_photo_uuid"
    )
    if not template_id or not placeholder_uuid:
        _check(
            "3. render_template LIVE — config lookup",
            False,
            f"template_id={template_id!r}, placeholder_uuid={placeholder_uuid!r}",
        )
        return

    image_url = f"https://creatomate.com/files/assets/{placeholder_uuid}"
    modifications = creatomate_helpers.build_modifications(
        image_fields={"Equipment-Photo": image_url},
        text_fields={"Hook-Text": "Test Hook Text"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "sub", "rendered.png")
        result = creatomate_helpers.render_template(
            template_id=template_id,
            modifications=modifications,
            output_path=output,
        )
        file_exists = os.path.exists(output)
        file_size = os.path.getsize(output) if file_exists else 0
        _check(
            "3. render_template LIVE — returns success, render_id, writes non-empty file",
            result.get("success") is True
            and bool(result.get("render_id"))
            and result.get("output_path") == output
            and file_exists
            and file_size > 0,
            f"result={result!r}, file_exists={file_exists}, file_size={file_size}",
        )


def run_tests(run_live: bool) -> int:
    print(f"Loading config from: {CONFIG_FILE}")
    config = load_config()

    print()
    print("Deterministic tests (no API calls):")
    test_build_modifications()
    test_render_missing_api_key()

    if run_live:
        print()
        print("Live test (calls Creatomate API — COSTS MONEY):")
        test_render_template_live(config)
    else:
        print()
        print("Live test skipped (pass --live to run it; costs money).")

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
