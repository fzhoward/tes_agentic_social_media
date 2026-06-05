#!/usr/bin/env python3
"""Exploratory harness: does Slack render an inline image fetched from the
executor tunnel, and in what message form?

This is a THROWAWAY probe (mirrors the Nano Banana image-gen explorer). It does
NOT touch the Content Queue, Drive, or any production code path. Its only job is
to answer empirically, before we write production prompts:

  1. Does the Cloudflare tunnel forward an arbitrary new path (/probe-media/...)
     to this Flask app? (If the tunnel only proxies known routes, Option C dies
     here and we fall back to file upload.)
  2. Does Slack's Block Kit `image` block render a JPEG fetched server-side from
     https://executor.tessys.org/...? (content-type, size, caching)
  3. Does the HMAC-signed path scheme work end-to-end through the tunnel?
  4. As a control: does files_upload_v2 of the same local file render? (This is
     the Option-A fallback — we post it so you can compare side by side.)

It serves the ALREADY-VALIDATED local image from the S42 run
(.tmp/STR-20260608-FB-01_infographic.jpg) — no Drive, no non-dry-run needed.

------------------------------------------------------------------------------
HOW TO RUN (two terminals, repo root, .venv active):

  # Terminal 1 — start the probe server bound to the port the tunnel forwards.
  #   --port must match whatever executor.tessys.org forwards to locally.
  #   The real executor runs on 127.0.0.1:8000 via gunicorn; if it's running,
  #   pick a DIFFERENT free port (e.g. 8011) and point the tunnel at it for the
  #   probe, OR stop the executor service for the duration of the probe.
  source .venv/bin/activate
  python tools/slack_image_probe.py serve \
      --image .tmp/STR-20260608-FB-01_infographic.jpg \
      --port 8011

  # Terminal 2 — post the candidate messages to #approvals.
  source .venv/bin/activate
  python tools/slack_image_probe.py post \
      --base-url https://executor.tessys.org \
      --channel '#approvals' \
      --image .tmp/STR-20260608-FB-01_infographic.jpg

Then LOOK at #approvals and report which of the posted candidates show the image
inline. That tells us which mechanism to wire into build_approval_blocks.

NOTE on the tunnel/port: the single biggest unknown this probe resolves is
whether your tunnel forwards an arbitrary path to the Flask app. If
`curl https://executor.tessys.org/probe-media/ping` (printed by `serve`) does
NOT return {"ok": true} from your laptop, the tunnel is route-scoped and we use
Option A (file upload) instead — tell me and I'll switch approaches.

Requires the same env the real tools use (.env): SLACK_BOT_TOKEN.
The HMAC secret here is a throwaway constant — production will use a real
secret from .env.
------------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import mimetypes
import os
import sys
from pathlib import Path

# Throwaway secret for the probe only. Production uses a real .env secret.
_PROBE_SECRET = "probe-only-not-a-real-secret"
_PROBE_ROW_ID = "STR-20260608-FB-01"


def _sign(row_id: str) -> str:
    """HMAC-SHA256 of row_id, hex, truncated — the C-auth-1 signed-path token."""
    return hmac.new(
        _PROBE_SECRET.encode(), row_id.encode(), hashlib.sha256
    ).hexdigest()[:32]


# ---------------------------------------------------------------------------
# serve: minimal Flask app that serves the one image at a signed path
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    from flask import Flask, Response, abort, jsonify, send_file

    image_path = Path(args.image).resolve()
    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        return 1

    app = Flask("slack_image_probe")

    @app.get("/probe-media/ping")
    def ping() -> Response:
        # Used to confirm the tunnel forwards arbitrary paths to this app.
        return jsonify({"ok": True, "probe": "alive"})

    @app.get("/probe-media/<row_id>/<token>")
    def media(row_id: str, token: str) -> Response:
        # Slack fetches this UNAUTHENTICATED — no bearer. Guard with HMAC.
        expected = _sign(row_id)
        if not hmac.compare_digest(token, expected):
            abort(403)
        if row_id != _PROBE_ROW_ID:
            abort(404)
        mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        # conditional=False / no etag games — keep it dumb for the probe.
        return send_file(str(image_path), mimetype=mime)

    token = _sign(_PROBE_ROW_ID)
    print("=" * 70)
    print(f"Serving {image_path}")
    print(f"  size: {image_path.stat().st_size} bytes")
    print(f"  mime: {mimetypes.guess_type(str(image_path))[0]}")
    print(f"Local probe URL:")
    print(f"  http://127.0.0.1:{args.port}/probe-media/{_PROBE_ROW_ID}/{token}")
    print(f"Public URL (if tunnel forwards this port):")
    print(f"  https://executor.tessys.org/probe-media/{_PROBE_ROW_ID}/{token}")
    print()
    print("FIRST verify the tunnel forwards arbitrary paths — from your laptop:")
    print(f"  curl https://executor.tessys.org/probe-media/ping")
    print("  -> expect: {\"ok\": true, \"probe\": \"alive\"}")
    print("=" * 70)
    app.run(host="127.0.0.1", port=args.port)
    return 0


# ---------------------------------------------------------------------------
# post: build candidate Slack messages and post them to #approvals
# ---------------------------------------------------------------------------

def cmd_post(args: argparse.Namespace) -> int:
    # Reuse the production Slack client so auth/scopes match exactly.
    try:
        from tools import slack_helpers
    except Exception as exc:  # pragma: no cover - run-context guard
        print(
            f"ERROR: could not import tools.slack_helpers "
            f"({type(exc).__name__}: {exc}). Run from repo root with .venv "
            f"active.",
            file=sys.stderr,
        )
        return 1

    token = _sign(_PROBE_ROW_ID)
    public_url = f"{args.base_url.rstrip('/')}/probe-media/{_PROBE_ROW_ID}/{token}"
    print(f"Candidate public image URL:\n  {public_url}\n")

    results: list[tuple[str, str]] = []

    # --- Candidate 1: Block Kit `image` block (the Option C target shape) ---
    # This is what we'd put in build_approval_blocks above the actions block.
    blocks_image = [
        {
            "type": "header",
            "text": {"type": "plain_text",
                     "text": "PROBE 1 — image block (Option C)", "emoji": True},
        },
        {
            "type": "image",
            "image_url": public_url,
            "alt_text": "infographic preview",
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "If you can see the image above this line, "
                             "Option C (tunnel + image block) works."},
        },
    ]
    try:
        r = slack_helpers.post_message(
            args.channel, "PROBE 1 image block", blocks=blocks_image
        )
        results.append(("PROBE 1 image block", "posted ts=" + str(r.get("ts"))))
    except Exception as exc:
        results.append(("PROBE 1 image block", f"FAILED: {exc}"))

    # --- Candidate 2: image block WITH a title (some clients differ) ---
    blocks_image_titled = [
        {
            "type": "header",
            "text": {"type": "plain_text",
                     "text": "PROBE 2 — image block + title", "emoji": True},
        },
        {
            "type": "image",
            "title": {"type": "plain_text", "text": "Infographic preview"},
            "image_url": public_url,
            "alt_text": "infographic preview",
        },
    ]
    try:
        r = slack_helpers.post_message(
            args.channel, "PROBE 2 image block titled", blocks=blocks_image_titled
        )
        results.append(("PROBE 2 image block + title",
                        "posted ts=" + str(r.get("ts"))))
    except Exception as exc:
        results.append(("PROBE 2 image block + title", f"FAILED: {exc}"))

    # --- Candidate 3: control — files_upload_v2 of the SAME local file ---
    # This is the Option A fallback. If C fails to render but this does, we
    # know the tunnel/URL is the problem, not Slack or the image itself.
    try:
        r = slack_helpers.upload_file(
            args.channel,
            args.image,
            title="PROBE 3 — files_upload_v2 control (Option A)",
            initial_comment="PROBE 3 — uploaded file (Option A fallback)",
        )
        results.append(("PROBE 3 files_upload_v2", f"posted ok={r.get('ok')}"))
    except Exception as exc:
        results.append(("PROBE 3 files_upload_v2", f"FAILED: {exc}"))

    print("\n" + "=" * 70)
    print("POSTED CANDIDATES — now LOOK at the channel:")
    for name, status in results:
        print(f"  {name}: {status}")
    print("=" * 70)
    print(
        "\nReport back which PROBE numbers render the image INLINE.\n"
        " - PROBE 1/2 render  -> Option C confirmed (tunnel + image block).\n"
        " - PROBE 1/2 blank, PROBE 3 renders -> tunnel/URL issue; image block\n"
        "   can't fetch the URL. We either fix the URL or fall back to A.\n"
        " - Nothing renders   -> Slack scope/channel problem; check files:write\n"
        "   and that the bot is in the channel.\n"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the probe image server")
    p_serve.add_argument("--image", required=True,
                         help="local image file to serve")
    p_serve.add_argument("--port", type=int, default=8011,
                         help="port to bind (must match tunnel forward)")
    p_serve.set_defaults(func=cmd_serve)

    p_post = sub.add_parser("post", help="post candidate messages to Slack")
    p_post.add_argument("--base-url", required=True,
                        help="public tunnel base, e.g. https://executor.tessys.org")
    p_post.add_argument("--channel", default="#approvals",
                        help="Slack channel name or ID")
    p_post.add_argument("--image", required=True,
                        help="local image file (for the upload control)")
    p_post.set_defaults(func=cmd_post)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
