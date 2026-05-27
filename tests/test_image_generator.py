"""Tests for tools/image_generator.py.

The first two tests are deterministic and do NOT call the OpenAI API.
Test 3 is a live integration test that calls the gpt-image-2 endpoint
and costs money. It is opt-in via ``--live`` on the command line.

Run from the project root:
    python -m tests.test_image_generator                 # deterministic only
    python -m tests.test_image_generator --live           # also runs test 3
"""

from __future__ import annotations

import io
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


def test_preprocess_small_image_no_resize() -> None:
    """Small image: no resize needed, but converted to a JPEG buffer."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.png")
        _write_tiny_png(src)

        buf = image_generator._preprocess_for_openai(src)
        buf.seek(0)
        out = Image.open(buf)
        _check(
            "4. _preprocess_for_openai — small image returns JPEG buffer at original size",
            out.format == "JPEG"
            and out.size == (100, 100)
            and getattr(buf, "name", "").endswith(".jpg"),
            f"format={out.format}, size={out.size}, name={getattr(buf, 'name', None)!r}",
        )


def test_preprocess_large_image_resized() -> None:
    """Image with longest side > 2048 gets scaled so longest side == 2048."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "big.jpg")
        Image.new("RGB", (4000, 3000), color=(100, 150, 200)).save(src, "JPEG")

        buf = image_generator._preprocess_for_openai(src)
        buf.seek(0)
        out = Image.open(buf)
        # 4000 → 2048, 3000 → 1536 (rounded from 1536.0).
        _check(
            "5. _preprocess_for_openai — image with longest side >2048 is scaled to 2048",
            out.format == "JPEG"
            and max(out.size) == 2048
            and out.size == (2048, 1536),
            f"format={out.format}, size={out.size}",
        )


def test_preprocess_quality_fallback_when_over_budget() -> None:
    """When quality-85 encoding exceeds the size budget, fall back to quality 70."""
    from PIL import Image

    saved_budget = image_generator._SIZE_BUDGET_BYTES
    image_generator._SIZE_BUDGET_BYTES = 1  # force any encoded buffer to exceed budget
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.png")
            _write_tiny_png(src)

            # Reference: what a quality-70 encoding of the same image looks like.
            opened = Image.open(src).convert("RGB")
            ref = io.BytesIO()
            opened.save(ref, format="JPEG", quality=70, optimize=True)
            expected_size = ref.tell()

            buf = image_generator._preprocess_for_openai(src)
            buf.seek(0, io.SEEK_END)
            actual_size = buf.tell()

            _check(
                "6. _preprocess_for_openai — falls back to quality 70 when over budget",
                actual_size == expected_size,
                f"expected_size={expected_size}, actual_size={actual_size}",
            )
    finally:
        image_generator._SIZE_BUDGET_BYTES = saved_budget


def test_preprocess_handles_rgba_input() -> None:
    """RGBA input gets converted to RGB so JPEG encoding works."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "rgba.png")
        Image.new("RGBA", (200, 200), color=(255, 0, 0, 128)).save(src, "PNG")

        buf = image_generator._preprocess_for_openai(src)
        buf.seek(0)
        out = Image.open(buf)
        _check(
            "7. _preprocess_for_openai — RGBA input is converted to RGB and encoded as JPEG",
            out.format == "JPEG" and out.mode == "RGB",
            f"format={out.format}, mode={out.mode}",
        )


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
    test_preprocess_small_image_no_resize()
    test_preprocess_large_image_resized()
    test_preprocess_quality_fallback_when_over_budget()
    test_preprocess_handles_rgba_input()

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
