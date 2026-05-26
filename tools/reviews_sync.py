"""Reviews Sync tool — pull GBP reviews and sync to a Google Sheet.

Fetches all reviews from the Google Business Profile Reviews API (v4),
processes each into a structured row (first name, star integer, excerpts,
usable flag), and upserts to the Reviews Sheet. Preserves the
`last_used_date` and `times_used` columns that downstream agents
(Strategist / Drafter) write back.

Posts a one-line summary to #system-health and emits a structured JSON
result to stdout. No LLM calls.

Usage:
    python -m tools.reviews_sync --create-sheet   # one-time setup
    python -m tools.reviews_sync                  # full sync
    python -m tools.reviews_sync --dry-run        # fetch + process, no writes
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

from tools import drive_helpers, sheets_helpers, slack_helpers
from tools.config_loader import Config, load_config


# --- Constants ---

GBP_SCOPES = ["https://www.googleapis.com/auth/business.manage"]

REVIEWS_TAB = "Reviews"

REVIEWS_HEADERS: list[str] = [
    "review_id",
    "reviewer_first_name",
    "star_rating",
    "review_text",
    "review_length",
    "word_count",
    "review_date",
    "owner_reply",
    "usable_for_social",
    "excerpt_short",
    "excerpt_long",
    "last_used_date",
    "times_used",
]

PRESERVED_COLUMNS: set[str] = {"last_used_date", "times_used"}

STAR_MAP: dict[str, int] = {
    "FIVE": 5,
    "FOUR": 4,
    "THREE": 3,
    "TWO": 2,
    "ONE": 1,
}

EXCERPT_SHORT_LIMIT = 30
EXCERPT_LONG_LIMIT = 80

USABLE_MIN_WORDS = 5

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 1.0

_TRAILING_PUNCTUATION = " \t\n.,;:!?-—–"


# --- Excerpt + processing helpers (pure functions) ---


def make_excerpt(text: str, limit: int) -> str:
    """Truncate `text` at a word boundary within `limit` chars, with "…".

    - Returns "" for empty/whitespace input.
    - Returns the full text (no ellipsis) when it already fits within `limit`.
    - Otherwise picks the longest word-boundary prefix that fits, strips
      trailing punctuation/whitespace, and appends a single "…".
    """
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text

    words = text.split()
    out = ""
    for word in words:
        candidate = f"{out} {word}".strip() if out else word
        if len(candidate) > limit:
            break
        out = candidate

    if not out:
        # First word alone exceeds the limit — hard truncate.
        out = text[:limit]

    out = out.rstrip(_TRAILING_PUNCTUATION)
    return f"{out}…"


def extract_first_name(display_name: str | None) -> str:
    """Return the first whitespace-separated token of `display_name`."""
    if not display_name:
        return ""
    parts = display_name.split()
    return parts[0] if parts else ""


def process_review(review: dict) -> dict:
    """Convert a raw GBP API review dict into a Reviews-Sheet row dict."""
    comment = (review.get("comment") or "").strip()
    words = comment.split() if comment else []
    word_count = len(words)

    reviewer = review.get("reviewer") or {}
    first_name = extract_first_name(reviewer.get("displayName"))

    star_int = STAR_MAP.get(review.get("starRating", ""), 0)

    reply_obj = review.get("reviewReply") or {}
    reply = reply_obj.get("comment") or ""

    is_usable = star_int == 5 and len(comment) > 0 and word_count >= USABLE_MIN_WORDS

    return {
        "review_id": review.get("name", "") or "",
        "reviewer_first_name": first_name,
        "star_rating": str(star_int),
        "review_text": comment,
        "review_length": str(len(comment)),
        "word_count": str(word_count),
        "review_date": review.get("createTime", "") or "",
        "owner_reply": reply,
        "usable_for_social": "TRUE" if is_usable else "FALSE",
        "excerpt_short": make_excerpt(comment, EXCERPT_SHORT_LIMIT),
        "excerpt_long": make_excerpt(comment, EXCERPT_LONG_LIMIT),
        "last_used_date": "",
        "times_used": "0",
    }


# --- GBP API access ---


def _load_gbp_credentials(token_path: str) -> Credentials:
    token = Path(token_path)
    if not token.is_file():
        raise RuntimeError(
            f"GBP token not found at {token_path}. "
            "Run: python tools/google_auth.py --gbp"
        )

    try:
        creds = Credentials.from_authorized_user_file(str(token), GBP_SCOPES)
    except Exception as exc:
        raise RuntimeError(
            f"GBP token at {token_path} could not be loaded ({exc}). "
            "Run: python tools/google_auth.py --gbp"
        ) from exc

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise RuntimeError(
                    f"GBP token refresh failed ({exc}). "
                    "Run: python tools/google_auth.py --gbp"
                ) from exc
            token.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(
                "GBP token invalid. Run: python tools/google_auth.py --gbp"
            )

    return creds


def _gbp_get_with_retry(session: AuthorizedSession, url: str, params: dict) -> dict:
    """GET request with retry on transient HTTP errors (429/5xx)."""
    delay = _BASE_DELAY_SECONDS
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, params=params, timeout=30)
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                print(
                    f"[reviews_sync] transport error on attempt {attempt} "
                    f"({type(exc).__name__}: {exc}); retrying in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise

        if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
            print(
                f"[reviews_sync] transient HTTP {response.status_code} on "
                f"attempt {attempt}; retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
            delay *= 2
            continue

        if not response.ok:
            raise RuntimeError(
                f"GBP Reviews API returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        return response.json()

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("GBP retry loop exited without result or exception")


def fetch_all_reviews(
    endpoint: str,
    page_size: int,
    creds: Credentials,
) -> list[dict]:
    """Page through the GBP Reviews API and return all review dicts."""
    session = AuthorizedSession(creds)
    url = f"https://{endpoint}"
    reviews: list[dict] = []
    page_token: str | None = None

    while True:
        params: dict[str, Any] = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        payload = _gbp_get_with_retry(session, url, params)
        reviews.extend(payload.get("reviews", []) or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return reviews


# --- Sheet creation (--create-sheet) ---


def create_reviews_sheet(config: Config) -> str:
    """Create the Reviews spreadsheet, move it into the project root folder,
    write the header row, and return the new spreadsheet ID.
    """
    root_folder_id = config.get("drive.root_folder_id")
    if not root_folder_id:
        raise RuntimeError("drive.root_folder_id is empty in config")

    sheets_service = sheets_helpers.get_sheets_service()
    drive_service = drive_helpers.get_drive_service()

    spreadsheet = sheets_service.spreadsheets().create(
        body={
            "properties": {"title": "Reviews — TES Rentals"},
            "sheets": [{"properties": {"title": REVIEWS_TAB}}],
        }
    ).execute()
    new_sheet_id = spreadsheet["spreadsheetId"]

    file_meta = drive_service.files().get(
        fileId=new_sheet_id, fields="parents"
    ).execute()
    old_parents = ",".join(file_meta.get("parents", []) or [])
    drive_service.files().update(
        fileId=new_sheet_id,
        addParents=root_folder_id,
        removeParents=old_parents,
        fields="id, parents",
    ).execute()

    quoted_tab = sheets_helpers._quote_tab(REVIEWS_TAB)
    sheets_service.spreadsheets().values().update(
        spreadsheetId=new_sheet_id,
        range=f"{quoted_tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [REVIEWS_HEADERS]},
    ).execute()

    sheets_helpers._invalidate_header_cache(new_sheet_id, REVIEWS_TAB)
    return new_sheet_id


# --- Slack summary ---


def _format_slack_summary(
    total_fetched: int,
    five_star_count: int,
    usable_count: int,
    new_rows: int,
    updated_rows: int,
    preserved_rows: int,
) -> str:
    return "\n".join([
        "Reviews Sync — Run Complete",
        f"• Total reviews fetched: {total_fetched}",
        f"• Five-star reviews: {five_star_count}",
        f"• Usable for social (5★, has text, ≥{USABLE_MIN_WORDS} words): {usable_count}",
        f"• New reviews added to sheet: {new_rows}",
        f"• Existing reviews updated: {updated_rows}",
        f"• Reviews in sheet not in API (kept): {preserved_rows}",
    ])


# --- Sync orchestration ---


def run(dry_run: bool = False) -> dict:
    """Fetch GBP reviews, upsert into the Reviews Sheet, post Slack summary."""
    config = load_config()

    reviews_sheet_id = config.get("catalog.reviews_sheet_id", "")
    if not reviews_sheet_id:
        raise RuntimeError(
            "catalog.reviews_sheet_id is empty in config. "
            "Run: python -m tools.reviews_sync --create-sheet first."
        )

    endpoint = config.get("metrics.sources.gbp_reviews.endpoint")
    page_size = config.get("metrics.sources.gbp_reviews.page_size", 50)
    gbp_token_path = config.get("metrics.sources.gbp_reviews.gbp_token_path")
    health_channel = config.get("health.slack_channel")

    if not endpoint:
        raise RuntimeError("metrics.sources.gbp_reviews.endpoint is empty in config")
    if not gbp_token_path:
        raise RuntimeError(
            "metrics.sources.gbp_reviews.gbp_token_path is empty in config"
        )

    creds = _load_gbp_credentials(gbp_token_path)
    raw_reviews = fetch_all_reviews(endpoint, page_size, creds)
    processed = [process_review(r) for r in raw_reviews]

    five_star_count = sum(1 for r in processed if r["star_rating"] == "5")
    usable_count = sum(1 for r in processed if r["usable_for_social"] == "TRUE")

    sheets_service = sheets_helpers.get_sheets_service()
    existing_rows = sheets_helpers.read_all_rows(
        reviews_sheet_id, REVIEWS_TAB, service=sheets_service
    )

    existing_by_id: dict[str, tuple[int, dict]] = {}
    for idx, row in enumerate(existing_rows):
        rid = row.get("review_id", "")
        if rid:
            existing_by_id[rid] = (idx + 2, row)

    fetched_ids: set[str] = {p["review_id"] for p in processed if p["review_id"]}
    preserved_rows = sum(1 for rid in existing_by_id if rid not in fetched_ids)

    updates: list[tuple[int, dict]] = []
    new_rows_to_append: list[dict] = []

    for row in processed:
        rid = row["review_id"]
        if not rid:
            continue
        if rid in existing_by_id:
            row_num, _existing = existing_by_id[rid]
            col_updates = {
                k: v for k, v in row.items() if k not in PRESERVED_COLUMNS
            }
            updates.append((row_num, col_updates))
        else:
            new_rows_to_append.append(row)

    if not dry_run:
        if updates:
            sheets_helpers.batch_update_cells(
                reviews_sheet_id, REVIEWS_TAB, updates, service=sheets_service
            )
        for new_row in new_rows_to_append:
            sheets_helpers.append_row(
                reviews_sheet_id, REVIEWS_TAB, new_row, service=sheets_service
            )

        summary = _format_slack_summary(
            total_fetched=len(processed),
            five_star_count=five_star_count,
            usable_count=usable_count,
            new_rows=len(new_rows_to_append),
            updated_rows=len(updates),
            preserved_rows=preserved_rows,
        )
        try:
            slack_helpers.post_message(health_channel, summary)
        except Exception as exc:
            print(
                f"[reviews_sync] WARN: Slack post failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    else:
        print("[reviews_sync] DRY RUN — no writes performed", file=sys.stderr)
        print(
            f"[reviews_sync] would update {len(updates)} existing rows, "
            f"append {len(new_rows_to_append)} new rows, "
            f"preserve {preserved_rows} rows not in API",
            file=sys.stderr,
        )

    return {
        "status": "success",
        "total_fetched": len(processed),
        "five_star_count": five_star_count,
        "usable_for_social_count": usable_count,
        "new_rows_added": len(new_rows_to_append),
        "existing_rows_updated": len(updates),
        "preserved_rows": preserved_rows,
        "dry_run": dry_run,
    }


# --- CLI ---


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GBP Reviews Sync tool")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--create-sheet",
        action="store_true",
        help="One-time: create the Reviews Sheet, then exit.",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and process reviews but do not write to the sheet or Slack.",
    )
    args = parser.parse_args(argv)

    if args.create_sheet:
        config = load_config()
        new_id = create_reviews_sheet(config)
        print(f"Reviews Sheet created: {new_id}")
        print("Add to business_config_tes_rentals.yaml:")
        print("  catalog:")
        print(f'    reviews_sheet_id: "{new_id}"')
        return 0

    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
