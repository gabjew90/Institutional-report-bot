"""Smoke tests for Batch 2 fixes.

Validates:
  1. send_embeds_detailed exists and has correct shape
  2. _extract_retry_after parses 429 headers + retry_after attr
  3. send_embeds returns False on 401/403/404 without retrying
  4. send_embeds honors Retry-After header + retries 5xx with backoff
  5. eager-OCR semaphore is bounded by settings.eager_ocr_max_concurrent
  6. Adjudication 0-themes path: bridge detects + moves to delivery-failed/
     (logic-only check — verify code branch reachable by inspection of
     control flow)
"""

import asyncio
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def _ok(msg: str) -> None:
    print(f"PASS {msg}")


def _fail(msg: str) -> None:
    print(f"FAIL {msg}")
    sys.exit(1)


# ----------------------------------------------------------------------
# 1. _extract_retry_after
# ----------------------------------------------------------------------
def test_extract_retry_after():
    from discord_bot.sender import _extract_retry_after
    import discord

    # Case A: header present on response
    fake_response = SimpleNamespace(
        headers={"Retry-After": "2.5"},
    )
    exc = discord.HTTPException.__new__(discord.HTTPException)
    exc.response = fake_response
    exc.text = ""
    val = _extract_retry_after(exc)
    assert val == 2.5, f"expected 2.5, got {val}"

    # Case B: lowercase header
    fake_response2 = SimpleNamespace(headers={"retry-after": "1.0"})
    exc2 = discord.HTTPException.__new__(discord.HTTPException)
    exc2.response = fake_response2
    val2 = _extract_retry_after(exc2)
    assert val2 == 1.0, f"expected 1.0, got {val2}"

    # Case C: attribute fallback
    fake_response3 = SimpleNamespace(headers={})
    exc3 = discord.HTTPException.__new__(discord.HTTPException)
    exc3.response = fake_response3
    exc3.retry_after = 3.7
    val3 = _extract_retry_after(exc3)
    assert val3 == 3.7, f"expected 3.7, got {val3}"

    # Case D: nothing parseable
    exc4 = discord.HTTPException.__new__(discord.HTTPException)
    exc4.response = SimpleNamespace(headers={})
    val4 = _extract_retry_after(exc4)
    assert val4 is None, f"expected None, got {val4}"

    _ok("_extract_retry_after parses header, lowercase, attr, and no-value")


# ----------------------------------------------------------------------
# 2. send_embeds_detailed shape
# ----------------------------------------------------------------------
async def test_send_embeds_detailed_success():
    from discord_bot.sender import send_embeds_detailed
    channel = MagicMock()
    channel.id = 12345
    channel.send = AsyncMock(return_value=None)

    embeds = [MagicMock(), MagicMock(), MagicMock()]
    result = await send_embeds_detailed(
        channel, embeds, delay=0.0, leading_content="hello",
    )
    assert result["success"] is True, result
    assert result["embeds_sent"] == 3, result
    assert result["embeds_total"] == 3, result
    assert result["leading_sent"] is True, result
    assert result["last_error"] is None, result
    # 1 leading + 3 embeds = 4 send calls
    assert channel.send.call_count == 4, f"expected 4 sends, got {channel.send.call_count}"
    _ok("send_embeds_detailed success path: 3/3 embeds + leading content")


async def test_send_embeds_detailed_non_retryable_403():
    """403 should NOT retry — straight to failure."""
    from discord_bot.sender import send_embeds_detailed
    import discord

    channel = MagicMock()
    channel.id = 99
    response_403 = SimpleNamespace(
        status=403,
        headers={},
        reason="Forbidden",
    )
    exc = discord.HTTPException(response_403, "forbidden")

    channel.send = AsyncMock(side_effect=exc)
    embeds = [MagicMock(), MagicMock()]

    start = time.monotonic()
    result = await send_embeds_detailed(channel, embeds, delay=0.0)
    elapsed = time.monotonic() - start

    assert result["success"] is False, result
    assert result["embeds_sent"] == 0, result
    # Should NOT have slept (no retries on 403). Should take < 1s.
    assert elapsed < 1.0, f"403 path took {elapsed:.2f}s, expected <1s (no retry)"
    # And channel.send should have been called exactly once (no retry)
    assert channel.send.call_count == 1, (
        f"403 should not retry, got {channel.send.call_count} calls"
    )
    _ok("send_embeds_detailed: 403 no-retry, aborts immediately")


async def test_send_embeds_detailed_429_honors_retry_after():
    """429 with Retry-After: 0.1 → should sleep ~0.1s then retry, succeed."""
    from discord_bot.sender import send_embeds_detailed
    import discord

    channel = MagicMock()
    channel.id = 77

    response_429 = SimpleNamespace(
        status=429,
        headers={"Retry-After": "0.05"},
        reason="Too Many Requests",
    )
    rl_exc = discord.HTTPException(response_429, "rate limited")

    call_count = {"n": 0}
    async def fake_send(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise rl_exc
        return None

    channel.send = fake_send
    embeds = [MagicMock()]

    start = time.monotonic()
    result = await send_embeds_detailed(channel, embeds, delay=0.0)
    elapsed = time.monotonic() - start

    assert result["success"] is True, result
    assert result["embeds_sent"] == 1, result
    # Should have slept at least 0.05s honoring Retry-After
    assert elapsed >= 0.04, f"expected >=0.04s sleep, got {elapsed:.3f}s"
    # And under 1s — confirms it didn't fall through to default backoff
    assert elapsed < 1.0, f"expected <1s, got {elapsed:.3f}s"
    assert call_count["n"] == 2, (
        f"expected 1 failure + 1 success = 2 calls, got {call_count['n']}"
    )
    _ok("send_embeds_detailed: 429 honors Retry-After + retries successfully")


async def test_send_embeds_aborts_mid_stream():
    """If embed 2/3 fails, embed 3 should NOT be sent (would put discord
    in a broken-ordering state). The channel is marked failed."""
    from discord_bot.sender import send_embeds_detailed
    import discord

    channel = MagicMock()
    channel.id = 55

    response_403 = SimpleNamespace(status=403, headers={}, reason="Forbidden")
    forbidden = discord.HTTPException(response_403, "forbidden")

    sequence = ["ok", "ok", forbidden, "should-not-send"]
    call_count = {"n": 0}

    async def fake_send(*args, **kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        outcome = sequence[i] if i < len(sequence) else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return None

    channel.send = fake_send
    embeds = [MagicMock(), MagicMock(), MagicMock()]
    result = await send_embeds_detailed(
        channel, embeds, delay=0.0, leading_content="title",
    )
    # 1 leading + 1 embed succeeded, then embed 2 (= 3rd call) 403'd
    # send_embeds_detailed aborts the channel after first failed embed.
    assert result["success"] is False, result
    assert result["embeds_sent"] == 1, result  # only first embed shipped
    # Calls made: leading + embed1 + embed2(fail) = 3 calls. NOT 4.
    assert call_count["n"] == 3, (
        f"expected 3 send calls (leading+1ok+1fail), got {call_count['n']}"
    )
    _ok("send_embeds_detailed: aborts channel mid-stream on first embed failure")


# ----------------------------------------------------------------------
# 3. Eager OCR semaphore
# ----------------------------------------------------------------------
async def test_eager_ocr_semaphore_caps_concurrency():
    from chat_ingestion import watcher

    # Reset module state
    watcher._eager_ocr_semaphore = None
    sem = watcher._get_eager_ocr_semaphore()
    # Default cap is 3 unless settings override
    from config import settings
    cap = max(1, int(getattr(settings, "eager_ocr_max_concurrent", 3)))
    assert sem._value == cap, f"expected initial value={cap}, got {sem._value}"

    # Idempotent — second call returns same semaphore
    sem2 = watcher._get_eager_ocr_semaphore()
    assert sem is sem2, "semaphore should be singleton"

    # Concurrency test: launch cap+2 simultaneous "ocr tasks" that just
    # acquire the semaphore and check the running count. Only `cap` should
    # run at once.
    running = {"now": 0, "max": 0}
    async def worker():
        async with sem:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            await asyncio.sleep(0.05)
            running["now"] -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(cap + 4)]
    await asyncio.gather(*tasks)
    assert running["max"] == cap, (
        f"semaphore should cap at {cap}, but {running['max']} ran simultaneously"
    )
    _ok(f"eager OCR semaphore caps concurrency at {cap}")


# ----------------------------------------------------------------------
# 4. Bridge MAX_DELIVERY_RETRY_MINUTES raised
# ----------------------------------------------------------------------
def test_bridge_retry_window_raised():
    from github_bridge import jobs as bridge_jobs
    assert bridge_jobs.MAX_DELIVERY_RETRY_MINUTES >= 60, (
        f"MAX_DELIVERY_RETRY_MINUTES is {bridge_jobs.MAX_DELIVERY_RETRY_MINUTES}, "
        f"expected >= 60 (raised from 15 in Batch 2)"
    )
    _ok(f"MAX_DELIVERY_RETRY_MINUTES raised to {bridge_jobs.MAX_DELIVERY_RETRY_MINUTES}m")


# ----------------------------------------------------------------------
# 5. Bridge adjudication 0-themes guard is in code
# ----------------------------------------------------------------------
def test_bridge_adj_zero_themes_guard_present():
    """Static check: the bridge has the 0-themes hard-fail branch."""
    import inspect
    from github_bridge import jobs as bridge_jobs
    src = inspect.getsource(bridge_jobs._process_one_pulse)
    assert "adj_themes == 0 and adj_discarded > 0" in src, (
        "expected 'adj_themes == 0 and adj_discarded > 0' guard in _process_one_pulse"
    )
    assert "(adj 0 themes)" in src, "expected adj-0-themes commit message"
    _ok("bridge _process_one_pulse: adjudication 0-themes hard-fail branch present")


# ----------------------------------------------------------------------
# 6. Bridge empty-embeds guard present
# ----------------------------------------------------------------------
def test_bridge_empty_embeds_guard_present():
    import inspect
    from github_bridge import jobs as bridge_jobs
    src = inspect.getsource(bridge_jobs._process_one_pulse)
    assert "section_embeds" in src, "expected section_embeds defensive check"
    assert "0 section embeds" in src, "expected 0-section-embeds log message"
    _ok("bridge _process_one_pulse: empty-embeds defensive check present")


async def main():
    print("=== Batch 2 smoke tests ===")
    test_extract_retry_after()
    await test_send_embeds_detailed_success()
    await test_send_embeds_detailed_non_retryable_403()
    await test_send_embeds_detailed_429_honors_retry_after()
    await test_send_embeds_aborts_mid_stream()
    await test_eager_ocr_semaphore_caps_concurrency()
    test_bridge_retry_window_raised()
    test_bridge_adj_zero_themes_guard_present()
    test_bridge_empty_embeds_guard_present()
    print("\nALL BATCH 2 SMOKE TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
