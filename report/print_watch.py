"""Economic prints, posted at release, from the agency that publishes them.

Owner ask 2026-09-11 ("post economic prints as soon as they're
available, similar to reminders"), the morning /ask reported July's CPI
as August's because FRED lags the release by hours. The agencies do
not: the BLS API carried the August index within a minute of 8:30 and
the Fed publishes the FOMC statement to its press feed at 2:00 sharp.

Two jobs, one module:

1. The WATCH. On a day the ForexFactory calendar lists a supported US
   release, a scheduler job wakes just before the release time, polls
   the agency until the reference month appears (CPI on September 11
   reports August; anything else is not the print), posts one embed
   with actual / consensus / prior per line, and records the post so a
   restart cannot repeat it.
2. The FEED. `enrich_rows_with_agency_actuals` fills `actual` on the
   econ-calendar rows from the same agency data, ahead of the FRED
   layer, so /ask and the pulse context carry the print as soon as it
   exists and never a neighbouring month.

Supported: CPI (BLS), Employment Situation (BLS), FOMC target range
(Federal Reserve press feed). PCE and GDP need a BEA key and are the
next step; ISM has no free source and is out by owner decision.
No db import anywhere in this module.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings
from report.fred_data import month_shift, reference_period

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FED_FEED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
_UA = {"User-Agent": "omnibeta-print-watch/1.0", "Content-Type": "application/json"}

# --------------------------------------------------------------- specs

@dataclass
class Line:
    label: str
    series: str              # BLS series id ("" for FOMC)
    transform: str           # mom | yoy | m_change_k | level | range
    ff_event: str            # ForexFactory event name for consensus/prior ("" = none)
    unit: str = "%"


@dataclass
class ReleaseSpec:
    key: str
    title: str
    release_et: str          # "08:30" / "14:00"
    ff_arming_events: tuple  # any of these on today's FF calendar arms the watch
    lines: list[Line] = field(default_factory=list)


CPI = ReleaseSpec(
    key="cpi", title="CPI", release_et="08:30",
    ff_arming_events=("CPI m/m", "Core CPI m/m", "CPI y/y"),
    lines=[
        Line("CPI m/m", "CUSR0000SA0", "mom", "CPI m/m"),
        Line("CPI y/y", "CUUR0000SA0", "yoy", "CPI y/y"),
        Line("Core CPI m/m", "CUSR0000SA0L1E", "mom", "Core CPI m/m"),
        Line("Core CPI y/y", "CUUR0000SA0L1E", "yoy", "Core CPI y/y"),
    ])

JOBS = ReleaseSpec(
    key="jobs", title="Employment Situation", release_et="08:30",
    ff_arming_events=("Non Farm Payrolls", "Unemployment Rate"),
    lines=[
        Line("Non Farm Payrolls", "CES0000000001", "m_change_k", "Non Farm Payrolls", unit="K"),
        Line("Unemployment Rate", "LNS14000000", "level", "Unemployment Rate"),
        Line("Avg Hourly Earnings m/m", "CES0500000003", "mom", "Average Hourly Earnings m/m"),
        Line("Avg Hourly Earnings y/y", "CES0500000003", "yoy", ""),
    ])

FOMC = ReleaseSpec(
    key="fomc", title="FOMC decision", release_et="14:00",
    ff_arming_events=("FOMC Interest Rate Decision",),
    lines=[Line("Target range", "", "range", "FOMC Interest Rate Decision")])

SPECS = (CPI, JOBS, FOMC)

# The FEED side: which econ-calendar rows an agency series can fill.
_ROW_TO_LINE = {ln.ff_event.lower(): ln for spec in (CPI, JOBS) for ln in spec.lines if ln.ff_event}


# ----------------------------------------------------------------- BLS

_BLS_CACHE: dict = {"at": None, "key": None, "obs": None}
_BLS_CACHE_TTL_S = 10 * 60


def _bls_post(series_ids: list[str], start_year: int, end_year: int) -> dict | None:
    body = {"seriesid": sorted(series_ids), "startyear": str(start_year), "endyear": str(end_year)}
    if settings.bls_api_key:
        body["registrationkey"] = settings.bls_api_key
    req = urllib.request.Request(BLS_URL, data=json.dumps(body).encode(), headers=_UA, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"print-watch: BLS fetch failed: {e}")
        return None


def parse_bls(payload: dict) -> dict[str, list[tuple[str, float]]]:
    """{series_id: [(period 'YYYY-MM', value), ...] newest first}. Annual
    rows (M13) and unparseable values are skipped."""
    out: dict[str, list[tuple[str, float]]] = {}
    if not payload or payload.get("status") != "REQUEST_SUCCEEDED":
        return out
    for s in (payload.get("Results") or {}).get("series") or []:
        rows = []
        for o in s.get("data") or []:
            p = str(o.get("period") or "")
            if not re.match(r"^M(0[1-9]|1[0-2])$", p):
                continue
            try:
                rows.append((f"{o['year']}-{p[1:]}", float(str(o["value"]).replace(",", ""))))
            except (KeyError, ValueError, TypeError):
                continue
        rows.sort(reverse=True)
        out[s.get("seriesID", "")] = rows
    return out


def fetch_bls(series_ids: list[str], *, force: bool = False) -> dict[str, list[tuple[str, float]]]:
    """Observations for the series, two calendar years back, cached ten
    minutes across the watch and the feed (the unregistered BLS quota
    is 25 requests a day)."""
    now = datetime.utcnow()
    key = tuple(sorted(series_ids))
    c = _BLS_CACHE
    if not force and c["obs"] is not None and c["key"] == key and c["at"] \
            and (now - c["at"]).total_seconds() < _BLS_CACHE_TTL_S:
        return c["obs"]
    payload = _bls_post(list(key), now.year - 1, now.year)
    obs = parse_bls(payload) if payload else {}
    if obs:
        c.update({"at": now, "key": key, "obs": obs})
    return obs or (c["obs"] if c["key"] == key and c["obs"] else {})


def _value_at(obs: list[tuple[str, float]], period: str) -> float | None:
    for p, v in obs or []:
        if p == period:
            return v
    return None


def compute(obs: list[tuple[str, float]], transform: str, period: str) -> float | None:
    """The headline figure for `period`, computed the way the agency
    rounds it, by calendar month (never by index position: FRED has no
    October 2025 CPI and that cost the room a wrong year-over-year)."""
    cur = _value_at(obs, period)
    if cur is None:
        return None
    if transform == "level":
        return round(cur, 1)
    if transform == "m_change_k":
        prev = _value_at(obs, month_shift(period, -1))
        return None if prev is None else round(cur - prev)
    if transform == "mom":
        prev = _value_at(obs, month_shift(period, -1))
        return None if not prev else round((cur / prev - 1) * 100, 1)
    if transform == "yoy":
        base = _value_at(obs, month_shift(period, -12))
        return None if not base else round((cur / base - 1) * 100, 1)
    return None


def release_ready(spec: ReleaseSpec, obs_by_series: dict, period: str) -> bool:
    """Every line of the release has its reference-month observation."""
    return all(_value_at(obs_by_series.get(ln.series) or [], period) is not None
               for ln in spec.lines if ln.series)


# ---------------------------------------------------------------- FOMC

_RANGE_RE = re.compile(
    r"(maintain|raise|lower|increase|decrease|reduce)\s+the\s+target\s+range\s+for\s+the\s+federal\s+funds\s+rate"
    r"\s+(?:at|to|by\s+\S+\s+percentage\s+points?\s+to)\s+([0-9][0-9\-/ ]*?)\s+to\s+([0-9][0-9\-/ ]*?)\s+percent",
    re.I)
# the dash between the tallies has arrived as an en dash, a hyphen and
# a replacement character depending on the fetch's decoding
_VOTE_RE = re.compile(r"by\s+a\s+(\d+)\s*[^\d\s]\s*(\d+)\s+vote", re.I)


def _fraction(s: str) -> float | None:
    """'3-1/2' -> 3.5, '4' -> 4.0, '3-3/4' -> 3.75."""
    s = s.strip().replace(" ", "")
    m = re.match(r"^(\d+)(?:-(\d+)/(\d+))?$", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None
    whole = float(m.group(1))
    if m.group(2):
        whole += float(m.group(2)) / float(m.group(3))
    return whole


_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−�"), "-")


def parse_fomc_statement(html_text: str) -> dict | None:
    """{'action', 'low', 'high', 'vote'} from a statement's HTML, or None.
    The Fed sets '3-1/2' with a non-breaking hyphen (U+2011) and the
    vote tally with an en dash; both fold to '-' before matching."""
    txt = _html.unescape(re.sub(r"<[^>]+>", " ", html_text or ""))
    txt = re.sub(r"\s+", " ", txt.replace("\xa0", " ").translate(_DASHES))
    m = _RANGE_RE.search(txt)
    if not m:
        return None
    low, high = _fraction(m.group(2)), _fraction(m.group(3))
    if low is None or high is None:
        return None
    action = m.group(1).lower()
    action = {"increase": "raise", "decrease": "lower", "reduce": "lower"}.get(action, action)
    v = _VOTE_RE.search(txt)
    return {"action": action, "low": low, "high": high,
            "vote": f"{v.group(1)}-{v.group(2)}" if v else ""}


def _http_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def fetch_fomc_today(today_iso: str) -> dict | None:
    """Today's FOMC statement from the Fed's monetary press feed, parsed,
    or None when it has not been published yet."""
    try:
        feed = _http_text(FED_FEED_URL)
    except Exception as e:
        log.warning(f"print-watch: Fed feed fetch failed: {e}")
        return None
    for item in re.findall(r"<item>(.*?)</item>", feed, re.S):
        title = re.sub(r"<!\[CDATA\[|\]\]>", "", (re.search(r"<title>(.*?)</title>", item, re.S) or [None, ""])[1])
        if "FOMC statement" not in title:
            continue
        pub = re.sub(r"<!\[CDATA\[|\]\]>", "", (re.search(r"<pubDate>(.*?)</pubDate>", item, re.S) or [None, ""])[1]).strip()
        link = re.sub(r"<!\[CDATA\[|\]\]>", "", (re.search(r"<link>(.*?)</link>", item, re.S) or [None, ""])[1]).strip()
        try:
            pub_dt = datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
            pub_et_date = pub_dt.astimezone(_ET).date().isoformat()
        except ValueError:
            pub_et_date = ""
        if pub_et_date != today_iso or not link:
            continue
        try:
            parsed = parse_fomc_statement(_http_text(link))
        except Exception as e:
            log.warning(f"print-watch: FOMC statement fetch failed: {e}")
            return None
        if parsed:
            parsed["url"] = link
        return parsed
    return None


# --------------------------------------------------------- consensus

def _ff_rows_for_day(today_iso: str) -> list[dict]:
    from report import news_data
    try:
        rows = news_data._fetch_ff_economic_events()
    except Exception as e:
        log.warning(f"print-watch: FF calendar unavailable: {e}")
        return []
    out = []
    for r in rows:
        if r.get("country") != "US" or not r.get("time"):
            continue
        try:
            d = datetime.fromisoformat(r["time"]).replace(tzinfo=ZoneInfo("UTC")).astimezone(_ET).date().isoformat()
        except ValueError:
            continue
        if d == today_iso:
            out.append(r)
    return out


def due_releases(today_iso: str, ff_rows: list[dict], release_et: str | None = None) -> list[ReleaseSpec]:
    names = {str(r.get("event") or "").lower() for r in ff_rows}
    out = []
    for spec in SPECS:
        if release_et and spec.release_et != release_et:
            continue
        if any(ev.lower() in names for ev in spec.ff_arming_events):
            out.append(spec)
    return out


def _fmt(value: float | None, unit: str, transform: str) -> str:
    if value is None:
        return "—"
    if unit == "K":
        return f"{value:+,.0f}K"
    if transform in ("mom",):
        return f"{value:+.1f}%"
    return f"{value:.1f}%"


def _ff_value(ff_rows: list[dict], event: str, key: str) -> float | None:
    for r in ff_rows:
        if str(r.get("event") or "").lower() == event.lower():
            v = r.get(key)
            return float(v) if isinstance(v, (int, float)) else None
    return None


def build_lines(spec: ReleaseSpec, obs_by_series: dict, period: str, ff_rows: list[dict]) -> list[str]:
    """One rendered line per series: actual, consensus, prior."""
    out = []
    for ln in spec.lines:
        actual = compute(obs_by_series.get(ln.series) or [], ln.transform, period)
        cons = _ff_value(ff_rows, ln.ff_event, "estimate") if ln.ff_event else None
        prior = _ff_value(ff_rows, ln.ff_event, "prev") if ln.ff_event else None
        if prior is None and ln.series:
            prior = compute(obs_by_series.get(ln.series) or [], ln.transform, month_shift(period, -1))
        bits = [f"**{ln.label}** {_fmt(actual, ln.unit, ln.transform)}"]
        if cons is not None:
            bits.append(f"consensus {_fmt(cons, ln.unit, ln.transform)}")
        if prior is not None:
            bits.append(f"prior {_fmt(prior, ln.unit, ln.transform)}")
        out.append(" · ".join(bits))
    return out


def fomc_lines(parsed: dict, ff_rows: list[dict]) -> list[str]:
    verb = {"maintain": "holds", "raise": "hikes", "lower": "cuts"}.get(parsed["action"], parsed["action"])
    line = f"**Fed {verb}** · target range {parsed['low']:.2f}%–{parsed['high']:.2f}%"
    cons = _ff_value(ff_rows, "FOMC Interest Rate Decision", "estimate")
    if cons is not None:
        line += f" · consensus upper bound {cons:.2f}%"
    if parsed.get("vote"):
        line += f" · vote {parsed['vote']}"
    return [line]


def period_label(period: str) -> str:
    return datetime.strptime(period + "-01", "%Y-%m-%d").strftime("%B %Y")


# ---------------------------------------------------------------- dedupe

def _ledger_path(today_iso: str) -> Path:
    base = Path(settings.db_path).resolve().parent / "print-alerts"
    return base / f"{today_iso}.json"


def already_posted(today_iso: str, key: str) -> bool:
    try:
        return key in json.loads(_ledger_path(today_iso).read_text(encoding="utf-8"))
    except Exception:
        return False


def mark_posted(today_iso: str, key: str, lines: list[str]) -> None:
    p = _ledger_path(today_iso)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        d = {}
    d[key] = {"at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "lines": lines}
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")


# ------------------------------------------------------------- the job

POLL_S_WITH_KEY = 10
POLL_S_NO_KEY = 30          # 25 unregistered requests a day; ~20 per watch
MAX_WAIT_S = 12 * 60


async def _post(bot, title: str, lines: list[str], footer: str) -> bool:
    import discord
    from discord_bot.sender import _send_with_retry
    raw = (settings.print_alert_channel_id or settings.reminder_channel_id or "").strip()
    if not raw:
        return False
    cid = int(raw)
    channel = bot.get_channel(cid)
    if channel is None:
        channel = await bot.fetch_channel(cid)
    embed = discord.Embed(title=title, description="\n".join(lines), color=0xE5A93F)
    embed.set_footer(text=footer)
    ok, err = await _send_with_retry(lambda emb=embed: channel.send(embed=emb),
                                     label=f"print-watch {title}")
    if not ok:
        log.warning(f"print-watch: post failed for {title!r}: {err}")
    return ok


async def print_watch_job(bot=None, release_et: str = "08:30") -> None:
    """Armed a minute before `release_et` on every weekday; exits at
    once when nothing supported is scheduled today."""
    if not (settings.print_alert_channel_id or settings.reminder_channel_id) or bot is None:
        return
    now = datetime.now(_ET)
    today = now.date().isoformat()
    ff_rows = _ff_rows_for_day(today)
    due = [s for s in due_releases(today, ff_rows, release_et) if not already_posted(today, s.key)]
    if not due:
        return
    log.info(f"print-watch: armed for {[s.key for s in due]} at {release_et} ET")
    hh, mm = (int(x) for x in release_et.split(":"))
    release_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    wait = (release_at - datetime.now(_ET)).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    period = reference_period(today)
    poll = POLL_S_WITH_KEY if settings.bls_api_key else POLL_S_NO_KEY
    deadline = datetime.now(_ET) + timedelta(seconds=MAX_WAIT_S)
    pending = list(due)
    while pending and datetime.now(_ET) < deadline:
        for spec in list(pending):
            try:
                if spec.key == "fomc":
                    parsed = await asyncio.to_thread(fetch_fomc_today, today)
                    if not parsed:
                        continue
                    lines = fomc_lines(parsed, ff_rows)
                    title = f"FOMC decision · {now.strftime('%B %-d') if os.name != 'nt' else now.strftime('%B %d')}"
                else:
                    series = [ln.series for ln in spec.lines if ln.series]
                    obs = await asyncio.to_thread(fetch_bls, series, force=True)
                    if not release_ready(spec, obs, period):
                        continue
                    lines = build_lines(spec, obs, period, ff_rows)
                    title = f"{spec.title} · {period_label(period)}"
                footer = f"released {release_et} ET · {'BLS' if spec.key != 'fomc' else 'Federal Reserve'} · consensus and prior from the calendar feed"
                if await _post(bot, title, lines, footer):
                    mark_posted(today, spec.key, lines)
                    log.info(f"print-watch: posted {spec.key} for {period}")
                pending.remove(spec)
            except Exception as e:
                log.warning(f"print-watch: {spec.key} attempt failed: {e}")
        if pending:
            await asyncio.sleep(poll)
    for spec in pending:
        log.warning(f"print-watch: {spec.key} not published within {MAX_WAIT_S // 60} min of {release_et} ET")


# ------------------------------------------------------------ the feed

def enrich_rows_with_agency_actuals(rows: list[dict]) -> list[dict]:
    """Fill `actual` on past US econ rows straight from the BLS series,
    before the FRED layer runs. Same reference-month rule: the
    observation must be the calendar month before the row date, or the
    row is left for FRED (which applies the same rule) and reads
    past_no_data. Never raises."""
    try:
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        wanted = [r for r in rows if r.get("country") == "US" and r.get("actual") is None
                  and (r.get("time") or "") and r["time"] <= now_iso
                  and str(r.get("event") or "").lower() in _ROW_TO_LINE]
        if not wanted:
            return rows
        series = sorted({_ROW_TO_LINE[str(r["event"]).lower()].series for r in wanted})
        obs_by = fetch_bls(series)
        if not obs_by:
            return rows
        for r in wanted:
            ln = _ROW_TO_LINE[str(r["event"]).lower()]
            period = reference_period(r["time"])
            value = compute(obs_by.get(ln.series) or [], ln.transform, period)
            if value is None:
                continue
            r["actual"] = value
            r["actual_period"] = period
            r["actual_source"] = f"bls:{ln.series}"
            if not r.get("unit"):
                r["unit"] = ln.unit
    except Exception as e:
        log.warning(f"print-watch: agency enrichment failed (non-fatal): {e}")
    return rows
