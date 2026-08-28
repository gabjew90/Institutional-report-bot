"""The /ask crash alert — deterministic, immediate, rate-limited.

Born 2026-08-28: three ungrounded asks died on an UnboundLocalError
for 14 hours and the fastest detector in the system was the owner. A
crash needs no judge and must not wait for the nightly QC's
day-delayed read.
"""
import asyncio
import sys

from discord_bot import bot as B


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


def _with_patched(cid, fn):
    import config
    sent = []
    orig_cid = config.settings.ops_alert_channel_id
    orig_last = dict(B._OPS_ALERT_LAST)
    B._OPS_ALERT_LAST.clear()

    async def run():
        # patch the network layer: capture instead of POST
        import urllib.request as rq
        orig_open = rq.urlopen

        def fake_open(req, timeout=0):
            sent.append(req.full_url)
            class R:  # minimal response
                def read(self): return b"{}"
            return R()
        rq.urlopen = fake_open
        try:
            config.settings.ops_alert_channel_id = cid
            await fn()
        finally:
            rq.urlopen = orig_open
            config.settings.ops_alert_channel_id = orig_cid
            B._OPS_ALERT_LAST.clear()
            B._OPS_ALERT_LAST.update(orig_last)
    _run(run())
    return sent


def test_disabled_when_channel_unset():
    async def go():
        await B._ops_alert("boom", dedupe_key="X")
    assert _with_patched("", go) == []


def test_sends_when_configured():
    async def go():
        await B._ops_alert("boom", dedupe_key="X")
    sent = _with_patched("123", go)
    assert len(sent) == 1 and "/channels/123/messages" in sent[0]


def test_rate_limited_per_error_class():
    """A crash loop pings once an hour, not once a turn — but a
    DIFFERENT error class gets its own ping."""
    async def go():
        await B._ops_alert("boom 1", dedupe_key="UnboundLocalError")
        await B._ops_alert("boom 2", dedupe_key="UnboundLocalError")
        await B._ops_alert("other", dedupe_key="TimeoutError")
    sent = _with_patched("123", go)
    assert len(sent) == 2


def test_send_failure_never_raises():
    """The alarm failing must not worsen the failure it reports."""
    import config
    orig = config.settings.ops_alert_channel_id

    async def go():
        # unpatched urlopen against an invalid host will raise inside;
        # _ops_alert must swallow it
        config.settings.ops_alert_channel_id = "0"
        try:
            await B._ops_alert("boom", dedupe_key="Z")
        finally:
            config.settings.ops_alert_channel_id = orig
            B._OPS_ALERT_LAST.clear()
    asyncio.run(go())  # passes iff nothing raised


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
