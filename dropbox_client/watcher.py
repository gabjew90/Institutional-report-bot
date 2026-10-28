"""Poll Dropbox for new PDFs and download them."""

import logging
from datetime import datetime
from pathlib import Path

import dropbox
from dropbox.files import FileMetadata
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from dropbox_client.models import DropboxFileEntry
import db

log = logging.getLogger(__name__)

# A PDF carries this marker in its first 1024 bytes. On 2026-10-27 Dropbox
# delivered 305 files named like research notes that held only the 17-byte
# reply {"success":false}: the uploader's download from the bank portal had
# failed and it saved the error body under the note's name.
PDF_MAGIC = b"%PDF-"
NOT_PDF_PREFIX = "not a PDF"
# The smallest real PDF on record is about 2.5 KB. A FAILED row under this
# size is treated as a non-PDF even when it predates NOT_PDF_PREFIX.
JUNK_MAX_BYTES = 1024


def not_pdf_reason(local_path: Path) -> str:
    """'' when the file looks like a PDF (or cannot be read, which extraction
    will report), else the error line stored on the row."""
    try:
        with open(local_path, "rb") as f:
            head = f.read(1024)
        size = local_path.stat().st_size
    except OSError:
        return ""
    if PDF_MAGIC in head:
        return ""
    return f"{NOT_PDF_PREFIX}: Dropbox file is {size} bytes starting {head[:40]!r}"


def not_pdf_alert(count: int, example_name: str, example_reason: str) -> str:
    return (f"Dropbox delivered {count} file(s) in one poll that are not PDFs, "
            f"for example \"{example_name}\" ({example_reason}). The uploader that "
            f"fills /Current saved an error reply in place of the note. Re-upload "
            f"them and the bot downloads the new versions on its next poll.")


def is_junk_row(row: dict) -> bool:
    """A pdf_files row whose Dropbox file was not a PDF."""
    if row.get("status") != "FAILED":
        return False
    size = row.get("file_size_bytes")
    return ((row.get("error_message") or "").startswith(NOT_PDF_PREFIX)
            or (size is not None and size < JUNK_MAX_BYTES))


def _get_client() -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=settings.dropbox_refresh_token,
        app_key=settings.dropbox_app_key,
        app_secret=settings.dropbox_app_secret,
    )


def get_temporary_link(dropbox_path: str) -> str:
    """Generate a 4-hour public download URL for a Dropbox file.

    Used by the ingestion feed to attach a downloadable link to each posted
    embed. Auto-expires so no public-link sprawl. Returns empty string on
    failure (e.g., file moved/deleted in Dropbox since processing).
    """
    if not dropbox_path:
        return ""
    try:
        dbx = _get_client()
        result = dbx.files_get_temporary_link(dropbox_path)
        return result.link or ""
    except Exception as e:
        log.warning(f"Dropbox temp link failed for {dropbox_path}: {e}")
        return ""


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
    floor: str | None = None

    if cursor:
        try:
            result = dbx.files_list_folder_continue(cursor)
        except dropbox.exceptions.ApiError as e:
            # Dropbox invalidates a cursor after folder restructures or
            # long gaps (ListFolderContinueError.reset). Before
            # 2026-09-01 that raised on every 15-minute poll forever and
            # ingestion stopped without a word: the tenacity retry list
            # never included it and /seedcursor is disabled. Recover in
            # place: forget the cursor, re-list from scratch, and treat
            # only entries newer than the newest PDF already registered
            # as new. Everything older is history the bot either has or
            # never wanted; get_pdf_by_path dedups the overlap.
            err = getattr(e, "error", None)
            if not (err is not None and hasattr(err, "is_reset")
                    and err.is_reset()):
                raise
            floor = db.get_latest_dropbox_modified_at()
            log.error(f"Dropbox cursor RESET by the server; re-listing "
                      f"{folder} from scratch with floor {floor}")
            try:
                from discord_bot.ops_alert import ops_alert_sync
                ops_alert_sync(
                    "Dropbox cursor was reset by the server; re-listed "
                    f"from scratch (floor {floor}). Ingestion continues.",
                    dedupe_key="dropbox-cursor-reset")
            except Exception as ae:
                log.warning(f"cursor-reset alert failed: {ae}")
            result = dbx.files_list_folder(folder, recursive=True)
    else:
        result = dbx.files_list_folder(folder, recursive=True)

    while True:
        for entry in result.entries:
            if isinstance(entry, FileMetadata) and entry.name.lower().endswith(".pdf"):
                modified = entry.server_modified.isoformat()
                if floor and modified <= floor:
                    continue
                entries.append(DropboxFileEntry(
                    path=entry.path_display,
                    name=entry.name,
                    rev=entry.rev,
                    size=entry.size,
                    server_modified=modified,
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
    not_pdfs: list[tuple[str, str]] = []
    for entry in new_files:
        # Skip if already in database. INTENTIONAL: dedup is by path only.
        # A bank re-uploading a corrected PDF at the same path (new rev)
        # is skipped — rev is stored but not compared. Accepted for this
        # feed: banks version by filename ("...v2.pdf"), and reprocessing
        # same-path re-uploads would re-analyze on every metadata touch.
        # The exception is a path whose file was not a PDF: a new revision
        # there is the uploader re-sending the note, so it is downloaded.
        existing = db.get_pdf_by_path(entry.path)
        if existing and not (is_junk_row(existing)
                             and entry.rev != existing.get("dropbox_rev")):
            continue

        # Download
        safe_name = entry.name.replace("/", "_")
        local_path = Path(settings.pdf_download_dir) / safe_name
        if existing and existing.get("local_path"):
            local_path = Path(existing["local_path"])
        # Handle duplicate filenames
        elif local_path.exists():
            stem = local_path.stem
            suffix = local_path.suffix
            counter = 1
            while local_path.exists():
                local_path = Path(settings.pdf_download_dir) / f"{stem}_{counter}{suffix}"
                counter += 1

        try:
            download_file(entry.path, local_path)
            reason = not_pdf_reason(local_path)
            meta = dict(dropbox_rev=entry.rev, file_size_bytes=entry.size,
                        dropbox_modified_at=entry.server_modified)
            if reason:
                pdf_id = existing["id"] if existing else db.insert_pdf_file(
                    dropbox_path=entry.path, file_name=entry.name,
                    local_path=str(local_path), status="FAILED", **meta)
                db.mark_pdf_not_a_pdf(pdf_id, reason, **meta)
                db.log_event(pdf_id, "download", "failed", reason)
                not_pdfs.append((entry.name, reason))
                log.warning(f"Not a PDF: {entry.path} ({reason})")
                continue
            if existing:
                db.reset_pdf_for_reupload(existing["id"], local_path=str(local_path), **meta)
                db.log_event(existing["id"], "download", "completed",
                             f"Re-upload replaced a non-PDF. Size: {entry.size} bytes")
                downloaded.append(db.get_pdf_by_path(entry.path))
                log.info(f"Re-upload registered: {entry.name} (id={existing['id']})")
                continue
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
            if existing:
                # A re-upload over a non-PDF row: the row stays as it was.
                db.log_event(existing["id"], "download", "failed",
                             f"re-upload of a non-PDF failed to download: {e}")
                continue
            # Register a FAILED row BEFORE the cursor advances (2026-08-20
            # review, HIGH): the cursor moves past this entry permanently
            # below, so without a row the PDF was silently gone unless
            # someone ran /load. With the row, the processing queue's
            # retry sweep picks it up and process_single_pdf re-downloads
            # from dropbox_path.
            try:
                pdf_id = db.insert_pdf_file(
                    dropbox_path=entry.path,
                    file_name=entry.name,
                    local_path=str(local_path),
                    dropbox_rev=entry.rev,
                    file_size_bytes=entry.size,
                    dropbox_modified_at=entry.server_modified,
                    status="FAILED",
                )
                db.log_event(
                    pdf_id or None, "download", "failed",
                    f"{entry.path}: {e} — registered for retry sweep",
                )
            except Exception as e2:
                log.error(f"Could not register failed download row: {e2}")
                db.log_event(None, "download", "failed", f"{entry.path}: {e}")

    if not_pdfs:
        name, reason = not_pdfs[0]
        try:
            from discord_bot.ops_alert import ops_alert_sync
            ops_alert_sync(not_pdf_alert(len(not_pdfs), name, reason),
                           dedupe_key=f"dropbox-not-pdf-{datetime.utcnow():%Y-%m-%d}")
        except Exception as ae:
            log.warning(f"not-a-PDF alert failed: {ae}")

    # Advance the cursor: every entry now has a pdf_files row (DOWNLOADED
    # or FAILED-for-retry), so nothing is lost by moving past it.
    db.update_dropbox_cursor(cursor)
    log.info(f"Dropbox poll complete. Downloaded {len(downloaded)} new PDF(s)")
    return downloaded
