"""The FOMC's rate projections (the "dots") for the print alert.

At four meetings a year (March, June, September, December) the Fed
publishes its Summary of Economic Projections at 2:00 PM with the
statement. Table 1 of the HTML page carries the median federal funds
rate each official sees for the end of each year and the longer run,
and under it the same medians from the previous projection meeting
("September projection"). The comparison the verdict needs is in that
one table, so nothing has to be kept from the last projection meeting.

The page is at /monetarypolicy/fomcprojtabl{YYYYMMDD}.htm, dated the
statement day. Before 2:00, and at meetings without projections, it
answers 404.
"""
from __future__ import annotations

import logging
import re
import urllib.error
from html.parser import HTMLParser

log = logging.getLogger(__name__)

PROJECTION_MONTHS = (3, 6, 9, 12)
TABLE_URL = "https://www.federalreserve.gov/monetarypolicy/fomcprojtabl{ymd}.htm"
LONGER_RUN = "Longer run"
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def is_projection_meeting(today_iso: str) -> bool:
    return int(today_iso[5:7]) in PROJECTION_MONTHS


class _Cells(HTMLParser):
    """Every th/td of the first table after the 'Table 1.' heading, as
    rows of {tag, id, headers, text}. The cell ids differ between pages
    (xt1b2, xb2); the headers attributes tie each value to its column
    and row, so the parser reads those instead of positions."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.rows: list[list[dict]] = []
        self._in_title = self._seen_title = self._in_table = self._done = False
        self._cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self._done:
            return
        if tag == "h4" and not self._seen_title:
            self._in_title = True
        elif tag == "table" and self._seen_title:
            self._in_table = True
        elif self._in_table and tag == "tr":
            self.rows.append([])
        elif self._in_table and tag in ("th", "td") and self.rows:
            self._cell = {"tag": tag, "id": a.get("id") or "",
                          "headers": (a.get("headers") or "").split(), "text": ""}
        elif tag == "sup" and self._cell is not None:
            # footnote markers ("Core PCE inflation<sup>4</sup>")
            self._cell["in_sup"] = True

    def handle_endtag(self, tag):
        if self._done:
            return
        if tag == "h4" and self._in_title:
            self._in_title = False
            if self.title.strip().startswith("Table 1"):
                self._seen_title = True
            else:
                self.title = ""
        elif tag == "sup" and self._cell is not None:
            self._cell.pop("in_sup", None)
        elif tag in ("th", "td") and self._cell is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._cell.pop("in_sup", None)
            self.rows[-1].append(self._cell)
            self._cell = None
        elif tag == "table" and self._in_table:
            self._in_table, self._done = False, True

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._cell is not None and not self._cell.get("in_sup"):
            self._cell["text"] += data


def _number(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def parse_projection_table(html_text: str) -> dict | None:
    """{'label': 'September 2026', 'columns': ['2026', ..., 'Longer run'],
    'median': {col: value}, 'prev_label': 'June', 'prev_median': {col: value}}
    for the federal funds rate, or None when the page is not the table."""
    p = _Cells()
    p.feed(html_text)
    m = re.search(r"(" + "|".join(_MONTHS) + r")\s+(\d{4})\s*$", " ".join(p.title.split()))
    if not m or not p.rows:
        return None
    cells = [c for row in p.rows for c in row]
    median_group = next((c["id"] for c in cells if c["tag"] == "th" and c["text"].startswith("Median")), None)
    if not median_group:
        return None
    columns = {c["id"]: c["text"] for c in cells
               if c["tag"] == "th" and c["headers"] == [median_group] and c["id"]}

    def medians(row: list[dict]) -> dict[str, float]:
        out = {}
        for c in row:
            if c["tag"] != "td":
                continue
            col = next((columns[h] for h in c["headers"] if h in columns), None)
            v = _number(c["text"])
            if col and v is not None:
                out[col] = v
        return out

    ff_row = next((r for r in p.rows if r and r[0]["tag"] == "th"
                   and r[0]["text"].lower() == "federal funds rate"), None)
    if not ff_row or not ff_row[0]["id"]:
        return None
    ff_id = ff_row[0]["id"]
    prev_row = next((r for r in p.rows if r and r[0]["tag"] == "th" and ff_id in r[0]["headers"]
                     and r[0]["text"].lower().endswith("projection")), None)
    median = medians(ff_row)
    if not median:
        return None
    return {
        "label": f"{m.group(1)} {m.group(2)}",
        "columns": list(dict.fromkeys(columns.values())),
        "median": median,
        "prev_label": prev_row[0]["text"].split()[0] if prev_row else None,
        "prev_median": medians(prev_row) if prev_row else {},
    }


def fetch_projections(today_iso: str) -> dict | None:
    """Today's projection table, parsed, or None when it is not published
    (yet) or does not belong to today's meeting."""
    from report.print_watch import _http_text
    url = TABLE_URL.format(ymd=today_iso.replace("-", ""))
    try:
        parsed = parse_projection_table(_http_text(url))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log.warning(f"print-watch: projection table fetch failed: {e}")
        return None
    except Exception as e:
        log.warning(f"print-watch: projection table fetch failed: {e}")
        return None
    expected = f"{_MONTHS[int(today_iso[5:7]) - 1]} {today_iso[:4]}"
    if not parsed or parsed["label"] != expected:
        return None
    parsed["url"] = url
    return parsed


def projection_signal(proj: dict | None, today_iso: str) -> int | None:
    """+1 when the median rate for the end of next year is above the
    previous projection meeting's, -1 below, 0 equal, None when either
    is missing. The Fed publishes both at one decimal."""
    if not proj:
        return None
    nxt = str(int(today_iso[:4]) + 1)
    now, was = proj["median"].get(nxt), proj["prev_median"].get(nxt)
    if now is None or was is None:
        return None
    diff = round(now - was, 4)
    return 1 if diff > 0 else -1 if diff < 0 else 0
