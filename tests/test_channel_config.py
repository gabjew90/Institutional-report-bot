"""channel_config: channels by permanent ID (owner, 2026-10-04)."""
from types import SimpleNamespace

import channel_config as cc

KLOH = 1415429535268343919
TEST = cc.TEST_CHANNEL_ID


def _ch(cid, name):
    return SimpleNamespace(id=cid, name=name)


def test_a_renamed_channel_still_matches_by_id():
    # kloh's channel before and after the 2026-10-02 rename
    assert cc.eager_ocr(_ch(KLOH, "🦉-kloh-alerts-🦉"))
    assert cc.eager_ocr(_ch(KLOH, "🦉-big-gloh-alerts-🦉"))
    assert cc.eager_ocr(KLOH)


def test_every_channel_is_ingested_except_test_channel():
    assert cc.ingests(_ch(1448522270535585912, "👨‍🍳cooking-channel-🥘"))
    assert cc.ingests(_ch(999, "a-channel-created-tomorrow"))
    assert not cc.ingests(_ch(TEST, "test-channel"))
    assert not cc.ingests(_ch(TEST, "renamed-test-channel"))
    # the private channels stay out even if access is granted later
    for cid in (1433287712810467432, 1404927980685230212,
                1480543318378152069, 1432569389273710613):
        assert not cc.ingests(_ch(cid, "👨🏻-private")), cid
    # and the webhook-feed channels, member comments included
    for cid in (1317611433235841126, 1317740920258428959, 1404891421558702201):
        assert not cc.ingests(_ch(cid, "a-feed")), cid
    assert cc.ingests(_ch(1317611693844463656, "🔔-0dte-yapping-🔔"))


def test_a_thread_in_test_channel_is_excluded_too():
    thread = SimpleNamespace(id=42, name="a-thread", parent_id=TEST)
    assert not cc.ingests(thread)
    assert cc.ingests(SimpleNamespace(id=43, name="a-thread", parent_id=KLOH))


def test_the_league_channel_is_found_by_id_after_a_rename():
    from discord_bot import ask_router as R
    assert R.in_fantasy_channel("🏆-sunday-sweats-🏆", cc.FANTASY_CHANNEL_ID)
    assert R.in_fantasy_channel("🏈-fantasy-football-yapping-🏈")
    assert not R.in_fantasy_channel("💬-stonks-yapping-💬", 1317587853282119745)


def test_callers_match_by_channel_id_and_by_name():
    kyle = 1420507457708621824
    assert cc.caller_for(_ch(kyle, "💅🏾-kyle-renamed-💅🏾"))["name"] == "bankerkyle"
    assert cc.caller_for("💅🏾-kyle-alerts-💅🏾")["name"] == "bankerkyle"
    assert cc.caller_for(_ch(KLOH, "🦉-big-gloh-alerts-🦉")) is None


def test_admin_commands_run_in_test_channel_only():
    assert cc.is_command_channel(_ch(TEST, "test-channel"))
    assert not cc.is_command_channel(_ch(1317587853282119745, "💬-stonks-yapping-💬"))
    assert cc.label(str(TEST)) == "#test-channel"


def test_a_name_written_the_old_way_still_works():
    entries = cc.parse("💬-stonks-yapping-💬, 1415429535268343919")
    assert cc.matches(_ch(1, "💬-stonks-yapping-💬"), entries)
    assert cc.matches(_ch(KLOH, "anything"), entries)
    assert not cc.matches(_ch(2, "other"), entries)


def test_member_batch_reads_alert_channels_by_id_and_skips_callers():
    from analyst_log.member_batch import member_channels
    chans = member_channels()
    assert KLOH in chans
    assert 1317587853282119747 not in chans      # abe-alerts: a caller, stays live
    assert all(isinstance(c, int) for c in chans)
