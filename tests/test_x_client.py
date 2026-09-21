"""X posting (2026-09-11): the signature against the published OAuth
1.0a worked example, the caption budget, the dry-run guard and the
one-post-per-date ledger. No network anywhere in here."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from report import x_client as X
from report.calendar_caption import calendar_caption, X_LIMIT
from report.calendar_data import CalendarDay, ConfRow, EarnRow, EconRow


def test_oauth1_signature_matches_the_reference_vector():
    """The worked example from X's 'Creating a signature' guide."""
    params = {
        "include_entities": "true",
        "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
        "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": "1318622958",
        "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "oauth_version": "1.0",
        "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
    }
    sig = X.oauth1_signature("POST", "https://api.twitter.com/1.1/statuses/update.json",
                             params, "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
                             "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE")
    assert sig == "hCtSmYh+iHYCEqBWrE7C7hYmtUk="
    hdr = X.oauth1_header("POST", "https://api.twitter.com/1.1/statuses/update.json?include_entities=true",
                          consumer_key="xvz1evFS4wEEPTGEFPHBog",
                          consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
                          token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
                          token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
                          extra_params={"status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
                          nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp="1318622958")
    assert hdr.startswith("OAuth ") and 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in hdr
    assert "status=" not in hdr, "body params are signed, never carried in the header"


def _day(n_bmo=2, n_amc=2, conf=True, econ=5):
    return CalendarDay(
        date_iso="2026-09-11", weekday_label="FRIDAY 9/11", is_holiday=False,
        econ=[EconRow("8:30", "Core CPI m/m", "high", True), EconRow("8:30", "CPI y/y", "high", True),
              EconRow("10:00", "Prelim UoM Consumer Sentiment", "medium", False),
              EconRow("10:00", "Prelim UoM Inflation Expectations", "low", False),
              EconRow("14:00", "Federal Budget Balance", "low", False)][:econ],
        bmo=[EarnRow(symbol=f"B{i}", name="x", cap_musd=1) for i in range(n_bmo)],
        amc=[EarnRow(symbol=f"A{i}", name="x", cap_musd=1) for i in range(n_amc)],
        conferences=[ConfRow("GS Communacopia + Technology Conference", "14:50",
                             "MSFT AMAT BKNG CMCSA CDNS PYPL ADSK ROP CHTR CSGP".split(), True)] if conf else [],
    )


from report.calendar_caption import x_length, _time_12h  # noqa: E402

# _day() covers Friday 2026-09-11; the sheet posts the afternoon before
EVE = "2026-09-10"


def test_caption_fits_280_and_keeps_the_important_parts():
    text = calendar_caption(_day(), today_iso=EVE)
    assert x_length(text) <= X_LIMIT
    assert text.startswith("\U0001F4C5 Tomorrow's market calendar · Friday 9/11\n\n")
    assert "Before the open: #B0 #B1" in text
    assert "After the close: #A0 #A1" in text
    assert "Data (ET): 8:30 AM Core CPI m/m, CPI y/y" in text
    assert "GS Communacopia" in text and "#MSFT" in text
    # a crowded day trims but never overflows, and keeps the first names
    busy = _day(n_bmo=15, n_amc=15)
    text = calendar_caption(busy, today_iso=EVE)
    assert x_length(text) <= X_LIMIT
    assert "Core CPI" in text and "#B0" in text and "#A0" in text


def test_the_heading_says_tomorrow_only_when_it_is():
    """Posted 3 PM ET for the next trading day. Friday's post covers
    Monday and a pre-holiday post skips the closed day, so 'Tomorrow'
    would be false on exactly the posts a reader is likeliest to act on
    wrongly (owner, 2026-09-21: be clear the calendar is for tomorrow)."""
    mon = CalendarDay(date_iso="2026-09-28", weekday_label="MONDAY 9/28", is_holiday=False)
    head = lambda d, t: calendar_caption(d, today_iso=t).splitlines()[0]
    assert head(_day(), "2026-09-10") == "\U0001F4C5 Tomorrow's market calendar · Friday 9/11"
    # Friday afternoon post, covering Monday
    assert head(mon, "2026-09-25") == "\U0001F4C5 Monday's market calendar · 9/28"
    assert "Tomorrow" not in head(mon, "2026-09-25")


def test_every_ticker_is_a_hashtag_and_none_is_a_cashtag():
    """Owner pick 2026-09-21 (option B). X refuses more than one cashtag
    on a self-serve API post, and the first live test failed on five."""
    import re as _re
    for d in (_day(), _day(n_bmo=15, n_amc=15), _day(conf=False)):
        text = calendar_caption(d, today_iso=EVE)
        assert not _re.search(r"\$[A-Za-z]", text), text
        for line in text.splitlines():
            if "Before the open: " in line or "After the close: " in line:
                for tok in line.split(": ", 1)[1].split(" "):
                    assert tok.startswith("#"), (tok, line)


def test_x_counts_emoji_as_two():
    assert x_length("abc") == 3
    assert x_length("\U0001F4C5 a") == 4


def test_times_read_as_12_hour():
    assert _time_12h("8:30") == "8:30 AM"
    assert _time_12h("14:00") == "2:00 PM"
    assert _time_12h("12:00") == "12:00 PM"
    assert _time_12h("All Day") == "All Day"


def test_a_holiday_says_closed():
    hol = CalendarDay(date_iso="2026-11-26", weekday_label="THURSDAY 11/26", is_holiday="Thanksgiving Day")
    assert calendar_caption(hol, today_iso="2026-11-25") == (
        "\U0001F4C5 Tomorrow's market calendar · Thursday 11/26\n\n"
        "Markets closed · Thanksgiving Day")


def test_the_post_time_backstop_keeps_one_cashtag_and_leaves_dollars():
    assert X.enforce_cashtag_limit("$NVDA and $AMD, $TSLA") == "$NVDA and AMD, TSLA"
    assert X.enforce_cashtag_limit("costs $5 or $1.2B, $NVDA") == "costs $5 or $1.2B, $NVDA"


def test_disabled_is_a_dry_run_with_no_network_and_no_ledger_entry():
    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")), \
         patch("config.settings.x_post_enabled", False), \
         patch("report.x_client._request", side_effect=AssertionError("network call in dry run")):
        assert X.post_image("hello", b"png", key="calendar", date_iso="2026-09-11") is None
        assert not X.already_posted("2026-09-11", "calendar")


def test_enabled_without_keys_does_not_post():
    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")), \
         patch("config.settings.x_post_enabled", True), \
         patch("config.settings.x_api_key", ""), \
         patch("report.x_client._request", side_effect=AssertionError("network call without keys")):
        assert X.post_image("hello", b"png", key="calendar", date_iso="2026-09-11") is None


def test_enabled_posts_once_and_records_the_id():
    calls = []

    def fake_request(method, url, body, ctype, creds, extra_params=None):
        calls.append(url)
        if "media" in url:
            return 200, {"data": {"id": "m1", "media_key": "3_m1"}}
        return 201, {"data": {"id": "1957", "text": "hello"}}

    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")), \
         patch("config.settings.x_post_enabled", True), \
         patch("config.settings.x_api_key", "k"), patch("config.settings.x_api_secret", "s"), \
         patch("config.settings.x_access_token", "t"), patch("config.settings.x_access_secret", "ts"), \
         patch("report.x_client._request", side_effect=fake_request):
        assert X.post_image("hello", b"png", key="calendar", date_iso="2026-09-11") == "1957"
        assert calls == [X.X_MEDIA_URLS[0], X.X_POST_URL]
        assert X.already_posted("2026-09-11", "calendar")
        ledger = json.loads(X._ledger_path("2026-09-11").read_text(encoding="utf-8"))
        assert ledger["calendar"]["post_id"] == "1957"
        # second call the same night: nothing sent
        assert X.post_image("hello", b"png", key="calendar", date_iso="2026-09-11") is None
        assert len(calls) == 2


def test_media_upload_falls_back_to_the_legacy_host_on_404_only():
    seen = []

    def fake_request(method, url, body, ctype, creds, extra_params=None):
        seen.append(url)
        if url == X.X_MEDIA_URLS[0]:
            return 404, {"error": "gone"}
        return 200, {"media_id_string": "77"}

    with patch("report.x_client._request", side_effect=fake_request):
        assert X.upload_media(b"png", {"consumer_key": "k", "consumer_secret": "s", "token": "t", "token_secret": "x"}) == "77"
    assert seen == list(X.X_MEDIA_URLS)
    with patch("report.x_client._request", return_value=(403, {"error": "forbidden"})):
        assert X.upload_media(b"png", {"consumer_key": "k", "consumer_secret": "s", "token": "t", "token_secret": "x"}) is None


def test_calendar_job_posts_to_x_after_discord():
    import inspect
    from scheduler import jobs
    src = inspect.getsource(jobs._daily_calendar_job)
    i = src.index("record_calendar_posts")
    j = src.index("x_client.post_calendar")
    assert i < j, "X posts only after the Discord post is recorded"


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


# --- owner-requested test post (2026-09-21) ----------------------------
def _fake_ok(method, url, body, ctype, creds, extra_params=None):
    if "media" in url:
        return 200, {"data": {"id": "m1"}}
    return 201, {"data": {"id": "4242"}}


def test_force_posts_once_without_turning_posting_on():
    """The test used to set settings.x_post_enabled = True. Inside the
    worker that would leave posting on for every later job."""
    from config import settings
    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")), \
         patch("config.settings.x_post_enabled", False), \
         patch("config.settings.x_api_key", "k"), patch("config.settings.x_api_secret", "s"), \
         patch("config.settings.x_access_token", "t"), patch("config.settings.x_access_secret", "ts"), \
         patch("report.x_client._request", side_effect=_fake_ok):
        assert X.post_image("hi", b"png", key="calendar", date_iso="2026-09-22", force=True) == "4242"
        assert settings.x_post_enabled is False
        # the nightly job, still disabled, does not double-post that date
        assert X.post_image("hi", b"png", key="calendar", date_iso="2026-09-22") is None


def test_the_request_job_is_idle_without_a_flag_and_clears_it_before_posting():
    import asyncio
    from scheduler import jobs
    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")):
        d = jobs._x_request_dir()
        # no flag: nothing built, nothing posted
        with patch("report.calendar_data.build_calendar_day",
                   side_effect=AssertionError("built without a request")):
            asyncio.run(jobs._x_test_post_request_job())

        d.mkdir(parents=True)
        (d / jobs.X_REQUEST_FLAG).write_text("")
        seen = {}

        def fake_post(text, png, *, key, date_iso, force=False):
            # the flag is gone BEFORE the post, so a crash mid-post can
            # never make the next minute post again
            seen["flag_present"] = (d / jobs.X_REQUEST_FLAG).exists()
            seen["force"] = force
            return "777"

        with patch("report.calendar_data.build_calendar_day", return_value=object()), \
             patch("report.calendar_render.render_calendar_png", return_value=b"png"), \
             patch("report.calendar_caption.calendar_caption", return_value="caption"), \
             patch("report.x_client.post_image", side_effect=fake_post):
            asyncio.run(jobs._x_test_post_request_job())
        assert seen == {"flag_present": False, "force": True}
        out = (d / f"{jobs.X_REQUEST_FLAG}.result").read_text(encoding="utf-8")
        assert "https://x.com/i/status/777" in out
