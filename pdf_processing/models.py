"""Dataclasses for PDF extraction results."""

from dataclasses import dataclass, field


@dataclass
class PageText:
    page_number: int
    text: str
    char_count: int
    has_images: bool
    has_tables: bool
    image_count: int


@dataclass
class PageImage:
    page_number: int
    image_base64: str
    media_type: str  # "image/png"


@dataclass
class PdfExtraction:
    file_path: str
    total_pages: int
    pages: list[PageText] = field(default_factory=list)
    selected_page_images: list[PageImage] = field(default_factory=list)
    full_text: str = ""
