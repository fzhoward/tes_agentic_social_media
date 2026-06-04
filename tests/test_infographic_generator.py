"""Tests for tools/infographic_generator.py.

ALL tests here are deterministic and make NO real Gemini API call (no
credits, no network). The tool imports ``google.genai`` lazily inside
``generate_infographic``, so we inject a fake ``google.genai`` /
``google.genai.types`` into ``sys.modules`` for the duration of each test
(mirrors the save/restore monkeypatching style used elsewhere in this repo).

Run from the project root:
    python -m tests.test_infographic_generator
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import traceback
import types as _pytypes
from pathlib import Path

# Snapshot whether we are running under pytest BEFORE our guarded import of
# pytest below (matches the convention in tests/test_drafter.py).
_UNDER_PYTEST = "pytest" in sys.modules

try:
    import pytest  # type: ignore  # noqa: F401
except ImportError:  # native runner must work without pytest installed
    pytest = None  # type: ignore

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import infographic_generator  # noqa: E402


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
# Fake google.genai SDK
# ----------------------------------------------------------------------


class _FakeImageConfig:
    """Stand-in for ``types.ImageConfig`` — records the kwargs it's built with."""

    def __init__(self, aspect_ratio=None, image_size=None):
        self.aspect_ratio = aspect_ratio
        self.image_size = image_size


class _FakeGenerateContentConfig:
    """Stand-in for ``types.GenerateContentConfig``."""

    def __init__(self, response_modalities=None, image_config=None):
        self.response_modalities = response_modalities
        self.image_config = image_config


class _FakeInlineData:
    def __init__(self, mime_type):
        self.mime_type = mime_type


class _FakeImage:
    """Stand-in for ``part.as_image()`` — ``.save(path)`` writes raw bytes."""

    def __init__(self, record, raise_on_save=False):
        self._record = record
        self._raise_on_save = raise_on_save

    def save(self, path):
        self._record.save_paths.append(path)
        if self._raise_on_save:
            raise OSError("simulated save failure")
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0FAKEJPEGBYTES")  # JPEG SOI marker + filler


class _FakePart:
    def __init__(self, inline_data=None, text=None, image=None):
        self.inline_data = inline_data
        self.text = text
        self._image = image

    def as_image(self):
        return self._image


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts):
        self.content = _FakeContent(parts)


class _FakeResponse:
    def __init__(self, parts=None, candidates=None, usage_metadata=None):
        # The tool reads response.parts then falls back to
        # response.candidates[0].content.parts.
        self.parts = parts if parts is not None else []
        self.candidates = candidates
        self.usage_metadata = usage_metadata


class _GenaiRecord:
    """Captures what the tool did with the fake SDK."""

    def __init__(self):
        self.api_key = "<<unset>>"
        self.client_kwargs = {}
        self.call_count = 0
        self.calls: list[dict] = []
        self.save_paths: list[str] = []


class _FakeModels:
    def __init__(self, impl, record):
        self._impl = impl
        self._record = record

    def generate_content(self, model, contents, config):
        idx = self._record.call_count
        self._record.call_count += 1
        self._record.calls.append(
            {"model": model, "contents": contents, "config": config}
        )
        return self._impl(self._record, idx, model, contents, config)


class _FakeClient:
    def __init__(self, impl, record):
        self.models = _FakeModels(impl, record)


@contextlib.contextmanager
def _fake_genai(impl):
    """Install a fake google.genai SDK into sys.modules for the test body.

    ``impl(record, call_index, model, contents, config)`` returns a fake
    response or raises — letting each test drive the SDK behavior. Yields the
    record so the test can assert on api_key / call_count / save_paths.
    """
    keys = ("google", "google.genai", "google.genai.types")
    saved = {k: sys.modules.get(k) for k in keys}
    record = _GenaiRecord()

    types_mod = _pytypes.ModuleType("google.genai.types")
    types_mod.GenerateContentConfig = _FakeGenerateContentConfig
    types_mod.ImageConfig = _FakeImageConfig

    def client_factory(api_key=None, **kwargs):
        record.api_key = api_key
        record.client_kwargs = kwargs
        return _FakeClient(impl, record)

    genai_mod = _pytypes.ModuleType("google.genai")
    genai_mod.Client = client_factory
    genai_mod.types = types_mod

    google_mod = _pytypes.ModuleType("google")
    google_mod.genai = genai_mod

    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod
    try:
        yield record
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


@contextlib.contextmanager
def _env(**overrides):
    """Temporarily set/unset env vars (value None => remove)."""
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _image_response(record, mime, usage_metadata=None, raise_on_save=False):
    """Build a fake response carrying a single image part of ``mime``."""
    part = _FakePart(
        inline_data=_FakeInlineData(mime),
        image=_FakeImage(record, raise_on_save=raise_on_save),
    )
    return _FakeResponse(parts=[part], usage_metadata=usage_metadata)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_happy_path_jpeg() -> None:
    """JPEG mime → success, output_path corrected to .jpg, file written,
    as_image().save called with the .jpg path; validated config shape used."""
    def impl(record, idx, model, contents, config):
        return _image_response(record, "image/jpeg")

    with tempfile.TemporaryDirectory() as tmp:
        # Pass a path WITHOUT a trusted extension; also nests a dir to exercise
        # makedirs.
        output = os.path.join(tmp, "sub", "infographic")
        with _env(GEMINI_API_KEY="test-key-123", Gemini_API_KEY=None):
            with _fake_genai(impl) as record:
                result = infographic_generator.generate_infographic(
                    prompt="An illustrated infographic about local rentals.",
                    output_path=output,
                )

        real_path = result.get("output_path", "")
        cfg = record.calls[0]["config"] if record.calls else None
        _check(
            "1. happy path JPEG — success, .jpg path, file written, save called "
            "with .jpg path, validated 1:1/1K config",
            result.get("success") is True
            and result.get("error") == ""
            and real_path.endswith(".jpg")
            and os.path.isfile(real_path)
            and record.save_paths[-1] == real_path
            and record.call_count == 1
            and cfg is not None
            and cfg.response_modalities == ["TEXT", "IMAGE"]
            and cfg.image_config.aspect_ratio == "1:1"
            and cfg.image_config.image_size == "1K",
            f"result={result!r}, save_paths={record.save_paths!r}, "
            f"calls={record.call_count}",
        )


def test_png_mime_variant() -> None:
    """PNG mime → output_path corrected to .png."""
    def impl(record, idx, model, contents, config):
        return _image_response(record, "image/png")

    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "infographic")
        with _env(GEMINI_API_KEY="test-key-123", Gemini_API_KEY=None):
            with _fake_genai(impl) as record:
                result = infographic_generator.generate_infographic(
                    prompt="prompt", output_path=output,
                )

        real_path = result.get("output_path", "")
        _check(
            "2. PNG mime variant — output_path ends in .png, file written",
            result.get("success") is True
            and real_path.endswith(".png")
            and os.path.isfile(real_path)
            and record.save_paths[-1] == real_path,
            f"result={result!r}, save_paths={record.save_paths!r}",
        )


def test_no_image_part() -> None:
    """Only a text part (refusal) → success False, error mentions no image
    AND includes the text snippet."""
    refusal = "I can't create that image because it violates policy."

    def impl(record, idx, model, contents, config):
        return _FakeResponse(parts=[_FakePart(text=refusal)])

    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "infographic")
        with _env(GEMINI_API_KEY="test-key-123", Gemini_API_KEY=None):
            with _fake_genai(impl):
                result = infographic_generator.generate_infographic(
                    prompt="prompt", output_path=output,
                )

        err = result.get("error", "")
        _check(
            "3. no image part — success False, error mentions no image + text snippet",
            result.get("success") is False
            and "no image" in err.lower()
            and "can't create that image" in err,
            f"result={result!r}",
        )


def test_candidates_fallback() -> None:
    """response.parts empty but candidates[0].content.parts populated →
    still extracts the image via the fallback path."""
    def impl(record, idx, model, contents, config):
        part = _FakePart(
            inline_data=_FakeInlineData("image/jpeg"),
            image=_FakeImage(record),
        )
        return _FakeResponse(parts=[], candidates=[_FakeCandidate([part])])

    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "infographic")
        with _env(GEMINI_API_KEY="test-key-123", Gemini_API_KEY=None):
            with _fake_genai(impl) as record:
                result = infographic_generator.generate_infographic(
                    prompt="prompt", output_path=output,
                )

        real_path = result.get("output_path", "")
        _check(
            "4. candidates fallback — empty response.parts, image extracted from "
            "candidates[0].content.parts",
            result.get("success") is True
            and real_path.endswith(".jpg")
            and os.path.isfile(real_path)
            and record.save_paths[-1] == real_path,
            f"result={result!r}, save_paths={record.save_paths!r}",
        )


def test_missing_key() -> None:
    """Neither GEMINI_API_KEY nor Gemini_API_KEY set → success False, no raise,
    no SDK call."""
    def impl(record, idx, model, contents, config):  # pragma: no cover - should not run
        raise AssertionError("SDK should not be reached when key is missing")

    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "infographic")
        with _env(GEMINI_API_KEY=None, Gemini_API_KEY=None):
            with _fake_genai(impl) as record:
                result = infographic_generator.generate_infographic(
                    prompt="prompt", output_path=output,
                )
        _check(
            "5. missing key — success False, error mentions key, no SDK call",
            result.get("success") is False
            and "key" in result.get("error", "").lower()
            and record.call_count == 0,
            f"result={result!r}, calls={record.call_count}",
        )


def test_mixed_case_key_only() -> None:
    """Only Gemini_API_KEY set (GEMINI_API_KEY unset) → key resolves and is
    passed EXPLICITLY to genai.Client(api_key=...)."""
    def impl(record, idx, model, contents, config):
        return _image_response(record, "image/jpeg")

    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "infographic")
        with _env(GEMINI_API_KEY=None, Gemini_API_KEY="mixed-case-key-value"):
            with _fake_genai(impl) as record:
                result = infographic_generator.generate_infographic(
                    prompt="prompt", output_path=output,
                )
        _check(
            "6. mixed-case key only — resolves Gemini_API_KEY, client built with "
            "api_key passed explicitly",
            result.get("success") is True
            and record.api_key == "mixed-case-key-value",
            f"result={result!r}, api_key={record.api_key!r}",
        )


def test_model_access_error_not_retried() -> None:
    """PERMISSION_DENIED / billing-style error → success False, NOT retried
    (generate_content called once), error mentions paid-tier access."""
    def impl(record, idx, model, contents, config):
        raise RuntimeError(
            "403 PERMISSION_DENIED: your project lacks billing for this model"
        )

    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "infographic")
        with _env(GEMINI_API_KEY="test-key-123", Gemini_API_KEY=None):
            with _fake_genai(impl) as record:
                result = infographic_generator.generate_infographic(
                    prompt="prompt", output_path=output,
                )
        _check(
            "7. model-access error — success False, called exactly once (no retry), "
            "error mentions paid-tier",
            result.get("success") is False
            and record.call_count == 1
            and "paid-tier" in result.get("error", "").lower(),
            f"result={result!r}, calls={record.call_count}",
        )


def test_transient_error_then_success() -> None:
    """Connection/timeout-classified error on first call, image on second →
    success True, generate_content called twice (retry-once)."""
    def impl(record, idx, model, contents, config):
        if idx == 0:
            raise ConnectionError("connection reset by peer")
        return _image_response(record, "image/jpeg")

    saved_delay = infographic_generator._RETRY_DELAY_SECONDS
    infographic_generator._RETRY_DELAY_SECONDS = 0.0  # don't sleep in tests
    try:
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "infographic")
            with _env(GEMINI_API_KEY="test-key-123", Gemini_API_KEY=None):
                with _fake_genai(impl) as record:
                    result = infographic_generator.generate_infographic(
                        prompt="prompt", output_path=output,
                    )
            real_path = result.get("output_path", "")
            # Assert while the tempdir still exists so isfile() is meaningful.
            _check(
                "8. transient error then success — retried once, success True, "
                "called twice",
                result.get("success") is True
                and record.call_count == 2
                and real_path.endswith(".jpg")
                and os.path.isfile(real_path),
                f"result={result!r}, calls={record.call_count}",
            )
    finally:
        infographic_generator._RETRY_DELAY_SECONDS = saved_delay


def test_as_image_save_raises() -> None:
    """as_image().save() raising is caught → success False, never propagates."""
    def impl(record, idx, model, contents, config):
        return _image_response(record, "image/jpeg", raise_on_save=True)

    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "infographic")
        raised = False
        result = None
        try:
            with _env(GEMINI_API_KEY="test-key-123", Gemini_API_KEY=None):
                with _fake_genai(impl):
                    result = infographic_generator.generate_infographic(
                        prompt="prompt", output_path=output,
                    )
        except Exception:
            raised = True

        _check(
            "9. as_image().save raises — caught, success False, never propagates",
            (not raised)
            and result is not None
            and result.get("success") is False
            and "failed to write output file" in result.get("error", "").lower(),
            f"raised={raised}, result={result!r}",
        )


def test_usage_metadata_present_and_absent() -> None:
    """usage_metadata present → logged without crashing; absent → no crash.
    Both still return success."""
    class _Usage:
        def __repr__(self):
            return "Usage(prompt=10, candidates=20, total=30)"

    def impl_present(record, idx, model, contents, config):
        return _image_response(record, "image/jpeg", usage_metadata=_Usage())

    def impl_absent(record, idx, model, contents, config):
        # usage_metadata defaults to None on _FakeResponse.
        return _image_response(record, "image/jpeg")

    with tempfile.TemporaryDirectory() as tmp:
        with _env(GEMINI_API_KEY="test-key-123", Gemini_API_KEY=None):
            with _fake_genai(impl_present):
                res_present = infographic_generator.generate_infographic(
                    prompt="prompt", output_path=os.path.join(tmp, "with_usage"),
                )
            with _fake_genai(impl_absent):
                res_absent = infographic_generator.generate_infographic(
                    prompt="prompt", output_path=os.path.join(tmp, "no_usage"),
                )

    _check(
        "10. usage_metadata present + absent — both succeed without crashing",
        res_present.get("success") is True
        and res_absent.get("success") is True,
        f"present={res_present!r}, absent={res_absent!r}",
    )


def run_tests() -> int:
    print("Deterministic tests (no API calls, fake google.genai SDK):")
    test_happy_path_jpeg()
    test_png_mime_variant()
    test_no_image_part()
    test_candidates_fallback()
    test_missing_key()
    test_mixed_case_key_only()
    test_model_access_error_not_retried()
    test_transient_error_then_success()
    test_as_image_save_raises()
    test_usage_metadata_present_and_absent()

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
