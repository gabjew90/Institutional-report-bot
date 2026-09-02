"""discord_bot/ingestion_feed.py had no test or smoke reference."""
import sys

from discord_bot import ingestion_feed as F


def _with_channel(value, fn):
    import config
    orig = config.settings.discord_ingest_feed_channel_id
    config.settings.discord_ingest_feed_channel_id = value
    try:
        return fn()
    finally:
        config.settings.discord_ingest_feed_channel_id = orig


def test_feed_disabled_when_channel_blank():
    assert _with_channel("", F.feed_enabled) is False
    assert _with_channel("", F._channel_id) is None


def test_channel_id_parses_and_strips():
    assert _with_channel(" 123456789012345678 ", F._channel_id) == 123456789012345678


def test_garbage_channel_id_is_none_not_a_crash():
    assert _with_channel("not-a-snowflake", F._channel_id) is None


def test_uploaded_label_accepts_both_timestamp_seams():
    """SQLite writes a space, isoformat writes a T; both must parse."""
    a = F._fmt_uploaded("2026-09-01T14:30:00")
    b = F._fmt_uploaded("2026-09-01 14:30:00")
    assert a == b
    assert a == "" or a.startswith("uploaded ")


def test_uploaded_label_never_raises():
    assert F._fmt_uploaded(None) == ""
    assert F._fmt_uploaded("garbage") == ""


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
