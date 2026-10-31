"""US-listed symbols, from Nasdaq's daily symbol directory (2026-10-30
quality audit, design note 1).

The router decided what a ticker is from hand-kept stopword lists, and
every audit found another word they missed: DOD (the Defense Department)
ran five tools on an ELMT question, EXXON was looked up as a symbol, TTM
(trailing twelve months) was priced. A capitalised word is a ticker only if
something is listed under it, and a company name in a ticker slot
("when is Exxon earnings") is looked up here by name.

Source: nasdaqlisted.txt and otherlisted.txt from nasdaqtrader.com, which
together list every Nasdaq, NYSE, NYSE American, NYSE Arca and Cboe
security, updated each night. Preferreds, units, warrants and rights are
dropped. OTC names (KRKNF) are not in it, so the router lets a five-letter
symbol ending in F or Y through on its own.

The repository carries a snapshot (`assets/listed_symbols.txt`, rebuilt by
`scripts/refresh_listed_symbols.py`) so every process starts with a table.
On the worker a copy next to the database is refreshed once a day in a
background thread; nothing on the /ask path waits for the download.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.request

log = logging.getLogger(__name__)

SOURCES = ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
           "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")
SNAPSHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "listed_symbols.txt")
WORDS = os.path.join(os.path.dirname(SNAPSHOT), "english_words.txt")
MAX_AGE_S = 24 * 3600
# A refresh that would shrink the table this much is a broken download.
MIN_ROWS = 8000

# symbol -> (name, is_etf)
_table: dict[str, tuple[str, bool]] | None = None
_by_word: dict[str, list[str]] = {}
_words: frozenset[str] | None = None
_last_refresh = 0.0
_lock = threading.Lock()

_NAME_CUT_RE = re.compile(
    r"\s+(?:Common Stock|Common Shares|Class [A-Z]\b|Ordinary Shares|American Depositary"
    r"|Depositary|Common Units|Units|Shares of|New Common)")
_EXTRA_RE = re.compile(r"\b(?:Warrants?|Rights?)\b")


def _trim(name: str) -> str:
    return _NAME_CUT_RE.split(name.split(" - ")[0])[0].strip()[:50]


def parse(nasdaq_text: str, other_text: str) -> list[tuple[str, str, bool]]:
    """[(symbol, name, is_etf)] from the two directory files. Test issues,
    preferreds, units, warrants and rights are left out."""
    rows = []
    # nasdaqlisted: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot|ETF|NextShares
    # otherlisted: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot|Test Issue|NASDAQ Symbol
    for text, test_col, etf_col in ((nasdaq_text, 3, 6), (other_text, 6, 4)):
        for ln in (text or "").splitlines()[1:]:
            p = ln.split("|")
            if (len(p) <= max(test_col, etf_col) or p[test_col] == "Y"
                    or ln.startswith("File Creation Time")):
                continue
            sym, name, etf = p[0].strip(), p[1].strip(), p[etf_col] == "Y"
            if not sym or "$" in sym or re.search(r"\.(?:U|W|R|WS|RT)$", sym):
                continue
            if not etf and _EXTRA_RE.search(name):
                continue
            if len(sym) == 5 and sym[-1] in "WUR" and re.search(r"\bUnits?\b", name):
                continue
            rows.append((sym, "" if etf else _trim(name), etf))
    return rows


def serialize(rows: list[tuple[str, str, bool]]) -> str:
    return "\n".join(f"{s}|{n}|{'E' if e else ''}" for s, n, e in rows) + "\n"


def _deserialize(text: str) -> dict[str, tuple[str, bool]]:
    out = {}
    for ln in (text or "").splitlines():
        p = ln.split("|")
        if len(p) == 3 and p[0]:
            out[p[0]] = (p[1], p[2] == "E")
    return out


def _cache_path() -> str | None:
    try:
        from config import settings
        return os.path.join(os.path.dirname(os.path.abspath(settings.db_path)), "listed_symbols.txt")
    except Exception:
        return None


def _first_word(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    if words and words[0] == "the":
        words = words[1:]
    return words[0] if words else ""


def use(table: dict[str, tuple[str, bool]] | None) -> None:
    """Install a table (tests, and the loader below). None clears it."""
    global _table, _by_word
    by_word: dict[str, list[str]] = {}
    for sym, (name, etf) in (table or {}).items():
        w = _first_word(name)
        if w and not etf:
            by_word.setdefault(w, []).append(sym)
    _table, _by_word = table, by_word


def _read(path: str | None) -> dict[str, tuple[str, bool]]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return _deserialize(f.read())
    except OSError:
        return {}


def _on_worker() -> bool:
    return bool(os.environ.get("RAILWAY_PROJECT_ID"))


def fetch() -> list[tuple[str, str, bool]]:
    texts = []
    for url in SOURCES:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            texts.append(r.read().decode("utf-8", errors="replace"))
    return parse(*texts)


def _refresh() -> None:
    path = _cache_path()
    try:
        rows = fetch()
    except Exception as e:
        log.warning(f"listed symbols: refresh failed ({e})")
        return
    if len(rows) < MIN_ROWS:
        log.warning(f"listed symbols: refresh returned {len(rows)} rows, kept the old table")
        return
    if path:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(serialize(rows))
            os.replace(tmp, path)
        except OSError as e:
            log.warning(f"listed symbols: could not write the cache ({e})")
    with _lock:
        use({s: (n, e) for s, n, e in rows})
    log.info(f"listed symbols: refreshed, {len(rows)} symbols")


def _ensure() -> dict[str, tuple[str, bool]]:
    global _last_refresh
    if _table is None:
        with _lock:
            if _table is None:
                cache = _cache_path()
                table = _read(cache) or _read(SNAPSHOT)
                use(table)
                if cache and os.path.exists(cache):
                    _last_refresh = os.path.getmtime(cache)
    if _on_worker() and time.time() - _last_refresh > MAX_AGE_S:
        _last_refresh = time.time()
        threading.Thread(target=_refresh, name="listed-symbols-refresh", daemon=True).start()
    return _table or {}


def is_listed(sym: str) -> bool | None:
    """True when `sym` trades on a US exchange; None when no table loaded
    (the caller then treats every symbol as listed, as before)."""
    table = _ensure()
    if not table:
        return None
    return (sym or "").upper() in table


def is_etf(sym: str) -> bool:
    return bool(_ensure().get((sym or "").upper(), ("", False))[1])


def by_name(word: str) -> str | None:
    """The one listed stock whose name starts with `word` ("exxon" ->
    XOM, from "ExxonMobil Holdings"). None when several do or none does.
    Four letters at least: shorter words start too many names."""
    w = (word or "").lower()
    if len(w) < 4 or not w.isalpha():
        return None
    _ensure()
    hits = list(_by_word.get(w, []))
    if not hits:
        hits = [s for k, syms in _by_word.items() if k.startswith(w) for s in syms]
    return hits[0] if len(hits) == 1 else None


def english_words() -> frozenset[str]:
    """Common English words of two to five letters (the first 10,000 words
    of Google's web corpus). A lowercase listed symbol that is one of these
    ("hood", "coin", "fun") needs a ticker slot to count as a ticker."""
    global _words
    if _words is None:
        try:
            with open(WORDS, encoding="utf-8") as f:
                _words = frozenset(w.strip() for w in f if w.strip())
        except OSError:
            _words = frozenset()
    return _words
