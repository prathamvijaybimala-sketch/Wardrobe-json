"""
Google Drive intake — list and download clothing photos.

Uses a service account to authenticate with the Drive API.
The target Drive folder must be shared with the service account's email
(see README for setup instructions).
"""

import io
import logging
import time
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

# Drive API scopes
_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# MIME types we care about (PNG images, as specified)
_IMAGE_MIME_TYPES = {
    "image/png",
}

# Also accept these common image types if encountered
_ALL_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
}

# Retry configuration
_MAX_RETRIES = 5
_BASE_DELAY_SECONDS = 1.0


def _build_drive_service(service_account_info: dict):
    """
    Build an authorized Drive API v3 service client.

    Args:
        service_account_info: Parsed service account JSON dict.

    Returns:
        Google Drive API service resource.
    """
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=_SCOPES
    )
    return build("drive", "v3", credentials=credentials)


def _retry_on_error(func, *args, **kwargs):
    """
    Execute a function with exponential backoff retry on 429/500 errors.

    Args:
        func: Callable to retry.
        *args, **kwargs: Arguments to pass to the callable.

    Returns:
        The return value of func.

    Raises:
        HttpError: If all retries are exhausted.
    """
    last_error = None
    for attempt in range(_MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            status_code = e.resp.status if hasattr(e, "resp") else None
            if status_code in (429, 500, 503):
                delay = _BASE_DELAY_SECONDS * (2 ** attempt)
                logger.warning(
                    f"Drive API error {status_code} (attempt {attempt + 1}/{_MAX_RETRIES}), "
                    f"retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                last_error = e
            else:
                raise
        except Exception:
            raise
    raise last_error


def list_new_images(
    folder_id: str,
    processed_ids: set[str],
    service_account_info: dict,
) -> list[dict]:
    """
    List PNG image files in the Drive folder that haven't been processed yet.

    Args:
        folder_id: Google Drive folder ID containing clothing photos.
        processed_ids: Set of Drive file IDs already ingested.
        service_account_info: Parsed service account JSON dict.

    Returns:
        List of dicts with keys: id, name, mimeType.
        Each represents a new, unprocessed image file.
    """
    service = _build_drive_service(service_account_info)

    new_images = []
    page_token = None

    while True:
        # Build query: must be in folder, must be an image, not trashed
        mime_query = " or ".join(
            f"mimeType = '{mime}'" for mime in _IMAGE_MIME_TYPES
        )
        query = (
            f"'{folder_id}' in parents "
            f"and ({mime_query}) "
            f"and trashed = false"
        )

        def _list_files(token=page_token):
            return (
                service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageToken=token,
                    pageSize=100,
                    orderBy="createdTime asc",
                )
                .execute()
            )

        response = _retry_on_error(_list_files)

        files = response.get("files", [])
        for f in files:
            if f["id"] not in processed_ids:
                new_images.append(f)
                logger.info(f"Found new image: {f['name']} ({f['id']})")

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info(
        f"Found {len(new_images)} new images "
        f"(skipped {len(files) - len(new_images)} already processed)"
    )
    return new_images


def download_image(
    file_id: str,
    service_account_info: dict,
) -> bytes:
    """
    Download an image file from Google Drive.

    Args:
        file_id: The Drive file ID to download.
        service_account_info: Parsed service account JSON dict.

    Returns:
        Raw image bytes.

    Raises:
        HttpError: If the download fails after retries.
    """
    service = _build_drive_service(service_account_info)

    def _do_download():
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                logger.debug(
                    f"Download progress for {file_id}: {int(status.progress() * 100)}%"
                )
        fh.seek(0)
        return fh.read()

    image_bytes = _retry_on_error(_do_download)
    logger.info(f"Downloaded image {file_id}: {len(image_bytes)} bytes")
    return image_bytes
