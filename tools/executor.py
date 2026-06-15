"""HTTP executor for the agentic social media pipeline.

A thin Flask service that lets the orchestration layer (n8n) and Slack
invoke the approval-pipeline CLIs on this machine. It does NOT contain pipeline
logic — it shells out to the existing tools via the repo's venv interpreter and
returns their JSON. Keeping it a dumb runner means a failure in one handler
cannot take down the listener, and the execution layer stays separate from
agent logic (per CLAUDE.md).

Endpoints:
    GET  /healthz                       liveness probe (no auth)
    POST /run/indexer                   Asset Indexer — bearer auth
    POST /run/strategist                Strategist — bearer auth,
                                        async (202 + background thread)
    POST /run/draft-cycle               Front-half revision loop — bearer auth,
                                        async (202 + background thread)
    POST /run/approval-card             n8n Approval Card workflow — bearer auth
    POST /run/approval-card-reschedule  n8n Reschedule workflow — bearer auth
    POST /slack/interactivity           Slack Scenario 6 — Slack signature auth
    POST /slack/events                  Slack Events API — edited-caption
                                        capture — Slack signature auth
    GET  /media/<row_id>/<token>        serves a row's generated media bytes —
                                        HMAC-token auth (NOT bearer; Slack
                                        fetches the image URL unauthenticated)

    /slack/events reads a JSON body (not a urlencoded form like
    /slack/interactivity) and answers Slack's one-time url_verification
    challenge synchronously; threaded replies map to a Content Queue row via
    thread_ts == slack_message_ts and shell out to the router's --edit-commit.

Auth:
    - Make endpoints (/run/*) require  Authorization: Bearer <EXECUTOR_TOKEN>.
      This includes /run/indexer, /run/strategist, and /run/draft-cycle.
    - Slack endpoint verifies X-Slack-Signature over the RAW request body
      using SLACK_SIGNING_SECRET (the standard Slack v0 scheme).

    /run/draft-cycle does N rows x multiple LLM calls and far exceeds the
    synchronous subprocess cap, so it runs in a background thread (like
    /slack/interactivity) and returns 202 immediately; the cycle writes its
    results to the Content Queue and logs its final JSON to stdout for
    journalctl. /run/strategist follows the same pattern — it makes many
    sequential LLM calls, so it runs in a background thread, returns 202, and
    posts a #system-health Slack summary in addition to logging to stdout.

Environment (loaded from .env via python-dotenv):
    EXECUTOR_TOKEN         shared secret for the Make-triggered endpoints
    SLACK_SIGNING_SECRET   for verifying Slack interactivity requests
    MEDIA_URL_SECRET       HMAC secret for the signed /media/<row_id>/<token>
                           URLs the approval card embeds
    BUSINESS_CONFIG_PATH   optional; defaults to business_config_tes_rentals.yaml

Run (production):
    .venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 tools.executor:app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, request


load_dotenv(override=False)

# Repo root is the parent of this file's directory (tools/executor.py -> repo).
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
CONFIG_PATH = os.environ.get(
    "BUSINESS_CONFIG_PATH", "business_config_tes_rentals.yaml"
)

# Allow Slack requests at most this many seconds of clock skew.
_SLACK_TIMESTAMP_TOLERANCE = 60 * 5  # 5 minutes
# Hard cap on how long a shelled-out CLI may run before we give up.
_SUBPROCESS_TIMEOUT = 120
# Generous cap for the draft-cycle, which does N rows x multiple LLM calls. It
# runs in a background thread, so a long cap does not block the request.
_DRAFT_CYCLE_TIMEOUT = 60 * 30
# Generous cap for the Strategist, which makes many sequential LLM calls. It
# runs in a background thread, so a long cap does not block the request.
_STRATEGIST_TIMEOUT = 60 * 30

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _python() -> str:
    """Return the interpreter to run the CLIs with.

    Prefers the repo venv; falls back to the current interpreter so the
    service still works if launched from an already-activated environment.
    """
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def _run_cli(
    args: list[str], timeout: int = _SUBPROCESS_TIMEOUT
) -> tuple[int, dict | str]:
    """Run a pipeline CLI module and return (exit_code, parsed_json_or_text).

    Runs from REPO_ROOT with BUSINESS_CONFIG_PATH set so the tools resolve
    their config exactly as they do when invoked by hand. `timeout` defaults to
    the synchronous cap; long-running async callers (the draft-cycle) pass a
    larger value.
    """
    env = dict(os.environ)
    env["BUSINESS_CONFIG_PATH"] = CONFIG_PATH
    try:
        proc = subprocess.run(
            [_python(), "-m", *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, {"success": False, "error": "CLI timed out"}

    stdout = (proc.stdout or "").strip()
    if stdout:
        try:
            return proc.returncode, json.loads(stdout)
        except ValueError:
            # CLI printed non-JSON (shouldn't happen, but don't crash).
            return proc.returncode, stdout
    # No stdout — surface stderr for debugging.
    return proc.returncode, {
        "success": proc.returncode == 0,
        "stderr": (proc.stderr or "").strip(),
    }


def _check_bearer() -> bool:
    """Constant-time check of the Authorization: Bearer token."""
    expected = os.environ.get("EXECUTOR_TOKEN", "")
    if not expected:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    provided = header[len("Bearer "):]
    return hmac.compare_digest(provided, expected)


def _verify_slack_signature(raw_body: bytes) -> bool:
    """Verify Slack's v0 request signature over the raw request body.

    See https://api.slack.com/authentication/verifying-requests-from-slack
    """
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        return False

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    slack_sig = request.headers.get("X-Slack-Signature", "")
    if not timestamp or not slack_sig:
        return False

    # Reject stale requests (replay protection).
    try:
        if abs(time.time() - int(timestamp)) > _SLACK_TIMESTAMP_TOLERANCE:
            return False
    except ValueError:
        return False

    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    computed = (
        "v0="
        + hmac.new(
            signing_secret.encode(), basestring, hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(computed, slack_sig)


# Image extensions the drafter writes to .tmp (e.g. _infographic.jpg,
# _generated.png, _rendered.png, _review.png) and their content types.
_MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png")
_MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _sign_media_token(row_id: str) -> str:
    """HMAC-SHA256 of row_id under MEDIA_URL_SECRET, hex, truncated to 32 chars.

    The approval-card builder computes the identical token to construct the
    public image URL. Shared secret lives in .env as MEDIA_URL_SECRET. When the
    secret is unset the route refuses to serve (see serve_media), so this never
    authorizes a request under an empty secret.
    """
    secret = os.environ.get("MEDIA_URL_SECRET", "")
    return hmac.new(secret.encode(), row_id.encode(), hashlib.sha256).hexdigest()[:32]


def _media_config_path() -> str:
    """Absolute path to the business config, resolved against the repo root.

    CONFIG_PATH may be a bare filename; gunicorn's cwd is not guaranteed to be
    the repo root, so anchor relative paths to REPO_ROOT.
    """
    p = Path(CONFIG_PATH)
    return str(p if p.is_absolute() else REPO_ROOT / p)


def _resolve_tab_name(sheet_id: str, service) -> str:
    """First sheet's title — mirrors approval_card.py::_resolve_tab_name."""
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(title))",
    ).execute()
    return meta["sheets"][0]["properties"]["title"]


def _drive_media_bytes(file_id: str) -> tuple[bytes, str]:
    """Download a Drive file's bytes into memory (no disk write).

    Returns (raw_bytes, mime_type). Raises on any Drive/HTTP error so the
    caller can fall back to the local .tmp copy.
    """
    from io import BytesIO

    from googleapiclient.http import MediaIoBaseDownload

    from tools import drive_helpers

    service = drive_helpers.get_drive_service()
    mime = (
        service.files()
        .get(fileId=file_id, fields="mimeType")
        .execute()
        .get("mimeType")
        or ""
    )
    buffer = BytesIO()
    downloader = MediaIoBaseDownload(
        buffer, service.files().get_media(fileId=file_id)
    )
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buffer.getvalue(), mime


def _local_media_file(row_id: str):
    """First .tmp image whose name starts with ``{row_id}_``, or None."""
    tmp_dir = REPO_ROOT / ".tmp"
    try:
        candidates = sorted(tmp_dir.glob(f"{row_id}_*"))
    except OSError:
        return None
    for path in candidates:
        if path.suffix.lower() in _MEDIA_EXTENSIONS and path.is_file():
            return path
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> Response:
    return jsonify({"ok": True, "service": "executor"})


@app.post("/run/indexer")
def run_indexer() -> Response:
    if not _check_bearer():
        return jsonify({"success": False, "error": "unauthorized"}), 401
    code, result = _run_cli(["agents.asset_indexer"])
    return jsonify({"exit_code": code, "result": result}), (
        200 if code == 0 else 500
    )


def _dispatch_strategist() -> None:
    """Background worker: run the Strategist planning pass to completion."""
    code, result = _run_cli(
        ["agents.strategist"], timeout=_STRATEGIST_TIMEOUT
    )
    print(
        json.dumps(
            {"strategist_exit_code": code, "strategist_result": result}
        ),
        flush=True,
    )


@app.post("/run/strategist")
def run_strategist() -> Response:
    if not _check_bearer():
        return jsonify({"success": False, "error": "unauthorized"}), 401
    # The Strategist makes many sequential LLM calls and exceeds the sync
    # cap, so run it in the background and acknowledge immediately so Make's
    # poke does not hang. Results land in the Content Queue + a #system-health
    # Slack summary + stdout (journalctl).
    threading.Thread(target=_dispatch_strategist, daemon=True).start()
    return jsonify({"accepted": True}), 202


def _dispatch_draft_cycle() -> None:
    """Background worker: run the front-half revision loop to completion."""
    code, result = _run_cli(
        ["agents.draft_cycle"], timeout=_DRAFT_CYCLE_TIMEOUT
    )
    # Log to stdout so journalctl captures the outcome (same pattern as the
    # Slack router dispatch).
    print(
        json.dumps(
            {"draft_cycle_exit_code": code, "draft_cycle_result": result}
        ),
        flush=True,
    )


@app.post("/run/draft-cycle")
def run_draft_cycle() -> Response:
    if not _check_bearer():
        return jsonify({"success": False, "error": "unauthorized"}), 401
    # The cycle does N rows x multiple LLM calls and far exceeds the sync cap,
    # so run it in the background and acknowledge immediately so Make's poke
    # does not hang. Results land in the Content Queue + stdout (journalctl).
    threading.Thread(target=_dispatch_draft_cycle, daemon=True).start()
    return jsonify({"accepted": True}), 202


@app.post("/run/approval-card")
def run_approval_card() -> Response:
    if not _check_bearer():
        return jsonify({"success": False, "error": "unauthorized"}), 401
    code, result = _run_cli(["tools.approval_card"])
    return jsonify({"exit_code": code, "result": result}), (
        200 if code == 0 else 500
    )


@app.post("/run/approval-card-reschedule")
def run_approval_card_reschedule() -> Response:
    if not _check_bearer():
        return jsonify({"success": False, "error": "unauthorized"}), 401
    code, result = _run_cli(["tools.approval_card", "--reschedule"])
    return jsonify({"exit_code": code, "result": result}), (
        200 if code == 0 else 500
    )


def _dispatch_router(payload_json: str) -> None:
    """Background worker: run the approval router for a Slack action."""
    code, result = _run_cli(
        ["tools.approval_router", "--payload", payload_json]
    )
    # Log to stdout/stderr so journalctl captures the outcome.
    print(
        json.dumps({"router_exit_code": code, "router_result": result}),
        flush=True,
    )


@app.post("/slack/interactivity")
def slack_interactivity() -> Response:
    raw_body = request.get_data()  # raw bytes — required for signature check
    if not _verify_slack_signature(raw_body):
        return jsonify({"error": "invalid signature"}), 401

    # Slack sends the interaction as a urlencoded `payload` form field.
    payload_str = request.form.get("payload")
    if not payload_str:
        return jsonify({"error": "no payload"}), 400

    # Acknowledge within Slack's 3s window, then process in the background.
    threading.Thread(
        target=_dispatch_router, args=(payload_str,), daemon=True
    ).start()
    return ("", 200)


def _dispatch_event(event: dict) -> None:
    """Background worker: capture an edited-caption reply and commit it.

    Maps a threaded Slack message back to its Content Queue row via
    thread_ts == slack_message_ts (persisted in Piece 1) and, when that row is
    still at status ``drafted`` (the edit-caption state set by the router),
    shells out to ``tools.approval_router --edit-commit`` to write the revised
    caption and publish. NEVER raises — this runs in a daemon thread, so every
    failure path logs one stderr line and returns.
    """
    # --- Guards: only genuine threaded human replies map to a card. ---------
    if event.get("type") != "message":
        print(f"slack/events ignore: type={event.get('type')}", file=sys.stderr)
        return
    if event.get("bot_id") or event.get("subtype"):
        # Bot/own/edited messages (bot_message, message_changed, ...).
        print(
            "slack/events ignore: bot_id/subtype "
            f"({event.get('bot_id')}/{event.get('subtype')})",
            file=sys.stderr,
        )
        return
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        print("slack/events ignore: not a threaded reply", file=sys.stderr)
        return
    if thread_ts == event.get("ts"):
        # The thread parent itself, not a reply to it.
        print("slack/events ignore: parent message, not a reply", file=sys.stderr)
        return
    caption_text = event.get("text")
    if caption_text:
        from tools.slack_helpers import unwrap_slack_mrkdwn
        caption_text = unwrap_slack_mrkdwn(caption_text)
    if not caption_text or not caption_text.strip():
        print("slack/events ignore: empty text", file=sys.stderr)
        return

    # --- Row lookup: mirror serve_media's SANCTIONED direct-read exception. --
    # This direct sheets_helpers/load_config import is permitted ONLY to READ
    # the Content Queue (same documented exception serve_media cites). The
    # publish itself MUST shell out to the router via _run_cli below — the
    # executor never imports the publish path.
    try:
        from tools import sheets_helpers
        from tools.config_loader import load_config

        config = load_config(_media_config_path())
        sheet_id = config.get("drive.content_queue_sheet_id")
        service = sheets_helpers.get_sheets_service()
        tab_name = _resolve_tab_name(sheet_id, service)
        matches = sheets_helpers.find_rows_by_column_value(
            sheet_id, tab_name, "slack_message_ts", thread_ts, service=service,
        )
    except Exception as exc:  # noqa: BLE001 — background thread, never raise
        print(
            f"slack/events lookup failed for thread_ts={thread_ts}: {exc}",
            file=sys.stderr,
        )
        return

    if not matches:
        print(f"slack/events no row for thread_ts={thread_ts}", file=sys.stderr)
        return
    _row_number, row = matches[0]

    # Load-bearing guard: reject-reason replies land in threads too, but reject
    # sets status `rejected`. Only genuine edit-caption rows sit at `drafted`.
    status = (row.get("status") or "").strip()
    if status != "drafted":
        print(
            f"slack/events ignore: status={status!r} (not 'drafted') "
            f"for thread_ts={thread_ts}",
            file=sys.stderr,
        )
        return

    # --- Dispatch: keep the publish in ONE place — shell out to the router. --
    row_id = row.get("row_id", "")
    code, result = _run_cli(
        [
            "tools.approval_router",
            "--edit-commit",
            "--row-id",
            row_id,
            "--caption-text",
            caption_text,
        ]
    )
    print(
        json.dumps(
            {"event_edit_commit_exit": code, "result": result, "row_id": row_id}
        ),
        flush=True,
    )


@app.post("/slack/events")
def slack_events() -> Response:
    raw_body = request.get_data()  # raw bytes — required for signature check
    if not _verify_slack_signature(raw_body):
        return jsonify({"error": "invalid signature"}), 401

    try:
        body = json.loads(raw_body or b"{}")
    except ValueError:
        return jsonify({"error": "bad json"}), 400

    # URL verification handshake — respond synchronously with the challenge.
    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge", "")}), 200

    if body.get("type") == "event_callback":
        event = body.get("event") or {}
        # Spawn background worker; ack immediately (Slack 3s window + retries).
        threading.Thread(
            target=_dispatch_event, args=(event,), daemon=True
        ).start()

    # Always ack 200 on a signed event_callback even if we ignore the event —
    # Slack retries on non-2xx, which would cause duplicate processing.
    return ("", 200)


@app.get("/media/<row_id>/<token>")
def serve_media(row_id: str, token: str) -> Response:
    # DELIBERATE EXCEPTION to this service's "dumb runner, shells out via
    # _run_cli" principle: serving a static media file is a delivery concern,
    # not pipeline logic, and it must return raw image bytes rather than JSON —
    # so it imports sheets_helpers/drive_helpers directly instead of shelling
    # out. Slack fetches image URLs server-side and UNAUTHENTICATED (no headers
    # we control), so this route MUST NOT use _check_bearer(); it is guarded by
    # the HMAC token in the path instead.
    secret = os.environ.get("MEDIA_URL_SECRET", "")
    if not secret:
        # No secret configured — every token is forgeable, so serve nothing.
        abort(403)
    expected = _sign_media_token(row_id)
    if not hmac.compare_digest(token, expected):
        abort(403)

    # Resolve the row from the Content Queue. Any failure here is a clean 404 —
    # never a 500 stacktrace to the caller.
    try:
        from tools import sheets_helpers
        from tools.config_loader import load_config

        config = load_config(_media_config_path())
        sheet_id = config.get("drive.content_queue_sheet_id")
        service = sheets_helpers.get_sheets_service()
        tab_name = _resolve_tab_name(sheet_id, service)
        matches = sheets_helpers.find_rows_by_column_value(
            sheet_id, tab_name, "row_id", row_id, service=service,
        )
    except Exception:
        abort(404)

    if not matches:
        abort(404)
    _row_number, row = matches[0]

    # media_url holds a Drive file ID (not a URL), written by the drafter.
    file_id = (row.get("media_url") or "").strip()

    body: bytes | None = None
    mime: str | None = None

    # Drive first.
    if file_id:
        try:
            body, mime = _drive_media_bytes(file_id)
        except Exception:
            body = None

    # .tmp fallback — used when media_url is empty or the Drive fetch failed.
    if body is None:
        local = _local_media_file(row_id)
        if local is not None:
            try:
                body = local.read_bytes()
                mime = _MIME_BY_EXT.get(local.suffix.lower())
            except OSError:
                body = None

    if body is None:
        abort(404)

    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    return Response(body, mimetype=mime)


if __name__ == "__main__":
    # Dev only. Production uses gunicorn (see module docstring / systemd unit).
    app.run(host="127.0.0.1", port=8000)
