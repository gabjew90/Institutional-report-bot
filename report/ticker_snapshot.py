"""The ticker snapshot /ask hands the model for any single-stock question.

2026-09-30, owner's swing-trader triage: what the stock is and what drives
it, the next binary event, liquidity, range, float and short interest, and
for a cash-burning company its cash and recent offering registrations.
Every figure is narrated with its date. Nothing here grades a figure
(owner: "this is all discretionary so don't dictate any thresholds just
narrate the data"), so there is no "low float" or "liquid" flag anywhere.

Source is Yahoo through yfinance (quote summary, daily history, and the SEC
filings list Yahoo mirrors, which spares an EDGAR client and its required
contact header). The next earnings date stays with `lookup_earnings_date`,
which the router prefetches beside this.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

CACHE_TTL_S = 15 * 60
_CACHE: dict[str, tuple[float, dict]] = {}

# Registration forms that put new shares or a shelf on file. 424B2 is left
# out: banks file it for every structured note and it is not equity supply.
OFFERING_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR", "F-1", "F-3", "F-3ASR", "424B4", "424B5"}
FILINGS_LOOKBACK_DAYS = 365
MAX_FILINGS = 5
SUMMARY_CHARS = 360
ATR_DAYS = 14
ADV_DAYS = 30


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _first_sentences(text: str, limit: int = SUMMARY_CHARS) -> str:
    """Whole sentences of the business summary up to `limit` characters."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    out = ""
    for part in text.split(". "):
        nxt = (out + ". " if out else "") + part
        if len(nxt) > limit:
            break
        out = nxt
    return (out.rstrip(".") + ".") if out else text[:limit].rsplit(" ", 1)[0] + "..."


def history_stats(rows: list[dict]) -> dict:
    """ATR, average volume and dollar volume from daily bars (oldest first),
    each bar {high, low, close, volume}. The last bar is the current
    session, which counts toward relative volume, not the averages."""
    out: dict = {}
    bars = [b for b in rows if _num(b.get("close")) is not None]
    # Before the open Yahoo can end the series with today's row at zero
    # volume; that is not a session yet.
    while bars and not _num(bars[-1].get("volume")):
        bars.pop()
    if len(bars) < 2:
        return out
    trs = []
    for prev, cur in zip(bars, bars[1:]):
        h, l, pc = _num(cur.get("high")), _num(cur.get("low")), _num(prev.get("close"))
        if h is None or l is None or pc is None:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if trs:
        window = trs[-ATR_DAYS:]
        atr = sum(window) / len(window)
        last = _num(bars[-1]["close"])
        out["atr_days"] = len(window)
        out["atr"] = round(atr, 2)
        if last:
            out["atr_pct"] = round(atr / last * 100, 2)
    done = bars[:-1][-ADV_DAYS:]
    vols = [_num(b.get("volume")) for b in done]
    vols = [v for v in vols if v is not None]
    if vols:
        out["adv_days"] = len(vols)
        out["adv_shares"] = round(sum(vols) / len(vols))
        dollar = [(_num(b.get("close")) or 0) * (_num(b.get("volume")) or 0) for b in done]
        out["adv_dollars"] = round(sum(dollar) / len(dollar))
        today = _num(bars[-1].get("volume"))
        if today is not None and out["adv_shares"]:
            out["today_volume"] = round(today)
            out["relative_volume"] = round(today / out["adv_shares"], 2)
    return out


def offering_filings(filings: list[dict], today: date | None = None) -> list[dict]:
    """Offering registrations in the last year, newest first."""
    today = today or datetime.now(timezone.utc).date()
    since = today - timedelta(days=FILINGS_LOOKBACK_DAYS)
    out = []
    for f in filings or []:
        form = (f.get("type") or "").strip().upper()
        d = f.get("date")
        if isinstance(d, datetime):
            d = d.date()
        elif isinstance(d, str):
            try:
                d = date.fromisoformat(d[:10])
            except ValueError:
                continue
        if form in OFFERING_FORMS and isinstance(d, date) and d >= since:
            out.append({"date": d.isoformat(), "form": form})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:MAX_FILINGS]


def build(symbol: str, info: dict, history: list[dict], filings: list[dict],
          today: date | None = None) -> dict:
    """Assemble the snapshot from already-fetched Yahoo data. Pure, so it is
    tested without the network."""
    sym = (symbol or "").upper()
    qt = (info.get("quoteType") or "").upper()
    if qt and qt != "EQUITY":
        return {"status": "not_a_stock", "symbol": sym, "quote_type": qt,
                "name": info.get("longName") or info.get("shortName")}
    # Yahoo answers an unknown symbol ("APPLE" guessed from "why is apple
    # down") with a near-empty dict rather than an error.
    if not (qt or info.get("longName") or info.get("shortName")) and not history:
        return {"status": "no_data", "symbol": sym}
    price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    hi, lo = _num(info.get("fiftyTwoWeekHigh")), _num(info.get("fiftyTwoWeekLow"))
    snap: dict = {
        "status": "ok", "symbol": sym,
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"), "industry": info.get("industry"),
        "business": _first_sentences(info.get("longBusinessSummary") or ""),
        "market_cap": _num(info.get("marketCap")),
        "price": price, "beta": _num(info.get("beta")),
        "week52_high": hi, "week52_low": lo,
    }
    if price and hi:
        snap["pct_below_52w_high"] = round((hi - price) / hi * 100, 1)
    if price and lo:
        snap["pct_above_52w_low"] = round((price - lo) / lo * 100, 1)
    snap.update(history_stats(history))
    snap.update({
        "float_shares": _num(info.get("floatShares")),
        "shares_outstanding": _num(info.get("sharesOutstanding")),
        "short_pct_float": (round(_num(info.get("shortPercentOfFloat")) * 100, 1)
                            if _num(info.get("shortPercentOfFloat")) is not None else None),
        "days_to_cover": _num(info.get("shortRatio")),
        "total_cash": _num(info.get("totalCash")),
        "total_debt": _num(info.get("totalDebt")),
        "operating_cashflow_ttm": _num(info.get("operatingCashflow")),
        "free_cashflow_ttm": _num(info.get("freeCashflow")),
        "offering_filings_12m": offering_filings(filings, today),
    })
    si = _num(info.get("dateShortInterest"))
    if si:
        snap["short_interest_as_of"] = datetime.fromtimestamp(si, timezone.utc).date().isoformat()
    snap["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Drop absent fields; an empty filings list stays, it is itself a fact.
    return {k: v for k, v in snap.items()
            if k == "offering_filings_12m" or (v is not None and v != "")}


def fetch(symbol: str) -> dict:
    """Network fetch plus build, cached 15 minutes per symbol."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "No symbol provided."}
    hit = _CACHE.get(sym)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    import yfinance as yf
    t = yf.Ticker(sym)
    try:
        info = t.info or {}
    except Exception as e:
        log.info(f"snapshot {sym}: info failed ({e})")
        info = {}
    rows: list[dict] = []
    try:
        h = t.history(period="3mo", interval="1d", auto_adjust=False)
        for _, r in h.iterrows():
            rows.append({"high": r.get("High"), "low": r.get("Low"),
                         "close": r.get("Close"), "volume": r.get("Volume")})
    except Exception as e:
        log.info(f"snapshot {sym}: history failed ({e})")
    filings: list[dict] = []
    if (info.get("quoteType") or "EQUITY").upper() == "EQUITY":
        try:
            filings = list(t.sec_filings or [])
        except Exception as e:
            log.info(f"snapshot {sym}: filings failed ({e})")
    snap = build(sym, info, rows, filings)
    # Cache only a complete read: a snapshot built after the quote summary
    # failed has no business line, float or filings, and serving it for
    # 15 minutes turns one Yahoo hiccup into a quarter hour of thin answers.
    if info and snap.get("status") in ("ok", "not_a_stock"):
        now = time.monotonic()
        for k, (t0, _) in list(_CACHE.items()):     # copied: executor threads write too
            if now - t0 < CACHE_TTL_S:
                continue
            _CACHE.pop(k, None)
        _CACHE[sym] = (now, snap)
    return snap


# ------------------------------------------------------------- rendering

def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    sign = "-" if v < 0 else ""
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:.1f}{unit}"
    return f"{sign}${a:,.0f}"


def _count(v: float | None) -> str:
    if v is None:
        return "n/a"
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:,.0f}"


def _market_open(now: datetime | None = None) -> bool:
    """Regular US session, 9:30 to 16:00 ET on a weekday that is not a
    market holiday."""
    from zoneinfo import ZoneInfo
    et = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    try:
        from world_context import is_us_market_holiday
        if is_us_market_holiday(et.date().isoformat()):
            return False
    except Exception:
        pass
    mins = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def render(snap: dict) -> str:
    """The snapshot as a few labelled lines for the model to read."""
    s = snap
    lines = []
    head = f"{s['symbol']}: {s.get('name') or ''}".rstrip(": ")
    what = " / ".join(x for x in (s.get("sector"), s.get("industry")) if x)
    if what:
        head += f" ({what})"
    lines.append(head)
    if s.get("business"):
        lines.append(f"business: {s['business']}")
    if s.get("market_cap"):
        lines.append(f"market cap: {_money(s['market_cap'])}")
    rng = []
    if s.get("week52_low") is not None and s.get("week52_high") is not None:
        rng.append(f"52-week range ${s['week52_low']:,.2f} to ${s['week52_high']:,.2f}")
    if s.get("pct_below_52w_high") is not None:
        rng.append(f"{s['pct_below_52w_high']}% below the high")
    if s.get("pct_above_52w_low") is not None:
        rng.append(f"{s['pct_above_52w_low']}% above the low")
    if rng:
        lines.append("range: " + ", ".join(rng))
    mv = []
    if s.get("atr") is not None:
        mv.append(f"{s['atr_days']}-day average true range ${s['atr']:,.2f} ({s.get('atr_pct')}% of price)")
    if s.get("beta") is not None:
        mv.append(f"beta {s['beta']:.2f} vs the S&P 500")
    if mv:
        lines.append("movement: " + ", ".join(mv))
    if s.get("adv_shares"):
        liq = (f"{s['adv_days']}-day average volume {_count(s['adv_shares'])} shares "
               f"({_money(s.get('adv_dollars'))} a day)")
        if s.get("today_volume") is not None:
            liq += f", latest session {_count(s['today_volume'])} ({s['relative_volume']}x the average"
            liq += "; the session is still open, so this is partial)" if _market_open() else ")"
        lines.append("volume: " + liq)
    fl = []
    if s.get("float_shares"):
        fl.append(f"float {_count(s['float_shares'])} shares")
    if s.get("shares_outstanding"):
        fl.append(f"{_count(s['shares_outstanding'])} outstanding")
    if s.get("short_pct_float") is not None:
        sp = f"short interest {s['short_pct_float']}% of float"
        if s.get("days_to_cover") is not None:
            sp += f", {s['days_to_cover']:.1f} days to cover"
        if s.get("short_interest_as_of"):
            sp += f" (exchange-reported as of {s['short_interest_as_of']})"
        fl.append(sp)
    if fl:
        lines.append("shares: " + ", ".join(fl))
    bs = []
    for key, label in (("total_cash", "cash"), ("total_debt", "debt"),
                       ("operating_cashflow_ttm", "operating cash flow, trailing 12 months"),
                       ("free_cashflow_ttm", "free cash flow, trailing 12 months")):
        if s.get(key) is not None:
            bs.append(f"{label} {_money(s[key])}")
    if bs:
        lines.append("balance sheet: " + ", ".join(bs))
    offs = s.get("offering_filings_12m") or []
    if offs:
        lines.append("offering registrations filed in the last 12 months: "
                     + ", ".join(f"{o['form']} on {o['date']}" for o in offs))
    else:
        lines.append("offering registrations filed in the last 12 months: none")
    lines.append(f"(Yahoo, {s.get('as_of', '')})")
    return "\n".join(lines)
