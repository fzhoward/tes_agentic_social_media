"""One-time OAuth setup script for Google Drive + Sheets (and optionally GBP).

Run manually:
    python tools/google_auth.py           # Drive + Sheets token
    python tools/google_auth.py --gbp     # GBP token

Behavior:
- Reads credentials path and token path from .env.
- If the token file is present and valid, exits with a notice.
- Otherwise launches the OAuth installed-app flow and saves the token.

This is a build-time utility; agents never import it. They consume the
saved token via tools/drive_helpers.get_drive_service().
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


DRIVE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
GBP_SCOPES = [
    "https://www.googleapis.com/auth/business.manage",
]


def _load_existing_credentials(token_path: Path, scopes: list[str]) -> Credentials | None:
    if not token_path.is_file():
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_path), scopes)
    except Exception:
        return None


def _save_credentials(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")


def run_auth(credentials_path: Path, token_path: Path, scopes: list[str], label: str) -> int:
    if not credentials_path.is_file():
        print(
            f"ERROR: OAuth client credentials file not found: {credentials_path}",
            file=sys.stderr,
        )
        return 1

    creds = _load_existing_credentials(token_path, scopes)

    if creds and creds.valid:
        print(f"{label} token already valid: {token_path}")
        return 0

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds, token_path)
            print(f"{label} token refreshed: {token_path}")
            return 0
        except Exception as exc:
            print(f"Refresh failed ({exc}); running full OAuth flow...")

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
    creds = flow.run_local_server(port=0)
    _save_credentials(creds, token_path)
    print(f"{label} token saved: {token_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Google OAuth tokens.")
    parser.add_argument(
        "--gbp",
        action="store_true",
        help="Authenticate for GBP APIs instead of Drive + Sheets.",
    )
    args = parser.parse_args(argv)

    load_dotenv(override=False)

    if args.gbp:
        credentials_path = os.environ.get("GBP_CREDENTIALS_PATH")
        token_path = os.environ.get("GBP_TOKEN_PATH")
        scopes = GBP_SCOPES
        label = "GBP"
        cred_var, token_var = "GBP_CREDENTIALS_PATH", "GBP_TOKEN_PATH"
    else:
        credentials_path = os.environ.get("GOOGLE_CREDENTIALS_PATH")
        token_path = os.environ.get("GOOGLE_TOKEN_PATH")
        scopes = DRIVE_SHEETS_SCOPES
        label = "Drive + Sheets"
        cred_var, token_var = "GOOGLE_CREDENTIALS_PATH", "GOOGLE_TOKEN_PATH"

    if not credentials_path:
        print(f"ERROR: {cred_var} is not set in .env", file=sys.stderr)
        return 1
    if not token_path:
        print(f"ERROR: {token_var} is not set in .env", file=sys.stderr)
        return 1

    return run_auth(Path(credentials_path), Path(token_path), scopes, label)


if __name__ == "__main__":
    sys.exit(main())
