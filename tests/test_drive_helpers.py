"""Tests for tools/drive_helpers.py.

Validates Drive operations against real TES Drive data.
Prerequisite: Run `python tools/google_auth.py` first to generate the token.

Run from the project root:
    python -m tests.test_drive_helpers
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from tools.config_loader import load_config  # noqa: E402
from tools import drive_helpers  # noqa: E402


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


def _expect_raises(name: str, exc_type: type, fn) -> Exception | None:
    try:
        result = fn()
    except exc_type as e:
        _check(name, True, str(e))
        return e
    except Exception as e:
        _check(
            name,
            False,
            f"expected {exc_type.__name__}, got {type(e).__name__}: {e}",
        )
        return None
    _check(
        name,
        False,
        f"expected {exc_type.__name__}, got result: {result!r}",
    )
    return None


def run_tests() -> int:
    print(f"Loading config from: {CONFIG_FILE}")
    config = load_config()

    # 1. Service creation
    try:
        service = drive_helpers.get_drive_service()
        _check("1. Service creation — get_drive_service() returns a service", service is not None)
    except RuntimeError as exc:
        _check(
            "1. Service creation — get_drive_service() returns a service",
            False,
            f"{exc} (run 'python tools/google_auth.py' first)",
        )
        print("\nCannot continue without an authenticated Drive service.")
        return 1

    # 2. Read Google Doc (skill file with unresolved placeholders)
    image_skill_id = config.get("skills.image_prompt_universal")
    try:
        text = drive_helpers.read_file_content(image_skill_id, service=service)
        _check(
            "2. Read Google Doc — image_prompt_universal contains {{BUSINESS_NAME}}",
            isinstance(text, str) and len(text) > 0 and "{{BUSINESS_NAME}}" in text,
            f"len={len(text) if isinstance(text, str) else 'N/A'}, "
            f"has_token={'{{BUSINESS_NAME}}' in text if isinstance(text, str) else 'N/A'}",
        )
    except Exception as e:
        _check("2. Read Google Doc — image_prompt_universal skill", False, f"{type(e).__name__}: {e}")

    # 3. Read brand voice file
    brand_voice_id = config.get("drive.brand_voice_file_id")
    try:
        text = drive_helpers.read_file_content(brand_voice_id, service=service)
        _check(
            "3. Read brand voice — returns non-empty string",
            isinstance(text, str) and len(text) > 0,
            f"len={len(text) if isinstance(text, str) else 'N/A'}",
        )
    except Exception as e:
        _check("3. Read brand voice", False, f"{type(e).__name__}: {e}")

    # 4. List files in generated images folder (may be empty)
    gen_folder_id = config.get("drive.generated_images_folder_id")
    try:
        gen_files = drive_helpers.list_files_in_folder(gen_folder_id, service=service)
        _check(
            "4. List files in folder — generated_images folder returns a list",
            isinstance(gen_files, list),
            f"got {type(gen_files).__name__}",
        )
    except Exception as e:
        _check("4. List files in folder", False, f"{type(e).__name__}: {e}")

    # 5. List catalog images (should be non-empty)
    catalog_folder_id = config.get("catalog.image_folder_id")
    catalog_files: list[dict] = []
    try:
        catalog_files = drive_helpers.list_files_in_folder(catalog_folder_id, service=service)
        _check(
            "5. List catalog images — returns a non-empty list",
            isinstance(catalog_files, list) and len(catalog_files) > 0,
            f"got {len(catalog_files) if isinstance(catalog_files, list) else 'N/A'} files",
        )
    except Exception as e:
        _check("5. List catalog images", False, f"{type(e).__name__}: {e}")

    # 6. Download an image
    tmp_dir = PROJECT_ROOT / ".tmp"
    download_path = tmp_dir / "test_download.jpg"
    if catalog_files:
        first_image = next(
            (f for f in catalog_files if (f.get("mimeType") or "").startswith("image/")),
            catalog_files[0],
        )
        try:
            result_path = drive_helpers.download_file(
                first_image["id"], download_path, service=service
            )
            ok = (
                result_path == str(download_path)
                and download_path.is_file()
                and download_path.stat().st_size > 0
            )
            _check(
                "6. Download image — file written to .tmp and non-empty",
                ok,
                f"path={result_path}, exists={download_path.is_file()}, "
                f"size={download_path.stat().st_size if download_path.is_file() else 0}",
            )
        except Exception as e:
            _check("6. Download image", False, f"{type(e).__name__}: {e}")
        finally:
            if download_path.is_file():
                download_path.unlink()
    else:
        _check("6. Download image", False, "no catalog files available to download")

    # 7. Upload and delete a test file
    test_upload = tmp_dir / "drive_helpers_test_upload.txt"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    test_upload.write_text("drive_helpers test upload — safe to delete\n", encoding="utf-8")
    uploaded_id: str | None = None
    try:
        uploaded_id = drive_helpers.upload_file(
            test_upload, gen_folder_id, service=service
        )
        _check(
            "7. Upload file — returns non-empty Drive file ID",
            isinstance(uploaded_id, str) and len(uploaded_id) > 0,
            f"id={uploaded_id!r}",
        )
    except Exception as e:
        _check("7. Upload file", False, f"{type(e).__name__}: {e}")
    finally:
        if test_upload.is_file():
            test_upload.unlink()
        if uploaded_id:
            try:
                service.files().delete(fileId=uploaded_id).execute()
            except Exception as e:
                print(f"  WARN  could not delete test upload {uploaded_id}: {e}")

    # 8. Read nonexistent file raises FileNotFoundError
    _expect_raises(
        "8. Read nonexistent file raises FileNotFoundError",
        FileNotFoundError,
        lambda: drive_helpers.read_file_content(
            "nonexistent_file_id_12345", service=service
        ),
    )

    # 9. Auth error message when token path is invalid
    original_token = os.environ.get("GOOGLE_TOKEN_PATH")
    os.environ["GOOGLE_TOKEN_PATH"] = str(PROJECT_ROOT / ".tmp" / "no_such_token.json")
    try:
        exc = _expect_raises(
            "9. Auth error — missing token raises RuntimeError mentioning google_auth.py",
            RuntimeError,
            lambda: drive_helpers.get_drive_service(),
        )
        if exc is not None:
            _check(
                "9b. Auth error message mentions 'google_auth.py'",
                "google_auth.py" in str(exc),
                f"got: {exc!s}",
            )
    finally:
        if original_token is None:
            os.environ.pop("GOOGLE_TOKEN_PATH", None)
        else:
            os.environ["GOOGLE_TOKEN_PATH"] = original_token

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
