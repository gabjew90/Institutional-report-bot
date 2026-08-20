"""PDF text extraction and page image rendering."""

import base64
import io
import logging
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from pdf_processing.models import PageText, PageImage, PdfExtraction
from config import settings

log = logging.getLogger(__name__)


def extract_text_per_page(pdf_path: str | Path) -> list[PageText]:
    """Extract text and metadata from every page using PyMuPDF."""
    pdf_path = Path(pdf_path)
    pages: list[PageText] = []

    doc = fitz.open(str(pdf_path))
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text")
            images = page.get_images(full=True)

            # Detect tables via text structure heuristics
            has_tables = _detect_tables(page)

            pages.append(PageText(
                page_number=page_num,
                text=text,
                char_count=len(text),
                has_images=len(images) > 0,
                has_tables=has_tables,
                image_count=len(images),
            ))
    finally:
        doc.close()

    return pages


def _detect_tables(page: fitz.Page) -> bool:
    """Heuristic table detection using text block analysis."""
    try:
        tables = page.find_tables()
        return len(tables.tables) > 0
    except Exception:
        # Fallback: check for tab-separated or column-aligned text
        text = page.get_text("text")
        lines = text.strip().split("\n")
        tab_lines = sum(1 for line in lines if "\t" in line or line.count("  ") >= 3)
        return tab_lines >= 3


def render_pages_to_images(
    pdf_path: str | Path,
    page_numbers: list[int],
    dpi: int | None = None,
) -> list[PageImage]:
    """Render selected pages to base64-encoded PNG images using PyMuPDF."""
    pdf_path = Path(pdf_path)
    dpi = dpi or settings.image_dpi
    zoom = dpi / 72  # PyMuPDF default is 72 DPI
    mat = fitz.Matrix(zoom, zoom)

    images: list[PageImage] = []
    doc = fitz.open(str(pdf_path))
    try:
        for page_num in page_numbers:
            if page_num >= len(doc):
                continue
            page = doc[page_num]
            pix = page.get_pixmap(matrix=mat)

            # Convert to PNG bytes via PIL for compression
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            # Resize if very large to save tokens (max 1568px on longest side per Claude docs)
            max_dim = 1568
            if max(img.size) > max_dim:
                ratio = max_dim / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")

            images.append(PageImage(
                page_number=page_num,
                image_base64=b64,
                media_type="image/png",
            ))
    finally:
        doc.close()

    log.info(f"Rendered {len(images)} page images from {pdf_path.name}")
    return images


def extract_pdf(
    pdf_path: str | Path,
    selected_pages: list[int] | None = None,
    pages: list[PageText] | None = None,
) -> PdfExtraction:
    """Full extraction: text from all pages + images of selected pages.

    `pages` (2026-08-20): the orchestrator already ran
    extract_text_per_page for triage — pass those through instead of
    re-opening and re-extracting the same file (pointless on 90-page
    diaries)."""
    pdf_path = Path(pdf_path)

    if pages is None:
        pages = extract_text_per_page(pdf_path)
    full_text = "\n\n".join(f"[Page {p.page_number + 1}]\n{p.text}" for p in pages)

    images = []
    if selected_pages:
        images = render_pages_to_images(pdf_path, selected_pages)

    return PdfExtraction(
        file_path=str(pdf_path),
        total_pages=len(pages),
        pages=pages,
        selected_page_images=images,
        full_text=full_text,
    )
