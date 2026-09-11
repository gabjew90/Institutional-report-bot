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


def test_caption_fits_280_and_keeps_the_important_parts():
    text = calendar_caption(_day())
    assert len(text) <= X_LIMIT
    assert text.startswith("Market calendar · Friday 9/11")
    assert "8:30 Core CPI m/m, CPI y/y" in text
    assert "$B0" in text and "$A0" in text
    assert "GS Communacopia" in text and "$MSFT" in text
    # a crowded day trims but never overflows
    busy = _day(n_bmo=15, n_amc=15)
    text = calendar_caption(busy)
    assert len(text) <= X_LIMIT and "Core CPI" in text and "$B0" in text


def test_important_names_are_hashtagged_and_survive_trimming():
    """Owner call 2026-09-11: hashtag the bold names on the sheet. The
    hashtag line is the last thing the trimmer touches."""
    from report.calendar_caption import important_tickers
    d = _day()
    d.amc[0] = EarnRow(symbol="ORCL", name="Oracle", cap_musd=650_000, important=True)
    d.bmo[1] = EarnRow(symbol="KR", name="Kroger", cap_musd=40_000, important=True)
    assert important_tickers(d)[:2] == ["KR", "ORCL"], "sheet order: before open, then after close"
    assert "MSFT" in important_tickers(d) and "CSGP" not in important_tickers(d), \
        "conference names hashtag only when on the major-ticker list"
    text = calendar_caption(d)
    assert len(text) <= X_LIMIT
    assert text.splitlines()[-1].startswith("#KR #ORCL #MSFT")
    # crowded: econ detail and tickers give way, the hashtags stay
    busy = _day(n_bmo=15, n_amc=15)
    for i in range(6):
        busy.bmo[i] = EarnRow(symbol=f"BIG{i}", name="x", cap_musd=100_000, important=True)
    text = calendar_caption(busy)
    assert len(text) <= X_LIMIT and "#BIG0" in text and "#BIG5" in text
    # nothing bold: no empty hashtag line
    assert not calendar_caption(_day(conf=False)).splitlines()[-1].startswith("#")
    # a holiday
    hol = CalendarDay(date_iso="2026-11-26", weekday_label="THURSDAY 11/26", is_holiday="Thanksgiving Day")
    assert calendar_caption(hol) == "Market calendar · Thursday 11/26\nMarkets closed · Thanksgiving Day"


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
