"""Poll Dropbox for new PDFs and download them."""

import logging
from datetime import datetime
from pathlib import Path

import dropbox
from dropbox.files import FileMetadata, FolderMetadata
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from dropbox_client.models import DropboxFileEntry
import db

log = logging.getLogger(__name__)


def _get_client() -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=settings.dropbox_refresh_token,
        app_key=settings.dropbox_app_key,
        app_secret=settings.dropbox_app_secret,
    )


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=16),
    retry=retry_if_exception_type((dropbox.exceptions.InternalServerError, ConnectionError, TimeoutError)),
    reraise=True,
)
def list_new_files(folder_path: str | None = None) -> tuple[list[DropboxFileEntry], str]:
    """List new PDF files using cursor-based delta detection.

    Returns (new_files, updated_cursor).
    """
    dbx = _get_client()
    folder = folder_path or settings.dropbox_folder_path
    cursor = db.get_dropbox_cursor()

    entries: list[DropboxFileEntry] = []

    if cursor:
        result = dbx.files_list_folder_continue(cursor)
    else:
        result = dbx.files_list_folder(folder, recursive=True)

    while True:
        for entry in result.entries:
            if isinstance(entry, FileMetadata) and entry.name.lower().endswith(".pdf"):
                entries.append(DropboxFileEntry(
                    path=entry.path_display,
                    name=entry.name,
                    rev=entry.rev,
                    size=entry.size,
                    server_modified=entry.server_modified.isoformat(),
                ))
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    return entries, result.cursor


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=16),
    retry=retry_if_exception_type((dropbox.exceptions.InternalServerError, ConnectionError, TimeoutError)),
    reraise=True,
)
def download_file(dropbox_path: str, local_dest: Path) -> Path:
    """Download a single file from Dropbox."""
    dbx = _get_client()
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    dbx.files_download_to_file(str(local_dest), dropbox_path)
    log.info(f"Downloaded: {dropbox_path} -> {local_dest}")
    return local_dest


def list_folder_files(
    folder_path: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[DropboxFileEntry]:
    """List PDF files in a folder, optionally filtered by upload date.

    Args:
        folder_path: Dropbox folder to scan (default: settings.dropbox_folder_path)
        since: Only return files modified after this datetime (UTC)
        until: Only return files modified before this datetime (UTC)

    Returns files sorted by most recently modified first.
    """
    dbx = _get_client()
    folder = folder_path or settings.dropbox_folder_path
    entries: list[DropboxFileEntry] = []

    result = dbx.files_list_folder(folder, recursive=True)
    while True:
        for entry in result.entries:
            if isinstance(entry, FileMetadata) and entry.name.lower().endswith(".pdf"):
                if since and entry.server_modified < since:
                    continue
                if until and entry.server_modified > until:
                    continue
                entries.append(DropboxFileEntry(
                    path=entry.path_display,
                    name=entry.name,
                    rev=entry.rev,
                    size=entry.size,
                    server_modified=entry.server_modified.isoformat(),
                ))
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    entries.sort(key=lambda e: e.server_modified, reverse=True)
    return entries


def poll_and_download() -> list[dict]:
    """Poll Dropbox for new PDFs, download them, and register in DB.

    Returns list of newly inserted pdf_file records.
    """
    log.info("Polling Dropbox for new PDFs...")
    new_files, cursor = list_new_files()
    log.info(f"Found {len(new_files)} new PDF(s)")

    downloaded = []
    for entry in new_files:
        # Skip if already in database
        existing = db.get_pdf_by_path(entry.path)
        if existing:
            continue

        # Download
        safe_name = entry.name.replace("/", "_")
        local_path = Path(settings.pdf_download_dir) / safe_name
        # Handle duplicate filenames
        if local_path.exists():
            stem = local_path.stem
            suffix = local_path.suffix
            counter = 1
            while local_path.exists():
                local_path = Path(settings.pdf_download_dir) / f"{stem}_{counter}{suffix}"
                counter += 1

        try:
            download_file(entry.path, local_path)
            pdf_id = db.insert_pdf_file(
                dropbox_path=entry.path,
                file_name=entry.name,
                local_path=str(local_path),
                dropbox_rev=entry.rev,
                file_size_bytes=entry.size,
                dropbox_modified_at=entry.server_modified,
            )
            db.log_event(pdf_id, "download", "completed", f"Size: {entry.size} bytes")
            downloaded.append(db.get_pdf_by_path(entry.path))
            log.info(f"Registered PDF: {entry.name} (id={pdf_id})")
        except Exception as e:
            log.error(f"Failed to download {entry.path}: {e}")
            db.log_event(None, "download", "failed", f"{entry.path}: {e}")

    # Update cursor only after successful processing
    db.update_dropbox_cursor(cursor)
    log.info(f"Dropbox poll complete. Downloaded {len(downloaded)} new PDF(s)")
    return downloaded
