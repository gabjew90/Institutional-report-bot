"""Price statistics computed in code (2026-11-21).

The figure sample of 2026-11-20 and the outcome measure found the same
thing under four questions: a statistic over a price series was left to
the model, which was handed raw bars or nothing.

  - "what is the average qqq return on Thursdays for 2026" and its repeat
    both ended in the no-answer line: the model was handed 400 bars to
    average by hand.
  - "200 day moving on btc is what" made five history calls (the symbol
    BTC is a trust on Yahoo, the coin is BTC-USD) and then quoted a web
    page, 0.9% off.
  - A year-to-date return was worked from "an opening price of $677.88
    on January 2", which was the dividend-adjusted close.
  - "spy ytd" was routed with a default window of 90 days.

So the router reads the statistic and its window here (`parse`), the
series is fetched once, the figure is computed here, and the lines are
posted as written (discord_bot/answer_block.py). Three statistics, the
ones the room has asked for:

  average   an N-day or N-week moving average and where the price sits
            against it
  change    the change over a window (the last N days, weeks, months or
            years, this year, this month, this week, since a date), with
            the window's high and low
  weekday   the average close-to-close move on one weekday over a period

Closes are split-adjusted and not dividend-adjusted, so a level is the
price that printed and a return is the price return. Windows in days,
weeks, months and years are calendar time: the base is the last close on
or before the day the window starts. "Sessions" or "trading days" counts
bars.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from statistics import mean, median

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?"
             r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_NUM = r"(\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve)"
_UNIT = r"(sessions?|trading\s+days?|days?|d|weeks?|wks?|w|months?|mos?|years?|yrs?|y)"

# Words the room uses for an index or a coin: Yahoo's symbol and the name
# posted. Checked in order, so "nasdaq 100" is read before "nasdaq".
_NAMES = (
    (r"s\s*&\s*p(?:\s*500)?|sp\s*500|spx", "^GSPC", "S&P 500"),
    (r"nasdaq[\s-]*100|ndx", "^NDX", "Nasdaq 100"),
    (r"nasdaq|naz", "^IXIC", "Nasdaq Composite"),
    (r"dow(?:\s+jones)?|djia", "^DJI", "Dow"),
    (r"russell(?:\s*2000)?|rut", "^RUT", "Russell 2000"),
    (r"vix", "^VIX", "VIX"),
    (r"bitcoin|btc", "BTC-USD", "Bitcoin"),
    (r"ethereum|eth", "ETH-USD", "Ethereum"),
    (r"solana|sol", "SOL-USD", "Solana"),
)
_NAME_RES = tuple((re.compile(rf"(?<![\w$])(?:{rx})(?![\w&])", re.I), sym, name) for rx, sym, name in _NAMES)
# The router's own tickers for the same things.
_TICKER_SYMBOLS = {"SPX": "^GSPC", "NDX": "^NDX", "VIX": "^VIX", "DOW": "^DJI", "DJIA": "^DJI",
                   "RUT": "^RUT", "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
_DISPLAY = {sym: name for _rx, sym, name in _NAMES}
# A coin's series back to the router's ticker, which is what a reply to
# the answer carries on with (discord_bot/follow_up.py).
_ROUTER_FORM = {"BTC-USD": "BTC", "ETH-USD": "ETH", "SOL-USD": "SOL"}


def _today() -> date:
    """New York's date: the session calendar the windows are read against."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return datetime.utcnow().date()

_AVG_RE = re.compile(
    r"\b(\d{1,3})\s*-?\s*(day|d|week|wk|w)?\s*-?\s*"
    r"(?:(simple|exponential)\s+)?(moving\s+average|moving\s+avg|moving|dma|sma|ema|ma)\b", re.I)
_WEEKDAY_RE = re.compile(r"\b(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?)\b", re.I)
_AVG_WORD_RE = re.compile(
    r"\b(?:average|avg|mean|typical(?:ly)?|usually|tend(?:s)?\s+to|median)\b", re.I)
_MOVE_WORD_RE = re.compile(r"\b(?:returns?|moves?|gains?|performance|perform(?:s|ed)?|change|do(?:es)?|trade(?:s)?)\b",
                           re.I)
# What makes a window a question about the change over it.
_CHANGE_RE = re.compile(
    r"\b(?:returns?|returned|performance|perform(?:ed|ing|s)?|up|down|dropp?(?:ed|ing|s)?|drop|fallen|fell|fall(?:ing)?"
    r"|gain(?:ed|s)?|lost|lose|risen|rose|moved?|chang(?:e|ed)|ytd|mtd|range|percent(?:age)?|doing|done"
    r"|how\s+(?:has|did|have|much|far))\b|%", re.I)
# The change asked about is not the price's: another quantity (market cap,
# volume), an option, or the asker's own position.
_NOT_PRICE_RE = re.compile(
    r"\b(?:m(?:arket)?\s*cap|volume|revenue|sales|dividends?|short\s+interest|float|implied"
    r"|calls?|puts?|contracts?|options?|strikes?|premium"
    r"|(?:am|are|have|has|did|do|was|were)\s+(?:i|we)|(?:i|we)\s+(?:am|are|was|were|have|had|moved)|i'?m"
    r"|my\s+(?:position|shares|account|port\w*|rank\w*)|rank(?:ing|ed|s)?)\b",
    re.I)
_RANGE_RE = re.compile(r"\brange\b", re.I)

_YTD_RE = re.compile(
    r"\b(?:ytd|year[\s-]*to[\s-]*date|this\s+year|on\s+the\s+year|for\s+the\s+year|so\s+far\s+this\s+year"
    r"|since\s+(?:the\s+)?(?:start|beginning)\s+of\s+(?:the\s+|this\s+)?year|since\s+new\s+years?"
    r"|since\s+(?:the\s+)?(?:end|close)\s+of\s+last\s+year"
    r"|since\s+jan(?:uary)?(?:\s+1(?:st)?)?(?!\s*\d))\b", re.I)
_MTD_RE = re.compile(r"\b(?:mtd|month[\s-]*to[\s-]*date|this\s+month|so\s+far\s+this\s+month)\b", re.I)
_WTD_RE = re.compile(r"\b(?:wtd|week[\s-]*to[\s-]*date|this\s+week|so\s+far\s+this\s+week)\b", re.I)
_LAST_RE = re.compile(
    rf"\b(?:last|past|previous|prior|trailing)\s+(?:{_NUM}\s*)?{_UNIT}\b", re.I)
# "last week", "last month", "last year" with no "the" and no number: the
# calendar period before this one, not the trailing stretch.
_PREV_RE = re.compile(r"(?<!\bthe )(?<!\bthese )(?<!\bthis )\blast (week|month|year)\b", re.I)
# "in 2025", "for 2024": that calendar year
_IN_YEAR_RE = re.compile(r"\b(?:in|for|during)\s+(20\d\d|19[89]\d)\b", re.I)
# "5 year return", "over 3 months". Never "in 2 weeks": that looks ahead.
_N_UNIT_RE = re.compile(
    rf"(?<![\w.$]){_NUM}\s*-?\s*{_UNIT}\s+(?:returns?|performance|change|move|gain|loss|chart|range)\b"
    rf"|\bover\s+(?:the\s+)?{_NUM}\s*-?\s*{_UNIT}\b", re.I)
_SINCE_DATE_RE = re.compile(
    rf"\bsince\s+(?:{_MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(20\d\d))?"
    r"|(20\d\d)-(\d{2})-(\d{2})"
    r"|(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?)(?!\d)", re.I)
_SINCE_MONTH_RE = re.compile(rf"\bsince\s+{_MONTH_RE}(?:\s+(20\d\d))?\b(?!\s*\d)", re.I)
_SINCE_YEAR_RE = re.compile(r"\bsince\s+(20\d\d|19\d\d)\b", re.I)
_SINCE_IPO_RE = re.compile(r"\bsince\s+(?:its\s+|the\s+)?(?:ipo|listing|inception|it\s+listed|going\s+public)\b",
                           re.I)


def _month(word: str) -> int:
    w = word.lower()[:3]
    return next(i + 1 for i, m in enumerate(_MONTHS) if m.startswith(w))


def _n(word: str | None) -> int:
    if not word:
        return 1
    w = word.lower()
    return int(w) if w.isdigit() else _NUM_WORDS.get(w, 1)


def _unit(word: str) -> str:
    w = re.sub(r"\s+", " ", word.lower())
    if w.startswith(("session", "trading")):
        return "session"
    return {"d": "day", "w": "week", "y": "year"}.get(w[0], "day") if w[0] in "dwy" else "month"


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def _back(today: date, n: int, unit: str) -> date:
    if unit == "day":
        return today - timedelta(days=n)
    if unit == "week":
        return today - timedelta(weeks=n)
    months = n if unit == "month" else 12 * n
    y, m = divmod(today.year * 12 + today.month - 1 - months, 12)
    m += 1
    for d in range(today.day, 27, -1):
        try:
            return date(y, m, d)
        except ValueError:
            continue
    return date(y, m, min(today.day, 28))


def window(q: str, today: date) -> dict | None:
    """The stretch of time a question names: `start` is the day whose close
    is the base (the last close on or before it), `before` says the base is
    the last close before that day (the start of a year, month or week),
    `sessions` counts bars back from the last instead."""
    q = q or ""
    if _YTD_RE.search(q):
        return {"start": date(today.year, 1, 1).isoformat(), "before": True, "label": "this year"}
    if _MTD_RE.search(q):
        return {"start": today.replace(day=1).isoformat(), "before": True, "label": "this month"}
    if _WTD_RE.search(q):
        return {"start": (today - timedelta(days=today.weekday())).isoformat(), "before": True,
                "label": "this week"}
    m = _SINCE_DATE_RE.search(q)
    if m:
        try:
            if m.group(1):
                d = date(int(m.group(3) or today.year), _month(m.group(1)), int(m.group(2)))
                explicit = bool(m.group(3))
            elif m.group(4):
                d, explicit = date(int(m.group(4)), int(m.group(5)), int(m.group(6))), True
            else:
                yr = int(m.group(9) or today.year)
                d = date(yr + 2000 if yr < 100 else yr, int(m.group(7)), int(m.group(8)))
                explicit = bool(m.group(9))
        except ValueError:
            return None
        if d > today and not explicit:
            d = d.replace(year=d.year - 1)
        if d > today:
            return None
        return {"start": d.isoformat(), "label": f"since {d:%b} {d.day}" + (f", {d.year}" if d.year != today.year else "")}
    m = _SINCE_MONTH_RE.search(q)
    if m:
        d = date(int(m.group(2) or today.year), _month(m.group(1)), 1)
        if d > today and not m.group(2):
            d = d.replace(year=d.year - 1)
        if d > today:
            return None
        return {"start": d.isoformat(), "before": True,
                "label": f"since the start of {d:%B}" + (f" {d.year}" if d.year != today.year else "")}
    m = _SINCE_YEAR_RE.search(q)
    if m:
        d = date(int(m.group(1)), 1, 1)
        return None if d > today else {"start": d.isoformat(), "before": True, "label": f"since the start of {d.year}"}
    if _SINCE_IPO_RE.search(q):
        return {"start": "1970-01-01", "label": "since it listed", "max": True}
    m = _PREV_RE.search(re.sub(r"\s+", " ", q))
    if m:
        unit = m.group(1).lower()
        if unit == "week":
            end = today - timedelta(days=today.weekday() + 1)           # last Sunday
            first = end - timedelta(days=6)
            label = f"last week ({first:%b} {first.day} to {(first + timedelta(days=4)):%b} {(first + timedelta(days=4)).day})"
        elif unit == "month":
            end = today.replace(day=1) - timedelta(days=1)
            first = end.replace(day=1)
            label = f"last month ({first:%B})"
        else:
            first, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
            label = f"last year ({first.year})"
        return {"start": first.isoformat(), "before": True, "end": end.isoformat(), "label": label}
    m = _LAST_RE.search(q) or _N_UNIT_RE.search(q)
    if m:
        groups = [g for g in m.groups() if g]
        num = next((g for g in groups if g.isdigit() or g.lower() in _NUM_WORDS), None)
        unit = _unit(groups[-1])
        n = _n(num)
        if n < 1 or (unit == "year" and n > 40):
            return None
        if unit == "session":
            return {"sessions": n, "label": f"over the last {_plural(n, 'session')}"}
        return {"start": _back(today, n, unit).isoformat(),
                "label": f"over the last {_plural(n, unit)}" if n > 1 else f"over the last {unit}"}
    # a calendar year, read last so "3 month return" wins over a year named beside it
    m = _IN_YEAR_RE.search(q)
    if m and int(m.group(1)) < today.year:
        y = int(m.group(1))
        return {"start": date(y, 1, 1).isoformat(), "before": True, "end": date(y, 12, 31).isoformat(),
                "label": f"in {y}"}
    if m and int(m.group(1)) == today.year:
        return {"start": date(today.year, 1, 1).isoformat(), "before": True, "label": "this year"}
    return None


def _lower_symbol(q: str) -> str | None:
    """A listed symbol typed in lowercase or with one capital, when the
    question is already known to ask for a statistic. Never an English
    word or a word of the room's."""
    from discord_bot import ask_router as R, listed_symbols as L
    # a link's path is not the asker's words
    for tok in re.findall(r"(?<![\w$#@<])[A-Za-z]{2,5}(?![\w'])", re.sub(r"https?://\S+", " ", q or "")):
        if tok.isupper() or R._is_word(tok) or R.not_a_ticker(tok):
            continue
        if L.is_listed(tok.upper()):
            return tok.upper()
    return None


def symbol_of(q: str, tickers: list[str] | None = None) -> tuple[str, str] | None:
    """(Yahoo symbol, the name posted) for what the question is about: the
    router's ticker, an index or coin by name, or a lowercase symbol."""
    from discord_bot import ask_router as R
    for t in tickers or []:
        sym = _TICKER_SYMBOLS.get(t.upper(), t.upper())
        # the router keeps an unlisted token when nothing listed is named
        # ("in slurs ranking" gave SLURS): no series exists for one
        if sym != t.upper() or R.listed(t):
            return sym, _DISPLAY.get(sym, t.upper())
    for rx, sym, name in _NAME_RES:
        if rx.search(q or ""):
            return sym, name
    low = _lower_symbol(q)
    return (low, low) if low else None


def _average_asked(m: re.Match | None) -> bool:
    """"200 day moving", "50dma", "200 sma", "200ma". A bare "moving" needs
    its unit and a bare "ma" a length of 5 or more."""
    if not m or not 2 <= int(m.group(1)) <= 400:
        return False
    word = m.group(4).lower()
    if m.group(2):
        return True
    return word not in ("moving", "ma") or (word == "ma" and int(m.group(1)) >= 5)


def parse(q: str, tickers: list[str] | None = None, today: date | None = None) -> dict | None:
    """The statistic a question asks for, as the prefetch's arguments, or
    None. `tickers` are the router's. More than one router ticker is a
    comparison, which this does not answer."""
    q = q or ""
    today = today or _today()
    if len(tickers or []) > 1:
        return None
    kind: dict | None = None
    m = _AVG_RE.search(q)
    if _average_asked(m):
        unit = "week" if (m.group(2) or "d").lower().startswith("w") else "day"
        ema = (m.group(3) or "").lower() == "exponential" or m.group(4).lower() == "ema"
        kind = {"kind": "average", "n": int(m.group(1)), "unit": unit, "ema": ema}
    if kind is None:
        wd = _WEEKDAY_RE.search(q)
        if wd and _AVG_WORD_RE.search(q) and _MOVE_WORD_RE.search(q):
            w = window(q, today)
            yr = _IN_YEAR_RE.search(q)
            if yr and int(yr.group(1)) <= today.year:
                y = int(yr.group(1))
                w = {"start": date(y, 1, 1).isoformat(), "label": f"in {y}",
                     "end": date(y, 12, 31).isoformat() if y < today.year else None}
            elif w is None or w.get("sessions") or w.get("max"):
                w = {"start": _back(today, 1, "year").isoformat(), "label": "over the last year"}
            kind = {"kind": "weekday", "weekday": WEEKDAYS.index(wd.group(1).lower().rstrip("s")),
                    "start": w["start"], "end": w.get("end"), "label": w["label"]}
    if kind is None:
        w = window(q, today)
        if w and _CHANGE_RE.search(q) and not _NOT_PRICE_RE.search(q):
            kind ={"kind": "change", **w, "range_first": bool(_RANGE_RE.search(q))}
    if kind is None:
        return None
    found = symbol_of(q, tickers)
    if not found:
        return None
    # `series` is what Yahoo is asked for, `symbol` the router's ticker
    return {"symbol": _ROUTER_FORM.get(found[0], found[0]), "series": found[0], "name": found[1], **kind}


# ---------------------------------------------------------------- compute

def _bars(hist: list[dict] | None) -> list[dict]:
    return [h for h in hist or [] if h.get("close") is not None and h.get("date")]


def moving_average(bars: list[dict], n: int, ema: bool = False) -> dict:
    """The N-bar average of closes (oldest first) and the last close against it."""
    closes = [float(b["close"]) for b in bars]
    if len(closes) < n:
        return {"error": f"only {len(closes)} closes in the history, {n} needed"}
    if ema:
        value, k = mean(closes[:n]), 2 / (n + 1)
        for c in closes[n:]:
            value = c * k + value * (1 - k)
    else:
        value = mean(closes[-n:])
    last = closes[-1]
    return {"value": round(value, 2), "last": round(last, 2), "last_date": str(bars[-1]["date"])[:10],
            "pct_vs": round((last / value - 1) * 100, 1)}


def change(bars: list[dict], *, start: str | None = None, before: bool = False,
           sessions: int | None = None, end: str | None = None) -> dict:
    """The change from a base close to the last, with the high and low
    since the base. The base is the last close on or before `start`
    (before it when `before`), or `sessions` bars back. `end` closes the
    window: the last close is the last one on or before it."""
    if end:
        bars = [h for h in bars if str(h["date"])[:10] <= end]
    if not bars:
        return {"error": "no closes in the history"}
    if not sessions and not start:
        return {"error": "no window to measure"}
    if sessions:
        b = len(bars) - 1 - sessions
        if b < 0:
            return {"error": f"only {len(bars)} sessions in the history"}
    else:
        at = [i for i, h in enumerate(bars)
              if (str(h["date"])[:10] < start if before else str(h["date"])[:10] <= start)]
        # nothing that early: the history starts later (a listing), so the
        # base is its first close and the lines say so
        b = at[-1] if at else 0
    if b >= len(bars) - 1:
        return {"error": "no session since the start of that window"}
    base, last = float(bars[b]["close"]), float(bars[-1]["close"])
    since = bars[b + 1:]
    hi = max(since, key=lambda h: float(h.get("high") or h["close"]))
    lo = min(since, key=lambda h: float(h.get("low") or h["close"]))
    high, low = float(hi.get("high") or hi["close"]), float(lo.get("low") or lo["close"])
    return {"base_date": str(bars[b]["date"])[:10], "base": round(base, 2),
            "last_date": str(bars[-1]["date"])[:10], "last": round(last, 2),
            "pct": round((last / base - 1) * 100, 1), "sessions": len(since),
            "high": round(high, 2), "high_date": str(hi["date"])[:10],
            "low": round(low, 2), "low_date": str(lo["date"])[:10],
            "range_pct": round((high / low - 1) * 100, 1) if low > 0 else None,
            "from_first_bar": not sessions and b == 0 and str(bars[0]["date"])[:10] > (start or "")}


def weekday_moves(bars: list[dict], weekday: int, start: str, end: str | None = None) -> dict:
    """Close-to-close moves on one weekday (0 = Monday) between two dates."""
    moves = []
    for prev, cur in zip(bars, bars[1:]):
        day = str(cur["date"])[:10]
        if day < start or (end and day > end) or date.fromisoformat(day).weekday() != weekday:
            continue
        moves.append((day, (float(cur["close"]) / float(prev["close"]) - 1) * 100))
    if not moves:
        return {"error": "no such weekday in that period's history"}
    pcts = [p for _d, p in moves]
    best, worst = max(moves, key=lambda x: x[1]), min(moves, key=lambda x: x[1])
    return {"count": len(moves), "avg_pct": round(mean(pcts), 2), "median_pct": round(median(pcts), 2),
            "up": sum(1 for p in pcts if p > 0), "down": sum(1 for p in pcts if p < 0),
            "best": {"date": best[0], "pct": round(best[1], 1)},
            "worst": {"date": worst[0], "pct": round(worst[1], 1)},
            "first_day": moves[0][0], "last_day": moves[-1][0]}


def _fetch_from(spec: dict, today: date) -> tuple[str, str]:
    """(start date, interval) that holds every bar the statistic needs."""
    kind = spec["kind"]
    if kind == "average":
        if spec["unit"] == "week":
            return (today - timedelta(days=int(spec["n"] * 7 * (4 if spec.get("ema") else 1.1)) + 21)).isoformat(), "1wk"
        return (today - timedelta(days=int(spec["n"] * (4.5 if spec.get("ema") else 1.6)) + 15)).isoformat(), "1d"
    if spec.get("max"):
        return "1970-01-01", "1d"
    if spec.get("sessions"):
        return (today - timedelta(days=int(spec["sessions"] * 1.6) + 15)).isoformat(), "1d"
    return (date.fromisoformat(spec["start"]) - timedelta(days=12)).isoformat(), "1d"


def _in_session(symbol: str, last_date: str) -> bool:
    """The last bar is today's and still moving: a coin always, anything
    else before 4 PM New York on the bar's own day."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return False
    if symbol.endswith("-USD"):
        return last_date >= datetime.utcnow().date().isoformat()
    return last_date == now.date().isoformat() and now.hour < 16


async def _execute_price_stats(args: dict) -> dict:
    """Prefetch only (ask_router.T_STATS): fetch the series once and
    compute the statistic `parse` read from the question."""
    from report import market_data as _md
    spec = dict(args or {})
    symbol = str(spec.get("series") or spec.get("symbol") or "").upper()
    if not symbol or spec.get("kind") not in ("average", "change", "weekday"):
        return {"status": "error", "error": "no statistic to compute"}
    today = _today()
    start, interval = _fetch_from(spec, today)
    try:
        hist = await asyncio.to_thread(_md.fetch_price_history, symbol, start, None, interval, False)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:160]}"}
    bars = _bars(hist)
    if not bars:
        return {"status": "no_data", "symbol": symbol,
                "error": f"no price history for {symbol}: say so, never fill in a figure"}
    if spec["kind"] == "average":
        stat = moving_average(bars, int(spec["n"]), bool(spec.get("ema")))
    elif spec["kind"] == "weekday":
        stat = weekday_moves(bars, int(spec["weekday"]), spec["start"], spec.get("end"))
    else:
        stat = change(bars, start=spec.get("start"), before=bool(spec.get("before")),
                      sessions=spec.get("sessions"), end=spec.get("end"))
    if stat.get("error"):
        return {"status": "no_data", "symbol": symbol, "error": stat["error"]}
    last_date = stat.get("last_date") or str(bars[-1]["date"])[:10]
    return {"status": "ok", **spec, "series": symbol, "name": spec.get("name") or symbol, **stat,
            "in_session": interval == "1d" and not spec.get("end") and _in_session(symbol, last_date),
            "how_computed": ("computed by the system from Yahoo's daily closes, adjusted for splits and "
                             "not for dividends"),
            "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}


# ------------------------------------------------------------------ lines

def _money(v: float, symbol: str) -> str:
    """Index levels in points, everything else in dollars."""
    return f"{v:,.2f}" if symbol.startswith("^") else f"${v:,.2f}"


def _day(iso: str, today: date) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d:%b} {d.day}" + (f", {d.year}" if d.year != today.year else "")


def _vs(pct: float) -> str:
    if pct == 0:
        return "level with"
    return f"**{abs(pct):.1f}% {'above' if pct > 0 else 'below'}**"


def block_lines(r: dict, today: date | None = None) -> list[str]:
    """The answer's lines (discord_bot/answer_block.py)."""
    today = today or _today()
    sym = r.get("series") or r["symbol"]
    name = r.get("name") or sym
    now = "now" if r.get("in_session") else f"at the {_day(r['last_date'], today)} close" if r.get("last_date") else ""
    if r["kind"] == "average":
        word = "exponential moving average" if r.get("ema") else "moving average"
        if r["unit"] == "week":
            # a weekly bar is dated by its first day, so no date is posted
            span, price = f"from the last {r['n']} weekly closes, this week's included", "Latest price"
        else:
            span = f"from the last {r['n']} daily closes through {_day(r['last_date'], today)}"
            price = f"Price {now}"
        if r.get("ema"):
            span = "weighted toward the latest closes, " + span.replace(f"from the last {r['n']} ", "on ")
        return [f"→ **{name}** {r['n']}-{r['unit']} {word}: **{_money(r['value'], sym)}**, {span}",
                f"→ {price}: **{_money(r['last'], sym)}**, {_vs(r['pct_vs'])} the average"]
    if r["kind"] == "weekday":
        wd = WEEKDAYS[r["weekday"]].capitalize()
        lines = [f"→ **{name}** on {wd}s {r['label']}: average move **{r['avg_pct']:+.2f}%**, median "
                 f"**{r['median_pct']:+.2f}%**, close to close over {r['count']} {wd}s "
                 f"({_day(r['first_day'], today)} to {_day(r['last_day'], today)})",
                 f"→ Up on {r['up']}, down on {r['down']}. Best **{r['best']['pct']:+.1f}%** "
                 f"({_day(r['best']['date'], today)}), worst **{r['worst']['pct']:+.1f}%** "
                 f"({_day(r['worst']['date'], today)})"]
        return lines
    pct = r["pct"]
    move = "flat" if pct == 0 else f"{'up' if pct > 0 else 'down'} **{abs(pct):.1f}%**"
    label = "since its first close in the history" if r.get("from_first_bar") else r["label"]
    path = (f"**{_money(r['base'], sym)}** at the {_day(r['base_date'], today)} close to "
            f"**{_money(r['last'], sym)}** {now}")
    low = f"**{_money(r['low'], sym)}** ({_day(r['low_date'], today)})"
    high = f"**{_money(r['high'], sym)}** ({_day(r['high_date'], today)})"
    # a window that has ended ("in 2025", "last week") is told in the past
    closed = bool(r.get("end"))
    if r.get("range_first"):
        # the question asked for the range: it leads, with its width
        width = f", **{r['range_pct']:.1f}%** from low to high" if r.get("range_pct") is not None else ""
        lines = [f"→ **{name}** {'traded' if closed else 'has traded'} between {low} and {high} {label}{width}",
                 f"→ It {'was' if closed else 'is'} {move} over that stretch: {path}"]
    else:
        lines = [f"→ **{name}** {'was' if closed else 'is'} {move} {label}: {path}",
                 f"→ Low {'in that stretch' if closed else 'since then'} {low}, high {high}"]
    days = (date.fromisoformat(r["last_date"]) - date.fromisoformat(r["base_date"])).days
    if days >= 180 and not sym.startswith("^") and not sym.endswith("-USD"):
        lines.append("→ Price change only. Dividends are not counted.")
    return lines
