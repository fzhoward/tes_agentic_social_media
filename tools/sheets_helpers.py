"""Google Sheets utilities for reading and writing tabbed sheet data.

Authentication mirrors drive_helpers.get_drive_service(): reads the OAuth
token saved by tools/google_auth.py and refreshes it transparently.

All public helpers accept an optional `service` parameter so tests can
inject a stub. When omitted, each helper builds its own service via
get_sheets_service().

Rows are exposed to callers as dicts keyed by header-row values. Row
addressing is 1-indexed to match the Sheets UI (row 1 = headers, row 2 =
first data row).

Transient API errors (HTTP 429 / 500 / 503) are retried up to 3 times
with exponential backoff (1s, 2s, 4s). Persistent failures surface as
exceptions.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

AUTH_ERROR_MESSAGE = (
    "Sheets token not found or expired. "
    "Run 'python tools/google_auth.py' to authenticate."
)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 1.0

_HEADER_CACHE: dict[tuple[str, str], list[str]] = {}


def get_sheets_service() -> Any:
    credentials_path = os.environ.get("GOOGLE_CREDENTIALS_PATH")  # noqa: F841 — parity with drive_helpers
    token_path = os.environ.get("GOOGLE_TOKEN_PATH")

    if not token_path or not Path(token_path).is_file():
        raise RuntimeError(AUTH_ERROR_MESSAGE)

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception as exc:
        raise RuntimeError(AUTH_ERROR_MESSAGE) from exc

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise RuntimeError(AUTH_ERROR_MESSAGE) from exc
            Path(token_path).write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(AUTH_ERROR_MESSAGE)

    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _http_status(exc: HttpError) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status
    resp = getattr(exc, "resp", None)
    if resp is not None:
        return getattr(resp, "status", None)
    return None


def _execute_with_retry(request: Any) -> Any:
    """Call request.execute() with retry for transient failures."""
    delay = _BASE_DELAY_SECONDS
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status = _http_status(exc)
            if status in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                print(
                    f"[sheets_helpers] transient HTTP {status} on attempt {attempt}; "
                    f"retrying in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                delay *= 2
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop exited without result or exception")


def _col_letter(index: int) -> str:
    """Convert a 0-based column index to A1 column letters (0->A, 26->AA)."""
    if index < 0:
        raise ValueError(f"column index must be >= 0, got {index}")
    letters = ""
    n = index
    while True:
        n, remainder = divmod(n, 26)
        letters = chr(ord("A") + remainder) + letters
        if n == 0:
            break
        n -= 1
    return letters


def _quote_tab(tab_name: str) -> str:
    """Quote a sheet/tab name for A1 notation (always safe to single-quote)."""
    return "'" + tab_name.replace("'", "''") + "'"


def _get_headers(
    spreadsheet_id: str,
    tab_name: str,
    service: Any = None,
) -> list[str]:
    cache_key = (spreadsheet_id, tab_name)
    if cache_key in _HEADER_CACHE:
        return _HEADER_CACHE[cache_key]

    if service is None:
        service = get_sheets_service()

    range_a1 = f"{_quote_tab(tab_name)}!1:1"
    response = _execute_with_retry(
        service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            majorDimension="ROWS",
        )
    )
    values = response.get("values", [])
    headers = [str(cell) for cell in values[0]] if values else []
    _HEADER_CACHE[cache_key] = headers
    return headers


def _invalidate_header_cache(
    spreadsheet_id: str | None = None,
    tab_name: str | None = None,
) -> None:
    """Clear cached headers. With no args, clears everything."""
    if spreadsheet_id is None and tab_name is None:
        _HEADER_CACHE.clear()
        return
    if spreadsheet_id is None or tab_name is None:
        raise ValueError("must pass both spreadsheet_id and tab_name, or neither")
    _HEADER_CACHE.pop((spreadsheet_id, tab_name), None)


def _read_raw_values(
    spreadsheet_id: str,
    tab_name: str,
    service: Any,
) -> list[list[str]]:
    range_a1 = _quote_tab(tab_name)
    response = _execute_with_retry(
        service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            majorDimension="ROWS",
        )
    )
    return response.get("values", [])


def read_all_rows(
    spreadsheet_id: str,
    tab_name: str,
    service: Any = None,
) -> list[dict]:
    if service is None:
        service = get_sheets_service()

    values = _read_raw_values(spreadsheet_id, tab_name, service)
    if not values:
        return []

    headers = [str(cell) for cell in values[0]]
    _HEADER_CACHE[(spreadsheet_id, tab_name)] = headers

    rows: list[dict] = []
    for raw_row in values[1:]:
        padded = list(raw_row) + [""] * (len(headers) - len(raw_row))
        if not any(str(cell).strip() for cell in padded):
            continue
        row = {headers[i]: ("" if padded[i] is None else str(padded[i])) for i in range(len(headers))}
        rows.append(row)
    return rows


def read_rows_filtered(
    spreadsheet_id: str,
    tab_name: str,
    column_name: str,
    value: Any,
    service: Any = None,
) -> list[dict]:
    rows = read_all_rows(spreadsheet_id, tab_name, service=service)
    target = str(value)
    return [r for r in rows if r.get(column_name, "") == target]


def find_rows_by_column_value(
    spreadsheet_id: str,
    tab_name: str,
    column_name: str,
    value: Any,
    service: Any = None,
) -> list[tuple[int, dict]]:
    if service is None:
        service = get_sheets_service()

    values = _read_raw_values(spreadsheet_id, tab_name, service)
    if not values:
        return []

    headers = [str(cell) for cell in values[0]]
    _HEADER_CACHE[(spreadsheet_id, tab_name)] = headers

    if column_name not in headers:
        raise ValueError(
            f"column '{column_name}' not found in tab '{tab_name}'. "
            f"Available headers: {headers}"
        )

    target = str(value)
    matches: list[tuple[int, dict]] = []
    for offset, raw_row in enumerate(values[1:], start=2):
        padded = list(raw_row) + [""] * (len(headers) - len(raw_row))
        if not any(str(cell).strip() for cell in padded):
            continue
        row = {headers[i]: ("" if padded[i] is None else str(padded[i])) for i in range(len(headers))}
        if row.get(column_name, "") == target:
            matches.append((offset, row))
    return matches


def append_row(
    spreadsheet_id: str,
    tab_name: str,
    row_dict: dict,
    service: Any = None,
) -> int:
    if service is None:
        service = get_sheets_service()

    headers = _get_headers(spreadsheet_id, tab_name, service=service)
    if not headers:
        raise ValueError(
            f"tab '{tab_name}' has no header row; cannot append by column name"
        )

    row_values = [str(row_dict.get(h, "")) for h in headers]

    range_a1 = _quote_tab(tab_name)
    response = _execute_with_retry(
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        )
    )

    updated_range = response.get("updates", {}).get("updatedRange", "")
    return _parse_first_row_from_range(updated_range)


def _parse_first_row_from_range(updated_range: str) -> int:
    """Extract the first row number from an A1 range like 'Sheet1'!A5:T5 -> 5."""
    if "!" not in updated_range:
        raise RuntimeError(f"unexpected updatedRange format: {updated_range!r}")
    cell_part = updated_range.split("!", 1)[1]
    start = cell_part.split(":", 1)[0]
    digits = "".join(ch for ch in start if ch.isdigit())
    if not digits:
        raise RuntimeError(f"could not parse row number from range: {updated_range!r}")
    return int(digits)


def update_cells(
    spreadsheet_id: str,
    tab_name: str,
    row_number: int,
    col_updates: dict,
    service: Any = None,
    *,
    value_input_option: str = "USER_ENTERED",
) -> None:
    batch_update_cells(
        spreadsheet_id,
        tab_name,
        [(row_number, col_updates)],
        service=service,
        value_input_option=value_input_option,
    )


def batch_update_cells(
    spreadsheet_id: str,
    tab_name: str,
    updates: list[tuple[int, dict]],
    service: Any = None,
    *,
    value_input_option: str = "USER_ENTERED",
) -> None:
    if not updates:
        return

    if service is None:
        service = get_sheets_service()

    headers = _get_headers(spreadsheet_id, tab_name, service=service)
    header_to_index = {h: i for i, h in enumerate(headers)}

    quoted_tab = _quote_tab(tab_name)
    data: list[dict] = []

    for row_number, col_updates in updates:
        if row_number < 1:
            raise ValueError(f"row_number must be >= 1, got {row_number}")
        for col_name, new_value in col_updates.items():
            if col_name not in header_to_index:
                raise ValueError(
                    f"column '{col_name}' not found in tab '{tab_name}'. "
                    f"Available headers: {headers}"
                )
            col_letter = _col_letter(header_to_index[col_name])
            cell_range = f"{quoted_tab}!{col_letter}{row_number}"
            data.append({
                "range": cell_range,
                "values": [["" if new_value is None else str(new_value)]],
            })

    body = {
        "valueInputOption": value_input_option,
        "data": data,
    }
    _execute_with_retry(
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=body,
        )
    )
