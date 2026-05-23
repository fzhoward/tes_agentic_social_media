"""Google Drive utilities for reading, downloading, listing, and uploading files.

Authentication is handled by get_drive_service(), which loads the OAuth token
saved by tools/google_auth.py. If the token is missing or unrecoverable, a
RuntimeError instructs the caller to run google_auth.py.

Helper functions accept an optional `service` parameter so tests can inject a
mock. When omitted, each helper builds its own service via get_drive_service().
"""

from __future__ import annotations

import io
import mimetypes
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload


SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
AUTH_ERROR_MESSAGE = (
    "Drive token not found or expired. "
    "Run 'python tools/google_auth.py' to authenticate."
)


def get_drive_service() -> Any:
    credentials_path = os.environ.get("GOOGLE_CREDENTIALS_PATH")
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

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _wrap_http_error(exc: HttpError, file_id: str) -> Exception:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
    if status == 404:
        return FileNotFoundError(f"Drive file not found: {file_id}")
    if status == 403:
        return PermissionError(f"Permission denied for Drive file: {file_id}")
    return exc


def read_file_content(file_id: str, service: Any = None) -> str:
    if service is None:
        service = get_drive_service()

    try:
        meta = service.files().get(fileId=file_id, fields="mimeType").execute()
    except HttpError as exc:
        raise _wrap_http_error(exc, file_id) from exc

    mime_type = meta.get("mimeType", "")

    try:
        if mime_type == GOOGLE_DOC_MIME:
            data = service.files().export(fileId=file_id, mimeType="text/plain").execute()
        else:
            data = service.files().get_media(fileId=file_id).execute()
    except HttpError as exc:
        raise _wrap_http_error(exc, file_id) from exc

    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)


def download_file(file_id: str, destination_path: str | os.PathLike, service: Any = None) -> str:
    if service is None:
        service = get_drive_service()

    dest = Path(destination_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
    except HttpError as exc:
        raise _wrap_http_error(exc, file_id) from exc

    dest.write_bytes(buffer.getvalue())
    return str(dest)


def list_files_in_folder(folder_id: str, service: Any = None) -> list[dict]:
    if service is None:
        service = get_drive_service()

    query = f"'{folder_id}' in parents and trashed = false"
    files: list[dict] = []
    page_token: str | None = None

    while True:
        try:
            response = service.files().list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=100,
                pageToken=page_token,
            ).execute()
        except HttpError as exc:
            raise _wrap_http_error(exc, folder_id) from exc

        for item in response.get("files", []):
            files.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "mimeType": item.get("mimeType"),
            })

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def upload_file(
    local_path: str | os.PathLike,
    folder_id: str,
    file_name: str | None = None,
    mime_type: str | None = None,
    service: Any = None,
) -> str:
    local = Path(local_path)
    if not local.is_file():
        raise FileNotFoundError(f"Local file not found: {local}")

    if service is None:
        service = get_drive_service()

    if file_name is None:
        file_name = local.name
    if mime_type is None:
        guessed, _ = mimetypes.guess_type(str(local))
        mime_type = guessed or "application/octet-stream"

    metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(str(local), mimetype=mime_type, resumable=False)

    try:
        created = service.files().create(
            body=metadata,
            media_body=media,
            fields="id",
        ).execute()
    except HttpError as exc:
        raise _wrap_http_error(exc, folder_id) from exc

    return created["id"]


def update_file_content(file_id: str, content: str, service: Any = None) -> None:
    if service is None:
        service = get_drive_service()

    try:
        meta = service.files().get(fileId=file_id, fields="mimeType").execute()
    except HttpError as exc:
        raise _wrap_http_error(exc, file_id) from exc

    mime_type = meta.get("mimeType", "")
    data = content.encode("utf-8")

    if mime_type == GOOGLE_DOC_MIME:
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype="text/plain", resumable=False)
    else:
        upload_mime = mime_type or "text/plain"
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=upload_mime, resumable=False)

    try:
        service.files().update(fileId=file_id, media_body=media).execute()
    except HttpError as exc:
        raise _wrap_http_error(exc, file_id) from exc
