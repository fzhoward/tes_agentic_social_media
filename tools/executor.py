"""HTTP executor for the agentic social media pipeline.

A thin Flask service that lets the orchestration layer (Make.com) and Slack
invoke the approval-pipeline CLIs on this machine. It does NOT contain pipeline
logic — it shells out to the existing tools via the repo's venv interpreter and
returns their JSON. Keeping it a dumb runner means a failure in one handler
cannot take down the listener, and the execution layer stays separate from
agent logic (per CLAUDE.md).

Endpoints:
    GET  /healthz                       liveness probe (no auth)
    POST /run/indexer                   Asset Indexer — bearer auth
    POST /run/strategist                Strategist — bearer auth
    POST /run/draft-cycle               Front-half revision loop — bearer auth,
                                        async (202 + background thread)
    POST /run/approval-card             Make Scenario 5 — bearer auth
    POST /run/approval-card-reschedule  Make Scenario 7 — bearer auth
    POST /slack/interactivity           Slack Scenario 6 — Slack signature auth

Auth:
    - Make endpoints (/run/*) require  Authorization: Bearer <EXECUTOR_TOKEN>.
      This includes /run/indexer, /run/strategist, and /run/draft-cycle.
    - Slack endpoint verifies X-Slack-Signature over the RAW request body
      using SLACK_SIGNING_SECRET (the standard Slack v0 scheme).

    /run/draft-cycle does N rows x multiple LLM calls and far exceeds the
    synchronous subprocess cap, so it runs in a background thread (like
    /slack/interactivity) and returns 202 immediately; the cycle writes its
    results to the Content Queue and logs its final JSON to stdout for
    journalctl.

Environment (loaded from .env via python-dotenv):
    EXECUTOR_TOKEN         shared secret for the Make-triggered endpoints
    SLACK_SIGNING_SECRET   for verifying Slack interactivity requests
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
from flask import Flask, Response, jsonify, request


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


@app.post("/run/strategist")
def run_strategist() -> Response:
    if not _check_bearer():
        return jsonify({"success": False, "error": "unauthorized"}), 401
    code, result = _run_cli(["agents.strategist"])
    return jsonify({"exit_code": code, "result": result}), (
        200 if code == 0 else 500
    )


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


if __name__ == "__main__":
    # Dev only. Production uses gunicorn (see module docstring / systemd unit).
    app.run(host="127.0.0.1", port=8000)
