"""Tests for tools/executor.py — the async /run/strategist route and the
HMAC-guarded GET /media/<row_id>/<token> media-serving route.

Native accumulator harness (NOT pytest), mirroring tests/test_strategist.py:
module-level _PASSED / _FAILURES, a non-raising _check, plain test_* functions,
and a run_tests() entrypoint.

This uses Flask's test client, so it requires Flask — which lives in the repo
venv. Run with the venv interpreter:

    .venv/bin/python -m tests.test_executor
    # or
    .venv/bin/python tests/test_executor.py

Chosen approach: Option A (tiny Flask test-client check). It is low-friction —
Flask is already a dependency, app.test_client() needs no server, and
_dispatch_strategist / _check_bearer monkeypatch cleanly (both are looked up as
module globals inside the route, so reassigning them on the module takes effect
at call time). No subprocess runs, no API calls, no Slack posts.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import executor  # noqa: E402


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


def test_strategist_route_returns_202_and_dispatches() -> None:
    # Fake the background dispatch (so no subprocess runs) and force the bearer
    # check to pass. The route spawns a daemon thread targeting
    # _dispatch_strategist; the Event lets us wait for it deterministically.
    recorded: list = []
    fired = threading.Event()
    real_dispatch = executor._dispatch_strategist
    real_bearer = executor._check_bearer

    def _fake_dispatch() -> None:
        recorded.append(True)
        fired.set()

    executor._dispatch_strategist = _fake_dispatch  # type: ignore[assignment]
    executor._check_bearer = lambda: True  # type: ignore[assignment]
    try:
        client = executor.app.test_client()
        resp = client.post("/run/strategist")
        dispatched = fired.wait(timeout=5)
        status = resp.status_code
        body = resp.get_json()
    finally:
        executor._dispatch_strategist = real_dispatch
        executor._check_bearer = real_bearer

    _check(
        "1. /run/strategist — 202 {accepted: true} and dispatch thread invoked",
        status == 202
        and body == {"accepted": True}
        and dispatched
        and recorded == [True],
        f"status={status}, body={body!r}, dispatched={dispatched}, "
        f"recorded={recorded}",
    )


def test_strategist_route_unauthorized() -> None:
    # Bad/missing bearer → 401 and the dispatch must never fire.
    called: list = []
    real_dispatch = executor._dispatch_strategist
    real_bearer = executor._check_bearer
    executor._dispatch_strategist = (
        lambda: called.append(True)  # type: ignore[assignment]
    )
    executor._check_bearer = lambda: False  # type: ignore[assignment]
    try:
        client = executor.app.test_client()
        resp = client.post("/run/strategist")
        status = resp.status_code
        body = resp.get_json()
    finally:
        executor._dispatch_strategist = real_dispatch
        executor._check_bearer = real_bearer

    _check(
        "2. /run/strategist — 401 when bearer check fails, no dispatch",
        status == 401 and called == [] and (body or {}).get("success") is False,
        f"status={status}, body={body!r}, called={called}",
    )


# ---------------------------------------------------------------------------
# GET /media/<row_id>/<token> — HMAC-guarded media serving
#
# NOTE on patching: the route imports its deps LAZILY inside the function body
# (`from tools import sheets_helpers`, `from tools.config_loader import
# load_config`), so there is no `executor.sheets_helpers` global to reassign.
# We patch at the source (tools.sheets_helpers.*, tools.config_loader.*). The
# executor's own module-level helpers (_resolve_tab_name, _drive_media_bytes,
# _local_media_file, _sign_media_token) are patched directly on `executor`.
# `MEDIA_URL_SECRET` is read via os.environ.get inside the route, so we set or
# remove it with patch.dict (auto-restored on exit).
# ---------------------------------------------------------------------------

_MEDIA_ROW_ID = "STR-20260608-FB-01"


def test_media_drive_path_success() -> None:
    # Valid signed request + Drive fetch succeeds → 200 with the exact bytes
    # and mimetype the Drive helper returned.
    secret = "secret-drive-success"
    cfg = Mock()
    cfg.get.return_value = "sheet-123"
    rows = [(2, {"row_id": _MEDIA_ROW_ID, "media_url": "drive-file-id-123"})]
    with patch.dict(os.environ, {"MEDIA_URL_SECRET": secret}, clear=False), \
            patch("tools.config_loader.load_config", return_value=cfg), \
            patch("tools.sheets_helpers.get_sheets_service", return_value=Mock()), \
            patch.object(executor, "_resolve_tab_name", return_value="Sheet1"), \
            patch("tools.sheets_helpers.find_rows_by_column_value",
                  return_value=rows), \
            patch.object(executor, "_drive_media_bytes",
                         return_value=(b"\xff\xd8fakejpegbytes", "image/jpeg")):
        token = executor._sign_media_token(_MEDIA_ROW_ID)
        client = executor.app.test_client()
        resp = client.get(f"/media/{_MEDIA_ROW_ID}/{token}")
        status = resp.status_code
        data = resp.data
        mimetype = resp.mimetype

    _check(
        "3. /media — valid token + Drive success → 200, image bytes, image/jpeg",
        status == 200
        and data == b"\xff\xd8fakejpegbytes"
        and mimetype == "image/jpeg",
        f"status={status}, data={data!r}, mimetype={mimetype!r}",
    )


def test_media_missing_secret_403() -> None:
    # MEDIA_URL_SECRET unset → 403 regardless of token, and the row lookup is
    # never reached.
    find_mock = Mock()
    with patch.dict(os.environ, {}, clear=False), \
            patch("tools.sheets_helpers.find_rows_by_column_value", find_mock):
        os.environ.pop("MEDIA_URL_SECRET", None)
        client = executor.app.test_client()
        resp = client.get(f"/media/{_MEDIA_ROW_ID}/anytoken")
        status = resp.status_code
        called = find_mock.called

    _check(
        "4. /media — missing MEDIA_URL_SECRET → 403, no row lookup",
        status == 403 and called is False,
        f"status={status}, find_called={called}",
    )


def test_media_bad_token_403() -> None:
    # Secret set but token does not match → 403, lookup never reached.
    secret = "secret-bad-token"
    find_mock = Mock()
    with patch.dict(os.environ, {"MEDIA_URL_SECRET": secret}, clear=False), \
            patch("tools.sheets_helpers.find_rows_by_column_value", find_mock):
        client = executor.app.test_client()
        resp = client.get(f"/media/{_MEDIA_ROW_ID}/wrongtoken")
        status = resp.status_code
        called = find_mock.called

    _check(
        "5. /media — bad token → 403, no row lookup",
        status == 403 and called is False,
        f"status={status}, find_called={called}",
    )


def test_media_row_not_found_404() -> None:
    # Valid token but no matching row → 404.
    secret = "secret-no-row"
    cfg = Mock()
    cfg.get.return_value = "sheet-123"
    with patch.dict(os.environ, {"MEDIA_URL_SECRET": secret}, clear=False), \
            patch("tools.config_loader.load_config", return_value=cfg), \
            patch("tools.sheets_helpers.get_sheets_service", return_value=Mock()), \
            patch.object(executor, "_resolve_tab_name", return_value="Sheet1"), \
            patch("tools.sheets_helpers.find_rows_by_column_value",
                  return_value=[]):
        token = executor._sign_media_token(_MEDIA_ROW_ID)
        client = executor.app.test_client()
        resp = client.get(f"/media/{_MEDIA_ROW_ID}/{token}")
        status = resp.status_code

    _check(
        "6. /media — row not found → 404",
        status == 404,
        f"status={status}",
    )


def test_media_drive_fails_tmp_fallback_success() -> None:
    # Drive fetch raises → fall back to the local .tmp file and serve it.
    secret = "secret-tmp-fallback"
    cfg = Mock()
    cfg.get.return_value = "sheet-123"
    rows = [(2, {"row_id": _MEDIA_ROW_ID, "media_url": "drive-file-id-123"})]

    fd, tmp_name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_path.write_bytes(b"\x89PNG-local-fallback-bytes")
    try:
        with patch.dict(os.environ, {"MEDIA_URL_SECRET": secret}, clear=False), \
                patch("tools.config_loader.load_config", return_value=cfg), \
                patch("tools.sheets_helpers.get_sheets_service", return_value=Mock()), \
                patch.object(executor, "_resolve_tab_name", return_value="Sheet1"), \
                patch("tools.sheets_helpers.find_rows_by_column_value",
                      return_value=rows), \
                patch.object(executor, "_drive_media_bytes",
                             side_effect=RuntimeError("drive down")), \
                patch.object(executor, "_local_media_file",
                             return_value=tmp_path):
            token = executor._sign_media_token(_MEDIA_ROW_ID)
            client = executor.app.test_client()
            resp = client.get(f"/media/{_MEDIA_ROW_ID}/{token}")
            status = resp.status_code
            data = resp.data
            mimetype = resp.mimetype
    finally:
        tmp_path.unlink(missing_ok=True)

    _check(
        "7. /media — Drive fails, .tmp fallback → 200, local bytes, image/png",
        status == 200
        and data == b"\x89PNG-local-fallback-bytes"
        and mimetype == "image/png",
        f"status={status}, data={data!r}, mimetype={mimetype!r}",
    )


def test_media_empty_url_tmp_fallback() -> None:
    # Empty media_url (no Drive id) → never call Drive, serve the local file.
    secret = "secret-empty-url"
    cfg = Mock()
    cfg.get.return_value = "sheet-123"
    rows = [(2, {"row_id": _MEDIA_ROW_ID, "media_url": ""})]
    drive_mock = Mock()

    fd, tmp_name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_path.write_bytes(b"\x89PNG-no-drive-id-bytes")
    try:
        with patch.dict(os.environ, {"MEDIA_URL_SECRET": secret}, clear=False), \
                patch("tools.config_loader.load_config", return_value=cfg), \
                patch("tools.sheets_helpers.get_sheets_service", return_value=Mock()), \
                patch.object(executor, "_resolve_tab_name", return_value="Sheet1"), \
                patch("tools.sheets_helpers.find_rows_by_column_value",
                      return_value=rows), \
                patch.object(executor, "_drive_media_bytes", drive_mock), \
                patch.object(executor, "_local_media_file",
                             return_value=tmp_path):
            token = executor._sign_media_token(_MEDIA_ROW_ID)
            client = executor.app.test_client()
            resp = client.get(f"/media/{_MEDIA_ROW_ID}/{token}")
            status = resp.status_code
            data = resp.data
            mimetype = resp.mimetype
            drive_called = drive_mock.called
    finally:
        tmp_path.unlink(missing_ok=True)

    _check(
        "8. /media — empty media_url, .tmp present → 200, local, Drive not called",
        status == 200
        and data == b"\x89PNG-no-drive-id-bytes"
        and mimetype == "image/png"
        and drive_called is False,
        f"status={status}, data={data!r}, mimetype={mimetype!r}, "
        f"drive_called={drive_called}",
    )


def test_media_no_drive_no_local_404() -> None:
    # Empty media_url and no local file → 404.
    secret = "secret-nothing"
    cfg = Mock()
    cfg.get.return_value = "sheet-123"
    rows = [(2, {"row_id": _MEDIA_ROW_ID, "media_url": ""})]
    with patch.dict(os.environ, {"MEDIA_URL_SECRET": secret}, clear=False), \
            patch("tools.config_loader.load_config", return_value=cfg), \
            patch("tools.sheets_helpers.get_sheets_service", return_value=Mock()), \
            patch.object(executor, "_resolve_tab_name", return_value="Sheet1"), \
            patch("tools.sheets_helpers.find_rows_by_column_value",
                  return_value=rows), \
            patch.object(executor, "_local_media_file", return_value=None):
        token = executor._sign_media_token(_MEDIA_ROW_ID)
        client = executor.app.test_client()
        resp = client.get(f"/media/{_MEDIA_ROW_ID}/{token}")
        status = resp.status_code

    _check(
        "9. /media — no Drive id and no local file → 404",
        status == 404,
        f"status={status}",
    )


def test_media_lookup_raises_404() -> None:
    # The row lookup raises (e.g. Sheets error) → route catches it → 404,
    # never a 500 stacktrace to the caller.
    secret = "secret-lookup-boom"
    cfg = Mock()
    cfg.get.return_value = "sheet-123"
    with patch.dict(os.environ, {"MEDIA_URL_SECRET": secret}, clear=False), \
            patch("tools.config_loader.load_config", return_value=cfg), \
            patch("tools.sheets_helpers.get_sheets_service", return_value=Mock()), \
            patch.object(executor, "_resolve_tab_name", return_value="Sheet1"), \
            patch("tools.sheets_helpers.find_rows_by_column_value",
                  side_effect=Exception("boom")):
        token = executor._sign_media_token(_MEDIA_ROW_ID)
        client = executor.app.test_client()
        resp = client.get(f"/media/{_MEDIA_ROW_ID}/{token}")
        status = resp.status_code

    _check(
        "10. /media — lookup raises → 404 (not 500)",
        status == 404,
        f"status={status}",
    )


def run_tests() -> int:
    print()
    print("Deterministic tests (no API calls, no subprocess, no Slack):")
    test_strategist_route_returns_202_and_dispatches()
    test_strategist_route_unauthorized()
    test_media_drive_path_success()
    test_media_missing_secret_403()
    test_media_bad_token_403()
    test_media_row_not_found_404()
    test_media_drive_fails_tmp_fallback_success()
    test_media_empty_url_tmp_fallback()
    test_media_no_drive_no_local_404()
    test_media_lookup_raises_404()

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
