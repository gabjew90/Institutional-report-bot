"""pdf_processing/page_selector.py is live again under the multimodal
carve-out (analyzer.py imports it) but had no test."""
import sys

from pdf_processing.models import PageText
from pdf_processing.page_selector import select_pages


def _page(n, text="", images=0, tables=False):
    return PageText(page_number=n, text=text, char_count=len(text),
                    has_images=images > 0, has_tables=tables,
                    image_count=images)


def test_short_document_returns_every_page():
    pages = [_page(i, "x") for i in range(1, 4)]
    assert select_pages(pages, max_pages=8) == [1, 2, 3]


def test_long_document_is_capped_at_max_pages():
    pages = [_page(i, f"page {i} " * 20) for i in range(1, 21)]
    out = select_pages(pages, max_pages=5)
    assert len(out) == 5
    assert all(1 <= p <= 20 for p in out)


def test_exhibit_and_thesis_pages_outrank_filler():
    rich = _page(3, "Price target change: upgrade to Buy. EPS beat, guidance "
                    "raised, EBITDA margin expansion. Exhibit 1.", images=3,
                 tables=True)
    filler = [_page(i, "This page intentionally left blank.") for i in (1, 2, 4, 5, 6, 7)]
    out = select_pages(filler + [rich], max_pages=2)
    assert 3 in out, out


def test_returns_indices_not_objects():
    pages = [_page(i, "x" * 200, images=i % 2) for i in range(1, 12)]
    out = select_pages(pages, max_pages=4)
    assert all(isinstance(p, int) for p in out)


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
