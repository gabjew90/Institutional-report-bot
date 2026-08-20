"""Concurrency and rate limit management for Gemini API calls.

(Docstring corrected 2026-08-20 — this has always gated Gemini, not
the Anthropic SDK; the google-genai client retries its own 429s.)
"""

import asyncio
import logging
import time
from collections import deque

log = logging.getLogger(__name__)


class RateLimiter:
    """Controls concurrency and requests-per-minute for Gemini calls.

    The SDK handles 429 retries internally; this prevents flooding the
    API in the first place. Sleeping happens while holding the lock —
    intentional: during backpressure every waiter should queue behind
    the window, not race it.
    """

    def __init__(self, max_concurrent: int = 5, rpm_limit: int = 50):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._rpm_limit = rpm_limit
        self._request_times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until we can make a request (respecting both concurrency and RPM)."""
        await self._semaphore.acquire()

        async with self._lock:
            # Loop until the window genuinely has room (2026-08-20: the
            # old code slept once and appended without re-checking, so a
            # burst arriving during the sleep could overshoot the RPM).
            while True:
                now = time.monotonic()
                # Remove timestamps older than 60 seconds
                while self._request_times and self._request_times[0] < now - 60:
                    self._request_times.popleft()
                if len(self._request_times) < self._rpm_limit:
                    break
                wait_time = 60 - (now - self._request_times[0])
                log.info(f"RPM limit reached, waiting {wait_time:.1f}s")
                await asyncio.sleep(max(wait_time, 0.05))

            self._request_times.append(time.monotonic())

    def release(self) -> None:
        """Release the concurrency slot."""
        self._semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()
