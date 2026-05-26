"""Tests for tools/image_generator.py.

The first two tests are deterministic and do NOT call the OpenAI API.
Test 3 is a live integration test that calls the gpt-image-2 endpoint
and costs money. It is opt-in via ``--live`` on the command line.

Run from the project root:
    python -m tests.test_image_generator                 # deterministic only
    python -m tests.test_image_generator --live           # also runs test 3
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

from tools import image_generator  # noqa: E402


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


def _write_tiny_png(path: str) -> None:
    """Write a 100x100 solid-color PNG to ``path`` using Pillow."""
    from PIL import Image  # local import — Pillow is in requirements.txt

    img = Image.new("RGB", (100, 100), color=(232, 96, 28))  # TES orange
    img.save(path, format="PNG")


def test_generate_image_missing_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bad_source = os.path.join(tmp, "does_not_exist.png")
        output = os.path.join(tmp, "out.png")
        result = image_generator.generate_image(
            source_image_path=bad_source,
            prompt="dummy prompt",
            output_path=output,
        )
        _check(
            "1. generate_image — missing source returns success=False without API call",
            result.get("success") is False
            and "source image not found" in result.get("error", "").lower()
            and not os.path.exists(output),
            f"result={result!r}",
        )


def test_generate_image_missing_api_key() -> None:
    saved_key = os.environ.pop("OPENAI_API_KEY", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "src.png")
            _write_tiny_png(source)
            output = os.path.join(tmp, "out.png")
            result = image_generator.generate_image(
                source_image_path=source,
                prompt="dummy prompt",
                output_path=output,
            )
            _check(
                "2. generate_image — missing OPENAI_API_KEY returns success=False gracefully",
                result.get("success") is False
                and "openai_api_key" in result.get("error", "").lower()
                and not os.path.exists(output),
                f"result={result!r}",
            )
    finally:
        if saved_key is not None:
            os.environ["OPENAI_API_KEY"] = saved_key


def test_generate_image_live() -> None:
    # ⚠️ Calls OpenAI API — costs money. Do not run in CI.
    if not os.environ.get("OPENAI_API_KEY"):
        _check(
            "3. generate_image LIVE — skipped (OPENAI_API_KEY not set)",
            False,
            "set OPENAI_API_KEY in .env to run this test",
        )
        return

    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "src.png")
        _write_tiny_png(source)
        output = os.path.join(tmp, "subdir", "generated.png")
        result = image_generator.generate_image(
            source_image_path=source,
            prompt="Enhance this photo: improve clarity and contrast.",
            output_path=output,
            size="1024x1024",
        )
        try:
            size_ok = os.path.exists(output) and os.path.getsize(output) > 0
            _check(
                "3. generate_image LIVE — returns success, writes non-empty file",
                result.get("success") is True
                and result.get("output_path") == output
                and size_ok,
                f"result={result!r}, file_exists={os.path.exists(output)}, "
                f"file_size={os.path.getsize(output) if os.path.exists(output) else 0}",
            )
        finally:
            # tempdir cleanup handles it; nothing else to do.
            pass


def run_tests(run_live: bool) -> int:
    print("Deterministic tests (no API calls):")
    test_generate_image_missing_source()
    test_generate_image_missing_api_key()

    if run_live:
        print()
        print("Live test (calls OpenAI API — COSTS MONEY):")
        test_generate_image_live()
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
