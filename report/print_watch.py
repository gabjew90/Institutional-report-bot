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
   reports August; anything else is not the print), posts one alert
   built with no model call (2026-10-28: report/print_data.py,
   print_verdict.py, print_templates.py, print_render.py; the verdict
   line is the message text a phone shows), and records the post so a
   restart cannot repeat it.
2. The FEED. `enrich_rows_with_agency_actuals` fills `actual` on the
   econ-calendar rows from the same agency data, ahead of the FRED
   layer, so /ask and the pulse context carry the print as soon as it
   exists and never a neighbouring month.

Supported: CPI (BLS), Employment Situation (BLS), FOMC target range
(Federal Reserve press feed), PCE (BEA, needs BEA_API_KEY, set
2026-09-25). GDP uses the same BEA key and is not yet wired; ISM has no
free source and is out by owner decision.

Arming (2026-09-25): the ForexFactory feed OR the agencies' published
schedule (OFFICIAL_RELEASES) listing the release today.
No db import anywhere in this module.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings
from report.fred_data import month_shift, reference_period
# The agencies' published release schedule (2026-10-05: moved to
# world_context so the omni-calendar reads the same table). Keys with no
# ReleaseSpec here, the ISM reports, are ignored by the watch.
from world_context import OFFICIAL_RELEASES

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FED_FEED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
_UA = {"User-Agent": "omnibeta-print-watch/1.0", "Content-Type": "application/json"}

# --------------------------------------------------------------- specs

@dataclass
class Line:
    label: str
    series: str              # agency series id ("" for FOMC)
    transform: str           # mom | yoy | m_change_k | level | raw
    ff_event: str            # ForexFactory event name for consensus/prior ("" = none)
    unit: str = "%"
    source: str = "bls"      # bls | bea
    table: str = ""          # BEA NIPA table, "" means the release default (BEA_PCE_TABLE)
    display: str = ""        # reader-facing name, defaults to label
    optional: bool = False   # not needed to post, dropped when the agency has no value
    signed: bool | None = None  # None: changes (mom, m_change_k) carry a sign, rates and levels do not
    hidden: bool = False     # an input to a computed value (the unrounded jobless rate), never shown


@dataclass
class ReleaseSpec:
    key: str
    title: str
    release_et: str          # "08:30" / "14:00"
    ff_arming_events: tuple  # any of these on today's FF calendar arms the watch
    lines: list[Line] = field(default_factory=list)
    agency: str = "BLS"      # footer credit
    name: str = ""           # the alert's short name: "Sep CPI: Hotter than expected"


CPI = ReleaseSpec(
    key="cpi", title="CPI", release_et="08:30", name="CPI",
    ff_arming_events=("CPI m/m", "Core CPI m/m", "CPI y/y"),
    lines=[
        Line("Core CPI m/m", "CUSR0000SA0L1E", "mom", "Core CPI m/m", display="Core CPI MoM"),
        Line("Core CPI y/y", "CUUR0000SA0L1E", "yoy", "Core CPI y/y", display="Core CPI YoY"),
        Line("CPI m/m", "CUSR0000SA0", "mom", "CPI m/m", display="Headline CPI MoM"),
        Line("CPI y/y", "CUUR0000SA0", "yoy", "CPI y/y", display="Headline CPI YoY"),
        Line("Shelter m/m", "CUSR0000SAH1", "mom", "", display="Shelter", optional=True),
        Line("Food m/m", "CUSR0000SAF1", "mom", "", display="Food", optional=True),
        Line("Energy m/m", "CUSR0000SA0E", "mom", "", display="Energy", optional=True),
    ])

JOBS = ReleaseSpec(
    key="jobs", title="Employment Situation", release_et="08:30", name="Jobs",
    ff_arming_events=("Non Farm Payrolls", "Unemployment Rate"),
    lines=[
        Line("Non Farm Payrolls", "CES0000000001", "m_change_k", "Non Farm Payrolls", unit="K", display="Nonfarm payrolls"),
        Line("Unemployment Rate", "LNS14000000", "level", "Unemployment Rate", display="Unemployment"),
        Line("Avg Hourly Earnings m/m", "CES0500000003", "mom", "Average Hourly Earnings m/m", display="Avg hourly earnings MoM"),
        Line("Avg Hourly Earnings y/y", "CES0500000003", "yoy", "", display="Avg hourly earnings YoY", signed=True),
        Line("Participation Rate", "LNS11300000", "level", "", display="Participation", optional=True),
        Line("Avg Weekly Hours", "CES0500000002", "level", "", unit="", display="Weekly hours", optional=True),
        # the unemployed and the labor force, for the jobless rate before rounding
        Line("Unemployed", "LNS13000000", "raw", "", unit="", optional=True, hidden=True),
        Line("Labor Force", "LNS11000000", "raw", "", unit="", optional=True, hidden=True),
    ])

FOMC = ReleaseSpec(
    key="fomc", title="FOMC decision", release_et="14:00", name="FOMC",
    ff_arming_events=("FOMC Interest Rate Decision",),
    lines=[Line("Target range", "", "range", "FOMC Interest Rate Decision")],
    agency="Federal Reserve")

# PCE (owner ask 2026-09-17): the Fed's own inflation gauge, from BEA's
# NIPA table 2.8.4 (monthly price indexes by major type of product).
# Series codes: DPCERG is the PCE price index, DPCCRG the index excluding
# food and energy. Needs BEA_API_KEY (free, registered per user); with
# no key the release is never armed and the feed skips it. The calendar
# feed lists only the core m/m line, so headline and y/y lines carry
# no consensus.
# T20600 is Personal Income and Its Disposition (A065RC personal income,
# DPCERC nominal PCE, A072RC saving rate). T20806 is real PCE by type of
# product (DPCERX). If a code is wrong, parse_bea logs what the table
# carries on the first live run and the line is dropped from the body,
# while the core lines still post.
PCE = ReleaseSpec(
    key="pce", title="PCE price index", release_et="08:30", name="PCE",
    ff_arming_events=("Core PCE Price Index m/m",),
    lines=[
        Line("Core PCE m/m", "DPCCRG", "mom", "Core PCE Price Index m/m", source="bea", display="Core PCE MoM"),
        Line("Core PCE y/y", "DPCCRG", "yoy", "", source="bea", display="Core PCE YoY"),
        Line("PCE m/m", "DPCERG", "mom", "", source="bea", display="Headline PCE MoM"),
        Line("PCE y/y", "DPCERG", "yoy", "", source="bea", display="Headline PCE YoY"),
        Line("Real Consumer Spending m/m", "DPCERX", "mom", "", source="bea", table="T20806",
             display="Real consumer spending", optional=True),
        Line("Personal Spending m/m", "DPCERC", "mom", "Personal Spending m/m", source="bea", table="T20600",
             display="Personal spending", optional=True),
        Line("Personal Income m/m", "A065RC", "mom", "Personal Income m/m", source="bea", table="T20600",
             display="Personal income", optional=True),
        Line("Saving Rate", "A072RC", "level", "", source="bea", table="T20600",
             display="Saving rate", optional=True),
    ],
    agency="BEA")

# PPI (2026-10-18 audit A3/P1): on 10/15 the bot told the room PPI was not
# out two minutes after it printed, because nothing filled the calendar
# row's actual. Final demand SA for m/m, NSA for y/y, the way BLS headlines
# them; core is final demand less foods and energy (WPSFD49104 +0.7% for
# September 2026, matching the release table), the same measure every
# release. WPSFD49116 also leaves out trade services (retail and wholesale
# margins), the steadier measure economists quote beside it.
PPI = ReleaseSpec(
    key="ppi", title="PPI", release_et="08:30", name="PPI",
    ff_arming_events=("PPI m/m", "Core PPI m/m"),
    lines=[
        Line("Core PPI m/m", "WPSFD49104", "mom", "Core PPI m/m", display="Core PPI MoM"),
        Line("Core PPI y/y", "WPUFD49104", "yoy", "Core PPI y/y", display="Core PPI YoY"),
        Line("PPI m/m", "WPSFD4", "mom", "PPI m/m", display="Headline PPI MoM"),
        Line("PPI y/y", "WPUFD4", "yoy", "PPI y/y", display="Headline PPI YoY"),
        Line("PPI ex trade m/m", "WPSFD49116", "mom", "", display="PPI ex food, energy, trade MoM", optional=True),
    ])

SPECS = (CPI, JOBS, FOMC, PCE, PPI)

# The FEED side: which econ-calendar rows an agency series can fill.
_ROW_TO_LINE = {ln.ff_event.lower(): ln for spec in (CPI, JOBS, PCE, PPI) for ln in spec.lines if ln.ff_event}


def source_available(source: str) -> bool:
    return source != "bea" or bool((settings.bea_api_key or "").strip())


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


# ----------------------------------------------------------------- BEA

BEA_URL = "https://apps.bea.gov/api/data/"
BEA_PCE_TABLE = "T20804"
_BEA_CACHE: dict[tuple, dict] = {}   # (table, series) -> {"at", "obs"}


def _bea_get(table: str, years: list[int]) -> dict | None:
    key = (settings.bea_api_key or "").strip()
    if not key:
        return None
    q = urllib.parse.urlencode({
        "UserID": key, "method": "GetData", "DataSetName": "NIPA",
        "TableName": table, "Frequency": "M",
        "Year": ",".join(str(y) for y in years), "ResultFormat": "JSON"})
    req = urllib.request.Request(f"{BEA_URL}?{q}", headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"print-watch: BEA fetch failed: {e}")
        return None


def parse_bea(payload: dict, series_codes: list[str] | None = None) -> dict[str, list[tuple[str, float]]]:
    """{series_code: [(period 'YYYY-MM', value), ...] newest first} from a
    NIPA GetData response (TimePeriod '2026M08', DataValue '126.512').
    An API error or a missing series is logged with what the table did
    carry, so a wrong series code shows up on the first live run."""
    out: dict[str, list[tuple[str, float]]] = {}
    results = ((payload or {}).get("BEAAPI") or {}).get("Results") or {}
    if isinstance(results, dict) and results.get("Error"):
        log.warning(f"print-watch: BEA error: {results['Error']}")
        return out
    for row in (results.get("Data") if isinstance(results, dict) else None) or []:
        code = str(row.get("SeriesCode") or "")
        m = re.match(r"^(\d{4})M(0[1-9]|1[0-2])$", str(row.get("TimePeriod") or ""))
        if not code or not m:
            continue
        try:
            value = float(str(row.get("DataValue") or "").replace(",", ""))
        except ValueError:
            continue
        out.setdefault(code, []).append((f"{m.group(1)}-{m.group(2)}", value))
    for code in out:
        out[code].sort(reverse=True)
    missing = [c for c in (series_codes or []) if c not in out]
    if missing and out:
        seen = sorted({(str(r.get("SeriesCode")), str(r.get("LineDescription")))
                       for r in results.get("Data") or [] if isinstance(r, dict)})
        log.warning(f"print-watch: BEA table lacks {missing}; it carries {seen[:40]}")
    return out


def fetch_bea(series_codes: list[str], *, table: str = BEA_PCE_TABLE, force: bool = False) -> dict[str, list[tuple[str, float]]]:
    """Observations for the series in one NIPA table, two calendar years
    back, cached ten minutes per (table, series) like the BLS fetch. Empty
    without a BEA key."""
    now = datetime.utcnow()
    key = (table, tuple(sorted(series_codes)))
    c = _BEA_CACHE.setdefault(key, {"at": None, "obs": None})
    if not force and c["obs"] is not None and c["at"] \
            and (now - c["at"]).total_seconds() < _BLS_CACHE_TTL_S:
        return c["obs"]
    payload = _bea_get(table, [now.year - 1, now.year])
    obs = parse_bea(payload, list(key[1])) if payload else {}
    obs = {k: v for k, v in obs.items() if k in key[1]}
    if obs:
        c.update({"at": now, "obs": obs})
    return obs or (c["obs"] or {})


def fetch_observations(lines: list, *, force: bool = False) -> dict[str, list[tuple[str, float]]]:
    """Observations for every series a release's lines need, from
    whichever agency and table each line names."""
    out: dict[str, list[tuple[str, float]]] = {}
    groups: dict[tuple[str, str], list[str]] = {}
    for ln in lines:
        if ln.series:
            # only BEA lines carry a table, so a BLS line never splits the BLS request
            table = (ln.table or BEA_PCE_TABLE) if ln.source == "bea" else ""
            groups.setdefault((ln.source, table), []).append(ln.series)
    for (source, table), series in groups.items():
        if not source_available(source):
            continue
        if source == "bea":
            out.update(fetch_bea(sorted(set(series)), table=table, force=force))
        else:
            out.update(fetch_bls(sorted(set(series)), force=force))
    return out


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
    if transform == "raw":
        return cur
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


def unrounded_change(obs: list[tuple[str, float]], period: str) -> float | None:
    """The month-on-month change in percent before rounding, from the
    index levels the agency publishes: the figure behind a headline 0.3%."""
    cur, prev = _value_at(obs, period), _value_at(obs, month_shift(period, -1))
    return None if cur is None or not prev else (cur / prev - 1) * 100


def release_ready(spec: ReleaseSpec, obs_by_series: dict, period: str) -> bool:
    """Every required line of the release has its reference-month
    observation. Optional lines never hold a post."""
    return all(_value_at(obs_by_series.get(ln.series) or [], period) is not None
               for ln in spec.lines if ln.series and not ln.optional)


# ---------------------------------------------------------------- FOMC

_RANGE_RE = re.compile(
    r"(?P<action>maintain|raise|lower|increase|decrease|reduce)\s+the\s+target\s+range\s+for\s+the\s+federal\s+funds\s+rate"
    r"\s+(?:at|to|by\s+(?P<size>\S+)\s+percentage\s+points?\s+to)\s+(?P<low>[0-9][0-9\-/ ]*?)\s+to\s+(?P<high>[0-9][0-9\-/ ]*?)\s+percent",
    re.I)
# the dash between the tallies has arrived as an en dash, a hyphen and
# a replacement character depending on the fetch's decoding
_VOTE_RE = re.compile(r"by\s+a\s+(\d+)\s*[^\d\s]\s*(\d+)\s+vote", re.I)


def _fraction(s: str) -> float | None:
    """'3-1/2' -> 3.5, '4' -> 4.0, '3-3/4' -> 3.75, '1/4' -> 0.25 (a move's size)."""
    s = s.strip().replace(" ", "")
    bare = re.match(r"^(\d+)/(\d+)$", s)
    if bare:
        return float(bare.group(1)) / float(bare.group(2))
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

# The statement body sits between the release line ("For release at
# 2:00 p.m. EDT", followed by a "Share" button label) and the press
# contact. Some statements open with "Recent indicators" instead.
_STATEMENT_START_RE = re.compile(r"For release at \d{1,2}:\d{2} [ap]\.m\. [A-Z]{2,4}\s*(?:Share\s+)?", re.I)
_STATEMENT_END_MARKERS = ("For media inquiries", "Implementation Note")


def _statement_body(txt: str, range_at: int) -> str:
    """The statement alone, cut out of the whole page's text. `range_at`
    is where the target-range sentence sits, which is always inside the
    statement, so the start is the release line or the "Recent
    indicators" opener before it, else that sentence's own start."""
    start = None
    for m in _STATEMENT_START_RE.finditer(txt, 0, range_at + 1):
        start = m.end()
    if start is None:
        i = txt.rfind("Recent indicators", 0, range_at)
        start = i if i >= 0 else txt.rfind(". ", 0, range_at) + 2
    end = len(txt)
    for marker in _STATEMENT_END_MARKERS:
        j = txt.find(marker, start)
        if j >= 0:
            end = j
            break
    # 2026-10-28: the stored statement opened with "--> --> -->", the tail
    # of HTML comments whose bodies held a "<" the tag strip cut at
    return re.sub(r"^(?:-->\s*)+", "", txt[start:end].strip())


def parse_fomc_statement(html_text: str) -> dict | None:
    """{'action', 'low', 'high', 'size', 'vote', 'text'} from a statement's
    HTML, or None. 'action' is raise / maintain / lower, 'size' the move in
    percentage points when the statement states it (None on a hold). The
    Fed sets '3-1/2' with a non-breaking hyphen (U+2011) and the vote tally
    with an en dash; both fold to '-' before matching. 'text' is the
    statement body alone (no page navigation or footer), kept in the
    ledger so the next decision can show the sentences that changed."""
    raw = re.sub(r"<!--.*?-->", " ", html_text or "", flags=re.S)
    txt = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    txt = re.sub(r"\s+", " ", txt.replace("﻿", "").replace("\xa0", " ").translate(_DASHES)).strip()
    m = _RANGE_RE.search(txt)
    if not m:
        return None
    low, high = _fraction(m.group("low")), _fraction(m.group("high"))
    if low is None or high is None:
        return None
    action = m.group("action").lower()
    action = {"increase": "raise", "decrease": "lower", "reduce": "lower"}.get(action, action)
    v = _VOTE_RE.search(txt)
    return {"action": action, "low": low, "high": high,
            "size": _fraction(m.group("size")) if m.group("size") else None,
            "vote": f"{v.group(1)}-{v.group(2)}" if v else "",
            "text": _statement_body(txt, m.start())}


# A dissent names the members and the move they preferred: "Voting
# against the monetary policy action were Lisa D. Cook and Beth M.
# Hammack, who preferred to raise the target range for the federal funds
# rate by 1/4 percentage point at this meeting." Several groups chain with
# ", and": "... at this meeting, and Michelle W. Bowman, who preferred no
# change".
_MOVE_RANK = {"cut": -1, "hold": 0, "hike": 1}
FED_ACTION = {"raise": "hike", "maintain": "hold", "lower": "cut"}


def _last_names(names: str) -> list[str]:
    parts = [p.strip() for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", names) if p.strip()]
    return [p.split()[-1] for p in parts]


def _preferred_move(pref: str) -> tuple[str, float | None]:
    p = pref.lower()
    move = "hike" if "raise" in p else "cut" if "lower" in p else "hold"
    size = re.search(r"by\s+(\S+)\s+percentage\s+point", p)
    return move, (_fraction(size.group(1)) if size else None)


def parse_dissents(text: str) -> list[dict]:
    """[{'names': ['Cook', 'Hammack'], 'move': 'hike', 'size': 0.25}] from
    the statement's "Voting against" sentence; [] when the vote was
    unanimous or the sentence is missing."""
    s = next((x for x in _sentences(text or "") if x.startswith("Voting against")), "")
    m = re.match(r"Voting against .*? (?:were|was) (.*)$", s)
    if not m:
        return []
    parts = re.split(r",?\s*who preferred\s+", m.group(1).rstrip("."))
    groups, names = [], parts[0]
    for chunk in parts[1:]:
        nxt = re.match(r"(.*?)(?:[,;]\s*and\s+)([A-Z].*)$", chunk)
        pref, following = (nxt.group(1), nxt.group(2)) if nxt else (chunk, "")
        move, size = _preferred_move(pref)
        if _last_names(names):
            groups.append({"names": _last_names(names), "move": move, "size": size})
        names = following
    return groups


def _signed_move(move: str, size: float | None) -> float:
    """The change in points a move stands for; a hike or cut of unstated
    size counts as a quarter point."""
    return 0.0 if move == "hold" else _MOVE_RANK[move] * (size or 0.25)


def dissent_net(groups: list[dict], action: str, size: float | None = None) -> int:
    """Members who wanted a higher rate than the decision minus those who
    wanted a lower one. `action` is hike / hold / cut and `size` its move,
    so a dissent for a bigger cut counts as wanting a lower rate."""
    base = _signed_move(action, size)
    total = 0
    for g in groups:
        want = _signed_move(g["move"], g.get("size"))
        total += len(g["names"]) * ((want > base) - (want < base))
    return total


# Periods that do not end a sentence: "U.S.", "p.m.", "a.m." and a
# single-capital initial ("Stephen I. Miran").
_ABBREV_RE = re.compile(r"\b(?:U\.S\.|[ap]\.m\.|[A-Z]\.)(?=\s)")
_DOT_HOLD = "\x00"


def _sentences(text: str) -> list[str]:
    held = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", _DOT_HOLD), (text or "").strip())
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", held)
    return [s.replace(_DOT_HOLD, ".").strip() for s in parts if s.strip()]


# The decision and the vote get their own lines, so their sentences are
# never shown as a wording change.
_DECISION_SENTENCE_RE = re.compile(r"target range for the federal funds rate|\bvot(?:e|ing)\b", re.I)


def statement_diff(prev_text: str, cur_text: str, limit: int = 2) -> list[tuple[str | None, str | None]]:
    """(was, now) pairs for the sentences that changed since the previous
    statement, in order, up to `limit`: a rewritten sentence is a pair, a
    new one has no `was`, a dropped one no `now`. Empty without a previous
    statement."""
    import difflib
    if not prev_text or not cur_text:
        return []
    before = [s for s in _sentences(prev_text) if not _DECISION_SENTENCE_RE.search(s)]
    after = [s for s in _sentences(cur_text) if not _DECISION_SENTENCE_RE.search(s)]
    out: list[tuple[str | None, str | None]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        olds, news = before[i1:i2], after[j1:j2]
        for k in range(max(len(olds), len(news))):
            out.append((olds[k] if k < len(olds) else None, news[k] if k < len(news) else None))
    return out[:limit]


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


def official_calendar_exhausted(today_iso: str) -> bool:
    return today_iso > max(OFFICIAL_RELEASES)


def ff_lists(spec: ReleaseSpec, ff_rows: list[dict]) -> bool:
    names = {str(r.get("event") or "").lower() for r in ff_rows}
    return any(ev.lower() in names for ev in spec.ff_arming_events)


def due_releases(today_iso: str, ff_rows: list[dict], release_et: str | None = None) -> list[ReleaseSpec]:
    """Armed when EITHER the ForexFactory feed or the agencies' published
    schedule (OFFICIAL_RELEASES) lists the release today."""
    official = OFFICIAL_RELEASES.get(today_iso, ())
    out = []
    for spec in SPECS:
        if release_et and spec.release_et != release_et:
            continue
        if not (ff_lists(spec, ff_rows) or spec.key in official):
            continue
        if not all(source_available(ln.source) for ln in spec.lines if ln.series):
            log.info(f"print-watch: {spec.key} is on today's calendar but its agency key is not set; skipped")
            continue
        out.append(spec)
    return out


def _fmt(value: float | None, unit: str, transform: str, signed: bool | None = None) -> str:
    """A value at the agency's precision. Changes carry a sign (+0.3%,
    +142K), rates and levels do not (2.6%, 4.2%), zero never does."""
    if value is None:
        return ""
    if unit == "K":
        return f"{value:+,.0f}K"
    if unit == "":
        return f"{value:.1f}"
    if round(value, 1) == 0:
        return "0.0%"
    if signed is None:
        signed = transform in ("mom", "m_change_k")
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def _ff_value(ff_rows: list[dict], event: str, key: str) -> float | None:
    for r in ff_rows:
        if str(r.get("event") or "").lower() == event.lower():
            v = r.get(key)
            return float(v) if isinstance(v, (int, float)) else None
    return None


def build_rows(spec: ReleaseSpec, obs_by_series: dict, period: str, ff_rows: list[dict]) -> list[dict]:
    """One row per series: label, formatted actual / consensus / prior
    (consensus and prior are None when the feed has none), the verdict
    against consensus and the reference month, which the ledger keeps so
    next month's revision line knows which vintage it posted."""
    out = []
    for ln in spec.lines:
        actual = compute(obs_by_series.get(ln.series) or [], ln.transform, period)
        cons = _ff_value(ff_rows, ln.ff_event, "estimate") if ln.ff_event else None
        prior = _ff_value(ff_rows, ln.ff_event, "prev") if ln.ff_event else None
        if prior is None and ln.series:
            prior = compute(obs_by_series.get(ln.series) or [], ln.transform, month_shift(period, -1))
        if ln.optional and actual is None:
            log.info(f"print-watch: {spec.key} extra line {ln.label} has no {period} value, dropped")
            continue
        out.append(make_row(ln, period, actual, cons, prior))
    return out


def make_row(ln: Line, period: str, actual: float | None, cons: float | None = None,
             prior: float | None = None) -> dict:
    """One series as the alert reads it: values, their formatted text and
    the verdict against consensus."""
    def fmt(v):
        return _fmt(v, ln.unit, ln.transform, ln.signed) if v is not None else None
    return {
        "label": ln.label, "display": ln.display or ln.label,
        "optional": ln.optional, "hidden": ln.hidden,
        "unit": ln.unit, "transform": ln.transform, "period": period,
        "actual": fmt(actual), "actual_value": actual,
        "consensus": fmt(cons), "consensus_value": cons,
        "prior": fmt(prior), "prior_value": prior,
        "verdict": verdict(actual, cons, ln.unit, ln.transform),
    }


def verdict(actual: float | None, cons: float | None, unit: str, transform: str) -> str:
    """'above' / 'below' / 'in line' against consensus at the displayed
    precision, '' when either side is missing. The forecast feed has one
    decimal, so a +0.34% that publishes as +0.3% against +0.3% is in line."""
    if actual is None or cons is None:
        return ""
    a, c = _fmt(actual, unit, transform), _fmt(cons, unit, transform)
    if a == c:
        return "in line"
    return "above" if actual > cons else "below"


def annualized_3m(series: list[tuple[str, float]], period: str) -> float | None:
    """The last three monthly index changes compounded to a year, one
    decimal, or None when a month is missing."""
    idx = {p: v for p, v in series or []}
    end, start = idx.get(period), idx.get(month_shift(period, -3))
    if not end or not start:
        return None
    return round(((end / start) ** 4 - 1) * 100, 1)


# FRED twins of the agency series whose revisions the post reports.
_FRED_TWIN = {"CES0000000001": "PAYEMS"}


def _revised_line(spec: ReleaseSpec) -> Line | None:
    ln = next((x for x in spec.lines if x.transform == "m_change_k"), None)
    return ln if ln and ln.series in _FRED_TWIN else None


def payroll_vintage(spec: ReleaseSpec, period: str, today_iso: str) -> list[tuple[str, float]] | None:
    """The payroll levels as FRED's archive of past values (ALFRED) showed
    them the day before the release: the first prints the release then
    revises. Known before 8:30, so the job fetches it while it waits for
    the release, off the posting path. None when FRED cannot answer."""
    ln = _revised_line(spec)
    if ln is None:
        return None
    from report.fred_data import _fred_get
    asof = (datetime.fromisoformat(today_iso) - timedelta(days=1)).date().isoformat()
    data = _fred_get("series/observations", {
        "series_id": _FRED_TWIN[ln.series], "realtime_start": asof, "realtime_end": asof,
        "observation_start": f"{month_shift(period, -3)}-01"})
    if not data:
        return None
    return [(o["date"][:7], float(o["value"])) for o in data.get("observations", [])
            if o.get("value") not in (None, ".")]


# ---------------------------------------------------------------- dedupe

def _ledger_dir() -> Path:
    return Path(settings.db_path).resolve().parent / "print-alerts"


def _ledger_path(today_iso: str) -> Path:
    return _ledger_dir() / f"{today_iso}.json"


def already_posted(today_iso: str, key: str) -> bool:
    try:
        return key in json.loads(_ledger_path(today_iso).read_text(encoding="utf-8"))
    except Exception:
        return False


def mark_posted(today_iso: str, key: str, lines: list[str], rows: list[dict] | None = None,
                statement: str | None = None, extra: dict | None = None) -> None:
    """Record the print: the body lines, the rows with their numeric
    actuals (next month's revision line compares against them), for the
    FOMC the statement text (next meeting's changes diff against it), and
    `extra` (the message text, the verdict, the FOMC action and dissents)."""
    p = _ledger_path(today_iso)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        d = {}
    entry: dict = {"at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "lines": lines}
    if rows is not None:
        entry["rows"] = [{k: r.get(k) for k in ("label", "actual_value", "period")} for r in rows]
    if statement:
        entry["statement"] = statement[:8000]
    entry.update(extra or {})
    d[key] = entry
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")


def _ledger_days(before: str) -> list[str]:
    try:
        return sorted((f.stem for f in _ledger_dir().glob("*.json") if f.stem < before), reverse=True)
    except OSError:
        return []


def _ledger_entries(key: str, before: str):
    for day in _ledger_days(before):
        try:
            d = json.loads((_ledger_dir() / f"{day}.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get(key), dict):
            yield d[key]


def previous_post(key: str, before: str) -> dict | None:
    """The most recent ledger entry for `key` on a day before `before`."""
    return next(_ledger_entries(key, before), None)


def fomc_history(before: str, n: int = 4) -> list[dict]:
    """The last `n` FOMC decisions in the ledger before `before`, newest first."""
    out = []
    for entry in _ledger_entries("fomc", before):
        out.append(entry)
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------- the job

POLL_S_WITH_KEY = 10
# The first two minutes after the release time poll faster: with no model
# call left on the path (2026-10-28), the poll interval is the delay.
POLL_S_FAST = 5
FAST_WINDOW_S = 120
POLL_S_NO_KEY = 30          # 25 unregistered requests a day; ~20 per watch
MAX_WAIT_S = 12 * 60
# BEA's API has not yet been watched through a live release, and an
# API table that lags the 8:30 press release would be abandoned at 12
# minutes (2026-09-25 review). BLS stays at 12: unregistered BLS allows
# 25 requests a day and the 30 s poll spends about 20 of them.
MAX_WAIT_S_BY_AGENCY = {"BEA": 30 * 60}


def alert_channel_ids() -> list[int]:
    """PRINT_ALERT_CHANNEL_ID as a comma-separated list (2026-09-17: the
    owner wants the print in the test channel beside the room), falling
    back to REMINDER_CHANNEL_ID. Bad entries are skipped, not fatal."""
    raw = (settings.print_alert_channel_id or settings.reminder_channel_id or "")
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return out


# The /ask embeds' green (discord_bot.bot._build_ask_embeds), so a print
# reads as the bot's own data post (owner, 2026-09-30).
EMBED_COLOR = 0x228B22
# Every release the watch posts is a Tier-1 print (CPI, jobs, PCE, FOMC),
# so each one pings the room (owner, 2026-09-30).
MENTION_EVERYONE = True


async def _post(bot, content: str, lines: list[str], footer: str) -> bool:
    """Post the alert to every alert channel at once: `content` is the
    message text a phone notification shows, the embed carries the body.
    True when at least one channel took it; the ledger then marks the
    print posted."""
    import discord
    from discord_bot.sender import _send_with_retry
    cids = alert_channel_ids()
    if not cids:
        return False
    embed = discord.Embed(description="\n".join(lines), color=EMBED_COLOR)
    embed.set_footer(text=footer)
    text = f"@everyone {content}" if MENTION_EVERYONE else content
    mentions = discord.AllowedMentions(everyone=MENTION_EVERYONE)

    async def _one(cid: int) -> bool:
        try:
            channel = bot.get_channel(cid)
            if channel is None:
                channel = await bot.fetch_channel(cid)
            ok, err = await _send_with_retry(
                lambda emb=embed: channel.send(content=text, embed=emb, allowed_mentions=mentions),
                label=f"print-watch {content} -> {cid}")
        except Exception as e:
            ok, err = False, str(e)
        if not ok:
            log.warning(f"print-watch: post failed for {content!r} in {cid}: {err}")
        return bool(ok)

    results = await asyncio.gather(*(_one(c) for c in cids))
    return any(results)


def compose_alert(spec: ReleaseSpec, rel, today_iso: str, release_et: str):
    """(alert, verdict) for a release: the verdict by rule, the policy
    state from the FOMC ledger, the templated takeaway, the rendered text.
    No network and no model call."""
    from report import print_data, print_render, print_templates, print_verdict
    v = print_verdict.verdict_for(rel)
    policy = print_verdict.policy_state([print_data.ledger_action(e) for e in fomc_history(before=today_iso)])
    take = print_templates.takeaway(rel, v, policy, today_iso)
    return print_render.render(spec, rel, v, take, release_et), v


async def _publish(bot, spec: ReleaseSpec, rel, today_iso: str, release_et: str,
                   statement: str | None) -> bool:
    alert, v = compose_alert(spec, rel, today_iso, release_et)
    if not await _post(bot, alert.content, alert.lines, alert.footer):
        return False
    extra = {"content": alert.content,
             "verdict": {"label": v.label, "kind": v.kind, "direction": v.direction, "signals": v.signals}}
    if rel.fomc:
        extra.update({"action": rel.fomc["action"], "dissent_net": rel.fomc["dissent_net"]})
    mark_posted(today_iso, spec.key, alert.lines, rows=rel.ledger_rows(), statement=statement, extra=extra)
    log.info(f"print-watch: posted {spec.key} for {rel.period}: {alert.content}")
    return True


def _poll_s(release_at: datetime) -> int:
    if not settings.bls_api_key:
        return POLL_S_NO_KEY
    early = (datetime.now(_ET) - release_at).total_seconds() < FAST_WINDOW_S
    return POLL_S_FAST if early else POLL_S_WITH_KEY


def _has_a_core_value(spec: ReleaseSpec, obs: dict | None, period: str) -> bool:
    return bool(obs) and any(_value_at(obs.get(ln.series) or [], period) is not None
                             for ln in spec.lines if ln.series and not ln.optional)


async def print_watch_job(bot=None, release_et: str = "08:30") -> None:
    """Armed a minute before `release_et` on every weekday; exits at
    once when nothing supported is scheduled today."""
    if not (settings.print_alert_channel_id or settings.reminder_channel_id) or bot is None:
        return
    from discord_bot.ops_alert import ops_alert
    from report import print_data
    now = datetime.now(_ET)
    today = now.date().isoformat()
    if official_calendar_exhausted(today):
        await ops_alert("print-watch: OFFICIAL_RELEASES in world_context.py has "
                        "no dates left; add next year's BLS/BEA/Fed/ISM schedule",
                        dedupe_key=f"print-watch-calendar-{today}")
    ff_rows = _ff_rows_for_day(today)
    # A release the agency schedule lists for this slot but the watch
    # will not arm for (its agency key is missing) is a silent miss
    # unless someone is told before it happens.
    for key in OFFICIAL_RELEASES.get(today, ()):
        spec = next((s for s in SPECS if s.key == key), None)
        if not spec or spec.release_et != release_et:
            continue
        if not all(source_available(ln.source) for ln in spec.lines if ln.series):
            await ops_alert(f"print-watch: {spec.title} is scheduled {release_et} ET today "
                            f"but its agency API key is not set; it will not post",
                            dedupe_key=f"print-watch-nokey-{today}-{key}")
        elif not ff_lists(spec, ff_rows):
            log.warning(f"print-watch: {key} is on the agency schedule but not the "
                        f"ForexFactory feed today; armed from the schedule")
    due = [s for s in due_releases(today, ff_rows, release_et) if not already_posted(today, s.key)]
    if not due:
        return
    log.info(f"print-watch: armed for {[s.key for s in due]} at {release_et} ET")
    hh, mm = (int(x) for x in release_et.split(":"))
    release_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    period = reference_period(today)
    # The pre-release payroll levels the revision line compares against,
    # fetched now so the post does not wait on FRED after the release.
    vintages: dict = {}
    for s in due:
        if _revised_line(s) is not None:
            try:
                vintages[s.key] = await asyncio.to_thread(payroll_vintage, s, period, today)
            except Exception as e:
                log.warning(f"print-watch: FRED vintage for {s.key} failed, ledger fallback: {e}")
    wait = (release_at - datetime.now(_ET)).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    wait_s = max(MAX_WAIT_S_BY_AGENCY.get(s.agency, MAX_WAIT_S) for s in due)
    deadline = datetime.now(_ET) + timedelta(seconds=wait_s)
    pending = list(due)
    # What the ledger holds from the last post of each release: the rows
    # the revision lines compare against, the statement the FOMC diffs.
    prev_by_key = {s.key: previous_post(s.key, before=today) for s in pending}
    last_obs: dict = {}
    while pending and datetime.now(_ET) < deadline:
        for spec in list(pending):
            try:
                prev = prev_by_key.get(spec.key)
                if spec.key == "fomc":
                    parsed = await asyncio.to_thread(fetch_fomc_today, today)
                    if not parsed:
                        continue
                    rel = print_data.fomc_release(parsed, ff_rows, prev, today)
                    statement = parsed.get("text", "")
                else:
                    obs = await asyncio.to_thread(fetch_observations, spec.lines, force=True)
                    last_obs[spec.key] = obs
                    if not release_ready(spec, obs, period):
                        continue
                    rel = print_data.data_release(spec, obs, period, ff_rows, prev, vintages.get(spec.key))
                    statement = None
                await _publish(bot, spec, rel, today, release_et, statement)
                pending.remove(spec)
            except Exception as e:
                log.warning(f"print-watch: {spec.key} attempt failed: {e}")
        if pending:
            await asyncio.sleep(_poll_s(release_at))
    for spec in pending:
        obs = last_obs.get(spec.key)
        if spec.key != "fomc" and _has_a_core_value(spec, obs, period):
            # Part of the release is out and the rest never came: post what
            # there is, titled as incomplete, rather than nothing.
            try:
                rel = print_data.data_release(spec, obs, period, ff_rows, prev_by_key.get(spec.key),
                                              vintages.get(spec.key), incomplete=True)
                if await _publish(bot, spec, rel, today, release_et, None):
                    await ops_alert(f"print-watch: {spec.title} posted as incomplete data after "
                                    f"{wait_s // 60} min; a core series never published",
                                    dedupe_key=f"print-watch-partial-{today}-{spec.key}")
                    continue
            except Exception as e:
                log.warning(f"print-watch: {spec.key} incomplete post failed: {e}")
        log.warning(f"print-watch: {spec.key} not published within {wait_s // 60} min of {release_et} ET")
        await ops_alert(f"print-watch: {spec.title} was due {release_et} ET and the agency "
                        f"had not published it after {wait_s // 60} min; nothing posted",
                        dedupe_key=f"print-watch-miss-{today}-{spec.key}")


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
        obs_by = fetch_observations([_ROW_TO_LINE[str(r["event"]).lower()] for r in wanted])
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
            r["actual_source"] = f"{ln.source}:{ln.series}"
            if not r.get("unit"):
                r["unit"] = ln.unit
    except Exception as e:
        log.warning(f"print-watch: agency enrichment failed (non-fatal): {e}")
    return rows
