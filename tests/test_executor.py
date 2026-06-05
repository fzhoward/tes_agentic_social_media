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

import json
import os
import sys
import tempfile
import threading
import traceback
from contextlib import ExitStack
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


# ---------------------------------------------------------------------------
# POST /slack/events — Slack Events API edited-caption capture
#
# The endpoint verifies the Slack signature over the RAW body, answers the
# url_verification challenge synchronously, and otherwise spawns a daemon
# thread targeting _dispatch_event(inner_event) and acks 200. Like the media
# route, _dispatch_event imports its deps LAZILY (`from tools import
# sheets_helpers`, `from tools.config_loader import load_config`), so we patch
# at the source and patch executor's own module-level helpers (_run_cli,
# _resolve_tab_name, _verify_slack_signature) directly on `executor`.
#
# Endpoint-level tests replace threading.Thread with a recording stub so the
# worker dispatch is observable and deterministic (no real thread). Worker-guard
# tests call _dispatch_event(event) directly.
# ---------------------------------------------------------------------------

_EVENT_THREAD_TS = "1700000000.000100"

_VALID_EVENT = {
    "type": "message",
    "text": "Here is my revised caption",
    "thread_ts": _EVENT_THREAD_TS,
    "ts": "1700000050.000200",
    "channel": "C123",
    "user": "U123",
}


def _recording_thread():
    """Return (created, _Thread): a Thread stub that records constructions.

    .start() is a no-op — the worker is NOT run; tests assert on what the
    endpoint *handed* to the thread (target + args), keeping them deterministic.
    """
    created: list = []

    class _Thread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None,
                     **_extra):
            self.target = target
            self.args = args
            self.daemon = daemon
            created.append(self)

        def start(self) -> None:
            pass

    return created, _Thread


def _patch_row_read(stack: ExitStack, *, find_return=None,
                    find_side_effect=None):
    """Patch the lazy row-read deps used by _dispatch_event; return the find Mock.

    Mirrors the /media tests' source-level patching (tools.sheets_helpers.*,
    tools.config_loader.load_config) plus executor._resolve_tab_name.
    """
    cfg = Mock()
    cfg.get.return_value = "sheet-123"
    stack.enter_context(
        patch("tools.config_loader.load_config", return_value=cfg))
    stack.enter_context(
        patch("tools.sheets_helpers.get_sheets_service", return_value=Mock()))
    stack.enter_context(
        patch.object(executor, "_resolve_tab_name", return_value="Sheet1"))
    if find_side_effect is not None:
        return stack.enter_context(patch(
            "tools.sheets_helpers.find_rows_by_column_value",
            side_effect=find_side_effect))
    return stack.enter_context(patch(
        "tools.sheets_helpers.find_rows_by_column_value",
        return_value=find_return if find_return is not None else []))


# --- Endpoint level --------------------------------------------------------

def test_events_bad_signature_401() -> None:
    # Invalid Slack signature → 401, and the worker thread is never spawned.
    created, fake_thread = _recording_thread()
    with patch.object(executor, "_verify_slack_signature", return_value=False), \
            patch.object(executor.threading, "Thread", fake_thread):
        client = executor.app.test_client()
        resp = client.post(
            "/slack/events",
            data=json.dumps({"type": "event_callback", "event": _VALID_EVENT}),
            content_type="application/json",
        )
        status = resp.status_code
        body = resp.get_json()

    _check(
        "11. /slack/events — bad signature → 401, worker never spawned",
        status == 401
        and (body or {}).get("error") == "invalid signature"
        and created == [],
        f"status={status}, body={body!r}, threads={len(created)}",
    )


def test_events_url_verification_handshake() -> None:
    # url_verification → synchronous 200 echoing the challenge; no worker.
    created, fake_thread = _recording_thread()
    with patch.object(executor, "_verify_slack_signature", return_value=True), \
            patch.object(executor.threading, "Thread", fake_thread):
        client = executor.app.test_client()
        resp = client.post(
            "/slack/events",
            data=json.dumps(
                {"type": "url_verification", "challenge": "abc123"}),
            content_type="application/json",
        )
        status = resp.status_code
        body = resp.get_json()

    _check(
        "12. /slack/events — url_verification → 200 {challenge}, no worker",
        status == 200
        and body == {"challenge": "abc123"}
        and created == [],
        f"status={status}, body={body!r}, threads={len(created)}",
    )


def test_events_event_callback_acks_and_spawns_worker() -> None:
    # event_callback (valid sig) → 200 and a worker thread targeting
    # _dispatch_event with the inner event.
    created, fake_thread = _recording_thread()
    with patch.object(executor, "_verify_slack_signature", return_value=True), \
            patch.object(executor.threading, "Thread", fake_thread):
        client = executor.app.test_client()
        resp = client.post(
            "/slack/events",
            data=json.dumps({"type": "event_callback", "event": _VALID_EVENT}),
            content_type="application/json",
        )
        status = resp.status_code

    spawned = created[0] if created else None
    _check(
        "13. /slack/events — event_callback → 200, worker spawned with event",
        status == 200
        and len(created) == 1
        and spawned.target is executor._dispatch_event
        and spawned.args == (_VALID_EVENT,)
        and spawned.daemon is True,
        f"status={status}, threads={len(created)}, "
        f"target={getattr(spawned, 'target', None)}, "
        f"args={getattr(spawned, 'args', None)!r}",
    )


def test_events_malformed_json_400() -> None:
    # Valid signature but the body is not JSON → 400, no worker.
    created, fake_thread = _recording_thread()
    with patch.object(executor, "_verify_slack_signature", return_value=True), \
            patch.object(executor.threading, "Thread", fake_thread):
        client = executor.app.test_client()
        resp = client.post(
            "/slack/events",
            data="not json{",
            content_type="application/json",
        )
        status = resp.status_code
        body = resp.get_json()

    _check(
        "14. /slack/events — malformed JSON → 400, no worker",
        status == 400
        and (body or {}).get("error") == "bad json"
        and created == [],
        f"status={status}, body={body!r}, threads={len(created)}",
    )


# --- Worker guards (call _dispatch_event directly) -------------------------

def test_event_non_message_ignored() -> None:
    # event.type != "message" → ignored before any row read or dispatch.
    event = {"type": "reaction_added", "thread_ts": _EVENT_THREAD_TS,
             "ts": "x", "text": "hi"}
    with ExitStack() as stack:
        find = _patch_row_read(stack)
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        executor._dispatch_event(event)

    _check(
        "15. _dispatch_event — non-message type ignored (no read, no dispatch)",
        find.called is False and run.called is False,
        f"find_called={find.called}, run_called={run.called}",
    )


def test_event_bot_message_ignored() -> None:
    # A message carrying bot_id is the bot's own post → ignored.
    event = {"type": "message", "bot_id": "B123",
             "thread_ts": _EVENT_THREAD_TS, "ts": "x", "text": "hi"}
    with ExitStack() as stack:
        find = _patch_row_read(stack)
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        executor._dispatch_event(event)

    _check(
        "16. _dispatch_event — bot_id message ignored",
        find.called is False and run.called is False,
        f"find_called={find.called}, run_called={run.called}",
    )


def test_event_subtype_ignored() -> None:
    # A subtype (e.g. message_changed/edited) → ignored.
    event = {"type": "message", "subtype": "message_changed",
             "thread_ts": _EVENT_THREAD_TS, "ts": "x", "text": "hi"}
    with ExitStack() as stack:
        find = _patch_row_read(stack)
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        executor._dispatch_event(event)

    _check(
        "17. _dispatch_event — subtype (message_changed) ignored",
        find.called is False and run.called is False,
        f"find_called={find.called}, run_called={run.called}",
    )


def test_event_no_thread_ts_ignored() -> None:
    # Top-level message (no thread_ts) does not map to a card → ignored.
    event = {"type": "message", "ts": "1700000050.000200",
             "text": "hello channel"}
    with ExitStack() as stack:
        find = _patch_row_read(stack)
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        executor._dispatch_event(event)

    _check(
        "18. _dispatch_event — no thread_ts ignored",
        find.called is False and run.called is False,
        f"find_called={find.called}, run_called={run.called}",
    )


def test_event_parent_message_ignored() -> None:
    # The thread parent itself (thread_ts == ts) is not a reply → ignored.
    event = {"type": "message", "thread_ts": _EVENT_THREAD_TS,
             "ts": _EVENT_THREAD_TS, "text": "the original card text"}
    with ExitStack() as stack:
        find = _patch_row_read(stack)
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        executor._dispatch_event(event)

    _check(
        "19. _dispatch_event — parent message (thread_ts == ts) ignored",
        find.called is False and run.called is False,
        f"find_called={find.called}, run_called={run.called}",
    )


def test_event_empty_text_ignored() -> None:
    # Whitespace-only reply text → ignored.
    event = {"type": "message", "thread_ts": _EVENT_THREAD_TS,
             "ts": "1700000050.000200", "text": "   \n  "}
    with ExitStack() as stack:
        find = _patch_row_read(stack)
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        executor._dispatch_event(event)

    _check(
        "20. _dispatch_event — empty/whitespace text ignored",
        find.called is False and run.called is False,
        f"find_called={find.called}, run_called={run.called}",
    )


def test_event_row_not_found() -> None:
    # No row matches thread_ts → no dispatch, no raise.
    with ExitStack() as stack:
        find = _patch_row_read(stack, find_return=[])
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        raised = False
        try:
            executor._dispatch_event(dict(_VALID_EVENT))
        except Exception:  # noqa: BLE001
            raised = True

    _check(
        "21. _dispatch_event — no matching row → no dispatch, no raise",
        find.called is True and run.called is False and raised is False,
        f"find_called={find.called}, run_called={run.called}, raised={raised}",
    )


def test_event_row_read_raises_swallowed() -> None:
    # The row lookup raises → swallowed (background thread), no dispatch.
    with ExitStack() as stack:
        find = _patch_row_read(stack, find_side_effect=Exception("sheets boom"))
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        raised = False
        try:
            executor._dispatch_event(dict(_VALID_EVENT))
        except Exception:  # noqa: BLE001
            raised = True

    _check(
        "22. _dispatch_event — row read raises → swallowed, no dispatch",
        find.called is True and run.called is False and raised is False,
        f"find_called={find.called}, run_called={run.called}, raised={raised}",
    )


def test_event_status_not_drafted_guard() -> None:
    # Matched row not at 'drafted' (e.g. a reject-reason thread reply lands on
    # an awaiting_approval/rejected row) → load-bearing guard blocks dispatch.
    rows = [(2, {"row_id": "ROW-1", "status": "awaiting_approval"})]
    with ExitStack() as stack:
        find = _patch_row_read(stack, find_return=rows)
        run = stack.enter_context(patch.object(executor, "_run_cli", Mock()))
        executor._dispatch_event(dict(_VALID_EVENT))

    _check(
        "23. _dispatch_event — status != 'drafted' → no dispatch (reject guard)",
        find.called is True and run.called is False,
        f"find_called={find.called}, run_called={run.called}",
    )


def test_event_happy_path_dispatches_edit_commit() -> None:
    # Matched 'drafted' row + valid threaded reply → shell out exactly once to
    # the router's --edit-commit with the row_id and the reply text.
    rows = [(2, {"row_id": "ROW-42", "status": "drafted"})]
    run = Mock(return_value=(0, {"success": True}))
    with ExitStack() as stack:
        find = _patch_row_read(stack, find_return=rows)
        stack.enter_context(patch.object(executor, "_run_cli", run))
        executor._dispatch_event(dict(_VALID_EVENT))

    expected = [
        "tools.approval_router",
        "--edit-commit",
        "--row-id",
        "ROW-42",
        "--caption-text",
        _VALID_EVENT["text"],
    ]
    called_args = run.call_args.args[0] if run.call_args else None
    _check(
        "24. _dispatch_event — drafted row → _run_cli --edit-commit with args",
        find.called is True
        and run.call_count == 1
        and called_args == expected,
        f"run_count={run.call_count}, args={called_args!r}",
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
    # S44 Piece 2b — /slack/events endpoint + _dispatch_event guards
    test_events_bad_signature_401()
    test_events_url_verification_handshake()
    test_events_event_callback_acks_and_spawns_worker()
    test_events_malformed_json_400()
    test_event_non_message_ignored()
    test_event_bot_message_ignored()
    test_event_subtype_ignored()
    test_event_no_thread_ts_ignored()
    test_event_parent_message_ignored()
    test_event_empty_text_ignored()
    test_event_row_not_found()
    test_event_row_read_raises_swallowed()
    test_event_status_not_drafted_guard()
    test_event_happy_path_dispatches_edit_commit()

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
