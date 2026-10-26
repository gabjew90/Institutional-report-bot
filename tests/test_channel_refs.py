"""A question about a channel the bot does not store is answered in code
(2026-10-26 audit A15)."""
from discord_bot import channel_refs as C


def test_an_excluded_feed_channel_is_unread(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "chat_ingest_exclude", "1317740920258428959,test-channel")
    monkeypatch.setattr(settings, "chat_ingestion_channels", "")
    monkeypatch.setattr(settings, "chat_ingest_all_channels", True)
    q = "Any activity on any of these names  from <#1317740920258428959>  that we should be made aware of ?"
    assert C.unread_channels(q) == [1317740920258428959]
    assert "<#1317740920258428959>" in C.note([1317740920258428959])
    assert C.unread_channels("what did kloh post in <#1400000000000000001>") == []
    assert C.unread_channels("no channel here") == []
