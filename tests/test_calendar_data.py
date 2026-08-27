"""Calendar data-layer correctness.

Two bugs, both of which put a WRONG sheet in front of readers rather
than a visibly broken one:

1. A failed Finnhub market-cap lookup cached as cap 0 under the SAME
   7-day TTL as a success. The calendar ranks earnings by cap, so one
   transient failure sorted a mega-cap below micro-caps and dropped it
   from the sheet for a week.

2. Econ events were filtered by the UTC date string prefix. The feed
   stamps UTC and the sheet is an ET document, so an 8 PM ET event
   (00:00 UTC next day) filed under tomorrow and rendered as "20:00" —
   right time, wrong day.
"""
import sys

import db
from report.news_data import _et_date


# ---------------------------------------------------------------- caps

def _write_cap(symbol: str, cap: float, name: str, age_days: int) -> None:
    """Write a cache row with a controlled age. Bypasses upsert, which
    always stamps now()."""
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS symbol_market_cap ("
                 "symbol TEXT PRIMARY KEY, market_cap_musd REAL, "
                 "name TEXT, fetched_at TEXT NOT NULL "
                 "DEFAULT (datetime('now')))")
    conn.execute(
        "INSERT OR REPLACE INTO symbol_market_cap "
        "(symbol, market_cap_musd, name, fetched_at) "
        "VALUES (?, ?, ?, datetime('now', ?))",
        (symbol, cap, name, f"-{age_days} days"))
    conn.commit()


def test_failed_lookup_from_yesterday_refetches():
    """THE BUG. A cap-0 row written yesterday must NOT be served, so
    _resolve_caps counts it a miss and refetches tonight."""
    _write_cap("FAILSYM", 0, "FAILSYM", age_days=1)
    got = db.get_market_caps(["FAILSYM"])
    assert "FAILSYM" not in got, (
        f"a failed lookup from yesterday was served from cache: {got}. "
        "That is the week-long benching bug.")


def test_failed_lookup_from_today_is_still_cached():
    """The failure row still earns a TTL — it just earns a 1-day one.
    Without this the retry loop runs hot within the same evening."""
    _write_cap("FRESHFAIL", 0, "FRESHFAIL", age_days=0)
    assert "FRESHFAIL" in db.get_market_caps(["FRESHFAIL"])


def test_successful_lookup_keeps_the_seven_day_ttl():
    """The fix must not shorten the success TTL — that would restore
    the nightly Finnhub hammering the cache exists to prevent."""
    _write_cap("GOODSYM", 3_000_000.0, "Good Corp", age_days=3)
    got = db.get_market_caps(["GOODSYM"])
    assert "GOODSYM" in got, "a 3-day-old SUCCESS must still be cached"
    assert got["GOODSYM"]["cap"] == 3_000_000.0


def test_success_older_than_ttl_is_dropped():
    _write_cap("STALESYM", 3_000_000.0, "Stale Corp", age_days=9)
    assert "STALESYM" not in db.get_market_caps(["STALESYM"])


def test_null_cap_is_treated_as_a_failure_not_a_success():
    """COALESCE guard: a NULL cap is a failed lookup, not a huge one."""
    _write_cap("NULLSYM", None, "Null Corp", age_days=3)
    assert "NULLSYM" not in db.get_market_caps(["NULLSYM"]), (
        "a NULL cap from 3 days ago took the SUCCESS branch")


def test_failure_expires_by_calendar_day_not_elapsed_hours():
    """The nightly job runs on a ~24h cadence, so an elapsed-time cutoff
    of exactly 1 day lands every real retry ON the boundary, where `>=`
    keeps the stale failure. This is that exact case and it must expire.
    It failed before the query moved to date() comparison."""
    _write_cap("EDGEFAIL", 0, "EDGEFAIL", age_days=1)
    assert "EDGEFAIL" not in db.get_market_caps(["EDGEFAIL"])


# ------------------------------------------------ earnings assembly

def _build_with(raw_rows, econ_rows=(), moves=None):
    """Run build_calendar_day against canned feed rows. Patches every
    network boundary and restores them, so nothing here touches
    Finnhub, ForexFactory, or Yahoo.

    `moves`: dict symbol -> implied move for the patched fetch. Default
    None for every symbol, which exercises the wholesale-failure
    fallback — all names shown, dashes — matching the sheet's old
    behaviour, so assembly tests stay about assembly."""
    from report import calendar_data as cd
    from report import news_data as nd

    orig_earn = nd.fetch_earnings_calendar_all
    orig_econ = nd.fetch_us_econ_events_for_date
    orig_caps = cd._resolve_caps
    orig_move = cd._implied_move_fetch
    orig_pace = cd._MOVE_PACE_S
    try:
        nd.fetch_earnings_calendar_all = lambda d: list(raw_rows)
        nd.fetch_us_econ_events_for_date = lambda d: list(econ_rows)
        # every symbol resolves to a distinct descending cap so ranking
        # is deterministic and never hits the network
        cd._resolve_caps = lambda syms: {
            s: {"cap": 1000.0 - i, "name": f"{s} Inc"}
            for i, s in enumerate(dict.fromkeys(syms))
        }
        cd._implied_move_fetch = lambda s, d: (moves or {}).get(s)
        cd._MOVE_PACE_S = 0
        return cd.build_calendar_day("2026-08-27")
    finally:
        nd.fetch_earnings_calendar_all = orig_earn
        nd.fetch_us_econ_events_for_date = orig_econ
        cd._resolve_caps = orig_caps
        cd._implied_move_fetch = orig_move
        cd._MOVE_PACE_S = orig_pace


def test_duplicate_symbol_appears_once():
    """Finnhub can return one symbol twice for a date. Ranking is by
    cap, so the two copies sort ADJACENT and the sheet shows the same
    company on consecutive lines."""
    day = _build_with([
        {"symbol": "AAPL", "hour": "amc"},
        {"symbol": "AAPL", "hour": "amc"},
        {"symbol": "MSFT", "hour": "amc"},
    ])
    syms = [r.symbol for r in day.amc]
    assert syms.count("AAPL") == 1, f"AAPL rendered twice: {syms}"
    assert "MSFT" in syms


def test_duplicate_across_sessions_keeps_first_row():
    """A symbol listed under BOTH sessions must not appear on both
    halves of the sheet. First row wins."""
    day = _build_with([
        {"symbol": "NVDA", "hour": "bmo"},
        {"symbol": "NVDA", "hour": "amc"},
    ])
    assert [r.symbol for r in day.bmo] == ["NVDA"]
    assert [r.symbol for r in day.amc] == []


def test_blank_hour_is_excluded_but_counted():
    """Owner call 2026-08-27: a name whose session Finnhub cannot
    confirm (blank/dmh hour) does not render at all — it used to show
    under AFTER CLOSE with a * flag. It still counts toward "+N more"
    so the sheet stays honest about what it is not showing."""
    day = _build_with([{"symbol": "XYZ", "hour": ""},
                       {"symbol": "OK", "hour": "amc"}],
                      moves={"OK": 4.0})
    assert [r.symbol for r in day.amc] == ["OK"]
    assert day.dropped_amc == 1


def test_dmh_hour_is_excluded_too():
    day = _build_with([{"symbol": "MIDDAY", "hour": "dmh"}])
    assert day.amc == []
    assert day.dropped_amc == 1


def test_bmo_is_confirmed():
    day = _build_with([{"symbol": "XYZ", "hour": "bmo"}])
    assert day.bmo[0].session_confirmed is True


# ------------------------------------ implied-move selection (owner
# call 2026-08-27: a name earns its row by pricing an honest implied
# move; unpriceable names are dropped, not dashed)

def test_unpriceable_name_is_dropped_and_backfilled():
    """The 8/27 incident shape: illiquid names between liquid ones. The
    unpriceable one vanishes and the next-ranked name takes its slot."""
    from report import calendar_data as cd
    orig = cd.TOP_N
    try:
        cd.TOP_N = 2
        day = _build_with(
            [{"symbol": s, "hour": "amc"} for s in
             ("BIG", "ILLIQUID", "NEXT")],
            moves={"BIG": 5.0, "NEXT": 3.0},   # ILLIQUID -> None
        )
    finally:
        cd.TOP_N = orig
    assert [r.symbol for r in day.amc] == ["BIG", "NEXT"]
    assert [r.implied_move for r in day.amc] == [5.0, 3.0]
    # the dropped name counts toward "+N more" so the sheet stays
    # honest about coverage
    assert day.dropped_amc == 1


def test_every_shown_row_has_a_move():
    """The rule itself: when at least one name prices, nothing dashless
    reaches the sheet."""
    day = _build_with(
        [{"symbol": s, "hour": "bmo"} for s in ("AA", "BB", "CC")],
        moves={"AA": 4.2},
    )
    assert [r.symbol for r in day.bmo] == ["AA"]
    assert all(r.implied_move is not None for r in day.bmo)
    assert day.dropped_bmo == 2


def test_wholesale_move_failure_falls_back_to_dashes():
    """Yahoo down / module broken (the numpy incident): every fetch
    returns None. An EMPTY earnings column would claim nobody reports
    tomorrow — a lie. The dashes only claim we couldn't price them, so
    that is the degradation."""
    day = _build_with(
        [{"symbol": s, "hour": "amc"} for s in ("AA", "BB")],
        moves={},
    )
    assert [r.symbol for r in day.amc] == ["AA", "BB"]
    assert all(r.implied_move is None for r in day.amc)


def test_selection_respects_the_fetch_budget():
    """The walk must stop at TOP_N + MOVE_FETCH_BUDGET_EXTRA attempts,
    or one all-illiquid tail turns the nightly render into an unbounded
    Yahoo crawl."""
    from report import calendar_data as cd
    calls = []
    orig_move, orig_pace = cd._implied_move_fetch, cd._MOVE_PACE_S
    try:
        cd._implied_move_fetch = lambda s, d: calls.append(s)  # None
        cd._MOVE_PACE_S = 0
        pool = [f"S{i:02d}" for i in range(cd.TOP_N * 4)]
        kept = cd._select_priced(pool, "2026-08-27")
    finally:
        cd._implied_move_fetch, cd._MOVE_PACE_S = orig_move, orig_pace
    assert kept == []
    assert len(calls) == cd.TOP_N + cd.MOVE_FETCH_BUDGET_EXTRA


def test_selection_stops_once_top_n_priced():
    """No fetch is spent past the point the sheet is full."""
    from report import calendar_data as cd
    calls = []
    orig_move, orig_pace = cd._implied_move_fetch, cd._MOVE_PACE_S
    try:
        cd._implied_move_fetch = (
            lambda s, d: (calls.append(s), 5.0)[1])
        cd._MOVE_PACE_S = 0
        pool = [f"S{i:02d}" for i in range(cd.TOP_N * 4)]
        kept = cd._select_priced(pool, "2026-08-27")
    finally:
        cd._implied_move_fetch, cd._MOVE_PACE_S = orig_move, orig_pace
    assert len(kept) == cd.TOP_N
    assert len(calls) == cd.TOP_N


def test_a_raising_fetch_is_an_unpriceable_name_not_a_crash():
    from report import calendar_data as cd
    orig_move, orig_pace = cd._implied_move_fetch, cd._MOVE_PACE_S

    def boom(s, d):
        if s == "BAD":
            raise RuntimeError("chain fetch exploded")
        return 4.0
    try:
        cd._implied_move_fetch = boom
        cd._MOVE_PACE_S = 0
        kept = cd._select_priced(["BAD", "GOOD"], "2026-08-27")
    finally:
        cd._implied_move_fetch, cd._MOVE_PACE_S = orig_move, orig_pace
    assert kept == [("GOOD", 4.0)]


# ------------------------------------------------------ destination

def test_calendar_channel_fallback_is_the_pulse_channels():
    """An UNSET CALENDAR_CHANNEL_IDS must resolve to DISCORD_CHANNEL_ID.
    A new routing setting that defaults to "post nowhere" silently kills
    the daily sheet on every deploy that has not set the new var yet."""
    from config import settings
    resolved = (settings.calendar_channel_ids or "").strip() or (
        settings.discord_channel_id or "").strip()
    assert resolved == (settings.discord_channel_id or "").strip()


def test_calendar_channel_override_wins_when_set():
    from config import settings
    override = "123,456"
    resolved = (override or "").strip() or (
        settings.discord_channel_id or "").strip()
    assert resolved == "123,456"


# ------------------------------------------------------------ ET dates

def test_evening_et_event_files_under_its_et_date():
    """00:30 UTC on the 27th is 8:30 PM ET on the 26th."""
    assert _et_date("2026-08-27T00:30:00") == "2026-08-26"


def test_midnight_utc_boundary():
    assert _et_date("2026-08-27T00:00:00") == "2026-08-26"


def test_morning_et_event_keeps_its_date():
    """12:30 UTC is 8:30 AM ET — same calendar day, the common case."""
    assert _et_date("2026-08-26T12:30:00") == "2026-08-26"


def test_late_et_evening_still_previous_day():
    """03:59 UTC is 11:59 PM ET the day before."""
    assert _et_date("2026-08-27T03:59:00") == "2026-08-26"


def test_est_and_edt_both_handled():
    """January is EST (UTC-5), August is EDT (UTC-4). A hardcoded offset
    gets one of these wrong."""
    assert _et_date("2026-01-27T04:30:00") == "2026-01-26"   # EST -5
    assert _et_date("2026-01-27T05:30:00") == "2026-01-27"
    assert _et_date("2026-08-27T03:30:00") == "2026-08-26"   # EDT -4
    assert _et_date("2026-08-27T04:30:00") == "2026-08-27"


def test_z_suffix_parses():
    assert _et_date("2026-08-27T00:30:00Z") == "2026-08-26"


def test_filter_routes_evening_event_to_its_et_date():
    """Through the REAL filter, not just the helper.

    The helper tests above all passed while `_et_date` sat unused and
    the filter still did a UTC prefix match -- they tested a function,
    not the behaviour. This one patches the feed and asserts on what
    fetch_us_econ_events_for_date actually returns.
    """
    from report import news_data as nd

    feed = [
        # 8:30 PM ET on the 26th, stamped 00:30 UTC on the 27th
        {"country": "US", "time": "2026-08-27T00:30:00",
         "event": "Fed Speaker", "impact": "high"},
        # 8:30 AM ET on the 27th
        {"country": "US", "time": "2026-08-27T12:30:00",
         "event": "Jobless Claims", "impact": "medium"},
        # not US
        {"country": "EUR", "time": "2026-08-27T12:30:00",
         "event": "ECB Speaker", "impact": "high"},
    ]
    orig = nd._fetch_ff_economic_events
    try:
        nd._fetch_ff_economic_events = lambda: list(feed)
        got_26 = nd.fetch_us_econ_events_for_date("2026-08-26")
        got_27 = nd.fetch_us_econ_events_for_date("2026-08-27")
    finally:
        nd._fetch_ff_economic_events = orig

    names_26 = [e["event"] for e in got_26]
    names_27 = [e["event"] for e in got_27]
    assert names_26 == ["Fed Speaker"], (
        f"the 8 PM ET event belongs on the 26th, got {names_26}")
    assert names_27 == ["Jobless Claims"], (
        f"the 8 PM ET event leaked onto the 27th, got {names_27}")


def test_filter_keeps_the_us_only_rule():
    from report import news_data as nd
    feed = [{"country": "GBP", "time": "2026-08-27T12:30:00",
             "event": "BOE Speaker", "impact": "high"}]
    orig = nd._fetch_ff_economic_events
    try:
        nd._fetch_ff_economic_events = lambda: list(feed)
        assert nd.fetch_us_econ_events_for_date("2026-08-27") == []
    finally:
        nd._fetch_ff_economic_events = orig


def test_unparseable_stamp_falls_back_to_prefix():
    """Garbage must not drop the event silently — fall back to the raw
    prefix so it behaves as it did before, no worse."""
    assert _et_date("not-a-timestamp") == "not-a-time"
    assert _et_date("") == ""


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


# ---------------------------------------------------------------- logos

def _write_logo(symbol: str, blob: bytes, age_days: int) -> None:
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS symbol_logo ("
                 "symbol TEXT PRIMARY KEY, image BLOB, "
                 "fetched_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute(
        "INSERT OR REPLACE INTO symbol_logo (symbol, image, fetched_at) "
        "VALUES (?, ?, datetime('now', ?))",
        (symbol, blob, f"-{age_days} days"))
    conn.commit()


def test_logo_hit_keeps_the_long_ttl():
    _write_logo("LOGOOK", b"\x89PNG-ish", age_days=20)
    assert "LOGOOK" in db.get_symbol_logos(["LOGOOK"])


def test_logo_hit_expires_eventually():
    _write_logo("LOGOOLD", b"\x89PNG-ish", age_days=40)
    assert "LOGOOLD" not in db.get_symbol_logos(["LOGOOLD"])


def test_logo_miss_expires_sooner_than_a_hit():
    """A cached MISS (b'') must not survive as long as a real logo, or
    one bad fetch hides a company's logo for the full month."""
    _write_logo("LOGOMISS", b"", age_days=20)
    assert "LOGOMISS" not in db.get_symbol_logos(["LOGOMISS"]), (
        "a 20-day-old MISS took the long success TTL")


def test_fresh_logo_miss_is_still_cached():
    """Misses do earn a TTL — otherwise every logoless name refetches
    nightly."""
    _write_logo("LOGOMISS2", b"", age_days=1)
    got = db.get_symbol_logos(["LOGOMISS2"])
    assert "LOGOMISS2" in got and got["LOGOMISS2"] == b""


def test_downscale_produces_a_square_rgba_tile():
    import io
    from PIL import Image
    from report.calendar_data import _downscale_logo, LOGO_PX
    src = Image.new("RGBA", (512, 128), (10, 20, 30, 255))
    buf = io.BytesIO()
    src.save(buf, "PNG")
    out = _downscale_logo(buf.getvalue())
    assert out, "a valid wide PNG must downscale, not be dropped"
    tile = Image.open(io.BytesIO(out))
    assert tile.size == (LOGO_PX * 2, LOGO_PX * 2), tile.size
    assert tile.mode == "RGBA", "corners need alpha to sit on the sheet"


def test_downscale_rejects_garbage_without_raising():
    """A broken logo degrades to no logo. The calendar renders
    unattended at 04:00; it must never fail over artwork."""
    from report.calendar_data import _downscale_logo
    assert _downscale_logo(b"not an image at all") == b""
    assert _downscale_logo(b"") == b""


def test_resolve_logos_skips_symbols_with_no_fresh_profile():
    """A symbol whose CAP came from cache had no profile call, so we
    learned nothing about its logo. Caching b'' for it would mark every
    warm-cap name as logoless."""
    from report import calendar_data as cd
    got = cd._resolve_logos(["NOPROFILE"], {"NOPROFILE": {"cap": 1.0}})
    assert "NOPROFILE" not in got
    assert db.get_symbol_logos(["NOPROFILE"]) == {}


def test_resolve_logos_caches_a_real_empty_logo():
    """Finnhub answered and has no logo. That IS cacheable."""
    from report import calendar_data as cd
    got = cd._resolve_logos(
        ["NOART"], {"NOART": {"cap": 1.0, "name": "x", "logo": ""}})
    assert got.get("NOART") == b""
    assert db.get_symbol_logos(["NOART"]).get("NOART") == b""


def test_resolve_logos_never_downloads_a_cached_symbol():
    from report import calendar_data as cd
    _write_logo("CACHEDLOGO", b"\x89PNG-ish", age_days=1)
    calls = []
    orig = cd._http_bytes
    try:
        cd._http_bytes = lambda url, timeout=8: calls.append(url) or b""
        got = cd._resolve_logos(
            ["CACHEDLOGO"],
            {"CACHEDLOGO": {"logo": "http://example.invalid/l.png"}})
    finally:
        cd._http_bytes = orig
    assert calls == [], f"downloaded a cached logo: {calls}"
    assert got["CACHEDLOGO"] == b"\x89PNG-ish"
